"""
向量库适配器（rag/adapters/vector_store.py）

milvus：主实现（pymilvus 同步 SDK + asyncio.to_thread）
qdrant / pgvector：备选实现（协议同构）

三者共用同一份命名约定：**物理集合名 = collection_prefix + 逻辑集合名**
（milvus/qdrant 的 collection 名、pgvector 的表名都是它；pgvector 的库名另由
配置项 database 决定）。逻辑集合名来自权限映射里的知识域名，如 default。
"""
from __future__ import annotations

import asyncio
import re
import threading
import time

from rag.config.models import VectorStoreConfig
from rag.models import RetrievedChunk
from rag.observability.logging import get_logger

from .base import VectorStoreAdapter
from .registry import AdapterRegistry

log = get_logger("rag.adapters.vector_store")

# ── 探测预算（代码常量，刻意不做成用户配置项）──────────────────────
# 与 MySQL / ES 同源（TS-014 / TS-015）："连不上要等多久"交给用户填，只会让人
# 以为调大它就能解决连接问题。两条链路共用同一份预算与口径，才不会出现
# "页面测通了、保存却说不可用"。探测只发一次 GetVersion，正常毫秒级返回。
HEALTH_BUDGET_SEC = 8.0
# 单次探测超过该阈值即告警（不改判定，只把"合法地慢"说清楚）
SLOW_PROBE_SEC = 3.0
# 建连超时（MilvusClient(timeout=)）：只作用于**建立连接与断线重连**，不是每条
# RPC 的超时（ConnectionManager.connect_timeout），所以可以安全地按探测预算来
# 约束建连本身。旧实现走 ORM 的 connections.connect()，那个没有超时 —— 地址被
# 防火墙静默丢包时，启动会被挂住直到系统 TCP 超时。
PROBE_REQUEST_TIMEOUT_SEC = 5.0

# Milvus 的标识符规则（collection 名与前缀）：字母或下划线开头，其余为字母
# 数字下划线，长度 ≤255。qdrant / pgvector 本来更宽松，这里统一按最严的来 ——
# 三套实现共用一份校验，用户不必记住"哪个 adapter 允许短横线"。
IDENT_MAX_LEN = 255
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def full_name(prefix: str, name: str) -> str:
    """逻辑集合名 → 物理集合名（prefix + name）"""
    return f"{prefix or ''}{name}"


def name_error(full: str) -> str | None:
    """校验物理集合名：合法返回 None，否则返回可读原因

    为什么必须在入口拦：名字不合法时 Milvus 只在**建集合**那一步抛错，而启动期
    ensure 失败会把整条向量路关掉（container.initialize 里 vector 置空）——
    用户只看到"向量库不可用"，根本看不出是中文域名或短横线惹的祸。
    """
    if not full:
        return "集合名为空"
    if len(full) > IDENT_MAX_LEN:
        return (f"集合名过长（{len(full)} > {IDENT_MAX_LEN}）：{full[:32]}…，"
                "请缩短集合前缀或知识域名")
    if not _IDENT_RE.match(full):
        return (f"集合名 {full!r} 不合法：只允许字母、数字与下划线，且不能以数字"
                "开头（中文、短横线、空格、点号都不行）")
    return None


def prefix_error(prefix: str) -> str | None:
    """校验集合前缀（长度由完整集合名兜住，这里只管字符集）"""
    if prefix and not _IDENT_RE.match(prefix):
        return (f"集合前缀 {prefix!r} 不合法：只允许字母、数字与下划线，且不能以"
                "数字开头（不能含中文、短横线、空格）")
    return None


def credential_error(config: VectorStoreConfig) -> str | None:
    """校验用户名/密码是否成对：pymilvus 只在**两者都非空**时才挂认证头

    只填一个不会报错，而是静默退化成匿名连接 —— 服务端开了认证时表现为一句
    "illegal connection params or server unavailable"，排查方向完全被带偏。
    """
    user = (config.user or "").strip()
    password = (config.password or "").strip()
    if user and not password:
        return ("已填用户名但密码为空：Milvus 只在用户名与密码**都非空**时才启用"
                "认证，只填一个会被当成匿名连接")
    if password and not user:
        return "已填密码但用户名为空：请补齐用户名，或两者都留空（匿名连接）"
    return None


# Milvus 异常 → 可读原因（同 TS-011 对 MySQL errno、TS-016 对 ES 异常）。
# pymilvus 把"地址写错 / 服务没起 / 认证被拒 / 权限不足"统统压成 code=2 的
# "illegal connection params or server unavailable"，而这几种的处置方向完全不同。
_MILVUS_CODE_HINTS = {
    1: "服务端返回了未预期错误",
    2: "无法建立连接：地址/端口不对、服务未启动，或凭据被拒绝",
    3: "权限不足：该账号无权执行此操作，请让 Milvus 管理员授予 collection 级权限",
    5: "参数不合法",
}


