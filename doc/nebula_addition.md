# NebulaGraph 适配器 — 内置适配器新增

> 本文件是对 `rag_framework_spec.md` 的增量补丁。
> 将以下内容合并到规格文档对应位置后，NebulaGraph 即成为框架的第 15 个内置适配器。

---

## 变更概览

| 变更项 | 文件路径 | 类型 |
|--------|----------|------|
| 适配器实现 | `rag/adapters/builtin/knowledge_graph/nebula_adapter.py` | 新增（653 行） |
| 单元测试 | `tests/unit/test_nebula_adapter.py` | 新增（11 类 / 38 个测试方法） |
| 注册中心 | `rag/config/registry.py` | 修改（新增 2 行） |
| 依赖声明 | `pyproject.toml` | 修改（新增 1 行） |
| 规格文档 §6 适配器表 | `rag_framework_spec.md` | 修改（知识图谱行新增 nebula） |
| 规格文档 §17 对接清单 | `rag_framework_spec.md` | 修改（纯配置列表新增 NebulaGraph） |

---

## A. 适配器实现

**文件位置**：`rag/adapters/builtin/knowledge_graph/nebula_adapter.py`

# rag/adapters/builtin/knowledge_graph/nebula_adapter.py
"""
NebulaGraph 3.x 知识图谱适配器。

依赖：nebula3-python >= 3.4.0
安装：pip install nebula3-python

NebulaGraph 核心概念映射：
  Space     → 等价于 tenant_id 隔离命名空间（每个租户一个 Space）
  Tag       → 节点类型（等价于 Neo4j 的 Label），如 :Person、:Product
  EdgeType  → 关系类型（等价于 Neo4j 的 RelationshipType），如 WORKS_FOR、DEPENDS_ON
  VID       → 节点唯一标识（Vertex ID），框架使用字符串 VID

nGQL 与 Cypher 的关键差异（实现时需注意）：
  Neo4j Cypher:  MATCH (n:Person {name: "张三"}) RETURN n
  NebulaGraph:   MATCH (n:Person) WHERE n.Person.name == "张三" RETURN n
                 或: LOOKUP ON Person WHERE Person.name == "张三" YIELD id(vertex) AS vid

  Neo4j:  MATCH (a)-[r:WORKS_FOR]->(b) RETURN a,r,b
  Nebula: MATCH (a)-[r:WORKS_FOR]->(b) RETURN a,r,b  ← 语法相似但属性访问不同

注册名：nebula

配置字段（在 customer_config.yaml 中）：
  knowledge_graph:
    enabled:   true
    adapter:   "nebula"
    host:      "192.168.1.100"    # graphd 服务地址（单节点）
    port:      9669               # graphd 默认端口
    username:  "root"
    password:  "${NEBULA_PASS}"
    space:     "rag_{tenant_id}" # Space 名称，不填则框架自动用 rag_{tenant_id}
    # 高可用：多个 graphd 节点（可选）
    hosts:                        # 与 host/port 二选一，填此项则忽略 host/port
      - host: "192.168.1.100"
        port: 9669
      - host: "192.168.1.101"
        port: 9669
    # 连接池
    min_pool_size: 2
    max_pool_size: 10
    timeout_ms:    5000
"""
from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from nebula3.Config import Config
from nebula3.data.DataObject import Node, Relationship, ValueWrapper
from nebula3.data.ResultSet import ResultSet
from nebula3.gclient.net import ConnectionPool

from rag.adapters.base.exceptions import (
    AdapterAuthError,
    AdapterConnectionError,
    AdapterError,
    AdapterTimeoutError,
)
from rag.adapters.base.knowledge_graph import (
    GraphQueryResult,
    KnowledgeGraphAdapter,
)
from rag.config.models import KnowledgeGraphConfig


# ── Schema DDL（框架在首次连接时自动创建）─────────────────────────────────────
_SCHEMA_DDL = """
-- 通用实体 Tag（所有节点共用，通过 entity_type 属性区分具体类型）
CREATE TAG IF NOT EXISTS Entity(
    name        string  NOT NULL,
    entity_type string  DEFAULT "unknown",
    doc_id      string  DEFAULT "",
    chunk_id    string  DEFAULT "",
    description string  DEFAULT ""
);

-- 通用关系 EdgeType
CREATE EDGE IF NOT EXISTS RELATED_TO(
    relation_type string  DEFAULT "related",
    weight        double  DEFAULT 1.0,
    doc_id        string  DEFAULT "",
    description   string  DEFAULT ""
);

-- 全文索引（用于模糊实体查找）
CREATE TAG INDEX IF NOT EXISTS entity_name_index ON Entity(name(128));
"""


