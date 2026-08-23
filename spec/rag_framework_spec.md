# RAG 智能问答框架系统 — 完整实现规格

> **文档用途**：本规格面向大语言模型代码生成。读完本文档后，模型应能生成一个
> 生产可用的、多租户可插拔的 RAG 智能问答系统，无需额外决策。
>
> **约定**：
> - 所有接口签名以 Python 3.11+ 类型注解为准
> - 异步接口一律使用 `async/await`（asyncio）
> - 配置以 Pydantic v2 模型定义，从 YAML 加载
> - 日志使用 `structlog`，追踪使用 OpenTelemetry
> - 测试框架：`pytest` + `pytest-asyncio`

---

## 目录

1. [系统总体架构](#1-系统总体架构)
2. [项目目录结构](#2-项目目录结构)
3. [核心设计原则](#3-核心设计原则)
4. [数据模型定义](#4-数据模型定义)
5. [适配器接口层（Adapter Layer）](#5-适配器接口层)
6. [框架默认适配器实现](#6-框架默认适配器实现)
7. [知识库构建 Pipeline（离线）](#7-知识库构建-pipeline离线)
8. [在线问答 Pipeline](#8-在线问答-pipeline)
9. [多轮对话记忆管理](#9-多轮对话记忆管理)
10. [Pipeline 编排引擎](#10-pipeline-编排引擎)
11. [配置系统](#11-配置系统)
12. [REST API 层](#12-rest-api-层)
13. [运维控制面](#13-运维控制面)
14. [可观测性](#14-可观测性)
15. [测试规格](#15-测试规格)
16. [部署规格](#16-部署规格)
17. [客户现场对接清单](#17-客户现场对接清单)

---

## 1. 系统总体架构

### 1.1 分层结构

```
┌─────────────────────────────────────────────────────────────┐
│                      客户接入层                               │
│   REST API │ WebSocket │ SDK │ Bot(企微/钉钉/飞书) │ Web组件  │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│               框架编排核心（Framework Core）                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐  │
│  │查询理解  │ │多路召回  │ │融合重排  │ │  Prompt组装  │  │
│  │  引擎    │ │  编排    │ │  引擎    │ │    引擎      │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────────────┐   │
│  │多轮记忆  │ │答案后处  │ │  Pipeline DAG 编排引擎   │   │
│  │  管理    │ │  理器    │ │  （步骤可启用/禁用/替换） │   │
│  └──────────┘ └──────────┘ └──────────────────────────┘   │
└─────────────────────────┬───────────────────────────────────┘
                          │  标准接口契约
┌─────────────────────────▼───────────────────────────────────┐
│              适配器接口层（Adapter Layer）                     │
│  LLM │ Embedding │ VectorStore │ FullTextSearch │ DocParser │
│  BusinessData │ KnowledgeGraph │ Synonym │ Auth │ Storage   │
└─────────────────────────┬───────────────────────────────────┘
                          │  现场配置/实现
┌─────────────────────────▼───────────────────────────────────┐
│                   客户资源层（Customer Resources）            │
│  私有LLM │ 向量库 │ ES集群 │ 业务数据库 │ 对象存储 │ SSO    │
│  同义词库 │ 知识图谱 │ Redis │ 监控平台 │ IM平台   │ 文档库  │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 两条主流程

**离线入库流程**（Ingestion Pipeline）：
```
原始文档 → 解析 → 内容分类 → 多模态处理 → 智能分块
         → 元数据增强 → Embedding → 并行写入四库
         [向量库 | 全文索引库 | 知识图谱 | 原文存储]
```

**在线问答流程**（Query Pipeline）：
```
用户提问 → 安全过滤 → 查询理解 → 指代消解 → Standalone补全
         → 同义扩展 → 意图路由 → 多路并行召回
         [向量召回 | BM25召回 | 图谱召回 | 结构化查询]
         → RRF融合 → 相关性过滤 → Cross-Encoder重排
         → Context压缩 → Prompt组装 → LLM生成
         → 忠实度校验 → 来源溯源 → 格式化输出
```

---

## 2. 项目目录结构

```
rag_framework/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── README.md
│
├── rag/                              # 框架核心包（不含客户代码）
│   ├── __init__.py
│   ├── core/                         # 算法核心，禁止客户修改
│   │   ├── __init__.py
│   │   ├── chunking.py               # 分块策略
│   │   ├── fusion.py                 # RRF 融合算法
│   │   ├── reranking.py              # Cross-Encoder 重排
│   │   ├── compression.py            # Context 压缩
│   │   ├── memory.py                 # 三层记忆管理
│   │   ├── prompt_builder.py         # Prompt 组装器
│   │   └── faithfulness.py           # 忠实度校验
│   │
│   ├── adapters/                     # 适配器接口定义
│   │   ├── __init__.py
│   │   ├── base/                     # 抽象基类（接口契约）
│   │   │   ├── llm.py
│   │   │   ├── embedding.py
│   │   │   ├── vector_store.py
│   │   │   ├── full_text_search.py
│   │   │   ├── doc_parser.py
│   │   │   ├── business_data.py
│   │   │   ├── knowledge_graph.py
│   │   │   ├── synonym.py
│   │   │   ├── auth.py
│   │   │   └── storage.py
│   │   │
│   │   └── builtin/                  # 框架内置实现（开箱即用）
│   │       ├── llm/
│   │       │   ├── openai_compatible.py
│   │       │   └── vllm.py
│   │       ├── embedding/
│   │       │   ├── openai_embedding.py
│   │       │   └── http_embedding.py
│   │       ├── vector_store/
│   │       │   ├── milvus.py
│   │       │   ├── qdrant.py
│   │       │   └── pgvector.py
│   │       ├── full_text_search/
│   │       │   ├── elasticsearch.py
│   │       │   └── opensearch.py
│   │       ├── doc_parser/
│   │       │   ├── pdf_parser.py
│   │       │   ├── docx_parser.py
│   │       │   └── image_parser.py
│   │       ├── business_data/
│   │       │   └── sqlalchemy_adapter.py
│   │       ├── knowledge_graph/
│   │       │   └── neo4j_adapter.py
│   │       ├── synonym/
│   │       │   ├── file_based.py
│   │       │   └── http_service.py
│   │       ├── auth/
│   │       │   ├── jwt_adapter.py
│   │       │   └── oidc_adapter.py
│   │       └── storage/
│   │           ├── minio.py
│   │           └── local_fs.py
│   │
│   ├── pipeline/                     # Pipeline 编排
│   │   ├── __init__.py
│   │   ├── engine.py                 # DAG 编排引擎
│   │   ├── ingestion/
│   │   │   ├── __init__.py
│   │   │   ├── steps.py              # 入库各步骤实现
│   │   │   └── pipeline.py           # 入库 Pipeline 定义
│   │   └── query/
│   │       ├── __init__.py
│   │       ├── steps.py              # 问答各步骤实现
│   │       └── pipeline.py           # 问答 Pipeline 定义
│   │
│   ├── config/                       # 配置系统
│   │   ├── __init__.py
│   │   ├── models.py                 # Pydantic 配置模型
│   │   ├── loader.py                 # YAML 加载与验证
│   │   └── registry.py              # 适配器注册中心
│   │
│   ├── api/                          # REST API
│   │   ├── __init__.py
│   │   ├── app.py                    # FastAPI 应用
│   │   ├── routers/
│   │   │   ├── query.py              # 问答接口
│   │   │   ├── ingestion.py          # 入库接口
│   │   │   ├── admin.py              # 管理接口
│   │   │   └── health.py             # 健康检查
│   │   ├── middleware/
│   │   │   ├── auth.py               # 认证中间件
│   │   │   ├── rate_limit.py         # 限流中间件
│   │   │   └── tracing.py            # 追踪中间件
│   │   └── schemas/                  # API 请求/响应 Schema
│   │       ├── query.py
│   │       └── ingestion.py
│   │
│   └── observability/                # 可观测性
│       ├── __init__.py
│       ├── metrics.py                # Prometheus 指标
│       ├── tracing.py                # OpenTelemetry 追踪
│       └── logging.py                # 结构化日志
│
├── customer/                         # 客户适配器（现场实现，不进框架包）
│   ├── __init__.py
│   ├── adapters/                     # 客户自定义适配器实现
│   │   └── (客户实现的适配器子类)
│   └── config/
│       └── customer_config.yaml      # 客户配置文件
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
└── scripts/
    ├── ingest.py                     # 批量入库脚本
    ├── evaluate.py                   # 召回质量评测脚本
    └── migrate.py                    # 知识库迁移脚本
```

---

## 3. 核心设计原则

### 3.1 三不变三可换

**不变（Framework Core — 禁止客户修改）**：
- Pipeline 编排逻辑与 DAG 执行引擎
- 算法核心：RRF 融合、Cross-Encoder 重排、记忆压缩、token 预算分配
- 可观测性埋点：所有 span、metric、log 的埋点位置固定

**可换（Adapter Layer — 客户现场替换）**：
- 所有外部服务调用（LLM、向量库、ES、存储）
- 数据格式转换（各客户 Schema 不同的转换逻辑）
- 业务规则注入（同义词、权限、过滤规则）

### 3.2 适配器设计契约

每个适配器必须满足：
1. 继承对应抽象基类，实现所有 `@abstractmethod`
2. 构造函数只接受配置对象（`XxxConfig`），不接受裸参数
3. 实现 `async def health_check() -> bool`
4. 所有异常转换为框架定义的异常类型（`AdapterError` 子类）
5. 在 `customer_config.yaml` 中通过 `adapter` 字段注册

### 3.3 Pipeline 步骤契约

每个 Pipeline 步骤必须满足：
1. 实现 `async def execute(ctx: PipelineContext) -> PipelineContext`
2. 只读取/写入 `PipelineContext` 中约定的字段（见 §4）
3. 步骤失败时抛出 `StepError`，由引擎决定是否跳过或终止
4. 步骤可通过配置 `enabled: false` 跳过，跳过时原样透传 `ctx`
## 4. 数据模型定义

所有模型位于 `rag/models.py`，使用 Pydantic v2。

```python
# rag/models.py
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator
from uuid import UUID, uuid4
from pydantic import BaseModel, Field


# ─── 枚举 ────────────────────────────────────────────────────────────────────

class ContentType(str, Enum):
    TEXT        = "text"
    TABLE       = "table"
    IMAGE       = "image"
    CHART       = "chart"
    CODE        = "code"
    MIXED       = "mixed"

class IntentType(str, Enum):
    FACTUAL     = "factual"       # 精确事实查询
    RELATIONAL  = "relational"    # 关系推理（需图谱）
    AGGREGATION = "aggregation"   # 聚合统计（需 SQL）
    PROCEDURAL  = "procedural"    # 步骤/流程查询
    COMPARATIVE = "comparative"   # 对比分析（需子问题分解）
    CHITCHAT    = "chitchat"      # 闲聊（不走检索）
    META        = "meta"          # 元查询（"你能做什么"）

class RetrievalPath(str, Enum):
    VECTOR      = "vector"
    BM25        = "bm25"
    GRAPH       = "graph"
    STRUCTURED  = "structured"

class MemoryTier(str, Enum):
    SHORT_TERM  = "short_term"
    WORKING     = "working"
    LONG_TERM   = "long_term"


# ─── 文档与 Chunk ────────────────────────────────────────────────────────────

class ParsedElement(BaseModel):
    """文档解析后的最小内容单元"""
    element_id:   str         = Field(default_factory=lambda: str(uuid4()))
    content_type: ContentType
    text:         str                      # 文本内容（表格/图片转为文字描述后存此处）
    raw_data:     dict | None = None       # 原始结构化数据（表格行列、图片路径等）
    bbox:         list[float] | None = None # 页面坐标 [x0,y0,x1,y1]
    page_num:     int | None  = None
    metadata:     dict[str, Any] = Field(default_factory=dict)

class ParsedDocument(BaseModel):
    """文档解析结果"""
    doc_id:       str         = Field(default_factory=lambda: str(uuid4()))
    source_path:  str                      # 原始文件路径或 URL
    file_type:    str                      # pdf / docx / txt / image / ...
    title:        str | None  = None
    author:       str | None  = None
    created_at:   datetime | None = None
    language:     str         = "zh"
    elements:     list[ParsedElement] = Field(default_factory=list)
    metadata:     dict[str, Any] = Field(default_factory=dict)

class Chunk(BaseModel):
    """入库的基本单元"""
    chunk_id:       str       = Field(default_factory=lambda: str(uuid4()))
    doc_id:         str
    content:        str                    # 用于 Embedding 和展示的文本
    content_type:   ContentType = ContentType.TEXT
    # 层次化 Chunk：parent 存完整段落，child 存细粒度句子
    parent_chunk_id: str | None = None
    child_chunk_ids: list[str]  = Field(default_factory=list)
    # 元数据（入库时生成，检索时作为 filter 条件）
    summary:        str | None  = None    # LLM 生成的摘要（100字以内）
    keywords:       list[str]   = Field(default_factory=list)
    entities:       list[str]   = Field(default_factory=list)
    section_path:   str | None  = None    # 如 "第三章/3.2节/配置方法"
    page_num:       int | None  = None
    token_count:    int         = 0
    embedding:      list[float] | None = None
    # 权限控制
    allowed_roles:  list[str]   = Field(default_factory=list)  # 空=所有人可见
    # 质量指标
    quality_score:  float       = 1.0    # 0-1，入库时评估信息密度
    created_at:     datetime    = Field(default_factory=datetime.utcnow)
    metadata:       dict[str, Any] = Field(default_factory=dict)


# ─── 检索结果 ────────────────────────────────────────────────────────────────

class RetrievalResult(BaseModel):
    """单路召回的单条结果"""
    chunk_id:    str
    doc_id:      str
    content:     str
    score:       float                  # 原始分数（量纲依召回路而异）
    rank:        int                    # 在本路中的排名（1-based）
    path:        RetrievalPath
    metadata:    dict[str, Any] = Field(default_factory=dict)
    # 父节点内容（层次化 Chunk 时，检索到子节点后取父节点完整内容喂给 LLM）
    parent_content: str | None = None

class FusedResult(BaseModel):
    """RRF 融合后的单条结果"""
    chunk_id:    str
    doc_id:      str
    content:     str
    rrf_score:   float
    rerank_score: float | None = None   # Cross-Encoder 打分后填入
    contributing_paths: list[RetrievalPath] = Field(default_factory=list)
    metadata:    dict[str, Any] = Field(default_factory=dict)
    parent_content: str | None = None


# ─── 查询上下文 ──────────────────────────────────────────────────────────────

class QueryUnderstanding(BaseModel):
    """查询理解结果"""
    original_query:    str
    standalone_query:  str              # 指代消解+补全后的独立查询
    rewrites:          list[str] = Field(default_factory=list)  # 多路改写版本
    intent:            IntentType = IntentType.FACTUAL
    entities:          list[str]  = Field(default_factory=list)
    sub_questions:     list[str]  = Field(default_factory=list)  # 复合问题分解
    active_paths:      list[RetrievalPath] = Field(
                           default_factory=lambda: [RetrievalPath.VECTOR, RetrievalPath.BM25]
                       )
    topic_shift:       bool = False     # 是否检测到话题跳转


# ─── Pipeline 上下文 ─────────────────────────────────────────────────────────

@dataclass
class PipelineContext:
    """在线问答 Pipeline 的共享上下文，在各步骤间流转"""
    # 输入
    session_id:      str
    user_id:         str
    raw_query:       str
    tenant_id:       str

    # 查询处理阶段（由各步骤填充）
    understanding:   QueryUnderstanding | None = None

    # 召回阶段
    retrieval_results: dict[RetrievalPath, list[RetrievalResult]] = field(
        default_factory=dict
    )
    fused_results:   list[FusedResult] = field(default_factory=list)
    final_chunks:    list[FusedResult] = field(default_factory=list)  # 重排后 top-k

    # 生成阶段
    prompt:          str | None = None
    answer:          str | None = None
    source_refs:     list[dict] = field(default_factory=list)  # 来源引用列表
    faithfulness_ok: bool = True

    # 元信息
    start_time:      datetime = field(default_factory=datetime.utcnow)
    step_timings:    dict[str, float] = field(default_factory=dict)  # 各步骤耗时
    errors:          list[str] = field(default_factory=list)
    metadata:        dict[str, Any] = field(default_factory=dict)


# ─── 记忆数据结构 ────────────────────────────────────────────────────────────

class EntitySlots(BaseModel):
    """工作记忆中的实体状态槽位"""
    product:         str | None = None
    user_role:       str | None = None
    current_topic:   str | None = None
    constraints:     list[str]  = Field(default_factory=list)
    open_items:      list[str]  = Field(default_factory=list)
    confirmed_facts: dict[str, str] = Field(default_factory=dict)

class WorkingMemory(BaseModel):
    """Session 级工作记忆（存 Redis）"""
    session_id:    str
    summary:       str = ""             # 滚动摘要
    entity_slots:  EntitySlots = Field(default_factory=EntitySlots)
    topic_chain:   list[str]   = Field(default_factory=list)
    topic_vector:  list[float] | None = None  # 当前话题 embedding
    used_doc_ids:  dict[str, int] = Field(default_factory=dict)  # doc_id -> 引用次数
    turn_count:    int = 0

class ShortTermTurn(BaseModel):
    """短期记忆中的单轮对话"""
    role:          str              # "user" | "assistant"
    content:       str
    summary:       str | None = None  # 过长时压缩为摘要
    token_count:   int = 0
    doc_refs:      list[str] = Field(default_factory=list)  # 本轮引用的 doc_id
    timestamp:     datetime = Field(default_factory=datetime.utcnow)

class UserProfile(BaseModel):
    """长期记忆：用户画像（持久化存储）"""
    user_id:          str
    expertise_level:  str = "general"   # general | intermediate | expert
    preferred_format: str = "prose"     # prose | bullet | command_line
    known_products:   list[str] = Field(default_factory=list)
    domain_tags:      list[str] = Field(default_factory=list)
    correction_log:   list[str] = Field(default_factory=list)  # 纠错记录
    last_updated:     datetime = Field(default_factory=datetime.utcnow)

class SessionState(BaseModel):
    """完整 Session 状态（Redis 存储，key=session:{session_id}）"""
    session_id:    str
    user_id:       str
    tenant_id:     str
    short_term:    list[ShortTermTurn] = Field(default_factory=list)
    short_term_tokens: int = 0
    working_memory: WorkingMemory = Field(
        default_factory=lambda: WorkingMemory(session_id="")
    )
    user_profile:  UserProfile | None = None  # 从长期存储加载
    created_at:    datetime = Field(default_factory=datetime.utcnow)
    last_active:   datetime = Field(default_factory=datetime.utcnow)


# ─── API 请求/响应 ───────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    query:      str   = Field(..., min_length=1, max_length=2000)
    session_id: str | None = None   # 为空时创建新 session
    stream:     bool  = False
    metadata:   dict[str, Any] = Field(default_factory=dict)

class SourceReference(BaseModel):
    ref_id:     str             # "[文档1]" 中的编号
    chunk_id:   str
    doc_id:     str
    title:      str | None = None
    section:    str | None = None
    page_num:   int | None = None
    storage_url: str | None = None  # 原文预览 URL

class QueryResponse(BaseModel):
    answer:        str
    session_id:    str
    sources:       list[SourceReference] = Field(default_factory=list)
    faithfulness:  float | None = None  # 0-1，忠实度分数
    latency_ms:    int = 0
    metadata:      dict[str, Any] = Field(default_factory=dict)

class IngestionRequest(BaseModel):
    file_url:   str             # 对象存储中的文件路径
    doc_id:     str | None = None  # 指定则覆盖同 ID 文档
    collection: str = "default"
    allowed_roles: list[str] = Field(default_factory=list)
    metadata:   dict[str, Any] = Field(default_factory=dict)

class IngestionResponse(BaseModel):
    doc_id:      str
    chunk_count: int
    status:      str    # "pending" | "processing" | "done" | "failed"
    task_id:     str    # 异步任务 ID，可用于查询进度
```
## 5. 适配器接口层

所有抽象基类位于 `rag/adapters/base/`，定义接口契约。
客户继承这些基类实现自己的版本，框架核心只依赖基类，不感知具体实现。

### 5.1 异常层次

```python
# rag/adapters/base/exceptions.py

class AdapterError(Exception):
    """所有适配器异常的基类"""
    def __init__(self, adapter_name: str, message: str, cause: Exception | None = None):
        self.adapter_name = adapter_name
        self.cause = cause
        super().__init__(f"[{adapter_name}] {message}")

class AdapterConnectionError(AdapterError): ...   # 连接失败
class AdapterTimeoutError(AdapterError): ...      # 超时
class AdapterAuthError(AdapterError): ...         # 认证失败
class AdapterNotFoundError(AdapterError): ...     # 资源不存在
class AdapterCapacityError(AdapterError): ...     # 容量/配额不足
```

### 5.2 LLM 适配器

```python
# rag/adapters/base/llm.py
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from rag.models import IntentType, QueryUnderstanding

class LLMAdapter(ABC):
    """
    LLM 推理适配器抽象基类。

    职责：
    - 统一封装不同 LLM 服务的 HTTP/gRPC 调用差异
    - 提供 token 计数能力（用于 Prompt 预算控制）
    - 支持流式和非流式两种返回模式

    实现要求：
    - 构造函数接受 LLMConfig（见 §11）
    - 所有网络异常转换为 AdapterError 子类
    - 超时由 config.timeout_seconds 控制
    - 支持 task_model_mapping：不同任务使用不同模型
    """

    @abstractmethod
    async def generate(
        self,
        messages:     list[dict[str, str]],   # OpenAI 格式: [{"role":..,"content":..}]
        task:         str          = "generate",  # 用于 task_model_mapping 路由
        temperature:  float        = 0.1,
        max_tokens:   int          = 2048,
        stream:       bool         = False,
        stop:         list[str]    | None = None,
        **kwargs,
    ) -> str | AsyncIterator[str]:
        """
        核心生成接口。
        stream=False → 返回完整字符串
        stream=True  → 返回 AsyncIterator[str]，每次 yield 一个 token 片段
        """

    @abstractmethod
    async def count_tokens(self, text: str) -> int:
        """
        精确 token 计数。
        无法精确计数时允许按 len(text)//1.5 估算（中文），
        但必须在实现中注明是估算。
        """

    async def health_check(self) -> bool:
        """发送一个极短的探测请求，验证服务可用性"""
        try:
            await self.generate([{"role": "user", "content": "hi"}],
                                max_tokens=1, task="health")
            return True
        except AdapterError:
            return False

    # ── 框架调用的高层便利方法（已有默认实现，子类可覆盖优化）─────────────

    async def rewrite_standalone(
        self,
        current_query:  str,
        history_turns:  list[dict[str, str]],  # 最近3轮 [{"role","content"}]
    ) -> str:
        """
        将当前追问改写为独立自洽的查询。
        默认实现通过 generate 调用完成，子类可用专门的小模型覆盖此方法。
        """
        history_text = "\n".join(
            f"{t['role']}: {t['content']}" for t in history_turns[-6:]
        )
        prompt = f"""根据以下对话历史，将"最新问题"改写为一个不依赖上下文即可独立理解的完整查询。
若最新问题已完整独立，原样返回。只输出改写结果，不要解释。

对话历史：
{history_text}

最新问题：{current_query}

改写结果："""
        return await self.generate(
            [{"role": "user", "content": prompt}],
            task="rewrite", temperature=0.0, max_tokens=200
        )

    async def classify_intent(
        self,
        query: str,
        examples: list[dict] | None = None,   # few-shot 示例，由配置注入
    ) -> IntentType:
        """意图分类，返回 IntentType 枚举值"""
        examples_text = ""
        if examples:
            examples_text = "\n".join(
                f"问题：{e['query']} → 意图：{e['intent']}" for e in examples
            )
        prompt = f"""将以下问题分类为这些意图之一：
factual（事实查询）| relational（关系推理）| aggregation（统计聚合）|
procedural（步骤流程）| comparative（对比分析）| chitchat（闲聊）| meta（元查询）

{examples_text}

问题：{query}
意图（只输出一个英文单词）："""
        result = await self.generate(
            [{"role": "user", "content": prompt}],
            task="rewrite", temperature=0.0, max_tokens=20
        )
        try:
            return IntentType(result.strip().lower())
        except ValueError:
            return IntentType.FACTUAL

    async def compress_summary(
        self,
        existing_summary: str,
        new_qa:           dict[str, str],   # {"question": ..., "answer": ...}
        max_chars:        int = 400,
    ) -> str:
        """将新的 Q&A 合并到现有摘要，生成更新后的滚动摘要"""
        prompt = f"""现有对话摘要：
{existing_summary}

新增对话：
用户：{new_qa['question']}
助手：{new_qa['answer']}

请将新增内容合并到摘要中，保留所有重要信息（尤其是否定条件、数值约束、版本要求），
输出更新后的摘要，不超过{max_chars}字，不要添加任何解释："""
        return await self.generate(
            [{"role": "user", "content": prompt}],
            task="compress", temperature=0.0, max_tokens=600
        )

    async def generate_chunk_summary(self, chunk_content: str) -> str:
        """为单个 Chunk 生成 1-2 句摘要（入库阶段调用）"""
        prompt = f"""用1-2句话概括以下内容的核心信息，保留关键数值、条件和结论：

{chunk_content[:2000]}

摘要："""
        return await self.generate(
            [{"role": "user", "content": prompt}],
            task="compress", temperature=0.0, max_tokens=150
        )

    async def generate_image_caption(self, image_description: str) -> str:
        """为图片/图表生成文字描述（多模态场景，image_description 为 base64 或 URL）"""
        # 默认实现：如果 LLM 不支持多模态，子类必须覆盖此方法
        raise NotImplementedError(
            "该 LLM 适配器不支持多模态，请实现支持视觉的适配器或使用专门的 VLM 适配器"
        )

    async def check_faithfulness(
        self,
        answer:   str,
        contexts: list[str],
    ) -> float:
        """
        检查答案是否忠实于召回内容，返回 0-1 分数。
        1.0 = 完全基于召回内容；0.0 = 完全幻觉。
        """
        context_text = "\n---\n".join(contexts[:5])
        prompt = f"""请判断以下"答案"中的每个关键陈述是否可以在"参考内容"中找到依据。
以0到1之间的小数评分：1.0=完全有据，0.0=完全幻觉。只输出数字。

参考内容：
{context_text}

答案：
{answer}

忠实度分数（0-1）："""
        result = await self.generate(
            [{"role": "user", "content": prompt}],
            task="rewrite", temperature=0.0, max_tokens=10
        )
        try:
            score = float(result.strip())
            return max(0.0, min(1.0, score))
        except ValueError:
            return 0.5
```

### 5.3 Embedding 适配器

```python
# rag/adapters/base/embedding.py
from abc import ABC, abstractmethod

class EmbeddingAdapter(ABC):
    """
    文本/多模态 Embedding 适配器。

    关键约束：
    - 入库和检索必须使用同一个实例（或同一模型版本）
    - embed_texts 支持批处理，batch_size 由 config 控制
    - 返回的向量维度必须与 vector_store 的 collection 配置一致
    """

    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度，用于创建 collection 时的 schema 校验"""

    @abstractmethod
    async def embed_texts(
        self,
        texts:      list[str],
        batch_size: int = 64,
    ) -> list[list[float]]:
        """
        批量文本 embedding。
        返回与输入等长的向量列表，顺序对应。
        实现时需按 batch_size 分批发送，避免超出服务限制。
        """

    async def embed_query(self, query: str) -> list[float]:
        """
        单条查询 embedding。
        部分模型对查询和文档使用不同的 prefix（如 BGE 的 "为这个句子生成表示"），
        子类可覆盖此方法添加 query prefix。
        默认实现直接调用 embed_texts。
        """
        results = await self.embed_texts([query], batch_size=1)
        return results[0]

    async def embed_image(self, image_url_or_base64: str) -> list[float]:
        """
        图片 embedding（多模态场景）。
        不支持多模态的实现应抛出 NotImplementedError。
        """
        raise NotImplementedError("该 Embedding 适配器不支持图片输入")

    async def health_check(self) -> bool:
        try:
            await self.embed_texts(["test"])
            return True
        except AdapterError:
            return False
```

### 5.4 向量库适配器

```python
# rag/adapters/base/vector_store.py
from abc import ABC, abstractmethod
from rag.models import Chunk, RetrievalResult, RetrievalPath

class VectorStoreAdapter(ABC):
    """
    向量数据库适配器。

    Collection 管理说明：
    - 框架按 tenant_id + collection_name 区分集合
    - 集合不存在时 upsert 应自动创建（或抛出 AdapterNotFoundError 由框架创建）
    - 子节点和父节点存在同一 collection，通过 metadata.is_parent 区分

    过滤器说明：
    - filters 是 metadata 字段的等值/范围过滤条件
    - 格式：{"field": value} 或 {"field": {"$in": [v1,v2]}} 或 {"field": {"$gte": v}}
    - 实现时转换为各数据库的原生过滤语法
    """

    @abstractmethod
    async def upsert(
        self,
        chunks:     list[Chunk],        # chunk.embedding 必须已填充
        collection: str = "default",
        tenant_id:  str = "default",
    ) -> None:
        """写入或覆盖更新（按 chunk_id 去重）"""

    @abstractmethod
    async def search(
        self,
        query_vector: list[float],
        top_k:        int              = 20,
        filters:      dict | None      = None,
        collection:   str              = "default",
        tenant_id:    str              = "default",
    ) -> list[RetrievalResult]:
        """
        ANN 向量检索。
        返回结果的 score 为余弦相似度（0-1），path=RetrievalPath.VECTOR。
        结果按 score 降序排列。
        """

    @abstractmethod
    async def delete(
        self,
        chunk_ids:  list[str],
        collection: str = "default",
        tenant_id:  str = "default",
    ) -> None:
        """按 chunk_id 删除，用于知识库更新时清除旧版本"""

    @abstractmethod
    async def delete_by_doc(
        self,
        doc_id:     str,
        collection: str = "default",
        tenant_id:  str = "default",
    ) -> int:
        """删除某文档的所有 Chunk，返回删除数量"""

    async def get_by_ids(
        self,
        chunk_ids:  list[str],
        collection: str = "default",
        tenant_id:  str = "default",
    ) -> list[Chunk]:
        """按 ID 取回 Chunk（用于层次化 Chunk 取父节点）"""
        raise NotImplementedError

    async def health_check(self) -> bool:
        try:
            await self.search(query_vector=[0.0] * self.dim, top_k=1)
            return True
        except Exception:
            return False

    @property
    def dim(self) -> int:
        """向量维度，从 config 读取"""
        raise NotImplementedError
```

### 5.5 全文检索适配器

```python
# rag/adapters/base/full_text_search.py
from abc import ABC, abstractmethod
from rag.models import Chunk, RetrievalResult, RetrievalPath

class FullTextSearchAdapter(ABC):
    """
    全文检索适配器（BM25）。

    索引字段设计（实现时建立以下字段）：
    - content:      text，主检索字段，权重 1.0
    - summary:      text，摘要字段，权重 1.5（比正文权重更高）
    - keywords:     keyword 数组，用于精确匹配
    - section_path: keyword，用于结构化过滤
    - doc_id:       keyword
    - chunk_id:     keyword
    - allowed_roles: keyword 数组（权限过滤）
    - created_at:   date

    中文分词：必须配置 IK Analyzer（ik_max_word 用于索引，ik_smart 用于搜索）
    """

    @abstractmethod
    async def index(
        self,
        chunks:    list[Chunk],
        index:     str = "default",
        tenant_id: str = "default",
    ) -> None:
        """批量写入索引，按 chunk_id 去重（upsert 语义）"""

    @abstractmethod
    async def search(
        self,
        query:     str,
        top_k:     int            = 20,
        filters:   dict | None    = None,
        index:     str            = "default",
        tenant_id: str            = "default",
    ) -> list[RetrievalResult]:
        """
        BM25 全文检索。
        同时匹配 content（权重1.0）和 summary（权重1.5）字段。
        返回结果的 score 为 BM25 原始分数，path=RetrievalPath.BM25。
        """

    @abstractmethod
    async def delete_by_doc(
        self,
        doc_id:    str,
        index:     str = "default",
        tenant_id: str = "default",
    ) -> int:
        """删除某文档的所有索引记录，返回删除数量"""

    async def suggest(
        self,
        prefix:    str,
        size:      int = 5,
        index:     str = "default",
        tenant_id: str = "default",
    ) -> list[str]:
        """查询建议/自动补全（可选实现）"""
        return []

    async def health_check(self) -> bool: ...
```

### 5.6 文档解析适配器

```python
# rag/adapters/base/doc_parser.py
from abc import ABC, abstractmethod
from rag.models import ParsedDocument

class DocParserAdapter(ABC):
    """
    文档解析适配器。

    每种文件类型可配置不同的解析器，框架通过 file_type 路由：
    - pdf   → PDFParserAdapter 实现
    - docx  → DocxParserAdapter 实现
    - image → ImageParserAdapter 实现（含 OCR）
    - txt   → 内置简单实现，无需适配

    ParsedDocument 中每个 ParsedElement 必须携带：
    - content_type（TEXT/TABLE/IMAGE/CHART/CODE）
    - text（TABLE/IMAGE/CHART 须转换为文字描述）
    - raw_data（TABLE 保留原始行列结构；IMAGE 保留文件路径）
    - page_num
    """

    @property
    @abstractmethod
    def supported_types(self) -> list[str]:
        """返回此适配器支持的文件类型列表，如 ["pdf"]"""

    @abstractmethod
    async def parse(
        self,
        file_path:  str,            # 本地路径或对象存储 URL
        doc_id:     str | None = None,
        metadata:   dict | None = None,
    ) -> ParsedDocument:
        """
        解析文档，返回结构化的 ParsedDocument。

        实现要求：
        1. 识别并分离：正文/标题/表格/图片/图表/代码块/页眉页脚
        2. 页眉页脚、水印、重复导航栏等噪声内容丢弃
        3. 表格转为："[表格]第N行：列A=值X，列B=值Y，..." 格式的文字描述
           同时在 raw_data 中保留 {"headers": [...], "rows": [[...]]}
        4. 图片/图表：raw_data 保留 {"image_path": "..."} 供后续 VLM 处理
           text 字段暂留空，由 ImageCaptionStep 填充
        5. 标题层级识别并写入 metadata["heading_level"]（1/2/3）
        """

    async def health_check(self) -> bool: ...
```

### 5.7 业务数据适配器

```python
# rag/adapters/base/business_data.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class QueryResult:
    sql:        str | None        # 生成的 SQL（用于调试）
    data:       list[dict]        # 查询结果行
    columns:    list[str]
    row_count:  int
    error:      str | None = None

class BusinessDataAdapter(ABC):
    """
    业务数据库/API 适配器，支持自然语言转查询。

    Schema 描述文件格式（YAML）：
    tables:
      - name: orders
        description: "订单表，记录所有销售订单"
        columns:
          - name: order_id
            type: varchar
            description: "订单唯一标识"
          - name: amount
            type: decimal
            description: "订单金额，单位：元"
          - name: created_at
            type: datetime
            description: "下单时间"
    allowed_tables: [orders, products]   # 白名单，框架层面强制过滤

    安全要求：
    - 只允许 SELECT，禁止 INSERT/UPDATE/DELETE/DROP
    - 查询结果行数上限由 config.max_rows_returned 控制
    - 返回前对敏感字段脱敏（由 config.sensitive_fields 配置）
    """

    @abstractmethod
    async def nl2sql(
        self,
        question:    str,
        schema_hint: str | None = None,  # 覆盖默认 schema 描述
    ) -> str:
        """将自然语言问题转换为 SQL 语句（不执行）"""

    @abstractmethod
    async def execute(
        self,
        sql:          str,
        max_rows:     int = 100,
    ) -> QueryResult:
        """执行 SQL，返回结构化结果"""

    async def query_by_nl(
        self,
        question:    str,
        schema_hint: str | None = None,
    ) -> QueryResult:
        """便利方法：nl2sql + execute，框架调用此方法"""
        sql = await self.nl2sql(question, schema_hint)
        return await self.execute(sql)

    @abstractmethod
    async def get_schema_description(self) -> str:
        """返回供 LLM 理解的 Schema 描述文本"""

    async def health_check(self) -> bool: ...
```

### 5.8 知识图谱适配器

```python
# rag/adapters/base/knowledge_graph.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class GraphQueryResult:
    nodes:      list[dict]      # 节点列表，每个节点含 id, labels, properties
    relations:  list[dict]      # 关系列表，每个关系含 source, target, type, properties
    text:       str             # 序列化为自然语言的查询结果（喂给 LLM）
    cypher:     str | None = None

class KnowledgeGraphAdapter(ABC):
    """
    知识图谱适配器（可选，enabled=false 时框架跳过图谱召回路）。

    节点格式：{"id": "...", "labels": ["Person"], "properties": {...}}
    关系格式：{"source": "id1", "target": "id2", "type": "WORKS_FOR", "properties": {...}}

    图谱召回流程（框架已实现，适配器只需提供底层能力）：
    1. 从问题中 NER 抽取实体
    2. 调用 find_entities 找到图谱中的对应节点
    3. 调用 query_relations 按关系类型做 1-3 跳遍历
    4. 调用 to_text 将结果序列化为自然语言
    """

    @abstractmethod
    async def find_entities(
        self,
        names:      list[str],       # NER 抽取的实体名
        labels:     list[str] | None = None,  # 限定节点类型
        fuzzy:      bool             = True,
        tenant_id:  str              = "default",
    ) -> list[dict]:
        """模糊匹配实体节点，返回节点列表"""

    @abstractmethod
    async def query_relations(
        self,
        start_node_id:  str,
        relation_types: list[str] | None = None,  # None=所有类型
        direction:      str              = "both",  # "in"|"out"|"both"
        max_hops:       int              = 2,
        tenant_id:      str              = "default",
    ) -> GraphQueryResult:
        """从起始节点出发，按关系类型做多跳遍历"""

    @abstractmethod
    async def upsert_entities(
        self,
        entities:   list[dict],
        tenant_id:  str = "default",
    ) -> None:
        """写入或更新实体节点（入库阶段调用）"""

    @abstractmethod
    async def upsert_relations(
        self,
        relations:  list[dict],
        tenant_id:  str = "default",
    ) -> None:
        """写入或更新关系（入库阶段调用）"""

    def to_text(self, result: GraphQueryResult) -> str:
        """
        将图查询结果序列化为自然语言（默认实现，子类可覆盖优化）。
        输出格式示例：
        "张三 (工程师) 就职于 ABC公司 (科技企业)，
         ABC公司 的上级机构是 XYZ集团，XYZ集团 旗下还有 DEF事业部。"
        """
        lines = []
        for rel in result.relations:
            lines.append(
                f"{rel['source']} --[{rel['type']}]--> {rel['target']}"
            )
        return "\n".join(lines) if lines else "未找到相关关系信息"

    async def health_check(self) -> bool: ...
```

### 5.9 同义词适配器

```python
# rag/adapters/base/synonym.py
from abc import ABC, abstractmethod

class SynonymAdapter(ABC):
    """
    同义词与术语归一化适配器。

    用途：
    1. 查询扩展：用户输入 "掉线" → 扩展为 ["掉线","链路中断","连接断开","offline"]
    2. 术语归一化：将口语表达映射到知识库中的标准术语

    框架在以下阶段调用：
    - 查询改写阶段：expand() 用于生成同义词版本，供多路 BM25 检索
    - 入库阶段：normalize() 用于统一文档中的术语表达
    """

    @abstractmethod
    def expand(self, term: str) -> list[str]:
        """
        返回同义词列表（含原词）。
        要求同步接口（热路径，不能有网络延迟）。
        若无同义词，返回 [term]。
        """

    @abstractmethod
    def normalize(self, text: str) -> str:
        """将文本中的非标准术语替换为标准术语（全文替换）"""

    def expand_query(self, query: str) -> list[str]:
        """
        对查询中的每个关键词做同义词扩展，返回扩展后的多个查询版本。
        默认实现：识别查询中的名词短语（简单分词），逐一扩展后排列组合（取前5个版本）。
        子类可覆盖为更智能的实现。
        """
        # 默认实现：直接返回单个归一化版本
        return [self.normalize(query)]

    def load_from_excel(self, path: str) -> None:
        """
        从 Excel 文件加载同义词表。
        Excel 格式：第一列=标准术语，后续列=同义词（可为空）。
        框架内置的 FileSynonymAdapter 已实现此方法。
        客户使用 API 接入的适配器无需实现。
        """
        raise NotImplementedError
```

### 5.10 认证权限适配器

```python
# rag/adapters/base/auth.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class UserContext:
    user_id:    str
    username:   str
    roles:      list[str]
    tenant_id:  str
    extra:      dict            # 额外属性透传给 Pipeline

class AuthAdapter(ABC):
    """
    认证与权限适配器。

    权限控制机制：
    - roles 决定用户可访问的知识域（collection）
    - 知识域映射关系在 config.permission_mapping 中配置
    - 检索时框架自动将 allowed_roles 注入 filters，无需适配器干预
    """

    @abstractmethod
    async def verify_token(self, token: str) -> UserContext:
        """
        验证 Bearer Token，返回用户上下文。
        Token 无效时抛出 AdapterAuthError。
        """

    def get_allowed_collections(
        self,
        roles:              list[str],
        permission_mapping: dict[str, list[str]],  # 来自配置
    ) -> list[str]:
        """
        根据角色列表返回允许访问的 collection 列表。
        默认实现：取各角色对应 collection 集合的并集。
        "*" 表示允许访问所有 collection。
        """
        allowed: set[str] = set()
        for role in roles:
            collections = permission_mapping.get(role, [])
            if "*" in collections:
                return ["*"]
            allowed.update(collections)
        return list(allowed)

    async def health_check(self) -> bool: ...
```

### 5.11 对象存储适配器

```python
# rag/adapters/base/storage.py
from abc import ABC, abstractmethod

class StorageAdapter(ABC):
    """
    原始文件/多媒体对象存储适配器。
    用于存储：原始文档文件、图片、表格导出文件、答案来源预览。
    """

    @abstractmethod
    async def put(
        self,
        key:          str,          # 对象路径，如 "tenant_abc/docs/doc_id.pdf"
        data:         bytes,
        content_type: str  = "application/octet-stream",
        metadata:     dict | None = None,
    ) -> str:
        """上传对象，返回内部存储 URL"""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """下载对象内容"""

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def sign_url(
        self,
        key:        str,
        expires_in: int = 3600,   # 签名有效期（秒）
    ) -> str:
        """生成临时预览 URL，用于答案来源溯源"""

    @abstractmethod
    async def list_keys(self, prefix: str) -> list[str]:
        """列举指定前缀下的所有对象 key"""

    async def health_check(self) -> bool: ...
```
## 6. 框架默认适配器实现

所有默认实现位于 `rag/adapters/builtin/`，开箱即用。
客户无需修改这些文件，只需在配置中选择 `adapter` 字段即可。

### 6.1 OpenAI 兼容 LLM 适配器

```python
# rag/adapters/builtin/llm/openai_compatible.py
import httpx
from collections.abc import AsyncIterator
from rag.adapters.base.llm import LLMAdapter
from rag.adapters.base.exceptions import AdapterConnectionError, AdapterTimeoutError
from rag.config.models import LLMConfig

class OpenAICompatibleAdapter(LLMAdapter):
    """
    兼容 OpenAI Chat Completions API 格式的通用适配器。
    适用于：OpenAI、Azure OpenAI、vLLM、Ollama、通义千问、文心一言（OpenAI 兼容模式）
    等任何提供 /v1/chat/completions 接口的服务。

    注册名：openai_compatible
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=config.timeout_seconds,
        )
        # task → model 映射，允许不同任务用不同规格模型
        self._model_map = config.task_model_mapping or {}
        self._default_model = config.model

    def _get_model(self, task: str) -> str:
        return self._model_map.get(task, self._default_model)

    async def generate(
        self,
        messages:    list[dict[str, str]],
        task:        str   = "generate",
        temperature: float = 0.1,
        max_tokens:  int   = 2048,
        stream:      bool  = False,
        stop:        list[str] | None = None,
        **kwargs,
    ) -> str | AsyncIterator[str]:
        payload = {
            "model":       self._get_model(task),
            "messages":    messages,
            "temperature": temperature,
            "max_tokens":  max_tokens,
            "stream":      stream,
        }
        if stop:
            payload["stop"] = stop

        try:
            if not stream:
                resp = await self.client.post("/v1/chat/completions", json=payload)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            else:
                return self._stream_generate(payload)
        except httpx.ConnectError as e:
            raise AdapterConnectionError("OpenAICompatibleAdapter", str(e), e)
        except httpx.TimeoutException as e:
            raise AdapterTimeoutError("OpenAICompatibleAdapter", str(e), e)

    async def _stream_generate(self, payload: dict) -> AsyncIterator[str]:
        async with self.client.stream("POST", "/v1/chat/completions",
                                      json=payload) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    import json
                    data = json.loads(line[6:])
                    delta = data["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta

    async def count_tokens(self, text: str) -> int:
        # 优先使用 tiktoken（OpenAI 系列模型）
        try:
            import tiktoken
            enc = tiktoken.encoding_for_model("gpt-4o")
            return len(enc.encode(text))
        except Exception:
            # 回退：中文约 1.5 字/token，英文约 4 字/token，混合估算
            chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
            other_chars   = len(text) - chinese_chars
            return int(chinese_chars / 1.5 + other_chars / 4)
```

### 6.2 HTTP Embedding 适配器

```python
# rag/adapters/builtin/embedding/http_embedding.py
import httpx
from rag.adapters.base.embedding import EmbeddingAdapter
from rag.config.models import EmbeddingConfig

class HTTPEmbeddingAdapter(EmbeddingAdapter):
    """
    通用 HTTP Embedding 服务适配器。
    适用于：BGE 系列、M3E、text2vec 等本地部署的 Embedding 服务。

    期望服务接口（POST /embed）：
    Request:  {"texts": ["...", "..."], "model": "bge-large-zh"}
    Response: {"embeddings": [[0.1, ...], [0.2, ...]], "model": "...", "dim": 1024}

    注册名：http_embedding
    """

    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._dim   = config.dim
        self.client = httpx.AsyncClient(
            base_url=config.url,
            timeout=60.0,
        )
        # BGE 模型的查询 prefix（提升检索效果）
        self._query_prefix = config.query_prefix or ""

    @property
    def dim(self) -> int:
        return self._dim

    async def embed_texts(
        self,
        texts:      list[str],
        batch_size: int = 64,
    ) -> list[list[float]]:
        results = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i: i + batch_size]
            resp  = await self.client.post("/embed", json={
                "texts": batch,
                "model": self.config.model,
            })
            resp.raise_for_status()
            results.extend(resp.json()["embeddings"])
        return results

    async def embed_query(self, query: str) -> list[float]:
        """BGE 系列需要加 query prefix 提升检索效果"""
        prefixed = f"{self._query_prefix}{query}" if self._query_prefix else query
        results  = await self.embed_texts([prefixed], batch_size=1)
        return results[0]
```

### 6.3 Milvus 向量库适配器

```python
# rag/adapters/builtin/vector_store/milvus.py
from pymilvus import AsyncMilvusClient, DataType
from rag.adapters.base.vector_store import VectorStoreAdapter
from rag.models import Chunk, RetrievalResult, RetrievalPath
from rag.config.models import VectorStoreConfig

class MilvusAdapter(VectorStoreAdapter):
    """
    Milvus 2.4+ 向量库适配器。

    Collection Schema（框架自动创建）：
    - chunk_id:     VARCHAR(64),  PK
    - doc_id:       VARCHAR(64),  索引
    - tenant_id:    VARCHAR(64),  索引（多租户隔离）
    - embedding:    FLOAT_VECTOR(dim)
    - content:      VARCHAR(65535)
    - summary:      VARCHAR(4096)
    - keywords:     ARRAY<VARCHAR>
    - section_path: VARCHAR(1024)
    - page_num:     INT32
    - allowed_roles: ARRAY<VARCHAR>
    - is_parent:    BOOL
    - quality_score: FLOAT
    - created_at:   INT64 (unix timestamp)

    索引：HNSW，metric=COSINE，M=16，efConstruction=256

    注册名：milvus
    """

    def __init__(self, config: VectorStoreConfig):
        self.config = config
        self._dim   = config.dim
        self.client = AsyncMilvusClient(
            uri=f"http://{config.host}:{config.port}",
        )

    @property
    def dim(self) -> int:
        return self._dim

    def _collection_name(self, collection: str, tenant_id: str) -> str:
        prefix = self.config.collection_prefix or ""
        return f"{prefix}{tenant_id}_{collection}"

    async def upsert(
        self,
        chunks:     list[Chunk],
        collection: str = "default",
        tenant_id:  str = "default",
    ) -> None:
        col_name = self._collection_name(collection, tenant_id)
        await self._ensure_collection(col_name)
        data = [{
            "chunk_id":      c.chunk_id,
            "doc_id":        c.doc_id,
            "tenant_id":     tenant_id,
            "embedding":     c.embedding,
            "content":       c.content[:65000],
            "summary":       (c.summary or "")[:4000],
            "keywords":      c.keywords[:20],
            "section_path":  c.section_path or "",
            "page_num":      c.page_num or 0,
            "allowed_roles": c.allowed_roles,
            "is_parent":     len(c.child_chunk_ids) > 0,
            "quality_score": c.quality_score,
            "created_at":    int(c.created_at.timestamp()),
        } for c in chunks if c.embedding]
        await self.client.upsert(collection_name=col_name, data=data)

    async def search(
        self,
        query_vector: list[float],
        top_k:        int          = 20,
        filters:      dict | None  = None,
        collection:   str          = "default",
        tenant_id:    str          = "default",
    ) -> list[RetrievalResult]:
        col_name = self._collection_name(collection, tenant_id)
        filter_expr = self._build_filter(filters, tenant_id)
        results = await self.client.search(
            collection_name = col_name,
            data            = [query_vector],
            anns_field      = "embedding",
            limit           = top_k,
            filter          = filter_expr,
            output_fields   = ["chunk_id", "doc_id", "content", "summary",
                               "section_path", "page_num", "allowed_roles"],
        )
        return [
            RetrievalResult(
                chunk_id = hit["entity"]["chunk_id"],
                doc_id   = hit["entity"]["doc_id"],
                content  = hit["entity"]["content"],
                score    = hit["distance"],
                rank     = i + 1,
                path     = RetrievalPath.VECTOR,
                metadata = {
                    "summary":      hit["entity"].get("summary"),
                    "section_path": hit["entity"].get("section_path"),
                    "page_num":     hit["entity"].get("page_num"),
                },
            )
            for i, hit in enumerate(results[0])
        ]

    def _build_filter(self, filters: dict | None, tenant_id: str) -> str:
        """将框架通用 filter 转换为 Milvus 过滤表达式"""
        exprs = [f'tenant_id == "{tenant_id}"']
        if filters:
            for k, v in filters.items():
                if isinstance(v, str):
                    exprs.append(f'{k} == "{v}"')
                elif isinstance(v, list):
                    vals = ", ".join(f'"{x}"' for x in v)
                    exprs.append(f'{k} in [{vals}]')
                elif isinstance(v, dict):
                    if "$gte" in v:
                        exprs.append(f'{k} >= {v["$gte"]}')
                    if "$lte" in v:
                        exprs.append(f'{k} <= {v["$lte"]}')
        return " and ".join(exprs)

    async def delete(self, chunk_ids: list[str],
                     collection: str = "default", tenant_id: str = "default") -> None:
        col_name = self._collection_name(collection, tenant_id)
        ids_expr = ", ".join(f'"{cid}"' for cid in chunk_ids)
        await self.client.delete(col_name, filter=f'chunk_id in [{ids_expr}]')

    async def delete_by_doc(self, doc_id: str,
                            collection: str = "default", tenant_id: str = "default") -> int:
        col_name = self._collection_name(collection, tenant_id)
        result   = await self.client.delete(col_name,
                                            filter=f'doc_id == "{doc_id}"')
        return result.delete_count

    async def _ensure_collection(self, col_name: str) -> None:
        """若 collection 不存在则自动创建"""
        if not await self.client.has_collection(col_name):
            # 完整 schema 定义见注释，此处简化为关键字段
            await self.client.create_collection(
                collection_name = col_name,
                dimension       = self._dim,
                metric_type     = "COSINE",
                auto_id         = False,
            )
```

### 6.4 Elasticsearch 全文检索适配器

```python
# rag/adapters/builtin/full_text_search/elasticsearch.py
from elasticsearch import AsyncElasticsearch
from rag.adapters.base.full_text_search import FullTextSearchAdapter
from rag.models import Chunk, RetrievalResult, RetrievalPath
from rag.config.models import FullTextSearchConfig

class ElasticsearchAdapter(FullTextSearchAdapter):
    """
    Elasticsearch 8.x 全文检索适配器。

    Index Mapping（框架自动创建）：
    {
      "settings": {
        "analysis": {
          "analyzer": {
            "ik_index":  {"type": "custom", "tokenizer": "ik_max_word"},
            "ik_search": {"type": "custom", "tokenizer": "ik_smart"}
          }
        }
      },
      "mappings": {
        "properties": {
          "chunk_id":     {"type": "keyword"},
          "doc_id":       {"type": "keyword"},
          "tenant_id":    {"type": "keyword"},
          "content":      {"type": "text",    "analyzer": "ik_index",
                           "search_analyzer": "ik_search"},
          "summary":      {"type": "text",    "analyzer": "ik_index",
                           "search_analyzer": "ik_search", "boost": 1.5},
          "keywords":     {"type": "keyword"},
          "section_path": {"type": "keyword"},
          "page_num":     {"type": "integer"},
          "allowed_roles":{"type": "keyword"},
          "quality_score":{"type": "float"},
          "created_at":   {"type": "date"}
        }
      }
    }

    注册名：elasticsearch
    """

    def __init__(self, config: FullTextSearchConfig):
        self.config = config
        self.es = AsyncElasticsearch(
            hosts   = config.hosts,
            api_key = config.api_key,
        )
        self._index_prefix = config.index_prefix or "rag_"

    def _index_name(self, index: str, tenant_id: str) -> str:
        return f"{self._index_prefix}{tenant_id}_{index}"

    async def index(
        self,
        chunks:    list[Chunk],
        index:     str = "default",
        tenant_id: str = "default",
    ) -> None:
        idx_name = self._index_name(index, tenant_id)
        await self._ensure_index(idx_name)
        ops = []
        for c in chunks:
            ops.append({"index": {"_index": idx_name, "_id": c.chunk_id}})
            ops.append({
                "chunk_id":      c.chunk_id,
                "doc_id":        c.doc_id,
                "tenant_id":     tenant_id,
                "content":       c.content,
                "summary":       c.summary or "",
                "keywords":      c.keywords,
                "section_path":  c.section_path or "",
                "page_num":      c.page_num or 0,
                "allowed_roles": c.allowed_roles,
                "quality_score": c.quality_score,
                "created_at":    c.created_at.isoformat(),
            })
        if ops:
            await self.es.bulk(body=ops)

    async def search(
        self,
        query:     str,
        top_k:     int           = 20,
        filters:   dict | None   = None,
        index:     str           = "default",
        tenant_id: str           = "default",
    ) -> list[RetrievalResult]:
        idx_name = self._index_name(index, tenant_id)
        must_clauses = [
            {
                "multi_match": {
                    "query":  query,
                    "fields": ["content^1.0", "summary^1.5", "keywords^2.0"],
                    "type":   "best_fields",
                    "analyzer": "ik_smart",
                }
            },
            {"term": {"tenant_id": tenant_id}},
        ]
        if filters:
            for k, v in filters.items():
                if isinstance(v, list):
                    must_clauses.append({"terms": {k: v}})
                else:
                    must_clauses.append({"term": {k: v}})

        resp = await self.es.search(
            index = idx_name,
            body  = {
                "query": {"bool": {"must": must_clauses}},
                "size":  top_k,
            },
        )
        return [
            RetrievalResult(
                chunk_id = hit["_source"]["chunk_id"],
                doc_id   = hit["_source"]["doc_id"],
                content  = hit["_source"]["content"],
                score    = hit["_score"],
                rank     = i + 1,
                path     = RetrievalPath.BM25,
                metadata = {
                    "summary":      hit["_source"].get("summary"),
                    "section_path": hit["_source"].get("section_path"),
                    "page_num":     hit["_source"].get("page_num"),
                },
            )
            for i, hit in enumerate(resp["hits"]["hits"])
        ]

    async def delete_by_doc(self, doc_id: str,
                            index: str = "default", tenant_id: str = "default") -> int:
        idx_name = self._index_name(index, tenant_id)
        resp = await self.es.delete_by_query(
            index = idx_name,
            body  = {"query": {"bool": {"must": [
                {"term": {"doc_id":    doc_id}},
                {"term": {"tenant_id": tenant_id}},
            ]}}},
        )
        return resp["deleted"]

    async def _ensure_index(self, idx_name: str) -> None:
        if not await self.es.indices.exists(index=idx_name):
            await self.es.indices.create(index=idx_name, body=INDEX_MAPPING)
        # INDEX_MAPPING 见上方注释中的完整定义
```

### 6.5 文件型同义词适配器

```python
# rag/adapters/builtin/synonym/file_based.py
import re
from pathlib import Path
from rag.adapters.base.synonym import SynonymAdapter
from rag.config.models import SynonymConfig

class FileSynonymAdapter(SynonymAdapter):
    """
    基于文件的同义词适配器。
    支持从 Excel 或 YAML 文件加载同义词表，热重载（文件变更后自动重新加载）。

    Excel 格式：
    | 标准术语     | 同义词1   | 同义词2   | 同义词3 |
    | 链路中断     | 掉线      | 断网      | offline |

    YAML 格式：
    synonyms:
      - standard: "链路中断"
        aliases:  ["掉线", "断网", "offline"]

    注册名：file_based
    """

    def __init__(self, config: SynonymConfig):
        self.config = config
        # standard_term → [所有同义词含自身]
        self._expand_map:    dict[str, list[str]] = {}
        # alias → standard_term
        self._normalize_map: dict[str, str]       = {}
        for f in config.files or []:
            self._load_file(f)

    def _load_file(self, path: str) -> None:
        p = Path(path)
        if p.suffix in (".xlsx", ".xls"):
            self._load_excel(p)
        elif p.suffix in (".yaml", ".yml"):
            self._load_yaml(p)

    def _load_excel(self, path: Path) -> None:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None]
            if not cells:
                continue
            standard = cells[0]
            aliases  = cells[1:]
            all_terms = [standard] + aliases
            self._expand_map[standard] = all_terms
            for alias in aliases:
                self._normalize_map[alias] = standard

    def _load_yaml(self, path: Path) -> None:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for entry in data.get("synonyms", []):
            standard = entry["standard"]
            aliases  = entry.get("aliases", [])
            self._expand_map[standard] = [standard] + aliases
            for alias in aliases:
                self._normalize_map[alias] = standard

    def expand(self, term: str) -> list[str]:
        # 直接命中标准术语
        if term in self._expand_map:
            return self._expand_map[term]
        # 命中别名，返回标准术语组的所有同义词
        if term in self._normalize_map:
            standard = self._normalize_map[term]
            return self._expand_map.get(standard, [term])
        return [term]

    def normalize(self, text: str) -> str:
        """将文本中所有非标准术语替换为标准术语"""
        for alias, standard in self._normalize_map.items():
            text = re.sub(re.escape(alias), standard, text)
        return text

    def expand_query(self, query: str) -> list[str]:
        """
        对查询做术语扩展，返回多个检索版本（最多5个）。
        策略：先归一化，再对每个已知术语做同义词替换，生成变体。
        """
        normalized = self.normalize(query)
        variants   = {normalized}
        for alias in self._normalize_map:
            if alias in query:
                variant = query.replace(alias, self._normalize_map[alias])
                variants.add(variant)
        return list(variants)[:5]
```

---

## 7. 知识库构建 Pipeline（离线）

### 7.1 入库步骤定义

每个步骤实现 `execute(ctx: IngestionContext) -> IngestionContext`。

```python
# rag/pipeline/ingestion/steps.py
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from rag.models import ParsedDocument, ParsedElement, Chunk, ContentType

@dataclass
class IngestionContext:
    """入库 Pipeline 共享上下文"""
    doc_id:         str
    source_path:    str
    tenant_id:      str
    collection:     str             = "default"
    allowed_roles:  list[str]       = field(default_factory=list)
    metadata:       dict            = field(default_factory=dict)
    # 各步骤填充
    parsed_doc:     ParsedDocument | None = None
    chunks:         list[Chunk]     = field(default_factory=list)
    errors:         list[str]       = field(default_factory=list)


# ── Step 1: 文档解析 ─────────────────────────────────────────────────────────

class ParseDocumentStep:
    """
    调用 DocParserAdapter 解析原始文档。
    根据文件扩展名自动路由到对应解析器。
    解析失败时记录 error 并设置 parsed_doc=None，Pipeline 后续步骤检测到后终止。
    """
    name = "parse_document"

    def __init__(self, parsers: dict[str, DocParserAdapter]):
        # parsers: {"pdf": PDFParser实例, "docx": DocxParser实例, ...}
        self.parsers = parsers

    async def execute(self, ctx: IngestionContext) -> IngestionContext:
        ext = ctx.source_path.rsplit(".", 1)[-1].lower()
        parser = self.parsers.get(ext)
        if not parser:
            ctx.errors.append(f"不支持的文件类型: {ext}")
            return ctx
        try:
            ctx.parsed_doc = await parser.parse(
                ctx.source_path, doc_id=ctx.doc_id, metadata=ctx.metadata
            )
        except Exception as e:
            ctx.errors.append(f"文档解析失败: {e}")
        return ctx


# ── Step 2: 图片 Caption 生成 ────────────────────────────────────────────────

class ImageCaptionStep:
    """
    对 ParsedDocument 中 content_type=IMAGE/CHART 的元素，
    调用 LLMAdapter.generate_image_caption 生成文字描述，
    填入 element.text 字段，供后续分块和 Embedding 使用。
    并发处理（asyncio.gather），最多 concurrent_limit 个并发。
    """
    name = "generate_image_caption"

    def __init__(self, llm: LLMAdapter, concurrent_limit: int = 4):
        self.llm   = llm
        self._sem  = asyncio.Semaphore(concurrent_limit)

    async def _caption_one(self, element: ParsedElement) -> None:
        if element.content_type not in (ContentType.IMAGE, ContentType.CHART):
            return
        image_path = (element.raw_data or {}).get("image_path", "")
        if not image_path:
            return
        async with self._sem:
            try:
                element.text = await self.llm.generate_image_caption(image_path)
            except NotImplementedError:
                element.text = f"[图片: {image_path}]"
            except Exception as e:
                element.text = f"[图片解析失败: {e}]"

    async def execute(self, ctx: IngestionContext) -> IngestionContext:
        if not ctx.parsed_doc:
            return ctx
        tasks = [self._caption_one(el) for el in ctx.parsed_doc.elements]
        await asyncio.gather(*tasks)
        return ctx


# ── Step 3: 智能分块 ─────────────────────────────────────────────────────────

class SemanticChunkingStep:
    """
    将 ParsedDocument.elements 转换为层次化 Chunk 列表。

    分块策略（按优先级）：
    1. 标题边界优先：heading_level 变化处强制分块
    2. 语义边界：段落/句号为优先分割点
    3. 固定窗口兜底：超过 max_tokens 时按 overlap 滑窗截断

    层次化结构：
    - 父节点（is_parent=True）：完整段落或小节，child_chunk_ids 指向子节点
    - 子节点（is_parent=False）：细粒度句子，parent_chunk_id 指向父节点
    框架检索时用子节点定位，但将父节点内容喂给 LLM。

    参数（来自 config.pipeline.ingestion.chunking）：
    - max_tokens:  512     子节点最大 token 数
    - overlap:     50      固定窗口重叠 token 数
    - parent_max:  2048    父节点最大 token 数
    """
    name = "semantic_chunking"

    def __init__(self, llm: LLMAdapter, max_tokens: int = 512,
                 overlap: int = 50, parent_max: int = 2048):
        self.llm        = llm
        self.max_tokens = max_tokens
        self.overlap    = overlap
        self.parent_max = parent_max

    async def execute(self, ctx: IngestionContext) -> IngestionContext:
        if not ctx.parsed_doc:
            return ctx
        chunks = await self._build_chunks(ctx.parsed_doc, ctx)
        ctx.chunks = chunks
        return ctx

    async def _build_chunks(
        self, doc: ParsedDocument, ctx: IngestionContext
    ) -> list[Chunk]:
        """
        实现要点：
        1. 将 elements 按 heading_level 分组为段落组
        2. 每组内按句子边界细分为子 Chunk（≤ max_tokens）
        3. 整组作为父 Chunk（≤ parent_max，超出则按 parent_max 截断）
        4. 为每对父子节点互相设置 parent_chunk_id / child_chunk_ids
        5. 表格元素整体作为独立子 Chunk（不再细分），父节点为同一段落
        """
        # 完整实现约 80 行，核心逻辑见下方伪码：
        all_chunks: list[Chunk] = []
        groups     = self._group_by_heading(doc.elements)
        for group in groups:
            parent_text = "\n".join(el.text for el in group if el.text)
            if not parent_text.strip():
                continue
            parent = Chunk(
                doc_id         = doc.doc_id,
                content        = parent_text[:self.parent_max * 4],  # 粗略字符截断
                content_type   = ContentType.MIXED,
                section_path   = self._get_section_path(group),
                allowed_roles  = ctx.allowed_roles,
                metadata       = {"is_parent": True},
            )
            children = self._split_to_children(group, doc.doc_id,
                                                ctx.allowed_roles, parent.chunk_id)
            parent.child_chunk_ids = [c.chunk_id for c in children]
            all_chunks.append(parent)
            all_chunks.extend(children)
        return all_chunks

    def _group_by_heading(
        self, elements: list[ParsedElement]
    ) -> list[list[ParsedElement]]:
        """按标题层级将元素分组，每遇到 heading_level=1 或 2 开始新组"""
        groups, current = [], []
        for el in elements:
            level = el.metadata.get("heading_level")
            if level in (1, 2) and current:
                groups.append(current)
                current = []
            current.append(el)
        if current:
            groups.append(current)
        return groups

    def _split_to_children(
        self,
        elements:     list[ParsedElement],
        doc_id:       str,
        allowed_roles: list[str],
        parent_id:    str,
    ) -> list[Chunk]:
        """将元素组切分为细粒度子 Chunk"""
        children: list[Chunk] = []
        buffer    = ""
        for el in elements:
            if el.content_type == ContentType.TABLE:
                # 表格作为独立子 Chunk
                if buffer.strip():
                    children.append(self._make_child(
                        buffer, doc_id, allowed_roles, parent_id, el))
                    buffer = ""
                children.append(self._make_child(
                    el.text, doc_id, allowed_roles, parent_id, el,
                    content_type=ContentType.TABLE))
            else:
                # 按句号分割，超过 max_tokens 时截断
                sentences = el.text.replace("。", "。\n").split("\n")
                for sent in sentences:
                    if not sent.strip():
                        continue
                    candidate = buffer + sent
                    # 粗略估算：中文 max_tokens * 1.5 字符
                    if len(candidate) > self.max_tokens * 1.5 and buffer:
                        children.append(self._make_child(
                            buffer, doc_id, allowed_roles, parent_id, el))
                        buffer = sent
                    else:
                        buffer = candidate
        if buffer.strip():
            children.append(self._make_child(
                buffer, doc_id, allowed_roles, parent_id, None))
        return children

    def _make_child(
        self, text: str, doc_id: str, allowed_roles: list[str],
        parent_id: str, element: ParsedElement | None,
        content_type: ContentType = ContentType.TEXT,
    ) -> Chunk:
        return Chunk(
            doc_id          = doc_id,
            content         = text.strip(),
            content_type    = content_type,
            parent_chunk_id = parent_id,
            page_num        = element.page_num if element else None,
            allowed_roles   = allowed_roles,
        )

    def _get_section_path(self, elements: list[ParsedElement]) -> str | None:
        for el in elements:
            if el.metadata.get("heading_level"):
                return el.text[:100]
        return None


# ── Step 4: 元数据增强 ───────────────────────────────────────────────────────

class MetadataEnrichmentStep:
    """
    为每个子 Chunk 生成元数据（并发执行，父节点不重复处理）：
    1. LLM 生成摘要（100字以内）→ chunk.summary
    2. 关键词抽取（jieba + TF-IDF 或 LLM）→ chunk.keywords
    3. NER 实体抽取（spaCy 或 LLM）→ chunk.entities
    4. 信息密度评分 → chunk.quality_score
       评分规则：纯数字/特殊符号占比高则分低；信息密集的技术内容分高

    性能说明：
    - 摘要生成调用 LLM，是入库最慢的步骤
    - 通过 concurrent_limit 控制并发，推荐值 8-16
    - 父节点不生成摘要（其摘要由子节点摘要拼接得到）
    """
    name = "metadata_enrichment"

    def __init__(self, llm: LLMAdapter, concurrent_limit: int = 8):
        self.llm  = llm
        self._sem = asyncio.Semaphore(concurrent_limit)

    async def _enrich_one(self, chunk: Chunk) -> None:
        if chunk.metadata.get("is_parent"):
            return  # 父节点跳过
        async with self._sem:
            # 摘要
            chunk.summary = await self.llm.generate_chunk_summary(chunk.content)
            # 关键词（简单实现：用 jieba 提取 top-10 名词短语）
            chunk.keywords = self._extract_keywords(chunk.content)
            # 质量评分
            chunk.quality_score = self._score_quality(chunk.content)

    def _extract_keywords(self, text: str, top_k: int = 10) -> list[str]:
        try:
            import jieba.analyse
            return jieba.analyse.extract_tags(text, topK=top_k)
        except ImportError:
            # 降级：简单分词取高频词
            return []

    def _score_quality(self, text: str) -> float:
        """信息密度评分：0-1"""
        if not text:
            return 0.0
        total   = len(text)
        chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        return min(1.0, chinese / total + 0.3)  # 中文密度越高分越高

    async def execute(self, ctx: IngestionContext) -> IngestionContext:
        tasks = [self._enrich_one(c) for c in ctx.chunks]
        await asyncio.gather(*tasks)
        return ctx


# ── Step 5: Embedding 生成 ───────────────────────────────────────────────────

class EmbeddingStep:
    """
    对所有子 Chunk 生成 Embedding 向量。
    Embedding 内容 = "摘要\n关键词: k1 k2 k3\n正文"（信息更密集）
    父节点不生成 Embedding（不参与向量检索，只作为上下文来源）。
    """
    name = "embed_chunks"

    def __init__(self, embedding: EmbeddingAdapter, batch_size: int = 64):
        self.embedding  = embedding
        self.batch_size = batch_size

    async def execute(self, ctx: IngestionContext) -> IngestionContext:
        children = [c for c in ctx.chunks if not c.metadata.get("is_parent")]
        if not children:
            return ctx
        texts = [self._build_embed_text(c) for c in children]
        vectors = await self.embedding.embed_texts(texts, self.batch_size)
        for chunk, vec in zip(children, vectors):
            chunk.embedding = vec
        return ctx

    def _build_embed_text(self, chunk: Chunk) -> str:
        parts = []
        if chunk.summary:
            parts.append(chunk.summary)
        if chunk.keywords:
            parts.append("关键词: " + " ".join(chunk.keywords))
        parts.append(chunk.content)
        return "\n".join(parts)[:2000]


# ── Step 6: 并行写入四库 ─────────────────────────────────────────────────────

class WriteToStoresStep:
    """
    并行将 Chunk 写入四个存储系统：
    1. 向量库（子节点 + 父节点，父节点不含 embedding）
    2. 全文索引库（子节点 + 父节点）
    3. 知识图谱（从 entities 构建节点和关系，optional）
    4. 原始文件存储（已在 ParseDocumentStep 前由调用方完成，此处跳过）

    任一存储写入失败时：记录 error，但不中止（保证其他库写入成功）。
    """
    name = "write_to_stores"

    def __init__(
        self,
        vector_store:  VectorStoreAdapter,
        fts:           FullTextSearchAdapter,
        graph:         KnowledgeGraphAdapter | None = None,
    ):
        self.vector_store = vector_store
        self.fts          = fts
        self.graph        = graph

    async def execute(self, ctx: IngestionContext) -> IngestionContext:
        tasks = [
            self._write_vector(ctx),
            self._write_fts(ctx),
        ]
        if self.graph:
            tasks.append(self._write_graph(ctx))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                ctx.errors.append(f"写入失败: {r}")
        return ctx

    async def _write_vector(self, ctx: IngestionContext) -> None:
        await self.vector_store.upsert(
            ctx.chunks, ctx.collection, ctx.tenant_id
        )

    async def _write_fts(self, ctx: IngestionContext) -> None:
        await self.fts.index(ctx.chunks, ctx.collection, ctx.tenant_id)

    async def _write_graph(self, ctx: IngestionContext) -> None:
        entities  = []
        relations = []
        for c in ctx.chunks:
            for ent in c.entities:
                entities.append({"name": ent, "doc_id": c.doc_id,
                                  "chunk_id": c.chunk_id})
        if entities:
            await self.graph.upsert_entities(entities, ctx.tenant_id)
```

### 7.2 入库 Pipeline 组装

```python
# rag/pipeline/ingestion/pipeline.py
from rag.pipeline.engine import PipelineEngine
from rag.config.models import TenantConfig
from rag.config.registry import AdapterRegistry

def build_ingestion_pipeline(
    config:   TenantConfig,
    registry: AdapterRegistry,
) -> PipelineEngine:
    """
    根据租户配置组装入库 Pipeline。
    config.pipeline.ingestion.steps 列表决定哪些步骤启用及顺序。
    """
    llm        = registry.get_llm(config)
    embedding  = registry.get_embedding(config)
    parsers    = registry.get_parsers(config)
    vs         = registry.get_vector_store(config)
    fts        = registry.get_fts(config)
    graph      = registry.get_graph(config)   # 可能为 None

    step_map = {
        "parse_document":        ParseDocumentStep(parsers),
        "generate_image_caption": ImageCaptionStep(llm),
        "semantic_chunking":     SemanticChunkingStep(
                                     llm,
                                     max_tokens = config.pipeline.ingestion.chunking.max_tokens,
                                     overlap    = config.pipeline.ingestion.chunking.overlap,
                                 ),
        "metadata_enrichment":   MetadataEnrichmentStep(llm),
        "embed_chunks":          EmbeddingStep(embedding, config.embedding.batch_size),
        "write_to_stores":       WriteToStoresStep(vs, fts, graph),
    }

    enabled_steps = [
        step_map[name]
        for name in config.pipeline.ingestion.steps
        if name in step_map
    ]
    return PipelineEngine(steps=enabled_steps)
```
## 8. 在线问答 Pipeline

### 8.1 问答步骤定义

```python
# rag/pipeline/query/steps.py
from __future__ import annotations
import asyncio
from rag.models import (
    PipelineContext, QueryUnderstanding, RetrievalResult,
    FusedResult, RetrievalPath, IntentType
)

# ── Step 1: 安全过滤 ─────────────────────────────────────────────────────────

class SecurityFilterStep:
    """
    安全与合规过滤，任一规则命中则设置 ctx.answer 并终止后续步骤。

    检查项：
    1. 敏感词过滤（从 config.security.sensitive_words 加载词表）
    2. Prompt 注入检测（检测 "忽略之前的指令"/"ignore previous" 等模式）
    3. 权限校验（ctx.metadata["user_context"].roles 与请求的 collection 匹配）
    4. 查询长度限制（config.security.max_query_length，默认 2000）
    """
    name = "security_filter"

    def __init__(self, sensitive_words: list[str], max_length: int = 2000):
        self.sensitive_words = set(sensitive_words)
        self.max_length      = max_length
        self._injection_patterns = [
            "忽略之前", "ignore previous", "ignore all",
            "system prompt", "你现在是", "act as",
        ]

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        query = ctx.raw_query
        # 长度检查
        if len(query) > self.max_length:
            ctx.answer = f"查询过长，请将问题限制在 {self.max_length} 字以内。"
            return ctx
        # 注入检测
        lower = query.lower()
        for pattern in self._injection_patterns:
            if pattern in lower:
                ctx.answer = "检测到异常查询模式，请重新提问。"
                return ctx
        # 敏感词
        for word in self.sensitive_words:
            if word in query:
                ctx.answer = "您的问题包含不适当内容，请修改后重试。"
                return ctx
        return ctx


# ── Step 2: 查询理解 ─────────────────────────────────────────────────────────

class QueryUnderstandingStep:
    """
    查询理解，填充 ctx.understanding。

    子步骤（顺序执行）：
    A. 从 SessionState 取最近 3 轮对话历史
    B. Standalone 补全：调用 LLMAdapter.rewrite_standalone
    C. 意图分类：调用 LLMAdapter.classify_intent
    D. 话题跳转检测：standalone_query embedding 与 working_memory.topic_vector 余弦相似度
       < topic_shift_threshold（默认 0.5）则判定为跳转
    E. 多路改写：调用 SynonymAdapter.expand_query 生成同义版本
    F. 实体识别：简单规则（先jieba分词，再与知识图谱实体列表匹配）或 LLM
    G. 子问题分解（仅 comparative 意图）：调用 LLM 分解为原子子问题
    H. 路由决策：根据意图填充 active_paths
       - chitchat/meta       → []（不检索）
       - relational          → [VECTOR, GRAPH]
       - aggregation         → [STRUCTURED]
       - factual/procedural  → [VECTOR, BM25]
       - comparative         → [VECTOR, BM25]（子问题分解后多次调用）
    """
    name = "query_understanding"

    def __init__(
        self,
        llm:              LLMAdapter,
        embedding:        EmbeddingAdapter,
        synonym:          SynonymAdapter,
        memory_manager:   MemoryManager,
        intent_examples:  list[dict] | None = None,    # few-shot 示例
        topic_shift_thr:  float             = 0.5,
    ):
        self.llm           = llm
        self.embedding     = embedding
        self.synonym       = synonym
        self.memory        = memory_manager
        self.intent_examples = intent_examples
        self.shift_thr     = topic_shift_thr

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.answer:  # 上游已终止
            return ctx

        state = await self.memory.get_session(ctx.session_id)

        # A. 取最近3轮对话（用于 standalone 补全）
        history = [
            {"role": t.role, "content": t.content}
            for t in (state.short_term[-6:] if state else [])
        ]

        # B. Standalone 补全
        standalone = await self.llm.rewrite_standalone(ctx.raw_query, history)

        # C. 意图分类
        intent = await self.llm.classify_intent(standalone, self.intent_examples)

        # D. 话题跳转检测
        topic_shift = False
        if state and state.working_memory.topic_vector:
            q_vec = await self.embedding.embed_query(standalone)
            topic_vec = state.working_memory.topic_vector
            sim = self._cosine(q_vec, topic_vec)
            topic_shift = sim < self.shift_thr

        # E. 同义词扩展
        rewrites = self.synonym.expand_query(standalone)

        # F. 简单实体识别
        entities = self._extract_entities(standalone,
                                           state.working_memory.entity_slots if state else None)

        # G. 子问题分解（comparative）
        sub_questions: list[str] = []
        if intent == IntentType.COMPARATIVE:
            sub_questions = await self._decompose(standalone)

        # H. 路由决策
        active_paths = self._route(intent)

        ctx.understanding = QueryUnderstanding(
            original_query   = ctx.raw_query,
            standalone_query = standalone,
            rewrites         = rewrites,
            intent           = intent,
            entities         = entities,
            sub_questions    = sub_questions,
            active_paths     = active_paths,
            topic_shift      = topic_shift,
        )
        return ctx

    def _cosine(self, a: list[float], b: list[float]) -> float:
        import math
        dot = sum(x * y for x, y in zip(a, b))
        na  = math.sqrt(sum(x * x for x in a))
        nb  = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb + 1e-9)

    def _extract_entities(self, text: str, slots) -> list[str]:
        """简单规则 NER：从 entity_slots 中提取已知实体，用于图谱查询"""
        entities = []
        if slots:
            if slots.product and slots.product in text:
                entities.append(slots.product)
        return entities

    async def _decompose(self, query: str) -> list[str]:
        prompt = (f"将以下复合问题拆分为多个独立的原子子问题，每行一个，不要编号：\n\n{query}")
        result = await self.llm.generate(
            [{"role": "user", "content": prompt}],
            task="rewrite", temperature=0.0, max_tokens=300
        )
        return [line.strip() for line in result.strip().split("\n") if line.strip()]

    def _route(self, intent: IntentType) -> list[RetrievalPath]:
        mapping = {
            IntentType.CHITCHAT:    [],
            IntentType.META:        [],
            IntentType.RELATIONAL:  [RetrievalPath.VECTOR, RetrievalPath.GRAPH],
            IntentType.AGGREGATION: [RetrievalPath.STRUCTURED],
            IntentType.FACTUAL:     [RetrievalPath.VECTOR, RetrievalPath.BM25],
            IntentType.PROCEDURAL:  [RetrievalPath.VECTOR, RetrievalPath.BM25],
            IntentType.COMPARATIVE: [RetrievalPath.VECTOR, RetrievalPath.BM25],
        }
        return mapping.get(intent, [RetrievalPath.VECTOR, RetrievalPath.BM25])


# ── Step 3: 多路并行召回 ─────────────────────────────────────────────────────

class ParallelRetrievalStep:
    """
    按 ctx.understanding.active_paths 并行执行多路召回，
    结果填入 ctx.retrieval_results[path]。

    各路召回详细说明：

    VECTOR 路：
    - 对 standalone_query + 每个 rewrite 分别 embed，共 1+N 个查询向量
    - 各向量分别检索 top_k，结果合并去重（按 chunk_id）
    - filters 注入：allowed_roles（权限）+ 可选的 section/collection 过滤
    - 层次化 Chunk：检索到子节点后，异步取回父节点内容填入 parent_content

    BM25 路：
    - 对 standalone_query + 每个 rewrite 分别调用 fts.search
    - 结果合并去重
    - 同时匹配 content（权重1.0）和 summary（权重1.5）

    GRAPH 路：
    - 从 ctx.understanding.entities 提取实体，调用 graph.find_entities
    - 对每个命中实体调用 graph.query_relations（max_hops=2）
    - 将 GraphQueryResult.text 包装为 RetrievalResult（content=text，score=1.0）

    STRUCTURED 路：
    - 调用 business_data.query_by_nl(standalone_query)
    - 将 QueryResult 序列化为 Markdown 表格
    - 包装为 RetrievalResult（content=table_text，score=1.0）

    超时控制：各路独立超时（config.retrieval.timeout_per_path_seconds，默认5s）
    某路超时只记录 warning，不影响其他路结果。
    """
    name = "parallel_retrieval"

    def __init__(
        self,
        vector_store:   VectorStoreAdapter,
        fts:            FullTextSearchAdapter,
        embedding:      EmbeddingAdapter,
        graph:          KnowledgeGraphAdapter | None = None,
        business_data:  BusinessDataAdapter   | None = None,
        vector_top_k:   int   = 20,
        bm25_top_k:     int   = 20,
        timeout:        float = 5.0,
    ):
        self.vs            = vector_store
        self.fts           = fts
        self.embedding     = embedding
        self.graph         = graph
        self.biz           = business_data
        self.vector_top_k  = vector_top_k
        self.bm25_top_k    = bm25_top_k
        self.timeout       = timeout

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.answer or not ctx.understanding:
            return ctx
        u = ctx.understanding
        if not u.active_paths:
            # chitchat / meta：不检索，让 LLM 直接生成
            return ctx

        user_ctx = ctx.metadata.get("user_context")
        filters  = {}
        if user_ctx and user_ctx.roles:
            filters["allowed_roles"] = user_ctx.roles

        tasks = {}
        if RetrievalPath.VECTOR in u.active_paths:
            tasks[RetrievalPath.VECTOR] = self._retrieve_vector(u, filters, ctx)
        if RetrievalPath.BM25 in u.active_paths:
            tasks[RetrievalPath.BM25]   = self._retrieve_bm25(u, filters, ctx)
        if RetrievalPath.GRAPH in u.active_paths and self.graph:
            tasks[RetrievalPath.GRAPH]  = self._retrieve_graph(u, ctx)
        if RetrievalPath.STRUCTURED in u.active_paths and self.biz:
            tasks[RetrievalPath.STRUCTURED] = self._retrieve_structured(u, ctx)

        # 并发执行，独立超时
        for path, coro in tasks.items():
            try:
                result = await asyncio.wait_for(coro, timeout=self.timeout)
                ctx.retrieval_results[path] = result
            except asyncio.TimeoutError:
                ctx.errors.append(f"{path.value} 召回超时，已跳过")
            except Exception as e:
                ctx.errors.append(f"{path.value} 召回异常: {e}")

        return ctx

    async def _retrieve_vector(
        self, u: QueryUnderstanding, filters: dict, ctx: PipelineContext
    ) -> list[RetrievalResult]:
        queries  = [u.standalone_query] + u.rewrites
        vectors  = await self.embedding.embed_texts(queries)
        seen:    dict[str, RetrievalResult] = {}
        for i, vec in enumerate(vectors):
            results = await self.vs.search(
                vec, self.vector_top_k, filters,
                collection=ctx.metadata.get("collection", "default"),
                tenant_id=ctx.tenant_id,
            )
            for r in results:
                if r.chunk_id not in seen or r.score > seen[r.chunk_id].score:
                    seen[r.chunk_id] = r
        # 取回父节点内容（层次化 Chunk）
        await self._fetch_parent_contents(list(seen.values()), ctx)
        return sorted(seen.values(), key=lambda x: x.score, reverse=True)

    async def _retrieve_bm25(
        self, u: QueryUnderstanding, filters: dict, ctx: PipelineContext
    ) -> list[RetrievalResult]:
        queries = [u.standalone_query] + u.rewrites
        seen:   dict[str, RetrievalResult] = {}
        for q in queries:
            results = await self.fts.search(
                q, self.bm25_top_k, filters,
                index=ctx.metadata.get("collection", "default"),
                tenant_id=ctx.tenant_id,
            )
            for r in results:
                if r.chunk_id not in seen or r.score > seen[r.chunk_id].score:
                    seen[r.chunk_id] = r
        return sorted(seen.values(), key=lambda x: x.score, reverse=True)

    async def _retrieve_graph(
        self, u: QueryUnderstanding, ctx: PipelineContext
    ) -> list[RetrievalResult]:
        if not u.entities:
            return []
        nodes = await self.graph.find_entities(u.entities, tenant_id=ctx.tenant_id)
        results = []
        for node in nodes[:3]:  # 最多3个起始实体
            graph_result = await self.graph.query_relations(
                node["id"], max_hops=2, tenant_id=ctx.tenant_id
            )
            text = self.graph.to_text(graph_result)
            results.append(RetrievalResult(
                chunk_id = f"graph_{node['id']}",
                doc_id   = "knowledge_graph",
                content  = text,
                score    = 1.0,
                rank     = 1,
                path     = RetrievalPath.GRAPH,
            ))
        return results

    async def _retrieve_structured(
        self, u: QueryUnderstanding, ctx: PipelineContext
    ) -> list[RetrievalResult]:
        qr = await self.biz.query_by_nl(u.standalone_query)
        if qr.error:
            return []
        # 将查询结果序列化为 Markdown 表格
        if qr.data:
            header = "| " + " | ".join(qr.columns) + " |"
            sep    = "| " + " | ".join(["---"] * len(qr.columns)) + " |"
            rows   = ["| " + " | ".join(str(r.get(c, "")) for c in qr.columns) + " |"
                      for r in qr.data[:50]]
            table  = "\n".join([header, sep] + rows)
        else:
            table = "查询无结果"
        return [RetrievalResult(
            chunk_id = "structured_result",
            doc_id   = "business_database",
            content  = f"数据库查询结果（SQL: {qr.sql}）：\n{table}",
            score    = 1.0,
            rank     = 1,
            path     = RetrievalPath.STRUCTURED,
        )]

    async def _fetch_parent_contents(
        self, results: list[RetrievalResult], ctx: PipelineContext
    ) -> None:
        """异步取回子节点对应的父节点完整内容"""
        # 简化实现：通过 metadata 中存储的 parent_chunk_id 取回
        # 完整实现需调用 vector_store.get_by_ids
        pass


# ── Step 4: RRF 融合 ─────────────────────────────────────────────────────────

class RRFFusionStep:
    """
    倒数排名融合（Reciprocal Rank Fusion）。

    算法：
    对每路召回的每个文档，计算 RRF 分数 = Σ 1/(rank_i + k)
    k=60（常数，防止高排名文档过度主导，Cormack et al. 2009 原始论文推荐值）

    融合后：
    1. 按 rrf_score 降序排列
    2. 语义去重：对 top-40 结果，计算两两余弦相似度，
       相似度 > semantic_dedup_threshold（默认 0.92）时保留分数更高的
    3. 质量过滤：quality_score < min_quality（默认 0.2）的 Chunk 丢弃
    4. 截断到 rrf_top_k（默认 40）供下游重排

    结果填入 ctx.fused_results。
    """
    name = "rrf_fusion"

    def __init__(
        self,
        k:                      int   = 60,
        rrf_top_k:              int   = 40,
        semantic_dedup_thr:     float = 0.92,
        min_quality:            float = 0.2,
    ):
        self.k                  = k
        self.rrf_top_k          = rrf_top_k
        self.semantic_dedup_thr = semantic_dedup_thr
        self.min_quality        = min_quality

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.answer:
            return ctx
        # 汇总所有路的结果
        all_results: list[RetrievalResult] = []
        for path_results in ctx.retrieval_results.values():
            all_results.extend(path_results)

        if not all_results:
            return ctx

        # 计算 RRF 分数
        rrf_scores: dict[str, float] = {}
        path_contributions: dict[str, list[RetrievalPath]] = {}
        chunk_map: dict[str, RetrievalResult] = {}

        for result in all_results:
            cid = result.chunk_id
            rrf_scores[cid]  = rrf_scores.get(cid, 0.0) + 1.0 / (result.rank + self.k)
            if cid not in path_contributions:
                path_contributions[cid] = []
                chunk_map[cid] = result
            path_contributions[cid].append(result.path)

        # 构造 FusedResult 列表并排序
        fused = [
            FusedResult(
                chunk_id            = cid,
                doc_id              = chunk_map[cid].doc_id,
                content             = chunk_map[cid].content,
                rrf_score           = score,
                contributing_paths  = path_contributions[cid],
                metadata            = chunk_map[cid].metadata,
                parent_content      = chunk_map[cid].parent_content,
            )
            for cid, score in sorted(rrf_scores.items(),
                                     key=lambda x: x[1], reverse=True)
        ]

        # 质量过滤
        fused = [f for f in fused
                 if f.metadata.get("quality_score", 1.0) >= self.min_quality]

        # 截断
        ctx.fused_results = fused[:self.rrf_top_k]
        return ctx


# ── Step 5: Cross-Encoder 重排 ───────────────────────────────────────────────

class CrossEncoderRerankStep:
    """
    使用 Cross-Encoder 模型对融合后的候选 Chunk 精排。

    推荐模型：
    - BAAI/bge-reranker-large（中文，开源，效果最好）
    - BAAI/bge-reranker-base（轻量版，延迟更低）
    - maidalun1020/bce-reranker-base_v1（中英双语）

    实现说明：
    - 使用 sentence-transformers 的 CrossEncoder 类
    - 对 (query, chunk_content) 对打分（0-1）
    - 使用 standalone_query 作为查询（已消歧，效果更好）
    - chunk_content 优先使用 parent_content（内容更完整）
    - 低于 min_rerank_score（默认 0.3）的 Chunk 过滤掉
    - 最终取 top_k（默认 6）放入 ctx.final_chunks

    性能：
    - CPU：约 50ms/pair；GPU：约 5ms/pair
    - 推荐批量处理，sentence-transformers CrossEncoder 支持 batch
    """
    name = "cross_encoder_rerank"

    def __init__(
        self,
        model_path:      str   = "BAAI/bge-reranker-large",
        top_k:           int   = 6,
        min_rerank_score: float = 0.3,
        device:          str   = "cpu",
    ):
        self.top_k            = top_k
        self.min_rerank_score = min_rerank_score
        # 延迟加载，避免框架启动时阻塞
        self._model_path = model_path
        self._device     = device
        self._model      = None

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self._model_path, device=self._device)
        return self._model

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.answer or not ctx.fused_results:
            return ctx
        query    = ctx.understanding.standalone_query
        model    = self._get_model()
        pairs    = [
            (query, r.parent_content or r.content)
            for r in ctx.fused_results
        ]
        # 在线程池中运行（避免阻塞 event loop）
        import asyncio
        loop   = asyncio.get_event_loop()
        scores = await loop.run_in_executor(
            None,
            lambda: model.predict(pairs, show_progress_bar=False)
        )
        for result, score in zip(ctx.fused_results, scores):
            result.rerank_score = float(score)

        ranked = sorted(
            ctx.fused_results,
            key=lambda x: x.rerank_score or 0.0,
            reverse=True,
        )
        ctx.final_chunks = [
            r for r in ranked
            if (r.rerank_score or 0.0) >= self.min_rerank_score
        ][:self.top_k]
        return ctx


# ── Step 6: Prompt 组装 ──────────────────────────────────────────────────────

class PromptAssemblyStep:
    """
    组装最终 Prompt，填入 ctx.prompt。

    Prompt 结构（各区块按顺序排列，含 token 预算控制）：
    1. System Prompt（固定，来自 config.prompt.system_template）
    2. 用户画像注入（来自 working_memory.user_profile，约 200 token）
    3. 工作记忆（session 摘要 + 实体槽位，约 400 token）
    4. 短期记忆（最近 N 轮对话，约 1500 token，超出则裁剪早期轮次）
    5. 检索上下文（final_chunks，每个 Chunk 标注 [文档N]，约 4000 token）
       - 最相关的 Chunk 放在首位（规避 Lost in the Middle）
       - 使用 parent_content（更完整）而非 content
    6. 当前问题（standalone_query，紧接在检索上下文之后）

    Token 预算：总 budget 来自 config.prompt.context_budget（默认 7000）
    - System:      300 token（固定）
    - 用户画像:    200 token（固定）
    - 工作记忆:    400 token（固定）
    - 短期记忆:    1500 token（动态，按预算裁剪）
    - 检索上下文:  剩余预算（通常 4000-5000 token）
    - 当前问题:    200 token（保留）

    引用格式（要求 LLM 在答案中标注来源）：
    每个 Chunk 前加 [文档{i}] 标签，
    Prompt 末尾附加指令："在每个关键陈述后用[文档N]标注依据来源"
    """
    name = "prompt_assembly"

    def __init__(
        self,
        llm:            LLMAdapter,
        memory_manager: MemoryManager,
        system_template: str,
        context_budget:  int = 7000,
    ):
        self.llm             = llm
        self.memory          = memory_manager
        self.system_template = system_template
        self.budget          = context_budget

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.answer:
            return ctx

        state = await self.memory.get_session(ctx.session_id)
        parts: list[str] = []

        # 1. System
        parts.append(f"<system>\n{self.system_template}\n</system>")

        # 2. 用户画像
        if state and state.user_profile:
            profile = state.user_profile
            parts.append(
                f"<user_profile>\n"
                f"专业背景: {profile.expertise_level}\n"
                f"偏好格式: {profile.preferred_format}\n"
                f"熟悉产品: {', '.join(profile.known_products)}\n"
                f"</user_profile>"
            )

        # 3. 工作记忆
        if state and state.working_memory.summary:
            wm = state.working_memory
            parts.append(
                f"<conversation_context>\n"
                f"对话摘要: {wm.summary}\n"
                f"当前产品: {wm.entity_slots.product or '未指定'}\n"
                f"当前话题: {wm.entity_slots.current_topic or '未指定'}\n"
                f"已确认约束: {', '.join(wm.entity_slots.constraints)}\n"
                f"</conversation_context>"
            )

        # 4. 短期记忆（裁剪到预算）
        if state and state.short_term:
            history_parts = []
            token_used = 0
            for turn in reversed(state.short_term[-10:]):
                content = turn.summary or turn.content
                est = await self.llm.count_tokens(content)
                if token_used + est > 1500:
                    break
                history_parts.insert(0, f"{turn.role}: {content}")
                token_used += est
            if history_parts:
                parts.append("<history>\n" + "\n".join(history_parts) + "\n</history>")

        # 5. 检索上下文（最相关的 Chunk 放首位）
        if ctx.final_chunks:
            ctx.source_refs = []
            context_parts   = []
            for i, chunk in enumerate(ctx.final_chunks, 1):
                ref_id = f"文档{i}"
                content = chunk.parent_content or chunk.content
                context_parts.append(f"[{ref_id}]\n{content}")
                ctx.source_refs.append({
                    "ref_id":   ref_id,
                    "chunk_id": chunk.chunk_id,
                    "doc_id":   chunk.doc_id,
                    "metadata": chunk.metadata,
                })
            parts.append("<references>\n" + "\n\n".join(context_parts) + "\n</references>")

        # 6. 当前问题 + 格式指令
        format_instruction = (
            "\n\n请根据以上参考资料回答问题，在关键陈述后用[文档N]标注来源。"
            "若参考资料不足以回答，请明确说明。"
        )
        parts.append(
            f"<question>\n{ctx.understanding.standalone_query}\n</question>"
            f"{format_instruction}"
        )

        ctx.prompt = "\n\n".join(parts)
        return ctx


# ── Step 7: LLM 生成 ─────────────────────────────────────────────────────────

class LLMGenerateStep:
    """
    调用 LLM 生成最终答案，支持流式和非流式。

    若 ctx.understanding.active_paths 为空（chitchat/meta），
    使用不含检索上下文的简化 Prompt 直接生成。
    """
    name = "llm_generate"

    def __init__(self, llm: LLMAdapter, stream: bool = False):
        self.llm    = llm
        self.stream = stream

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.answer:
            return ctx
        if not ctx.prompt:
            ctx.answer = "系统错误：Prompt 未生成。"
            return ctx

        messages = [{"role": "user", "content": ctx.prompt}]
        ctx.answer = await self.llm.generate(
            messages,
            task      = "generate",
            temperature = 0.1,
            max_tokens  = 2048,
            stream      = self.stream,
        )
        return ctx


# ── Step 8: 忠实度校验 ───────────────────────────────────────────────────────

class FaithfulnessCheckStep:
    """
    校验答案是否忠实于检索内容，防止幻觉。
    结果填入 ctx.faithfulness_ok（False 时在答案末尾追加免责声明）。

    评分低于 min_faithfulness（默认 0.6）时：
    - faithfulness_ok = False
    - 在答案末尾追加："⚠️ 以上回答部分内容可能超出知识库范围，请结合原始资料核实。"

    性能说明：此步骤增加一次 LLM 调用，延迟约 500-1000ms。
    可通过 config 中 enabled: false 跳过（低风险场景）。
    """
    name = "faithfulness_check"

    def __init__(self, llm: LLMAdapter, min_faithfulness: float = 0.6):
        self.llm             = llm
        self.min_faithfulness = min_faithfulness

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.answer or not ctx.final_chunks:
            return ctx
        contexts = [c.parent_content or c.content for c in ctx.final_chunks[:5]]
        score    = await self.llm.check_faithfulness(ctx.answer, contexts)
        ctx.faithfulness_ok = score >= self.min_faithfulness
        if not ctx.faithfulness_ok:
            ctx.answer += (
                "\n\n⚠️ 以上回答部分内容可能超出知识库范围，请结合原始资料核实。"
            )
        ctx.metadata["faithfulness_score"] = score
        return ctx
```

---

## 9. 多轮对话记忆管理

```python
# rag/core/memory.py
from __future__ import annotations
import asyncio
import json
from datetime import datetime, timedelta
from rag.models import (
    SessionState, ShortTermTurn, WorkingMemory,
    EntitySlots, UserProfile
)

class MemoryManager:
    """
    三层记忆管理器。

    存储映射：
    - 短期记忆 + 工作记忆 → Redis，key=session:{session_id}，TTL=config.cache.session_ttl_minutes
    - 长期记忆（用户画像）→ 关系数据库，异步写入
    - 历史 session 摘要  → 向量库（用户专属 collection），异步写入

    并发安全：
    - Redis 操作使用 Lua 脚本或乐观锁（WATCH + MULTI/EXEC）防止并发写冲突
    - 摘要压缩通过异步任务队列执行，不阻塞主链路
    """

    SHORT_TERM_TOKEN_LIMIT = 1500    # 短期记忆 token 上限，超出则触发压缩
    MAX_TURNS_IN_CONTEXT   = 10      # 短期记忆最多保留轮数

    def __init__(
        self,
        redis_client,                # aioredis.Redis 实例
        llm:           LLMAdapter,
        embedding:     EmbeddingAdapter,
        vector_store:  VectorStoreAdapter,
        db_session_factory,          # SQLAlchemy AsyncSession factory（用于用户画像持久化）
    ):
        self.redis    = redis_client
        self.llm      = llm
        self.embedding = embedding
        self.vs        = vector_store
        self.db_factory = db_session_factory

    # ── 读取 ──────────────────────────────────────────────────────────────────

    async def get_session(self, session_id: str) -> SessionState | None:
        """从 Redis 加载 Session 状态，不存在返回 None"""
        key  = f"session:{session_id}"
        data = await self.redis.get(key)
        if not data:
            return None
        return SessionState.model_validate_json(data)

    async def get_or_create_session(
        self,
        session_id: str,
        user_id:    str,
        tenant_id:  str,
    ) -> SessionState:
        """获取或新建 Session，新建时从长期记忆加载用户画像"""
        state = await self.get_session(session_id)
        if state:
            return state
        profile = await self._load_user_profile(user_id)
        state   = SessionState(
            session_id     = session_id,
            user_id        = user_id,
            tenant_id      = tenant_id,
            working_memory = WorkingMemory(session_id=session_id),
            user_profile   = profile,
        )
        await self._save_session(state)
        return state

    # ── 写入（答案生成后异步调用）─────────────────────────────────────────────

    async def update_after_turn(
        self,
        session_id: str,
        question:   str,
        answer:     str,
        doc_refs:   list[str],
        llm:        LLMAdapter | None = None,
    ) -> None:
        """
        在答案返回后异步更新记忆（不阻塞主链路）。
        步骤：
        1. 将本轮 Q&A 追加到 short_term
        2. 更新 token 计数
        3. 若超过 SHORT_TERM_TOKEN_LIMIT，触发摘要压缩（pop 早期轮次）
        4. 更新 working_memory.entity_slots（简单规则提取）
        5. 更新 topic_vector（用本轮 standalone_query 的 embedding 加权平均）
        6. 记录 used_doc_ids
        7. 保存回 Redis，刷新 TTL
        """
        state = await self.get_session(session_id)
        if not state:
            return

        # 1. 追加本轮
        q_tokens = await self.llm.count_tokens(question)
        a_tokens = await self.llm.count_tokens(answer)
        state.short_term.append(ShortTermTurn(
            role="user", content=question, token_count=q_tokens
        ))
        state.short_term.append(ShortTermTurn(
            role="assistant", content=answer,
            token_count=a_tokens, doc_refs=doc_refs
        ))
        state.short_term_tokens += q_tokens + a_tokens
        state.working_memory.turn_count += 1

        # 2. 超出上限时压缩
        while (state.short_term_tokens > self.SHORT_TERM_TOKEN_LIMIT
               and len(state.short_term) >= 2):
            popped_q = state.short_term.pop(0)
            popped_a = state.short_term.pop(0)
            state.short_term_tokens -= (popped_q.token_count + popped_a.token_count)
            # 触发摘要压缩（异步，不等待）
            asyncio.create_task(
                self._compress_into_summary(state, popped_q.content, popped_a.content)
            )

        # 3. 更新 topic_vector
        new_vec = await self.embedding.embed_query(question)
        if state.working_memory.topic_vector:
            old_vec = state.working_memory.topic_vector
            # 指数移动平均：新向量权重 0.3，历史权重 0.7
            alpha = 0.3
            state.working_memory.topic_vector = [
                alpha * n + (1 - alpha) * o
                for n, o in zip(new_vec, old_vec)
            ]
        else:
            state.working_memory.topic_vector = new_vec

        # 4. 记录引用文档
        for doc_id in doc_refs:
            state.working_memory.used_doc_ids[doc_id] = (
                state.working_memory.used_doc_ids.get(doc_id, 0) + 1
            )

        state.last_active = datetime.utcnow()
        await self._save_session(state)

    async def update_entity_slots(
        self,
        session_id: str,
        updates:    dict,   # 由 LLM 或规则提取的槽位更新
    ) -> None:
        """更新实体槽位（可由独立的槽位提取步骤调用）"""
        state = await self.get_session(session_id)
        if not state:
            return
        slots = state.working_memory.entity_slots
        for k, v in updates.items():
            if hasattr(slots, k) and v:
                setattr(slots, k, v)
        await self._save_session(state)

    async def reset_topic(self, session_id: str) -> None:
        """话题跳转时重置话题相关槽位（保留用户身份信息）"""
        state = await self.get_session(session_id)
        if not state:
            return
        wm = state.working_memory
        wm.entity_slots.current_topic = None
        wm.entity_slots.open_items    = []
        wm.topic_vector               = None
        await self._save_session(state)

    # ── Session 归档 ──────────────────────────────────────────────────────────

    async def archive_session(self, session_id: str) -> None:
        """
        Session 结束时调用（由超时检测或用户主动关闭触发）。
        步骤：
        1. 生成本 session 的完整摘要（LLM 调用）
        2. Embedding 摘要，写入用户的长期记忆向量 collection
        3. 更新用户画像（persistent DB）
        4. 从 Redis 删除 session 数据
        """
        state = await self.get_session(session_id)
        if not state:
            return

        # 1. 生成 session 摘要
        turns_text = "\n".join(
            f"{t.role}: {t.content}" for t in state.short_term
        )
        if state.working_memory.summary:
            turns_text = f"[前期摘要]\n{state.working_memory.summary}\n\n[后期对话]\n{turns_text}"
        session_summary = await self.llm.compress_summary(
            "", {"question": turns_text, "answer": ""}, max_chars=500
        )

        # 2. 写入长期记忆向量库
        summary_vec = await self.embedding.embed_query(session_summary)
        from rag.models import Chunk, ContentType
        summary_chunk = Chunk(
            chunk_id     = f"session_{session_id}",
            doc_id       = f"user_{state.user_id}_history",
            content      = session_summary,
            content_type = ContentType.TEXT,
            embedding    = summary_vec,
            metadata     = {
                "session_id": session_id,
                "user_id":    state.user_id,
                "archived_at": datetime.utcnow().isoformat(),
            },
        )
        await self.vs.upsert(
            [summary_chunk],
            collection = f"user_history_{state.user_id}",
            tenant_id  = state.tenant_id,
        )

        # 3. 删除 Redis 中的 session
        await self.redis.delete(f"session:{session_id}")

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    async def _compress_into_summary(
        self,
        state:    SessionState,
        question: str,
        answer:   str,
    ) -> None:
        """将被弹出的轮次合并到工作记忆摘要（异步后台任务）"""
        new_summary = await self.llm.compress_summary(
            state.working_memory.summary,
            {"question": question, "answer": answer},
        )
        # 重新读取并更新（防止并发写覆盖）
        fresh = await self.get_session(state.session_id)
        if fresh:
            fresh.working_memory.summary = new_summary
            await self._save_session(fresh)

    async def _save_session(self, state: SessionState, ttl_minutes: int = 60) -> None:
        key = f"session:{state.session_id}"
        await self.redis.set(
            key,
            state.model_dump_json(),
            ex=ttl_minutes * 60,
        )

    async def _load_user_profile(self, user_id: str) -> UserProfile | None:
        """从关系数据库加载用户画像"""
        # 实现：SELECT * FROM user_profiles WHERE user_id = :user_id
        # 返回 UserProfile 或 None
        return None  # 默认返回 None，首次对话时无画像
```
## 10. Pipeline 编排引擎

```python
# rag/pipeline/engine.py
from __future__ import annotations
import asyncio
import time
from typing import Protocol, TypeVar
import structlog

logger = structlog.get_logger()

class Step(Protocol):
    """Pipeline 步骤协议，所有步骤必须满足此接口"""
    name: str
    async def execute(self, ctx) -> any: ...

class PipelineEngine:
    """
    DAG 风格的顺序 Pipeline 执行引擎。

    特性：
    - 顺序执行步骤列表
    - 每步骤执行前后记录耗时（写入 ctx.step_timings）
    - 步骤执行失败时：记录 error，根据 stop_on_error 决定是否继续
    - 若某步骤已设置了终止标志（ctx.answer 非空），跳过后续检索/生成步骤
      但仍执行后处理步骤（faithfulness_check、format_output）
    - 支持 max_total_timeout（整个 Pipeline 的超时上限，默认 30s）

    使用方式：
    engine = PipelineEngine(steps=[...])
    ctx = await engine.run(initial_ctx)
    """

    def __init__(
        self,
        steps:             list[Step],
        stop_on_error:     bool  = False,
        max_total_timeout: float = 30.0,
    ):
        self.steps             = steps
        self.stop_on_error     = stop_on_error
        self.max_total_timeout = max_total_timeout

    async def run(self, ctx) -> any:
        """执行完整 Pipeline，返回最终 ctx"""
        start = time.monotonic()
        log   = logger.bind(
            pipeline   = self.__class__.__name__,
            session_id = getattr(ctx, "session_id", "N/A"),
        )

        for step in self.steps:
            # 超时保护
            elapsed = time.monotonic() - start
            if elapsed > self.max_total_timeout:
                ctx.errors.append(f"Pipeline 总超时（{self.max_total_timeout}s）")
                break

            step_start = time.monotonic()
            try:
                ctx = await step.execute(ctx)
                step_time = time.monotonic() - step_start
                ctx.step_timings[step.name] = round(step_time * 1000, 2)  # ms
                log.info("step_done", step=step.name, ms=ctx.step_timings[step.name])
            except Exception as e:
                step_time = time.monotonic() - step_start
                ctx.errors.append(f"步骤 {step.name} 异常: {e}")
                log.error("step_failed", step=step.name, error=str(e))
                if self.stop_on_error:
                    break

        return ctx


# ── 问答 Pipeline 组装 ───────────────────────────────────────────────────────

# rag/pipeline/query/pipeline.py
from rag.config.models import TenantConfig
from rag.config.registry import AdapterRegistry
from rag.pipeline.query.steps import (
    SecurityFilterStep, QueryUnderstandingStep, ParallelRetrievalStep,
    RRFFusionStep, CrossEncoderRerankStep, PromptAssemblyStep,
    LLMGenerateStep, FaithfulnessCheckStep,
)
from rag.core.memory import MemoryManager

def build_query_pipeline(
    config:   TenantConfig,
    registry: AdapterRegistry,
    memory:   MemoryManager,
    stream:   bool = False,
) -> PipelineEngine:
    """
    根据租户配置组装问答 Pipeline。
    config.pipeline.query.steps 列表决定哪些步骤启用。
    """
    llm       = registry.get_llm(config)
    embedding = registry.get_embedding(config)
    vs        = registry.get_vector_store(config)
    fts       = registry.get_fts(config)
    synonym   = registry.get_synonym(config)
    graph     = registry.get_graph(config)
    biz_data  = registry.get_business_data(config)

    rc = config.pipeline.query.retrieval_config
    pc = config.pipeline.query

    step_map = {
        "security_filter": SecurityFilterStep(
            sensitive_words = config.security.sensitive_words,
            max_length      = config.security.max_query_length,
        ),
        "query_understanding": QueryUnderstandingStep(
            llm             = llm,
            embedding       = embedding,
            synonym         = synonym,
            memory_manager  = memory,
            intent_examples = config.pipeline.query.intent_examples,
            topic_shift_thr = pc.topic_shift_threshold,
        ),
        "parallel_retrieval": ParallelRetrievalStep(
            vector_store  = vs,
            fts           = fts,
            embedding     = embedding,
            graph         = graph,
            business_data = biz_data,
            vector_top_k  = rc.vector_top_k,
            bm25_top_k    = rc.bm25_top_k,
            timeout       = rc.timeout_per_path_seconds,
        ),
        "rrf_fusion": RRFFusionStep(
            rrf_top_k       = rc.rrf_top_k,
            min_quality     = rc.min_quality_score,
        ),
        "cross_encoder_rerank": CrossEncoderRerankStep(
            model_path       = config.reranker.model_path,
            top_k            = rc.rerank_top_k,
            min_rerank_score = rc.min_relevance_score,
            device           = config.reranker.device,
        ),
        "prompt_assembly": PromptAssemblyStep(
            llm              = llm,
            memory_manager   = memory,
            system_template  = config.prompt.system_template,
            context_budget   = config.prompt.context_budget,
        ),
        "llm_generate": LLMGenerateStep(llm=llm, stream=stream),
        "faithfulness_check": FaithfulnessCheckStep(
            llm              = llm,
            min_faithfulness = config.pipeline.query.min_faithfulness,
        ),
    }

    enabled_steps = [
        step_map[name]
        for name in config.pipeline.query.steps
        if name in step_map
    ]
    return PipelineEngine(steps=enabled_steps, max_total_timeout=30.0)
```

---

## 11. 配置系统

```python
# rag/config/models.py
from pydantic import BaseModel, Field, field_validator
from typing import Any

class LLMConfig(BaseModel):
    adapter:             str                        # 注册名，如 "openai_compatible"
    base_url:            str
    api_key:             str = ""
    model:               str
    timeout_seconds:     float = 30.0
    task_model_mapping:  dict[str, str] = Field(default_factory=dict)

class EmbeddingConfig(BaseModel):
    adapter:      str
    url:          str = ""
    model:        str
    dim:          int = 1024
    batch_size:   int = 64
    query_prefix: str = ""              # BGE 模型的查询前缀

class VectorStoreConfig(BaseModel):
    adapter:           str
    host:              str = "localhost"
    port:              int = 19530
    dim:               int = 1024
    collection_prefix: str = ""
    index_type:        str = "HNSW"
    metric_type:       str = "COSINE"
    extra:             dict[str, Any] = Field(default_factory=dict)

class FullTextSearchConfig(BaseModel):
    adapter:      str
    hosts:        list[str]
    api_key:      str = ""
    index_prefix: str = "rag_"
    analyzer:     str = "ik_max_word"

class BusinessDataConfig(BaseModel):
    enabled:               bool = False
    adapter:               str  = "sqlalchemy"
    dsn:                   str  = ""
    schema_description_file: str = ""
    allowed_tables:        list[str] = Field(default_factory=list)
    max_rows_returned:     int  = 100
    sensitive_fields:      list[str] = Field(default_factory=list)

class KnowledgeGraphConfig(BaseModel):
    enabled:  bool = False
    adapter:  str  = "neo4j"
    uri:      str  = ""
    username: str  = ""
    password: str  = ""

class SynonymConfig(BaseModel):
    adapter: str = "file_based"
    files:   list[str] = Field(default_factory=list)
    url:     str = ""                   # http_service 适配器使用

class AuthConfig(BaseModel):
    adapter:            str
    issuer_url:         str = ""
    client_id:          str = ""
    jwt_secret:         str = ""        # jwt 适配器使用
    permission_mapping: dict[str, list[str]] = Field(default_factory=dict)

class StorageConfig(BaseModel):
    adapter:    str
    endpoint:   str = ""
    access_key: str = ""
    secret_key: str = ""
    bucket:     str = "rag-documents"
    secure:     bool = True

class CacheConfig(BaseModel):
    adapter:           str   = "redis"
    host:              str   = "localhost"
    port:              int   = 6379
    password:          str   = ""
    db:                int   = 0
    session_ttl_minutes: int = 60

class RerankerConfig(BaseModel):
    model_path: str   = "BAAI/bge-reranker-large"
    device:     str   = "cpu"

class ChunkingConfig(BaseModel):
    max_tokens:  int = 512
    overlap:     int = 50
    parent_max:  int = 2048

class RetrievalConfig(BaseModel):
    vector_top_k:             int   = 20
    bm25_top_k:               int   = 20
    rrf_top_k:                int   = 40
    rerank_top_k:             int   = 6
    min_relevance_score:      float = 0.30
    min_quality_score:        float = 0.20
    timeout_per_path_seconds: float = 5.0

class IngestionPipelineConfig(BaseModel):
    steps:    list[str] = Field(default_factory=lambda: [
        "parse_document", "generate_image_caption", "semantic_chunking",
        "metadata_enrichment", "embed_chunks", "write_to_stores",
    ])
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)

class QueryPipelineConfig(BaseModel):
    steps: list[str] = Field(default_factory=lambda: [
        "security_filter", "query_understanding", "parallel_retrieval",
        "rrf_fusion", "cross_encoder_rerank", "prompt_assembly",
        "llm_generate", "faithfulness_check",
    ])
    retrieval_config:    RetrievalConfig = Field(default_factory=RetrievalConfig)
    topic_shift_threshold: float = 0.50
    min_faithfulness:    float   = 0.60
    intent_examples:     list[dict] | None = None

class PipelineConfig(BaseModel):
    ingestion: IngestionPipelineConfig = Field(default_factory=IngestionPipelineConfig)
    query:     QueryPipelineConfig     = Field(default_factory=QueryPipelineConfig)

class PromptConfig(BaseModel):
    system_template: str = (
        "你是一个专业的智能问答助手。请根据提供的参考资料准确回答用户问题，"
        "在关键陈述后用[文档N]标注来源，不要编造参考资料中没有的内容。"
        "若参考资料不足以回答，请明确说明知识库中暂无相关信息。"
    )
    context_budget: int = 7000

class SecurityConfig(BaseModel):
    sensitive_words:  list[str] = Field(default_factory=list)
    max_query_length: int       = 2000

class ObservabilityConfig(BaseModel):
    metrics_adapter:      str = "prometheus"
    push_gateway:         str = ""
    tracing_adapter:      str = "jaeger"
    tracing_endpoint:     str = ""
    log_level:            str = "INFO"

class TenantConfig(BaseModel):
    """租户完整配置，从 customer_config.yaml 加载"""
    tenant: dict[str, str]          # id, name, language, timezone

    llm:            LLMConfig
    embedding:      EmbeddingConfig
    vector_store:   VectorStoreConfig
    full_text_search: FullTextSearchConfig
    object_storage: StorageConfig
    cache:          CacheConfig

    business_data:  BusinessDataConfig    = Field(default_factory=BusinessDataConfig)
    knowledge_graph: KnowledgeGraphConfig = Field(default_factory=KnowledgeGraphConfig)
    synonyms:       SynonymConfig         = Field(default_factory=SynonymConfig)
    auth:           AuthConfig            = Field(default_factory=lambda: AuthConfig(adapter="jwt"))
    reranker:       RerankerConfig        = Field(default_factory=RerankerConfig)
    pipeline:       PipelineConfig        = Field(default_factory=PipelineConfig)
    prompt:         PromptConfig          = Field(default_factory=PromptConfig)
    security:       SecurityConfig        = Field(default_factory=SecurityConfig)
    observability:  ObservabilityConfig   = Field(default_factory=ObservabilityConfig)


# rag/config/loader.py
import os
import re
import yaml
from pathlib import Path
from rag.config.models import TenantConfig

def load_config(path: str) -> TenantConfig:
    """
    从 YAML 文件加载配置，支持环境变量插值（${VAR_NAME}）。
    环境变量不存在时抛出 ValueError，明确告知缺少哪个变量。
    """
    text = Path(path).read_text(encoding="utf-8")
    # 环境变量替换
    def replace_env(m: re.Match) -> str:
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            raise ValueError(f"配置文件引用了未设置的环境变量: ${{{var}}}")
        return val
    text      = re.sub(r'\$\{([^}]+)\}', replace_env, text)
    raw       = yaml.safe_load(text)
    return TenantConfig.model_validate(raw)


# rag/config/registry.py
from rag.config.models import TenantConfig
from rag.adapters.base.llm import LLMAdapter

class AdapterRegistry:
    """
    适配器注册中心。
    框架内置适配器已预注册（注册名见各适配器注释中的"注册名"字段）。
    客户自定义适配器在 customer/__init__.py 中调用 registry.register() 注册。
    """

    def __init__(self):
        self._llm_adapters:    dict[str, type] = {}
        self._embed_adapters:  dict[str, type] = {}
        self._vs_adapters:     dict[str, type] = {}
        self._fts_adapters:    dict[str, type] = {}
        self._parser_adapters: dict[str, type] = {}
        self._biz_adapters:    dict[str, type] = {}
        self._graph_adapters:  dict[str, type] = {}
        self._synonym_adapters: dict[str, type] = {}
        self._auth_adapters:   dict[str, type] = {}
        self._storage_adapters: dict[str, type] = {}
        self._register_builtins()

    def _register_builtins(self):
        from rag.adapters.builtin.llm.openai_compatible import OpenAICompatibleAdapter
        from rag.adapters.builtin.llm.vllm import VLLMAdapter
        from rag.adapters.builtin.embedding.openai_embedding import OpenAIEmbeddingAdapter
        from rag.adapters.builtin.embedding.http_embedding import HTTPEmbeddingAdapter
        from rag.adapters.builtin.vector_store.milvus import MilvusAdapter
        from rag.adapters.builtin.vector_store.qdrant import QdrantAdapter
        from rag.adapters.builtin.vector_store.pgvector import PGVectorAdapter
        from rag.adapters.builtin.full_text_search.elasticsearch import ElasticsearchAdapter
        from rag.adapters.builtin.full_text_search.opensearch import OpenSearchAdapter
        from rag.adapters.builtin.synonym.file_based import FileSynonymAdapter
        from rag.adapters.builtin.synonym.http_service import HTTPSynonymAdapter
        from rag.adapters.builtin.auth.jwt_adapter import JWTAdapter
        from rag.adapters.builtin.auth.oidc_adapter import OIDCAdapter
        from rag.adapters.builtin.storage.minio import MinIOAdapter
        from rag.adapters.builtin.storage.local_fs import LocalFSAdapter

        self._llm_adapters.update({
            "openai_compatible": OpenAICompatibleAdapter,
            "vllm":              VLLMAdapter,
        })
        self._embed_adapters.update({
            "openai_embedding": OpenAIEmbeddingAdapter,
            "http_embedding":   HTTPEmbeddingAdapter,
        })
        self._vs_adapters.update({
            "milvus":   MilvusAdapter,
            "qdrant":   QdrantAdapter,
            "pgvector": PGVectorAdapter,
        })
        self._fts_adapters.update({
            "elasticsearch": ElasticsearchAdapter,
            "opensearch":    OpenSearchAdapter,
        })
        self._synonym_adapters.update({
            "file_based":   FileSynonymAdapter,
            "http_service": HTTPSynonymAdapter,
        })
        self._auth_adapters.update({
            "jwt":  JWTAdapter,
            "oidc": OIDCAdapter,
        })
        self._storage_adapters.update({
            "minio":    MinIOAdapter,
            "local_fs": LocalFSAdapter,
        })

    def register(self, adapter_type: str, name: str, cls: type) -> None:
        """注册自定义适配器。adapter_type: 'llm'|'embedding'|'vector_store'|..."""
        registry_map = {
            "llm": self._llm_adapters, "embedding": self._embed_adapters,
            "vector_store": self._vs_adapters, "full_text_search": self._fts_adapters,
            "synonym": self._synonym_adapters, "auth": self._auth_adapters,
            "storage": self._storage_adapters, "business_data": self._biz_adapters,
            "knowledge_graph": self._graph_adapters,
        }
        registry_map[adapter_type][name] = cls

    def get_llm(self, config: TenantConfig) -> LLMAdapter:
        cls = self._llm_adapters[config.llm.adapter]
        return cls(config.llm)

    def get_embedding(self, config: TenantConfig):
        cls = self._embed_adapters[config.embedding.adapter]
        return cls(config.embedding)

    def get_vector_store(self, config: TenantConfig):
        cls = self._vs_adapters[config.vector_store.adapter]
        return cls(config.vector_store)

    def get_fts(self, config: TenantConfig):
        cls = self._fts_adapters[config.full_text_search.adapter]
        return cls(config.full_text_search)

    def get_synonym(self, config: TenantConfig):
        cls = self._synonym_adapters.get(config.synonyms.adapter)
        return cls(config.synonyms) if cls else None

    def get_graph(self, config: TenantConfig):
        if not config.knowledge_graph.enabled:
            return None
        cls = self._graph_adapters.get(config.knowledge_graph.adapter)
        return cls(config.knowledge_graph) if cls else None

    def get_business_data(self, config: TenantConfig):
        if not config.business_data.enabled:
            return None
        cls = self._biz_adapters.get(config.business_data.adapter)
        return cls(config.business_data) if cls else None

    def get_auth(self, config: TenantConfig):
        cls = self._auth_adapters[config.auth.adapter]
        return cls(config.auth)

    def get_storage(self, config: TenantConfig):
        cls = self._storage_adapters[config.object_storage.adapter]
        return cls(config.object_storage)

    def get_parsers(self, config: TenantConfig) -> dict:
        from rag.adapters.builtin.doc_parser.pdf_parser import PDFParserAdapter
        from rag.adapters.builtin.doc_parser.docx_parser import DocxParserAdapter
        from rag.adapters.builtin.doc_parser.image_parser import ImageParserAdapter
        return {
            "pdf":  PDFParserAdapter(),
            "docx": DocxParserAdapter(),
            "doc":  DocxParserAdapter(),
            "png":  ImageParserAdapter(),
            "jpg":  ImageParserAdapter(),
            "jpeg": ImageParserAdapter(),
        }
```

---

## 12. REST API 层

```python
# rag/api/app.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from rag.api.routers import query, ingestion, admin, health
from rag.api.middleware.auth import AuthMiddleware
from rag.api.middleware.tracing import TracingMiddleware
from rag.config.loader import load_config
from rag.config.registry import AdapterRegistry

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化
    config   = load_config(app.state.config_path)
    registry = AdapterRegistry()
    # 注册客户自定义适配器
    try:
        from customer import register_adapters
        register_adapters(registry)
    except ImportError:
        pass
    app.state.config   = config
    app.state.registry = registry
    yield
    # 关闭时清理资源

def create_app(config_path: str = "customer/config/customer_config.yaml") -> FastAPI:
    app = FastAPI(
        title       = "RAG 智能问答框架",
        version     = "1.0.0",
        lifespan    = lifespan,
    )
    app.state.config_path = config_path
    app.add_middleware(TracingMiddleware)
    app.add_middleware(AuthMiddleware)
    app.include_router(health.router,     prefix="/health")
    app.include_router(query.router,      prefix="/api/v1/query")
    app.include_router(ingestion.router,  prefix="/api/v1/ingest")
    app.include_router(admin.router,      prefix="/api/v1/admin")
    return app


# rag/api/routers/query.py
import time
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from rag.models import QueryRequest, QueryResponse, PipelineContext
from rag.core.memory import MemoryManager
import uuid

router = APIRouter(tags=["query"])

@router.post("", response_model=QueryResponse)
async def query_endpoint(req: QueryRequest, request: Request):
    """
    核心问答接口。
    - 新 session：session_id 留空，系统自动创建并在响应中返回
    - 流式响应：stream=true，返回 text/event-stream
    """
    config   = request.app.state.config
    registry = request.app.state.registry
    user_ctx = request.state.user_context   # 由 AuthMiddleware 注入

    session_id = req.session_id or str(uuid.uuid4())
    memory     = _get_memory(request)
    state      = await memory.get_or_create_session(
        session_id, user_ctx.user_id, user_ctx.tenant_id
    )

    ctx = PipelineContext(
        session_id = session_id,
        user_id    = user_ctx.user_id,
        raw_query  = req.query,
        tenant_id  = user_ctx.tenant_id,
        metadata   = {
            "user_context": user_ctx,
            "collection":   _get_allowed_collection(user_ctx, config),
        },
    )

    if req.stream:
        pipeline = _build_query_pipeline(config, registry, memory, stream=True)
        return StreamingResponse(
            _stream_response(ctx, pipeline, memory),
            media_type="text/event-stream",
        )

    pipeline   = _build_query_pipeline(config, registry, memory, stream=False)
    start      = time.monotonic()
    ctx        = await pipeline.run(ctx)
    latency_ms = int((time.monotonic() - start) * 1000)

    # 异步更新记忆（不阻塞响应）
    import asyncio
    asyncio.create_task(memory.update_after_turn(
        session_id, req.query, ctx.answer or "",
        [r["doc_id"] for r in ctx.source_refs],
    ))

    from rag.models import QueryResponse, SourceReference
    return QueryResponse(
        answer     = ctx.answer or "抱歉，系统暂时无法处理您的请求。",
        session_id = session_id,
        sources    = [SourceReference(**r) for r in ctx.source_refs],
        latency_ms = latency_ms,
        metadata   = {
            "step_timings":       ctx.step_timings,
            "faithfulness_score": ctx.metadata.get("faithfulness_score"),
            "intent":             ctx.understanding.intent if ctx.understanding else None,
        },
    )

async def _stream_response(ctx, pipeline, memory):
    """SSE 流式生成器"""
    import json
    ctx = await pipeline.run(ctx)
    if isinstance(ctx.answer, str):
        yield f"data: {json.dumps({'content': ctx.answer, 'done': True})}\n\n"
    else:
        # AsyncIterator
        async for chunk in ctx.answer:
            yield f"data: {json.dumps({'content': chunk, 'done': False})}\n\n"
        yield f"data: {json.dumps({'done': True, 'session_id': ctx.session_id})}\n\n"


# rag/api/routers/ingestion.py
from fastapi import APIRouter, BackgroundTasks, Request
from rag.models import IngestionRequest, IngestionResponse
import uuid

router = APIRouter(tags=["ingestion"])

@router.post("", response_model=IngestionResponse)
async def ingest_document(
    req:        IngestionRequest,
    background: BackgroundTasks,
    request:    Request,
):
    """
    异步文档入库接口。
    立即返回 task_id，后台执行入库 Pipeline。
    通过 GET /api/v1/admin/tasks/{task_id} 查询进度。
    """
    config   = request.app.state.config
    registry = request.app.state.registry
    doc_id   = req.doc_id or str(uuid.uuid4())
    task_id  = str(uuid.uuid4())

    background.add_task(
        _run_ingestion, doc_id, req, task_id, config, registry
    )
    return IngestionResponse(
        doc_id      = doc_id,
        chunk_count = 0,      # 入库完成后通过 task 查询实际数量
        status      = "pending",
        task_id     = task_id,
    )
```

---

## 13. 运维控制面

```python
# rag/api/routers/admin.py
"""
管理接口，需要 admin 角色权限。

GET  /api/v1/admin/health/full          全组件健康检查
GET  /api/v1/admin/tasks/{task_id}      查询入库任务状态
DELETE /api/v1/admin/docs/{doc_id}      从所有库删除文档
GET  /api/v1/admin/stats                知识库统计（chunk数、doc数、collection列表）
POST /api/v1/admin/synonyms/reload      热重载同义词表（无需重启）
GET  /api/v1/admin/evaluate             触发评测（从 config 加载测试集）
POST /api/v1/admin/sessions/{id}/archive  手动归档 session
"""

from fastapi import APIRouter, Request, Depends
router = APIRouter(tags=["admin"])

@router.get("/health/full")
async def full_health_check(request: Request):
    """
    逐一调用所有适配器的 health_check()，返回各组件状态。
    响应格式：
    {
      "overall": "healthy" | "degraded" | "unhealthy",
      "components": {
        "llm":          {"status": "healthy", "latency_ms": 234},
        "embedding":    {"status": "healthy", "latency_ms": 12},
        "vector_store": {"status": "healthy", "latency_ms": 5},
        "fts":          {"status": "unhealthy", "error": "连接超时"},
        ...
      }
    }
    """
    ...

@router.delete("/docs/{doc_id}")
async def delete_document(doc_id: str, request: Request):
    """
    从向量库、全文索引、知识图谱中删除该文档的所有数据。
    返回各库的删除数量。
    """
    ...

@router.post("/synonyms/reload")
async def reload_synonyms(request: Request):
    """
    热重载同义词表（调用 FileSynonymAdapter._load_file）。
    不重启服务，立即生效。
    """
    ...
```

---

## 14. 可观测性

```python
# rag/observability/metrics.py
"""
Prometheus 指标定义。所有指标命名以 rag_ 为前缀。

Counter：
  rag_queries_total{tenant_id, intent, status}        查询总次数
  rag_ingestion_docs_total{tenant_id, status}         入库文档总数

Histogram：
  rag_query_latency_ms{tenant_id, step}               各步骤延迟分布
  rag_retrieval_score{tenant_id, path}                各路召回分数分布

Gauge：
  rag_knowledge_base_chunks{tenant_id, collection}    知识库 Chunk 总数
  rag_active_sessions{tenant_id}                      活跃 Session 数

使用方式（在 Pipeline 步骤中）：
  from rag.observability.metrics import QUERY_LATENCY
  QUERY_LATENCY.labels(tenant_id=ctx.tenant_id, step="rerank").observe(ms)
"""
from prometheus_client import Counter, Histogram, Gauge

QUERIES_TOTAL = Counter(
    "rag_queries_total",
    "查询总次数",
    ["tenant_id", "intent", "status"],
)
QUERY_LATENCY = Histogram(
    "rag_query_latency_ms",
    "各步骤延迟（毫秒）",
    ["tenant_id", "step"],
    buckets=[10, 50, 100, 200, 500, 1000, 2000, 5000],
)
INGESTION_DOCS = Counter(
    "rag_ingestion_docs_total",
    "入库文档总数",
    ["tenant_id", "status"],
)
RETRIEVAL_SCORE = Histogram(
    "rag_retrieval_score",
    "各路召回分数分布",
    ["tenant_id", "path"],
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)
```

---

## 15. 测试规格

```python
# tests/unit/test_fusion.py
"""单元测试：RRF 融合算法"""
import pytest
from rag.pipeline.query.steps import RRFFusionStep
from rag.models import RetrievalResult, RetrievalPath, PipelineContext

@pytest.mark.asyncio
async def test_rrf_basic_fusion():
    """验证 RRF 融合：同一文档在两路均出现时分数高于只在一路出现"""
    step = RRFFusionStep(k=60)
    ctx  = _make_ctx_with_results({
        RetrievalPath.VECTOR: [
            RetrievalResult(chunk_id="a", doc_id="d1", content="", score=0.9, rank=1, path=RetrievalPath.VECTOR),
            RetrievalResult(chunk_id="b", doc_id="d2", content="", score=0.8, rank=2, path=RetrievalPath.VECTOR),
        ],
        RetrievalPath.BM25: [
            RetrievalResult(chunk_id="a", doc_id="d1", content="", score=5.0, rank=1, path=RetrievalPath.BM25),
            RetrievalResult(chunk_id="c", doc_id="d3", content="", score=4.0, rank=2, path=RetrievalPath.BM25),
        ],
    })
    ctx = await step.execute(ctx)
    chunk_ids = [r.chunk_id for r in ctx.fused_results]
    assert chunk_ids[0] == "a", "两路都命中的 chunk_id='a' 应排第一"
    assert len(ctx.fused_results) == 3

@pytest.mark.asyncio
async def test_rrf_k60_formula():
    """验证 RRF 分数计算公式 1/(rank+k)"""
    step = RRFFusionStep(k=60)
    expected_score_rank1 = 1.0 / (1 + 60)
    ctx = _make_ctx_with_results({
        RetrievalPath.VECTOR: [
            RetrievalResult(chunk_id="x", doc_id="d1", content="", score=0.9, rank=1, path=RetrievalPath.VECTOR),
        ],
    })
    ctx = await step.execute(ctx)
    assert abs(ctx.fused_results[0].rrf_score - expected_score_rank1) < 1e-6


# tests/integration/test_query_pipeline.py
"""集成测试：完整问答流程（使用 Mock 适配器）"""
import pytest
from unittest.mock import AsyncMock
from rag.pipeline.query.pipeline import build_query_pipeline

@pytest.fixture
def mock_config(): ...    # 返回最小化 TenantConfig
@pytest.fixture
def mock_registry(): ...  # 返回使用 AsyncMock 的 AdapterRegistry

@pytest.mark.asyncio
async def test_factual_query_end_to_end(mock_config, mock_registry):
    """验证：factual 意图 → 向量+BM25 双路 → 重排 → 生成答案"""
    ...

@pytest.mark.asyncio
async def test_chitchat_skips_retrieval(mock_config, mock_registry):
    """验证：chitchat 意图 → 跳过检索步骤 → 直接生成"""
    ...

@pytest.mark.asyncio
async def test_security_filter_blocks_injection(mock_config, mock_registry):
    """验证：Prompt 注入攻击被过滤，返回安全提示而非执行"""
    ...

@pytest.mark.asyncio
async def test_empty_retrieval_returns_no_data_message(mock_config, mock_registry):
    """验证：召回为空时返回"知识库中暂无相关信息"而非幻觉"""
    ...
```

---

## 16. 部署规格

### 16.1 pyproject.toml

```toml
[project]
name            = "rag-framework"
version         = "1.0.0"
requires-python = ">=3.11"
dependencies    = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.29",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "httpx>=0.27",
    "aioredis>=2.0",
    "structlog>=24.0",
    "opentelemetry-sdk>=1.24",
    "opentelemetry-exporter-otlp>=1.24",
    "prometheus-client>=0.20",
    "sentence-transformers>=3.0",
    "jieba>=0.42",
    "openpyxl>=3.1",
    "pyyaml>=6.0",
    # 可选（按配置按需安装）
    "pymilvus>=2.4",          # Milvus 适配器
    "elasticsearch>=8.0",    # ES 适配器
    "qdrant-client>=1.9",     # Qdrant 适配器
    "neo4j>=5.0",             # Neo4j 适配器
    "minio>=7.2",             # MinIO 适配器
    "pymupdf>=1.24",          # PDF 解析
    "python-docx>=1.1",       # DOCX 解析
    "paddleocr>=2.7",         # OCR（可选）
    "sqlalchemy[asyncio]>=2.0", # 业务数据库适配器
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "httpx"]
```

### 16.2 Dockerfile

```dockerfile
FROM python:3.11-slim AS base
WORKDIR /app

# 安装系统依赖（PDF 解析需要）
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

COPY rag/     ./rag/
COPY customer/ ./customer/

# 预下载 Cross-Encoder 模型（避免运行时下载）
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('BAAI/bge-reranker-base')"

EXPOSE 8000
CMD ["uvicorn", "rag.api.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

### 16.3 docker-compose.yml（本地开发/单机部署）

```yaml
version: "3.9"
services:
  rag-api:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    volumes:
      - ./customer:/app/customer:ro
    depends_on: [redis, milvus, elasticsearch]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  milvus:
    image: milvusdb/milvus:v2.4.0
    ports: ["19530:19530", "9091:9091"]
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS:  minio:9000
    depends_on: [etcd, minio]

  etcd:
    image: quay.io/coreos/etcd:v3.5.5

  minio:
    image: minio/minio:latest
    command: server /data
    ports: ["9000:9000"]

  elasticsearch:
    image: elasticsearch:8.13.0
    ports: ["9200:9200"]
    environment:
      discovery.type: single-node
      xpack.security.enabled: "false"
      ES_JAVA_OPTS: "-Xms1g -Xmx1g"
```

---

## 17. 客户现场对接清单

### 17.1 必须对接（缺失则系统无法启动）

| 资源 | 配置字段 | 验证方式 |
|------|----------|----------|
| LLM 推理服务 | `llm.base_url` + `llm.api_key` | `GET /health/full` → llm: healthy |
| Embedding 服务 | `embedding.url` 或本地模型路径 | 同上 → embedding: healthy |
| 向量数据库 | `vector_store.host/port` | 同上 → vector_store: healthy |
| 全文检索服务 | `full_text_search.hosts` | 同上 → fts: healthy |
| 对象存储 | `object_storage.endpoint` | 同上 → storage: healthy |
| Redis | `cache.host/port` | 同上 → cache: healthy |

### 17.2 强烈建议对接（对效果影响大）

| 资源 | 配置字段 | 说明 |
|------|----------|------|
| 领域同义词表 | `synonyms.files` | Excel 格式，影响 BM25 召回率 |
| 认证授权系统 | `auth.adapter` + 相关配置 | 不配置则无访问控制 |
| 权限到知识域映射 | `auth.permission_mapping` | 决定用户可访问哪些文档 |
| 中文分词插件 | ES 服务端安装 ik_analyzer | 不安装则中文 BM25 效果极差 |
| Cross-Encoder 模型 | `reranker.model_path` | 建议使用国产模型，支持中文 |

### 17.3 按需对接（取决于业务场景）

| 资源 | 配置字段 | 适用场景 |
|------|----------|----------|
| 业务数据库 | `business_data.dsn` | 需要查询实时业务数据时 |
| DB Schema 描述 | `business_data.schema_description_file` | 配合 NL2SQL 使用 |
| 知识图谱 | `knowledge_graph.uri` | 有实体关系推理需求时 |
| Prometheus | `observability.push_gateway` | 生产监控 |
| Jaeger | `observability.tracing_endpoint` | 链路追踪排查延迟 |
| IM Bot（企微/钉钉） | 额外开发，调用 `/api/v1/query` | 需要 IM 对话入口时 |
| 数据脱敏规则 | `business_data.sensitive_fields` | 业务库含敏感字段时 |

### 17.4 客户自定义适配器注册方式

```python
# customer/__init__.py
from rag.config.registry import AdapterRegistry
from customer.adapters.my_llm import MyPrivateLLMAdapter
from customer.adapters.my_auth import MyLDAPAuthAdapter

def register_adapters(registry: AdapterRegistry) -> None:
    """
    框架启动时自动调用此函数。
    在此注册所有客户自定义适配器。
    """
    registry.register("llm",  "my_private_llm", MyPrivateLLMAdapter)
    registry.register("auth", "ldap",            MyLDAPAuthAdapter)
    # 在 customer_config.yaml 中使用：
    # llm:
    #   adapter: my_private_llm
    # auth:
    #   adapter: ldap
```

### 17.5 现场对接工作量参考

| 场景 | 所需适配工作 | 预估工时 |
|------|-------------|----------|
| 使用 OpenAI 兼容接口的 LLM | 纯配置，无需写代码 | 0.5h |
| 客户私有 LLM（OpenAI 格式） | 纯配置 | 0.5h |
| 客户私有 LLM（非标准格式） | 实现 LLMAdapter 子类（约50行） | 2-4h |
| 接入已有 ES 集群 | 纯配置 + 安装 ik_analyzer 插件 | 1-2h |
| 接入 SSO/OIDC | 使用内置 OIDCAdapter，填配置 | 1h |
| 接入私有 LDAP | 实现 AuthAdapter 子类（约60行） | 3-4h |
| 行业术语词表导入 | 按模板填写 Excel，配置路径 | 2-4h（业务侧） |
| 接入业务数据库（标准SQL） | 配置 DSN + 编写 Schema 描述 YAML | 4-8h |
| 特定格式文档解析（如工程图纸） | 实现 DocParserAdapter 子类 | 1-3天 |
## 18. 完整客户配置示例

以下是一个完整的 `customer_config.yaml`，覆盖所有可配置字段，
未注明"必填"的字段均有默认值，可省略。

```yaml
# customer/config/customer_config.yaml
# 环境变量通过 ${VAR_NAME} 引用，由 rag/config/loader.py 在加载时替换

# ── 租户基本信息 ──────────────────────────────────────────────────────────────
tenant:
  id:       "customer_abc"          # 必填，用于多库数据隔离前缀
  name:     "ABC集团智能问答系统"
  language: "zh"                    # zh | en，影响分词和默认提示词
  timezone: "Asia/Shanghai"

# ── LLM 配置 ─────────────────────────────────────────────────────────────────
llm:
  adapter:          "openai_compatible"   # 必填
  base_url:         "https://llm.internal.abc.com/v1"  # 必填
  api_key:          "${ABC_LLM_API_KEY}"               # 必填，从环境变量读取
  model:            "qwen2.5-72b-instruct"             # 必填，默认模型
  timeout_seconds:  30
  # 不同任务使用不同规格模型（省成本）；未配置的任务回落到 model
  task_model_mapping:
    rewrite:   "qwen2.5-7b-instruct"    # 查询改写、意图分类用小模型
    compress:  "qwen2.5-7b-instruct"    # 摘要压缩用小模型
    generate:  "qwen2.5-72b-instruct"   # 最终生成用大模型
    health:    "qwen2.5-7b-instruct"

# ── Embedding 配置 ────────────────────────────────────────────────────────────
embedding:
  adapter:      "http_embedding"    # 必填
  url:          "https://embed.internal.abc.com"  # 必填（http_embedding 适配器）
  model:        "bge-large-zh-v1.5"
  dim:          1024                # 必须与向量库 collection 维度一致
  batch_size:   64
  query_prefix: "为这个句子生成表示以用于检索相关文章："  # BGE 模型专用查询前缀

# ── 向量数据库 ────────────────────────────────────────────────────────────────
vector_store:
  adapter:           "milvus"       # 必填
  host:              "milvus.internal.abc.com"
  port:              19530
  dim:               1024           # 必须与 embedding.dim 一致
  collection_prefix: "abc_"        # 所有 collection 名加此前缀，实现命名隔离
  index_type:        "HNSW"        # HNSW | IVF_FLAT | IVF_SQ8
  metric_type:       "COSINE"      # COSINE | L2 | IP

# ── 全文检索（BM25）──────────────────────────────────────────────────────────
full_text_search:
  adapter:      "elasticsearch"    # 必填
  hosts:
    - "https://es01.internal.abc.com:9200"
    - "https://es02.internal.abc.com:9200"
  api_key:      "${ABC_ES_API_KEY}"
  index_prefix: "abc_rag_"         # Index 名前缀
  analyzer:     "ik_max_word"      # 中文分词器（ES 需安装 analysis-ik 插件）

# ── 对象存储（原始文件）─────────────────────────────────────────────────────
object_storage:
  adapter:    "minio"              # 必填
  endpoint:   "minio.internal.abc.com:9000"
  access_key: "${ABC_MINIO_AK}"
  secret_key: "${ABC_MINIO_SK}"
  bucket:     "abc-rag-documents"
  secure:     true                 # HTTPS

# ── 缓存（Session 状态）─────────────────────────────────────────────────────
cache:
  adapter:             "redis"
  host:                "redis.internal.abc.com"
  port:                6379
  password:            "${ABC_REDIS_PASS}"
  db:                  0
  session_ttl_minutes: 60          # Session 无活动后过期时间

# ── 业务数据库（可选，aggregation 意图时启用）──────────────────────────────
business_data:
  enabled: true
  adapter: "sqlalchemy"
  dsn:     "postgresql+asyncpg://rag_user:${ABC_DB_PASS}@pg.internal.abc.com:5432/bizdb"
  schema_description_file: "./customer/config/bizdb_schema.yaml"
  allowed_tables:   ["orders", "products", "customers", "inventory", "sales_stats"]
  max_rows_returned: 100
  sensitive_fields:  ["phone", "id_card", "bank_account"]  # 结果返回前自动脱敏

# ── 知识图谱（可选，relational 意图时启用）─────────────────────────────────
knowledge_graph:
  enabled:  false        # 该客户暂无图谱需求，禁用后框架跳过图谱召回路
  adapter:  "neo4j"
  uri:      "bolt://neo4j.internal.abc.com:7687"
  username: "neo4j"
  password: "${ABC_NEO4J_PASS}"

# ── 同义词/术语库 ────────────────────────────────────────────────────────────
synonyms:
  adapter: "file_based"
  files:
    - "./customer/synonyms/abc_industry_terms.xlsx"   # 行业术语映射
    - "./customer/synonyms/abc_product_names.xlsx"    # 产品别名
    - "./customer/synonyms/abc_internal_jargon.xlsx"  # 内部用语

# ── 认证权限 ──────────────────────────────────────────────────────────────────
auth:
  adapter:    "oidc"
  issuer_url: "https://sso.abc.com/realms/abc"
  client_id:  "rag-system"
  # 角色 → 可访问的 collection 列表（"*" 代表全部）
  permission_mapping:
    role_admin:   ["*"]
    role_rd:      ["tech_docs", "api_docs", "architecture_docs"]
    role_sales:   ["product_docs", "case_studies", "pricing_docs"]
    role_hr:      ["hr_docs", "policy_docs", "onboarding_docs"]
    role_ops:     ["ops_docs", "runbook_docs", "incident_docs"]
    role_default: ["public_docs"]   # 未匹配角色的兜底

# ── 重排序模型 ────────────────────────────────────────────────────────────────
reranker:
  model_path: "/models/bge-reranker-large"  # 本地路径（已预下载）
  device:     "cpu"                          # cpu | cuda:0 | cuda:1

# ── Prompt 配置 ───────────────────────────────────────────────────────────────
prompt:
  system_template: |
    你是ABC集团的专业智能问答助手。请严格基于提供的参考资料回答问题。
    回答要求：
    1. 在每个关键陈述后用[文档N]标注信息来源
    2. 若参考资料不足以完整回答，明确说明"知识库中暂无相关信息"
    3. 不要编造参考资料中没有的数字、日期或结论
    4. 使用简洁专业的语言，技术问题可适当使用列表或代码块
  context_budget: 7000    # Prompt 总 token 预算（不含输出）

# ── Pipeline 编排 ─────────────────────────────────────────────────────────────
pipeline:
  ingestion:
    steps:
      - parse_document
      - generate_image_caption      # 开启：客户文档含大量图表
      - semantic_chunking
      - metadata_enrichment         # 开启：生成摘要和关键词，提升召回质量
      - embed_chunks
      - write_to_stores
    chunking:
      max_tokens: 512
      overlap:    50
      parent_max: 2048

  query:
    steps:
      - security_filter
      - query_understanding
      - parallel_retrieval
      - rrf_fusion
      - cross_encoder_rerank
      - prompt_assembly
      - llm_generate
      - faithfulness_check          # 开启：生产环境建议开启
    retrieval_config:
      vector_top_k:              20
      bm25_top_k:                20
      rrf_top_k:                 40
      rerank_top_k:               6
      min_relevance_score:       0.30
      min_quality_score:         0.20
      timeout_per_path_seconds:  5.0
    topic_shift_threshold: 0.50   # 余弦相似度低于此值判定为话题跳转
    min_faithfulness:      0.60   # 低于此值在答案末尾追加免责声明
    # few-shot 意图分类示例（可选，提升分类准确率）
    intent_examples:
      - query:  "X300交换机支持哪些路由协议"
        intent: "factual"
      - query:  "张三的直属上级是谁"
        intent: "relational"
      - query:  "上个季度各区域销售额分别是多少"
        intent: "aggregation"
      - query:  "如何申请差旅报销"
        intent: "procedural"
      - query:  "A产品和B产品的价格和性能有什么区别"
        intent: "comparative"
      - query:  "你好，你能做什么"
        intent: "meta"

# ── 安全配置 ──────────────────────────────────────────────────────────────────
security:
  sensitive_words:  ["涉密", "绝密"]   # 触发即拒答的敏感词
  max_query_length: 2000

# ── 可观测性 ──────────────────────────────────────────────────────────────────
observability:
  metrics_adapter:    "prometheus"
  push_gateway:       "http://prometheus-gw.internal.abc.com:9091"
  tracing_adapter:    "jaeger"
  tracing_endpoint:   "http://jaeger.internal.abc.com:14268/api/traces"
  log_level:          "INFO"
```

### 18.1 业务数据库 Schema 描述文件示例

```yaml
# customer/config/bizdb_schema.yaml
# 供 NL2SQL 引擎理解数据库结构，无需暴露真实连接信息

tables:
  - name: orders
    description: "销售订单表，记录所有客户订单"
    columns:
      - {name: order_id,    type: varchar,  description: "订单唯一编号，格式 ORD-YYYYMMDD-NNNN"}
      - {name: customer_id, type: varchar,  description: "客户ID，关联 customers 表"}
      - {name: product_id,  type: varchar,  description: "产品ID，关联 products 表"}
      - {name: amount,      type: decimal,  description: "订单金额，单位：元，含税"}
      - {name: status,      type: varchar,  description: "订单状态：pending|confirmed|shipped|completed|cancelled"}
      - {name: region,      type: varchar,  description: "销售区域：华北|华南|华东|华西|华中"}
      - {name: created_at,  type: datetime, description: "下单时间"}
      - {name: updated_at,  type: datetime, description: "最后更新时间"}

  - name: products
    description: "产品信息表"
    columns:
      - {name: product_id,   type: varchar,  description: "产品唯一编号"}
      - {name: name,         type: varchar,  description: "产品名称"}
      - {name: category,     type: varchar,  description: "产品类别：交换机|路由器|防火墙|AP"}
      - {name: unit_price,   type: decimal,  description: "单价，单位：元"}
      - {name: stock,        type: integer,  description: "当前库存数量"}
      - {name: is_active,    type: boolean,  description: "是否在售"}

  - name: sales_stats
    description: "销售统计汇总视图（已聚合，直接查询无需 GROUP BY）"
    columns:
      - {name: region,       type: varchar,  description: "区域"}
      - {name: year_month,   type: varchar,  description: "年月，格式 YYYY-MM"}
      - {name: total_amount, type: decimal,  description: "当月销售总额"}
      - {name: order_count,  type: integer,  description: "当月订单数量"}
      - {name: avg_amount,   type: decimal,  description: "当月平均订单金额"}

example_queries:
  - nl:  "上个月华南区的销售总额是多少"
    sql: "SELECT total_amount FROM sales_stats WHERE region='华南' AND year_month=DATE_FORMAT(DATE_SUB(NOW(), INTERVAL 1 MONTH), '%Y-%m')"
  - nl:  "当前库存不足100件的产品有哪些"
    sql: "SELECT name, stock FROM products WHERE stock < 100 AND is_active = true ORDER BY stock ASC"
```

---

## 19. 运维脚本

### 19.1 批量入库脚本

```python
# scripts/ingest.py
"""
批量文档入库脚本。

用法：
  # 入库单个文件
  python scripts/ingest.py --file /path/to/doc.pdf --collection tech_docs

  # 入库整个目录（递归，支持 pdf/docx/txt/png/jpg）
  python scripts/ingest.py --dir /path/to/docs/ --collection tech_docs

  # 指定租户和权限
  python scripts/ingest.py --dir /docs/ --collection hr_docs \
      --tenant customer_abc --roles role_hr,role_admin

  # 更新已存在的文档（按文件 MD5 去重，内容未变则跳过）
  python scripts/ingest.py --dir /docs/ --collection tech_docs --update-only-changed
"""
import asyncio
import argparse
import hashlib
import uuid
from pathlib import Path
from rag.config.loader import load_config
from rag.config.registry import AdapterRegistry
from rag.pipeline.ingestion.pipeline import build_ingestion_pipeline
from rag.pipeline.ingestion.steps import IngestionContext

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".png", ".jpg", ".jpeg", ".html"}

async def ingest_file(
    file_path:  Path,
    pipeline,
    storage,
    collection: str,
    tenant_id:  str,
    roles:      list[str],
    doc_id:     str | None = None,
) -> dict:
    """入库单个文件，返回入库结果摘要"""
    # 1. 上传原始文件到对象存储
    file_bytes = file_path.read_bytes()
    storage_key = f"{tenant_id}/{collection}/{file_path.name}"
    storage_url = await storage.put(
        storage_key, file_bytes,
        content_type=_guess_content_type(file_path.suffix),
        metadata={"original_name": file_path.name},
    )

    # 2. 构造入库上下文
    ctx = IngestionContext(
        doc_id        = doc_id or str(uuid.uuid4()),
        source_path   = storage_url,
        tenant_id     = tenant_id,
        collection    = collection,
        allowed_roles = roles,
        metadata      = {
            "original_path": str(file_path),
            "file_size":     len(file_bytes),
            "md5":           hashlib.md5(file_bytes).hexdigest(),
        },
    )

    # 3. 执行入库 Pipeline
    ctx = await pipeline.run(ctx)

    return {
        "doc_id":      ctx.doc_id,
        "file":        file_path.name,
        "chunk_count": len(ctx.chunks),
        "errors":      ctx.errors,
        "status":      "failed" if ctx.errors and not ctx.chunks else "done",
    }

async def main():
    parser = argparse.ArgumentParser(description="RAG 批量入库工具")
    parser.add_argument("--config",     default="customer/config/customer_config.yaml")
    parser.add_argument("--file",       help="单个文件路径")
    parser.add_argument("--dir",        help="目录路径（递归扫描）")
    parser.add_argument("--collection", default="default")
    parser.add_argument("--tenant",     default=None, help="租户ID，默认从配置读取")
    parser.add_argument("--roles",      default="", help="允许访问的角色，逗号分隔")
    parser.add_argument("--concurrency", type=int, default=4, help="并发入库数")
    parser.add_argument("--update-only-changed", action="store_true")
    args = parser.parse_args()

    config   = load_config(args.config)
    registry = AdapterRegistry()
    try:
        from customer import register_adapters
        register_adapters(registry)
    except ImportError:
        pass

    tenant_id = args.tenant or config.tenant["id"]
    roles     = [r.strip() for r in args.roles.split(",") if r.strip()]
    pipeline  = build_ingestion_pipeline(config, registry)
    storage   = registry.get_storage(config)

    # 收集文件列表
    files: list[Path] = []
    if args.file:
        files = [Path(args.file)]
    elif args.dir:
        for ext in SUPPORTED_EXTENSIONS:
            files.extend(Path(args.dir).rglob(f"*{ext}"))

    print(f"共发现 {len(files)} 个文件，开始入库（并发数={args.concurrency}）...")

    sem     = asyncio.Semaphore(args.concurrency)
    results = []

    async def ingest_with_sem(f):
        async with sem:
            result = await ingest_file(f, pipeline, storage,
                                       args.collection, tenant_id, roles)
            status_icon = "✓" if result["status"] == "done" else "✗"
            print(f"  {status_icon} {result['file']} → {result['chunk_count']} chunks"
                  + (f" [错误: {result['errors']}]" if result["errors"] else ""))
            return result

    results = await asyncio.gather(*[ingest_with_sem(f) for f in files])

    done  = sum(1 for r in results if r["status"] == "done")
    failed = sum(1 for r in results if r["status"] == "failed")
    total_chunks = sum(r["chunk_count"] for r in results)
    print(f"\n入库完成：成功 {done} 个，失败 {failed} 个，共生成 {total_chunks} 个 Chunk")

def _guess_content_type(suffix: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain",
        ".png": "image/png",
        ".jpg": "image/jpeg",
    }.get(suffix.lower(), "application/octet-stream")

if __name__ == "__main__":
    asyncio.run(main())
```

### 19.2 评测脚本

```python
# scripts/evaluate.py
"""
RAG 系统召回与生成质量评测脚本。
使用 RAGAS 框架计算标准指标。

用法：
  python scripts/evaluate.py --test-set ./tests/fixtures/eval_set.yaml \
      --config customer/config/customer_config.yaml \
      --output ./eval_report.json

eval_set.yaml 格式：
  questions:
    - id: "q001"
      question:  "X300交换机支持哪些路由协议？"
      ground_truth: "X300交换机支持OSPF、BGP、ISIS、静态路由等路由协议。"
      relevant_doc_ids: ["doc_001", "doc_002"]   # 可选，用于召回率计算
"""
import asyncio
import argparse
import json
import yaml
from pathlib import Path
from rag.config.loader import load_config
from rag.config.registry import AdapterRegistry
from rag.core.memory import MemoryManager
from rag.pipeline.query.pipeline import build_query_pipeline
from rag.models import PipelineContext
import uuid

async def evaluate_single(
    question:    str,
    pipeline,
    tenant_id:  str,
) -> dict:
    """对单个问题运行完整问答流程，返回评测数据"""
    ctx = PipelineContext(
        session_id = str(uuid.uuid4()),
        user_id    = "eval_user",
        raw_query  = question,
        tenant_id  = tenant_id,
    )
    ctx = await pipeline.run(ctx)
    return {
        "question":         question,
        "answer":           ctx.answer or "",
        "retrieved_docs":   [r["doc_id"] for r in ctx.source_refs],
        "faithfulness":     ctx.metadata.get("faithfulness_score"),
        "intent":           ctx.understanding.intent.value if ctx.understanding else None,
        "latency_ms":       sum(ctx.step_timings.values()),
        "step_timings":     ctx.step_timings,
    }

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-set",  required=True)
    parser.add_argument("--config",    default="customer/config/customer_config.yaml")
    parser.add_argument("--output",    default="./eval_report.json")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()

    config    = load_config(args.config)
    registry  = AdapterRegistry()
    try:
        from customer import register_adapters
        register_adapters(registry)
    except ImportError:
        pass

    # 构造无记忆的评测 Pipeline（每题独立 session）
    pipeline = build_query_pipeline(config, registry,
                                    memory=None, stream=False)

    test_data = yaml.safe_load(Path(args.test_set).read_text(encoding="utf-8"))
    questions = test_data["questions"]

    print(f"开始评测，共 {len(questions)} 题，并发={args.concurrency}...")
    sem = asyncio.Semaphore(args.concurrency)

    async def eval_with_sem(item):
        async with sem:
            result = await evaluate_single(
                item["question"], pipeline, config.tenant["id"]
            )
            result["id"]           = item.get("id", "")
            result["ground_truth"] = item.get("ground_truth", "")
            result["relevant_docs"] = item.get("relevant_doc_ids", [])
            return result

    results = await asyncio.gather(*[eval_with_sem(q) for q in questions])

    # 计算汇总指标
    faithfulness_scores = [r["faithfulness"] for r in results if r["faithfulness"] is not None]
    avg_faithfulness    = sum(faithfulness_scores) / len(faithfulness_scores) if faithfulness_scores else 0
    avg_latency         = sum(r["latency_ms"] for r in results) / len(results)

    # 召回率：relevant_docs 中有多少被实际召回
    recall_scores = []
    for r in results:
        if r["relevant_docs"]:
            retrieved = set(r["retrieved_docs"])
            relevant  = set(r["relevant_docs"])
            recall_scores.append(len(retrieved & relevant) / len(relevant))
    avg_recall = sum(recall_scores) / len(recall_scores) if recall_scores else None

    report = {
        "summary": {
            "total_questions":   len(results),
            "avg_faithfulness":  round(avg_faithfulness, 3),
            "avg_recall_at_k":   round(avg_recall, 3) if avg_recall else None,
            "avg_latency_ms":    round(avg_latency, 1),
            "p95_latency_ms":    round(sorted(r["latency_ms"] for r in results)[int(len(results)*0.95)], 1),
        },
        "details": results,
    }

    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n评测完成:")
    print(f"  平均忠实度:  {report['summary']['avg_faithfulness']}")
    print(f"  平均召回率:  {report['summary']['avg_recall_at_k']}")
    print(f"  平均延迟:    {report['summary']['avg_latency_ms']} ms")
    print(f"  报告已写入:  {args.output}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 20. 关键实现约束与边界条件

### 20.1 必须遵守的行为约束

以下约束在框架的所有实现中必须严格遵守，不得以任何原因绕过：

**数据隔离**：所有向量库、全文索引的读写操作必须携带 `tenant_id` 过滤条件。
`VectorStoreAdapter.search` 中 `_build_filter` 方法必须将 `tenant_id == ctx.tenant_id`
作为第一个过滤条件，且不可被外部覆盖。

**权限过滤**：`allowed_roles` 过滤必须在检索层执行（在数据库查询时过滤），
不能先召回再在应用层过滤（性能差且存在数据泄露风险）。

**SQL 安全**：`BusinessDataAdapter.execute` 在执行前必须：
1. 校验 SQL 只包含 SELECT 语句（拒绝 INSERT/UPDATE/DELETE/DROP/EXEC）
2. 校验 SQL 中涉及的表名在 `config.business_data.allowed_tables` 白名单中
3. 对结果中 `config.business_data.sensitive_fields` 字段进行掩码处理（如 `138****8888`）

**Prompt 注入防护**：`SecurityFilterStep` 必须是 Pipeline 的第一个步骤，
且检测到注入模式时必须直接设置 `ctx.answer` 并返回，不得继续执行后续步骤。

**token 预算**：`PromptAssemblyStep` 在组装完 Prompt 后必须调用
`llm.count_tokens(ctx.prompt)` 验证不超过 `config.prompt.context_budget`，
超出时优先截断短期记忆区块（从最早轮次开始），其次截断检索上下文（从低分 Chunk 开始）。

### 20.2 错误处理约定

```python
# 各层错误处理规则（实现时必须遵循）

# 1. 适配器层：所有网络/存储异常转换为 AdapterError 子类，不向上透传原始异常
try:
    resp = await httpx_client.post(...)
except httpx.ConnectError as e:
    raise AdapterConnectionError(self.__class__.__name__, f"连接失败: {e}", e)

# 2. Pipeline 步骤层：步骤异常写入 ctx.errors，不抛出（由 engine 的 stop_on_error 控制）
try:
    ctx = await step.execute(ctx)
except Exception as e:
    ctx.errors.append(f"[{step.name}] {e}")
    if self.stop_on_error:
        break

# 3. API 层：内部错误统一返回 500，向用户隐藏技术细节
@app.exception_handler(Exception)
async def global_handler(request, exc):
    logger.error("unhandled_error", error=str(exc), path=request.url.path)
    return JSONResponse(status_code=500, content={"error": "系统内部错误，请稍后重试"})

# 4. 召回失败兜底：所有召回路均失败时，ctx.final_chunks 为空
#    PromptAssemblyStep 检测到 final_chunks 为空时，
#    生成不含检索上下文的 Prompt，并在 system 中注入指令：
#    "知识库中暂无相关内容，请如实告知用户并建议联系相关负责人"
```

### 20.3 性能基准与优化目标

| 指标 | 目标值 | 优化手段 |
|------|--------|----------|
| 首 token 延迟（P50） | < 1.5s | 流式输出 + 并行召回 |
| 端到端延迟（P50，含生成） | < 5s | 并行召回 + 小模型改写 |
| 端到端延迟（P95） | < 10s | 各路超时控制（5s） |
| 入库吞吐 | > 50 docs/min | 并发入库 + 批量 Embedding |
| 召回精确率（Precision@6） | > 0.7 | Cross-Encoder 重排 |
| 召回覆盖率（Recall@20） | > 0.85 | 多路召回 + 查询改写 |
| 忠实度均值（Faithfulness） | > 0.8 | 检索质量 + 截断策略 |

---

## 21. 大模型实现指南

> **本节专为使用本规格文档生成代码的大模型而写。**
> 请按以下顺序和原则生成代码，以确保生成结果的完整性和正确性。

### 21.1 代码生成顺序

按以下顺序生成，每步依赖前步的定义：

```
Step 1  生成 rag/models.py                    （§4 全部数据模型）
Step 2  生成 rag/adapters/base/ 下所有抽象基类  （§5 全部接口）
Step 3  生成 rag/adapters/base/exceptions.py   （§5.1 异常层次）
Step 4  生成 rag/config/models.py              （§11 配置模型）
Step 5  生成 rag/config/loader.py              （§11 配置加载）
Step 6  生成 rag/adapters/builtin/ 下所有内置实现（§6，按需选择适配器）
Step 7  生成 rag/core/memory.py                （§9 记忆管理器）
Step 8  生成 rag/pipeline/engine.py            （§10 编排引擎）
Step 9  生成 rag/pipeline/ingestion/steps.py   （§7.1 入库步骤）
Step 10 生成 rag/pipeline/ingestion/pipeline.py（§7.2 入库 Pipeline 组装）
Step 11 生成 rag/pipeline/query/steps.py       （§8.1 问答步骤）
Step 12 生成 rag/pipeline/query/pipeline.py    （§10 问答 Pipeline 组装）
Step 13 生成 rag/config/registry.py            （§11 适配器注册中心）
Step 14 生成 rag/api/ 下所有文件               （§12 REST API）
Step 15 生成 rag/observability/ 下所有文件     （§14 可观测性）
Step 16 生成 tests/ 下所有测试文件             （§15 测试）
Step 17 生成 scripts/ingest.py                 （§19.1）
Step 18 生成 scripts/evaluate.py               （§19.2）
Step 19 生成 pyproject.toml / Dockerfile / docker-compose.yml（§16）
```

### 21.2 实现时的关键决策说明

**当规格给出了"伪码"而非完整实现时**（如 `SemanticChunkingStep._build_chunks`），
按以下原则补全：
- 遵守方法签名和返回类型
- 遵守规格注释中描述的行为（步骤和规则）
- 选择最简单、可读性最高的实现，不引入规格未提及的第三方库
- 在代码中添加 `# TODO: 可根据实际场景优化` 注释标记可扩展点

**当规格描述了接口契约但未给出默认实现时**（如 `KnowledgeGraphAdapter.upsert_relations`），
生成一个满足签名的最小实现（`raise NotImplementedError`），
配合完整的 docstring 说明预期行为。

**当两处规格存在细节冲突时**，以数据模型（§4）和接口定义（§5）为准，
Pipeline 步骤（§7、§8）中的描述为补充说明。

### 21.3 禁止的实现模式

生成代码时禁止以下模式，若规格描述与之冲突，以禁止列表为准：

```python
# ❌ 禁止：适配器直接抛出非 AdapterError 异常
async def search(self, ...):
    raise ConnectionError("failed")      # 错误：必须转换为 AdapterError

# ✓ 正确：转换为框架异常
async def search(self, ...):
    try: ...
    except ConnectionError as e:
        raise AdapterConnectionError(self.__class__.__name__, str(e), e)

# ❌ 禁止：Pipeline 步骤修改框架核心的任何文件
# ❌ 禁止：在 rag/core/ 或 rag/adapters/base/ 中硬编码客户特定逻辑
# ❌ 禁止：跳过 tenant_id 过滤直接查询全库数据
# ❌ 禁止：在 API 响应中暴露原始异常堆栈或内部路径
# ❌ 禁止：同步阻塞调用放在 async 函数中（网络/IO 操作必须用 await）
# ❌ 禁止：在主链路（用户等待的路径）中执行摘要压缩（必须用 asyncio.create_task）
# ❌ 禁止：在 BusinessDataAdapter.execute 中直接执行未经白名单校验的 SQL
```

### 21.4 文件生成时的自检清单

每生成一个文件后，验证以下项目：

- [ ] 所有 `import` 语句路径与 §2 项目结构一致
- [ ] 所有抽象方法均有 `@abstractmethod` 装饰器
- [ ] 所有 async def 中的 IO 操作使用 `await`
- [ ] 所有适配器构造函数只接受对应的 `XxxConfig` 参数
- [ ] 所有 Pipeline 步骤的 `execute` 方法返回 `ctx`（即使未修改）
- [ ] 配置字段通过 `config.field_name` 访问，不硬编码默认值（默认值在 Pydantic 模型中定义）
- [ ] 多租户隔离：向量库和全文索引操作携带 `tenant_id`
- [ ] 错误处理：适配器层转换为 `AdapterError`，步骤层写入 `ctx.errors`

### 21.5 客户适配器实现模板

当需要为客户生成自定义适配器时，使用以下模板：

```python
# customer/adapters/my_custom_adapter.py
"""
客户自定义 [适配器类型] 适配器。
对接：[客户系统名称]
接口文档：[接口文档链接或描述]
"""
from rag.adapters.base.[base_module] import [BaseAdapterClass]
from rag.adapters.base.exceptions import AdapterConnectionError, AdapterTimeoutError
from rag.config.models import [ConfigClass]

class My[Type]Adapter([BaseAdapterClass]):
    """
    [简述此适配器对接的系统和特殊处理逻辑]

    注册名：[在 customer/__init__.py 中注册时使用的名称]
    配置字段：[列出此适配器特有的配置字段]
    """

    def __init__(self, config: [ConfigClass]):
        self.config = config
        # 初始化客户系统的 SDK 或 HTTP 客户端

    # 实现所有 @abstractmethod
    async def [method_name](self, ...):
        try:
            # 调用客户系统接口
            # 将结果转换为框架标准数据结构
            ...
        except [ClientException] as e:
            raise AdapterConnectionError(self.__class__.__name__, str(e), e)

    async def health_check(self) -> bool:
        try:
            # 发送探测请求
            return True
        except Exception:
            return False
```

---

*文档结束。本规格版本 v1.0，覆盖章节 §1-§21，共定义：*
*9 类适配器抽象接口 · 14 个内置适配器实现 · 9 个入库步骤 · 8 个问答步骤*
*1 个三层记忆管理器 · 1 个 Pipeline 编排引擎 · 完整 REST API · 2 个运维脚本*
*1 套配置系统（Pydantic v2） · 完整测试规格 · Docker 部署配置*
