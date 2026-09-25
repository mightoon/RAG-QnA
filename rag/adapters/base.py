"""
适配器抽象基类（rag/adapters/base.py）

框架核心只调用这些抽象基类的方法，不依赖任何具体实现。
客户自定义适配器：继承对应基类 → 实现方法 → 注册到 AdapterRegistry。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Iterable

from rag.models import (
    ChunkMeta, DocumentMeta, IngestBatch, IngestStatus, IngestTask,
    ParsedDocument, RetrievedChunk, TableData, UserContext, UserProfile,
    MessageFeedback, QueryConstraint,
)


# ═══════════════════════════════════════════════════════════
# LLM
# ═══════════════════════════════════════════════════════════

class LLMAdapter(ABC):
    """
    大语言模型适配器。
    默认实现兼容 OpenAI Chat Completions 格式；
    task 参数允许路由到不同模型（rewrite/summary/generate/evaluate）。
    """

    @abstractmethod
    async def generate(
        self,
        messages: list[dict],
        task: str = "generate",
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        thinking: bool | None = None,
    ) -> str:
        """非流式生成，返回完整文本

        `response_format`：结构化输出约束（OpenAI 兼容接口的
        `{"type": "json_object"}`）。要求"必须回 JSON"的场景（摘要/关键词/实体）
        应当传它 —— 靠 prompt 求模型吐 JSON 在推理模型上并不可靠：思考过程会先把
        `max_tokens` 吃光，`content` 为空、只剩 `reasoning_content`。
        实现若不被服务端支持，应自动去掉该参数重试，而不是让整条链路失败。

        `thinking`：是否让推理模型"别思考"（**内部参数**，不作为配置项暴露给客户）。
        `None` = 按内置策略：摘要/抽取/分类/评估/看图描述这类"短且结构化"的任务关掉
        思考，开放式作答保留。关闭方式随服务端而异（DeepSeek `thinking.type`、
        OpenAI `reasoning_effort`、自托管 `chat_template_kwargs`），实现应自行探测
        并在不生效时降级，绝不能因为"关不掉"就抛错。
        """

    @abstractmethod
    async def stream_generate(
        self,
        messages: list[dict],
        task: str = "generate",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式生成，逐 token yield"""

    def count_tokens(self, text: str) -> int:
        """粗略 token 计数（中文≈字数，英文≈词数×1.3）"""
        cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        rest = len(text) - cjk
        return cjk + int(rest / 3.5) + 1

    async def health_check(self) -> bool:
        return True


# ═══════════════════════════════════════════════════════════
# Embedding
# ═══════════════════════════════════════════════════════════

class EmbeddingAdapter(ABC):
    """向量化适配器。入库与检索必须使用同一实例保证向量空间一致。"""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（入库用，无查询前缀）"""

    @abstractmethod
    async def embed_query(self, text: str) -> list[float]:
        """查询向量化（BGE 系列注入 query_prefix）"""

    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度"""

    async def health_check(self) -> bool:
        return True


# ═══════════════════════════════════════════════════════════
# VectorStore
# ═══════════════════════════════════════════════════════════