def milvus_failure_reason(exc: Exception) -> str:
    """把 pymilvus 异常翻译成能指导下一步动作的一句话"""
    code = getattr(exc, "code", None)
    msg = str(getattr(exc, "message", "") or exc).replace("\n", " ").strip()[:200]
    # 认证失败的真原因在被 from 包住的 grpc.RpcError 里（UNAUTHENTICATED）
    cause = str(getattr(exc, "__cause__", "") or "")
    blob = f"{msg} {cause}".lower()
    if ("unauthenticated" in blob or "authentication" in blob
            or "authorization" in blob):
        return ("认证失败：服务端开启了认证，而用户名/密码不正确（或只填了其中"
                f"一个，退化成了匿名连接）。原始信息：{msg}")
    if "privilege" in blob or "permission" in blob:
        return f"权限不足：该账号无权执行此操作。原始信息：{msg}"
    if "should create connection first" in blob or "connectionnotexist" in blob:
        return "连接未建立（内部状态异常）：请重新保存一次配置以重建连接"
    if "timeout" in blob or "deadline" in blob:
        return f"探测超时：服务端在预算内未响应。原始信息：{msg}"
    hint = _MILVUS_CODE_HINTS.get(code)
    if hint:
        return f"{hint}（code={code}）：{msg}"
    return f"{type(exc).__name__}: {msg}"


# ── Milvus 客户端（MilvusClient）────────────────────────────
# 旧实现走 ORM 式 API（connections / Collection / utility / FieldSchema）：pymilvus
# 3.1 起将被移除，且在 3.0 上**每次调用**都会打一条 PyMilvusDeprecationWarning，
# 把 UI 日志刷满。迁移后本文件不再出现任何 ORM 符号，可直接 grep 验收：
#   grep -nE "connections|Collection|utility|FieldSchema" rag/adapters/vector_store.py
#
# 迁移依据（服务端 Milvus 3.0.0 + pymilvus 3.0.1 真机实测，非文档推断）：
# 1) MilvusClient **构造即建连**：地址不可达时构造期就抛 MilvusException code=2
#    （实测 2.0s）。所以本适配器把建连推迟到首次使用（见 _client）—— 否则连接
#    失败会从 __init__ 抛出，container._try_create 只能记一句 None，degraded 里
#    不会留下任何原因（配置页只显示"向量库不可用"），而「测试连接」走的是另一条
#    分支、抛的是未翻译的原始报错。推迟之后两条链路都经 health_probe →
#    milvus_failure_reason 出结论，口径一致（TS-014）。
# 2) dedicated=True 的连接在 close() 时才真正释放 gRPC 通道；共享连接按
#    address|token 常驻 ConnectionManager._registry，close() 只摘客户端引用、
#    不关通道 —— 一次性探测必须 dedicated，否则"每点一次测试连接就攒一条通道"
#    （TS-016）。dedicated 同样会注册故障回调，断线重连能力不变。
def milvus_client(config: VectorStoreConfig, *,
                  dedicated: bool = True) -> "MilvusClient":
    """按配置建一个 MilvusClient（谁建谁负责 close）

    认证口径与 credential_error 一致：**用户名与密码都有**才挂 token，只填一个
    退化为匿名连接 —— 这是 pymilvus 的既有行为，这里保持原样并已在入口拦截。
    """
    from pymilvus import MilvusClient

    user = (config.user or "").strip()
    password = (config.password or "").strip()
    return MilvusClient(
        uri=f"http://{config.host}:{config.port}",
        token=f"{user}:{password}" if user and password else "",
        timeout=PROBE_REQUEST_TIMEOUT_SEC,
        dedicated=dedicated,
    )


# ── 集合结构：建集合 / 写数据 / 查数据共用一份口径 ──────────────
# 旧实现把字段表手写了两遍（建集合一遍、upsert 一遍），漏一个要到写入时才由服务端
# 报错。这里收敛成一份，字段**顺序也与旧 ORM 版本一致**：既有集合就是按这个顺序
# 建成的，出错时便于与 describe_collection 的结果逐行对照。
# 元素：字段名 → (类型标记, VARCHAR 长度)
_SCHEMA_FIELDS: dict[str, tuple[str, int]] = {
    "chunk_id": ("pk", 64),
    "embedding": ("vector", 0),
    "doc_id": ("varchar", 64),
    "tenant_id": ("varchar", 64),
    "collection": ("varchar", 128),
    "chunk_type": ("varchar", 32),
    "text": ("varchar", 8192),
    "title": ("varchar", 512),
    "section_path": ("varchar", 1024),
    "page_num": ("int64", 0),
    "figure_label": ("varchar", 128),
    "figure_caption": ("varchar", 512),
    "storage_url": ("varchar", 2048),
    "quality_score": ("float", 0),
    "allowed_roles": ("varchar", 2048),
}
# 写入时要逐字段填的文本字段（主键单独给，allowed_roles 由角色列表拼串）
_ROW_TEXT_FIELDS = tuple(name for name, (kind, _) in _SCHEMA_FIELDS.items()
                         if kind == "varchar" and name != "allowed_roles")
