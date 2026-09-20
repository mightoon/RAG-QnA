# RAG 智能问答平台（cb_IQA_platform）

企业级检索增强问答平台：多格式文档入库 → 五路混合召回 → 精排 → 流式生成，内置会话记忆、RBAC 权限、临时文档、断点续写与完整可观测性。

**核心设计原则：配置即产品** —— 所有客户化差异通过 `customer/` 目录下的 YAML 配置表达，不修改代码即可完成新客户交付。

---

## 1. 系统架构

### 1.1 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│  API 层  rag/api/          FastAPI + SSE 流式               │
│  auth / chat / documents / ephemeral / admin                │
├─────────────────────────────────────────────────────────────┤
│  编排层                                                     │
│  WorkflowRegistry      流水线组装（workflows.yaml 驱动）     │
│  IngestionCoordinator  入库队列/并发/重试/断点续写           │
├─────────────────────────────────────────────────────────────┤
│  Pipeline 层  rag/pipeline/                                 │
│  入库：parse → outline → chunk → enrich → embed →           │
│        write → verify → finalize                            │
│  查询：security → memory_load → understand → retrieve →     │
│        merge → rerank → generate → faithfulness →           │
│        memory_update                                        │
├─────────────────────────────────────────────────────────────┤
│  服务层  rag/services/                                      │
│  MemoryService（记忆）  EphemeralService（临时文档）         │
│  EntityLinker（实体链接） ProgressBus（进度推送）            │
│  ConsistencyChecker（巡检） NotificationService（通知）     │
├─────────────────────────────────────────────────────────────┤
│  适配器层  rag/adapters/  （11 类适配器 + Registry）        │
│  LLM│Embedding│VectorStore│FullText│Meta│Storage│Graph│     │
│  BusinessData│Synonym│Auth│DocParser                        │
├─────────────────────────────────────────────────────────────┤
│  基础设施                                                   │
│  MySQL（元数据）Redis（会话）Milvus（向量）ES（BM25）        │
│  MinIO（文件）Neo4j（图谱，可选）                            │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 关键设计

| 设计点 | 说明 |
|---|---|
| **适配器模式** | 每类外部依赖定义基类 + 多实现 + `AdapterRegistry`，切换供应商只改配置 `adapter` 字段 |
| **配置驱动流水线** | `workflows.yaml` 声明步骤序列，按名从 `StepRegistry` 实例化，客户可增删步骤 |
| **父子分块** | 子块（≤512 token）参与 ANN 检索，命中后返回父块（≤2000 token）作生成上下文 |
| **两阶段写入** | MySQL（检查点）→ ES → Milvus → Graph，任一环节失败可从检查点续写 |
| **优雅降级** | 图谱/业务数据/精排/Redis 均可选，不可用时自动跳过对应检索路或降级 LLM 精排 |
| **安全下限** | RBAC 角色过滤在检索前置层强制执行，LLM 过滤条件只能收紧不可放宽 |

### 1.3 查询数据流

```
用户提问
  → security        长度/敏感词/注入校验
  → memory_load     短期对话 + 工作摘要 + 实体槽位
  → understand      LLM 生成 QueryPlan（独立问题/意图/实体/时间窗/过滤）
  → retrieve        五路并行召回（每路独立超时 5s + 降级）
       ├─ kw_exact      关键词精确（编号/型号，match_phrase）
       ├─ vector        语义向量（Milvus ANN）
       ├─ bm25          稀疏检索（ES）
       ├─ graph         图谱多跳（Neo4j，可选）
       ├─ structured    业务数据 NL2SQL（可选）
       └─ ephemeral     会话临时文档（优先加权）
  → merge           RRF 融合（k=60）+ 语义去重（0.92）
  → rerank          Cross-Encoder 精排（不可用时 LLM listwise 降级）
  → generate        SSE token 流式推送，引用标注 [文档N]
  → faithfulness    忠实度自评：<0.5 二轮检索，<0.6 拒答
  → memory_update   摘要压缩 / 实体槽位 / 话题链
```