class VectorStoreAdapter(ABC):
    """向量库适配器：写入 / ANN 检索 / 按 doc_id 批量删除。"""

    @abstractmethod
    async def ensure_collection(self, name: str, dim: int) -> None:
        """确保 collection 存在（不存在则创建）"""

    @abstractmethod
    async def upsert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        metadatas: list[dict],
    ) -> None:
        """批量 upsert（幂等，按 id 覆盖）"""

    @abstractmethod
    async def search(
        self,
        collection: str,
        vector: list[float],
        top_k: int = 20,
        filter: dict | None = None,      # {"tenant_id":.., "allowed_roles":.., "chunk_ids":[..]}
        nprobe: int = 16,
    ) -> list[RetrievedChunk]:
        """ANN 检索"""

    @abstractmethod
    async def delete_by_doc(self, collection: str, doc_id: str) -> int:
        """按 doc_id 删除该文档所有向量，返回删除数"""

    @abstractmethod
    async def delete_by_ids(self, collection: str, ids: list[str]) -> int:
        """按 chunk_id 批量删除，返回删除数（"-1" = 该后端不返回计数）

        入库重跑时要清掉"上次留下、这次没再产出"的旧块，调用方把返回值写进日志
        与运维结论，所以**不要把计数丢掉**。
        """

    @abstractmethod
    async def get_doc_chunk_ids(self, collection: str, doc_id: str) -> set[str]:
        """一致性巡检：取该文档在向量库中的全部 chunk_id"""

    async def health_check(self) -> bool:
        return True

    # ── 向量空间指纹（可选能力，判据在 rag/vector_space.py）──────────
    # 一个 collection 里的向量必须来自同一个向量空间（同一实现 + 同一模型 +
    # 同一维度），否则 ANN 会静默返回同维但不同源的噪声，见该模块开头。
    # 载体自选：Milvus 用 collection properties（建集合之后仍可写，description
    # 不行）。**未实现**的适配器保持 space_tag_supported = False —— 容器会据此
    # 如实说明"无法校验"，而不是假装校验通过。

    space_tag_supported: bool = False

    async def read_space_tag(self, collection: str) -> str | None:
        """读该集合记录的空间指纹（无 / 不支持 → None）"""
        return None

    async def write_space_tag(self, collection: str, tag: str) -> bool:
        """把指纹写到集合上（成功 → True；载体不支持/写失败 → False，不抛异常）"""
        return False

    async def collection_rows(self, collection: str) -> int | None:
        """集合内的向量条数（判"空集合"用）；读不到返回 None

        None 与 0 必须区分：0 = 确认是空集合（可安全打标/放行），None = 拿不准
        （按"可能有数据"保守处理，见 vector_space.judge_space）。
        """
        return None


# ═══════════════════════════════════════════════════════════
# FullTextSearch
# ═══════════════════════════════════════════════════════════

class FullTextSearchAdapter(ABC):
    """
    全文检索适配器。
    索引结构：content（IK 分词，BM25）、content.kw_exact（keyword 不分词）、
    keywords（专有名词）、summary（高权重）。
    """

    @abstractmethod
    async def ensure_index(self, index: str) -> None:
        """确保索引存在（含 IK 分词 + kw_exact 子字段 mapping）"""

    @abstractmethod
    async def upsert_chunks(
        self, index: str, chunks: list[ChunkMeta],
        texts: list[str], summaries: list[str | None], keywords: list[list[str]],
        doc: dict | None = None,
    ) -> None:
        """批量写入 Chunk 全文索引

        `doc` 为文档级字段（filename / file_type / created_at）：mapping 里声明了
        它们，但每个 chunk 都不携带，必须由调用方从 DocumentMeta 带进来 ——
        不写的话，Kibana 里按时间字段筛选会因为"字段根本不存在"而查不到任何文档，
        引用来源也拿不到文件名。
        """

    @abstractmethod
    async def search(
        self,
        index: str,
        query: str,
        top_k: int = 20,
        filter: dict | None = None,
        synonym_boost_terms: list[str] | None = None,   # 同义词降权合并
    ) -> list[RetrievedChunk]:
        """BM25 全文检索（summary 权重 1.5，content 1.0）"""

    @abstractmethod
    async def search_exact(
        self,
        index: str,
        terms: list[str],                    # 实体规范名/型号/编号
        top_k: int = 20,
        filter: dict | None = None,
    ) -> list[RetrievedChunk]:
        """kw_exact 精确匹配（content.kw_exact + keywords 字段 term 查询）"""

    @abstractmethod
    async def delete_by_doc(self, index: str, doc_id: str) -> int:
        """按 doc_id 删除"""

    @abstractmethod
    async def get_doc_chunk_ids(self, index: str, doc_id: str) -> set[str]:
        """一致性巡检辅助"""

    async def delete_by_ids(self, index: str, chunk_ids: list[str]) -> int:
        """按 chunk_id 批量删除（重跑清理旧块用）；返回删除条数。

        默认返回 0：不支持的实现不会因此中断入库，只是清理不生效。
        """
        return 0

    async def get_doc_enrichment(self, index: str,
                                 doc_id: str) -> dict[str, dict]:
        """取该文档各块的增强字段 `{chunk_id: {"summary":…, "keywords":[…]}}`

        供一致性巡检**补写**时保留原有摘要/关键词：补写走的是同一个 upsert，
        传空值就等于把 ES 里已有的增强结果抹掉（而这两样在 MySQL 没有副本）。
        """
        return {}

    async def health_check(self) -> bool:
        return True