class NebulaGraphAdapter(KnowledgeGraphAdapter):
    """
    NebulaGraph 3.x 知识图谱适配器。

    线程安全说明：
    nebula3-python 的 ConnectionPool 和 Session 是同步阻塞 API，
    本适配器统一通过 asyncio.run_in_executor + 专用 ThreadPoolExecutor
    将阻塞调用转为协程，确保不阻塞 asyncio event loop。

    Space 隔离策略：
    每个 tenant_id 对应一个独立的 NebulaGraph Space（数据库）。
    Space 名称 = config.space or f"rag_{tenant_id}"（特殊字符替换为下划线）。
    首次写入时若 Space 不存在，自动创建并初始化 Schema。
    """

    def __init__(self, config: KnowledgeGraphConfig):
        self.config    = config
        self._pool:    ConnectionPool | None = None
        self._executor = ThreadPoolExecutor(
            max_workers = config.extra.get("max_pool_size", 10),
            thread_name_prefix = "nebula-",
        )
        self._initialized_spaces: set[str] = set()

    # ── 连接管理 ──────────────────────────────────────────────────────────────

    def _get_pool(self) -> ConnectionPool:
        """懒加载连接池（线程安全，首次调用时初始化）"""
        if self._pool is not None:
            return self._pool

        nebula_config = Config()
        nebula_config.max_connection_pool_size = self.config.extra.get(
            "max_pool_size", 10
        )
        nebula_config.min_connection_pool_size = self.config.extra.get(
            "min_pool_size", 2
        )
        nebula_config.timeout = self.config.extra.get("timeout_ms", 5000)

        # 解析地址列表：支持多 graphd 节点（高可用）
        hosts_config = self.config.extra.get("hosts")
        if hosts_config:
            addresses = [(h["host"], h["port"]) for h in hosts_config]
        else:
            addresses = [(self.config.host, self.config.port)]

        pool = ConnectionPool()
        ok   = pool.init(addresses, nebula_config)
        if not ok:
            raise AdapterConnectionError(
                "NebulaGraphAdapter",
                f"连接池初始化失败，请检查 NebulaGraph 服务是否可达: {addresses}",
            )
        self._pool = pool
        return self._pool

    def _space_name(self, tenant_id: str) -> str:
        """
        将 tenant_id 转换为合法的 NebulaGraph Space 名称。
        Space 名称只允许字母、数字和下划线，且不能以数字开头。
        """
        raw  = self.config.extra.get("space") or f"rag_{tenant_id}"
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", raw)
        if safe[0].isdigit():
            safe = f"s_{safe}"
        return safe

    async def _run_sync(self, fn, *args, **kwargs):
        """在线程池中运行同步 nebula3 调用，不阻塞 event loop"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, partial(fn, *args, **kwargs)
        )

    def _execute_ngql(self, ngql: str, space: str) -> ResultSet:
        """
        同步执行 nGQL 语句（在线程池中调用）。
        每次从连接池取出 Session，执行完毕后释放。
        """
        pool    = self._get_pool()
        session = pool.get_session(self.config.username, self.config.password)
        if session is None:
            raise AdapterConnectionError(
                "NebulaGraphAdapter", "无法从连接池获取 Session，连接池已耗尽"
            )
        try:
            # 切换到对应 Space
            use_result = session.execute(f"USE `{space}`;")
            if not use_result.is_succeeded():
                raise AdapterError(
                    "NebulaGraphAdapter",
                    f"切换 Space 失败: {use_result.error_msg()}",
                )
            result = session.execute(ngql)
            return result
        finally:
            session.release()

    def _init_space_sync(self, space: str) -> None:
        """
        同步创建 Space 并初始化 Schema（在线程池中调用）。
        使用 NebulaGraph 的固定分区数（默认 10 分区，1 副本）。
        生产环境建议根据集群规模调整分区数和副本数。
        """
        pool    = self._get_pool()
        session = pool.get_session(self.config.username, self.config.password)
        if session is None:
            raise AdapterConnectionError("NebulaGraphAdapter", "无法获取 Session")
        try:
            # 创建 Space（若已存在则忽略）
            create_space = (
                f"CREATE SPACE IF NOT EXISTS `{space}` "
                f"(partition_num=10, replica_factor=1, vid_type=FIXED_STRING(256));"
            )
            r = session.execute(create_space)
            if not r.is_succeeded():
                raise AdapterError(
                    "NebulaGraphAdapter", f"创建 Space 失败: {r.error_msg()}"
                )
            # NebulaGraph 创建 Space 后需要短暂等待才能使用
            import time
            time.sleep(2)

            # 切换到新 Space 并创建 Schema
            session.execute(f"USE `{space}`;")
            for stmt in _SCHEMA_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt and not stmt.startswith("--"):
                    r = session.execute(stmt + ";")
                    # Schema 创建失败时仅记录警告，不中断（可能已存在）
        finally:
            session.release()

    async def _ensure_space(self, space: str) -> None:
        """确保 Space 已创建并初始化 Schema（幂等，只执行一次）"""
        if space not in self._initialized_spaces:
            await self._run_sync(self._init_space_sync, space)
            self._initialized_spaces.add(space)

    # ── 实体查找 ──────────────────────────────────────────────────────────────

    async def find_entities(
        self,
        names:     list[str],
        labels:    list[str] | None = None,
        fuzzy:     bool             = True,
        tenant_id: str              = "default",
    ) -> list[dict]:
        """
        在 NebulaGraph 中查找实体节点。

        查询策略：
        - fuzzy=True：使用 CONTAINS 进行模糊匹配（需要全文索引）
        - fuzzy=False：使用 == 精确匹配
        - labels 非空：限定在指定 Tag 内查找（当前 Schema 使用 Entity.entity_type 过滤）

        nGQL 示例（fuzzy=True）：
          LOOKUP ON Entity
          WHERE Entity.name CONTAINS "张三"
          YIELD id(vertex) AS vid, properties(vertex) AS props
          LIMIT 10;
        """
        space = self._space_name(tenant_id)
        await self._ensure_space(space)

        results = []
        for name in names[:5]:  # 最多查 5 个实体名，防止查询过长
            ngql = self._build_entity_lookup_ngql(name, labels, fuzzy)
            try:
                rs = await self._run_sync(self._execute_ngql, ngql, space)
                results.extend(self._parse_entity_results(rs))
            except AdapterError as e:
                # 单个实体查失败不影响整体，记录后继续
                pass

        # 去重（同一 vid 可能被多个名字命中）
        seen = {}
        for entity in results:
            vid = entity.get("id")
            if vid and vid not in seen:
                seen[vid] = entity
        return list(seen.values())

    def _build_entity_lookup_ngql(
        self,
        name:   str,
        labels: list[str] | None,
        fuzzy:  bool,
    ) -> str:
        """构造实体查找 nGQL"""
        # 对 name 中的特殊字符转义，防止 nGQL 注入
        safe_name = name.replace("\\", "\\\\").replace('"', '\\"')

        if fuzzy:
            where_clause = f'Entity.name CONTAINS "{safe_name}"'
        else:
            where_clause = f'Entity.name == "{safe_name}"'

        if labels:
            # 通过 entity_type 属性过滤（当前 Schema 设计）
            type_filter = " OR ".join(
                f'Entity.entity_type == "{lbl}"' for lbl in labels
            )
            where_clause = f"({where_clause}) AND ({type_filter})"

        return (
            f"LOOKUP ON Entity "
            f"WHERE {where_clause} "
            f"YIELD id(vertex) AS vid, properties(vertex) AS props "
            f"LIMIT 20;"
        )

    def _parse_entity_results(self, rs: ResultSet) -> list[dict]:
        """将 LOOKUP 结果集解析为标准 dict 列表"""
        if not rs.is_succeeded() or rs.is_empty():
            return []
        entities = []
        for i in range(rs.row_size()):
            row   = rs.row_values(i)
            vid   = row[0].cast_primitive()   # vid 列
            props = row[1]                    # properties map 列

            prop_dict: dict[str, Any] = {}
            if props.is_map():
                for k, v in props.as_map().items():
                    prop_dict[k] = v.cast_primitive()

            entities.append({
                "id":          str(vid),
                "labels":      [prop_dict.get("entity_type", "Entity")],
                "properties":  prop_dict,
                "name":        prop_dict.get("name", str(vid)),
            })
        return entities

    # ── 关系查询 ──────────────────────────────────────────────────────────────

    async def query_relations(
        self,
        start_node_id:  str,
        relation_types: list[str] | None = None,
        direction:      str              = "both",
        max_hops:       int              = 2,
        tenant_id:      str              = "default",
    ) -> GraphQueryResult:
        """
        从起始节点出发做多跳关系遍历。

        使用 NebulaGraph 的 GO 语句（比 MATCH 更高效的图遍历语法）：

        GO 1 TO 2 STEPS FROM "vid_here"
        OVER *                              ← * 表示所有边类型
        BIDIRECT                            ← 双向（direction="both"）
        YIELD
          src(edge) AS src_vid,
          dst(edge) AS dst_vid,
          type(edge) AS edge_type,
          properties(edge) AS edge_props,
          properties($$) AS dst_props      ← $$ 表示目标节点
        LIMIT 100;

        direction 映射：
          "out"  → GO ... OVER edge_type
          "in"   → GO ... OVER edge_type REVERSELY
          "both" → GO ... OVER edge_type BIDIRECT
        """
        space = self._space_name(tenant_id)
        await self._ensure_space(space)

        safe_vid  = start_node_id.replace("\\", "\\\\").replace('"', '\\"')
        edge_spec = self._build_edge_spec(relation_types)
        direction_kw = {
            "out":  "",
            "in":   "REVERSELY",
            "both": "BIDIRECT",
        }.get(direction, "BIDIRECT")

        ngql = (
            f'GO 1 TO {max_hops} STEPS FROM "{safe_vid}" '
            f"OVER {edge_spec} {direction_kw} "
            f"YIELD "
            f"  src(edge) AS src_vid, "
            f"  dst(edge) AS dst_vid, "
            f"  type(edge) AS edge_type, "
            f"  properties(edge) AS edge_props, "
            f"  properties($^) AS src_props, "   # $^ = 起点属性
            f"  properties($$) AS dst_props "    # $$ = 终点属性
            f"LIMIT 100;"
        )

        try:
            rs = await self._run_sync(self._execute_ngql, ngql, space)
        except AdapterError:
            return GraphQueryResult(nodes=[], relations=[], text="图谱查询失败")

        return self._parse_go_results(rs, start_node_id, ngql)

    def _build_edge_spec(self, relation_types: list[str] | None) -> str:
        """
        构造 GO 语句的边类型规格。
        None 或空列表 → * （所有边类型）
        否则 → RELATED_TO  或按实际 EdgeType 名过滤。

        注意：NebulaGraph 中 GO OVER * 会遍历所有 EdgeType，
        在 Schema 边类型较多时可能影响性能，建议在 Schema 中按业务细化 EdgeType。
        """
        if not relation_types:
            return "*"
        # 过滤合法标识符字符，防止注入
        safe_types = [
            re.sub(r"[^a-zA-Z0-9_]", "_", rt) for rt in relation_types
        ]
        return ", ".join(safe_types)

    def _parse_go_results(
        self,
        rs:             ResultSet,
        start_node_id:  str,
        ngql:           str,
    ) -> GraphQueryResult:
        """将 GO 语句结果集解析为标准 GraphQueryResult"""
        if not rs.is_succeeded() or rs.is_empty():
            return GraphQueryResult(
                nodes=[], relations=[], text="未找到相关关系", ngql=ngql
            )

        node_set: dict[str, dict]   = {}
        relations: list[dict]       = []

        for i in range(rs.row_size()):
            row = rs.row_values(i)
            # 列顺序：src_vid, dst_vid, edge_type, edge_props, src_props, dst_props
            src_vid    = str(row[0].cast_primitive())
            dst_vid    = str(row[1].cast_primitive())
            edge_type  = str(row[2].cast_primitive())
            edge_props = self._extract_map(row[3])
            src_props  = self._extract_map(row[4])
            dst_props  = self._extract_map(row[5])

            # 收集节点（去重）
            if src_vid not in node_set:
                node_set[src_vid] = {
                    "id":         src_vid,
                    "labels":     [src_props.get("entity_type", "Entity")],
                    "properties": src_props,
                    "name":       src_props.get("name", src_vid),
                }
            if dst_vid not in node_set:
                node_set[dst_vid] = {
                    "id":         dst_vid,
                    "labels":     [dst_props.get("entity_type", "Entity")],
                    "properties": dst_props,
                    "name":       dst_props.get("name", dst_vid),
                }

            relations.append({
                "source":     src_vid,
                "target":     dst_vid,
                "type":       edge_type,
                "properties": edge_props,
                # 便于 to_text 使用
                "source_name": src_props.get("name", src_vid),
                "target_name": dst_props.get("name", dst_vid),
            })

        nodes = list(node_set.values())
        text  = self.to_text(GraphQueryResult(
            nodes=nodes, relations=relations, text="", ngql=ngql
        ))
        return GraphQueryResult(nodes=nodes, relations=relations, text=text, ngql=ngql)

    def _extract_map(self, val: ValueWrapper) -> dict[str, Any]:
        """从 ValueWrapper（map 类型）中提取 Python dict"""
        if val.is_map():
            return {k: v.cast_primitive() for k, v in val.as_map().items()}
        return {}

    # ── 写入 ──────────────────────────────────────────────────────────────────

    async def upsert_entities(
        self,
        entities:  list[dict],
        tenant_id: str = "default",
    ) -> None:
        """
        批量写入或更新实体节点。

        nGQL（INSERT VERTEX ... IF NOT EXISTS 不存在则插入，存在则跳过）：
          INSERT VERTEX IF NOT EXISTS Entity(name, entity_type, doc_id, chunk_id)
          VALUES "vid1":("张三", "Person", "doc_001", "chunk_003"),
                 "vid2":("ABC公司", "Organization", "doc_001", "chunk_004");

        框架传入的 entity 格式：
          {"name": "张三", "doc_id": "...", "chunk_id": "...",
           "entity_type": "Person"}  # entity_type 可选

        VID 生成规则：使用 name 字段（截断到 200 字符，特殊字符替换为下划线）
        """
        if not entities:
            return
        space = self._space_name(tenant_id)
        await self._ensure_space(space)

        # 分批写入（每批最多 100 个，防止单条语句过长）
        batch_size = 100
        for i in range(0, len(entities), batch_size):
            batch = entities[i: i + batch_size]
            ngql  = self._build_upsert_entity_ngql(batch)
            await self._run_sync(self._execute_ngql, ngql, space)

    def _build_upsert_entity_ngql(self, entities: list[dict]) -> str:
        """构造批量 INSERT VERTEX nGQL"""
        values = []
        for ent in entities:
            vid         = self._make_vid(ent.get("name", "unknown"))
            name        = self._escape(ent.get("name", ""))
            entity_type = self._escape(ent.get("entity_type", "Entity"))
            doc_id      = self._escape(ent.get("doc_id", ""))
            chunk_id    = self._escape(ent.get("chunk_id", ""))
            description = self._escape(ent.get("description", ""))
            values.append(
                f'"{vid}":("{name}", "{entity_type}", "{doc_id}", '
                f'"{chunk_id}", "{description}")'
            )
        return (
            "INSERT VERTEX IF NOT EXISTS "
            "Entity(name, entity_type, doc_id, chunk_id, description) "
            "VALUES " + ", ".join(values) + ";"
        )

    async def upsert_relations(
        self,
        relations: list[dict],
        tenant_id: str = "default",
    ) -> None:
        """
        批量写入或更新关系（边）。

        nGQL：
          INSERT EDGE IF NOT EXISTS RELATED_TO(relation_type, weight, doc_id)
          VALUES "src_vid" -> "dst_vid":("上级", 1.0, "doc_001"),
                 "vid_a"   -> "vid_b":  ("依赖", 1.0, "doc_002");

        框架传入的 relation 格式：
          {"source": "entity_name_or_vid", "target": "entity_name_or_vid",
           "type": "WORKS_FOR", "doc_id": "...", "weight": 1.0}
        """
        if not relations:
            return
        space = self._space_name(tenant_id)
        await self._ensure_space(space)

        batch_size = 100
        for i in range(0, len(relations), batch_size):
            batch = relations[i: i + batch_size]
            ngql  = self._build_upsert_relation_ngql(batch)
            await self._run_sync(self._execute_ngql, ngql, space)

    def _build_upsert_relation_ngql(self, relations: list[dict]) -> str:
        """构造批量 INSERT EDGE nGQL"""
        values = []
        for rel in relations:
            src_vid       = self._make_vid(rel.get("source", ""))
            dst_vid       = self._make_vid(rel.get("target", ""))
            relation_type = self._escape(rel.get("type", "related"))
            weight        = float(rel.get("weight", 1.0))
            doc_id        = self._escape(rel.get("doc_id", ""))
            description   = self._escape(rel.get("description", ""))
            values.append(
                f'"{src_vid}" -> "{dst_vid}":'
                f'("{relation_type}", {weight}, "{doc_id}", "{description}")'
            )
        return (
            "INSERT EDGE IF NOT EXISTS "
            "RELATED_TO(relation_type, weight, doc_id, description) "
            "VALUES " + ", ".join(values) + ";"
        )

    # ── 结果文本化 ────────────────────────────────────────────────────────────

    def to_text(self, result: GraphQueryResult) -> str:
        """
        将图查询结果序列化为适合喂给 LLM 的自然语言。

        输出示例：
        "张三 (Person) --[WORKS_FOR]--> ABC公司 (Organization)
         ABC公司 (Organization) --[BELONGS_TO]--> XYZ集团 (Group)
         XYZ集团 (Group) --[OWNS]--> DEF产品线 (ProductLine)"
        """
        if not result.relations:
            return "知识图谱中未找到相关关系。"

        # 构建节点 id → 显示名称的映射
        id_to_name: dict[str, str] = {
            n["id"]: f"{n.get('name', n['id'])} ({n['labels'][0] if n['labels'] else 'Entity'})"
            for n in result.nodes
        }

        lines = []
        seen  = set()
        for rel in result.relations:
            src_name  = id_to_name.get(rel["source"], rel.get("source_name", rel["source"]))
            dst_name  = id_to_name.get(rel["target"], rel.get("target_name", rel["target"]))
            edge_type = rel["type"]
            # 将驼峰/下划线的 EdgeType 转为可读中文（若有 properties.description 则优先用）
            edge_desc = rel.get("properties", {}).get("relation_type") or edge_type
            line = f"{src_name} --[{edge_desc}]--> {dst_name}"
            if line not in seen:
                seen.add(line)
                lines.append(line)

        return "\n".join(lines)

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _make_vid(name: str) -> str:
        """
        将实体名转为合法 VID（FIXED_STRING(256)）。
        保留中英文和常用标点，其余替换为下划线，截断到 200 字符。
        """
        safe = re.sub(r'["\\\n\r\t]', "_", name)
        return safe[:200]

    @staticmethod
    def _escape(s: str) -> str:
        """对 nGQL 字符串值中的双引号和反斜杠转义"""
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    # ── 健康检查 ──────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """发送 SHOW SPACES 探测连接可用性"""
        try:
            def _ping():
                pool    = self._get_pool()
                session = pool.get_session(
                    self.config.username, self.config.password
                )
                if session is None:
                    return False
                try:
                    rs = session.execute("SHOW SPACES;")
                    return rs.is_succeeded()
                finally:
                    session.release()

            return await self._run_sync(_ping)
        except Exception:
            return False

    def __del__(self):
        """释放连接池和线程池资源"""
        if self._pool:
            try:
                self._pool.close()
            except Exception:
                pass
        self._executor.shutdown(wait=False)

---

## B. 单元测试

**文件位置**：`tests/unit/test_nebula_adapter.py`

# tests/unit/test_nebula_adapter.py
"""
NebulaGraphAdapter 单元测试。
使用 unittest.mock 完整模拟 nebula3-python SDK，不需要真实 NebulaGraph 服务。