# 检索要带的字段（含主键）：必须与集合 schema 完全一致，多一个就 code=1100
_OUTPUT_FIELDS = [
    "chunk_id", "doc_id", "text", "title", "section_path", "page_num",
    "figure_label", "figure_caption", "chunk_type", "storage_url",
    "collection", "allowed_roles", "quality_score",
]


def schema_for(cli, dim: int):
    """按 _SCHEMA_FIELDS 建 schema（单一事实来源）"""
    from pymilvus import DataType

    type_of = {
        "pk": DataType.VARCHAR, "varchar": DataType.VARCHAR,
        "int64": DataType.INT64, "float": DataType.FLOAT,
        "vector": DataType.FLOAT_VECTOR,
    }
    schema = cli.create_schema(auto_id=False, enable_dynamic_field=False)
    for name, (kind, max_length) in _SCHEMA_FIELDS.items():
        if kind == "vector":
            schema.add_field(name, type_of[kind], dim=dim)
        elif kind == "pk":
            schema.add_field(name, type_of[kind], max_length=max_length,
                             is_primary=True)
        elif kind == "varchar":
            schema.add_field(name, type_of[kind], max_length=max_length)
        else:
            schema.add_field(name, type_of[kind])
    return schema


def build_row(chunk_id: str, vector: list[float], meta: dict) -> dict:
    """元数据 → 一行（服务端要求字段齐备：缺一个整批拒收）

    真机实测的两个坑：
    · 字段缺失 → code=1 "Insert missed an field doc_id ... without nullable"；
    · 字段为 None → code=1100 "num_rows of field ... is not equal to passed
      num_rows"（None 被当成"这一列没给"，整批失败）。
    所以不能沿用旧的 m.get(k, "")：**键存在且值为 None 时它返回的正是 None**；
    旧代码的 float(m.get("quality_score", 1.0)) 在值为 None 时还会抛 TypeError。
    这里对 None 统一归一。
    """
    row = {"chunk_id": chunk_id, "embedding": vector}
    for name in _ROW_TEXT_FIELDS:
        row[name] = str(meta.get(name) or "")
    row["page_num"] = int(meta.get("page_num") or 0)
    row["quality_score"] = float(meta.get("quality_score") or 1.0)
    row["allowed_roles"] = ",".join(meta.get("allowed_roles") or [])
    return row