# ═══════════════════════════════════════════════════════════
# MetaStore（元数据库）
# ═══════════════════════════════════════════════════════════

class MetaStoreAdapter(ABC):
    """
    元数据库适配器（注册类型 meta）：文档/Chunk/表格/任务/批次/反馈/画像的
    CRUD，以及元数据前置过滤（query_chunk_ids 白名单）。
    首次启动自动建表（DDL 幂等）。
    """

    # ── 文档元数据 ─────────────────────────────────────────
    @abstractmethod
    async def upsert_document(self, doc: DocumentMeta) -> None: ...

    @abstractmethod
    async def update_doc_status(self, doc_id: str, status: IngestStatus,
                                chunk_count: int | None = None) -> None: ...

    @abstractmethod
    async def get_document(self, doc_id: str, tenant_id: str) -> DocumentMeta | None: ...

    @abstractmethod
    async def list_documents(self, tenant_id: str, collection: str | None = None,
                             status: IngestStatus | None = None,
                             limit: int = 100, offset: int = 0) -> list[DocumentMeta]: ...

    @abstractmethod
    async def delete_document(self, doc_id: str, tenant_id: str) -> None: ...

    @abstractmethod
    async def soft_delete_document(self, doc_id: str,
                                   tenant_id: str) -> str | None:
        """移入回收站（status=deleted）：只改状态与 deleted_at/prev_status，
        **不动任何内容**（chunks / 向量 / 全文索引 / 对象存储都原样保留），
        这样"恢复"才是原样回来。返回删除前的状态；文档不存在返回 None。"""

    @abstractmethod
    async def restore_document(self, doc_id: str,
                               tenant_id: str) -> str | None:
        """从回收站恢复：把 status 还原成 prev_status（老数据按有无块兜底），
        并清掉 deleted_at/prev_status。返回恢复后的状态。"""

    @abstractmethod
    async def find_doc_by_md5(self, file_md5: str, tenant_id: str) -> DocumentMeta | None: ...

    # ── Chunk 元数据 ───────────────────────────────────────
    @abstractmethod
    async def upsert_chunks(self, chunks: list[ChunkMeta],
                            texts: dict[str, str] | None = None) -> None:
        """批量写入 chunk 元数据；texts 可选，键值 chunk_id→正文，
        用于父子回补与一致性巡检修复。"""

    @abstractmethod
    async def query_chunk_ids(
        self, tenant_id: str,
        collections: list[str] | None = None,
        file_types: list[str] | None = None,
        date_from: Any = None, date_to: Any = None,
        allowed_roles: list[str] | None = None,
        extra_hints: dict | None = None,       # section_keyword / ephemeral_only
    ) -> list[str]:
        """元数据前置过滤：返回符合条件 chunk_id 白名单（上限 50000）。
        allowed_roles 语义：文档/块角色列表为空 = 公开；否则须含用户角色之一。"""

    @abstractmethod
    async def list_chunk_ids(self, doc_id: str) -> list[str]: ...

    @abstractmethod
    async def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[ChunkMeta]: ...

    async def get_chunk_texts(self, chunk_ids: list[str]) -> dict[str, str]:
        """按 chunk_id 批量取回正文；不支持正文字段的实现返回空字典。"""
        return {}

    async def delete_chunks(self, chunk_ids: list[str]) -> int:
        """按 chunk_id 批量删除元数据（重跑清理旧块用）；返回删除条数。

        默认返回 0（不支持则不动），调用方据返回值判断是否真的清掉。
        """
        return 0

    async def list_collections(self, tenant_id: str) -> list[str]:
        """该租户下已有数据的逻辑集合清单（供多集合检索展开 "*"）"""
        return ["default"]

    async def adjust_chunk_quality(self, chunk_id: str, delta: float,
                                   floor: float = 0.05) -> float | None:
        """反馈闭环：微调 chunk 质量分；返回新分数或不支持时返回 None"""
        return None

    # ── 表格数据 ───────────────────────────────────────────
    @abstractmethod
    async def upsert_table_data(self, tables: list[TableData]) -> None: ...

    # ── 入库任务/批次 ──────────────────────────────────────
    @abstractmethod
    async def save_task(self, task: IngestTask) -> None: ...

    @abstractmethod
    async def get_task(self, task_id: str,
                       tenant_id: str = "") -> IngestTask | None: ...

    @abstractmethod
    async def list_tasks(self, tenant_id: str, collection: str | None = None,
                         status: IngestStatus | None = None,
                         limit: int = 20, offset: int = 0,
                         batch_id: str | None = None) -> list[IngestTask]: ...

    @abstractmethod
    async def find_by_md5(self, file_md5: str, tenant_id: str) -> IngestTask | None: ...

    @abstractmethod
    async def find_incomplete_tasks(self) -> list[IngestTask]: ...

    @abstractmethod
    async def save_batch(self, batch: IngestBatch) -> None: ...

    @abstractmethod
    async def get_batch(self, batch_id: str) -> IngestBatch | None: ...

    @abstractmethod
    async def list_batches(self, tenant_id: str, limit: int = 20,
                           offset: int = 0) -> list[IngestBatch]: ...

    @abstractmethod
    async def recompute_batch(self, batch_id: str) -> IngestBatch | None: ...

    # ── 反馈（V2.1）────────────────────────────────────────
    @abstractmethod
    async def save_feedback(self, fb: MessageFeedback) -> None: ...

    @abstractmethod
    async def list_negative_feedback(self, days: int = 30, limit: int = 1000) -> list[MessageFeedback]: ...

    # ── 用户画像 ───────────────────────────────────────────
    @abstractmethod
    async def upsert_profile(self, profile: UserProfile) -> None: ...

    @abstractmethod
    async def get_profile(self, user_id: str, tenant_id: str) -> UserProfile | None: ...

    # ── 实体表（EntityLinker 规则阶段）─────────────────────
    @abstractmethod
    async def list_entities(self, tenant_id: str, limit: int = 50000) -> list[dict]: ...

    @abstractmethod
    async def upsert_entities(self, tenant_id: str, entities: list[dict]) -> None: ...

    # ── 统计 ───────────────────────────────────────────────
    @abstractmethod
    async def collection_stats(self, tenant_id: str) -> list[dict]: ...

    @abstractmethod
    async def health_check(self) -> bool: ...