---

## 2. 核心功能

### 2.1 文档入库

- **格式**：PDF（文本/扫描/混合三型自动路由）、Word、Excel、PowerPoint、Markdown、TXT、HTML、图片
- **扫描件**：PaddleOCR（可选安装），OCR 置信度与空页率进入质量报告
- **图片**：VLM 生成描述与题注（多模态）
- **表格**：Excel 原始数据单独入 MySQL `table_data` 表，支持精确数值查询
- **LLM 增强**：批量生成 Chunk 摘要 / 关键词 / 命名实体
- **可靠性**：MD5 秒传、检查点断点续写、指数退避重试（60s/300s/900s）、入库后抽样验证

### 2.2 检索与生成

- 五路混合召回 + RRF 融合 + 语义去重 + Cross-Encoder 精排
- 同义词扩展 + 口语归一（热加载）
- 复合问题分解、指代消解改写
- 忠实度校验：不一致时二轮检索或明确拒答，杜绝编造
- SSE 全链路事件：`start → token → sources → done`

### 2.3 会话与记忆

- 短期对话窗口（10 轮 / 1500 token 预算）+ 滚动工作摘要
- 实体槽位记忆（"张三的部门"跨轮保持）、话题链追踪（切换阈值 0.45）
- 临时文档：会话级上传即问即答（30 秒轻量流水线）、TTL 自动清理、可转正

### 2.4 权限与安全

- RBAC：角色 → 知识库映射，检索前置过滤（ES 与 Milvus 双端强制）
- JWT / OIDC 认证；`dev` 模式免认证（仅开发环境）
- 查询安全校验；服务器路径入库限制在 `server_ingest_root` 内

### 2.5 运维与可观测性

- 结构化 JSON 日志（structlog）
- Prometheus 指标（`/metrics`）：步骤耗时、召回命中率、忠实度分布
- 入库进度 SSE 实时推送（Redis Pub/Sub）
- 一致性巡检：定时抽样核对 MySQL/ES/Milvus，超阈值告警
- 负向反馈分析、批次完成通知（邮件/Webhook）

---

## 3. 目录结构

```
cb_IQA_platform/
├── run.py                        # 启动入口
├── requirements.txt              # Python 依赖
├── customer/                     # ★ 客户配置（交付物，改这里不改代码）
│   ├── customer_config.yaml      #   主配置（模型/存储/检索/RBAC/提示词）
│   ├── workflows.yaml            #   流水线步骤序列
│   ├── synonyms.yaml             #   同义词表（热加载）
│   └── business_schema.yaml      #   NL2SQL 业务表 schema（可选）
├── doc/                          # 设计文档
└── rag/                          # 平台代码
    ├── models.py                 # 全部 Pydantic 数据模型
    ├── container.py              # ServiceContainer 依赖装配
    ├── vector_space.py           # 向量空间指纹（防"同维不同源"向量混库）
    ├── config/                   # 配置模型 + 加载器（${ENV:-default} 插值）
    ├── adapters/                 # 适配器层（11 类 + Registry）
    │   ├── llm.py  embedding.py  vector_store.py  fulltext.py
    │   ├── meta_mysql.py  storage.py  knowledge_graph.py
    │   ├── business_data.py  synonym.py  auth.py  doc_parser.py
    │   └── base.py  registry.py
    ├── pipeline/
    │   ├── base.py               #   PipelineStep / StepRegistry
    │   ├── engine.py             #   WorkflowRegistry
    │   ├── context.py            #   上下文与事件流
    │   └── steps/                #   入库 11 步骤 + 查询 9 步骤
    ├── ingestion/coordinator.py  #   入库协调器（队列/worker/重试/秒传）
    ├── services/                 #   记忆/临时文档/实体链接/进度/巡检/通知
    ├── api/                      #   FastAPI 工厂 + 5 组路由
    └── observability/            #   日志 / 指标
```

