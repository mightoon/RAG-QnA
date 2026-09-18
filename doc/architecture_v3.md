# RAG 智能问答系统 — 架构、设计与功能说明（V2）

> **文档版本**：V2，在 V1 基础上全面更新。
> 核心变化：文档处理工业化、多库数据一致性保障、用户主动检索路控制、元数据前置过滤层、Session 临时文档。

---

## 目录

1. [系统定位与目标](#1-系统定位与目标)
2. [整体架构概览](#2-整体架构概览)
3. [核心设计原则](#3-核心设计原则)
4. [三大主体功能域](#4-三大主体功能域)
5. [知识库构建体系（V2 重构）](#5-知识库构建体系)
6. [在线问答引擎（V2 扩展）](#6-在线问答引擎)
7. [多轮对话记忆体系](#7-多轮对话记忆体系)
8. [适配器层与外部资源集成（V2 扩展）](#8-适配器层与外部资源集成)
9. [多库数据一致性体系（V2 新增）](#9-多库数据一致性体系)
10. [用户界面体系（V2 扩展）](#10-用户界面体系)
11. [安全与权限体系](#11-安全与权限体系)
12. [可观测性与运维体系](#12-可观测性与运维体系)
13. [部署架构与交付模式](#13-部署架构与交付模式)
14. [性能特征与质量基准](#14-性能特征与质量基准)
15. [各层模块交互总览](#15-各层模块交互总览)

---

## 1. 系统定位与目标

本系统是一套**企业级私有化 RAG 智能问答框架**，定位为可复用的交付产品。V2 在 V1 基础上重点加强了三个维度：

**文档处理工业化**：针对企业真实文档的复杂性（PDF 有文字型/扫描型/混合型/单栏双栏，Excel 有数据表和透视表，PPT 有备注）建立工业级处理流水线，每类文档有专属的处理路径和质量检测机制。

**数据一致性保障**：引入协调层（IngestionCoordinator）统管写入 MinIO、MySQL、ES、Milvus、Neo4j 五个系统的全过程，通过两阶段写入、检查点断点续写、软删除版本管理和定时一致性巡检，保障分布式写入的最终一致性。

**用户检索主权**：用户可在问答界面主动选择激活哪些检索路（向量/全文/图谱/结构化），并通过元数据筛选（文件类型/时间范围）缩小检索范围，将检索决策权从系统内部传递给用户。

---

## 2. 整体架构概览

### 2.1 共享内核分层

V3 采用**共享内核（Shared Kernel）架构**，在 V2 单体框架基础上建立清晰的内部边界：写侧（知识库构建）和读侧（在线问答）通过稳定的内核 API 解耦，为后续独立部署留路。

```
┌────────────────────────┐  ┌────────────────────────┐
│   模块 A：知识库构建    │  │   模块 B：在线问答      │
│  （写侧，IO密集）       │  │  （读侧，延迟敏感）     │
│  UI管理界面            │  │  对话界面               │
│  批次/任务管理         │  │  检索路选择             │
└──────────┬─────────────┘  └──────────┬─────────────┘
           │  调用内核 API               │  调用内核 API
           ▼                            ▼
┌───────────────────────────────────────────────────────┐
│                    共享内核层                           │
│                                                       │
│  IngestionKernel              RetrievalKernel         │
│  ┌──────────────────┐         ┌──────────────────┐   │
│  │ 声明式YAML Workflow│         │ QueryPlan 查询理解 │   │
│  │ 文档类型路由       │         │ 五路并行召回      │   │
│  │ 三阶段质量检测     │         │ SelfEval受控迭代  │   │
│  │ IngestionCoord.  │         │ RRF+精排          │   │
│  │ 两阶段写入        │         │ Prompt组装        │   │
│  └──────────────────┘         └──────────────────┘   │
│                                                       │
│  共享：MemoryManager | AdapterRegistry | MySQLMeta    │
└────────────────────────┬──────────────────────────────┘
                         │  标准接口契约
┌────────────────────────▼──────────────────────────────┐
│                   适配器接口层                          │
│  LLM │ Embedding │ VectorStore │ FullTextSearch        │
│  MySQLMeta │ DocParser │ KnowledgeGraph │ BusinessData │
│  Synonym │ Auth │ Storage │ EntityLinker               │
└────────────────────────┬──────────────────────────────┘
                         │
┌────────────────────────▼──────────────────────────────┐
│                  客户外部资源层                         │
│  私有LLM │ Milvus │ Elasticsearch │ MySQL(元数据)      │
│  MySQL/PG(业务) │ Neo4j/NebulaGraph │ MinIO │ Redis   │
└───────────────────────────────────────────────────────┘
```

**当前部署形态**：模块A + 共享内核 + 模块B 在同一 FastAPI 应用中运行（单体），但内核 API 边界清晰，后续可按需拆分为独立服务（写侧和读侧在资源需求和扩缩容策略上差异显著）。

### 2.2 数据存储职责分工

| 存储 | 存什么 | 服务什么场景 |
|------|--------|-------------|
| MinIO | 原始文件二进制 | 答案来源溯源，原文预览 |
| MySQL（元数据库） | 文档/Chunk元数据、表格结构化数据、入库任务/批次状态、实体规范名映射 | 元数据前置过滤、约束硬下推、一致性巡检 |
| Elasticsearch | Chunk 文本（content字段 BM25 + content.kw_exact keyword 子字段精确匹配 + keywords字段） | BM25全文检索 + kw_exact 精确匹配（两路独立） |
| Milvus | Chunk Embedding 向量 | 向量语义检索 |
| Neo4j/NebulaGraph | 实体节点和关系三元组 | 多跳关系推理（实体已归一化） |
| MySQL（业务库） | 客户业务数据（不迁移，实时查询） | NL2SQL 结构化查询 |
| Redis | Session状态、三层对话记忆 | 多轮对话上下文 |

**ES 双路索引的关键设计**：`content` 字段存正文（IK分词，走BM25），同时有 `content.kw_exact`（keyword 子字段，不分词）和独立的 `keywords`（keyword 类型，存提取的专有名词/型号）。三个字段共享存储，各自走不同查询语义，无重复存储开销。

## 3. 核心设计原则

### 3.1 三不变三可换（继承 V1）

**不变**：Pipeline 编排逻辑、RRF 算法、Cross-Encoder 重排、三层记忆压缩、两阶段写入协议、一致性巡检规则。

**可换**：所有外部服务调用（通过适配器）、文档解析实现、业务规则注入。

### 3.2 渐进式降级（V2 强化）

资源缺失时系统自动降级而不崩溃。V2 新增降级场景：

- MySQL 元数据库不可用 → 跳过元数据前置过滤，直接进行全库检索
- 某库写入失败 → 标记 PARTIAL 状态，其他库正常可用，一致性巡检后台修复
- 所有检索路被用户关闭 → 自动回退到系统默认路（向量+BM25）

### 3.3 入库的最终一致性

写入五个存储系统的操作不可能原子化，V2 采用"顺序提交+检查点+自动恢复"三机制组合，保证最终一致：即使中间任何一步失败，系统通过检查点断点续写和定时巡检自动修复，用户不需要手动干预。

---

## 4. 三大主体功能域

系统的所有功能归属三个相互协作但边界清晰的域：

```
┌─────────────────────────────────────────────────────────────┐
│  功能域 A：知识库构建                                         │
│  ├── 文档入库（UI上传 / 服务器路径 / 命令行脚本）             │
│  ├── 文档类型分派（PDF文字/扫描/混合 / Word / Excel / PPT…） │
│  ├── 异步任务管理（状态机 / 进度推送 / 重试 / 幂等）          │
│  ├── 三阶段质量检测（解析 / 分块 / 入库后验证）               │
│  └── 多库一致性写入（两阶段 / 检查点 / 软删除 / 巡检）        │
└─────────────────────┬───────────────────────────────────────┘
                      │ 知识库（Milvus+ES+MySQL+Neo4j+MinIO）
┌─────────────────────▼───────────────────────────────────────┐
│  功能域 B：RAG 问答                                           │
│  ├── 查询理解（意图/改写/指代消解/话题跳转/子问题分解）        │
│  ├── 元数据前置过滤（MySQL，缩小检索范围）                    │
│  ├── 用户检索路选择（向量/BM25/图谱/结构化，用户主动控制）    │
│  ├── 多路并行召回 + RRF 融合 + Cross-Encoder 重排             │
│  ├── Session 临时文档（对话中上传，优先召回，2小时有效）       │
│  ├── 三层记忆管理（短期/工作/长期，跨轮次上下文维护）         │
│  └── Prompt 组装 + LLM 生成 + 忠实度校验 + 来源溯源          │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  功能域 C：配置管理                                           │
│  ├── 资源连接（含 MySQL 元数据库，各适配器连接配置）           │
│  ├── 功能开关（检索路 / Pipeline 步骤 / 可观测性）            │
│  ├── 知识域权限（角色 → collection 映射）                     │
│  ├── 提示词管理（System Prompt + 意图示例）                   │
│  └── 通知配置（邮件 / Webhook）                               │
└─────────────────────────────────────────────────────────────┘
```

功能域 A 和 B 之间的关联点有两处：
一是**全局知识库**（A 构建，B 查询）；
二是**Session 临时文档**（用户在 B 的对话中上传，走轻量化的 A 处理路径，只写入临时向量 collection，不入全局库）。

---

## 5. 知识库构建体系

### 5.1 入库方式（三路）

**UI 拖拽上传**：单次最大 500MB，支持多文件；上传后立即创建 IngestBatch，每个文件创建 IngestTask 子任务，SSE 实时推送进度。

**本地目录批量入库**（V3 新增）：管理员在浏览器中选择本机整个目录（`<input webkitdirectory>`），JS 递归列出所有文件，以 4 并发队列逐文件上传后触发入库。扫描结果先展示预览（文件数量、类型分布、总大小），确认后才开始上传。适用于管理员本机有文档、1GB 以内的场景。

**服务器路径指定**：配置 `server_ingest_root` 后，管理员填写服务器挂载目录路径，后端直接读取，无 HTTP 传输，适合 10GB+ 大批量场景。

所有方式统一创建 IngestBatch（单文件也建批次，total=1），任务视图提供**批次维度的聚合进度**。

### 5.2 文档类型路由与声明式 YAML 工作流

**V3 将 Pipeline 从 Python 硬编码字典改为声明式 YAML 工作流注册表**（`workflows.yaml`），步骤序列在运行时从 YAML 加载，支持热重载。新增格式只需注册一条 workflow，不修改核心代码。

YAML 支持**条件分支**（`condition_*` 步骤在运行时返回 `next_steps`），消除了 V2 中 `pdf_text / pdf_scan / pdf_mixed` 三条独立 Pipeline 变体——统一走 `pdf` workflow，由 `detect_scan_type` 步骤在运行时动态路由。

**所有 workflow 的第一步是 `detect_format`（magic bytes 文件头检测）**，防止扩展名伪装（如 XLSX 重命名为 .pdf 上传）。检测失败的文件直接拒绝入库。

PDF workflow 增加两个专项步骤：
- `extract_toc`：提取 PDF 内置书签目录（TOC），在 `outline_build` 步骤中与识别出的标题交叉验证——TOC 中有但正文未识别为标题的节点记录告警，反之亦然
- `reorder_columns`：输出 `adjacent_pair_accuracy` 指标（相邻对准确率），用于量化双栏重排质量

### 5.3 OutlineBuilder 与 section_path

每个文档在解析阶段由 `OutlineBuilder` 维护标题栈，为每个 ParsedElement 生成 `section_path`（如 `"第三章 系统配置/3.1 网络参数配置/3.1.2 路由协议设置"`）。

PDF 通过 TOC 交叉验证标题识别的正确性；DOCX 直接读取 Word Heading 样式；Markdown 通过 `#` 数量；PPT 通过幻灯片标题框；Excel 使用 Sheet 名作一级路径。

`section_path` 的三处用途：
1. **检索过滤**：`section_path LIKE '第三章%'`（MySQL 元数据前置过滤）
2. **溯源定位**：答案来源展示 `第三章 > 3.1.2` 而非仅页码
3. **上下文聚合**：召回某 Chunk 时，可快速找到同 section 的相邻 Chunk 扩展上下文

### 5.4 图片序号识别与溯源

`FigureLabelExtractor` 在版面分析阶段检测图片下方紧邻的短文本块，用正则识别"图28 / Figure 3-2 / 表5"等标注，存入 `raw_data.figure_label` 和 `raw_data.figure_caption`。

答案溯源时展示优先级：`figure_label`（"图28"）> `figure_caption`（图题文字）> `page_num`（"第12页"）。

### 5.5 确定性 chunk_id（幂等写入）

`chunk_id = sha256(tenant_id:doc_id:seq:content_hash[:8])[:32]`

同一文档重新入库时，相同位置和内容的 Chunk 产生相同 chunk_id，ES 和 Milvus 的 upsert 操作直接覆盖旧数据，无需额外删除逻辑，大幅简化一致性保障的实现复杂度。

### 5.6 两级任务模型

```
IngestBatch（批次）
  ├── batch_id
  ├── total / succeeded / failed / pending
  ├── status（pending/running/done/partial_failed）
  └── IngestTask × N（子任务，每个文件一个）
        ├── task_id → batch_id 外键
        ├── status（PENDING→PARSING→...→DONE/FAILED）
        ├── WriteCheckpoint（各库写入进度，断点续写用）
        └── quality_summary

```

任务视图提供两级视图切换：**批次视图**（"这批200个文件中已完成180个，失败3个"）和**任务视图**（展开某批次查看每个文件的详细状态）。

### 5.7 四库写入顺序与一致性保障

写入顺序严格按依赖关系：`MinIO → MySQL → ES(kw+fulltext) → Milvus → Neo4j`。每步完成后更新 `WriteCheckpoint`，支持断点续写。文档更新采用软删除版本管理，每小时定时一致性巡检自动修复差异。

（一致性体系详见 §9）

## 6. 在线问答引擎

### 6.1 V3 完整处理流程

```
用户输入（含检索路选择 + 元数据筛选）
  ↓
① 安全过滤          → 敏感词/注入/权限校验
  ↓
② 查询理解 → QueryPlan
   ├── 实体识别 + EntityLinker（原始名 → 规范名/canonical）
   ├── 约束提取（时间/类型 → QueryConstraint，硬下推不参与相关性）
   ├── 隐式元数据意图（ImplicitMetaExtractor）
   ├── 同义词扩展（SynonymAdapter）
   ├── 软路由权重（每路 0-1 浮点数，替代布尔开关）
   └── 语义句构造（剥离约束+实体的纯语义核心）
  ↓
③ 元数据前置过滤    → MySQL chunk_id 白名单（显式+隐式合并，section_keyword支持）
  ↓
④ 五路并行召回（独立5s超时，QueryPlan.constraints 统一下推到所有路的filter）
   ├── KW_EXACT：ES content.kw_exact + keywords 字段 term 精确匹配
   ├── VECTOR：Milvus 向量检索（semantic_query + rewrites）
   ├── BM25：ES text 全文检索（同义词扩展词降权合并）
   ├── GRAPH：图谱多跳推理（entities.canonical 定位节点）
   ├── STRUCTURED：NL2SQL（plan.constraints 辅助WHERE生成）
   └── EPHEMERAL：临时文档检索（始终激活，分数×1.2）
  ↓
⑤ RRF 融合（route权重参与融合系数）+ 语义去重
  ↓
⑥ 受控迭代自评（SelfEvalStep）
   └── 最高分 < 0.5 且未超迭代上限(2) → 触发失败导向改写 + HyDE，二轮结果合并
  ↓
⑦ Cross-Encoder 精排（top-40→top-6）
  ↓
⑧ Prompt 组装（记忆注入 + 上下文压缩 + 引用编号 + figure_label 溯源）
  ↓
⑨ LLM 流式生成
  ↓
⑩ 忠实度校验 + 来源溯源（figure_label优先）+ 格式化输出
```

### 6.2 QueryPlan：结构化查询计划

V3 查询理解的产物是结构化 `QueryPlan`（而非 V2 的改写字符串），把用户问题分解为五个互不干扰的层次分别投放到对应检索路：

| 层次 | 内容 | 投放路径 |
|------|------|----------|
| `constraints` | 时间/类型/部门等硬约束 | 所有路的 filter（不参与相关性计算） |
| `entities.canonical` | 实体归一化规范名 | kw_exact 精确匹配 |
| `keywords` | 专有名词/型号/编号 | kw_exact + BM25 |
| `semantic_query` | 剥离约束后的纯语义核心 | VECTOR 向量化 |
| `synonyms` | 同义词扩展 | BM25 降权合并 |

约束做硬过滤、实体走精确、关键词走词面、语义走向量、同义词降权合并——各类信息互不干扰，精度和覆盖率均优于单一查询文本的全库检索。

### 6.3 EntityLinker 实体归一化

用户说"华为"，文档存的是"华为技术有限公司"；用户说"合规部"，知识库里是"合规与法务部"。不做实体归一化，kw_exact 路就无法精确命中。

EntityLinker 两阶段匹配：规则阶段（O(n) 字符串匹配 MySQL 实体列表，毫秒级）→ 向量阶段（向量近邻，仅规则未命中时触发）。命中阈值 0.88，低于此值不归一化，保留原始名称走 BM25 模糊匹配。

### 6.4 五路检索的互补设计

| 检索路 | 底层引擎 | 擅长场景 | 劣势 |
|--------|----------|----------|------|
| KW_EXACT | ES keyword | 型号/编号/专有名词精确匹配 | 无语义理解 |
| VECTOR | Milvus | 语义/意图/近义词/口语化查询 | 精确关键词不稳定 |
| BM25 | ES text | 关键词自然语言、中文分词 | 词汇鸿沟（近义词覆盖差） |
| GRAPH | Neo4j/NebulaGraph | 多跳关系推理 | 实体识别误差放大 |
| STRUCTURED | MySQL/PG | 聚合统计、数值查询 | 仅覆盖结构化数据 |

KW_EXACT 和 BM25 同为 ES，但查询语义根本不同：前者不分词（完整匹配），后者分词（模糊匹配）。分开作为独立路，用户可单独控制，路由权重也独立计算。

### 6.5 受控迭代查询（SelfEvalStep）

首轮融合结果的最高相关性分数若低于阈值（默认 0.5），且未超过迭代上限（默认 2 轮），SelfEvalStep 自动触发第二轮：

- **失败导向改写**：提示 LLM"上轮未找到足够相关内容，请换角度措辞"
- **HyDE**：让 LLM 生成假设性回答文档，以该文档为向量查询

二轮结果与首轮合并去重（取分数最高版本），再走精排。普通查询直通（无额外延迟），触发时增加约 800-1500ms。最多 2 轮，防延迟失控。

### 6.6 元数据前置过滤层

MySQL 元数据库不是检索路，而是检索前的**过滤层**，先于所有检索路执行，把候选 Chunk 收窄为 chunk_id 白名单（上限 50000），再把白名单作为 filter 注入所有检索路。

过滤条件来源两路合并（显式优先）：
- **显式**：用户在 UI 检索路面板设置（文件类型/时间范围/知识域）
- **隐式**：ImplicitMetaExtractor 从问题语义推断（"去年Q3" → 时间范围，"第三章" → section_path 前缀）

### 6.7 软路由权重替代布尔开关

V2 的路由是布尔开关（路是否激活），V3 改为 0-1 浮点权重，影响 RRF 融合时各路的系数。

意图基础权重示例：
- `factual`（事实查询）→ kw_exact=0.9, bm25=0.9, vector=0.8, graph=0.0
- `relational`（关系查询）→ vector=0.7, graph=0.9
- `aggregation`（统计查询）→ structured=1.0, others=0.1

权重在意图基础上还受实体类型（有精确实体则提升 kw_exact）和约束（有时间约束则提升 structured）动态调整，最后与用户 UI 选择取交集（用户关闭的路权重置 0）。

## 7. 多轮对话记忆体系

（与 V1 一致，详见 V1 文档 §6）

---

## 8. 适配器层与外部资源集成

### 8.1 V2 新增适配器

**MySQL 元数据适配器**（注册名：`mysql_meta`）：独立于业务数据适配器，专门管理系统自身的元数据。核心方法包括：文档和 Chunk 元数据的 upsert/查询，元数据过滤查询（`query_chunk_ids`，返回符合条件的 chunk_id 白名单），入库任务的 CRUD，一致性巡检辅助查询。自动建表（DDL 幂等，首次启动时执行）。

V2 适配器完整清单（在 V1 十类基础上增加 MySQL 元数据适配器）：

| 类型 | 注册名 | 内置实现 | 用途 |
|------|--------|----------|------|
| LLM | openai_compatible / vllm | ✓ | 文本生成、查询改写、摘要 |
| Embedding | http_embedding / openai_embedding | ✓ | 向量化 |
| VectorStore | milvus / qdrant / pgvector | ✓ | 语义检索 |
| FullTextSearch | elasticsearch / opensearch | ✓ | BM25 检索 |
| **MySQLMeta** | **mysql_meta** | **✓（V2新增）** | **元数据过滤 / 任务管理** |
| DocParser | pdf / docx / image | ✓ | 文档解析 |
| BusinessData | sqlalchemy | ✓ | NL2SQL |
| KnowledgeGraph | neo4j / nebula | ✓ | 关系推理 |
| Synonym | file_based / http_service | ✓ | 术语扩展 |
| Auth | jwt / oidc | ✓ | 认证权限 |
| Storage | minio / local_fs | ✓ | 文件存储 |

---

## 9. 多库数据一致性体系

### 9.1 一致性挑战

一个文档入库需要写入五个系统（MinIO→MySQL→ES→Milvus→Neo4j），这五次写入不在同一事务中。常见故障场景：进程在 ES 写入后崩溃，Milvus 数据缺失；文档更新时旧版本删除后新版本写入一半失败。

### 9.2 两阶段写入协议

IngestionCoordinator 将写入过程分为两阶段：

**阶段一**（解析/分块/Embedding）：纯计算，无外部写入，失败重试成本低。

**阶段二**（顺序写入）：按依赖顺序逐库写入，每步完成后在 MySQL `WriteCheckpoint` 字段打钩。故障恢复时读取 `WriteCheckpoint`，从第一个 false 的库开始续写，已完成的库不重复写入（ES 和 Milvus 均支持 upsert 语义，重复写入安全）。

### 9.3 软删除版本管理

文档更新（覆盖入库）时不直接删除旧版本：

1. 旧版本标记为 `SUPERSEDED`（数据仍在各库中）
2. 新版本入库（checkpoint 全部完成）
3. 异步清理旧版本（从 ES/Milvus 删除旧 chunk_id，MySQL 级联删除）

这保证了在新版本写入期间，旧版本持续可用，用户不会经历"文档消失"的窗口期。

### 9.4 定时一致性巡检

每小时执行一次巡检（`ConsistencyCheckConfig` 配置）：取最近 100 个 DONE 文档，对比 MySQL 中的 chunk_id 集合与 ES、Milvus 中的实际数据，发现差集则自动补写。巡检发现的不一致数量和修复操作数量写入监控指标，差异超阈值时告警。

---

## 10. 用户界面体系

### 10.1 V2 界面变更概述

**聊天界面新增**：
- 折叠式检索路选择面板（向量/全文/图谱/结构化，开关独立）
- 元数据筛选器（文件类型/时间范围，可选，影响 MySQL 前置过滤）
- "本次对话文档"上传区（临时文档上传，仅本 Session 有效）
- 检索路偏好存 localStorage，跨 Session 记忆

**知识库管理界面新增**：
- 视图切换：文档列表 / 任务列表
- 任务列表：状态机展示，SSE 实时进度（EventSource，非轮询）
- 手动重试按钮（仅 FAILED 状态）
- 质量报告弹窗（高/中/低质量 Chunk 分布，OCR 置信度，告警列表）
- 服务器路径入库选项卡（需配置 server_ingest_root）

**配置界面新增**：
- 资源连接：MySQL 元数据库配置 section
- 新增"通知"Tab：邮件 SMTP 配置和 Webhook 配置

### 10.2 关键交互设计

**检索路选择器的可用性逻辑**：未在系统配置中启用的检索路显示为灰色且不可勾选，并标注"未配置"，避免用户误操作后无法理解为什么选项不生效。

**任务进度的 SSE 实时推送**：知识库界面对进行中的任务订阅 SSE EventSource（`/api/v1/ingest/tasks/{id}/progress`），服务端在每个阶段变更时推送事件，前端实时更新进度条和阶段描述，无需轮询。

**质量报告的可视化**：高/中/低质量 Chunk 以比例条形图展示（非列表），使管理员能快速判断整体质量水平，而不是淹没在数字中。

---

## 11. 安全与权限体系

（与 V1 一致，详见 V1 文档 §9）

---

## 12. 可观测性与运维体系

### 12.1 V2 新增监控指标

在 V1 指标基础上新增：

- `rag_ingest_tasks_total{status}` — 入库任务总量按状态分类
- `rag_ingest_duration_seconds{stage}` — 各入库阶段耗时分布
- `rag_consistency_issues_total` — 一致性巡检发现的问题数量
- `rag_consistency_repairs_total` — 自动修复的数量
- `rag_ephemeral_docs_active` — 当前有效的临时文档数量
- `rag_chunk_quality_score{bucket}` — Chunk 质量分布（高/中/低）

### 12.2 V2 新增 API

在 V1 运维 API 基础上新增：

- `GET /api/v1/admin/consistency-report` — 最近一次巡检结果
- `POST /api/v1/admin/consistency-check` — 手动触发一次巡检
- `GET /api/v1/admin/retrieval-paths` — 系统可用检索路列表

---

## 13. 部署架构与交付模式

### 13.1 V2 新增外部依赖

在 V1 的基础上，V2 新增一个必须对接的外部依赖：

```
系统服务（Docker）
  ├── FastAPI 应用（无状态）
  │
外部依赖（客户自有或新建）
  ├── Redis             ← Session 状态缓存
  ├── Milvus/Qdrant     ← 向量存储
  ├── Elasticsearch     ← 全文索引
  ├── MySQL（元数据库）  ← ⭐ V2 新增，系统自用，独立于客户业务库
  ├── MinIO/对象存储     ← 原文件存储
  ├── Neo4j/NebulaGraph ← 知识图谱（可选）
  ├── MySQL/PG（业务库）← 业务数据（可选，实时查询）
  └── LLM/Embedding     ← 模型推理服务
```

MySQL 元数据库可以与客户的业务数据库是同一个 MySQL 实例（不同的 database），也可以是独立的实例。推荐独立，以避免业务数据库的性能波动影响系统。

### 13.2 V2 现场交付附加步骤

在 V1 七阶段基础上，V2 增加以下步骤：

- 部署 MySQL 元数据库，执行建表 DDL（系统启动时自动执行，无需手工）
- 配置界面填写 MySQL 元数据库连接信息，测试连通
- 若客户有大量文档（>1GB），配置 `server_ingest_root`，使用服务器路径入库
- 配置通知方式（邮件/Webhook），告知客户管理员如何接收入库完成通知

---

## 14. 性能特征与质量基准

### 14.1 V2 入库性能

| 场景 | 吞吐目标 | 说明 |
|------|----------|------|
| 纯文字 PDF（100页） | < 2 分钟 | 不含图片理解 |
| 含图片 PDF（100页，20张图） | < 8 分钟 | 含 VLM 图片理解（4并发） |
| 扫描型 PDF（100页） | < 5 分钟 | OCR 处理 |
| Excel（1万行） | < 1 分钟 | 行列转文字描述 |
| 批量入库（100个文档） | > 50文档/分钟 | 4并发，无 VLM |

### 14.2 V2 延迟影响

元数据前置过滤步骤（MySQL 查询）增加约 10-50ms 延迟（有过滤条件时），无过滤条件时跳过（0ms）。整体端到端延迟目标与 V1 一致（P50 首 token < 1.5s，完整答案 P50 < 5s）。

---

## 15. 各层模块交互总览

```
用户
  │ HTTP/SSE
  ▼
┌───────────────────────────────────────────────────────────────────┐
│  FastAPI 应用层                                                     │
│  AuthMiddleware → 页面路由（Jinja2）│ API 路由（/api/v1/...）      │
└──────────┬────────────────────────────────────────┬───────────────┘
           │  模块A（写侧）                           │  模块B（读侧）
┌──────────▼──────────┐                 ┌────────────▼──────────────┐
│  入库 Pipeline       │                 │  问答 Pipeline             │
│                     │                 │                           │
│  detect_format      │                 │  SecurityFilter           │
│  （magic bytes）     │                 │  QueryUnderstandingStep   │
│  detect_scan_type   │                 │   ├─ EntityLinker         │
│  condition_route    │                 │   ├─ 约束提取下推          │
│  detect_layout      │                 │   └─ 软路由权重计算        │
│  reorder_columns    │                 │  MetadataPreFilter        │
│  extract_toc        │                 │   └─ 显式+隐式条件合并     │
│  outline_build      │                 │  ParallelRetrieval        │
│  figure_label_ext   │                 │   ├─ KW_EXACT  ← 新增     │
│  QualityCheckParse  │                 │   ├─ VECTOR               │
│  SemanticChunking   │                 │   ├─ BM25                 │
│  MetaEnrichment     │                 │   ├─ GRAPH                │
│  QualityCheckChunk  │                 │   ├─ STRUCTURED           │
│  EmbeddingStep      │                 │   └─ EPHEMERAL            │
│  WriteToStores      │                 │  RRFFusion（路由权重加权） │
│  QualityCheckPost   │                 │  SelfEvalStep  ← 新增     │
│  finalize           │                 │   └─ 迭代改写+HyDE        │
└──────────┬──────────┘                 │  CrossEncoderRerank       │
           │                            │  PromptAssembly           │
           │                            │  LLMGenerate              │
           │                            │  FaithfulnessCheck        │
           │                            └────────────┬──────────────┘
           └──────────────────┬─────────────────────┘
                              │ 通过适配器调用
┌─────────────────────────────▼─────────────────────────────────────┐
│  共享内核服务                                                        │
│  IngestionCoordinator（两阶段写入 + WriteCheckpoint + 软删除）       │
│  EphemeralKnowledgeManager（临时文档 TTL 2h）                       │
│  MemoryManager（短期/工作/长期三层记忆）                             │
│  NotificationService（UI/邮件/Webhook）                            │
└─────────────────────────────┬─────────────────────────────────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────────┐
│  适配器层（各客户实现不同，框架调用接口相同）                          │
│  LLM → 推理服务          Embedding → 向量化服务                    │
│  VectorStore → Milvus    FullTextSearch → Elasticsearch           │
│    kw_exact子字段（精确）                                          │
│    text字段（BM25）                                                │
│  MySQLMeta → MySQL元数据库   EntityLinker → 规范名映射             │
│  DocParser → 各格式解析器    KnowledgeGraph → Neo4j/NebulaGraph    │
│  BusinessData → 业务库      Auth → SSO/LDAP    Storage → MinIO    │
└────────────────────────────────────────────────────────────────────┘
           ↑ 贯穿全局
┌──────────┴─────────────────────────────────────────────────────────┐
│  运维控制面                                                          │
│  配置管理（YAML热重载）│ 健康检查 │ 一致性巡检 │ 质量报告 │ 通知     │
│  声明式Workflow注册表  │ 批次任务管理          │ 文档详情页          │
└────────────────────────────────────────────────────────────────────┘
```

### 15.1 关键数据流节点说明

**YAML WorkflowRegistry**（写侧核心）：替代 V2 的 Python 字典，声明式定义各文档类型的处理步骤。`detect_format`（magic bytes）是所有 workflow 的第一步，防止格式伪装。条件分支步骤（`condition_pdf_route`）在运行时动态决定后续步骤序列，消除了 V2 的三条 PDF 变体 Pipeline。

**QueryPlan**（读侧核心）：替代 V2 的 QueryUnderstanding 字符串结果，将用户问题结构化为五个层次（约束/实体/关键词/语义/同义词），各层独立投放到对应检索路，互不干扰。`active_paths` 由 `route` 权重（浮点）动态计算，而非 V2 的布尔开关。

**EntityLinker**（新增）：在 QueryUnderstandingStep 内部运行，将查询实体链接到知识库规范名，使 kw_exact 精确路能命中正确文档。两阶段实现（规则→向量），规则阶段毫秒级，不影响主链路延迟。

**SelfEvalStep**（新增）：位于 RRFFusion 和 CrossEncoderRerank 之间，评估当前融合结果质量，不足时触发第二轮（改写+HyDE），合并结果后再精排。最多 2 轮，普通查询零开销直通。

**IngestBatch**（新增）：所有入库操作（UI上传/本地目录/服务器路径）统一创建批次，子任务挂在批次下。UI 提供批次视图（聚合进度）和任务视图（每文件详情）两级切换。

**make_chunk_id**（替代 uuid4）：确定性哈希生成，同位置同内容的 Chunk id 不变，文档重新入库时 ES/Milvus upsert 精确覆盖，无孤立旧数据，大幅简化一致性保障逻辑。

---

*文档版本 V3 | 对应规格文档：rag_framework_spec_v3.md、nebula_addition.md、ui_spec_v3.md*

*V3 核心合并（来自参考方案）：共享内核分层 · QueryPlan结构化查询 · EntityLinker实体归一化 · 约束硬下推 · kw_exact精确路 · 声明式YAML workflow · magic bytes检测 · TOC交叉验证 · 相邻对准确率 · 确定性chunk_id · 受控迭代SelfEval · 两级任务batch+task · 软路由权重 · 文档详情页*