@AdapterRegistry.register("vector_store", "milvus")
class MilvusVectorStore(VectorStoreAdapter):
    """Milvus 实现

    前置条件：Milvus 在跑 + 账号具备**建集合 / 建索引 / 加载 / 读写**权限。
    集合与索引都由本类自动创建（启动期就会建 collection_prefix + "default"），
    所以权限不足不是"首次写入才失败"，而是启动自检失败 → 整条向量路被关闭。
    """

    def __init__(self, config: VectorStoreConfig):
        self.config = config
        self._prefix = config.collection_prefix or ""
        # 在入口失败：非法前缀 / 半套凭据会让"每次建集合都抛错"，而启动期 ensure
        # 失败会把整条向量路关掉 —— 这里既不建连，也不留下半成品连接
        err = prefix_error(self._prefix) or credential_error(config)
        if err:
            raise ValueError(err)
        # 惰性建连（理由见 milvus_client 上方）：__init__ 不碰网络，连接问题一律
        # 由 health_probe 给出可读原因，而不是在这里变成一句未翻译的原始报错
        self._client_obj = None
        self._client_lock = threading.Lock()
        # 延迟创建索引（首次 upsert 时按 dim 建）
        self._ensured: set[str] = set()

    def _client(self):
        """首次使用时才建连

        双检锁：ensure_collection / upsert / search 都跑在 asyncio.to_thread 里，
        并发进来时不能建出两条连接（旧实现靠 alias 天然去重，这里自己保证）。
        """
        if self._client_obj is None:
            with self._client_lock:
                if self._client_obj is None:
                    self._client_obj = milvus_client(self.config)
        return self._client_obj

    def _full_name(self, name: str) -> str:
        """逻辑集合名 → 物理集合名（含合法性校验，见 name_error）"""
        full = full_name(self._prefix, name)
        err = name_error(full)
        if err:
            raise ValueError(err)
        return full

    def _warn_dim_mismatch(self, cli, full: str, dim: int) -> None:
        """已有集合的向量维度与当前 embedding 不一致 → 只告警，不改结构

        Milvus 不会自动改维度：不提醒的话，用户换了 embedding 维度后要到首次
        upsert 才看到 dimension mismatch，而那时数据已经入库到一半。
        """
        try:
            fields = (cli.describe_collection(full) or {}).get("fields") or []
            existing = next((int((f.get("params") or {}).get("dim") or 0)
                             for f in fields if f.get("name") == "embedding"), 0)
        except Exception:
            return          # 探测失败不影响主流程，真实错误留给 upsert 报
        if existing and dim and existing != dim:
            log.warning("vector_dim_mismatch", collection=full,
                        existing_dim=existing, embedding_dim=dim,
                        hint="已存在的集合不会按新维度重建：请换 collection_prefix "
                             "或删掉该集合后重新入库")

    def _ensure_loaded(self, cli, full: str) -> None:
        """确保集合处于 Loaded（search / query 都要求，否则 code=101）

        用 get_load_state 判断而不是无条件 load_collection()：旧实现每次检索都
        load 一次，等于每次检索白发一条 RPC。这里不缓存状态，是为了让服务端因
        内存压力把集合卸下之后能自动重新加载，而不是一直报 101。
        集合不存在时 get_load_state 返回 NotExist（不抛异常），随后的
        load_collection 抛 code=100 —— 与旧实现在同一处失败，调用方口径不变。
        """
        state = (cli.get_load_state(full) or {}).get("state")
        if not str(getattr(state, "name", None) or state).endswith("Loaded"):
            cli.load_collection(full)

    async def ensure_collection(self, name: str, dim: int) -> None:
        full = self._full_name(name)
        if full in self._ensured:
            return

        def _create():
            cli = self._client()
            if cli.has_collection(full):
                self._warn_dim_mismatch(cli, full, dim)
                return
            index = cli.prepare_index_params()
            index.add_index(field_name="embedding", index_type="HNSW",
                            metric_type="IP",   # 内积（向量已归一化 ≈ 余弦）
                            params={"M": 16, "efConstruction": 200})
            # create_collection 连带建索引与 load（实测建完即 Loaded），这里不再
            # 单独 load；万一某版本只建不加载，下面检索入口的 _ensure_loaded 会补
            cli.create_collection(collection_name=full,
                                  schema=schema_for(cli, dim),
                                  index_params=index,
                                  description=f"RAG collection {name}")

        await asyncio.to_thread(_create)
        self._ensured.add(full)

    async def upsert(self, collection: str, ids: list[str],
                     vectors: list[list[float]], metadatas: list[dict]) -> None:
        full = self._full_name(collection)
        dim = len(vectors[0]) if vectors else self.config_dim
        await self.ensure_collection(collection, dim)

        def _upsert():
            self._client().upsert(
                collection_name=full,
                data=[build_row(cid, vec, m) for cid, vec, m
                      in zip(ids, vectors, metadatas)])
        await asyncio.to_thread(_upsert)

    @property
    def config_dim(self) -> int:
        """兜底维度：优先 extra.dim；不传 embedding 配置时退化为 1024"""
        extra = getattr(self.config, "extra", None) or {}
        try:
            return int(extra.get("dim", 1024))
        except (TypeError, ValueError):
            return 1024

    async def search(self, collection: str, vector: list[float],
                     top_k: int = 20, filter: dict | None = None,
                     nprobe: int = 16) -> list[RetrievedChunk]:
        full = self._full_name(collection)

        def _search():
            cli = self._client()
            self._ensure_loaded(cli, full)
            res = cli.search(
                collection_name=full, data=[vector], anns_field="embedding",
                search_params={"metric_type": "IP", "params": {"ef": nprobe * 8}},
                limit=top_k, filter=self._build_expr(filter),   # 空串=不加过滤
                output_fields=_OUTPUT_FIELDS,
            )
            out: list[RetrievedChunk] = []
            # Hit 是 dict 子类（键为 主键 / distance / entity），其 .get 会回落到
            # entity，所以 output_fields 直接按字段名取即可；相似度就是 distance
            # （Hit.score 只是它的别名）
            for i, hit in enumerate(res[0]):
                roles_str = hit.get("allowed_roles") or ""
                out.append(RetrievedChunk(
                    chunk_id=hit.get("chunk_id"),
                    doc_id=hit.get("doc_id", ""),
                    text=hit.get("text", ""),
                    score=float(hit.get("distance") or 0.0),
                    source_path=None, rank=i + 1,
                    title=hit.get("title"), section_path=hit.get("section_path"),
                    page_num=hit.get("page_num") or None,
                    figure_label=hit.get("figure_label") or None,
                    figure_caption=hit.get("figure_caption") or None,
                    chunk_type=hit.get("chunk_type", "text"),
                    collection=hit.get("collection"),
                    storage_url=hit.get("storage_url") or None,
                    quality_score=float(hit.get("quality_score") or 1.0),
                    metadata={"allowed_roles": roles_str.split(",") if roles_str else []},
                ))
            return out
        return await asyncio.to_thread(_search)

    def _build_expr(self, filter: dict | None) -> str:
        if not filter:
            return ""
        import re as _re
        parts: list[str] = []
        if filter.get("tenant_id"):
            parts.append(f'tenant_id == "{filter["tenant_id"]}"')
        if filter.get("collection"):
            parts.append(f'collection == "{filter["collection"]}"')
        if filter.get("doc_id"):
            parts.append(f'doc_id == "{filter["doc_id"]}"')
        if filter.get("chunk_ids"):
            ids = [f'"{c}"' for c in filter["chunk_ids"][:50000]]
            if ids:
                parts.append(f"chunk_id in [{','.join(ids)}]")
        if filter.get("allowed_roles"):
            # 数据层权限硬过滤：空串=公开；否则拼接串须含用户角色之一
            safe = [r for r in filter["allowed_roles"]
                    if _re.fullmatch(r"[\w\-.]+", str(r))]
            if safe:
                ors = [f'allowed_roles like "%{r}%"' for r in safe]
                ors.append('allowed_roles == ""')
                parts.append("(" + " or ".join(ors) + ")")
        return " and ".join(parts)

    async def delete_by_doc(self, collection: str, doc_id: str) -> int:
        full = self._full_name(collection)

        def _delete():
            cli = self._client()
            if not cli.has_collection(full):
                return 0
            expr = f'doc_id == "{doc_id}"'
            # 先数再删：返回的是"这次删掉了几条"。删完立刻 query 会因可见性延迟
            # 读到旧数据（真机实测），所以计数必须在 delete 之前取。
            # consistency_level="Strong"：默认 Bounded 下"刚写完就读"会读到空
            # （实测 upsert 后 +0ms 读到 0 条、+200ms 才读到 1 条；Strong 在
            # +161ms 就能读到），而这里读到空集合会**直接跳过删除**、留下孤儿
            # 片段 —— 属于"静默算错"，所以宁可变慢也要读准
            self._ensure_loaded(cli, full)
            found = cli.query(collection_name=full, filter=expr,
                              output_fields=["chunk_id"],
                              consistency_level="Strong")
            if found:
                cli.delete(collection_name=full, filter=expr)
            return len(found)
        return await asyncio.to_thread(_delete)

    async def delete_by_ids(self, collection: str, ids: list[str]) -> None:
        if not ids:
            return
        full = self._full_name(collection)

        def _delete():
            cli = self._client()
            if not cli.has_collection(full):
                return
            quoted = ",".join(f'"{i}"' for i in ids)
            cli.delete(collection_name=full, filter=f"chunk_id in [{quoted}]")
        await asyncio.to_thread(_delete)

    async def get_doc_chunk_ids(self, collection: str, doc_id: str) -> set[str]:
        full = self._full_name(collection)

        def _query():
            cli = self._client()
            if not cli.has_collection(full):
                return set()
            self._ensure_loaded(cli, full)
            # 与 delete_by_doc 同理：services/consistency.py 拿这个集合判断
            # "向量库是否缺片段"，刚入库就读到空会被误判成不一致而触发无谓修复
            res = cli.query(collection_name=full, filter=f'doc_id == "{doc_id}"',
                            output_fields=["chunk_id"],
                            consistency_level="Strong")
            return {r["chunk_id"] for r in res}
        return await asyncio.to_thread(_query)

    # ── 健康检查 / 探测 ─────────────────────────────────────

    def _probe_sync(self) -> tuple[bool, str]:
        """一次 GetVersion：建连与认证都要过才算可用

        这里的连接是**惰性建**的：连不上会在这一步抛出并被翻译成可读原因，
        而不是在 __init__ 里抛出（那样配置页只能看到一句未翻译的原始报错）
        """
        try:
            version = self._client().get_server_version()
            return True, (f"Milvus {version} 连接正常"
                          f"（集合前缀 {self._prefix or '无'}）")
        except Exception as e:
            log.warning("vector_health_failed", host=self.config.host,
                        port=self.config.port, error=str(e)[:200])
            return False, milvus_failure_reason(e)

    async def health_detail(self) -> tuple[bool, str]:
        """返回 (是否可用, 可读原因)：所有分支都给原因，不只给 bool"""
        return await asyncio.to_thread(self._probe_sync)

    async def health_check(self) -> bool:
        ok, _ = await self.health_detail()
        return ok

    async def health_probe(self) -> tuple[bool, str]:
        """带预算的健康检查：配置页「测试连接」与运行期自检共用同一口径

        与 MySQL / ES 同源（TS-014 / TS-015）：两条链路各用各的超时，就会出现
        "页面测通了、保存却说不可用"，而两边结论都"有据可查"，极难排查。
        """
        started = time.perf_counter()
        try:
            ok, reason = await asyncio.wait_for(
                self.health_detail(), timeout=HEALTH_BUDGET_SEC)
        except (asyncio.TimeoutError, TimeoutError):
            elapsed = time.perf_counter() - started
            log.warning("vector_health_timeout", budget=HEALTH_BUDGET_SEC,
                        elapsed=round(elapsed, 2), host=self.config.host)
            return False, (f"探测超时（超过 {HEALTH_BUDGET_SEC:g}s 无响应）："
                           f"{self.config.host}:{self.config.port} 未在预算内回应 "
                           "GetVersion；请确认地址/端口是否正确、是否有防火墙"
                           "静默丢包")
        elapsed = time.perf_counter() - started
        if ok and elapsed >= SLOW_PROBE_SEC:
            # "慢但成功"不会走任何失败分支，必须自己发声（同 mysql_slow_connect）
            log.warning("vector_slow_probe", elapsed=round(elapsed, 2),
                        host=self.config.host,
                        hint="Milvus 响应明显偏慢：检查服务端负载与网络")
        return ok, reason

    @classmethod
    def probe(cls, config: VectorStoreConfig) -> "MilvusVectorStore":
        """建一个**一次性**探测实例（用完由调用方 aclose 释放通道）

        与运行期实例的唯一区别只是生命周期：每次「测试连接」都新建一条
        dedicated 通道，用完就关，不会牵动运行期那条（TS-016）
        """
        return cls(config)

    async def aclose(self) -> None:
        """释放本实例持有的连接

        dedicated 实例（本适配器建的都是）在这里真正关闭 gRPC 通道；即使拿到
        的是共享连接，MilvusClient.close() 也只摘掉自己的客户端引用、不关通道，
        所以 _close_quietly 在"换上新实例之后"调用它是安全的
        """
        cli, self._client_obj = self._client_obj, None
        if cli is not None:
            await asyncio.to_thread(cli.close)