---

## 4. 环境准备

### 4.1 组件要求

| 组件 | 版本 | 必需性 | 用途 |
|---|---|---|---|
| Python | ≥ 3.11 | 必需 | 运行时 |
| MySQL | ≥ 8.0 | 必需 | 元数据（文档/任务/批次/反馈/表格） |
| Redis | ≥ 6.0 | 必需 | 会话存储、进度 Pub/Sub |
| Milvus | ≥ 2.4 | 必需 | 向量检索 |
| Elasticsearch | ≥ 8.13 | 必需 | BM25 + 关键词精确 |
| LLM 服务 | OpenAI 兼容 API | 必需 | 生成/改写/理解（vLLM、Ollama 等部署 Qwen） |
| Embedding 服务 | HTTP API | 必需 | bge-m3（1024 维） |
| MinIO | 任意 | 可选 | 原文件存储（可降级 local_fs） |
| Neo4j | ≥ 5.20 | 可选 | 图谱检索路 |
| PaddleOCR | ≥ 2.7 | 可选 | 扫描件 OCR |
| sentence-transformers | ≥ 3.0 | 可选 | 本地精排模型 |

### 4.2 安装依赖

```bash
pip install -r requirements.txt

# 可选重依赖（按需）
pip install paddleocr paddlepaddle            # 扫描件 OCR
pip install sentence-transformers             # 本地 BGE-Reranker 精排
```

### 4.3 Docker 快速拉起基础设施（开发环境）

```bash
docker run -d --name mysql -p 3306:3306 -e MYSQL_ROOT_PASSWORD=rag123 \
  -e MYSQL_DATABASE=rag_meta mysql:8
docker run -d --name redis -p 6379:6379 redis:7
docker run -d --name milvus -p 19530:19530 milvusdb/milvus:v2.4-latest milvus run standalone
docker run -d --name es -p 9200:9200 -e discovery.type=single-node \
  -e xpack.security.enabled=false docker.elastic.co/elasticsearch/elasticsearch:8.13.4
docker run -d --name minio -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=rag -e MINIO_ROOT_PASSWORD=rag123456 \
  minio/minio server /data --console-address ":9001"
```

> MySQL 首次启动自动建表（`auto_create_tables: true`），MinIO 桶自动创建。

---

## 5. 客户配置详解

所有配置集中在 `customer/` 目录。支持环境变量插值：`${VAR}`（未定义保留原样）与 `${VAR:-默认值}`。

### 5.1 customer_config.yaml（主配置）

#### 认证（auth）

```yaml
auth:
  adapter: jwt                     # jwt / oidc / dev
  jwt_secret: ${JWT_SECRET:-change-me-in-production}   # 生产必须更换！
  jwt_algorithm: HS256
  token_expire_hours: 24
  # OIDC 企业单点登录：
  # adapter: oidc
  # oidc_issuer: https://sso.example.com/realms/company
  # oidc_client_id: rag-platform
```

- `jwt`：平台自签发（测试用 `POST /api/auth/token` 签发）
- `oidc`：对接企业 IdP（Keycloak / Casdoor 等），Token 由 IdP 签发
- `dev`：无认证，**仅限开发环境**

#### 大模型（llm）

```yaml
llm:
  adapter: openai_compatible       # openai_compatible / vllm
  base_url: ${LLM_BASE_URL:-http://localhost:11434/v1}
  api_key: ${LLM_API_KEY:-}
  model: qwen2.5-14b-instruct      # 生成主模型（建议 14B+）
  rewrite_model: qwen2.5-7b-instruct   # 改写/意图/忠实度小模型（省时）
  temperature: 0.3
  max_tokens: 2048
  timeout: 60
  max_concurrency: 8               # LLM 并发信号量
```

#### 向量化（embedding）

```yaml
embedding:
  adapter: http_embedding
  base_url: ${EMBEDDING_BASE_URL:-http://localhost:9000}
  model: bge-m3
  dim: 1024                        # 必须与模型输出维度一致
  batch_size: 32
  query_prefix: ""                 # bge 系列留空
```