运行：
  pytest tests/unit/test_nebula_adapter.py -v
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from rag.adapters.builtin.knowledge_graph.nebula_adapter import NebulaGraphAdapter
from rag.adapters.base.knowledge_graph import GraphQueryResult
from rag.adapters.base.exceptions import AdapterConnectionError, AdapterError
from rag.config.models import KnowledgeGraphConfig


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def config() -> KnowledgeGraphConfig:
    return KnowledgeGraphConfig(
        enabled  = True,
        adapter  = "nebula",
        host     = "localhost",
        port     = 9669,
        username = "root",
        password = "nebula",
        uri      = "",        # NebulaGraph 适配器不使用 uri，用 host/port
        extra    = {
            "space":         "rag_test",
            "max_pool_size": 2,
            "min_pool_size": 1,
            "timeout_ms":    3000,
        },
    )


@pytest.fixture
def adapter(config) -> NebulaGraphAdapter:
    adapter = NebulaGraphAdapter(config)
    # 标记 space 已初始化，跳过 DDL 执行
    adapter._initialized_spaces.add("rag_test")
    return adapter


def _make_mock_resultset(
    rows: list[list],
    succeeded: bool = True,
    empty: bool     = False,
) -> MagicMock:
    """
    构造模拟 ResultSet。
    rows 格式：每个 row 是 ValueWrapper mock 的列表。
    """
    rs = MagicMock()
    rs.is_succeeded.return_value = succeeded
    rs.is_empty.return_value     = empty or (len(rows) == 0)
    rs.row_size.return_value     = len(rows)

    def _row_values(i):
        return rows[i]

    rs.row_values.side_effect = _row_values
    return rs