@AdapterRegistry.register("vector_store", "qdrant")
class QdrantVectorStore(VectorStoreAdapter):
    """Qdrant 实现（REST API，httpx）

    集合命名与 milvus 一致：物理集合名 = collection_prefix + 逻辑集合名。
    """

    def __init__(self, config: VectorStoreConfig):
        import httpx
        self.config = config
        self._prefix = config.collection_prefix or ""
        err = prefix_error(self._prefix)
        if err:
            raise ValueError(err)
        self._ensured: set[str] = set()
        self._client = httpx.AsyncClient(
            base_url=f"http://{config.host}:{config.port}",
            timeout=config.timeout,
        )

    def _full_name(self, name: str) -> str:
        """逻辑集合名 → 物理集合名（含合法性校验，见 name_error）"""
        full = full_name(self._prefix, name)
        err = name_error(full)
        if err:
            raise ValueError(err)
        return full

    async def ensure_collection(self, name: str, dim: int) -> None:
        full = self._full_name(name)
        if full in self._ensured:
            return
        resp = await self._client.put(
            f"/collections/{full}",
            json={"vectors": {"size": dim, "distance": "Cosine"}},
        )
        if resp.status_code >= 400 and "exist" not in resp.text.lower():
            resp.raise_for_status()
        self._ensured.add(full)

    async def upsert(self, collection: str, ids: list[str],
                     vectors: list[list[float]], metadatas: list[dict]) -> None:
        points = [
            {"id": i, "vector": v, "payload": m}
            for i, v, m in zip(ids, vectors, metadatas)
        ]
        resp = await self._client.put(
            f"/collections/{self._full_name(collection)}/points?wait=true",
            json={"points": points},
        )
        resp.raise_for_status()

    async def search(self, collection: str, vector: list[float],
                     top_k: int = 20, filter: dict | None = None,
                     nprobe: int = 16) -> list[RetrievedChunk]:
        qfilter = None
        if filter:
            must = []
            for k in ("tenant_id", "collection", "doc_id"):
                if filter.get(k):
                    must.append({"key": k, "match": {"value": filter[k]}})
            if filter.get("chunk_ids"):
                must.append({"key": "chunk_id",
                             "match": {"any": filter["chunk_ids"][:50000]}})
            qfilter = {"must": must} if must else None
        resp = await self._client.post(
            f"/collections/{self._full_name(collection)}/points/search",
            json={"vector": vector, "limit": top_k, "filter": qfilter,
                  "with_payload": True},
        )
        resp.raise_for_status()
        out = []
        for i, hit in enumerate(resp.json()["result"]):
            p = hit.get("payload", {})
            out.append(RetrievedChunk(
                chunk_id=str(hit["id"]), doc_id=p.get("doc_id", ""),
                text=p.get("text", ""), score=hit["score"], rank=i + 1,
                title=p.get("title"), section_path=p.get("section_path"),
                page_num=p.get("page_num"), chunk_type=p.get("chunk_type", "text"),
                collection=p.get("collection"), metadata=p,
            ))
        return out

    async def delete_by_doc(self, collection: str, doc_id: str) -> int:
        resp = await self._client.post(
            f"/collections/{self._full_name(collection)}/points/delete",
            json={"filter": {"must": [{"key": "doc_id",
                                       "match": {"value": doc_id}}]}},
        )
        resp.raise_for_status()
        return -1   # Qdrant 不返回计数

    async def delete_by_ids(self, collection: str, ids: list[str]) -> None:
        resp = await self._client.post(
            f"/collections/{self._full_name(collection)}/points/delete?wait=true",
            json={"points": ids},
        )
        resp.raise_for_status()

    async def get_doc_chunk_ids(self, collection: str, doc_id: str) -> set[str]:
        resp = await self._client.post(
            f"/collections/{self._full_name(collection)}/points/scroll",
            json={"filter": {"must": [{"key": "doc_id",
                                       "match": {"value": doc_id}}]},
                  "with_payload": False, "limit": 10000},
        )
        resp.raise_for_status()
        return {str(p["id"]) for p in resp.json()["result"]["points"]}

    # ── 健康检查 / 探测 ─────────────────────────────────────

    async def health_detail(self) -> tuple[bool, str]:
        base = f"{self.config.host}:{self.config.port}"
        try:
            resp = await self._client.get("/")
        except Exception as e:
            return False, (f"无法连接 {base}（{type(e).__name__}: "
                           f"{str(e)[:150]}）")
        if resp.status_code >= 400:
            return False, f"Qdrant 返回 HTTP {resp.status_code}：{resp.text[:150]}"
        return True, f"Qdrant {base} 连接正常（集合前缀 {self._prefix or '无'}）"

    async def health_probe(self) -> tuple[bool, str]:
        """带预算的探测：与容器自检共用同一份超时（同 milvus / MySQL / ES）"""
        try:
            return await asyncio.wait_for(self.health_detail(),
                                          timeout=HEALTH_BUDGET_SEC)
        except (asyncio.TimeoutError, TimeoutError):
            return False, (f"探测超时（超过 {HEALTH_BUDGET_SEC:g}s 无响应）："
                           f"{self.config.host}:{self.config.port} 未在预算内回应")

    async def health_check(self) -> bool:
        ok, _ = await self.health_detail()
        return ok

    async def aclose(self) -> None:
        """释放 httpx 客户端：换配置时旧实例的连接池要还回去"""
        await self._client.aclose()