#### 存储组件

```yaml
vector_store:                      # Milvus
  adapter: milvus
  enabled: true                    # false = 降级（无向量路）
  host: ${MILVUS_HOST:-localhost}
  port: 19530
  collection_prefix: rag_

fulltext:                          # Elasticsearch
  adapter: elasticsearch
  enabled: true
  hosts: [${ES_HOST:-http://localhost:9200}]
  index_prefix: rag_

meta:                              # 元数据库（文档/Chunk/任务/反馈等）
  adapter: mysql                   # 内存替身：memory
  enabled: true
  host: ${MYSQL_HOST:-localhost}
  port: 3306
  user: ${MYSQL_USER:-rag}
  password: ${MYSQL_PASSWORD:-}
  database: rag_meta
  auto_create_tables: true         # 首次启动自动建表

redis:
  host: ${REDIS_HOST:-localhost}
  port: 6379
  session_ttl_hours: 48            # 会话过期时间

storage:                           # 原文件存储
  adapter: minio                   # minio / local_fs
  endpoint: ${MINIO_ENDPOINT:-localhost:9000}
  access_key: ${MINIO_ACCESS_KEY:-}
  secret_key: ${MINIO_SECRET_KEY:-}
  bucket: rag-docs
  # 轻量部署（无 MinIO）：
  # adapter: local_fs
  # local_root: ./data/files
```

> **必需组件**（llm / embedding / meta / auth / storage）连接失败 → 启动报 `CoreDependencyError` 并退出；
> **可选组件**（vector_store / fulltext / graph / business / redis）失败 → 仅告警降级。

#### 可选检索组件

```yaml
knowledge_graph:                   # 启用后自动开启 graph 检索路
  adapter: neo4j
  enabled: false
  uri: bolt://localhost:7687

business_data:                     # 启用后自动开启 structured（NL2SQL）路
  adapter: sqlalchemy
  enabled: false
  dsn: mysql+pymysql://user:pass@localhost:3306/business
  schema_file: customer/business_schema.yaml
  allowed_tables: []               # 空 = schema 全部表
```

#### 检索路开关与参数（retrieval）

```yaml
retrieval:
  enable_kw_exact: true            # 关键词精确（编号/型号必备）
  enable_vector: true
  enable_bm25: true
  enable_graph: false              # 与 knowledge_graph.enabled 联动
  enable_structured: false         # 与 business_data.enabled 联动
  enable_ephemeral: true
  top_k_per_path: 20
  vector_top_k: 10
  bm25_top_k: 10
  rrf_k: 60                        # RRF 融合常数
  rerank_enabled: true
  rerank_model: BAAI/bge-reranker-v2-m3
  rerank_threshold: 0.3
  final_top_n: 6                   # 进入 Prompt 的 Chunk 数
  dedup_similarity: 0.92
  route_timeout_seconds: 5         # 单路召回超时
  self_eval_threshold: 0.5         # 忠实度低于此分二轮检索
  ephemeral_score_boost: 1.2
```

#### Pipeline 行为（pipeline）

```yaml
pipeline:
  enable_vlm: true                 # 图片 VLM 描述
  enable_summary: true             # Chunk 摘要
  enable_faithfulness: true        # 忠实度校验
  faithfulness_threshold: 0.6      # 低于此分拒答
  enable_query_rewrite: true       # 指代消解
  enable_sub_query: true           # 复合问题分解
  total_timeout_seconds: 30
  chunk_parent_max_tokens: 2000    # 父块（生成上下文）
  chunk_child_max_tokens: 512      # 子块（参与检索）
  security_max_query_length: 2000
  security_blocked_words: []
```

#### 会话记忆与临时文档