# ═══════════════════════════════════════════════════════════
# DocParser
# ═══════════════════════════════════════════════════════════

class DocParserAdapter(ABC):
    """
    文档解析适配器：文件 → 统一格式的 ParsedDocument。
    每种格式一个实现类，按扩展名路由。
    """

    @property
    @abstractmethod
    def supported_extensions(self) -> set[str]: ...

    @abstractmethod
    async def parse(self, file_path: str, doc_id: str, filename: str,
                    tenant_id: str = "", collection: str = "default") -> ParsedDocument: ...


# ═══════════════════════════════════════════════════════════
# BusinessData（NL2SQL）
# ═══════════════════════════════════════════════════════════

class BusinessDataAdapter(ABC):
    """业务数据库适配器：NL2SQL + 安全校验 + 执行 + 脱敏。"""

    @abstractmethod
    async def nl2sql(self, question: str, constraints: QueryConstraint | None = None,
                     plan_context: dict | None = None) -> str | None:
        """自然语言 → SQL（由 LLM 生成，schema 描述注入 prompt）。失败返回 None"""

    @abstractmethod
    def validate_sql(self, sql: str) -> bool:
        """白名单校验：仅 SELECT、仅允许配置声明的表"""

    @abstractmethod
    async def execute(self, sql: str) -> list[dict]:
        """执行并脱敏返回"""

    @abstractmethod
    def describe_schema(self) -> str:
        """Schema 文本描述（注入 NL2SQL prompt）"""

    async def health_check(self) -> bool:
        return True


