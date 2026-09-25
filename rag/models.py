"""
核心数据模型（rag/models.py）

定义入库 Pipeline 与查询 Pipeline 共享的所有结构化数据。
基于 Pydantic v2；所有模型可通过 model_dump(mode="json") 序列化。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def new_id(prefix: str = "") -> str:
    uid = uuid.uuid4().hex[:16]
    return f"{prefix}_{uid}" if prefix else uid


def make_chunk_id(tenant_id: str, doc_id: str, seq: int, content: str) -> str:
    """
    确定性 chunk_id：sha256(tenant_id:doc_id:seq:content_hash[:8])[:32]
    同一文档重新入库时，相同位置和内容的 Chunk 产生相同 id，
    ES / Milvus 的 upsert 直接覆盖旧数据（幂等写入）。
    """
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
    raw = f"{tenant_id}:{doc_id}:{seq}:{content_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def file_md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


# ═══════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════

# 子块**参与向量化的文本**的拼法版本（见 ingest_write._embed_text）。
# 放在这里而不是步骤模块里：入库侧（拼文本）与协调器（算库指纹）都要读它，而
# 协调器不该依赖流水线步骤模块。
# ⚠ 改动拼法必须同时改这个值：它会进入库指纹（coordinator._ingest_fingerprint），
# 否则同一集合里会出现"一部分文档用旧拼法、一部分用新拼法"，而秒传判据却认为
# 口径一致（两边向量不可比，检索质量下降又无从归因）。
EMBED_TEXT_VERSION = "v2"

# 块**正文与元数据生成逻辑**的版本（分块切分、表格块正文、页码归属、图区裁剪…）。
# ⚠ 只要改动会**改变入库内容**的流水线逻辑，就必须把它 +1：
#   它会进入库指纹（coordinator._ingest_fingerprint），决定"同一份文件重传时能不能
#   秒传"。不 bump 的后果是——用户重传同一文件，系统判"口径没变"直接秒传完成，
#   索引里留着的还是旧逻辑产出的块，而界面上显示的是"已完成"。
#   历史教训：v1 期间的改动（换行被吃掉导致英文粘连、表格块正文是 HTML、图区永不裁剪）
#   就是被秒传挡住的 —— 用户重传后以为已修复，其实一个块都没更新。
# 与 EMBED_TEXT_VERSION 的分工：那个只管"向量化文本怎么拼"，这个管"块本身长什么样"。
CHUNK_BUILD_VERSION = "v2"

class IngestStatus(str, Enum):
    PENDING = "pending"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    WRITING = "writing"
    DONE = "done"
    PARTIAL = "partial"           # 部分库写入失败，可降级使用，巡检后台修复
    FAILED = "failed"
    RETRYING = "retrying"
    SUPERSEDED = "superseded"     # 软删除：被新版本覆盖
    DELETED = "deleted"           # 回收站：可恢复，内容不参与检索


# "正在跑"的任务状态：文档行（documents.status）只会停在 pending 与 done/partial，
# 中间的 解析中/分块中/向量化/写入中 全都只在任务行上。任何"这篇文档现在能不能删"
# 的判断都必须看任务行，不能看文档行 —— 否则一篇正在写索引的文档可以被"删掉"，
# 而入库收尾（ingest_write 最后一步 upsert_document）转头把它写回 done，
# 用户看到的是"删了又自己回来了"。
ACTIVE_TASK_STATUSES = (
    IngestStatus.PENDING.value, IngestStatus.PARSING.value,
    IngestStatus.CHUNKING.value, IngestStatus.EMBEDDING.value,
    IngestStatus.WRITING.value, IngestStatus.RETRYING.value)


class IntentType(str, Enum):
    FACTUAL = "factual"           # 事实查询
    RELATIONAL = "relational"     # 关系推理
    AGGREGATION = "aggregation"   # 聚合统计
    PROCEDURAL = "procedural"     # 步骤流程
    COMPARATIVE = "comparative"   # 对比分析
    CHITCHAT = "chitchat"         # 闲聊


class ContentType(str, Enum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"
    CHART = "chart"
    CODE = "code"
    TITLE = "title"
    HEADER = "header"
    FOOTER = "footer"


class ChunkType(str, Enum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image_caption"
    CODE = "code"
    PARENT = "parent"


class FeedbackType(str, Enum):
    UP = "up"
    DOWN = "down"


class RetrievalPath(str, Enum):
    KW_EXACT = "kw_exact"
    VECTOR = "vector"
    BM25 = "bm25"
    GRAPH = "graph"
    STRUCTURED = "structured"
    EPHEMERAL = "ephemeral"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# ═══════════════════════════════════════════════════════════
# 入库侧：元数据与任务
# ═══════════════════════════════════════════════════════════

class DocumentMeta(BaseModel):
    doc_id: str
    tenant_id: str
    collection: str = "default"
    filename: str
    file_type: str                          # pdf/docx/xlsx/pptx/md/txt/html/img
    file_size: int | None = None
    file_md5: str | None = None
    storage_url: str | None = None
    language: str = "zh"
    page_count: int | None = None
    status: IngestStatus = IngestStatus.PENDING
    chunk_count: int = 0
    allowed_roles: list[str] = Field(default_factory=list)
    version: int = 1
    quality_report: dict | None = None
    created_by: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # ── 回收站（status=deleted）专用 ──────────────────────────
    # 为什么要有这两列：`status=deleted` 只说"在回收站里"，回答不了"恢复成什么"。
    # 原来恢复时无条件写 done —— 一篇 `partial`（部分库没写进去）或 `failed`
    # 的文档一删一恢复就变成"已完成"，用户按状态筛出来的质量视图直接失真。
    # deleted_at 则是回收站列表的排序与展示依据（列表按"删得最近的在上"）。
    deleted_at: datetime | None = None
    prev_status: str | None = None


class ChunkMeta(BaseModel):
    chunk_id: str
    doc_id: str
    tenant_id: str
    collection: str
    chunk_type: str = "text"
    is_parent: bool = False
    parent_chunk_id: str | None = None
    page_num: int | None = None
    section_path: str | None = None         # "第3章/3.1节/3.1.2"
    quality_score: float = 1.0
    token_count: int = 0
    allowed_roles: list[str] = Field(default_factory=list)
    figure_label: str | None = None         # 图/表标题编号，如"图28"
    figure_caption: str | None = None       # 图/表题注文本
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TableData(BaseModel):
    """Excel/表格原始行列结构（入 MySQL table_data，供精确数值查询）"""
    chunk_id: str
    doc_id: str
    tenant_id: str
    table_index: int = 0
    page_num: int | None = None
    headers: list[str] = Field(default_factory=list)
    row_data: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    numeric_stats: dict = Field(default_factory=dict)   # {"列名": {"min":..,"max":..,"avg":..}}


class WriteCheckpoint(BaseModel):
    """两阶段写入检查点：每库写入完成后打钩，断点续写依据"""
    minio: bool = False
    mysql: bool = False
    es: bool = False
    milvus: bool = False
    graph: bool = False

    def first_incomplete(self, order: list[str] | None = None) -> str | None:
        order = order or ["minio", "mysql", "es", "milvus", "graph"]
        for name in order:
            if not getattr(self, name):
                return name
        return None

    @property
    def all_done(self) -> bool:
        return all([self.minio, self.mysql, self.es, self.milvus, self.graph])


class IngestTask(BaseModel):
    task_id: str = Field(default_factory=lambda: new_id("task"))
    doc_id: str | None = None
    batch_id: str | None = None
    tenant_id: str
    collection: str = "default"
    filename: str
    file_md5: str | None = None
    file_type: str | None = None
    status: IngestStatus = IngestStatus.PENDING
    retry_count: int = 0
    max_retries: int = 3
    error_message: str | None = None
    checkpoint: WriteCheckpoint = Field(default_factory=WriteCheckpoint)
    total_pages: int | None = None
    processed_pages: int = 0
    total_chunks: int = 0
    written_chunks: int = 0
    quality_summary: dict = Field(default_factory=dict)
    source_type: str = "upload"             # upload / server_path / local_dir / cli / ephemeral_promote / reingest
    submitted_by: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def progress(self) -> float:
        """0-1 的总体进度估算（按阶段权重 + 页/块完成度）

        前端任务视图一直在读 `t.progress`，而它原先是个不存在的字段 → 恒为 None、
        进度条永远空着。权重与 task_progress(stage_detail) 上报的百分比一致，
        两处口径相同才不会出现"SSE 推到 60%、列表却显示 0%"。
        """
        base = {
            IngestStatus.PENDING: 0.0,
            IngestStatus.PARSING: 0.05,
            IngestStatus.CHUNKING: 0.35,
            IngestStatus.EMBEDDING: 0.6,
            IngestStatus.WRITING: 0.85,
            IngestStatus.DONE: 1.0,
            IngestStatus.PARTIAL: 1.0,
            IngestStatus.FAILED: 1.0,
            IngestStatus.RETRYING: 0.0,
            IngestStatus.SUPERSEDED: 1.0,
            IngestStatus.DELETED: 1.0,
        }.get(self.status, 0.0)
        if self.status == IngestStatus.PARSING and self.total_pages:
            return min(1.0, base + 0.25 * self.processed_pages / self.total_pages)
        if self.status == IngestStatus.WRITING and self.total_chunks:
            return min(1.0, base + 0.1 * self.written_chunks / self.total_chunks)
        return min(1.0, base)


class IngestBatch(BaseModel):
    """批次：所有入库方式统一创建（单文件 total=1）"""
    batch_id: str = Field(default_factory=lambda: new_id("batch"))
    tenant_id: str
    collection: str = "default"
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    pending: int = 0
    status: str = "pending"                 # pending/running/done/partial_failed
    source_type: str = "upload"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TaskProgressEvent(BaseModel):
    """SSE 进度事件"""
    task_id: str
    batch_id: str | None = None
    doc_id: str | None = None
    filename: str = ""
    status: IngestStatus
    progress: float = 0.0                   # 0-1
    stage_detail: str = ""
    error: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ═══════════════════════════════════════════════════════════
# 解析侧：结构化文档
# ═══════════════════════════════════════════════════════════

class ParsedElement(BaseModel):
    """解析后的最小文档元素（段落/表格/图片/代码块）"""
    element_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    content_type: ContentType = ContentType.TEXT
    text: str = ""
    raw_data: dict = Field(default_factory=dict)   # 图片: image_path/figure_label/figure_caption/bbox...
    page_num: int | None = None
    bbox: list[float] | None = None                # [x0, y0, x1, y1]
    metadata: dict = Field(default_factory=dict)   # heading_level / is_slide_title / sheet_name / ocr_confidence...


class ParsedDocument(BaseModel):
    doc_id: str
    filename: str
    file_type: str
    tenant_id: str = ""
    collection: str = "default"
    elements: list[ParsedElement] = Field(default_factory=list)
    toc: list[dict] = Field(default_factory=list)          # PDF 内置书签
    page_count: int | None = None
    language: str = "zh"
    scan_type: str = "text"                                # text / scanned / hybrid
    metadata: dict = Field(default_factory=dict)


class QualityIssue(BaseModel):
    stage: str                              # parse / chunk / post_write
    severity: str                           # info / low / medium / high
    code: str                               # char_density_low / ocr_low_conf / ...
    message: str
    page_num: int | None = None
    action: str = ""                        # auto_ocr / warn / partial...


class QualityReport(BaseModel):
    doc_id: str
    issues: list[QualityIssue] = Field(default_factory=list)
    high_quality: int = 0
    medium_quality: int = 0
    low_quality: int = 0
    ocr_avg_confidence: float | None = None
    char_density: float | None = None
    blank_page_ratio: float | None = None
    document_score: float = 1.0             # 文档级质量系数（ChunkStep降权）
    garbled_ratio: float | None = None
    # 这份文档实际走了哪条解析路径（text / scanned / mixed）与用了几页 OCR。
    # 判据是「页面有没有文本层」，不是字符密度 —— 见 doc_parser.PDFParser._parse_sync。
    doc_type: str = ""
    used_ocr_pages: int = 0
    summary: str = ""


# ═══════════════════════════════════════════════════════════
# 查询侧：QueryPlan 五层次
# ═══════════════════════════════════════════════════════════

class QueryConstraint(BaseModel):
    """从问题中提取的硬约束（下推到所有路的 filter，不参与相关性计算）"""
    date_from: datetime | None = None
    date_to: datetime | None = None
    file_types: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    section_keyword: str | None = None


class QueryEntity(BaseModel):
    """识别出的实体（含归一化结果）"""
    original: str
    canonical: str | None = None            # EntityLinker 归一化后的规范名
    entity_type: str = "generic"            # product/person/org/model/...
    linked: bool = False


class MetadataFilter(BaseModel):
    """元数据前置过滤条件（显式 UI 设置 + 隐式意图推断合并）"""
    collections: list[str] | None = None
    file_types: list[str] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    allowed_roles: list[str] | None = None
    # V2.1：隐式意图推断的额外提示（不直接过滤，影响排序和路由）
    _extra_hints: dict | None = None        # {"order_by","ephemeral_only","section_keyword"}

    def merged_hints(self) -> dict:
        return self._extra_hints or {}


class ImplicitMetaIntent(BaseModel):
    """从用户问题语义中自动推断的元数据过滤条件（V2.1）"""
    date_from: datetime | None = None
    date_to: datetime | None = None
    file_types: list[str] = Field(default_factory=list)
    prefer_latest: bool = False             # "最新版本" 语义 → 按时间降序
    ephemeral_only: bool = False            # "我上传的" 语义 → 只查临时文档
    section_keyword: str | None = None      # "第三章" 语义 → section_path 过滤
    confidence: float = 0.0                 # 推断置信度 0-1


class QueryPlan(BaseModel):
    """
    查询理解的产物：把问题分解为五个互不干扰的层次，
    分别投放到对应检索路。
    """
    original_query: str
    standalone_query: str = ""              # 指代消解后的独立问题
    semantic_query: str = ""                # 剥离约束+实体的纯语义核心
    intent: IntentType = IntentType.FACTUAL
    confidence: float = 0.0
    entities: list[QueryEntity] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    synonyms: dict[str, list[str]] = Field(default_factory=dict)
    constraints: QueryConstraint = Field(default_factory=QueryConstraint)
    rewrites: list[str] = Field(default_factory=list)       # 多路改写
    sub_queries: list[str] = Field(default_factory=list)    # 复合问题分解
    route_weights: dict[str, float] = Field(default_factory=dict)  # 软路由权重 0-1
    metadata_filter: MetadataFilter = Field(default_factory=MetadataFilter)
    topic_switched: bool = False
    is_followup: bool = False
    session_id: str | None = None
    ephemeral_session: str | None = None

    def active_paths(self, enabled: set[str]) -> list[str]:
        """最终激活路 = 系统启用路 ∩ 权重>0 的路"""
        return [p for p, w in self.route_weights.items()
                if w > 0 and p in enabled]


# ═══════════════════════════════════════════════════════════
# 检索侧
# ═══════════════════════════════════════════════════════════

class RetrievedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    text: str
    score: float = 0.0
    source_path: RetrievalPath | None = None
    rank: int | None = None
    title: str | None = None
    section_path: str | None = None
    page_num: int | None = None
    figure_label: str | None = None
    figure_caption: str | None = None
    chunk_type: str = "text"
    collection: str | None = None
    is_ephemeral: bool = False
    storage_url: str | None = None
    quality_score: float = 1.0
    parent_content: str | None = None       # 命中子块时回取的父块正文
    preview_url: str | None = None          # 原文预览URL（presigned）
    metadata: dict = Field(default_factory=dict)


class SourceReference(BaseModel):
    """答案来源溯源（V2.1 扩展版）"""
    ref_id: str
    chunk_id: str
    doc_id: str
    title: str | None = None
    section: str | None = None              # section_path 的最后一级
    section_path: str | None = None         # 完整路径 "第3章/3.1节/3.1.2"
    page_num: int | None = None
    figure_label: str | None = None         # V2.1："图28"
    figure_caption: str | None = None       # V2.1："系统整体架构示意图"
    storage_url: str | None = None
    preview_url: str | None = None          # 原文预览 URL（presigned）
    chunk_type: str = "text"                # text/table/image_caption/code

    @property
    def display_location(self) -> str:
        """UI 展示位置串。优先级：figure_label > figure_caption > section > page_num"""
        parts: list[str] = []
        if self.figure_label:
            parts.append(self.figure_label)
        elif self.figure_caption:
            parts.append(f"图：{self.figure_caption[:20]}")
        if self.section_path:
            parts.append(self.section_path.split("/")[-1])
        if self.page_num:
            parts.append(f"第{self.page_num}页")
        return " · ".join(parts) if parts else ""


# ═══════════════════════════════════════════════════════════
# 会话与记忆
# ═══════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: new_id("msg"))
    session_id: str
    role: MessageRole
    content: str
    sources: list[SourceReference] = Field(default_factory=list)
    retrieval_paths: dict = Field(default_factory=dict)
    feedback: FeedbackType | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EntitySlot(BaseModel):
    """工作记忆中的实体槽位"""
    name: str
    value: str
    slot_type: str = "entity"               # entity / constraint / pending_confirm
    confirmed: bool = False
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class WorkingMemory(BaseModel):
    """工作记忆：对话摘要 + 实体槽位 + 话题链路（Redis）"""
    summary: str = ""
    entity_slots: list[EntitySlot] = Field(default_factory=list)
    topic_chain: list[str] = Field(default_factory=list)
    topic_vector: list[float] | None = None
    turn_count: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SessionState(BaseModel):
    """Session 状态（Redis）"""
    session_id: str = Field(default_factory=lambda: new_id("sess"))
    user_id: str = ""
    tenant_id: str = ""
    title: str = "新对话"
    short_term: list[ChatMessage] = Field(default_factory=list)   # 最近 3-10 轮
    working: WorkingMemory = Field(default_factory=WorkingMemory)
    ephemeral_doc_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    archived: bool = False


class UserProfile(BaseModel):
    """长期记忆：用户画像（关系 DB）"""
    user_id: str
    tenant_id: str
    professional_background: str = ""
    preferred_format: str = ""
    common_products: list[str] = Field(default_factory=list)
    corrections: list[str] = Field(default_factory=list)
    # 归档会话摘要空间：[{session_id, summary, vector, archived_at}]
    custom: dict = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ═══════════════════════════════════════════════════════════
# 用户反馈（V2.1）
# ═══════════════════════════════════════════════════════════

class MessageFeedback(BaseModel):
    """用户对 AI 回答的反馈（存 MySQL feedback 表）"""
    feedback_id: str = Field(default_factory=lambda: new_id("fb"))
    session_id: str
    message_id: str
    tenant_id: str
    user_id: str
    feedback: FeedbackType
    query: str = ""
    answer: str = ""
    sources: list[dict] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)   # wrong_fact/incomplete/wrong_source/off_topic/other
    comment: str = ""
    retrieval_paths: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ═══════════════════════════════════════════════════════════
# 用户上下文（认证中间件产物）
# ═══════════════════════════════════════════════════════════

class UserContext(BaseModel):
    user_id: str
    username: str = ""
    roles: list[str] = Field(default_factory=list)
    tenant_id: str = "default"
    email: str | None = None
    is_admin: bool = False