```yaml
memory:
  short_term_max_turns: 10
  short_term_token_budget: 1500
  working_summary_max_tokens: 400
  topic_switch_threshold: 0.45

ephemeral:
  enabled: true
  ttl_hours: 2                     # 临时文档存活时间
  max_file_size_mb: 50
  max_files_per_session: 5
  target_seconds: 30               # 轻量入库时限
```

#### RBAC 权限（permissions）

```yaml
permissions:
  - role: admin
    collections: ["*"]             # * = 全部知识库
    is_admin: true                 # 管理员（删除/服务器路径入库/管理接口）
  - role: it_staff
    collections: ["it_docs", "faq", "default"]
  - role: employee
    collections: ["faq", "default"]
  - role: guest
    collections: ["faq"]
```

- 用户角色来自 JWT claims（`roles` 字段）
- 检索只召回有权知识库的 Chunk（ES 与 Milvus 双端前置过滤）
- 上传 / 临时文档转正同样受目标知识库权限约束

#### 提示词（prompts）——客户化定制核心入口

```yaml
prompts:
  system_prompt: >-
    你是企业知识库问答助手。严格依据提供的参考文档回答问题，
    在关键陈述后用 [文档N] 标注来源；若参考文档不足以回答，
    明确说明知识库中没有相关信息，不要编造。
  temperature: 0.3
  max_answer_tokens: 2048
```

#### 其他

```yaml
workflows_file: customer/workflows.yaml
default_collection: default
max_upload_mb: 500                 # 单次上传总量上限
server_ingest_root: null           # 服务器路径入库根目录（null = 禁用）
```

### 5.2 workflows.yaml（流水线序列）

按文档类型声明入库步骤序列；查询流水线全局唯一。客户可增删步骤（如关闭 VLM、跳过质量检查）：

```yaml
ingest_workflows:
  default: &default_ingest         # docx/pptx/md/txt/html 复用
    - parse
    - outline
    - chunk
    - enrich
    - embed
    - write
    - verify
    - finalize
  pdf_scanned:                     # 扫描件额外质量检查
    - parse
    - quality_parse
    - ...
  xlsx:                            # Excel 表格数据单独入库
    - parse
    - table_extract
    - ...
  image:                           # 图片走 VLM 描述
    - parse
    - vlm_caption
    - ...

query_workflow:
  - security
  - memory_load
  - understand
  - retrieve
  - merge
  - rerank
  - generate
  - faithfulness
  - memory_update
```

全部可用步骤及说明见该文件头部注释。修改后重启生效。

### 5.3 synonyms.yaml（同义词表）

```yaml
groups:                            # 组内互为同义词（双向扩展）
  - [华为, 华为技术有限公司, HUAWEI]
  - [密码重置, 改密码, 修改密码]

normalize:                         # 口语 → 标准术语（单向归一）
  上不了网: 网络连接故障
```

按 `auto_reload_minutes` 检查 mtime 自动热加载；也可 `POST /api/admin/synonyms/reload` 立即生效。

### 5.4 business_schema.yaml（NL2SQL 表描述）

启用 `business_data` 后，向 LLM 描述可查询的业务表结构（表描述 + 字段类型 + 中文注释），LLM 据此生成 SQL。

---

## 6. 运行应用

### 6.1 启动前检查清单

- [ ] Python ≥ 3.11，`pip install -r requirements.txt` 完成
- [ ] MySQL / Redis / Milvus / Elasticsearch 已启动，配置中连接信息正确
- [ ] LLM 与 Embedding 服务可达（`curl ${LLM_BASE_URL}/models`）
- [ ] 生产环境已设置 `JWT_SECRET`（勿用默认值）
- [ ] `customer/customer_config.yaml` 按客户环境修改完毕

### 6.2 启动命令