@AdapterRegistry.register("vector_store", "pgvector")
class PgVectorStore(VectorStoreAdapter):
    """pgvector 实现（psycopg 异步，需现场安装 psycopg[binary]）

    命名与 milvus 一致：物理**表名** = collection_prefix + 逻辑集合名；
    库名另由配置项 database 决定（库必须先存在，本类不建库）。
    """

    def __init__(self, config: VectorStoreConfig):
        import psycopg  # noqa: F401  未安装时构造期即失败，降级原因更直白
        self.config = config
        self._prefix = config.collection_prefix or ""
        err = prefix_error(self._prefix)
        if err:
            raise ValueError(err)
        # 库名不再从集合前缀反推（旧实现 dbname=collection_prefix.rstrip("_")）：
        # 那是把"命名前缀"当成"库名"用，前缀一改就会去找一个不存在的库，
        # 报的还是 psycopg 的 "database ... does not exist"
        self._dsn = (f"host={config.host} port={config.port} "
                     f"user={config.user or 'postgres'} "
                     f"password={config.password or ''} "
                     f"dbname={config.database or 'rag'}")

    def _table(self, name: str) -> str:
        """逻辑集合名 → 物理表名（表名会直接拼进 SQL，必须先校验字符集）"""
        full = full_name(self._prefix, name)
        err = name_error(full)
        if err:
            raise ValueError(err)
        return full

    async def _conn(self):
        import psycopg
        return await psycopg.AsyncConnection.connect(self._dsn)

    async def ensure_collection(self, name: str, dim: int) -> None:
        table = self._table(name)
        async with await self._conn() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    chunk_id TEXT PRIMARY KEY,
                    embedding vector({dim}),
                    doc_id TEXT, tenant_id TEXT, collection TEXT,
                    chunk_type TEXT, text TEXT, title TEXT,
                    section_path TEXT, page_num INT,
                    figure_label TEXT, figure_caption TEXT,
                    storage_url TEXT, quality_score REAL,
                    allowed_roles TEXT[]
                )""")
            await conn.commit()

    async def upsert(self, collection: str, ids: list[str],
                     vectors: list[list[float]], metadatas: list[dict]) -> None:
        table = self._table(collection)
        async with await self._conn() as conn:
            for cid, vec, m in zip(ids, vectors, metadatas):
                await conn.execute(
                    f"""INSERT INTO {table} VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (chunk_id) DO UPDATE SET embedding=EXCLUDED.embedding,
                            text=EXCLUDED.text, title=EXCLUDED.title""",
                    (cid, str(vec), m.get("doc_id", ""), m.get("tenant_id", ""),
                     m.get("collection", ""), m.get("chunk_type", "text"),
                     m.get("text", ""), m.get("title", ""),
                     m.get("section_path", ""), m.get("page_num"),
                     m.get("figure_label"), m.get("figure_caption"),
                     m.get("storage_url"), m.get("quality_score", 1.0),
                     m.get("allowed_roles") or []))
            await conn.commit()

    async def search(self, collection: str, vector: list[float],
                     top_k: int = 20, filter: dict | None = None,
                     nprobe: int = 16) -> list[RetrievedChunk]:
        table = self._table(collection)
        conds, params = ["TRUE"], [str(vector)]
        if filter:
            if filter.get("tenant_id"):
                conds.append("tenant_id = %s"); params.append(filter["tenant_id"])
            if filter.get("doc_id"):
                conds.append("doc_id = %s"); params.append(filter["doc_id"])
            if filter.get("chunk_ids"):
                conds.append("chunk_id = ANY(%s)"); params.append(filter["chunk_ids"])
        sql = (f"SELECT chunk_id, doc_id, text, title, section_path, page_num, "
               f"chunk_type, 1 - (embedding <=> %s::vector) AS score "
               f"FROM {table} WHERE {' AND '.join(conds)} "
               f"ORDER BY embedding <=> %s::vector LIMIT {top_k}")
        params2 = [params[0], *params[1:], params[0]]
        async with await self._conn() as conn:
            cur = await conn.execute(sql, params2)
            rows = await cur.fetchall()
        # 注意：不要在这里读 cur.description —— 连接已退出 with 块被关闭，
        # 该属性可能抛 ProgrammingError（且结果本来就没被用到）
        return [RetrievedChunk(chunk_id=r[0], doc_id=r[1], text=r[2],
                               score=float(r[-1]), rank=i + 1,
                               title=r[3], section_path=r[4], page_num=r[5],
                               chunk_type=r[6] or "text")
                for i, r in enumerate(rows)]

    async def delete_by_doc(self, collection: str, doc_id: str) -> int:
        table = self._table(collection)
        async with await self._conn() as conn:
            cur = await conn.execute(f"DELETE FROM {table} WHERE doc_id=%s",
                                     (doc_id,))
            await conn.commit()
            return cur.rowcount or 0

    async def delete_by_ids(self, collection: str, ids: list[str]) -> None:
        table = self._table(collection)
        async with await self._conn() as conn:
            await conn.execute(f"DELETE FROM {table} WHERE chunk_id = ANY(%s)",
                               (ids,))
            await conn.commit()

    async def get_doc_chunk_ids(self, collection: str, doc_id: str) -> set[str]:
        table = self._table(collection)
        async with await self._conn() as conn:
            cur = await conn.execute(
                f"SELECT chunk_id FROM {table} WHERE doc_id=%s", (doc_id,))
            rows = await cur.fetchall()
        return {r[0] for r in rows}

    # ── 健康检查 / 探测 ─────────────────────────────────────

    async def health_detail(self) -> tuple[bool, str]:
        db = self.config.database or "rag"
        target = f"{self.config.host}:{self.config.port}/{db}"
        try:
            async with await self._conn():
                pass
        except Exception as e:
            msg = str(e).replace("\n", " ").strip()[:200]
            low = msg.lower()
            if "does not exist" in low and "database" in low:
                # 库名现在是独立配置项，最容易踩的就是"填了个还没建的库"
                return False, (f"库 {db} 不存在：pgvector 用的库必须先在 PostgreSQL "
                               f"里创建（本适配器只建表、不建库）。原始信息：{msg}")
            if "password" in low or "authentication" in low \
                    or "role" in low:
                return False, f"认证失败：用户名/密码或库权限有误。原始信息：{msg}"
            if "could not connect" in low or "connection refused" in low \
                    or "timeout" in low:
                return False, (f"无法连接 {target}：地址/端口不对或服务未启动。"
                               f"原始信息：{msg}")
            return False, f"PostgreSQL 探测失败：{msg}"
        return True, (f"PostgreSQL {target} 连接正常"
                      f"（表名前缀 {self._prefix or '无'}）")

    async def health_probe(self) -> tuple[bool, str]:
        """带预算的探测：与容器自检共用同一份超时（同 milvus / MySQL / ES）"""
        try:
            return await asyncio.wait_for(self.health_detail(),
                                          timeout=HEALTH_BUDGET_SEC)
        except (asyncio.TimeoutError, TimeoutError):
            return False, (f"探测超时（超过 {HEALTH_BUDGET_SEC:g}s 无响应）："
                           f"{self.config.host}:{self.config.port} 未在预算内回应")

    async def health_check(self) -> bool:
        ok, _ = await self.health_detail()
        return ok