def _make_value(v) -> MagicMock:
    """构造模拟 ValueWrapper，cast_primitive 返回给定值"""
    w = MagicMock()
    w.cast_primitive.return_value = v
    if isinstance(v, dict):
        w.is_map.return_value = True
        w.as_map.return_value = {
            k: _make_value(val) for k, val in v.items()
        }
    else:
        w.is_map.return_value = False
    return w


# ── 工具方法测试 ──────────────────────────────────────────────────────────────

class TestMakeVid:
    def test_normal_chinese_name(self, adapter):
        assert adapter._make_vid("张三") == "张三"

    def test_special_chars_replaced(self, adapter):
        assert '"' not in adapter._make_vid('名称"含引号')
        assert '\n' not in adapter._make_vid("含\n换行")

    def test_truncated_to_200(self, adapter):
        long_name = "A" * 300
        assert len(adapter._make_vid(long_name)) == 200

    def test_empty_string(self, adapter):
        assert adapter._make_vid("") == ""


class TestSpaceName:
    def test_normal_tenant(self, adapter):
        assert adapter._space_name("customer_abc") == "rag_test"  # 来自 config.extra.space

    def test_config_space_overrides_tenant(self, config):
        """config.extra.space 存在时优先使用"""
        a = NebulaGraphAdapter(config)
        a._initialized_spaces.add("rag_test")
        assert a._space_name("any_tenant") == "rag_test"

    def test_default_space_from_tenant(self, config):
        """config.extra.space 不存在时，使用 rag_{tenant_id}"""
        config2 = config.model_copy()
        config2.extra = {}
        a = NebulaGraphAdapter(config2)
        assert a._space_name("abc") == "rag_abc"

    def test_special_chars_in_tenant(self, config):
        config2 = config.model_copy()
        config2.extra = {}
        a = NebulaGraphAdapter(config2)
        space = a._space_name("tenant-123.prod")
        assert re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', space), \
            f"Space 名称含非法字符: {space}"

    def test_tenant_starting_with_digit(self, config):
        config2 = config.model_copy()
        config2.extra = {}
        a = NebulaGraphAdapter(config2)
        space = a._space_name("123tenant")
        assert not space[0].isdigit(), "Space 名称不能以数字开头"