```bash
# 默认配置（customer/customer_config.yaml）
python run.py

# 完整参数
python run.py --config customer/customer_config.yaml \
               --host 0.0.0.0 --port 8000 --workers 1

# 开发模式（代码热重载）
python run.py --reload --port 8000

# 演示模式（无任何外部服务，见 6.2.1）
python run.py --noconnection
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--config, -c` | `customer/customer_config.yaml` | 配置文件路径 |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8000` | 监听端口 |
| `--workers` | `1` | worker 数（入库队列按进程独立，建议保持 1） |
| `--reload` | 关 | 开发热重载 |
| `--noconnection, -n` | 关 | 演示模式：不连接任何外部服务 |

#### 6.2.1 演示模式（--noconnection）

**无 LLM / Milvus / Elasticsearch / MySQL / Redis / MinIO 时**，用此模式启动，界面与全部 API 正常可用：

```bash
python run.py --noconnection
```

外部依赖自动替换为本地实现：

| 依赖 | 替换为 | 行为 |
|---|---|---|
| LLM | Mock | 按 task 返回结构合法的演示输出（问答返回演示文案并流式输出） |
| Embedding | Mock | 字符桶确定性向量（相同文本→相同向量，共享字符越多越相似） |
| MySQL 元数据 | 进程内存 | 全部元数据接口可用，**重启即失** |
| 认证 | dev | 免 Token（无 Authorization 头也放行，默认管理员） |
| 对象存储 | local_fs | 文件落本地 `./data/files/` |
| 向量/全文/图谱/业务数据 | 禁用 | 检索路自动降级（`/api/health` 中可见） |
| Redis | 禁用 | 会话记忆降级为进程内存 |

演示模式下可完整走通：上传文档（解析→分块→Mock 向量化→内存元数据写入）→ 任务/批次查询 → SSE 问答 → 会话管理 → Swagger UI（`/docs`）。回答内容由 Mock 生成并明确标注演示模式，仅供界面联调与流程验证。

启动成功标志：

```
INFO  api_started  app=企业智能问答平台  version=3.0.0
INFO  Uvicorn running on http://0.0.0.0:8000
```

核心组件连接失败时启动即退出并给出清晰错误（如 `CoreDependencyError: 核心组件 storage/minio 初始化失败: ...`）。

### 6.3 环境变量（容器化部署）

配置中所有 `${VAR:-默认值}` 均可由环境变量覆盖：

```bash
export JWT_SECRET="prod-secret-xxx"
export LLM_BASE_URL="http://llm-server:8000/v1"
export LLM_API_KEY="sk-xxx"
export EMBEDDING_BASE_URL="http://embed-server:9000"
export MILVUS_HOST="milvus"
export ES_HOST="http://es:9200"
export MYSQL_HOST="mysql" MYSQL_PASSWORD="xxx"
export REDIS_HOST="redis"
export MINIO_ENDPOINT="minio:9000" MINIO_ACCESS_KEY="rag" MINIO_SECRET_KEY="xxx"
python run.py
```

### 6.4 Docker 部署（参考）

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 7. API 接口一览

启动后访问 **Swagger 交互文档**：`http://localhost:8000/docs`。

除系统接口外均需请求头 `Authorization: Bearer <token>`。

### 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（组件可用性 + 已启用检索路） |
| GET | `/metrics` | Prometheus 指标抓取 |

### 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/token` | 测试签发 Token（OIDC 模式下禁用） |
| GET | `/api/auth/me` | 当前用户信息 |

### 对话

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 问答。`stream: true`（默认）SSE 流式；`false` 返回完整 JSON |
| GET / POST | `/api/sessions` | 会话列表 / 新建会话 |
| GET / DELETE | `/api/sessions/{id}` | 会话详情（含消息历史）/ 归档 |
| POST | `/api/feedback` | 消息反馈（up/down + 原因 + 评论） |

`POST /api/chat` 请求体：

```json
{
  "session_id": null,
  "question": "VPN 连接失败怎么处理？",
  "collections": ["it_docs"],
  "stream": true
}
```

SSE 事件序列：`start` → `token`（逐个）→ `sources`（引用列表）→ `done`（含耗时与元信息）；异常时 `error` 事件。