# ═══════════════════════════════════════════════════════════
# KnowledgeGraph
# ═══════════════════════════════════════════════════════════

class KnowledgeGraphAdapter(ABC):
    """知识图谱适配器：实体/关系写入 + 多跳遍历 + 自然语言序列化。"""

    @abstractmethod
    async def ensure_schema(self) -> None: ...

    @abstractmethod
    async def upsert_entities(self, tenant_id: str, doc_id: str,
                              entities: list[dict]) -> None: ...

    @abstractmethod
    async def upsert_relations(self, tenant_id: str, doc_id: str,
                               relations: list[dict]) -> None: ...

    @abstractmethod
    async def traverse(self, tenant_id: str, entity: str, hops: int = 2,
                       limit: int = 50) -> str:
        """从实体出发多跳遍历，返回自然语言序列化的关系链路"""

    @abstractmethod
    async def delete_by_doc(self, tenant_id: str, doc_id: str) -> None: ...

    @abstractmethod
    async def health_check(self) -> bool: ...


# ═══════════════════════════════════════════════════════════
# Synonym
# ═══════════════════════════════════════════════════════════

class SynonymAdapter(ABC):
    """同义词/术语适配器：扩展 + 归一化，支持热重载。"""

    @abstractmethod
    def expand(self, term: str) -> list[str]:
        """术语 → 所有同义词（含自身）"""

    @abstractmethod
    def normalize(self, term: str) -> str:
        """口语 → 知识库标准术语（未命中返回原词）"""

    @abstractmethod
    def reload(self) -> int:
        """热重载，返回术语条数"""

    def all_groups(self) -> list[list[str]]:
        return []


# ═══════════════════════════════════════════════════════════
# Auth
# ═══════════════════════════════════════════════════════════

class AuthAdapter(ABC):
    """认证适配器：Token → UserContext。"""

    @abstractmethod
    async def verify(self, token: str) -> UserContext:
        """验证失败抛 AuthError"""

    @abstractmethod
    async def issue_token(self, user_id: str, roles: list[str],
                          tenant_id: str, extra: dict | None = None) -> str:
        """签发 token（dev/jwt 模式支持；OIDC 模式抛异常）"""


class AuthError(Exception):
    pass


# ═══════════════════════════════════════════════════════════
# Storage
# ═══════════════════════════════════════════════════════════

class StorageAdapter(ABC):
    """对象存储适配器：原始文件与多媒体内容。"""

    @abstractmethod
    async def put(self, key: str, content: bytes,
                  content_type: str = "application/octet-stream") -> str:
        """写入，返回 storage_url"""

    @abstractmethod
    async def get(self, url: str) -> bytes: ...

    @abstractmethod
    async def delete(self, url: str) -> None: ...

    @abstractmethod
    async def preview_url(self, url: str, ttl: int = 3600) -> str:
        """生成临时预览 URL"""

    async def health_check(self) -> bool:
        return True