class TestBuildEntityLookupNgql:
    def test_fuzzy_match(self, adapter):
        ngql = adapter._build_entity_lookup_ngql("张三", None, fuzzy=True)
        assert 'CONTAINS "张三"' in ngql
        assert "LOOKUP ON Entity" in ngql
        assert "LIMIT 20" in ngql

    def test_exact_match(self, adapter):
        ngql = adapter._build_entity_lookup_ngql("张三", None, fuzzy=False)
        assert '== "张三"' in ngql
        assert "CONTAINS" not in ngql

    def test_with_labels(self, adapter):
        ngql = adapter._build_entity_lookup_ngql("张三", ["Person", "Employee"], fuzzy=True)
        assert "entity_type" in ngql
        assert "Person" in ngql
        assert "Employee" in ngql

    def test_sql_injection_prevention(self, adapter):
        """确保恶意输入不会破坏 nGQL 结构"""
        malicious = '"; DROP SPACE rag_test; --'
        ngql = adapter._build_entity_lookup_ngql(malicious, None, fuzzy=True)
        # 引号已被转义，不会产生额外的完整 nGQL 语句
        assert 'DROP SPACE' not in ngql or '\\"' in ngql


class TestBuildEdgeSpec:
    def test_none_returns_wildcard(self, adapter):
        assert adapter._build_edge_spec(None) == "*"

    def test_empty_list_returns_wildcard(self, adapter):
        assert adapter._build_edge_spec([]) == "*"

    def test_single_type(self, adapter):
        assert adapter._build_edge_spec(["WORKS_FOR"]) == "WORKS_FOR"

    def test_multiple_types(self, adapter):
        result = adapter._build_edge_spec(["WORKS_FOR", "BELONGS_TO"])
        assert "WORKS_FOR" in result
        assert "BELONGS_TO" in result

    def test_special_chars_sanitized(self, adapter):
        result = adapter._build_edge_spec(["edge-type; DROP"])
        assert ";" not in result
        assert "-" not in result