### 文档

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/documents/upload` | 登录 | 多文件上传入库（格式/配额校验） |
| POST | `/api/documents/upload-path` | 管理员 | 服务器路径入库（限 `server_ingest_root`） |
| GET | `/api/documents` | 登录 | 文档列表（按知识库/状态过滤） |
| GET | `/api/documents/{id}` | 登录 | 文档详情 |
| DELETE | `/api/documents/{id}` | 管理员 | 删除文档（五库清理） |
| GET | `/api/documents/{id}/download` | 登录 | 下载原文件 |
| GET | `/api/tasks` `/api/tasks/{id}` | 登录 | 入库任务查询 |
| GET | `/api/batches` `/api/batches/{id}` | 登录 | 批次查询 |
| GET | `/api/progress` | 登录 | 入库进度 SSE（可按 batch_id 过滤） |

### 临时文档

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/sessions/{sid}/ephemeral` | 上传临时文档（即问即答） |
| GET | `/api/sessions/{sid}/ephemeral` | 临时文档列表 |
| POST | `/api/sessions/{sid}/ephemeral/{doc}/promote` | 转正入库 |
| DELETE | `/api/sessions/{sid}/ephemeral/{doc}` | 移除 |

### 管理（均需管理员）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/stats` | 知识库统计 + 组件状态 |
| GET | `/api/admin/workflows` | 流水线结构描述 |
| POST | `/api/admin/consistency/run` | 手动触发一致性巡检 |
| GET | `/api/admin/feedback` | 负向反馈列表（低质召回分析） |
| POST | `/api/admin/synonyms/reload` | 同义词热加载 |

---

## 8. 快速验证

```bash
# 1. 健康检查
curl http://localhost:8000/api/health

# 2. 签发测试 Token
curl -X POST http://localhost:8000/api/auth/token \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","username":"测试员","roles":["admin"],"tenant_id":"default"}'
# → {"access_token":"eyJ...", ...}

# 3. 上传文档入库
curl -X POST http://localhost:8000/api/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@产品手册.pdf" -F "collection=default"

# 4. 查询入库进度（SSE）
curl -N http://localhost:8000/api/progress?batch_id=$BATCH_ID \
  -H "Authorization: Bearer $TOKEN"

# 5. 提问（SSE 流式）
curl -N -X POST http://localhost:8000/api/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question":"VPN 连接失败怎么处理？"}'
```

---

## 9. 常见问题

**Q: 启动报 `CoreDependencyError: 核心组件 xxx 初始化失败`？**
必需组件（LLM/Embedding/MySQL/Storage）连不上。检查对应服务是否启动、配置连接信息与环境变量。无 MinIO 时可切换 `storage.adapter: local_fs` 轻量部署。

**Q: 扫描版 PDF 解析出来是空的？**
需安装可选依赖 `pip install paddleocr paddlepaddle`，并确认 `pipeline.enable_vlm: true` 且 LLM 服务支持图片输入（VLM 题注）。

**Q: 精排没生效？**
默认精排走 LLM listwise 降级方案；要启用本地 Cross-Encoder 精排需 `pip install sentence-transformers` 并确认 `rerank_model` 可下载。

**Q: 如何新增一个知识库（collection）？**
无需建库：上传时指定 `collection` 参数即可自动创建；在 `permissions` 中为角色授权访问。

**Q: 修改了同义词多久生效？**
按 `synonym.auto_reload_minutes`（默认 30 分钟）自动热加载，或调管理接口立即生效；其余配置修改需重启服务。

**Q: 如何对接企业 SSO？**
`auth.adapter: oidc` 并填写 `oidc_issuer` / `oidc_client_id`，用户角色从 IdP Token claims 的 `roles` 字段读取，与 `permissions` 中的角色名对齐即可。

**Q: 忠实度校验总是拒答？**
先确认知识库中确有相关文档；若文档充分仍拒答，可下调 `pipeline.faithfulness_threshold`（默认 0.6）或 `retrieval.self_eval_threshold`（默认 0.5）。

