"""
MySQL 元数据适配器（rag/adapters/mysql_meta.py）

按规格附录 C 实现：documents / chunks_meta / table_data / ingest_tasks /
ingest_batches / feedback / user_profiles / entities 八张表，
首次启动自动建表（DDL 幂等）。SQLAlchemy async + aiomysql。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                    create_async_engine)

from rag.config.models import MySQLConfig
from rag.models import (ChunkMeta, DocumentMeta, IngestBatch, IngestStatus,
                        IngestTask, MessageFeedback, TableData, UserProfile)
from rag.observability.logging import get_logger

from .base import MySQLMetaAdapter
from .registry import AdapterRegistry

log = get_logger("rag.adapters.mysql_meta")

# ── 连接参数（由代码管理，**不作为用户配置项**）─────────────────
# 这三项只决定"怎么连"，不是需要用户做业务选择的参数；暴露在配置页只会让
# 用户以为调大就能解决连接问题（TS-014 的原始误判正是"把预算当成可调开关"）。
POOL_SIZE = 10
# 单次建连的等待上限（秒）；只约束 TCP 连接本身
CONNECT_TIMEOUT_SEC = 3
# 健康检查 /「测试连接」的总预算（秒）：容器自检与配置页探测**共用**它。
# 两条链路必须共用同一预算，否则会出现"页面测通了、保存却说不可达"（TS-014）。
# 取值偏宽是刻意的：误判一次就要把元数据库降级为内存模式（数据不再持久化），
# 代价远大于多等几秒。
HEALTH_BUDGET_SEC = 12.0

# 建连耗时超过该阈值即告警（不改判定，只把"慢"说清楚）
SLOW_CONNECT_SEC = 3.0


def _build_engine(config: MySQLConfig):
    """按配置造引擎（惰性建连：此处不产生任何 TCP 连接）

    每次调用都返回**全新引擎**：探测一律真实建连，不复用任何缓存连接，
    这样"测试连接"反映的永远是此刻服务端的真实状态（服务端刚修好、或刚宕机，
    点一下就能立刻看出来，而不是拿到一条早就建好的空闲连接）—— 见 TS-014。
    """
    dsn = (f"mysql+aiomysql://{config.user}:{config.password}"
           f"@{config.host}:{config.port}/{config.database}"
           f"?charset={config.charset}")
    return create_async_engine(
        dsn, pool_size=POOL_SIZE, pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SEC})


def _driver_errno(exc: Exception) -> int | None:
    """从 SQLAlchemy 的异常包装链里取出驱动层 errno

    SQLAlchemy 会把 pymysql 的异常塞进 ``orig``，形如
    ``OperationalError(1130, "...")`` —— 具体错误码只在最内层 args[0]。
    """
    cur: Any = exc
    for _ in range(4):
        args = getattr(cur, "args", None)
        if args and isinstance(args[0], int):
            return args[0]
        nxt = getattr(cur, "orig", None)
        if nxt is None or nxt is cur:
            break
        cur = nxt
    return None


def _mysql_failure_reason(exc: Exception) -> str:
    """把驱动异常翻译成可操作的中文原因（配置页「测试连接」直接展示）

    只返回 bool 的 health_check 会让用户看到一句无信息的"连接失败"，
    而 MySQL 的错误码本身就有明确语义（授权/建库/认证/网络），必须透出。
    """
    code = _driver_errno(exc)
    msg = str(getattr(exc, "orig", exc)).replace("\n", " ")[:180]
    if code == 1130:
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", msg)
        src = m.group(1) if m else "应用所在机器"
        return (f"来源主机 {src} 未被授权（errno 1130）：服务端缺少匹配该 IP 的账号，"
                f"需在 MySQL 上 CREATE USER '<用户>'@'{src}' 并 GRANT 后重试")
    if code == 1049:
        return f"目标数据库不存在（errno 1049）：请先 CREATE DATABASE；原始信息：{msg}"
    if code == 1045:
        return f"认证失败（errno 1045）：用户名或密码与服务端账号不匹配；原始信息：{msg}"
    if code == 1044:
        return f"账号无权访问该数据库（errno 1044）：请补 GRANT；原始信息：{msg}"
    if code == 2003:
        return (f"无法建立连接（errno 2003）：服务未监听该地址或端口被防火墙拦截；"
                f"原始信息：{msg}")
    prefix = f"errno {code}" if code is not None else "无错误码"
    return f"MySQL 连接失败（{prefix}）：{msg}"


def mysql_timeout_reason(cfg: MySQLConfig, budget: float, elapsed: float) -> str:
    """建连超时的可读原因 —— 与"不可达"区分开

    TCP 已通、SQL 也没报错时，耗时几乎全在**服务端的问候包**上：
    服务端在建连阶段做反向 DNS（`skip_name_resolve=OFF`）或链路抖动，
    都会让每个**新连接**都等满解析超时（实测 10s）。
    这种"等满预算"与"服务真不可达"在旧文案里都被写成"MySQL 不可达"，
    用户无法据此判断该改哪里。
    """
    return (f"MySQL 建连超时（{elapsed:.1f}s 超过预算 {budget:.0f}s）："
            f"TCP 已通但服务端 {cfg.host}:{cfg.port} 的问候包迟迟未返回，"
            "常见于服务端在建连阶段做反向 DNS 解析（skip_name_resolve=OFF）"
            "或网络链路抖动；可在 MySQL 服务端 [mysqld] 段设 "
            "skip_name_resolve=ON 后重启验证")


DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS documents (
      doc_id         VARCHAR(64)   PRIMARY KEY,
      tenant_id      VARCHAR(64)   NOT NULL,
      collection     VARCHAR(128)  NOT NULL,
      filename       VARCHAR(512)  NOT NULL,
      file_type      VARCHAR(32)   NOT NULL,
      file_size      BIGINT,
      file_md5       VARCHAR(64),
      storage_url    TEXT,
      language       VARCHAR(16)   DEFAULT 'zh',
      page_count     INT,
      status         VARCHAR(32)   NOT NULL DEFAULT 'pending',
      chunk_count    INT           DEFAULT 0,
      allowed_roles  JSON,
      version        INT           DEFAULT 1,
      quality_report JSON,
      created_by     VARCHAR(64),
      created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                     ON UPDATE CURRENT_TIMESTAMP,
      INDEX idx_tenant_col (tenant_id, collection),
      INDEX idx_status     (status),
      INDEX idx_md5        (file_md5, tenant_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS chunks_meta (
      chunk_id         VARCHAR(64)  PRIMARY KEY,
      doc_id           VARCHAR(64)  NOT NULL,
      tenant_id        VARCHAR(64)  NOT NULL,
      collection       VARCHAR(128) NOT NULL,
      chunk_type       VARCHAR(32)  NOT NULL DEFAULT 'text',
      is_parent        TINYINT(1)   DEFAULT 0,
      parent_chunk_id  VARCHAR(64),
      page_num         INT,
      section_path     TEXT,
      quality_score    FLOAT        DEFAULT 1.0,
      token_count      INT          DEFAULT 0,
      allowed_roles    JSON,
      figure_label     VARCHAR(64),
      figure_caption   TEXT,
      text             MEDIUMTEXT,
      created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
      INDEX idx_doc_id      (doc_id),
      INDEX idx_tenant_col  (tenant_id, collection),
      INDEX idx_chunk_type  (chunk_type),
      FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS table_data (
      id             BIGINT        AUTO_INCREMENT PRIMARY KEY,
      chunk_id       VARCHAR(64)   NOT NULL,
      doc_id         VARCHAR(64)   NOT NULL,
      tenant_id      VARCHAR(64)   NOT NULL,
      table_index    INT           DEFAULT 0,
      page_num       INT,
      headers        JSON,
      row_data       JSON,
      row_count      INT           DEFAULT 0,
      numeric_stats  JSON,
      created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
      INDEX idx_doc_id   (doc_id),
      INDEX idx_tenant   (tenant_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS ingest_tasks (
      task_id          VARCHAR(64)  PRIMARY KEY,
      doc_id           VARCHAR(64),
      batch_id         VARCHAR(64),
      tenant_id        VARCHAR(64)  NOT NULL,
      collection       VARCHAR(128) NOT NULL DEFAULT 'default',
      filename         VARCHAR(512) NOT NULL,
      file_md5         VARCHAR(64),
      file_type        VARCHAR(32),
      status           VARCHAR(32)  NOT NULL DEFAULT 'pending',
      retry_count      INT          DEFAULT 0,
      max_retries      INT          DEFAULT 3,
      error_message    TEXT,
      checkpoint       JSON,
      total_pages      INT,
      processed_pages  INT          DEFAULT 0,
      total_chunks     INT          DEFAULT 0,
      written_chunks   INT          DEFAULT 0,
      quality_summary  JSON,
      source_type      VARCHAR(32)  DEFAULT 'upload',
      submitted_by     VARCHAR(64),
      started_at       DATETIME,
      completed_at     DATETIME,
      created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
      INDEX idx_status  (status),
      INDEX idx_doc_id  (doc_id),
      INDEX idx_batch   (batch_id),
      INDEX idx_md5     (file_md5)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS ingest_batches (
      batch_id     VARCHAR(64)  PRIMARY KEY,
      tenant_id    VARCHAR(64)  NOT NULL,
      collection   VARCHAR(128) NOT NULL DEFAULT 'default',
      total        INT DEFAULT 0,
      succeeded    INT DEFAULT 0,
      failed       INT DEFAULT 0,
      pending      INT DEFAULT 0,
      status       VARCHAR(32) DEFAULT 'pending',
      source_type  VARCHAR(32) DEFAULT 'upload',
      created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS feedback (
      feedback_id    VARCHAR(64)   PRIMARY KEY,
      session_id     VARCHAR(64)   NOT NULL,
      message_id     VARCHAR(64)   NOT NULL,
      tenant_id      VARCHAR(64)   NOT NULL,
      user_id        VARCHAR(64)   NOT NULL,
      feedback       VARCHAR(8)    NOT NULL,
      query          TEXT,
      answer         TEXT,
      sources        JSON,
      reasons        JSON,
      comment        TEXT,
      retrieval_paths JSON,
      created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
      INDEX idx_tenant    (tenant_id),
      INDEX idx_feedback  (feedback),
      INDEX idx_session   (session_id),
      UNIQUE INDEX idx_msg (message_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS user_profiles (
      user_id       VARCHAR(64)  NOT NULL,
      tenant_id     VARCHAR(64)  NOT NULL,
      profile       JSON,
      updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                     ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (user_id, tenant_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS entities (
      id         BIGINT       AUTO_INCREMENT PRIMARY KEY,
      tenant_id  VARCHAR(64)  NOT NULL,
      doc_id     VARCHAR(64),
      name       VARCHAR(256) NOT NULL,
      canonical  VARCHAR(256),
      entity_type VARCHAR(64) DEFAULT 'generic',
      embedding  JSON,
      UNIQUE INDEX idx_name (tenant_id, name),
      INDEX idx_doc (doc_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


@AdapterRegistry.register("mysql_meta", "mysql_meta")
class MySQLMetaStore(MySQLMetaAdapter):

    def __init__(self, config: MySQLConfig):
        self._config = config
        self._initialized = False
        # 每个实例持有自己的引擎：不做跨实例的引擎/连接缓存 ——
        # 任何一次"测试连接"都必须是真实建连，否则页面可能报出早已失效的
        # 缓存连接为"正常"（见 TS-014）
        self._engine = _build_engine(config)
        self._session = async_sessionmaker(self._engine,
                                           class_=AsyncSession,
                                           expire_on_commit=False)

    async def _ensure_tables(self) -> None:
        if self._initialized:
            return
        async with self._engine.begin() as conn:
            for ddl in DDL_STATEMENTS:
                await conn.execute(text(ddl))
            # 存量库升级：chunks_meta 补充正文与图题字段
            for mig in (
                "ALTER TABLE chunks_meta ADD COLUMN text MEDIUMTEXT NULL",
                "ALTER TABLE chunks_meta ADD COLUMN figure_label VARCHAR(64) NULL",
                "ALTER TABLE chunks_meta ADD COLUMN figure_caption TEXT NULL",
                "ALTER TABLE documents ADD INDEX idx_md5 (file_md5)",
            ):
                try:
                    await conn.execute(text(mig))
                except Exception:
                    pass  # 列/索引已存在
        self._initialized = True

    # ── 文档元数据 ─────────────────────────────────────────

    async def upsert_document(self, doc: DocumentMeta) -> None:
        await self._ensure_tables()
        d = doc.model_dump(mode="json")
        d["allowed_roles"] = json.dumps(d.get("allowed_roles") or [])
        d["quality_report"] = json.dumps(d.get("quality_report") or {})
        d["created_at"] = d.get("created_at") or datetime.utcnow()
        d["updated_at"] = datetime.utcnow()
        async with self._session() as s:
            await s.execute(text("""
                INSERT INTO documents
                  (doc_id, tenant_id, collection, filename, file_type,
                   file_size, file_md5, storage_url, language, page_count,
                   status, chunk_count, allowed_roles, version,
                   quality_report, created_by, created_at, updated_at)
                VALUES
                  (:doc_id, :tenant_id, :collection, :filename, :file_type,
                   :file_size, :file_md5, :storage_url, :language, :page_count,
                   :status, :chunk_count, :allowed_roles, :version,
                   :quality_report, :created_by, :created_at, :updated_at)
                ON DUPLICATE KEY UPDATE
                  status=VALUES(status), chunk_count=VALUES(chunk_count),
                  quality_report=VALUES(quality_report),
                  storage_url=VALUES(storage_url),
                  page_count=VALUES(page_count), version=VALUES(version),
                  updated_at=NOW()
            """), d)
            await s.commit()

    async def update_doc_status(self, doc_id: str, status: IngestStatus,
                                chunk_count: int | None = None) -> None:
        async with self._session() as s:
            if chunk_count is not None:
                await s.execute(text(
                    "UPDATE documents SET status=:st, chunk_count=:cc, "
                    "updated_at=NOW() WHERE doc_id=:id"),
                    {"st": status.value, "cc": chunk_count, "id": doc_id})
            else:
                await s.execute(text(
                    "UPDATE documents SET status=:st, updated_at=NOW() "
                    "WHERE doc_id=:id"), {"st": status.value, "id": doc_id})
            await s.commit()

    async def get_document(self, doc_id: str, tenant_id: str) -> DocumentMeta | None:
        await self._ensure_tables()
        async with self._session() as s:
            row = await s.execute(text(
                "SELECT * FROM documents WHERE doc_id=:id AND tenant_id=:t"),
                {"id": doc_id, "t": tenant_id})
            r = row.mappings().first()
            return self._row_to_doc(r) if r else None

    async def list_documents(self, tenant_id: str, collection: str | None = None,
                             status: IngestStatus | None = None,
                             limit: int = 100, offset: int = 0) -> list[DocumentMeta]:
        await self._ensure_tables()
        filters = ["tenant_id=:t"]
        params: dict = {"t": tenant_id}
        if collection:
            filters.append("collection=:c"); params["c"] = collection
        if status:
            filters.append("status=:s"); params["s"] = status.value
        params.update({"limit": limit, "offset": offset})
        async with self._session() as s:
            rows = await s.execute(text(
                f"SELECT * FROM documents WHERE {' AND '.join(filters)} "
                f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"), params)
            return [self._row_to_doc(r) for r in rows.mappings()]

    async def delete_document(self, doc_id: str, tenant_id: str) -> None:
        async with self._session() as s:
            await s.execute(text(
                "DELETE FROM documents WHERE doc_id=:id AND tenant_id=:t"),
                {"id": doc_id, "t": tenant_id})
            await s.commit()

    async def find_doc_by_md5(self, file_md5: str, tenant_id: str) -> DocumentMeta | None:
        async with self._session() as s:
            row = await s.execute(text(
                "SELECT * FROM documents WHERE file_md5=:m AND tenant_id=:t "
                "AND status='done' ORDER BY created_at DESC LIMIT 1"),
                {"m": file_md5, "t": tenant_id})
            r = row.mappings().first()
            return self._row_to_doc(r) if r else None

    @staticmethod
    def _row_to_doc(r: dict) -> DocumentMeta:
        d = dict(r)
        for k in ("allowed_roles", "quality_report"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    d[k] = None
        return DocumentMeta(**d)

    # ── Chunk 元数据 ───────────────────────────────────────

    async def upsert_chunks(self, chunks: list[ChunkMeta],
                            texts: dict[str, str] | None = None) -> None:
        await self._ensure_tables()
        texts = texts or {}
        batch_size = 500
        async with self._session() as s:
            for i in range(0, len(chunks), batch_size):
                batch = chunks[i:i + batch_size]
                values = []
                for c in batch:
                    d = c.model_dump(mode="json")
                    d["allowed_roles"] = json.dumps(d.get("allowed_roles") or [])
                    d["is_parent"] = 1 if d.get("is_parent") else 0
                    if texts.get(c.chunk_id):
                        d["text"] = texts[c.chunk_id]
                    else:
                        d.pop("text", None)
                    values.append(d)
                await s.execute(text("""
                    INSERT INTO chunks_meta
                      (chunk_id, doc_id, tenant_id, collection, chunk_type,
                       is_parent, parent_chunk_id, page_num, section_path,
                       quality_score, token_count, allowed_roles,
                       figure_label, figure_caption, created_at)
                    VALUES
                      (:chunk_id, :doc_id, :tenant_id, :collection, :chunk_type,
                       :is_parent, :parent_chunk_id, :page_num, :section_path,
                       :quality_score, :token_count, :allowed_roles,
                       :figure_label, :figure_caption, :created_at)
                    ON DUPLICATE KEY UPDATE
                      quality_score=VALUES(quality_score),
                      token_count=VALUES(token_count),
                      section_path=VALUES(section_path),
                      figure_label=VALUES(figure_label),
                      figure_caption=VALUES(figure_caption)
                """), values)
                # 正文单独写入（避免插入参数过大）
                text_vals = [{"cid": c.chunk_id, "txt": texts[c.chunk_id]}
                             for c in batch if texts.get(c.chunk_id)]
                if text_vals:
                    await s.execute(text(
                        "UPDATE chunks_meta SET text=:txt WHERE chunk_id=:cid"),
                        text_vals)
            await s.commit()

    async def get_chunk_texts(self, chunk_ids: list[str]) -> dict[str, str]:
        """按 chunk_id 批量取回正文（用于父子回补与巡检修复）"""
        if not chunk_ids:
            return {}
        await self._ensure_tables()
        out: dict[str, str] = {}
        async with self._session() as s:
            for i in range(0, len(chunk_ids), 500):
                sub = chunk_ids[i:i + 500]
                ph = ",".join(f":id{i}" for i in range(len(sub)))
                rows = await s.execute(text(
                    f"SELECT chunk_id, text FROM chunks_meta "
                    f"WHERE chunk_id IN ({ph})"),
                    {f"id{i}": v for i, v in enumerate(sub)})
                for cid, txt in rows:
                    if txt:
                        out[cid] = txt
        return out

    async def list_collections(self, tenant_id: str) -> list[str]:
        """该租户下已有数据的逻辑集合清单"""
        await self._ensure_tables()
        async with self._session() as s:
            rows = await s.execute(text(
                "SELECT DISTINCT collection FROM documents "
                "WHERE tenant_id=:t AND status IN ('done','partial')"),
                {"t": tenant_id})
            return [r[0] for r in rows]

    async def adjust_chunk_quality(self, chunk_id: str, delta: float,
                                   floor: float = 0.05) -> float | None:
        """反馈闭环：微调 chunk 质量分（负增量=降权），返回新分数"""
        await self._ensure_tables()
        async with self._session() as s:
            await s.execute(text(
                "UPDATE chunks_meta SET quality_score="
                "LEAST(1.0, GREATEST(:floor, quality_score + :delta)) "
                "WHERE chunk_id=:cid"),
                {"floor": floor, "delta": delta, "cid": chunk_id})
            await s.commit()
            row = await s.execute(text(
                "SELECT quality_score FROM chunks_meta WHERE chunk_id=:cid"),
                {"cid": chunk_id})
            val = row.scalar_one_or_none()
            return float(val) if val is not None else None

    async def query_chunk_ids(
        self, tenant_id: str,
        collections: list[str] | None = None,
        file_types: list[str] | None = None,
        date_from: Any = None, date_to: Any = None,
        allowed_roles: list[str] | None = None,
        extra_hints: dict | None = None,
    ) -> list[str]:
        """元数据前置过滤（上限 50000）。
        两阶段：1) documents 过滤出可见候选文档（含秒传别名文档，
        其物理 chunk 挂在源文档上）；2) 按 file_md5 扩展出物理文档
        集合，再取 chunks。角色过滤对文档/块均遵循"空数组=公开"。"""
        await self._ensure_tables()
        # ── 阶段 1：候选文档（含秒传别名）─────────────────
        conds = ["tenant_id = :tenant_id", "status IN ('done','partial')"]
        params: dict = {"tenant_id": tenant_id}
        if collections and "*" not in collections:
            ph = ",".join(f":col{i}" for i in range(len(collections)))
            conds.append(f"collection IN ({ph})")
            params.update({f"col{i}": v for i, v in enumerate(collections)})
        if file_types:
            ph = ",".join(f":ft{i}" for i in range(len(file_types)))
            conds.append(f"file_type IN ({ph})")
            params.update({f"ft{i}": v for i, v in enumerate(file_types)})
        if date_from:
            conds.append("created_at >= :date_from")
            params["date_from"] = date_from
        if date_to:
            conds.append("created_at <= :date_to")
            params["date_to"] = date_to
        if allowed_roles:
            role_conds = []
            for i, r in enumerate(allowed_roles):
                role_conds.append(f"JSON_CONTAINS(allowed_roles, :role{i}, '$')")
                params[f"role{i}"] = json.dumps(r)
            conds.append(
                "(JSON_LENGTH(COALESCE(allowed_roles, JSON_ARRAY())) = 0"
                f" OR {' OR '.join(role_conds)})")
        async with self._session() as s:
            rows = await s.execute(text(
                f"SELECT doc_id, file_md5 FROM documents "
                f"WHERE {' AND '.join(conds)} LIMIT 20000"), params)
            doc_ids = {r[0] for r in rows}
            md5s = {r[1] for r in rows if r[1]}
            # ── 阶段 2：md5 扩展物理文档（秒传别名→源文档）──
            if md5s:
                ph = ",".join(f":m{i}" for i in range(len(md5s)))
                params2 = {"tenant_id": tenant_id,
                           **{f"m{i}": v for i, v in enumerate(md5s)}}
                rows2 = await s.execute(text(
                    f"SELECT doc_id FROM documents WHERE tenant_id=:tenant_id "
                    f"AND file_md5 IN ({ph}) "
                    f"AND status IN ('done','partial')"), params2)
                doc_ids |= {r[0] for r in rows2}
            if not doc_ids:
                return []
            # ── 阶段 3：取 chunk（块级角色过滤沿用空=公开）──
            ph = ",".join(f":d{i}" for i in range(len(doc_ids)))
            chunk_conds = ["tenant_id = :tenant_id", f"doc_id IN ({ph})"]
            params3 = {"tenant_id": tenant_id,
                       **{f"d{i}": v for i, v in enumerate(doc_ids)}}
            if allowed_roles:
                rc = []
                for i, r in enumerate(allowed_roles):
                    rc.append(f"JSON_CONTAINS(allowed_roles, :cr{i}, '$')")
                    params3[f"cr{i}"] = json.dumps(r)
                chunk_conds.append(
                    "(JSON_LENGTH(COALESCE(allowed_roles, JSON_ARRAY())) = 0"
                    f" OR {' OR '.join(rc)})")
            if extra_hints and extra_hints.get("section_keyword"):
                chunk_conds.append("section_path LIKE :section_kw")
                params3["section_kw"] = f"{extra_hints['section_keyword']}%"
            rows3 = await s.execute(text(
                f"SELECT chunk_id FROM chunks_meta "
                f"WHERE {' AND '.join(chunk_conds)} LIMIT 50000"), params3)
            return [row[0] for row in rows3]

    async def list_chunk_ids(self, doc_id: str) -> list[str]:
        async with self._session() as s:
            rows = await s.execute(text(
                "SELECT chunk_id FROM chunks_meta WHERE doc_id=:d"), {"d": doc_id})
            return [row[0] for row in rows]

    async def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[ChunkMeta]:
        if not chunk_ids:
            return []
        out: list[ChunkMeta] = []
        async with self._session() as s:
            for i in range(0, len(chunk_ids), 500):
                sub = chunk_ids[i:i + 500]
                ph = ",".join(f":id{i}" for i in range(len(sub)))
                rows = await s.execute(text(
                    f"SELECT * FROM chunks_meta WHERE chunk_id IN ({ph})"),
                    {f"id{i}": v for i, v in enumerate(sub)})
                for r in rows.mappings():
                    d = dict(r)
                    if isinstance(d.get("allowed_roles"), str):
                        d["allowed_roles"] = json.loads(d["allowed_roles"])
                    d["is_parent"] = bool(d.get("is_parent"))
                    out.append(ChunkMeta(**d))
        return out

    # ── 表格数据 ───────────────────────────────────────────

    async def upsert_table_data(self, tables: list[TableData]) -> None:
        await self._ensure_tables()
        async with self._session() as s:
            for t in tables:
                d = t.model_dump(mode="json")
                d["headers"] = json.dumps(d.get("headers") or [])
                d["row_data"] = json.dumps(d.get("row_data") or [])
                d["numeric_stats"] = json.dumps(d.get("numeric_stats") or {})
                await s.execute(text("""
                    INSERT INTO table_data
                      (chunk_id, doc_id, tenant_id, table_index, page_num,
                       headers, row_data, row_count, numeric_stats, created_at)
                    VALUES
                      (:chunk_id, :doc_id, :tenant_id, :table_index, :page_num,
                       :headers, :row_data, :row_count, :numeric_stats, NOW())
                """), d)
            await s.commit()

    # ── 任务 ───────────────────────────────────────────────

    async def save_task(self, task: IngestTask) -> None:
        await self._ensure_tables()
        async with self._session() as s:
            await s.execute(text("""
                INSERT INTO ingest_tasks
                  (task_id, doc_id, batch_id, tenant_id, collection, filename,
                   file_md5, file_type, status, retry_count, max_retries,
                   error_message, checkpoint, total_pages, processed_pages,
                   total_chunks, written_chunks, quality_summary, source_type,
                   submitted_by, started_at, completed_at, created_at)
                VALUES
                  (:task_id, :doc_id, :batch_id, :tenant_id, :collection,
                   :filename, :file_md5, :file_type, :status, :retry_count,
                   :max_retries, :error_message, :checkpoint, :total_pages,
                   :processed_pages, :total_chunks, :written_chunks,
                   :quality_summary, :source_type, :submitted_by,
                   :started_at, :completed_at, :created_at)
                ON DUPLICATE KEY UPDATE
                  doc_id=VALUES(doc_id), status=VALUES(status),
                  retry_count=VALUES(retry_count), error_message=VALUES(error_message),
                  checkpoint=VALUES(checkpoint), processed_pages=VALUES(processed_pages),
                  total_chunks=VALUES(total_chunks), written_chunks=VALUES(written_chunks),
                  quality_summary=VALUES(quality_summary),
                  started_at=VALUES(started_at), completed_at=VALUES(completed_at)
            """), self._task_to_dict(task))
            await s.commit()

    @staticmethod
    def _task_to_dict(task: IngestTask) -> dict:
        d = task.model_dump(mode="json")
        d["checkpoint"] = json.dumps(d.get("checkpoint") or {})
        d["quality_summary"] = json.dumps(d.get("quality_summary") or {})
        return d

    async def get_task(self, task_id: str,
                       tenant_id: str = "") -> IngestTask | None:
        sql = "SELECT * FROM ingest_tasks WHERE task_id=:t"
        params: dict = {"t": task_id}
        if tenant_id:
            sql += " AND tenant_id=:ten"
            params["ten"] = tenant_id
        async with self._session() as s:
            row = await s.execute(text(sql), params)
            r = row.mappings().first()
            return self._row_to_task(r) if r else None

    async def list_tasks(self, tenant_id: str, collection: str | None = None,
                         status: IngestStatus | None = None,
                         limit: int = 20, offset: int = 0,
                         batch_id: str | None = None) -> list[IngestTask]:
        await self._ensure_tables()
        filters = ["tenant_id=:t"]
        params: dict = {"t": tenant_id}
        if collection:
            filters.append("collection=:c"); params["c"] = collection
        if status:
            filters.append("status=:s"); params["s"] = status.value
        if batch_id:
            filters.append("batch_id=:b"); params["b"] = batch_id
        params.update({"limit": limit, "offset": offset})
        async with self._session() as s:
            rows = await s.execute(text(
                f"SELECT * FROM ingest_tasks WHERE {' AND '.join(filters)} "
                f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"), params)
            return [self._row_to_task(r) for r in rows.mappings()]

    async def find_by_md5(self, file_md5: str, tenant_id: str) -> IngestTask | None:
        async with self._session() as s:
            row = await s.execute(text(
                "SELECT * FROM ingest_tasks WHERE file_md5=:m AND tenant_id=:t "
                "ORDER BY created_at DESC LIMIT 1"),
                {"m": file_md5, "t": tenant_id})
            r = row.mappings().first()
            return self._row_to_task(r) if r else None

    async def find_incomplete_tasks(self) -> list[IngestTask]:
        """所有未完成任务（含中断在解析/分块/嵌入阶段的孤儿任务）"""
        async with self._session() as s:
            rows = await s.execute(text(
                "SELECT * FROM ingest_tasks WHERE status IN "
                "('pending','parsing','chunking','embedding','writing','retrying') "
                "AND created_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)"))
            return [self._row_to_task(r) for r in rows.mappings()]

    @staticmethod
    def _row_to_task(r: dict) -> IngestTask:
        d = dict(r)
        for k in ("checkpoint", "quality_summary"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    d[k] = {}
        return IngestTask(**d)

    # ── 批次 ───────────────────────────────────────────────

    async def save_batch(self, batch: IngestBatch) -> None:
        await self._ensure_tables()
        async with self._session() as s:
            await s.execute(text("""
                INSERT INTO ingest_batches
                  (batch_id, tenant_id, collection, total, succeeded, failed,
                   pending, status, source_type, created_at)
                VALUES
                  (:batch_id, :tenant_id, :collection, :total, :succeeded,
                   :failed, :pending, :status, :source_type, :created_at)
                ON DUPLICATE KEY UPDATE
                  total=VALUES(total), succeeded=VALUES(succeeded),
                  failed=VALUES(failed), pending=VALUES(pending),
                  status=VALUES(status)
            """), batch.model_dump(mode="json"))
            await s.commit()

    async def get_batch(self, batch_id: str) -> IngestBatch | None:
        async with self._session() as s:
            row = await s.execute(text(
                "SELECT * FROM ingest_batches WHERE batch_id=:b"), {"b": batch_id})
            r = row.mappings().first()
            return IngestBatch(**dict(r)) if r else None

    async def list_batches(self, tenant_id: str, limit: int = 20,
                           offset: int = 0) -> list[IngestBatch]:
        await self._ensure_tables()
        async with self._session() as s:
            rows = await s.execute(text(
                "SELECT * FROM ingest_batches WHERE tenant_id=:t "
                "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
                {"t": tenant_id, "limit": limit, "offset": offset})
            return [IngestBatch(**dict(r)) for r in rows.mappings()]

    async def recompute_batch(self, batch_id: str) -> IngestBatch | None:
        """按子任务状态重算批次聚合字段"""
        async with self._session() as s:
            rows = await s.execute(text("""
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN status IN ('done','partial') THEN 1 ELSE 0 END) AS succeeded,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                  SUM(CASE WHEN status IN ('pending','parsing','chunking',
                                           'embedding','writing','retrying')
                           THEN 1 ELSE 0 END) AS pending
                FROM ingest_tasks WHERE batch_id=:b
            """), {"b": batch_id})
            r = rows.mappings().first()
            batch = await self.get_batch(batch_id)
            if not batch or not r:
                return batch
            batch.total = int(r["total"] or 0)
            batch.succeeded = int(r["succeeded"] or 0)
            batch.failed = int(r["failed"] or 0)
            batch.pending = int(r["pending"] or 0)
            if batch.total > 0 and batch.succeeded == batch.total:
                batch.status = "done"
            elif batch.failed > 0 and batch.pending == 0:
                batch.status = "partial_failed"
            elif batch.pending > 0:
                batch.status = "running"
            await self.save_batch(batch)
            return batch

    # ── 反馈 ───────────────────────────────────────────────

    async def save_feedback(self, fb: MessageFeedback) -> None:
        await self._ensure_tables()
        d = fb.model_dump(mode="json")
        for k in ("sources", "reasons", "retrieval_paths"):
            d[k] = json.dumps(d.get(k) or ([] if k != "retrieval_paths" else {}))
        async with self._session() as s:
            await s.execute(text("""
                INSERT INTO feedback
                  (feedback_id, session_id, message_id, tenant_id, user_id,
                   feedback, query, answer, sources, reasons, comment,
                   retrieval_paths, created_at)
                VALUES
                  (:feedback_id, :session_id, :message_id, :tenant_id, :user_id,
                   :feedback, :query, :answer, :sources, :reasons, :comment,
                   :retrieval_paths, :created_at)
                ON DUPLICATE KEY UPDATE
                  feedback=VALUES(feedback), reasons=VALUES(reasons),
                  comment=VALUES(comment), sources=VALUES(sources)
            """), d)
            await s.commit()

    async def list_negative_feedback(self, days: int = 30,
                                     limit: int = 1000) -> list[MessageFeedback]:
        async with self._session() as s:
            rows = await s.execute(text(
                "SELECT * FROM feedback WHERE feedback='down' AND "
                "created_at > DATE_SUB(NOW(), INTERVAL :d DAY) "
                "ORDER BY created_at DESC LIMIT :l"),
                {"d": days, "l": limit})
            out = []
            for r in rows.mappings():
                d = dict(r)
                for k in ("sources", "reasons", "retrieval_paths"):
                    if isinstance(d.get(k), str):
                        d[k] = json.loads(d[k])
                out.append(MessageFeedback(**d))
            return out

    # ── 用户画像 ───────────────────────────────────────────

    async def upsert_profile(self, profile: UserProfile) -> None:
        await self._ensure_tables()
        async with self._session() as s:
            await s.execute(text("""
                INSERT INTO user_profiles (user_id, tenant_id, profile)
                VALUES (:user_id, :tenant_id, :profile)
                ON DUPLICATE KEY UPDATE profile=VALUES(profile)
            """), {"user_id": profile.user_id, "tenant_id": profile.tenant_id,
                   "profile": json.dumps(profile.model_dump(mode="json"),
                                         ensure_ascii=False)})
            await s.commit()

    async def get_profile(self, user_id: str, tenant_id: str) -> UserProfile | None:
        async with self._session() as s:
            row = await s.execute(text(
                "SELECT profile FROM user_profiles "
                "WHERE user_id=:u AND tenant_id=:t"), {"u": user_id, "t": tenant_id})
            r = row.first()
            if not r or not r[0]:
                return None
            try:
                return UserProfile(**json.loads(r[0]))
            except (json.JSONDecodeError, TypeError):
                return None

    # ── 实体（EntityLinker 规则阶段）───────────────────────

    async def list_entities(self, tenant_id: str, limit: int = 50000) -> list[dict]:
        async with self._session() as s:
            rows = await s.execute(text(
                "SELECT name, canonical, entity_type FROM entities "
                "WHERE tenant_id=:t LIMIT :l"), {"t": tenant_id, "l": limit})
            return [dict(r) for r in rows.mappings()]

    async def upsert_entities(self, tenant_id: str, entities: list[dict]) -> None:
        await self._ensure_tables()
        async with self._session() as s:
            for e in entities:
                await s.execute(text("""
                    INSERT INTO entities (tenant_id, doc_id, name, canonical, entity_type)
                    VALUES (:t, :d, :name, :canonical, :etype)
                    ON DUPLICATE KEY UPDATE canonical=VALUES(canonical)
                """), {"t": tenant_id, "d": e.get("doc_id"),
                       "name": e["name"], "canonical": e.get("canonical") or e["name"],
                       "etype": e.get("entity_type", "generic")})
            await s.commit()

    # ── 统计 ───────────────────────────────────────────────

    async def collection_stats(self, tenant_id: str) -> list[dict]:
        await self._ensure_tables()
        async with self._session() as s:
            rows = await s.execute(text("""
                SELECT collection,
                       COUNT(DISTINCT doc_id) AS doc_count,
                       COUNT(*) AS chunk_count
                FROM chunks_meta WHERE tenant_id=:t
                GROUP BY collection
            """), {"t": tenant_id})
            return [dict(r) for r in rows.mappings()]

    async def health_detail(self) -> tuple[bool, str]:
        """返回 (是否可用, 可读原因)；失败原因不再被吞掉

        另见模块级 _mysql_failure_reason：把 errno 翻译成「该去服务端做什么」，
        避免配置页只显示一句无信息的"连接失败"（TS-011）。
        """
        try:
            async with self._session() as s:
                await s.execute(text("SELECT 1"))
            return True, "MySQL 连接正常"
        except Exception as e:
            log.warning("mysql_health_failed", error=str(e)[:200])
            return False, _mysql_failure_reason(e)

    async def health_check(self) -> bool:
        ok, _ = await self.health_detail()
        return ok

    async def health_probe(self) -> tuple[bool, str]:
        """带预算的健康检查：容器自检与配置页「测试连接」共用同一口径

        用同一个 HEALTH_BUDGET_SEC 包住 health_detail，两条链路的结论才会一致：
        否则「测试连接」无预算、慢慢等满后报成功，而容器自检用另一个（更短的）
        预算判死，用户看到的就是"页面测通了、保存却说不可达"（TS-014）。
        超时不再笼统写成"不可达"，而是给出"建连超时 + 服务端名称解析"的线索。
        """
        budget = HEALTH_BUDGET_SEC
        started = time.perf_counter()
        try:
            ok, reason = await asyncio.wait_for(self.health_detail(),
                                                timeout=budget)
        except (asyncio.TimeoutError, TimeoutError):
            elapsed = time.perf_counter() - started
            log.warning("mysql_health_timeout", budget=budget,
                        elapsed=round(elapsed, 2), host=self._config.host)
            return False, mysql_timeout_reason(self._config, budget, elapsed)
        elapsed = time.perf_counter() - started
        if ok and elapsed >= SLOW_CONNECT_SEC:
            # "等满但没超预算"最容易被当成"正常只是慢"：这里主动把原因说清楚，
            # 否则用户只会看到每次建连都要 10 秒，日志里却没有任何线索（TS-014）
            log.warning("mysql_slow_connect", elapsed=round(elapsed, 2),
                        host=self._config.host,
                        hint="TCP 早已连通，这 %s 秒等的是服务端问候包，"
                             "常见于服务端在建连阶段做反向 DNS 解析"
                             "（skip_name_resolve=OFF）" % f"{elapsed:.1f}")
        return ok, reason

    async def close(self) -> None:
        """释放本适配器的连接池

        每个适配器都持有自己的引擎（探测一律真实建连、不复用缓存连接），
        因此这里要真正 dispose：否则每次热重建（保存配置、探测一次）都会在
        后台留下一条永不回收的空闲连接。
        """
        await self._engine.dispose()