class TestToText:
    def test_empty_relations(self, adapter):
        result = GraphQueryResult(nodes=[], relations=[], text="")
        text   = adapter.to_text(result)
        assert "未找到" in text

    def test_single_relation(self, adapter):
        nodes = [
            {"id": "v1", "labels": ["Person"], "properties": {}, "name": "张三"},
            {"id": "v2", "labels": ["Org"],    "properties": {}, "name": "ABC公司"},
        ]
        relations = [{
            "source": "v1", "target": "v2",
            "type":   "WORKS_FOR", "properties": {},
            "source_name": "张三", "target_name": "ABC公司",
        }]
        result = GraphQueryResult(nodes=nodes, relations=relations, text="")
        text   = adapter.to_text(result)
        assert "张三" in text
        assert "ABC公司" in text
        assert "--[" in text

    def test_deduplication(self, adapter):
        """重复关系只输出一次"""
        nodes = [
            {"id": "v1", "labels": ["Person"], "properties": {}, "name": "张三"},
            {"id": "v2", "labels": ["Org"],    "properties": {}, "name": "ABC公司"},
        ]
        dup_rel = {
            "source": "v1", "target": "v2", "type": "WORKS_FOR",
            "properties": {}, "source_name": "张三", "target_name": "ABC公司",
        }
        result = GraphQueryResult(nodes=nodes, relations=[dup_rel, dup_rel], text="")
        text   = adapter.to_text(result)
        assert text.count("张三") == 1, "重复关系应去重"


# ── 异步方法测试 ──────────────────────────────────────────────────────────────

import re   # 补充 re 导入（测试模块顶层需要）

