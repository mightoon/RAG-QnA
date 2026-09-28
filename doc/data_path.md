# 数据通路说明：各存储存什么、按什么顺序写入、靠什么字段关联

> 本文回答三个问题：**一次文档上传，MinIO / MySQL / Elasticsearch / Milvus（+可选 Neo4j）各存了什么？按什么顺序、以什么粒度写进去的？这些存储之间靠哪些字段互相关联？**
>
> 与既有文档的关系：存储职责分工见 `doc/architecture.md` §2.2（`doc/architecture.md:105-119`），写入顺序设计见 §5.8（`:375-390`），两阶段写入协议见 §9.2（`:689-712`）。本文是**代码实现口径**的落地说明：每条结论都标了代码位置，并以一次真实入库（`Generative+UI.pdf`，22 页，`doc_236c329d885b4d65`）的实测数据做样例。
>
> 代码基线：工作区当前版本（`rag/` 下各文件）。实测样例的库地址：MySQL `192.168.100.239:3306/rag_meta`、ES `192.168.100.239:9200`、Milvus `192.168.100.239:19530`、MinIO `192.168.100.239:9000`。

---

## 目录

1. [总览：一次上传的数据通路](#1-总览一次上传的数据通路)
2. [各存储的职责与内容](#2-各存储的职责与内容)
3. [写入顺序与检查点](#3-写入顺序与检查点) — 含 **§3.12 处理逻辑：一份文档如何变成块**
4. [跨库关联字段](#4-跨库关联字段)
5. [读路径如何把四库串起来](#5-读路径如何把四库串起来)
6. [实测样例：一次真实上传的完整数据](#6-实测样例一次真实上传的完整数据)
7. [删除、重传与修复路径](#7-删除重传与修复路径)
8. [已知偏差与风险](#8-已知偏差与风险)
9. [自查命令](#9-自查命令)
10. [代码位置索引](#10-代码位置索引)

---

## 1. 总览：一次上传的数据通路

```
上传（HTTP）/ 服务器路径导入
        │  rag/api/routes/documents.py:51-107   （落到系统临时目录）
        ▼
IngestionCoordinator.submit()          rag/ingestion/coordinator.py:70-157
        │  · ingest_batches 行 → 逐文件算 MD5 → MD5 秒传判定
        │  · documents 行（doc_id 此时确定）→ ingest_tasks 行 → 入队
        ▼
Worker._process()                       rag/ingestion/coordinator.py:173-244
        │
   ① MinIO    原始文件字节        checkpoint.minio   coordinator.py:181-201
        │
        │  按 YAML 工作流跑 Pipeline：detect_format → layout_parse → parse → …
        ▼
   ② MySQL    documents / chunks_meta / table_data     checkpoint.mysql   ingest_write.py:206-217
        ▼
   ③ ES       全文索引（全部 chunk，含父块）            checkpoint.es      ingest_write.py:220-230
        ▼
   ④ Milvus   向量索引（仅子块）                        checkpoint.milvus  ingest_write.py:232-262
        ▼
   ⑤ Neo4j    实体/关系（可选，当前关闭）               checkpoint.graph   ingest_write.py:264-273
        ▼
   verify（抽样核对 ES/Milvus 存在性）                  ingest_write.py:279-309
        ▼
   finalize（写回 documents.status / quality_report、tasks.quality_summary）
                                                        ingest_write.py:312-373
```

写入过程中还夹着**重跑清理**：MySQL/ES/Milvus 各自写成功后，删掉"上次留下、这次没再
产出"的旧块（`WriteStep._prune_stale`，见 §3.11）。

三个要点先立在这里，后文展开：

- **写的是"块"，不是"文档"**：除 MinIO 存原始文件、`documents`/`ingest_tasks` 存文档级状态外，MySQL/ES/Milvus 承载的都是 **chunk**（块）。
- **一条主键贯穿：`doc_id` + `chunk_id`**。`chunk_id` 是**确定性**的（内容 + 位置哈希），因此 ES/Milvus 的写入天然幂等（重复入库是覆盖，不是追加）。
- **顺序不可调换**：`chunk_id` 必须先落到 MySQL，ES/Milvus 才有可靠的寻址依据；`storage_url` 必须先由 MinIO 确定，引用出链才有目标。

---

## 2. 各存储的职责与内容

### 2.1 职责分工

| 存储 | 存什么 | 粒度 | 服务什么场景 |
|---|---|---|---|
| **MinIO**（`rag-docs`） | 原始文件二进制，**只有它一份真身** | 文件 | 原文预览、答案溯源、重建索引时的文件来源 |
| **MySQL**（`rag_meta`） | 文档元数据、chunk 元数据**含正文**、表格结构化行、任务/批次状态 | 文档 + 块 + 表格 | 元数据前置过滤、任务管理、父子回补、一致性巡检、修复的数据源 |
| **Elasticsearch** | chunk 全文索引 + 摘要（高权重）+ 关键词 | 块 | BM25 关键词检索、精确匹配（`content.kw_exact` / `keywords`） |
| **Milvus** | chunk 向量 + 一份可展示的元数据副本 | 块（**不含父块**） | 语义相似度检索（ANN） |
| **Neo4j / NebulaGraph** | 实体节点与关系三元组 | 实体 | 多跳关系推理（`knowledge_graph.enabled: false` 时不写） |

**元数据库的特殊定位**（`doc/architecture.md:117`）：MySQL 不是一条检索路，而是**检索前的过滤层**——先用 `tenant_id / collection / allowed_roles / section_path` 把 `chunk_id` 收窄成白名单，ES/Milvus 在这个白名单范围内检索。

### 2.2 MinIO 存什么

- **对象 key**：`{tenant_id}/{doc_id}/{filename}`，由 `coordinator.py:193-194` 拼接；返回的 `storage_url` 形如 `s3://rag-docs/default/doc_236c329d885b4d65/Generative+UI.pdf`。
- **写入**：`rag/adapters/storage.py:52-64`（`put_object`；bucket 不存在时自动 `make_bucket`）。
- **只存原始字节，不存派生对象**：图区裁片、VLM 输入图片都在内存里用完即弃（`storage.put` 全仓只有 `coordinator.py:193` 一个调用点）。所以 MinIO 里一篇文档**只有 1 个对象**。
- **对象 ETag = 文件 MD5**，这就是与 MySQL `documents.file_md5` 的连接点（实测：`22654822c947aac93886f798b2c7681b`）。

### 2.3 MySQL 存什么

四张表参与上传通路（DDL 见 `rag/adapters/meta_mysql.py:123-229`）：

**① `documents`（文档级，1 行/文档）** — `meta_mysql.py:124-147`

| 字段 | 说明 |
|---|---|
| `doc_id` | 主键，`doc_<16hex>`；由 `task_id` 派生（`task_xxx` → `doc_xxx`，`coordinator.py:97`），全链路不再变化 |
| `tenant_id` / `collection` | 租户与逻辑集合（也是物理索引/集合名的来源） |
| `filename` / `file_type` / `file_size` / `file_md5` | 文件身份；`file_md5` 有 `idx_md5(file_md5, tenant_id)`，用于秒传与去重 |
| `storage_url` | 指向 MinIO 对象，**只有这张表与 Milvus 行持有** |
| `language` / `page_count` / `chunk_count` | 解析结果概要 |
| `status` | `pending / parsing / chunking / embedding / writing / done / partial / failed / retrying / superseded / deleted`（`rag/models.py:64-96`）。**文档行只停在两处**：提交时 `pending`、finalize 时 `done/partial`；中间阶段全在 `ingest_tasks.status` 上（见 §8.26）。`deleted` = 在回收站（见 §8.27） |
| `allowed_roles` | JSON 数组，数据层权限 |
| `quality_report` | JSON，质量报告（含 `ingest_fingerprint` 解析口径指纹） |
| `deleted_at` / `prev_status` | 回收站专用：移入回收站的时间（列表按它倒序）与**删除前的状态**（恢复时原样还原）。存量库由 `_ensure_tables` 里的 ALTER 补齐（`meta_mysql.py:296-306`） |
| `created_by` / `created_at` / `updated_at` | 审计字段 |

**② `chunks_meta`（块级，1 行/chunk）** — `meta_mysql.py:149-170`

`chunk_id`(PK) / `doc_id`(FK→documents, ON DELETE CASCADE) / `tenant_id` / `collection` / **`seq`（块在文档内的构建序号 —— 决定展示顺序；存量行为 NULL，见 §8.29）** / `chunk_type` / `is_parent` / **`parent_chunk_id`** / `page_num`（起始页） / **`page_end`（结束页：跨页小节据此显示"第 2–3 页"，见 §8.32）** / `section_path` / `quality_score` / `token_count` / `allowed_roles` / `figure_label` / `figure_caption` / **`figure_bbox`（图区在页面上的 PDF point 框，JSON 文本 `[x0,y0,x1,y1]`；见 §8.36）** / **`text`（块正文全文）** / `created_at`。

- 字段是怎么算出来的（分块与页归属的完整口径）见 **§3.12**。

- **`text` 是块正文的唯一权威副本**：父子回补（`query_retrieve.py:808-827`）与一致性巡检修复（`consistency.py:121-160`）都从这里取真文。
- **没有** `summary` / `keywords` 列——这两样只在 ES。
- 写入语句是 `INSERT ... ON DUPLICATE KEY UPDATE`（只更新 5 个非键字段）+ 一条单独的 `UPDATE ... SET text=`（避免单次参数过大），批大小 500（`meta_mysql.py:407-450`）。

**③ `table_data`（表格结构化行，1 行/表）** — `meta_mysql.py:172-186`

`id`(自增 PK) / `chunk_id` / `doc_id` / `tenant_id` / `table_index` / `page_num` / `headers`(JSON) / `row_data`(JSON) / `row_count` / `numeric_stats`(JSON) / `created_at`。

- 写入语义是**"按 doc 整体替换"**：同一事务内先 `DELETE FROM table_data WHERE doc_id IN (...)` 再插入（`meta_mysql.py` 的 `upsert_table_data`）。这不是"顺手删一下"，而是因为该表**没有可用的自然唯一键**：`table_index` 在 Excel 分支里是 200 行切片的编号（同一 sheet 的多个切片会重复）、`chunk_id` 在"表格块没生成"时是空串——没有唯一键就无法 `ON DUPLICATE KEY UPDATE`，重跑只会叠加出重复行。
- `chunk_id` 由分块阶段按**元素身份**回填（`TableExtractStep` 在元素上写 `raw_data["table_data_index"]`，`ChunkStep` 按下标回填；见 §8.1）。

**④ `ingest_tasks` / `ingest_batches`（任务级）** — `meta_mysql.py:188-229`

任务行含 `checkpoint`(JSON)、`status`、`retry_count`、`total_chunks` / `written_chunks`、`quality_summary`、`error_message`、`started_at` / `completed_at`。**`checkpoint` 是断点续写的唯一依据**（`rag/models.py:172-178`）：

```json
{"minio": true, "mysql": true, "es": true, "milvus": true, "graph": false}
```

### 2.4 Elasticsearch 存什么

- **索引名** = `index_prefix` + `collection` = `rag_` + `default` = `rag_default`（`fulltext.py:177-181`）。
- **mapping**（`fulltext.py:192-229`）显式声明 16 个字段（实测索引共 17 个，多出的 `figure_caption` 由动态映射补上）；其中 `content` 是 `text`+`ik_max_word`，并带一个不分词的 `content.kw_exact`（`keyword`，`ignore_above: 512`）；`summary` 检索权重 1.5，`keywords` 是 `keyword` 数组。
- **实际写入的文档体**（`fulltext.py` 的 `upsert_chunks`，`_id = chunk_id`，`refresh="wait_for"`）：

  `chunk_id / doc_id / tenant_id / collection / chunk_type / content / summary / keywords / section_path / page_num / figure_label / figure_caption / allowed_roles / quality_score` + **文档级三项 `filename / file_type / created_at`**（由调用方通过 `doc={...}` 传入，见 §8.2）

- **写入范围是全部 chunk（含父块）**：`WriteStep` 传的是 `ctx.chunks` 全量，不做 `is_parent` 过滤。
- **ES 独有的内容**：`summary`（LLM 摘要）、`keywords`（关键词/专有名词）、`content.kw_exact` 子字段。MySQL 与 Milvus 都没有这三样。

### 2.5 Milvus 存什么

- **集合名** = `collection_prefix` + `collection` = `rag_default`（`vector_store.py:252` + `_full_name` `:281`）。
- **schema 15 个字段**（`vector_store.py:170-195`）：

  `chunk_id`(VARCHAR PK,64) / `embedding`(FLOAT_VECTOR, 配置维度 1024) / `doc_id` / `tenant_id` / `collection` / `chunk_type` / `text`(8192) / **`title`** / `section_path` / `page_num` / `figure_label` / `figure_caption` / **`storage_url`**(2048) / `quality_score` / `allowed_roles`(逗号拼串)

- **写入**：`upsert`（`vector_store.py:344-355`），行由 `build_row` 归一化（`None` → `""`/`0`/`1.0`，`allowed_roles` 列表 → 逗号串，`:221-238`）。
- **只写子块**：`EmbedStep` 跳过 `is_parent`（`ingest_write.py:151`），`WriteStep` 也只为有向量的 chunk 建元数据（`:237-252`）。所以 **Milvus 行数 = 非父块数**。
- **`text` 截断 4000 字符**（`ingest_write.py:243`），且是**未加摘要/关键词的原始块文本**（`_embed_text` 里拼的关键词+摘要只用于算向量，不入库）。
- **Milvus 独有的字段**：`embedding`、`title`。**没有** `parent_chunk_id`、`summary`、`keywords`。
- 索引：`HNSW`，`metric_type=IP`，`M=16`，`efConstruction=200`（实测 `describe_index`）。

### 2.6 Neo4j 存什么

`WriteStep` 在 `ctx.entities` 非空且 `graph` 适配器可用时写实体与共现关系（`ingest_write.py:264-273`），失败只记 warning、**不影响文档状态**。本环境 `knowledge_graph.enabled: false`，实测 `checkpoint.graph = false`，写入为 0。

---

## 3. 写入顺序与检查点

### 3.1 提交阶段（写 MySQL 的文档/任务行）

`coordinator.submit()` 有**两种内容重复策略**，由入口决定：

| 入口 | `dedup` | 命中"内容已入库"时的行为 |
|---|---|---|
| 网页上传 `POST /api/documents/upload` | `ask`（默认） | **什么都不写**，返回待确认清单 → 弹框 → 确认后才入库（见 §3.1.1） |
| 上传框里显式选"直接覆盖/直接新建" | `auto` / `force` | 秒传复用 / 一律当新文档 |
| 服务器路径导入 `upload-path` | `auto` | 秒传复用（管理员批量，没有人守着弹框） |
| 会话临时文档 | `auto` | 秒传复用 |

流程（`coordinator.py`）：

1. `save_batch()` → `ingest_batches` 1 行（`status=running`）。
2. 逐文件：读内容 → `file_md5(content)` → `find_doc_by_md5(md5, tenant_id)`。
   - **同一文件的判定只看内容 MD5**（`file_md5`）：文件名、作者、修改时间等属性都不参与 ——
     它们是元数据，改个名不该被当成"另一份文件"。代价是"重新导出/另存"后的文件
     即便肉眼内容一致，字节变了就算不同文件（要覆盖这种场景需要另建"正文指纹"，
     见 §8.18 待决策）。
   - **MD5 秒传**（`dedup="auto"`）：同租户已有 `status='done'` 的同 MD5 文档，且**同一集合**（物理索引/集合名 = 前缀 + `collection`，跨集合复用会得到"文档 done 但目标集合里一条块都没有"）、**权限视图一致**（`allowed_roles` 集合相同）、**解析口径一致**（`ingest_fingerprint` 相同，含 `CHUNK_BUILD_VERSION`）→ 只写 `documents`（复用 `storage_url`、`status=done`）+ `ingest_tasks`（`deduplicated=True`），**不入队、不碰 ES/Milvus/MinIO**。这种任务在界面上标「秒传」并写明"未重新解析、未写入任何库"。
   - **待用户确认**（`dedup="ask"`，网页上传默认）：只要**这个集合里已有同内容文档**就
     先把决定权交给用户，**本次不写任何库**（不建文档、不建任务）。返回的候选里带上
     已有文档的信息：`doc_id`（若命中的是秒传别名，已解析成物理源文档）、
     `existing_filename`、`chunk_count`/`page_count`、`version`、`uploaded_at`、
     `engine_changed`（解析口径是否已变）等。
   - 否则：同名不同 MD5 的旧版本先被标 `superseded`（软删除，见 §7.2）。
3. `upsert_document(doc)` → `documents` 1 行（**`doc_id` 在此确定**）。
4. `save_task(task)` → `ingest_tasks` 1 行（`status=pending`，`checkpoint` 全 false）。
5. 入队，由 worker 消费。

### 3.1.1 重复文件的确认流程（上传即问）

```
前端拖入文件
   │ POST /api/documents/upload（multipart，duplicate_action 缺省=ask）
   ▼
服务端落临时目录 → 逐文件算内容 MD5 → 已在**本集合**存在？
   ├─ 否 → 正常入库，返回 {batch_id, tasks}
   └─ 是 → 不建文档/不建任务，返回 {duplicates:[…], staging_token, expires_in:1800}
             │  字节**只传这一次**，留在临时目录里等确认
             ▼
        前端弹框（每个重复文件一行）：
          重新入库（覆盖原文档） | 作为新文档入库 | 跳过
             │
             ├─ 确认 → POST /api/documents/upload/confirm
             │        {staging_token, decisions:{"<md5>": "reingest|new|skip"}}
             │        · reingest → 沿用原 doc_id 重跑（清旧块、写新块，不产生重复），
             │                     角色等按**本次上传**的取值覆盖，版本号 +1，
             │                     source_type=reupload
             │        · new      → force 新建一篇文档（同内容两套块，慎用）
             │        · skip     → 丢弃这份（删临时文件）
             └─ 取消 → POST /api/documents/upload/discard（清临时文件，零写入）
```

要点：

- **判定在服务端做**：前端不实现 MD5（浏览器 WebCrypto 没有 MD5），也不重复传一遍字节 ——
  文件先落到临时目录，服务端算 MD5，`staging_token` 指向这批文件，确认时直接从本地入库。
- **暂存有超时**（`STAGED_TTL_SEC = 1800s`）：超时未确认即连同临时文件一起丢弃，
  避免大文件长期占盘；`token` 一次性，确认或丢弃后立即失效。
- **未给出决定的文件按 `skip` 处理**：宁可什么都不做，也不替用户猜。
- **"作为新文档"会把同一份内容入库两次**：块内容相同但 `chunk_id` 不同（含 `doc_id`），
  检索可能重复命中；弹框里对此有明确警示。
- 判定命中的是**秒传别名**时，弹框与后续重建都指向**物理源文档**（见 §8.17），
  否则用户会看到一篇"没有内容的文档"。

### 3.2 阶段 ①：MinIO（`checkpoint.minio`）

`coordinator.py:181-201`。worker 拿到任务后**第一件事**就是把原文件写进对象存储：

```
文件不可得 → 先尝试 _restore_from_storage() 从对象存储拉回（断点恢复）
put(f"{tenant_id}/{doc_id}/{filename}") → 返回 storage_url
→ doc.storage_url = url → upsert_document() → task.checkpoint.minio = True → save_task()
失败 → _retry_or_fail("对象存储上传失败")（可重试，退避 60/300/900s）
```

**为什么必须最先**：`storage_url` 是"块 → 原文"引用链的锚点，也是后续重跑/修复时唯一能拿回原始文件的地方（`reingest_document` 与 `_restore_from_storage` 都依赖它）。

### 3.3 阶段 ②：MySQL（`checkpoint.mysql`）— 必须成功

```
if not cp.mysql:
    texts = {chunk_id: 块正文}                  # 正文一并落 MySQL
    await meta.upsert_chunks(ctx.chunks, texts=texts)     # chunks_meta（含父块）
    await meta.upsert_table_data(ctx.tables)              # table_data（按 doc 整体替换）
    cp.mysql = True; save_task(); task.written_chunks = len(ctx.chunks)
    await self._prune_stale(ctx)                          # 清掉"上次留下、这次没再产出"的旧块
```

- 这是**阶段一（必须成功）**：失败即抛异常上溯，任务走重试/失败，**不进入 ES/Milvus**。
- 写入是幂等的：`chunk_id` 同 → `ON DUPLICATE KEY UPDATE` 覆盖。

### 3.4 阶段 ③：Elasticsearch（`checkpoint.es`）— 可降级

`ingest_write.py:220-230`：`bulk` 写入全部 chunk，**单库失败不中断**，只记 `ctx.add_warning` + 一条 `QualityIssue(code="es_write_failed", severity="high", action="partial")`，`cp.es` 保持 false。

### 3.5 阶段 ④：Milvus（`checkpoint.milvus`）— 可降级

`ingest_write.py:232-262`：仅当 `ctx.embeddings` 非空（`embed` 步骤成功且未被向量空间门禁拦下）时写，元数据逐块组装后 `upsert`；失败记 `milvus_write_failed`。

> 向量空间门禁：若该集合已有另一套向量空间的向量，`EmbedStep` 会**有意跳过**（`ingest_write.py:142-148`），此时 `ctx.embeddings` 为空 → 完全不碰 Milvus，且"没写"与"写失败"可区分。

### 3.6 阶段 ⑤：Neo4j（`checkpoint.graph`）— 可选

`ingest_write.py:264-273`：失败只 warning，不产生 `QualityIssue`，**不影响状态**。

### 3.7 verify：抽样核对

`ingest_write.py:279-309`：从**子块**里随机抽 `ingest.verify_sample_size`（默认 6）个 `chunk_id`，与该文档在 ES / Milvus 中的实际 id 集合比对，缺则记 warning。只在对应 `checkpoint` 为 true 时才查。

### 3.8 finalize：状态与质量报告落库

`ingest_write.py:312-373`：

- 统计高/中/低质块数；
- **状态判定**：只要存在 `code` 以 `_write_failed` 结尾的 issue（即 ES 或 Milvus 写失败）→ `partial`（`ctx.doc.status`），否则 `done`。质量备注（如"摘要生成成功率低"）**不会**把文档判成 partial（`:338-344`）；
- 写回 `documents.quality_report`（含 `ingest_fingerprint`）、`status`、`chunk_count`（`upsert_document`）与 `ingest_tasks.quality_summary`、`status`（`save_task`）；
- worker 收尾：`task.completed_at`、批次重算、通知、清临时文件（`coordinator.py:227-244`）。

### 3.9 顺序为什么不可调换

| 依赖 | 原因 |
|---|---|
| MySQL 先于 ES/Milvus | `chunk_id` 是全文/向量检索的唯一寻址键；巡检修复、父子回补也以 MySQL 为真文来源。MySQL 没写成功就写检索库，会产生"检索得到、元数据查不到"的孤儿块 |
| MinIO 先于全部 | `storage_url` 是引用出链与断点恢复的锚点 |
| ES 在 Milvus 前 | 二者互不依赖，顺序固定只是为了日志/检查点便于排查；都失败时 `partial` 语义一致 |
| verify/finalize 最后 | 需要前三库的实际写入结果（checkpoint）才能判断 |

### 3.10 失败与降级矩阵

| 库 | 失败后果 | 任务/文档状态 | 是否自动重试 |
|---|---|---|---|
| MinIO | 无法继续（流程中止） | `retrying` → 超限后 `failed` | ✅ 退避 60/300/900s，`max_retries=3` |
| MySQL | 无法继续（阶段一必须成功） | 同上 | ✅ |
| ES | 记 `es_write_failed`；BM25 路不可用 | `partial`，可降级使用 | ❌ 由定时巡检（`consistency_check.interval_hours`）补写 |
| Milvus | 记 `milvus_write_failed`；向量路不可用 | `partial` | ❌ 同上 |
| Neo4j | 仅 warning | 不影响（仍 `done`） | ❌ |

### 3.11 写入之后的清理（重跑场景）

`chunk_id` 是确定性的，所以重跑（reingest / 改分块参数后重入库）只会**覆盖**同 id 的块；
块数变少、顺序变化或内容变化时，旧块会留在三库里成为"检索命中得到、元数据查不到"的孤儿。
`WriteStep._prune_stale()` 处理这件事：

```
MySQL 写成功后：stale = list_chunk_ids(doc) − 本次产出的 chunk_id 集合
                → meta.delete_chunks(stale)          # 三库共用同一批 id
ES   写成功后：fulltext.delete_by_ids(stale)
Milvus 写成功后：vector.delete_by_ids(stale)
```

**顺序很关键**：先算差集再删，且每个库的删除都放在**它自己写成功之后** ——
新块没写进去就不会删旧块，也不会出现"删了旧的、新的还没落"。清理失败只记 warning
（旧块残留比"整篇文档判失败"轻），并且会往任务备注里写一条"N 个旧块已清理"。

### 3.12 处理逻辑：一份文档如何变成块（分块 + 页归属）

写库之前，文档先经过四步变成"块"。这一节是**算法口径**（历史上几处坑都在这里，
详见 §8.29–§8.32）：

```
① 解析（parse）      PDF/图片 → ParsedElement 序列（**版面引擎的阅读顺序**）
                     每个元素带 page_num（引擎的全局页序，1 起）与 block_label
        ▼
② 大纲（outline）    标题层级栈 → 每个元素写 section_path（"第3章/3.1节"）
                     · 标题里的 ASCII 斜杠换成全角（9/22、src/app/page.tsx）
                     · 引擎不给层级 → 按标题编号推断（5.1→2 级、3.2.1→3 级），
                       护栏：同一主号出现≥2 次才算，且只加深不改浅
        ▼
③ 分块（chunk）      按 section_path **变化**切"节"，一节产出一组父块+子块：
                     · 父块 = 节内 TEXT/TITLE 顺序拼接（"\n" 分隔）；
                       超过 chunk_parent_max_tokens(2000) 按段落切多个父块
                     · 子块 = 父块正文的**滑窗**（≤ chunk_child_max_tokens 512），
                       短于 min_chunk_tokens(20) 的窗口丢弃
                     · 表格块整表一块（不切分），并按**元素身份**回填 table_data 下标
                     · 同文档近似重复（正文前 200 字符指纹）跳过 —— **表格豁免**
        ▼
④ 标注（emit）       每块写：chunk_id / seq / page_num / page_end / section_path /
                     类型 / 质量分（详见下表）
```

**每个块的字段是怎么定的**

| 字段 | 怎么来 | 说明 |
|---|---|---|
| `chunk_id` | `sha256(tenant:doc:seq:内容前若干字符)[:32]` | **确定性**：同 seq 同内容 → 同 id → 重跑是同 id 覆盖，不产生重复块 |
| `seq` | 构建序号（0 起，父块先于其子块） | 决定**展示顺序**（`list_chunk_ids` 按它排）。历史缺陷：它只进过 chunk_id 的哈希、从不落库 ⇒ 页面按哈希序显示 |
| `page_num` | 该块**起始**位置所在元素的页 | 起始偏移用 `_el_at(ranges, offset, fallback)` 精确映射回元素，而不是"整节都记节首页"（那是修过的老 bug：13 个可校验块里 8 个页码偏小 1–3 页） |
| `page_end` | 该块**末尾**位置所在元素的页 | 一节常跨页，只有起始页时无法如实表达"这一节排到第 2 页"；取不到时退回起始页 |
| `section_path` | 标题栈拼接（最多 4 级，截断 512 字符） | 检索前置过滤支持 `section_path LIKE 'kw%'` |
| `chunk_type` / `is_parent` | 产出它的元素类型（text/table/image_caption/code/parent） | 父块不进向量库；表格/图题算高质量块 |

**父子两级在写入与检索里的角色**

- 写入：`WriteStep` 传的是 `ctx.chunks` **全量（含父块）** → MySQL 与 ES 都有父块；
  `EmbedStep` 跳过 `is_parent` → **Milvus 只有子块**（实测 `Milvus = 非父块 = 46`，
  `ES = MySQL = 69`）。
- 检索：命中子块后按 `parent_chunk_id` 取回**整节**喂给生成
  （`RerankStep._fill_parent_content`，父块与子块正文相同时跳过）；
  同一步顺带从 MySQL 补 `page_end`，让引用能写"第 3–4 页"。
- 因此**父子正文天然重叠**（子块是父块的一部分）。真冗余只有一种：一节只切出
  1 个子块且与父块逐字相同 —— 实测三篇文档里这种父块各为 0 个。

**页归属的两级粒度**（回答"按页看内容"时容易误会的地方）

| 粒度 | 谁在用 | 口径 |
|---|---|---|
| 块级 | 详情页的块列表、引用来源、提示词里的来源定位 | `page_num`（起）/ `page_end`（止），**块不按页切**，所以一块可能跨页 |
| 页组级 | 详情页「内容预览」的页标记 | 把"起始页相同的一组块"求该组真正排到第几页，相邻组区间可以重叠（如实反映小节跨页） |

**存量数据怎么补 `seq` / `page_end`**：两列都是后加的，存量行为 NULL。`seq` 可以用
**ES 的 `_seq_no` 写入序**补齐（入库时 ES 按 `ctx.chunks` 顺序 bulk 写入，所以
`_seq_no` 升序 == 构建序；这一条先被"新入库一篇、逐个比对 ES 序与 MySQL seq 序"
验证过，完全一致）。`page_end` 只能重解析补齐。

> 这一节讲的是"文字怎么变成块"。**图 / 表格 / 公式**那三类非文字内容怎么进库
> （内嵌位图、矢量图、扫描页整页位图、表格 HTML、公式区域，以及各自的实测数据与
> 未处理项）另见 **`doc/pdf_process.md`**。

---

## 4. 跨库关联字段

### 4.1 连接键矩阵

| 字段 | MinIO | MySQL | ES | Milvus | 作用 |
|---|:--:|:--:|:--:|:--:|---|
| `doc_id` | 路径段 | `documents` PK、`chunks_meta` FK、`table_data`、`ingest_tasks` | `keyword` | `varchar` | **文档级全局连接键** |
| `chunk_id` | — | `chunks_meta` PK、`table_data` | `_id` + `_source.chunk_id` | **PK** | **块级全局连接键** |
| `tenant_id` | 路径段 | 各表列 | `keyword` | `varchar` | 租户隔离、权限过滤 |
| `collection` | — | 各表列 | `keyword` | `varchar` | 逻辑集合；物理索引/集合名的来源 |
| `file_md5` | ETag（相等） | `documents`、`ingest_tasks` | — | — | 文件身份、秒传去重；与 MinIO 的连接点 |
| `storage_url` | （key 本身） | `documents` | — | 每行 | 块 → 原文的引用出链 |
| `parent_chunk_id` | — | `chunks_meta` | — | — | 子块 → 父块，**只有 MySQL 有** |
| `page_num` | — | ✅ | ✅ | ✅ | 页码溯源 |
| `section_path` | — | ✅ | ✅ | ✅ | 章节路径（层级） |
| `figure_label` / `figure_caption` | — | ✅ | ✅ | ✅ | 图表题注 |
| `allowed_roles` | — | ✅(JSON) | ✅(array) | ✅(逗号串) | 数据层权限过滤（形态各异，语义一致） |
| `quality_score` | — | ✅ | ✅ | ✅ | 质量分 |
| `summary` / `keywords` | — | ❌ | ✅ | ❌ | 增强字段，仅 ES |
| `embedding` | — | ❌ | ❌ | ✅ | 向量 |
| `text` | — | ✅（全文，权威） | ✅（`content`） | ✅（截断 4000） | 块正文 |

### 4.2 逐个说明

**`doc_id`** —— 文档级连接键。生成于 `coordinator.py:97`（`task_id.replace("task","doc")`），此后作为：`documents.doc_id`(PK)、`chunks_meta.doc_id`(外键，`ON DELETE CASCADE`)、`table_data.doc_id`、`ingest_tasks.doc_id`、ES `doc_id`(keyword)、Milvus `doc_id`、MinIO 路径第二段。**一次"删文档"的动作就是按它横扫各库**。

**`chunk_id`** —— 块级连接键，**确定性生成**（`rag/models.py:27-35`）：

```
content_hash = sha256(块正文)[:8]
chunk_id     = sha256(f"{tenant_id}:{doc_id}:{seq}:{content_hash}")[:32]
```

生成点在 `ingest_chunk.py:115`。三个直接后果：

1. **幂等写入**：同文档重跑，同位置同内容产生同 id → ES 用 `_id=chunk_id` 覆盖、Milvus 用 PK upsert 覆盖、MySQL `ON DUPLICATE KEY UPDATE`，不会产生重复块。
2. **`doc_id` 变了就是另一批块**：所以"重建索引"必须沿用原 `doc_id`（`coordinator.py:413-424` 有专门注释说明）。
3. **内容或顺序一变，id 就变**：旧 id 不会被覆盖，只会留下孤儿（见 §8.6）。

**`tenant_id` + `collection`** —— 双重作用：① 三库统一的多租户/多集合过滤条件（ES `_base_filter` `fulltext.py:258-279`；Milvus `_build_expr` `vector_store.py:405-428`；MySQL 白名单 `meta_mysql.py:498-573`）；② 物理名来源（`rag_default`）。注意 MinIO 路径用的是 `tenant_id`，**不是** `collection`（本例两者同为 `default`，容易误读）。

**`file_md5`** —— 只有 MySQL 有，但**值等于 MinIO 对象的 ETag**。它是"原始文件 ↔ 元数据"的连接点，也是秒传判据（`find_doc_by_md5` 要求 `status='done'`）。

**`storage_url`** —— `documents.storage_url` 与 Milvus 每行都有；**ES 没有**。因此：从向量路召回的块能直接出原文预览链接，只从 BM25 路召回的块拿不到（`query_generate.py:60-66` 组引用时就带这个字段）。

**`parent_chunk_id`** —— 父子关系的**唯一出处**。父块是一段完整小节，子块是它的滑窗切片（`ingest_chunk.py:153-160`）。检索命中子块后，靠 `_fill_parent_content`（`query_retrieve.py:808-827`）回 MySQL 取父块正文填 `parent_content`，再喂给生成模型。ES/Milvus 都不带这个字段，**所以父子回补必须能访问 MySQL**。

**溯源四件套（`page_num`/`section_path`/`figure_label`/`figure_caption`）** —— 三库都有，用于引用定位与按章节过滤。注意 ES 的 `figure_caption` 是动态映射出来的 `text`（不在 `ensure_index` 的显式 mapping 里）。

### 4.3 用一条块数据把关系串起来

以 `chunk_id = 170c20529d8f8fa31405954993632ebe` 为例：

| 存储 | 该块的呈现 | 关联键 |
|---|---|---|
| MySQL `chunks_meta` | `chunk_type=text, is_parent=0, parent_chunk_id=68e3632adfd28c82cbce5e3b95bcdcb2, page_num=6, section_path=5RelatedWork, token_count=296, text`(1033 字) | `chunk_id`(PK)、`doc_id`、`parent_chunk_id` |
| ES `rag_default` | `_id = 同一个 chunk_id`，`content` 同文本，另有 `summary`/`keywords` | `_id`/`chunk_id`、`doc_id` |
| Milvus `rag_default` | 主键 = 同一个 `chunk_id`，`embedding`(1024 维)，`title=Generative+UI.pdf`，`storage_url=s3://…` | `chunk_id`(PK)、`doc_id`、`storage_url` |
| MinIO | 不含块；`storage_url` 指向的对象里有这段原文 | `storage_url` ↔ 路径中的 `doc_id` |

---

## 5. 读路径如何把四库串起来

```
用户提问
  ▼ 查询理解 / 路由
MySQL 白名单（可选前置过滤）      meta_mysql.query_chunk_ids：tenant/collection/file_type/日期/allowed_roles/section_path
  ▼
多路并行召回
  ├─ ES   BM25（content 权重 1.0 / summary 1.5）＋ kw_exact / keywords 精确匹配   → RetrievedChunk(chunk_id, content…)
  └─ Milvus ANN（HNSW/IP）                                                        → RetrievedChunk(chunk_id, text, storage_url…)
  ▼ RRF 融合 → Cross-Encoder 重排
MySQL 父子回补   按 chunk_id 取 chunks_meta.parent_chunk_id → 取父块 text 填 parent_content
  ▼ Prompt 组装（父块正文优先）
生成答案 + 引用来源（title / section_path / page_num / figure_label / preview_url(storage_url)）
```

两个实现细节值得记住：

- **`storage_url` 只在 Milvus 结果里有**（ES 不存该字段）→ 引用里的"原文预览"链接依赖向量路命中。
- **`title`（文件名）同理**：Milvus 行的 `title` 来自 `ctx.doc.filename`；ES 侧的 `fulltext.py:311` 读的是 `s.get("filename")`，而该字段从未写入 → **BM25 单独召回的引用没有文件名**（见 §8.2）。

---

## 6. 实测样例：一次真实上传的完整数据

> ⚠ 本节是**修复前**（旧代码）的一次真实入库，保留作为"问题现象"的原始证据；
> 各条问题的修复状态与修复后的实测数字见 **§8**。

样例：`Generative+UI.pdf`（4,371,532 B，22 页，文本层完好），`doc_id=doc_236c329d885b4d65`，`task_id=task_236c329d885b4d65`，`batch_id=batch_812cc5d0a44b4c4b`，`2026-09-25 07:53:58 → 07:55:35`（97s），`status=done`。

### 6.1 各库实际落库量

| 存储 | 实际内容 | 数量 |
|---|---|---|
| MinIO | `default/doc_236c329d885b4d65/Generative+UI.pdf` | **1 个对象**，ETag `22654822c947aac93886f798b2c7681b` |
| MySQL `documents` | 文档行（`page_count=22`、`chunk_count=66`、`status=done`） | 1 行 |
| MySQL `chunks_meta` | parent 21 + text 20 + image_caption 18 + table 7 | 66 行 |
| MySQL `table_data` | 8 行（**仅 7 个不同 chunk_id**） | 8 行 |
| MySQL `ingest_tasks` | checkpoint `{minio:true, mysql:true, es:true, milvus:true, graph:false}` | 1 行 |
| ES `rag_default` | 全部 66 块（含 21 父块），正文合计 76,660 字符 | 66 条 |
| Milvus `rag_default` | 仅非父块 45 行，正文合计 33,723 字符（= MySQL 子块字符数，逐个相等） | 45 行 |
| Neo4j | 未写（`enabled: false`） | 0 |

### 6.2 一致性实测（跨库主键比对）

| 比对 | 结果 |
|---|---|
| ES `_id`/`_source.chunk_id` 集合 vs `chunks_meta.chunk_id` 集合 | **完全相等**（66 = 66） |
| Milvus 主键集合 vs `chunks_meta` 非父块集合 | **完全相等**（45 = 45），与父块交集 0 |
| Milvus ⊆ ES，差集 21 条 | 全是 `parent` 型（符合设计） |
| `table_data.chunk_id` | 7 个全部落在 MySQL/ES 的 7 个 `table` 型块上 |

### 6.3 质量备注（`ingest_tasks.quality_summary`）

```json
{"high": 43, "medium": 2, "low": 0, "doc_type": "text", "used_ocr_pages": 0,
 "warnings": ["1 处图区未裁出（坐标不可换算或无对应图像），这些图不会被图片理解描述",
              "标题与 PDF 书签匹配率低 (0/21)，大纲可能不完整",
              "摘要生成成功率低 (16/45)"]}
```

"摘要生成成功率低 (16/45)"与 §8.3 的观察一致：45 个非父块里只有 16 块拿到 LLM 摘要。

---

## 7. 删除、重传与修复路径

### 7.1 永久删除（`rag/web/routes.py:1923-1946`）

顺序与写入相反，**先删检索库、再删存储与元数据**：

```
ES delete_by_doc(collection, doc_id)
→ Milvus delete_by_doc(collection, doc_id)
→ Neo4j delete_by_doc(tenant_id, doc_id)
→ MinIO delete(storage_url)
→ MySQL delete_document(doc_id, tenant_id)
     ├─ 显式删除 table_data（该表没有外键，无法级联）
     └─ 删除 documents 行 → chunks_meta 走外键级联
```

⚠️ 历史坑（已修）：`table_data` 原先既不删也没有外键，永久删除后结构化行会永久残留。

### 7.2 软删除与覆盖上传

同名不同 MD5 的旧版本在提交阶段被标 `superseded`（`coordinator.py:261-281`），**物理清理延迟到新版本写入成功后**异步执行（`_cleanup_superseded`，`:283-306`，删除顺序 Milvus → ES → MinIO → MySQL）。

### 7.3 重建索引（reingest，`coordinator.py:413-463`）

- 从 MinIO 拉回原文件到临时目录；
- 新建任务但**沿用原 `doc_id`**、`checkpoint.minio = True`（原文件已在对象存储）、**不走 MD5 秒传**（否则会直接判 done 什么都不做）；
- 意义：换了版面引擎/分块参数/上次解析质量差时，重跑解析链而内容不变 → 同 `seq` 同内容 → 同 `chunk_id` → 三库 upsert 覆盖。

### 7.4 定时一致性巡检与修复（`rag/services/consistency.py`）

- 抽查：MySQL 的**子块** id 集合 vs Milvus 实际 id 集合，缺失 >10% 判为不一致（注释说明了"父块不算"的原因）；
- 修复：`_repair_doc` 从 MySQL 取 `chunks_meta` + `text`，**重写 ES 与 Milvus**。正文缺失的行会被跳过（宁可缺失也不写占位符）。
- 修复时先调 `get_doc_enrichment()` 把 ES 里已有的 `summary/keywords` 读回来再写（历史坑：原先传 `None`/`[]`，一次巡检就把这些增强字段永久抹掉 —— MySQL 没有它们的副本）。

---

## 8. 已知偏差、修复状态与残余风险

以下均为**实测确认**的实现偏差（不是设计意图），按影响面排序。每条标注当前状态：
`已修复` = 代码已改并跑过回归；`待决策` = 需要产品/设计取舍；`残余` = 已知但不打算改。

### 8.1 表格块与 `table_data` 的配对 —— 已修复

- 原现象：本例 8 行 `table_data` 只有 7 个不同 `chunk_id`——p13 的 `table_index=6` 与 `table_index=7` 两张表**共用** `c5878fbdf527cc08055cc30a51795240`（该 chunk 的实际内容是 table 6）。
- 根因（已定位到确切触发点）：`emit()` 的"前 200 字符指纹去重"把 p5 与 p13 的两张 Method 对比表判成重复（**实测两者指纹完全相同**），第 8 张表没有产出块；而回填用的是"第几个 TABLE 元素 + `chunks[-1].chunk_id`"，于是错位一格、共用上一张表的 id。
- 修复：① `TableExtractStep` 在元素上写 `raw_data["table_data_index"]`，`ChunkStep` 按该下标精确回填（没产出块就留空，绝不顶替）；② 表格块**不再参与**文本指纹去重；③ `upsert_table_data` 改为"按 doc 整体替换"（同事务先删后插），重跑不再叠加。
- 回归实测：表格块 8 个、`table_data` 8 行 / **8 个不同 chunk_id**、每个 id 都指向真实表格块。

### 8.2 ES 漏写 `filename` / `file_type` / `created_at` —— 已修复

- 原现象：mapping 声明了这三个字段，但 `upsert_chunks` 的文档体不含它们——实测 `value_count` 全为 0。
- 影响面（修复前）：
  1. 引用来源：`fulltext.py` 读 `s.get("filename")` → `title=None`，**BM25 单独召回的引用没有文件名**；
  2. Kibana：数据视图 `rag_default` 的时间字段正是 `created_at`，Discover 时间过滤把**所有文档全部滤掉**（`range: created_at` 放到 2000–2100 也是 0 命中），看起来"ES 里没有内容"。
- 修复：写入口新增 `doc={filename, file_type, created_at}`（`created_at` 为带 `Z` 的 ISO8601），由 `WriteStep._doc_meta()` 从 `DocumentMeta` 组装；一致性巡检补写时同样带上。
- 回归实测：67/67 条都有这三个字段，`created_at` 范围查询命中 67/67。
- ⚠ **存量数据仍需回填**（或按文档重建索引）：修复只对新写入/重跑生效。

### 8.3 `summary` / `keywords` 稀疏 —— 已修复（可见化 + 产出率）

- 原现象：45 个非父块只有 16 块有摘要；`except Exception:` 静默降级成正则关键词；降级关键词是 `['Introduction','powerful','tools','are','often']` 这类噪声。
- **真正的根因（两次定位，第二次才是关键）**：
  1. 第一轮：推理模型的思考过程会先吃掉 `max_tokens`，`content` 变空；而适配器
     `_extract_reply` 在 `content` 为空时**把 `reasoning_content`（思考）当回复返回** ——
     于是下游拿到的是一段"我在想该怎么回答…"的独白，`json.loads` 失败 →
     `enrich_llm_failed`；或者解析出垃圾关键词。日志文案还把它报成
     "模型返回了合法 JSON 但 summary 为空"，与真实原因不符。
  2. 第二轮（拿到真实日志样本后）：那些样本正是模型的思考原文
     （`我们需要回答用户要求：分析文本块，输出 JSON…`），且 `finish_reason=length` ——
     **思考把 512 的额度吃满了**，答案还没开始写。实测某块思考 1383 字符、
     另一块 4925 字符（连 2048 都不够）。
- 修复：
  1. **思考绝不当答案**（`_extract_reply`）：`content` 为空就返回空串，并新增
     `_reasoning_len()` 让 `llm_empty_reply` 能报出"思考占了额度"这一成因。
     （顺带修掉一个更隐蔽的问题：VLM 图片描述此前可能把思考写进去当描述。）
  2. **JSON 模式**：`generate()` 新增 `response_format`，增强调用传
     `{"type":"json_object"}`，由服务端保证 `content` 是合法 JSON；服务端不支持时
     **端点级记住并自动去掉该参数重试**（只告警一次，之后不再发送，避免每次多一次往返）。
  3. **预算阶梯按配置上限展开**：增强调用按
     `(512, 1500) → (2048, 1500) → (llm.max_tokens, 800)`
     （max_tokens, 送入文本上限）逐级升级，成功后立即停止；最后一级就是**用户配置的上限**
     —— 配置写 65536 就该用上（详见 §8.22）。也试过"截短输入"这条捷径：
     2048 + 截到 600 字**仍然失败**（那次思考 7178 字符，
     说明思考长度与输入长度无关），所以最后一级是"更大预算 + 适度截短"。
  4. `_extract_json` 全部候选都解析不了时返回 `{}`（而不是返回那个破候选），
     免得把"模型没给 JSON"误报成"解析异常"。
  5. 降级关键词改为"型号 → 专有名词 → 中文 2-gram → 长词"且滤停用词；LLM 给的
     关键词也过同一份停用词表。
- 回归实测：
  - 修复前（用户的一次真实入库）：46 个非父块里 24 个"无摘要" + 1 个 JSON 解析失败；
  - 修复后（同一批真实块跑阶梯，`llm.max_tokens=65536`）：**8/8 都拿到摘要**，
    0 例"返回非 JSON"，0 例"思考吃满"。
  - 桩级单测覆盖 6 个场景（首次成功/升级后成功/三级全空/返回散文/缺字段/低成功率进报告）：
    `tmp_selftest/t_enrich_ladder.py`。
- 残余：父块按设计不生成摘要（`is_parent` 直接走正则关键词）；是否换非推理模型由你定 ——
  阶梯已把"思考吃额度"这条路走到头，日志会带上预算与思考字符数供判断。


### 8.4 `content.kw_exact` 对长块失效 —— 待决策

- `content.kw_exact` 是 `keyword` + `ignore_above: 512`，而块正文普遍长于 512 字符（本例 66 条中 38 条超限）→ 这些块的该子字段**根本不建索引**（响应里标记 `_ignored: ["content.kw_exact"]`）。
- 更根本的问题：该字段把**整段正文**当一个 keyword 存，短查询词做 `term` 查询在语义上不可能命中，`search_exact` 实际只能靠 `keywords` 字段兜底。
- 需要决策：把 `kw_exact` 改成"专有名词/型号抽取"字段，还是取消这条路径（连带 `retrieval.enable_kw_exact`）。

### 8.5 检索库与元数据库的字段不对称 —— 部分已修复

| 缺失 | 后果 | 状态 |
|---|---|---|
| Milvus 无 `parent_chunk_id` | 父子回补必须访问 MySQL，无法离线自洽 | 残余（可接受） |
| ES 无 `storage_url` | 只走 BM25 召回的块无法直接出原文预览链接 | 残余（可接受） |
| MySQL 无 `summary` / `keywords` | 巡检修复会清空 ES 的摘要/关键词（§7.4），且这两样**没有第二份副本** | **已修复**：补写前先从 ES 读回 `get_doc_enrichment()` 再写回 |
| ES 有父块、Milvus 无父块 | 检索口径不一致：BM25 可直接召回父块，向量路只能召回子块再回补 | 待决策 |

### 8.6 reingest 不清理旧块 —— 已修复

- 原现象：`reingest_document` 只依赖 `chunk_id` 幂等覆盖，重跑后块数减少/顺序变化时旧块会留在三库里。
- 修复：`WriteStep._prune_stale()` 在 MySQL 写成功后算出"库里 − 本次产出"的差集并删除，再在 ES/Milvus 各自写成功后删同一批 id（`delete_by_ids`）。判据是差集，因此**新块没写进去就不会删旧块**。
- 回归实测：注入一个僵尸块后重跑，`prune_stale_chunks keep=67 removed=19 stale=19`，MySQL/ES/Milvus 三库同步清干净，三库计数一致。

### 8.7 `table_data` 会在永久删除后残留 —— 已修复

- 原现象：`delete_document` 只删 `documents`，`chunks_meta` 走外键级联，而 `table_data` **没有外键**（存量表结构也改不动）→ 结构化行永久残留。
- 修复：`delete_document` 在同一事务里先删 `table_data`；内存兜底适配器同样清理。
- 回归实测：删除后 `table_data` 残留 0 行。

### 8.8 子块正文丢失换行/句后空格（英文单词粘连）—— 已修复

- 原现象：`re.split(r"(?<=[。！？；!?;])\s*|\n", text)` 把分隔符当分隔符吃掉、再用 `"".join` 拼回 → 实测 20 个 text 块**无一例外**没有换行，英文单词直接粘连（`a\nlong-standing` → `along-standing`、`just a` → `justa`）。
- 修复：分隔符随句携带、拼回时按两侧是否 CJK 决定补空格（`_sentences_keep_seps` / `_join_sents`）。
- 回归实测：`workbuilds` / `along-standing` / `customvisual` 等粘连片段全部消失。
- 残余（来源侧）：引擎自己的 `block_content` 偶有无空格拼接（实测 p7 原文即 `for any prompt.We show`）——强行补空格会误伤 `iPhone`/`eBay` 这类词，故不做启发式。

### 8.9 `page_num` 是"节级"口径 —— 已修复

- 原现象：父块与子块都用 `section_buf[0]` 当元素传进 `emit`，跨页小节整节的块都记成节首页（实测 13 个可校验 text 块里 8 个页码偏小 1–3 页）。
- 修复：`_split_parent` / `_split_child` 返回 `(文本, 起始偏移)`，按偏移把块映射回它**真正所属的元素**再取页码。
- 回归实测：可校验 text 块 **一致 11 / 不一致 2 / 未匹配 7**（修复前 5 / 8 / 7）；剩余 2 例是跨页续写段落（内容被 pdfplumber 按页切开，块起始落在前页末尾）。

### 8.10 图区裁剪完全失效 —— 已修复

- 原现象：`_engine_page_px` 在页结果里找 `page_size`/`width`/`height` 等键，而**实测这些键一个都不存在** → 换算倍率恒为 None → 永不裁图 → 图区理解整条链静默失效（只剩一条 `layout_figure_crop_incomplete` 警告）。
- 修复：引擎栅格页尺寸的**唯一实测来源**是响应顶层的 `result.dataInfo.pages[i] = {width, height}`（实测 1224×1584，对应 PDF 612×792 → 恰好 2.0 倍，即 144 DPI）。`DocParseClient` 现在把它盖章到每页结果的 `_engine_px` 上，`_engine_page_px` 首选该键。顺带更正了代码注释里"约 82 DPI / 1.19 倍 / 2.93 倍"的陈旧估值。
- 回归实测：第 3 页那张矢量图（修复前 18 张图里唯一没有描述的）裁出 8056 字节 PNG，`figure_region_cropped` 进入质量报告，**18/18 图片全部有 VLM 描述**。

### 8.11 表格块正文是原始 HTML —— 已修复

- 原现象：7 个表格块 2701 字符里 1730 字符是标签（64%），"某表某列是多少"在 ES/向量库里没有可命中的自然语言文本（结构化行只在 MySQL，而检索路不查 `table_data`）。
- 修复：`TableExtractStep` 把表格块正文改写成 `第N行：列=值；…`（与 Excel 分支同一格式），原始 HTML 保留在 `raw_data["table_html"]`；Excel 的 200 行切片带 `metadata.row_range`，跳过改写以保绝对行号。
- 回归实测：8 个表格块正文 0 个 HTML 标签，全部含 `第1行：`。

### 8.12 ES 批量写入部分失败只记 warning —— 已修复

- 原现象：`bulk` 返回 `errors=true` 时只打一条 `es_bulk_partial_error`，文档仍是 `done`、质量报告不提 → "悄悄少了几块"。
- 修复：解析失败明细并抛 `RuntimeError`，由 `WriteStep` 记成 `es_write_failed` → 文档转 `partial`。

### 8.13 同文件跨集合上传被误判秒传（本次新发现）—— 已修复

- 现象：MD5 秒传只比 `tenant_id`，不比 `collection`。同一份文件传到另一个集合时"秒传成功"（`status=done`、`chunk_count` 照抄源文档），但目标集合里**一条块都没有**——检索永远命中不到，界面上看不出异常。
- 修复：秒传条件加一条"同一集合"（`coordinator.submit`），不满足则记 `md5_dedup_collection_changed` 日志并按新集合正常入库。

### 8.14 入库队列无界 —— 待决策（P2）

`coordinator.py` 用 `asyncio.Queue()`（maxsize=0），上传数千文件时无背压；前端队列视图依赖 `queue.maxsize`，因此上限提示永远为空。

### 8.15 改了"块生成逻辑"却仍被秒传放行 —— 已修复

- 现象（真实踩到）：把 P0/P1 的 12 项修复上线后，用户**重新拖入同一份 PDF** 上传，界面"一瞬间"变成完成，数据库里什么都没变。原因是 MD5 秒传：同一租户 + 同一集合 + `allowed_roles` 一致 + **`ingest_fingerprint` 一致** → 直接判 `done`，不入队、不写任何库。
- 根因：指纹只覆盖"外部引擎口径"（`embed_text` 拼法、`doc_parse` 服务地址与批大小、`vlm`/`embedding` 的 base_url+model），**没有覆盖"块正文/元数据是怎么生成的"**。于是改了分块切分、表格块正文、页码归属、图区裁剪这些**确实会改变入库内容**的逻辑之后，指纹不变 → 秒传照旧放行 → 索引里还是旧逻辑产出的块，而界面显示"已完成"。
- 修复：`rag/models.py` 新增 `CHUNK_BUILD_VERSION` 并纳入指纹（`_ingest_fingerprint`）。规则写进常量注释：**只要改动会改变入库内容，就必须 +1**。本次为 `v2`（v1 = 修复前的块生成逻辑）。
- 效果：升级后再次上传同一文件 → 指纹不一致 → 记 `md5_dedup_engine_changed` 并**真正重新入库**（不会再"秒传空转"）。
- 回归实测：`CHUNK_BUILD_VERSION=v2` 的指纹 `c41864e8c52b26f4` 与 `v1` 的 `9d00df6eeda187a6` 不同 ✓（单测 `tmp_selftest/t_dedup_fixes.py`）。

### 8.16 秒传完成在界面上无法区分 —— 已修复

- 现象：秒传任务 `created_at == completed_at`、checkpoint 全 false，但任务列表只显示"完成"，用户无从判断"到底写没写库"。
- 修复：`_task_view` / `_task_brief` 暴露 `dedup` 标记与说明文案；任务列表在状态旁显示「秒传」徽标并在文件名下写明"同文件已入库过（MD5 秒传）：本次未重新解析、未写入任何库；要按当前解析口径重跑请用文档列表里的「重新入库」"；上传完成后的 toast 也会提示"其中 N 个是已入库过的同一文件（秒传…）"。

### 8.17 对秒传别名执行「重新入库」会在同一集合写出重复内容 —— 已修复

- 现象：秒传产生的是**别名文档**（没有自己的块，`chunk_count` 照抄源文档）。若对别名执行重建，会以别名 `doc_id` 重新产出一整套同内容的块，而源文档那套仍在同一集合里 → 检索命中双份（同内容、不同 `chunk_id`）。
- 修复：`reingest_document` 识别 `quality_report.deduplicated` + `source_doc_id`，把重建目标**改到物理源文档**（记 `reingest_retarget_alias` 日志）；源文档不存在时拒绝重建（记 `reingest_alias_source_missing`），不产生"半个 pending 状态"。
- 回归实测：别名请求 → 任务落在源文档、`update_doc_status` 只动源文档、别名保持 `done`；源文档缺失时返回 `None` 且不改任何状态。

### 8.18 重复上传的确认闸门 —— 已实现（网页上传默认）

见 §3.1.1 的完整流程。要点：网页上传默认 `dedup="ask"` —— 内容已在本集合存在时
**零写入**并弹出确认框，用户选「重新入库 / 作为新文档 / 跳过」后才真正入库；
暂存 30 分钟超时、token 一次性、取消即清临时文件。

- 回归实测（真实环境跑那份 PDF）：返回 1 条待确认项（
  `doc_id=doc_236c329d885b4d65`、`alias_doc_id=doc_f05625d1074f455c`、
  `engine_changed=true`），`documents/chunks_meta/table_data` 行数**完全没变**，
  取消后暂存文件被清理 ✓（`tmp_selftest/t_duplicate_live.py`）。
- 桩级单测覆盖 8 个场景（ask 零写入 / reingest 沿用原 doc_id 并更新角色 / new 新建 /
  skip / 取消 / auto 老行为 / 超时清理 / 版本号自增）：`tmp_selftest/t_duplicate_flow.py`。

### 8.19 "同一文件"的判定粒度 —— 待决策

当前判定是**字节级 MD5**（`file_md5`，与秒传共用同一份口径）：改名、改属性都不影响，
判定精确、零成本（本来就要算）。它的盲区是"**内容看着一样、字节不一样**"：
PDF 重新导出会写入新的 producer/创建时间，Word 另存会重排 zip，扫描件重扫更是不一样。

要覆盖这种场景，需要另建一个"**正文指纹**"（例如：解析后正文的 SHA-256，或
"页数 + 每页文本归一化后的哈希"），代价是：判定必须发生在**解析之后**，也就是
"先花一次解析成本才知道是不是重复"。可选方案（未实施）：

- 上传后在**解析阶段**算正文指纹，命中已有文档则**停下并询问**（比字节级晚一步，
  但能抓"另存过"的文件）；
- 或者两级：字节 MD5 先拦一道（零成本），正文指纹在解析后补一道。

需要你决定是否做、以及用哪种指纹口径。

### 8.20 确认弹框的返回值恒为"跳过" —— 已修复

- 现象（用户真实踩到）：上传同一文件、在弹框里选「重新入库」并点确认，界面一瞬间就结束、
  什么都没重新解析。库里只有一行"待确认"用的批次，没有任务、没有新文档。
- 根因：前端弹框用 Promise 等用户选择，而模态框的 `close()` 会回调 `onClose`
  （见 `partials.js` 的 `buildModal`）。原实现把 `onClose` 写成 `resolve('skip')`，
  按钮处理函数里又是先 `m.close()` 再 `resolve(选中项)` —— **Promise 只认第一次
  settle**，于是 `close()` 触发的那个 `resolve('skip')` 永远赢，用户选什么都是"跳过"
  （连「取消本次上传」也退化成 skip，因此 discard 也没被调用）。
- 修复：加 `settled` 标志，`decide()` 只允许 settle 一次；按钮里先 `decide(...)`
  再 `m.close()`。
- 回归实测：`tmp_selftest/t_dialog.mjs` 用最小 DOM 桩直接驱动**真实的** `askDuplicate`
  ——修复前「重新入库/作为新文档/取消」三项全错（都返回 skip），修复后四项全对。
- 教训：这类"只在浏览器里出现"的 Promise/回调顺序问题，用桩驱动真实函数最划算，
  比人工点一遍可靠得多。

### 8.21 整批都是重复文件时留下"幽灵批次" —— 已修复

- 现象：上传的文件**全部**命中"内容已入库、等确认"时，一个任务都不会产生，
  但 `submit()` 已经先写了一行 `ingest_batches`（`total=0, status=running`）——
  它永远不会被推进，界面上永远是"进行中"。用户两次尝试因此留下两行幽灵批次。
- 修复：批次行**推迟到确实有任务要入队时**再落库（`_ensure_batch()`）；没有任何任务时
  不建批次行，接口也不返回 `batch_id`（返回一个查不到的 id 只会误导调用方）。
- 回归实测：真实环境跑 `dedup="ask"` 的全重复上传，`ingest_batches` 行数不变 ✓。

### 8.22 配置里的 `max_tokens` 被调用点的硬编码小值挡掉 —— 已修复

- 现象（用户提出的疑问）：配置里 `llm.max_tokens = 65536`，摘要却频繁报
  "思考把 token 吃光"。**配置值根本没被用上**。
- 根因：适配器的取值规则是 `max_tokens or config.max_tokens`，而增强步骤**显式传了
  512**（写死的），显式值优先 → 用户的 65536 成了摆设。同类隐患还在另外几处：
  视觉探针 `max_tokens=200`、忠实度自评 `max_tokens=10`。
  （顺带纠正一个说法：我之前贴的那次"content 260 字 / reasoning 1323 字"是**成功**
  的调用，用来说明两个字段并存；真正被吃光的是日志里那些 `finish_reason=length` 的调用。）
- 修复（三条一起）：
  1. **阶梯按配置上限展开**：`enrich_ladder(llm.max_tokens)` —— 最后一级就是配置值
     （本例 `[(512,1500), (2048,1500), (65536,800)]`）。推理模型写完思考就停，
     把上限放开不会让正常块多烧 token，只让"思考特别长"的块有机会把答案写出来。
     配置比基础两级还小时**尊重配置**、阶梯自动收缩。
  2. **推理端点自动加思考额度**：适配器按**真实响应**（出现 `reasoning_content`）判定
     "这是推理端点"，此后任何显式 `max_tokens` 都会额外获得 `+2048` 思考额度
     （上限仍是配置值）。实测：显式 512 → 实际发送 2560；不传 → 65536。
     这样其它调用点（自评 512、意图 600、画像 300…）也不会再被思考静默饿死。
  3. 视觉探针与自评的预算同时上调，并改用"能不能读出探测图上的文字"作为判定
     （见 §8.23）。
- 回归实测：配置 `llm.max_tokens=65536` 下跑 8 个真实块，**8/8 拿到摘要**；
  每级实际预算在 warning（`llm_empty_reply`）与 `generate_ex()` 的返回值里可见。
  **"探到推理端点/探到关思考的那一招"不再打日志**（用户要求）：它们是成功路径上的
  实现细节，而且本进程的 structlog 没有级别过滤（见 §8.28），降成 debug 也照样会打。
  要看结论就读 `generate_ex()` 的 `no_think` 字段，或本节的实测表。

### 8.23 严格化带来的两处连带影响 —— 已修复

把"思考不当答案"落地后，靠"有没有拿到文本"判成功的两个地方会误判，一并修掉：

- **视觉探针**（`container.probe_vision`）：原来问"这张图什么颜色"、预算 200、
  只要有文本就算通过。推理模型把 200 花在思考上 → 正文为空 → **探针判失败 →
  整条图片理解链路被"不可用"**（静默丢掉所有图片描述）。现在改为让它
  **复述探针图上画的文字**（图上固定写着 `PROBE OK 0123456789`），判定时
  `content + reasoning` 一起看（正文为空但思考里读出了文字，同样说明它真看见了），
  预算 1024；失败信息也区分"有回复但读不出图"与"完全没有回复"。
- **忠实度自评**（`query_generate._evaluate`）：`max_tokens=10` 在推理模型上必然
  只回思考 → 解析不到小数 → 恒返回 1.0 → 自评与二轮检索静默失效。提到 512
  （再叠加思考额度），并保留"评估失败不阻塞"的兜底语义。

### 8.24 摘要这类任务不该让模型思考 —— 已实现"按任务关思考"

- 问题：`summary` / `keywords` / 意图分类 / 忠实度评估 / 看图写描述都是**短且结构化**
  的任务，推理模型在这里"想半天"只有坏处：慢、贵、思考把额度吃光还会让正文为空。
  实测同一块、同一预算（512）：
  | | 用时 | 思考 | 正文 | 结果 |
  |---|---|---|---|---|
  | 允许思考 | 3.0s | 1217 字符 | **0** | `finish_reason=length`，摘要丢失 |
  | 关闭思考 | 0.7s | 0 | 397 字符 | 正常拿到摘要 |
  → **快 4 倍，且从"拿不到"变成"稳定拿到"**。
  另外 DeepSeek 官方文档写明：思考模式下 `temperature` / `presence_penalty` /
  `frequency_penalty` 会被**忽略且不报错** —— 抽取类任务本来靠低温求稳定，
  开着思考等于这层控制也失效了。

- **各家的"别思考"写法不一样**（这是关键：不能写死一招）：
  | 服务 | 参数 | 备注 |
  |---|---|---|
  | DeepSeek（OpenAI 格式） | `{"thinking": {"type": "disabled"}}` | [官方文档](https://api-docs.deepseek.com/guides/thinking_mode)；用 SDK 时放在 `extra_body` |
  | DeepSeek（Anthropic 格式） | `{"reasoning": {"effort": "none"}}` | 同上文档 |
  | OpenAI 系（GPT-5 等） | `reasoning_effort: "none"`（`minimal` 只是减弱） | — |
  | 自托管 vLLM / SGLang（Qwen3 等） | `chat_template_kwargs: {"enable_thinking": false}` | 有的版本还认提示词里的 `/no_think` |
  | Anthropic | `thinking: {"type": "disabled"}`（默认即不思考） | — |
  | Gemini | `thinkingConfig: {thinkingBudget: 0}` | — |

- 实现（`rag/adapters/llm.py`）：
  1. **内置按任务判定，不做成配置项**：`no_think_for(task)` —— `summary / extract /
     intent / rewrite / evaluate / vision / caption` 关思考，开放式作答
     （`task="generate"`）保留思考。"该不该关"是框架的取舍：实测关掉既更快又更稳，
     那就不该把"要不要关思考"塞给客户去猜（与"vlm 段不给 temperature 输入框"同一个
     理由：少一个能配错的旋钮）。只有**代码内部**能临时覆盖 ——
     `generate(thinking=True/False)`，供探测与自测使用。
  2. **逐招探测 + 记住结论**：候选按 `thinking.type → reasoning_effort → chat_template_kwargs`
     顺序试；判据是"回了 200 且 `reasoning_content` 为空"。网关不认（400）或仍然思考
     就换下一招；探明后**缓存**（`"base_url|model" → 生效的那一招`），之后直接复用。
     真机实测两个端点上生效的招数不同：`api.deepseek.com|deepseek-flash` →
     `deepseek_thinking`；`192.168.100.242|qwen38-27b`（VLM）→ `openai_reasoning_effort`
     —— 所以"一招通吃"是不成立的。
  3. **关不掉也不丢功能**：全都无效时记一次 `llm_no_think_unavailable`，之后不再逐招
     试错（避免每次三倍请求），可靠性交给**预算阶梯**（§8.22）与"思考不当答案"（§8.3）兜底。
- 配置：**没有配置项**。`llm` / `vlm` 两段都不暴露这个开关，配置页也不会出现这一行
  （`_MODEL_SECTIONS` 里没有它）；YAML 里手写 `thinking:` 无效（模型里没这个字段，被忽略）。
- 回归实测：摘要任务 3/3 拿到摘要、每块 **0.8-0.9s**（此前 8-10s 且常失败）；
  视觉探针 1.9s 通过；桩级单测覆盖"内置策略真值表 / 第一招有效 / 400 换招 /
  逐级找到第三招 / 全都关不掉 / 显式 thinking=True"：`tmp_selftest/t_thinking_policy.py`。

### 8.25 暂存文件的删除只允许发生在临时目录 —— 已加固

- 真实踩到（开发自测时）：把**服务器上的真实文件路径**交给暂存项后，取消上传
   （`discard_staged`）会把那个原始文件 `unlink` 掉 —— 自测脚本就这么删掉了素材 PDF
   （已从 MinIO 取回同一份字节，MD5 一致；正式流程里暂存项都指向
   `tempfile.mkdtemp` 下的副本，所以线上没受影响）。
- 加固：新增 `_unlink_staged()`，**只删系统临时目录内的文件**，其余记
   `staged_unlink_refused` 警告并跳过；`discard_staged` / `confirm_staged` /
   `_purge_staged` 三处统一走它。

### 8.26 大文档：状态只在两处落库 + 版面解析静默 + pdfminer 刷屏 —— 已修复

实测对象：`Codex-从入门到精通…pdf`（**96 页**，4.36 MB）。任务 11:07:51 → 11:11:36
= **225 秒**，其中用户看到的是"排队中"卡了约 2 分钟，然后才跳到"解析中"。

- **`detect_format` 不慢**（读 16 字节 magic，毫秒级）。日志里停在 `step=detect_format`
  只是因为**下一个步骤 `layout_parse` 在完成前不报任何阶段**：96 页要按
  `max_pages_per_request=10` 分 **10 批**送版面服务，实测 **0.6~0.7 s/页 → 整篇约 63 秒**，
  这期间任务状态还是 `pending` → 界面显示"排队中"。
- 真正"解析中"那段是 `parse` 步骤里的 **pdfplumber 全文扫描**：实测 **16.6 秒**
  （96 页 `extract_text()` + `page.chars`），同时产出 **766 条** pdfminer 告警
  `Could not get FontBBox from font descriptor...`（该 PDF 的字体描述符缺 FontBBox，
  pdfminer 退化为零矩形并逐次告警；整篇各类 pdfminer 告警合计 **7688 条**）。

修复三处：

1. **阶段上报**：`detect_format` 结束时即报 `parsing 0.02`（毫秒级步骤之后立刻脱离"排队中"）；
   版面步骤的**每一批**完成都回调上报（`parse_layout(on_batch=...)` →
   `版面解析 30/96 页`，进度 0.03→0.18 线性插值）。版面解析属**解析**阶段，
   阶段名从原来的 `chunking` 改为 `parsing`，后续 `parse 0.20/0.22`、
   `region_enhance 0.25`、`outline 0.28`、`chunk 0.35`… 阶段与进度都单调、语义一致
   （原来会出现 chunking 0.18 → parsing 0.05 这种"倒退"）。
2. **pdfminer 噪声聚合**（`observability/logging.py`）：过滤器**挂在 handler 上**
   （挂 logger 不生效——日志传播只对 handler 应用过滤，这点踩过），只拦
   `pdfminer*`/`pdfplumber*` 的 WARNING 及以上阈值以下的记录，计数并在每个文档
   解析结束时汇总成**一条** `pdf_parse_warnings_suppressed`
   （含条数、来源 logger、样本、解释）。ERROR 照常放行；其它 logger 一概不动。
   实测：同一份 96 页文档，应用日志里的 FontBBox 条数 **766 → 0**，
   换成一条 `total=766` 的汇总。
3. `_pdf_batches` 里的回调异常被吞掉并记 `doc_parse_batch_cb_failed` ——
   进度上报是增强，不能因为它出错而打断解析。

回归：`tmp_selftest/t_parse_noise.py`（过滤生效/ERROR 放行/只拦 pdfminer 族/汇总一条/
分批回调推进/回调抛错不影响解析），以及真实文件复测（上面的 766→0）。

### 8.27 删除拆成两段：移入回收站 / 从回收站彻底删除 —— 已实现

用户口径：**列表里的「删除」= 移入回收站**（列表不再显示、可恢复）；
**回收站里勾选后再删除，才真正落到各数据库上**。这条把"删错一次"的代价从
"五个库的内容不可恢复"降到"点一次恢复"。

**数据层面的三个字段**（`documents`，DDL 与存量库 ALTER 见 `meta_mysql.py:139-146,296-306`）：

| 字段 | 作用 |
|---|---|
| `status='deleted'` | 回收站标记。**内容一个字都不动**：`chunks_meta` / ES / Milvus / MinIO 原文件全部原样保留，所以恢复不需要重新解析 |
| `deleted_at` | 移入时间；回收站列表按它倒序（在回收站里要回答的问题是"我刚删的是哪一篇"，不是"什么时候上传的"） |
| `prev_status` | **删除前**的状态。恢复时原样还原 —— 原来是无条件写 `done`，一篇 `partial`/`failed` 的文档删一次再恢复就变成"已完成"，状态筛选与质量视图当场失真。旧数据没有这一列时按"有没有块"兜底（有块→done，没块→failed），而不是猜 |

**检索为什么天然不含回收站文档**：元数据前置过滤的第一阶段就限定
`status IN ('done','partial')`（`meta_mysql.query_chunk_ids:535`），deleted/superseded
的文档拿不到 `chunk_id` 白名单，而 ES/Milvus 路由都以这个白名单为过滤条件 ⇒
回收站里的块**检索不到**，不需要重建索引。（秒传别名的 md5 扩展同样只扩 `done/partial`
的物理文档。）

**接口分工**（前端两处「删除」都走同一条，语义一致）：

| 动作 | 接口 | 落到哪一层 |
|---|---|---|
| 移入回收站 | `DELETE /api/documents/{doc_id}` | 只改 `documents` 一行 |
| 恢复（单篇 / 批量） | `POST /api/documents/{id}/restore`、`POST /api/documents/restore` | 只改状态（还原 `prev_status`） |
| 彻底删除（单篇 / 批量） | `DELETE /api/documents/{id}/permanent`、`POST /api/documents/purge` | 五个存储：ES → Milvus → 图谱 → MinIO → MySQL |

**三个必须挡住的口子**（都是本次实测出来的因果关系）：

1. **正在入库/重解析的文档不许删除**（409）。文档行的状态由**入库收尾**写：
   `ingest_write` 最后一步是 `upsert_document(ctx.doc)`，status=done/partial。
   若允许在解析途中删除，收尾会把 `deleted` 覆盖成 `done` —— 用户看到"删掉的文档
   自己回来了"，而界面上没有任何提示。
   · 判据必须看**任务行**（`ACTIVE_TASK_STATUSES`，`rag/models.py:84-96`）而不是文档行：
     文档行压根不会停在 parsing/chunking 这些中间态。
   · 另加一道 SQL 兜底：`upsert_document` 的 `ON DUPLICATE KEY UPDATE` 写成
     `status=IF(status='deleted', status, VALUES(status))`（`meta_mysql.py:334-341`）
     —— 回收站里的行不接受任何后续 upsert 带来的状态变化。
2. **回收站里的文档不许重解析**（409）。同样是收尾写状态的问题；要重跑先恢复。
3. **不在回收站的行不许彻底删除**（`skipped`）。批量彻底删除是不可恢复的，
   入口收紧到"必须先移入回收站"：误点最多进回收站。

**顺手修掉的一个老毛病**：`_purge_document` 原来把 ES 的 `index_not_found_exception`
（这个集合从来没建过索引）记成**失败** —— 在一个没索引过的集合里删文档，界面永远显示
"部分清理失败：fulltext"，用户以为没删干净、反复点。现在这类"目标库里本来就没有内容"
（含 Milvus 的 collection not found）记为 `cleaned: "fulltext(absent)"`，
真正的异常（集群不可达等）照旧进 `failed`。

**实测**（`tmp_selftest/t_recycle_bin.py`，真实 MySQL/MinIO + 真实模板渲染）：

- 软删除 → `status=deleted / prev_status=partial / deleted_at` 三处都对；块与原文件**没动**；
  列表默认不返回它；重复删除**不覆盖** `prev_status`；
- 恢复 → 回到 `partial`（不是 done）；批量恢复两篇都回列表；
- 护栏：活动任务 409（"文档正在处理中（parsing）"）、回收站重解析 409、
  不在回收站的行 `skipped`；
- 批量彻底删除 → 两篇的 `documents`/`chunks_meta`/MinIO 对象全清，`deleted=[…]`；
- 片段数据源（`_piece_data`）：回收站片段只含已删除文档、按 `deleted_at` 倒序，
  文档列表片段一篇都不含。

回归：`t_recycle_bin.py`、`t_permanent_delete.py`（后者改为注入真实异常来验证
"单库失败不阻断其余库"，并新增"索引不存在记为 absent"一项）。

### 8.28 顺带定位并修好：日志级别这个旋钮原本是空的（`configure_logging` 从未被调用）

起因是用户说"`llm_no_think_strategy_ok` 这行别打了"。第一反应是把它从 `info` 降成
`debug` —— 但**实测发现降级根本不会让它消失**：

```
$ python -c "from rag.api.app import create_app; from rag.observability.logging import get_logger; \
             app=create_app(); get_logger('rag.selftest').debug('debug_should_be_hidden')"
2026-09-25 19:58:00 [debug    ] debug_should_be_hidden   ← 照样打出来了
```

- 根因：`rag/observability/logging.py` 里的 `configure_logging(level, json_output)`
  **全仓没有任何调用点**（`create_app` 的 lifespan 里没有、入口脚本里也没有）。
  structlog 因此跑在**默认配置**上：`ConsoleRenderer` + `PrintLogger`，
  **不做级别过滤**，所有级别（含 debug）直接写 stdout。
- 后果（两个配置项都是摆设）：`observability.log_level` 不生效（6 处 `log.debug`，
  含 `pipeline_step_done` / `es_bulk_done` 这类逐步/逐批的进度行全都照打）；
  `log_json` 不生效（控制台仍是"人读格式"）。
  **连带影响**：`_install_noise_filters()` 也只在 `configure_logging` 里被调用 ——
  所以上一轮做的 **pdfminer 告警聚合在真实应用里根本没生效**（只在自测里生效过）。
- 修法（用户选择：接线，但保持现在的可读格式）：
  1. `create_app` 最前面调用 `configure_logging(config.observability.log_level,
     config.observability.log_json)`；
  2. `rebuild_container` 里再调一次 —— 配置页改了级别**保存即生效**，不必重启；
  3. `configure_logging` 自身加固：显式 `logging.getLogger().setLevel(lv)`
     （`basicConfig` 在 root 已有 handler 时直接返回，连级别都不设）、
     时间戳用**本机时区** `%Y-%m-%d %H:%M:%S`（`fmt="iso"` 是 UTC，会让控制台
     时间突然少 8 小时）、`cache_logger_on_first_use=False`（否则热更新对已建
     logger 失效）；
  4. `customer/customer_config.yaml` 的 `log_json` 由 `true` 改成 **`false`**
     —— 让配置与实际行为一致（要 JSON 就改回 true，改完立即生效）。
- 顺带**直接删掉**两行成功路径的实现细节日志（因为降级救不了它们）：
  `llm_no_think_strategy_ok`（探明关思考用哪一招）与
  `llm_reasoning_endpoint_detected`（探到推理端点、之后显式预算多留 2048）。
  结论仍可从 `generate_ex()` 的 `no_think` 返回值读；失败路径的 warning
  （`llm_no_think_unavailable`、`llm_no_think_param_rejected`、`llm_empty_reply`、
  `temperature_clamped`）全部保留 —— 静默的只能是"成功细节"，不能是"出了问题"。
- 实测：
  · `log_level: INFO` → `log.debug(...)` 被过滤、`log.info(...)` 正常，格式与之前
    完全一致（`2026-09-25 20:00:56 [info     ] …`）；
  · 临时配置改 `log_level: DEBUG` → debug 行出现（旋钮真的通了）；
  · 临时配置改 `log_json: true` → 每行一个 JSON 对象；
  · **pdfminer 聚合在应用上下文里终于生效**：连发 5 条 FontBBox 告警 → 0 行输出 +
    一条 `pdf_parse_warnings_suppressed total=5` 汇总；ERROR 照常打印。

### 8.29 块的**文档内顺序**现在落库了（chunks_meta.seq）——顺带修掉两处入库缺陷

起因：详情页「分块」点了 500、「内容预览」看不到东西（详见 `doc/trouble-shooting.md`
TS-026）。其中"看不到东西"的一半原因是**顺序**：展示用的块顺序一直是主键（哈希）序。

**为什么以前没有顺序可用**：`seq` 只活在分块过程里（它进了 `chunk_id` 的哈希，
而哈希不可逆），从来没落库。`SELECT chunk_id FROM chunks_meta WHERE doc_id=…`
走二级索引拿到的是 `(doc_id, chunk_id)` 序 = 哈希序，所以预览实测是
`1.2 → 四、 → 1.1 → 5.2 → 七、` 这种乱序。

修法（数据侧）：

| 改动 | 说明 |
|---|---|
| `chunks_meta.seq INT NULL` | DDL + 存量库 ALTER（`meta_mysql.py` 的 `_ensure_tables`）；分块时写 `seq`（父块先于其子块） |
| `ChunkMeta.seq`（`int = 0`） | 加 `field_validator(mode="before")` 把**NULL 当 0**：存量行读出来是 NULL，不兜会让 `ChunkMeta(**row)` 抛错，而调用点（预览、分块列表、一致性巡检、父子回补）**全都包在 try/except 里** → 异常被吞 → "块元数据全空"的静默降级（实测踩到：详情页 token/section 全丢） |
| `list_chunk_ids` | `ORDER BY (seq IS NULL), seq, page_num, chunk_id` —— 新数据严格按构建顺序；老数据退化为"按页码 + 哈希"，至少不跨页乱跳 |

**存量文档怎么补 seq（不用重解析）**：ES 的 `_seq_no` 是**写入序**，而入库时
`ctx.chunks` 就是按文档顺序生成的、ES 也按这个顺序 bulk 写入 ⇒ `_seq_no` 升序
== 构建顺序。这条假设先被验证过：新入库一篇文档后断言"ES `_seq_no` 顺序 ==
MySQL `seq` 顺序"，**逐个相同**（`tmp_selftest/t_chunk_order.py` 第 4 项）。
随后用它给存量块补齐（`tmp_selftest/t_seq_backfill.py`，只写 `seq IS NULL` 的行）：
本环境三篇文档 214 / 13 / 338 块，seq 范围 `[0, N-1]` 无重复，预览随即变成
封面 → 1.1 → 1.2 → 1.3 → 二、 → 四、 → 5.1 → 5.2 → 六、的正确顺序。

**同一次诊断里修掉的两处入库缺陷**（都会让内容或类型静默丢失）：

1. **短节的正文被整段丢弃**：分块时"一节短于 `min_chunk_tokens` 就 `continue`"
   （注释写的是"过短节并入"，实际是丢掉）→ 那一节在 MySQL/ES/Milvus 里都不存在。
   实测 6 节的 markdown 只入库 5 个块、第一章凭空消失。
   现在父块不再按 token 门槛丢弃：`min_tokens` 是**子块**的粒度门槛，
   父块代表"这一节的正文"，丢了就是内容缺失。
   ⚠ 存量文档里已被丢掉的短节只能靠重解析找回。
2. **非 PDF 文件被当图片送 OCR**：`ParseStep` 的 OCR 预取原来对"非 PDF"一律
   `ocr_image(文件字节)` → 服务端 `Invalid input file`（.md 实测 HTTP 422）→
   抛**可重试**的 StepError → 文档重试三轮后失败，md/txt/csv/Word/Excel 全都入不了库。
   现在只有 `_OCR_EXTS`（PDF + 图片）才预取；并把 4xx（除 404）归为
   `DocParseBadInput` → **不可重试**，直接失败并说清"服务端拒收该文件"。

回归：`tmp_selftest/t_doc_detail_pieces.py`（两个面板的渲染、顺序、不重复）、
`tmp_selftest/t_chunk_order.py`（真实入库验证 seq 与 ES 序一致）、
`tmp_selftest/t_outline_path.py`（标题自带斜杠不再劈坏路径）。

### 8.30 标题里的斜杠会劈坏 section_path（大纲/引用/检索三处一起中招）—— 已修

`section_path` 用 **ASCII `/`** 当层级分隔符（`"第3章/3.1节/3.1.2"`），
而**标题自身就会带斜杠**：中文研报的日期（`三、9/22晨间最新催化…`）、
并发写法（`输入/输出`）、代码路径（`src/app/page.tsx`）。

后果是**所有按 `/` 取叶子、按段数算层级**的消费点一起出错：

| 消费点 | 表现 |
|---|---|
| 详情页「内容预览」的大纲（`routes._parse_preview_data`） | 段数从 1 变 2 ⇒ 层级多一级，标题显示成上一个标题的**子项**，叶子被截断成 `22晨间最新催化…`（用户实际报的就是这个） |
| 引用里的 chapter（`models.py` 的 `RetrievedChunk.section`） | 引用显示 `page.tsx）` 这种半截标题 |
| 生成阶段的 section（`query_generate.py`） | 同上 |
| 详情页「分块」列表 | 展示原始路径，`9/22` 与层级分隔符看起来一样，分不清 |

修法（**源头一处，四处全对**）：`OutlineStep` 拼路径前把每条标题里的 ASCII 斜杠
换成全角斜杠 `／`（`ingest_parse._path_safe`）。路径从此无歧义（分隔符只剩 ASCII `/`），
中文语境里全角斜杠本来也更自然。

实测影响面（存量数据）：

- `doc_273a001538184e68`：1 块（`三、9/22…` → `三、9／22…`，重解析后已修正）；
- `doc_cf356feaae774355`：**26 块**——`§08 Skills、MCP、Automations与/goal:…`（21 块）、
  `1.首页（src/app/page.tsx)`、`2.历史记录页（src/app/history/page.tsx）`（被劈成 4 段、
  层级 4）、`3.迁移脚本（drizzle.config.ts+scripts/migrate.ts）` 等；
- `doc_822b10d4be4f4d03`：无（该篇标题里没有斜杠）。

⚠ 存量文档的路径是旧口径，**重解析一次**才会变成新口径（`tmp_selftest/t_slash_titles.py`
可列出还有哪些文档受影响）。

### 8.31 大纲层级：引擎不给层级 → 按标题编号推断（带护栏）—— 已实现

用户复查大纲时问"层级为什么是扁平的"。实测证据：

- 版面服务的区块**只有三个字段** `block_bbox` / `block_label` / `block_content`
  —— 没有 `level`/`heading_level`（`tmp_selftest/t_block_keys.py`）；
- 该 PDF **没有书签**（`tmp_selftest/t_pdf_bookmarks.py` → 0 条），
  `_calibrate_with_toc` 无从校正；
- 于是 `doc_parse.blocks_to_elements` 只能按 `block.get("heading_level") or 1` 全判 1 级
  （代码里的原话："宁可全判一级也不猜"），1.1/5.1 与 一、/五、 并列。

两条推断线索的实测对比（**用户选定按编号推断**）：

| 线索 | 本文件实测 | 风险 |
|---|---|---|
| 标题编号（`一、`→1 级；`5.1`→2 级；`3.2.1`→3 级） | 完全吻合文档真实结构（一、→1.1~1.3，五、→5.1~5.2） | 被版面模型误判成标题的**数据标签**会被算成二级（另一篇里就有 `6.32亿人`、`~3000亿美元`）→ 用护栏收窄 |
| 字号（`block_bbox` 框高聚类） | **分不开**：章节 30.5~39.3px，小节 25.5~30.3px，「五、」30.5 与「1.1」29.9 只差 0.6px | 不用 |

实现（`ingest_parse._numbering_levels` + `OutlineStep`）：

- 规则：标题以 `N.M[.K]` 开头 → 层级 = 点数 + 1；数字段只认 **1~2 位**
  （于是 "2026.9 版本说明" 这种年份开头的标题不会被误判）；
- **护栏**：同一个主号在本篇里**至少出现两次**才算"这篇真的在用编号分层" ——
  孤立的 `6.32亿人`（研报里的数据标签）因此保持 1 级；
- 优先级：编号推断**只加深**层级（`num > level` 才采用），不会覆盖解析器给出的
  更深层级；`MAX_DEPTH=4`；
- 效果（本文件实测）：`一、` L1 → `1.1/1.2` L2；`五、` L1 → `5.1/5.2` L2；
  `三、9/22…` 仍是 L1 且标题完整。

回归：`tmp_selftest/t_outline_path.py`（路径不被斜杠劈坏 + 编号层级真值表 +
孤立编号不推断的护栏）。

### 8.32 块与页的关系（分页逻辑）：页归属、结束页、父子两级

用户看修复后的大纲又问了两问，答案都落在**数据口径**上，记在这里。

**一、块上的页码是怎么定的**

- `page_num` 来自**版面引擎**（按送进去的页序，1 起；`_pdf_batches` 每批 ≤
  `max_pages_per_request` 页，但页码是**全局**的）。实测一篇 5 页文档的 15 个块，
  标注页与 PDF 文字层里文字真正所在的页**基本一致**。
- 块**不按页切**：分块单位是"节"（父块）与"节内窗口"（子块），所以一节常常跨页 ——
  实测跨页的小节：`1.2敏感性结构` P1–2、`三、9/22晨间…` P2–3、`5.1…` P3–4、
  `六、风险因素与证伪点` P4–5（`tmp_selftest/t_page_span.py`）。
  任何"按第 N 页看内容"的期待，都要先接受这一点：**页是块的属性，不是切分单位**。
- 因此新增 **`chunks_meta.page_end`**（块正文**结束**页；`ChunkMeta.page_end`）：
  分块时由"正文末尾所在的元素"给出（`ingest_chunk.emit(..., end_el=…)`），
  结束页取不到就退回起始页。有它才能如实表达"这一节排到第 2 页"。
  展示层按"起始页相同的一组"求该组真正排到第几页（`routes._parse_preview_data`
  算 `page_span_end`），相邻组区间会重叠 —— 那是如实反映"小节跨页"。
- ⚠ `page_end` 与 `seq` 都是本轮新列，存量块为 NULL → **重解析一次**才会补上。

**二、父子两级为什么会有内容重叠（分块逻辑）**

| | 父块（`is_parent=1`） | 子块（`chunk_type=text`） |
|---|---|---|
| 是什么 | **一整节**的聚合正文（节内 TEXT/TITLE 顺序拼接） | 父块正文切出来的**窗口**（`chunk_child_max_tokens=512`） |
| 进哪些库 | MySQL + ES（全文检索有它） | MySQL + ES + **Milvus**（向量只嵌子块） |
| 干什么用 | 检索命中子块后，按 `parent_chunk_id` 取回**整节**喂给生成（上下文） | **检索单元**（BM25/向量命中的就是它） |

同一节的父块与第一个子块经常"看起来一样"：这一节刚过 512 token 的窗口线时，
会被切成 499 + 65 两个窗口，第一个子块与父块重合 ~93%。这是两级的必然重叠。

- **真冗余只有一种情形**：一节只切出 1 个子块、且子块正文与父块逐字相同 ——
  这时父块没有任何额外信息（检索侧本来就会跳过它：`query_retrieve.py` 的
  `parent_txt != c.text`）。本次统计（`tmp_selftest/t_page_chunk_stats.py`）：
  三篇文档里"只有 1 个子块的父块"**各为 0 个** ⇒ 这种冗余一条都没有，
  因此**没有改入库逻辑**（保持现状）。
- 关于全文索引：真冗余的父块被跳过时，该节正文**仍然在 ES 里**（由那个逐字相同的
  子块承载 —— ES 收全部块：实测 `ES 块集合 = MySQL 块集合 = 69`，而
  `Milvus = 非父块 = 46`）。那类文字今天其实被索引了两遍，跳过冗余父块只是去重。

### 8.33 引用的页码：只到"块级"——已把结束页打通到检索链路

用户接着问："预览按区间标记了，那召回时给出来源页会不会出错、或无法定位到精确页？"

**结论**：不会指到块范围之外的页（引用给的是**命中块的起始页**），但精度上限是"块"：
块跨页时你要找的那句话可能在下一页。而且有一条链路根本没打通：

| 环节 | 现状（修之前） |
|---|---|
| 提示词里的来源定位（`query_generate`） | `c.title · section_path · 第3页` —— 只有起始页 |
| `SourceReference` | 有 `page_num`、**没有** `page_end`（`page_end` 只落在 MySQL） |
| ES / Milvus 映射 | 只有 `page_num`（给 Milvus 加字段必须**重建集合**，代价太大） |
| 前端来源抽屉（`chat-sources.js`） | 读的是 `src.page`/`src.filename`/`src.text`/`src.score` —— 后端一个都没有 ⇒ **页号从不显示**、标题退化成内部 `doc_id`、摘录与相关度永远为空 |

修法：把 `page_end` 一路打通到检索链路。

1. **不碰 ES/Milvus 的映射**：`page_end` 由 `RerankStep._fill_parent_content`
   从 **MySQL** 补到命中块上 —— 这一步本来就在按 `chunk_id` 取元数据、顺手补齐
   是零额外查询（顺带用元数据里的 `page_num` 覆盖 ES/Milvus 的快照值）。补不到时
   保留原值，不会把引用搞坏。
   为什么不给 Milvus 加字段：**给已存在的集合加列必须重建集合**（等于全量重嵌入），
   而元数据本来每轮查询都要取一次，从 MySQL 补是零成本。
2. **一处实现多处使用**：`rag.models.page_label(page_num, page_end)` →
   `第3页` / 跨页 `第3–4页`；提示词里的来源定位与 `SourceReference` 共用它
   （`SourceReference.page_label` 是它的属性包装），避免"有的地方给单页、
   有的地方给区间"。
3. ⚠ `page_end` 是本轮新列，**存量文档要重解析一次**才会带上；本环境三篇已重解析。

实测（`tmp_selftest/t_cite_page.py`，桩驱动真实函数）：`page_label` 真值表
（3,4→第3–4页；3,3/3,None→第3页；None→空串）、`_fill_parent_content` 确实把
`page_end` 补到命中块且父子回取不受影响、元数据查不到时保留原 `page_num`、
`SourceReference` 序列化带 `page_end`。

**"块级精度"到底差多少（用落库的 page_end 统计，`tmp_selftest/t_page_stats.py`）**：

| 文档 | 检索单元（子块） | 跨页的 | 跨度分布 |
|---|---|---|---|
| Codex（96 页） | 153 | **53 = 35%** | 跨 1 页 43、跨 2 页 9、跨 7 页 1 |
| 麦肯锡（116 页） | 281 | **94 = 33%** | 跨 1 页 85、跨 2 页 6、跨 3 页 2、跨 4 页 1 |
| 大摩 Muse（5 页） | 2 | 0 | — |

⇒ 约 **1/3 的命中块跨页**，其中 **87%（128/147）只跨 1 页**：也就是"引用页最多差
一页、翻一下就到"；跨 ≥2 页的只占 6%（19 个，多为图表块）。
（顺带：三篇重解析后短节补齐，麦肯锡 337 → 339 块、Codex 215 块、Muse 15 块，
`page_end` 无空值。）

**仍待决定（未做）**：要"精确到单页"就得让**检索单元（子块）不跨页** ——
切窗口时按页边界再切一刀（`ranges` 里已有元素→页映射），代价是块数变多
（按上面的比例约 1/3 的块要再切一刀）、页尾碎片要合并策略，且必须重解析。

### 8.34 质量分档的口径（只统计检索块）+ 库里"两个时钟"

**一、质量分档只统计检索块 —— 这是"15 块里只有 2 块有质量信息"的答案**

`ingest_write.FinalizeStep` 里分档**显式跳过父块**：

```python
for c in ctx.chunks:
    if c.is_parent:
        continue        # ← 父块不计入质量分档
```

一篇 13 父 + 2 子的文档，只有 2 块有档位（子块是检索单元，父块是整节聚合体）。
原摘要把"总块数"和"只管子块的分档"并列，看着就像漏统计了 13 块。文案经过两次调整
（`rag.models.quality_short_summary`，入库与展示共用）：

```
第一次（说明口径）：共 15 块 = 检索块 2 + 父块 13；检索块质量：高质 1 / 中质 1 / 低质 0（父块不计档）
第二次（用户要求收短）：检索块质量：高质 264 / 中质 17 / 低质 0
```

第二次只留质量分布那一句：**总块数/父子拆分在详情页左栏「分块数」那一行已经有了**
（`314（检索块 256 + 父块 58）`），摘要里再说一遍是重复；「父块不计档」改成悬停提示。
两个**只在非零时出现**的尾巴保留（`；写入失败库 N`、`；质量备注 M 条`）——它们是
"这份文档出过问题"的信号，去掉就没人看得见了。

⚠ 函数签名**保持老参数顺序**（`total_chunks`/`parent_chunks`/`doc_type`/`ocr_pages`
不再参与拼接）：老调用点是按位置传的，改动参数表会让 `15, 13` 落到
`failed_stores/warnings` 上，摘要里就会冒出"写入失败库 15；质量备注 13 条"这种
看不出处来的假数据。有单测盯着这条（`tmp_selftest/t_doc_attrs.py`）。

解析路径（`text/scanned/mixed` 型、OCR 页数）从摘要里拆成独立字段，
任务进度行仍带着它们（`FinalizeStep.parse_info`）—— 那一行是排查入库路径的现场记录。
摘要还在**展示时按当前口径重算**：老文档库里存的是旧文案，不重算就得把文档全部
重解析才能看到新说法。分档规则本身不变：`token_count < 20 或 quality_score < 0.3`
→ 低质；`table/image_caption 或 token ≥ 100` 且 `quality_score ≥ 0.6` → 高质；其余中质。

**图区计数（`quality_report.figure_stats`）** —— 详情页左栏「图表」那一行的括号用它：
`{cropped, crop_failed, page_fallback, angle_corrected, bbox_clamped, bbox_skipped,
page_scope, blank_skipped, image_caption_chunks, table_chunks}`。前 7 项由
`ingest_parse._surface_figure_notes` 从解析元数据写入，后 3 项在 `FinalizeStep`
补（空白跳过是 VLM 阶段才知道的、图/表块数以**真落库的块**为准）。老文档没有这个
字段 → 括号不显示（页面自然降级），重解析一次即补齐。


**二、库里有两个时钟 —— 已统一到 UTC**

核对"最后一次处理时间"时先发现库里存的时间不是一个口径，实测：

```
MySQL NOW() = 2026-09-28 10:37:51（服务端本地，UTC+8）
MySQL UTC_TIMESTAMP() = 2026-09-28 02:37:51
documents.created_at（Python utcnow） = 2026-09-25 11:07:50   ← UTC
documents.updated_at（MySQL NOW()）   = 2026-09-28 10:27:46   ← 本地
ingest_tasks.*（Python utcnow）       = 2026-09-28 02:27:50   ← UTC
```

⇒ **同一张表里两套时钟**：`created_at` 是 UTC，而 `updated_at`/`deleted_at`
（SQL 里的 `NOW()` / `ON UPDATE CURRENT_TIMESTAMP`）是服务端本地时间，
两者相差 8 小时（示例里 11:07 与 19:07 是同一时刻）。

修法：

1. 连接上把会话时区钉成 UTC（`meta_mysql._build_engine` 的
   `init_command="SET time_zone = '+00:00'"`）→ 从此 `NOW()` /
   `CURRENT_TIMESTAMP` / `ON UPDATE CURRENT_TIMESTAMP` 与 Python 侧同口径
   （**新数据**统一 UTC，一个时钟）；
2. 展示层统一把 UTC 转本地时区（`routes._fmt_local`），所有时间字段都走它。
   ⚠ 存量行里由 MySQL 写的老值仍是本地口径（只影响 `updated_at` / `deleted_at`；
   `deleted_at` 只在回收站里用到，而当前三篇文档都不在回收站）。

「处理时间」取**任务行**里 `completed_at` 最晚的那条（重解析会新增一条任务行），
**不用 `documents.updated_at`** —— 软删除/恢复/状态回写都会动它，不等于"处理过"。

回归：`tmp_selftest/t_doc_attrs.py`（16 项：时间换算、摘要口径、质量字段、
旧文案重算、`_doc_error` 回退），外加全量自测（15 个 Python + 4 个前端）全绿。

### 8.35 本轮（2026-09-25 ~ 09-28）处理逻辑改动一览

按"数据/算法怎么变"汇总，细节见对应小节。**界面字段与文案的改动不在本表内** ——
那份记录在 `doc/ui_spec_v3.md`。

| # | 处理逻辑 | 改了什么 | 详见 |
|---|---|---|---|
| 1 | **分块（顺序）** | `chunks_meta.seq` 落库（分块时的构建序号，父块先于其子块）；`list_chunk_ids` 按 `ORDER BY (seq IS NULL), seq, page_num, chunk_id` 返回。原先是主键（哈希）序 ⇒ 同一篇文档的块是乱序 | §8.29 |
| 2 | **分块（内容不丢）** | 短节不再被丢弃：原来"一节短于 `min_chunk_tokens` 就 `continue`"会把整节正文从三库里抹掉（实测 6 节的 md 只入库 5 节）。`min_tokens` 只是**子块**的粒度门槛，父块代表"这一节的正文" | §8.29 |
| 3 | **分页（页归属）** | 块上的 `page_num` = 版面引擎的全局页序；新增 **`page_end`**（正文结束页，由"末尾元素"给出）⇒ 跨页小节能如实表达成 `第 2–3 页` | §8.32 |
| 4 | **分页（存量补序号）** | 用 **ES 的 `_seq_no` 写入序**给存量块补 `seq`（先验证"ES 写入序 == 构建序"，逐个一致），省掉"为了顺序把文档全部重解析" | §8.29 |
| 5 | **大纲（路径）** | 标题里的 ASCII 斜杠拼进 `section_path` 前换成全角（`9/22`、`src/app/page.tsx` 这类标题不再把层级劈坏：原来会多算一级、叶子标题被截断） | §8.30 |
| 6 | **大纲（层级）** | 引擎不给层级 → 按标题编号推断（`5.1`→2 级、`3.2.1`→3 级），带护栏"同一主号出现 ≥2 次才算"；只加深不改浅 | §8.31 |
| 7 | **解析（类型门禁）** | OCR 预取只对 PDF/图片做：原来"非 PDF 一律当图片送 OCR"，md/txt/csv/Word/Excel 全被判 `Invalid input file`（实测 .md → HTTP 422）后反复重试失败 | §8.29 |
| 8 | **解析（错误分级）** | 4xx（除 404）归为 `DocParseBadInput` → **不可重试**，直接失败并说清"服务端拒收该文件" | §8.29 |
| 9 | **解析（进度）** | 版面解析按批回调上报（`版面解析 30/96 页`）、阶段号单调；pdfminer 族良性告警按文档聚合（766 条 → 1 条） | §8.26 |
| 10 | **删除生命周期** | 两段式：列表删除 = 移入回收站（`status=deleted` + `deleted_at` + `prev_status`，内容一概不动）；回收站勾选后才清五库。三道护栏：处理中的文档 409、回收站文档禁止重解析 409、不在回收站的行不许彻底删除 | §8.27 |
| 11 | **删除（容错）** | "目标库里本来就没有内容"（ES index_not_found / Milvus collection not found）记为 `absent` 而不是失败；单库失败不阻断其余库 | §8.27 |
| 12 | **检索（引用页码）** | 命中块从 **MySQL** 补 `page_end`（`RerankStep._fill_parent_content`，零额外查询）；`models.page_label()` 统一"第3页/第3–4页"口径。不给 Milvus 加字段：加列必须重建集合 | §8.33 |
| 13 | **质量口径** | 分档只统计**检索块**；摘要文案两轮调整后**只留一句** `检索块质量：高质 X / 中质 Y / 低质 Z`（总数与父子拆分交给左栏「分块数」那行），且展示时按当前口径重算（老文档不用重解析）；函数签名保持老参数顺序，避免位置实参错位成假数据 | §8.34 |
| 14 | **时间口径** | 库里两套时钟（Python UTC vs MySQL 本地）→ 连接会话时区钉成 UTC（`SET time_zone='+00:00'`），展示统一转本地时区 | §8.34 |
| 15 | **可观测性** | `configure_logging` 原本**全仓没人调用**（级别/JSON 两个配置项都是摆设、debug 行照打）；现在在 `create_app` 与配置热更新里调用 | §8.28 |
| 16 | **图区坐标（旋转）** | 引擎的 `doc_preprocessor_res.angle ≠ 0` 时，框在"转正后"的坐标系里而 `dataInfo` 报输入尺寸 → 按 angle 做**逆变换**（90/180/270 三套公式都对真值量过），再落页校验 | §8.36 |
| 17 | **图区坐标（兜底）** | 换算不出来不再"这张图彻底没有"：退回**整页渲染**交给 VLM，兜底处数进 metadata + 质量报告（粒度粗 > 搜不到） | §8.36 |
| 18 | **内嵌图出血位** | 内嵌图对象框常比 MediaBox 大 0.05~1.3pt → 裁剪前**夹到页内**（原来 `page.crop()` 抛异常被 `except: continue` 静默丢图，实测 26 个对象丢 18 个）；夹框/跳过计数进质量报告 | §8.36 |
| 19 | **图区落库（读侧现裁）** | `figure_bbox`（PDF point）随块落 MySQL；新增两个端点：按框现裁区域图、整页渲染并在服务端画红框标位置（图片字节仍不落盘） | §8.36 |
| 20 | **表格结构（rowspan）** | HTML 表格原来只展开 `colspan`，`rowspan` 不展开 ⇒ 第 2 行起整体左移一列（列=值对应关系全错，且静默）。实测麦肯锡 p87 首格 `rowspan="19"` 就是这种表 | §8.37 |
| 21 | **表格/公式区域识别** | 触发条件扩了一条（引擎 HTML **解析不出行列**时也送服务）；服务结果不无条件覆盖引擎的（更好才替换，否则记 `kept`）；表格响应按实测键树取 `table_res_list[0].pred_html` + `cell_box_list` | §8.37 |
| 22 | **能力失败分级** | 5xx/超时归 `DocParseTransient`：只进 degraded + 一条汇总告警，**不写"本进程不再尝试"的缓存**（原来一次 500 = 该能力永久降级，而配置页还是绿的） | §8.37 |
| 23 | **能力静默期** | 可恢复失败后开 **120s 静默期**（期间不发请求、到期自动放一次过去）—— 用户有意停掉某个识别服务时，不再为每个区域白跑 3 次连接尝试 + 退避（实测 ≈3s/区域），而服务恢复后**不用改配置、不用重启进程**就自动恢复 | §8.37 |
| 24 | **图题去壳** | VLM 描述里的提示词/思考残留（`**最终答案：**`、`**视觉引擎已解析图片，内容描述如下：**`、`**一句话描述：**`、`**内容描述：**`、正文里的 `**`）在入库前清掉；清完为空则记"该图按无描述入库"。实测本次重解析前有 **6 条**带残留 | §8.38 |
| 25 | **空白图区跳过图片理解** | 裁出来**近空白**的图区（64×64 灰度标准差 < 3）不问 VLM：实测这篇 116 页里 **42 个**（占全部图元素 55%）是纯白 0.2~0.4% 区域，每个都换回一段「完全空白的白色图片…」；本次重解析前库里有 **30 条**这种噪声块 | §8.38 |
| 26 | **整页粒度如实标注** | 图区面积 ≥85% 页面 → `figure_scope="page"`，块正文写成「图片描述：（整页图）…」（扫描页/整页照片页共 10 处）；空白跳过的整页区域写「（整页图，未做图片理解）」 | §8.38 |
| 27 | **图区计数落库** | `quality_report.figure_stats`：裁出/整页/空值兜底/角度纠正/夹框/跳过/空白跳过/图表块数 —— 详情页「图表」一行与悬停说明读它（不再从 issue 文案里反解） | §8.34 |

**本轮实测基线（便于以后判断"变慢/变差"）**

| 项 | 数值 |
|---|---|
| 版面服务 | 0.6~0.7 s/页（96 页约 63 s，按 `max_pages_per_request=10` 分 10 批） |
| pdfplumber 全文扫描 | 96 页 ≈ 16.6 s（产出 ~7.7k 条 pdfminer 告警，现聚合为 1 条） |
| 96 页 PDF 端到端 | 225 s（首次入库）／重解析 217 s（215 块） |
| 116 页 PDF 重解析 | 386 s（339 块） |
| 分块窗口 | 父块 ≤ 2000 tok、子块 ≤ 512 tok、`min_chunk_tokens=20` |
| 跨页比例 | 检索单元约 1/3 跨页，其中 87% 只跨 1 页 |

---

### 8.36 图区：从"整张图在索引里不存在"到"能定位到框"（本轮新增）

**问题一：引擎转过栅格时，图区坐标必然换算失败。** PaddleX 的文档方向分类判定页面
需要转正时（`doc_preprocessor_res.angle = 90/180/270`），`block_bbox` 落在**转正后**的
坐标系里，而 `result.dataInfo.pages[i]` 仍是**输入栅格**尺寸 —— 两个坐标系差一个旋转。
麦肯锡那篇 116 页里 114 页 `angle=0`（两个坐标系重合，一直没暴露），p10/p56 是整页
照片页、被判成需要转 90°/270°，于是：

- 引擎框 `[0,1,1684,1191]`（1684 宽）÷ 2.0 = 842pt，而页宽只有 595pt → 落页校验拒绝；
- 这两页**连一个 `image_caption` 块都没有**（没有文本层、引擎也只回 1 个 image 区块 0 字符）
  ⇒ 两张整页照片在索引里完全不存在。实测影响：拿"照片真实内容"去检索，
  一篇是 top1 0.490（等于搜不到），另一篇**0.823 命中封面上另一张小图**（高置信度错页引用）。

**修法（三件，互相兜底）**：

| 措施 | 实现 | 关键点 |
|---|---|---|
| 认角度做逆变换 | `doc_parse._rotate_box_to_input` | 90°: `[W-y1, x0, W-y0, x1]`；180°: `[W-x1, H-y1, W-x0, H-y0]`；270°: `[y0, H-x1, y1, H-x0]`。**换算出"落在页内"的框才用**，否则依次退到"原样"再退到 None |
| 换算失败 → 整页兜底 | `doc_parser._fill_layout_region_bytes` | 渲染整页交 VLM；`figure_page_fallback` 计数进 metadata + 质量报告（说明"描述粒度是整页"） |
| 内嵌图出血 → 夹框 | `doc_parse.clamp_box_to_page` | 夹到页内再裁；`image_bbox_clamped` / `image_bbox_skipped` 计数进报告（原来 18/26 个对象被静默丢掉） |

**角度公式是"量"出来的**：造一张内容已知旋转 θ 的页送服务，用未旋转版的框顺时针转 θ
当真值比 IoU（`tmp_selftest/t_angle_convention.py`）——90°→0.953、180°→0.906、
270°→0.903；不做变换只有 0.120/0.113/0.722。落页校验与夹框都有单测
（`t_angle_fixed.py`），离线重跑全篇的计数对照见 `t_q1q2_parse.py`。

**问题二：图看得到、但"哪一张"说不清，也点不开。** 图片字节只在解析期间的内存里
（MinIO 只有原始 PDF），落库的只有描述文字，所以来源只能给到"文档 · 第 N 页"。

**修法：落"框"而不是落"图"。** `figure_bbox`（PDF point，四位小数）随 `image_caption`
块进 MySQL（`chunks_meta.figure_bbox`，读侧 `json.loads`）；检索时由 `RerankStep` 与
`page_end` 一起从 MySQL 补到命中块、进 `SourceReference`。两个端点按需现裁：

| 端点 | 作用 |
|---|---|
| `GET /api/documents/{doc}/chunks/{chunk}/figure?dpi=` | 按框裁出那张图（来源抽屉缩略图、详情页分块列表） |
| `GET /api/documents/{doc}/pages/{n}/image?highlight={chunk}&dpi=` | 整页渲染并在**服务端**画红框标出图区（也支持 `bbox=x0,y0,x1,y1`） |

**红框为什么在服务端画**：前端画框需要"渲染尺寸 ÷ PDF point 尺寸"的比例，而预览图按
dpi 渲染（同一页换 dpi 比例就变），比例一旦不一致框就偏移 —— 而这种偏移在界面上"看着像
是对的"。服务端渲染与画框用同一张图、同一坐标系，框不可能偏。

**为什么不存图片字节**：一篇 116 页文档有 73 张图，另存 PNG 会让图片存储翻倍；框只有
4 个浮点数，读时几十毫秒现裁即可，还顺带支持换 dpi 看更清楚。存量块
`figure_bbox` 为 NULL → 前端不显示缩略图（不出现破图），重解析一次即可补上。

### 8.37 表格 / 公式识别能力接线（本轮新增）

本环境已配两项能力：`18082 /table-recognition`、`18083 /formula-recognition`。接线时
按实测响应把字段名钉死，并改了触发条件与失败分级：

- **表格响应键树（实测）**：`prunedResult.table_res_list[0] = {pred_html,
  cell_box_list, table_ocr_pred}`；原实现取 HTML 靠"按候选键名深挖"（恰好命中
  `pred_html`），但**单元格框永远取不到**（取 `prunedResult.cells`/`table_cells`，
  实测这两个键都不存在）——现在按实测路径取，并把单元格文字+框一并返回
  （后续做"表内小图单独理解"要靠它定位）。
- **触发条件**：原来只在"引擎只给框、没给 HTML"时送服务。现在"引擎给了 HTML 但
  **解析不出行列**"也送 —— 否则等于"引擎解析不出来 = 这张表没有结构"，而能力明明可用。
- **不无条件覆盖**：服务结果只有在"能解析出更多行"时才替换引擎那份；否则保留引擎结果、
  把服务结果留档在 `raw_data["table_html_service"]`，并记 `kept`（**不算失败**，
  避免报告里出现误导性的"区域识别未完成"）。
- **失败分级**：5xx/超时归 `DocParseTransient` —— 只进 `degraded`（监控页可见）+
  逐类汇总成**一条**告警，**不写"本进程不再尝试"的缓存**。原实现把任何
  `DocParseUnavailable` 都缓存成"能力不可用"：实测公式服务 18083 出现
  `/health` 200、`/formula-recognition` 连续 12 次全 500 的状态，一旦被缓存，
  服务恢复后本进程也永远不会再试，而配置页还是绿的。
- ⚠ 公式识别的字段名（`formula_res_list[*].rec_formula`）**只核对过空结果响应**：
  服务当前每次推断都 500，等服务恢复后要用真公式图再核一次字段名 ——
  否则会出现"调用成功但永远取不到内容"的静默降级。

**"服务被有意停掉"这种情况的降级路径**（本环境 18082/18083 已按需停掉）：

| 配置状态 | 代码行为 | 代价 |
|---|---|---|
| **没配**（配置页里删掉这两条模型） | `doc_parse_ready` 直接返回原因 → 不发请求，记 info issue + warning（"表格区域未做识别，已降级（内容以引擎文字为准）"） | 零 |
| **配了但服务停了** | 第一次调用失败（连不上，是 `DocParseTransient`）→ 开 **120s 静默期** + 汇总告警 + degraded；静默期内其余区域不再发请求；到期自动再试一次 | 每 2 分钟最多 1 次失败调用 |
| 服务恢复（进程没重启） | 静默期到期后的那次调用成功 → `note_doc_parse_ok` 清掉静默期与 degraded，恢复正常使用 | 零 |

两条路都**不会**影响文档入库：表的正文仍来自引擎 HTML，公式仍用引擎给的文本；
缺的只是"引擎只给框、没给内容"那一小类区域的补全。

### 8.38 图片描述的两处噪声与一处粒度问题（本轮新增）

用户实测看到的三个问题，都用**可量的判据**处理：

**① VLM 描述里混进提示词/思考残留。** 实测麦肯锡那篇的图题正文里有
`**视觉引擎已解析图片，内容描述如下：**`、`**最终答案：**`、`**一句话描述：**`、
`**内容描述：**`、`**图片内容如下：**`，以及正文里的 `**柱状图**` 这类加粗标记 ——
它们会作为 `image_caption` 块正文进三库，既污染检索也让引用里出现莫名标题。
修法：入库前**去壳**（`clean_vlm_caption`）—— 只删"一眼看就是壳"的东西：
开头的标签式前缀（可能叠好几层）与加粗标记；描述内容一个字不动。
清完为空说明模型只吐了壳 → 该图按"无描述"入库并记 warning。
本次重解析前实测有 **6 条**带残留。

**② 装饰性/空白图区白花一次 VLM 调用。** 引擎会在正文页上标出 **0.2~0.4% 页面**的
小区域当 figure，裁出来是**整块纯白**（64×64 灰度标准差 0.0、灰度范围 255~255）：
这篇 116 页里这样的区域有 **42 个**，占全部图元素的 **55%**，每一个都换回一段
「完全空白的白色图片…图片文字：麦肯锡全球研究院 中国的数字化转型」。
修法：裁出来近空白（标准差 < 3）就**不问 VLM**（记 info issue + metadata），
该元素若还有 OCR/引擎文字照旧出块。真实图片的标准差都在 20 以上，阈值有 7 倍余量。
本次实测：规则命中 **36 个**（42 个近白区域里 6 个标准差略高、仍送 VLM），
`image_caption` 从 **75 条降到 48 条**（−27，即那 27 个区域原本只靠"空白描述"成块）；
剩下 7 条没有「图片描述：」、只有引擎文字（其中 6 条正是品牌行
「图片文字：麦肯锡全球研究院 中国的数字化转型」）—— 文字一条没丢，只去掉了噪声描述。

**③ 扫描页的"图"其实是整页，粒度必须如实。** 扫描件整页就是一张位图，引擎与
pdfplumber 都只给"整页"一个区域，于是那条描述说的是**整页**（含正文）而不是某张插图；
只看正文分不出这一点。修法：图区 ≥85% 页面 → `raw_data["figure_scope"]="page"`，
块正文写成「图片描述：**（整页图）**…」，并在质量报告里给出处数（本篇 10 处）——
检索命中与来源抽屉里都能一眼看出这不是单张插图。

**④ 跨页表与表内小图：实测没有真案例，所以不动代码。**

- 表内小图：三篇文档 **57 个表块所在页，pdfplumber 的图像对象数全是 0**（表都是矢量绘制的）
  → "按单元格裁表内小图"没有可验证的样本（而且单元格框要表格识别服务给，该服务已停）。
- 跨页表：Codex 那篇有 54 张表、确实存在相邻页都有表的情形（p20/21、p24/25/26、
  p79–p89…），但**表头全部不同**，是彼此独立的小表（多为配置/命令片段）；
  McKinsey 的 3 张表分别在第 73/87/106 页。既然找不到"同一张表跨页"的真样本，
  合并规则就无从验证，硬做只会把两张无关的表合成一张（静默的数据损坏）→ 留作**已知限制**。
- 顺带实测到的一条：McKinsey p73/p106 两张"表"其实是**图表/来源布局**（首行是图题而不是
  表头，如 `['图17 …2400亿元', '', '', '数据来源…']`），于是"第N行：列=值"里的"列"读起来很怪。
  试过两条自动纠正规则（首行仅 1 个非空格子→当标题；下一行≥2 个非空格子→当表头），
  在真实数据上**都不成立**（p106 的下一行也只有 1 个非空格子）→ 不猜，保持现状并记录在此。

**本次重解析的实测前后对照**（同一篇 116 页，`tmp_selftest/t_verify_figures.py`）：

| 项 | 清理前 | 清理后 |
|---|---|---|
| 块总数 | 341 | **314** |
| `image_caption` 块 | 75 | 48（全部带 `figure_bbox`） |
| 图题里的提示词残留 | 6 条（`**最终答案：**` 等） | **0 条**（本轮实际清理 10 条，含正文里的 `**柱状图**`） |
| 「完全空白的白色图片…」噪声块 | 30 条 | **0 条**（跳过 36 个近空白图区） |
| 「（整页图）」标记 | 0 条 | **10 条** |
| p10 / p56 的图 | **没有块** | 有块，`figure_bbox=[0,0,595.3,841.9]` |
| 引擎文字的保留 | — | 品牌行「麦肯锡全球研究院…」仍在 6 个只有文字的图块里（只去掉空白描述） |

---

## 9. 自查命令

以下命令可直接在对应服务上跑（本环境地址来自 `customer/customer_config.yaml`）。

**MinIO**（Python，密钥用 `customer/.secrets.key` 解密）：

```python
import yaml; from rag.config.secrets import decrypt
from minio import Minio
st = yaml.safe_load(open('customer/customer_config.yaml', encoding='utf-8'))['storage']
cli = Minio(st['endpoint'], access_key=st['access_key'],
            secret_key=decrypt(st['secret_key'], 'customer'), secure=False)
for o in cli.list_objects(st['bucket'], prefix='default/doc_236c329d885b4d65/', recursive=True):
    print(o.object_name, o.size, o.etag)
```

**MySQL**：

```sql
SELECT doc_id, filename, file_md5, storage_url, page_count, chunk_count, status FROM documents;
SELECT chunk_type, is_parent, COUNT(*) FROM chunks_meta WHERE doc_id='doc_236c329d885b4d65' GROUP BY 1,2;
SELECT COUNT(*) rows_, COUNT(DISTINCT chunk_id) distinct_chunks FROM table_data WHERE doc_id='doc_236c329d885b4d65';
SELECT checkpoint, status, total_chunks, written_chunks FROM ingest_tasks WHERE doc_id='doc_236c329d885b4d65';
```

**Elasticsearch**（Kibana Dev Tools）：

```
GET rag_default/_count
GET rag_default/_search {"size":1,"query":{"term":{"doc_id":"doc_236c329d885b4d65"}}}
GET rag_default/_search {"size":0,"aggs":{"t":{"terms":{"field":"chunk_type"}}}}
```

**Milvus**（Python）：

```python
from pymilvus import MilvusClient
cli = MilvusClient(uri='http://192.168.100.239:19530', token='', timeout=15)
print(cli.get_collection_stats('rag_default'))
print(cli.query('rag_default', filter='doc_id == "doc_236c329d885b4d65"',
                output_fields=['count(*)']))
```

**跨库主键比对**（判"孤儿块"最快的方法）：分别取出三库的 `chunk_id` 集合求差集——`ES - MySQL` 与 `Milvus - MySQL` 应当为空。

---

## 10. 代码位置索引

| 主题 | 位置 |
|---|---|
| 上传入口（临时目录、格式校验、限额） | `rag/api/routes/documents.py:51-107` |
| 批次/任务/DocumentMeta 创建、MD5 去重策略 | `rag/ingestion/coordinator.py` 的 `submit` |
| 重复上传确认（暂存/确认/丢弃/超时） | `rag/ingestion/coordinator.py` 的 `stage_uploads` / `confirm_staged` / `discard_staged` |
| 上传接口与确认接口 | `rag/api/routes/documents.py` 的 `upload` / `upload_confirm` / `upload_discard` |
| 前端确认弹框 | `rag/web/static/js/knowledge-upload.js` 的 `askDuplicate` / `resolvePendingDuplicates` |
| 阶段 ① MinIO、断点恢复 | `rag/ingestion/coordinator.py:181-201`、`:465-482` |
| 工作流选择（pdf → `_quick_scan_check` → text/scanned/hybrid） | `rag/ingestion/coordinator.py:203-212` |
| 重试与失败策略 | `rag/ingestion/coordinator.py:310-333` |
| 软删除与异步清理 | `rag/ingestion/coordinator.py:261-306` |
| 重建索引 reingest | `rag/ingestion/coordinator.py:413-463` |
| 两阶段写入 / ES / Milvus / Graph | `rag/pipeline/steps/ingest_write.py:192-277` |
| verify / finalize | `rag/pipeline/steps/ingest_write.py:279-373` |
| enrich（摘要/关键词/实体） | `rag/pipeline/steps/ingest_write.py:32-127` |
| embed（向量化文本拼法、向量空间门禁） | `rag/pipeline/steps/ingest_write.py:130-189` |
| 确定性 chunk_id | `rag/models.py:27-35`；生成点 `rag/pipeline/steps/ingest_chunk.py:115` |
| 表格 chunk_id 回填、图片题注块拼装 | `rag/pipeline/steps/ingest_chunk.py:163-189` |
| MySQL DDL 与 upsert | `rag/adapters/meta_mysql.py:123-229`、`:407-450`、`:602-618` |
| 表格结构化行"按 doc 整体替换" | `rag/adapters/meta_mysql.py` 的 `upsert_table_data` |
| 块删除（重跑清理） | `meta_mysql.delete_chunks` / `meta_memory.delete_chunks` |
| 元数据前置过滤（chunk_id 白名单） | `rag/adapters/meta_mysql.py:498-573` |
| ES mapping / 写入 / 检索 / 健康检查 | `rag/adapters/fulltext.py:187-256`、`:281-352`、`:429-477` |
| ES 文档级字段与部分失败处理 | `rag/adapters/fulltext.py` 的 `upsert_chunks` / `get_doc_enrichment` / `delete_by_ids` |
| 引擎栅格页尺寸来源（`dataInfo`） | `rag/adapters/doc_parse.py` 的 `_stamp_engine_px` / `_engine_page_px` |
| 表格块正文改写（HTML → 自然语言行） | `rag/pipeline/steps/ingest_parse.py` 的 `TableExtractStep` / `table_to_natural_text` |
| 子块切分（分隔符保留）与页码归属 | `rag/pipeline/steps/ingest_chunk.py` 的 `_sentences_keep_seps` / `_join_sents` / `_el_at` |
| 重跑清理旧块 | `rag/pipeline/steps/ingest_write.py` 的 `_prune_stale` / `_prune_store` |
| MD5 秒传（含集合一致性判据） | `rag/ingestion/coordinator.py` 的 `submit` |
| Milvus schema / 客户端 / upsert / 检索 / 权限表达式 | `rag/adapters/vector_store.py:146-238`、`:344-355`、`:405-428` |
| MinIO put/get/delete/preview（`local_fs` 为同契约的本地实现） | `rag/adapters/storage.py:52-94`、`:97-132` |
| 父子回补 | `rag/pipeline/steps/query_retrieve.py:808-827` |
| 引用来源组装（title / storage_url / preview） | `rag/pipeline/steps/query_generate.py:42-77` |
| 永久删除顺序 | `rag/web/routes.py:1923-1946` |
| 巡检与修复 | `rag/services/consistency.py:100-160` |
| 设计口径（职责分工 / 写入顺序 / 一致性协议） | `doc/architecture.md:105-119`、`:375-390`、`:683-712` |