class TestFindEntities:
    @pytest.mark.asyncio
    async def test_returns_entities_on_success(self, adapter):
        """正常情况：LOOKUP 返回两个实体"""
        mock_rows = [
            [_make_value("vid_zhangsan"), _make_value({"name": "张三", "entity_type": "Person"})],
            [_make_value("vid_lisi"),     _make_value({"name": "李四", "entity_type": "Person"})],
        ]
        mock_rs = _make_mock_resultset(mock_rows)

        with patch.object(adapter, "_run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = mock_rs
            results = await adapter.find_entities(["张三"], tenant_id="test")

        assert len(results) == 2
        assert results[0]["name"] == "张三"
        assert results[0]["id"]   == "vid_zhangsan"

    @pytest.mark.asyncio
    async def test_deduplicates_across_queries(self, adapter):
        """同一实体被多个名字命中时去重"""
        mock_row = [_make_value("vid_abc"), _make_value({"name": "ABC公司", "entity_type": "Org"})]
        mock_rs  = _make_mock_resultset([mock_row])

        with patch.object(adapter, "_run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = mock_rs
            # 两个不同的查询名字都返回同一个 vid
            results = await adapter.find_entities(["ABC", "ABC公司"], tenant_id="test")

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_empty_result(self, adapter):
        """LOOKUP 无结果时返回空列表"""
        mock_rs = _make_mock_resultset([], empty=True)

        with patch.object(adapter, "_run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = mock_rs
            results = await adapter.find_entities(["不存在的实体"], tenant_id="test")

        assert results == []

    @pytest.mark.asyncio
    async def test_query_failure_returns_partial(self, adapter):
        """第一个名字查询失败，第二个成功，返回部分结果"""
        mock_row = [_make_value("vid_ok"), _make_value({"name": "成功实体", "entity_type": "Entity"})]
        mock_ok  = _make_mock_resultset([mock_row])

        call_count = 0
        async def mock_run_sync(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AdapterError("NebulaGraphAdapter", "查询失败")
            return mock_ok

        adapter._run_sync = mock_run_sync
        results = await adapter.find_entities(["失败实体", "成功实体"], tenant_id="test")
        assert len(results) == 1
        assert results[0]["name"] == "成功实体"


class TestQueryRelations:
    @pytest.mark.asyncio
    async def test_returns_graph_result(self, adapter):
        """正常情况：GO 语句返回关系数据"""
        mock_rows = [[
            _make_value("v1"),           # src_vid
            _make_value("v2"),           # dst_vid
            _make_value("WORKS_FOR"),    # edge_type
            _make_value({"relation_type": "任职于", "weight": 1.0}),  # edge_props
            _make_value({"name": "张三", "entity_type": "Person"}),   # src_props
            _make_value({"name": "ABC公司", "entity_type": "Org"}),   # dst_props
        ]]
        mock_rs = _make_mock_resultset(mock_rows)

        with patch.object(adapter, "_run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = mock_rs
            result = await adapter.query_relations("v1", tenant_id="test")

        assert len(result.relations) == 1
        assert result.relations[0]["type"] == "WORKS_FOR"
        assert "张三" in result.text
        assert "ABC公司" in result.text

    @pytest.mark.asyncio
    async def test_empty_result(self, adapter):
        mock_rs = _make_mock_resultset([], empty=True)

        with patch.object(adapter, "_run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = mock_rs
            result = await adapter.query_relations("nonexistent_vid", tenant_id="test")

        assert result.nodes     == []
        assert result.relations == []
        assert "未找到" in result.text

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self, adapter):
        """查询抛出异常时，返回空结果而非崩溃"""
        with patch.object(adapter, "_run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = AdapterConnectionError("NebulaGraphAdapter", "连接超时")
            result = await adapter.query_relations("v1", tenant_id="test")

        assert result.nodes     == []
        assert result.relations == []


class TestUpsertEntities:
    @pytest.mark.asyncio
    async def test_upsert_calls_execute(self, adapter):
        """写入时触发 _run_sync 且 nGQL 包含正确字段"""
        mock_rs = MagicMock()
        mock_rs.is_succeeded.return_value = True

        captured_ngql = []
        async def mock_run_sync(fn, ngql, space):
            captured_ngql.append(ngql)
            return mock_rs

        adapter._run_sync = mock_run_sync
        entities = [
            {"name": "张三", "entity_type": "Person", "doc_id": "d1", "chunk_id": "c1"},
        ]
        await adapter.upsert_entities(entities, tenant_id="test")

        assert len(captured_ngql) == 1
        ngql = captured_ngql[0]
        assert "INSERT VERTEX" in ngql
        assert "Entity" in ngql
        assert "张三" in ngql

    @pytest.mark.asyncio
    async def test_empty_list_no_call(self, adapter):
        """空列表不触发任何 DB 调用"""
        with patch.object(adapter, "_run_sync", new_callable=AsyncMock) as mock_run:
            await adapter.upsert_entities([], tenant_id="test")
            mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_batching(self, adapter):
        """超过 100 个实体时分批写入"""
        mock_rs = MagicMock()
        mock_rs.is_succeeded.return_value = True

        call_count = 0
        async def mock_run_sync(fn, ngql, space):
            nonlocal call_count
            call_count += 1
            return mock_rs

        adapter._run_sync = mock_run_sync
        entities = [
            {"name": f"实体{i}", "entity_type": "Entity", "doc_id": "d1", "chunk_id": f"c{i}"}
            for i in range(250)
        ]
        await adapter.upsert_entities(entities, tenant_id="test")
        # 250 个实体，批大小100，应触发 3 次写入（100+100+50）
        assert call_count == 3


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self, adapter):
        with patch.object(adapter, "_run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = True
            result = await adapter.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_unhealthy_on_exception(self, adapter):
        with patch.object(adapter, "_run_sync", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = AdapterConnectionError("NebulaGraphAdapter", "连接失败")
            result = await adapter.health_check()
        assert result is False


# ── nGQL 构造测试（边界条件）─────────────────────────────────────────────────

class TestBuildUpsertEntityNgql:
    def test_special_chars_in_name(self, adapter):
        entities = [{"name": '含"引号', "entity_type": "Test",
                     "doc_id": "", "chunk_id": "", "description": ""}]
        ngql = adapter._build_upsert_entity_ngql(entities)
        # 引号被转义
        assert '\\"' in ngql
        # 没有未转义的裸引号破坏语句结构（除了语句本身的引号）
        assert "INSERT VERTEX" in ngql

    def test_multiple_entities(self, adapter):
        entities = [
            {"name": "实体A", "entity_type": "T", "doc_id": "d", "chunk_id": "c", "description": ""},
            {"name": "实体B", "entity_type": "T", "doc_id": "d", "chunk_id": "c", "description": ""},
        ]
        ngql = adapter._build_upsert_entity_ngql(entities)
        assert ngql.count("实体A") == 1
        assert ngql.count("实体B") == 1
        assert "," in ngql   # 多值之间有逗号

    def test_empty_optional_fields(self, adapter):
        """可选字段为空时不报错"""
        entities = [{"name": "极简实体"}]  # 其余字段都缺失
        ngql = adapter._build_upsert_entity_ngql(entities)
        assert "极简实体" in ngql
        assert "INSERT VERTEX" in ngql


class TestBuildUpsertRelationNgql:
    def test_basic_relation(self, adapter):
        relations = [{"source": "v1", "target": "v2",
                      "type": "WORKS_FOR", "weight": 1.5,
                      "doc_id": "doc1", "description": "任职关系"}]
        ngql = adapter._build_upsert_relation_ngql(relations)
        assert '"v1" -> "v2"' in ngql
        assert "WORKS_FOR" not in ngql  # EdgeType 固定为 RELATED_TO，类型存属性
        assert "RELATED_TO" in ngql
        assert "1.5" in ngql

    def test_multiple_relations(self, adapter):
        relations = [
            {"source": "a", "target": "b", "type": "R1", "doc_id": ""},
            {"source": "c", "target": "d", "type": "R2", "doc_id": ""},
        ]
        ngql = adapter._build_upsert_relation_ngql(relations)
        assert '"a" -> "b"' in ngql
        assert '"c" -> "d"' in ngql

---

## C. 注册中心变更

**文件**：`rag/config/registry.py` — `_register_builtins` 方法

找到以下代码块（已有）：

```python
from rag.adapters.builtin.knowledge_graph.neo4j_adapter import Neo4jAdapter
# ...
self._graph_adapters.update({
    "neo4j": Neo4jAdapter,
})
```

替换为：

```python
from rag.adapters.builtin.knowledge_graph.neo4j_adapter import Neo4jAdapter
from rag.adapters.builtin.knowledge_graph.nebula_adapter import NebulaGraphAdapter   # 新增

self._graph_adapters.update({
    "neo4j":  Neo4jAdapter,
    "nebula": NebulaGraphAdapter,    # 新增
})
```

---

## D. 依赖声明变更

**文件**：`pyproject.toml` — `[project] dependencies` 列表

在 `"neo4j>=5.0"` 行之后新增：

```toml
"nebula3-python>=3.4.0",    # NebulaGraph 3.x 适配器
```

---

## E. 规格文档内联变更

### E.1  §6 适配器表（知识图谱行）

**原文**：

| 知识图谱 | Neo4j | TigerGraph、NebulaGraph 等 |

**改为**：

| 知识图谱 | Neo4j、NebulaGraph | TigerGraph、JanusGraph 等 |

### E.2  §17 对接清单（按需对接表）

**原文**：

| 知识图谱 | `knowledge_graph.uri` | 有实体关系推理需求时 |

**改为**：

| 知识图谱（Neo4j） | `knowledge_graph.adapter: neo4j` + `uri` | 有实体关系推理需求时 |
| 知识图谱（NebulaGraph） | `knowledge_graph.adapter: nebula` + `host/port/space` | 同上，客户使用 NebulaGraph 时 |

---

## F. 客户配置示例（NebulaGraph）

使用 NebulaGraph 时，`customer_config.yaml` 中的知识图谱配置如下：

```yaml
knowledge_graph:
  enabled:  true
  adapter:  "nebula"            # ← 改为 nebula，其余字段不变
  host:     "192.168.1.100"     # graphd 服务 IP
  port:     9669                # graphd 默认端口（非 Bolt，是 Thrift）
  username: "root"
  password: "${NEBULA_PASS}"

  # NebulaGraph 专有配置（通过 extra 字段透传）
  extra:
    space:         "rag_abc"    # Space 名称（不填则自动用 rag_{tenant_id}）
    min_pool_size: 2
    max_pool_size: 10
    timeout_ms:    5000
    # 多节点高可用（与 host/port 二选一）
    # hosts:
    #   - {host: "192.168.1.100", port: 9669}
    #   - {host: "192.168.1.101", port: 9669}
```

与 Neo4j 配置的唯一差异：

| 字段 | Neo4j | NebulaGraph |
|------|-------|-------------|
| `adapter` | `"neo4j"` | `"nebula"` |
| `uri` | `bolt://host:7687` | 不使用（改用 `host`/`port`） |
| `host`/`port` | 不使用 | `host: IP`, `port: 9669` |
| `extra.space` | 不需要 | Space 名称（可选） |

---

## G. NebulaGraph 与 Neo4j 的关键技术差异说明

> 此节面向实现者，解释为何两个适配器不能共用同一套 nGQL/Cypher 逻辑。

| 维度 | Neo4j（Cypher） | NebulaGraph（nGQL） |
|------|-----------------|---------------------|
| 查询语言 | Cypher | nGQL（语法相似但有差异） |
| 属性访问 | `n.name` | `n.Tag.name`（需指定 Tag） |
| 节点 ID | 自动整数 ID | 手动指定 VID（字符串） |
| 多租户 | 数据库（Database） | Space |
| 连接 SDK | Bolt 协议，官方 Python Driver | Thrift 协议，nebula3-python |
| 异步支持 | 原生 async | 仅同步，需 run_in_executor 包装 |
| 全文索引 | 原生全文索引 | 需手动 CREATE TAG INDEX |
| 模糊查询 | `WHERE n.name CONTAINS "x"` | `WHERE Tag.name CONTAINS "x"` |

框架通过适配器层屏蔽了上述所有差异。对于 Pipeline 编排核心（`ParallelRetrievalStep`）
而言，调用 `graph.find_entities(...)` 和 `graph.query_relations(...)` 的方式
在 Neo4j 和 NebulaGraph 之间完全一致，无需任何修改。

---

*NebulaGraph 适配器版本：v1.0，兼容 NebulaGraph 3.x，依赖 nebula3-python >= 3.4.0*
