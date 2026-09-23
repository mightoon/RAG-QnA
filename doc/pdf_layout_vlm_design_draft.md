# PDF 版面结构化与区域级多模态理解 — 设计草案

> **文档状态**：草案（待评审）
> **版本**：v0.1
> **定位**：`architecture.md` §5.1（入库六步）与 `rag_framework_spec_v3.md` §7.1（入库步骤定义）的**局部修订提案**
> **不修改任何现有文档**：本文件是增量提案。方案落地后再按 §12 的清单回写原文档。

---

## 1. 背景与要解决的问题

### 1.1 现状（代码实测，非推测）

| 环节 | 现状实现 | 位置 |
|---|---|---|
| PDF 类型判定 | 字符密度 `len(text)/page.area < 0.005` | `rag/adapters/doc_parser.py:94,190` |
| 文字型 PDF 文字 | pdfplumber 逐行 `extract_text()` | `doc_parser.py:111-120` |
| 表格提取 | `page.extract_tables()`（**仅靠"找线"**） | `doc_parser.py:122-131` |
| 多栏重排 | `x < 200 → 左栏` 硬编码阈值 | `rag/pipeline/steps/ingest_parse.py:198-209` |
| 扫描页 OCR | 模块级单例 `PaddleOCR(...)`，取不到则永久返回 `None` | `doc_parser.py:26-38,198-220` |
| 图片理解 | `VLMCaptionStep` 把带图请求发给 **LLM 段**（文本模型） | `ingest_parse.py:219-256` |
| 版面检测 | 无 | — |
| 扫描页预处理 | 无 | — |

### 1.2 四个必须解决的问题

1. **密度判据是错的**：一页 500–1500 字的正常中文 A4，密度约 `0.00015`，远低于 `0.005` → **正常文字页被判为扫描页**，走 OCR 分支后 `continue`，标题层级、表格、图片元素**全部不再产出**。实测数据见 §11.1。
2. **文字型 PDF 的表格质量不可用**：文字层只提供"带坐标的字符"，"哪几个字属于同一行同一列"是**视觉问题**，pdfplumber 靠线段拼表 → 无边框表、合并单元格、跨页表全部失败。
3. **多栏阅读顺序错误**：pdfplumber 逐行读取会把两栏交错，现有重排用硬编码 `x` 阈值，三栏/混排即失效。
4. **扫描页的图片从未被提取**：图片提取逻辑只写在"文字页"分支；走 OCR 分支时 `continue`（`doc_parser.py:108`）。即使强提，整页位图 bbox 加 15% padding 会越界抛 `ValueError` 被吞（`doc_parser.py:135-136,147-148`）。

### 1.3 本草案的目标

用**独立的 PaddleX HTTP 服务**提供版面结构与文字识别能力，主应用**不新增任何 Python 依赖**（避免 `paddlepaddle`→`opencv`→`numpy<2` 的降级链打断 `pandas`（`pymilvus` 硬依赖）与向量库链路），并补齐区域级多模态理解。

---

## 2. 设计原则

| 原则 | 说明 |
|---|---|
| **依赖物理隔离** | 版面/OCR 引擎跑在独立 venv 或独立机器，主应用零新增包 |
| **能力走适配器** | 延续 `architecture.md` §8.1：外部能力通过适配器可替换，主应用只认契约 |
| **失败必须发声** | 服务不可用 → 重试/告警/降级留痕，**绝不静默产出空内容**（对应 TS-009/TS-011/TS-015 的教训） |
| **区域决策归版面引擎** | "哪一块是图、哪一块是表、阅读顺序如何"由版面引擎给；"这一块讲了什么"由 VLM 给。两者不混 |
| **不重复劳动** | 同一页只渲染一次、同一张图只描述一次、同一份文字不 OCR 两遍 |
| **落库可追溯** | 每份文档记录解析引擎指纹与图片覆盖率，事后可归因 |

---

## 3. 总体架构

```
                       ┌────────────────────────────────────────────┐
   客户上传 PDF         │           主应用（零新增依赖）                │
        │              │  rag/ingestion  →  rag/pipeline/steps       │
        ▼              └────────────────────────────────────────────┘
   ┌─────────┐                     │                    │
   │  上传    │                     ▼                    ▼
   │ 鉴权/配额│            POST /layout-parsing    你的 VLM 段(qwen)
   │ MinIO   │            POST /ocr                    │
   └─────────┘                     │                    │
                                   ▼                    │
                    ┌──────────────────────────┐        │
                    │  PaddleX 服务（独立 venv） │        │
                    │  ─ 8812: layout_parsing   │        │
                    │  ─ 8811: OCR              │        │
                    │  oneDNN 已关闭            │        │
                    └──────────────────────────┘        │
                                                        │
        区域裁剪（figure bbox）──────────────────────────┘
```

**三类 PDF 的统一处理**（不再按类型分三条流水线，而是"一次调用 + 页内分流"）：

```
PDF（文字型 / 扫描型 / 混合型）
   │
   ├─ ① 页级分类（主应用本地做，见 §6.1）：text_page / scan_page
   │
   ├─ ② POST /layout-parsing（fileType=0，整篇一次）
   │      useTableRecognition=true, useFormulaRecognition=true
   │      服务端返回逐页：parsing_res_list + layout_det_res
   │                        + overall_ocr_res + table_res_list + formula_res_list
   │      文字型页 → 引擎直接取文字层；扫描型页 → 引擎内 OCR
   │
   ├─ ③ 页块归一化 → PageBlock[]（§4）
   │
   ├─ ④ figure 区域 → 裁剪 → VLM 描述（§5.1）
   │      table 区域 → pred_html → 双轨入库（§5.2）
   │      formula   → LaTeX 进 chunk 文本
   │      header/footer/page_number → 按 label 剔除
   │
   └─ ⑤ 分块 / 增强 / 质量检测 / 写库（沿用现有步骤，§7）
```

**为何 PDF 走 `/layout-parsing` 而不是 `/ocr`**：`/layout-parsing` 是 `/ocr` 的超集——一次请求同时给出文字、版面区域、表格结构、公式（字段实测见 §4.1）。`/ocr` 保留用于**独立图片文件**（png/jpg）与单页兜底。

---

## 4. 适配器契约

### 4.1 服务端接口契约（基于 `paddlex 3.7.2` 实读）

**端点一览**（来源：`paddlex/inference/serving/schemas/*.py` 的 `INFER_ENDPOINT`）

| 端点 | `--pipeline` 名 | 响应字段根 |
|---|---|---|
| `POST /layout-parsing` | `layout_parsing` / `PP-StructureV3` | `layoutParsingResults` |
| `POST /ocr` | `OCR` | `ocrResults` |
| `POST /table-recognition` | `table_recognition` / `table_recognition_v2` | `tableRecResults` |
| `POST /formula-recognition` | `formula_recognition` | `formulaRecResults` |
| `GET /health` | — | — |

**`/layout-parsing` 请求体**（`schemas/layout_parsing.py:33-56`）

| 字段 | 类型 | 本方案取值 | 说明 |
|---|---|---|---|
| `file` | str | base64(PDF bytes) | 也接受 URL / 服务端可达路径 |
| `fileType` | int | `0` | 0=PDF，1=图像 |
| `useDocOrientationClassify` | bool | `true` | 方向分类（补 `preprocess_scan`） |
| `useDocUnwarping` | bool | `true` | 扭曲矫正 |
| `useTextlineOrientation` | bool | `true` | 文本行方向 |
| `useTableRecognition` | bool | **`true`** | 表格识别开关 |
| `useFormulaRecognition` | bool | **`true`** | 公式识别开关 |
| `useSealRecognition` | bool | `false` | 印章（本方案不需要） |
| `layoutThreshold` / `layoutNms` / `layoutUnclipRatio` / `layoutMergeBboxesMode` | — | 不传 | 版面调参，用产线默认 |
| `visualize` | bool | `false` | 不回可视化图，省带宽 |

**`/layout-parsing` 响应结构**（`pipelines/layout_parsing/result.py:94-158` 实读）

```
result.layoutParsingResults[i]              # PDF 输入时每页一项，顺序即页序
  ├── prunedResult
  │     ├── parsing_res_list   # 版面块列表：block_label / block_bbox / block_content，阅读顺序已排
  │     ├── layout_det_res     # {"boxes":[{label, coordinate, score}, ...]}
  │     ├── overall_ocr_res    # {"rec_texts","rec_scores","rec_polys","dt_polys","rec_boxes",...}
  │     ├── table_res_list     # [{"pred_html": "<html>...", ...}, ...]
  │     └── formula_res_list   # [{"rec_formula"/LaTeX 字段}, ...]
  ├── outputImages             # visualize=true 时才有
  └── inputImage
result.dataInfo
```

> **待实测确认（§11.2 待办 T1）**：`prunedResult` 内各字段的精确子键名与 `block_label` 取值集合，需拿一篇真实文档跑一次响应落盘核对。本文档中凡标注"待核"处，均以真实响应为准回填。

### 4.2 主应用侧接口：`PageBlock` 统一中间表示

```python
# rag/adapters/layout.py（新增）
class PageBlock(BaseModel):
    page_idx: int                       # 0-based
    kind: str                           # title|text|table|figure|formula|header|footer|page_number|discarded
    bbox: list[float]                   # [x0,y0,x1,y1] —— 统一为「PDF point，原点左上」
    text: str = ""                      # 文字/表格 NL 描述/公式 LaTeX
    heading_level: int | None = None     # title 才有：供 OutlineBuilder 直接消费
    payload: dict = {}                   # table→{html}; figure→{image_bytes|img_ref}; formula→{latex}
    reading_order: int = 0               # 引擎给出的阅读顺序
    source: str = ""                     # engine_text | engine_ocr | engine_model
```

**契约硬约束**

1. **坐标系统一为 PDF point（原点左上）**。PaddleX 输出为**图像像素坐标**（其输入是栅格化的页面），适配器必须按 `pixel / render_dpi * 72` 换算。**换算收口在适配器内**，上层只见 point。做错这一条的后果是"图画裁错位置、VLM 描述张冠李戴"——比不做更糟。
2. **`kind` 由 `block_label` 映射**，映射表见 §4.3；未识别的 label 落到 `text` 并记 warning（不丢弃）。
3. **figure 必须带可裁剪信息**：要么引擎回图（`visualize` 或图片字段），要么适配器返回 bbox + render_dpi 供主应用从同一份栅格裁剪。**同一页只渲染一次**。

### 4.3 `block_label` → `kind` 映射表（待 T1 回填完整取值）

| 引擎 label（示意） | `kind` | 处置 |
|---|---|---|
| `doc_title` / `paragraph_title` / `title` | `title` | 进 OutlineBuilder 标题栈 |
| `text` / `abstract` / `content` | `text` | 进父块聚合 |
| `table` | `table` | 双轨入库（§5.2） |
| `image` / `figure` / `chart` | `figure` | 区域级 VLM（§5.1） |
| `formula` / `equation` | `formula` | LaTeX 进 chunk 文本 |
| `header` / `footer` / `page_number` | 同名 | 剔除出正文，页码保留供溯源 |
| 其他（`seal`、`reference`…） | `text` 或无 | 记 warning，不丢弃 |

---

## 5. 区域级多模态理解

### 5.1 figure 区域 → VLM 描述

```
layout_det_res 中 label ∈ {image, figure, chart} 的框
  → 换算为 PDF point
  → 过滤：面积占页面比 < vlm_min_area_ratio（默认 0.03）→ 丢弃（logo/装饰）
  → 预算门禁：单页 > vlm_max_regions_per_page（默认 8）、单文档 > vlm_max_regions_per_doc（默认 200）
      超限时按面积降序取前 N，其余仅保留引擎给出的 caption/footnote，
      并在 ctx.warnings 记 vlm_budget_exceeded（不静默丢弃）
  → 从同一份页面栅格按 bbox 裁剪
  → 并发调用 VLM 段（config.vlm），信号量 = vlm.max_concurrency
  → 写入对应 figure 块的 payload.caption
```

**VLM 调用的前置门禁**（必须先修，见 §6.2）：`vlm_ready()` 返回非空即跳过描述并记 warning，**不得**把请求发给 LLM 段或 mock。

**降级链（每一级都留痕）**

| 级 | 条件 | 行为 |
|---|---|---|
| 1 | `vlm.base_url` 未配置 / 仍是本地替身 | 不发请求，`add_warning("图片理解已跳过：<原因>")` |
| 2 | 调用异常（超时/4xx/空响应） | `add_warning("图片描述失败：<原因>")`，保留引擎 caption/OCR 文本 |
| 3 | 区域未检出任何 figure | 不调 VLM（**不是跳过，是本来就没有**） |

### 5.2 table 区域 → 双轨入库

```
table_res_list[i].pred_html
  ├── 轨道 A（检索用）：HTML → 自然语言描述「第N行：列A=值X」→ 子块文本 → 向量库 + ES
  └── 轨道 B（精确查询用）：HTML → headers/rows → MySQL table_data → STRUCTURED 检索路
```

**来源唯一性原则**：表格结构只从**引擎**取（`pred_html`），**不再叠加** pdfplumber 的 `extract_tables`——两条都跑会得到双份或不一致的数据。pdfplumber 仅作 §7.3 的**交叉校验**。

### 5.3 formula / 页眉页脚

- `formula`：LaTeX 作为该块 `text` 进入 chunk（公式常承载最关键语义），`chunk_type=formula`。
- `header` / `footer` / `page_number`：**从正文剔除**（不再依赖现有"前 200 字符指纹去重"，那个做法会误删合法的重复内容，如每页重复的表头行、标准条款）；`page_num` 保留在 chunk 元数据供溯源。
- ⚠️ 区分"从**检索**剔除"与"从**给 LLM 的上下文**剔除"：前者按 `kind` 剔除，后者可保留（页眉常含文档标识）。

---

## 6. 主应用改动清单

> **说明**：本节所有改动均为**待实施**，当前仓库代码未做任何修改。

### 6.1 页级分类（替换密度判据）

```python
has_text_layer = len(page.chars) > 0
char_count     = len(page.extract_text().strip())
scan_page      = (not has_text_layer) or char_count < K        # K ≈ 30~50（中文页）
doc_type       = "text" | "scanned" | "mixed"                   # 按页汇总
```

- `doc_type` 写入 `DocumentMeta`；`page_classes[]` 写入质量报告。
- `K` 需要按语种/典型字号标定，配置项 `pipeline.scan_page_char_threshold`。
- **`density < 0.005` 必须删除**，见 §11.1 实测依据。

### 6.2 VLM 接线（前置修复，独立于本草案）

| # | 改动 | 位置 |
|---|---|---|
| 1 | 容器按 `config.vlm` 建 `self.vlm`（复用 `openai_compatible`） | `rag/container.py` |
| 2 | `vlm` 进 `SECTION_ADAPTERS` / `SECTION_DEGRADED_KEYS` / `RECOVERABLE_SECTIONS` / `_CORE_FALLBACKS` | `rag/container.py:36,52,67,95` |
| 3 | 新增 `vlm_ready() -> str | None` 门禁 + `probe_vision()` 带图探测 | `rag/container.py`（风格同 `vector_write_blocked`） |
| 4 | `VLMCaptionStep` 改读 `ctx.services.vlm`，去掉 `task="summary"`，降级从 `log.debug` 升为 `ctx.add_warning` | `rag/pipeline/steps/ingest_parse.py:219-256` |
| 5 | 监控页加 `("vlm","视觉模型","core", cfg.vlm, c.vlm)` 一行 | `rag/web/routes.py:1168` |
| 6 | 「测试模型」的 vlm 分支改为**带图请求**（否则文本模型配进 vlm 段会显示"测试通过"） | `rag/web/routes.py:~2730` |

> 依据：`rag/config/models.py:369-372` 的注释已明确记载"本段眼下只做配置 + 模型库……运行期还没有消费者"，且全仓 `self.vlm` / `container.vlm` 零匹配。

### 6.3 新增版面适配器

```python
# rag/adapters/layout.py（新增文件）
class LayoutUnavailable(RuntimeError):
    """版面服务不可用。不吞异常：吞掉会变成"文档入库成功但结构全丢"。"""

class HTTPLayoutAdapter:
    def __init__(self, cfg): ...
    @property
    def configured(self) -> bool: ...
    async def parse_pdf(self, pdf_bytes: bytes) -> list[PageBlock]: ...
    async def recognize_images(self, images: list[bytes]) -> list[dict]: ...   # 走 /ocr
    async def health_detail(self) -> tuple[bool, str]: ...
    async def aclose(self) -> None: ...
```

- **不注册进 `AdapterRegistry`**：`AdapterRegistry.create_parsers()` 会实例化**所有** `doc_parser` 注册类并读 `supported_extensions`（`registry.py:77-84`），把 layout 注册进该槽位会在容器构造期抛 `AttributeError`。要进注册表需先给 `_ADAPTER_TYPES` 加独立的槽位（后续重构）。
- 超时按**文档级**设置（整篇 PDF 一次请求），默认 300s，可配。

### 6.4 步骤序列调整（`customer/workflows.yaml`）

```
pdf_text / pdf_scanned / pdf_hybrid 三条 → 合并为一条 pdf：

  - detect_format        （保留：magic bytes 门禁）
  - layout_parse         （新增：调版面服务，产出 PageBlock[] + 页级分类）
  - region_vlm           （新增/增强 vlm_caption：figure 区域 → VLM）
  - table_extract        （推广到 PDF：引擎表格 → table_data 双轨）
  - outline              （改造：用引擎 heading_level + 阅读顺序）
  - chunk                （沿用）
  - enrich / embed / write / verify / finalize   （沿用）

  废弃：reorder_columns（阅读顺序由引擎给）
```

> `parse` 步骤保留给非 PDF 类型（docx/xlsx/pptx/md/txt/html/image），PDF 路径改走 `layout_parse`。

### 6.5 异步改造

`BaseParser._parse_sync` 当前是同步函数、跑在 `asyncio.to_thread` 里（`doc_parser.py:50-53`）。版面适配器是 HTTP 调用（async），因此：

- **推荐**：`layout_parse` 作为**独立步骤**在步骤层 `await`，结果注入 context；PDF 路径不再经过 `DocParserAdapter._parse_sync`。
- 这让引擎可独立热切换、async 天然解决，且与 `architecture.md:244` / spec §7.2.1 里本就存在的步骤划分一致。

---

## 7. 质量判据（三阶段）

### 7.1 解析后

| 项 | 判据 | 动作 |
|---|---|---|
| 文字提取率 | 引擎 `overall_ocr_res.rec_texts` 覆盖的页面字符数 / 页面数 | 低 → 告警 |
| **OCR 置信度** | `overall_ocr_res.rec_scores` 均值 | < 0.6 → 告警；< 0.4 → `PARTIAL` + `quality_score=0.1` |
| **figure 覆盖率** | `figures_detected / cropped / described / dropped_no_text / unreadable` 五计数 | 任一异常 → 质量报告 issue（见 §8） |
| 表格识别失败 | `pred_html` 为空 | issue + 保留区域 bbox |
| 公式数 | `formula_res_list` 长度 | 仅记录 |

### 7.2 分块后

沿用现有：信息密度 < 0.3 降权、`token < min_chunk_tokens` 处理、近似重复跳过（`ingest_chunk.py:99-114`）。

### 7.3 入库后

沿用抽样回查（`ingest_write.py:249-279`）。**新增交叉校验**：抽样文档用 pdfplumber 独立提取一次页数与中位行长，与引擎结果对比；差异超阈值记 issue（用于发现"引擎接错文件/页序错乱"这类系统性错误）。

### 7.4 `PARTIAL` 语义收窄

现状 `FinalizeStep` 把**任何 warning 都判成 PARTIAL**（`ingest_write.py:308-309`），会导致"全库写入成功却报部分完成"、批次变 `partial_failed` 并触发失败通知。本草案建议：

- `PARTIAL` **仅表示"部分库写入失败"**；
- 图片预算超限、置信度偏低、书签匹配率低等 → 走**质量报告 issue**，不改变任务状态。

---

## 8. 可观测性

| 指标 | 类型 | 意义 |
|---|---|---|
| `rag_layout_requests_total{endpoint,status}` | Counter | 版面/OCR 服务调用量与失败率 |
| `rag_layout_latency_seconds{endpoint}` | Histogram | 单篇/单页耗时（决定是否需要 GPU） |
| `rag_layout_pages_total{engine}` | Counter | 处理页数 |
| `rag_ocr_confidence` | Histogram | OCR 置信度分布（用于标定阈值） |
| `rag_figures{stage}` | Counter | `detected` / `cropped` / `described` / `dropped_no_text` / `unreadable` |
| `rag_vlm_budget_exceeded_total` | Counter | 预算门禁触发次数 |

**解析引擎指纹**（与既有向量空间指纹 TS-022 同构）：每份文档记录

```json
{"engine": "paddlex:layout_parsing", "engine_version": "3.7.2",
 "render_dpi": 200, "method": "auto", "used_ocr_pages": 12}
```

写入 `DocumentMeta`。用于查"同一逻辑集合里混着两种引擎解析的文档"——换引擎后旧文档仍在库里的场景必然出现。

---

## 9. 配置项

### 9.1 主配置新增段（`customer_config.yaml`）

```yaml
layout:
  enabled: true
  base_url: http://192.168.100.240:8812    # layout_parsing 服务
  api_key: ''
  timeout: 300.0                            # 整篇 PDF 一次请求，超时给足
  render_dpi: 200                           # 区域裁剪用的栅格分辨率
  use_doc_orientation: true                 # 方向分类
  use_doc_unwarping: true                   # 扭曲矫正
  use_textline_orientation: true
  use_table_recognition: true
  use_formula_recognition: true
  use_seal_recognition: false
  # OCR 兜底服务（独立图片文件 / 单页）
  ocr_base_url: http://192.168.100.240:8811
  # 成本护栏
  vlm_min_area_ratio: 0.03                  # 小于页面 3% 的区域不送 VLM
  vlm_max_regions_per_page: 8
  vlm_max_regions_per_doc: 200

pipeline:
  scan_page_char_threshold: 40              # 页级分类阈值 K
```

### 9.2 服务端产线配置（`D:\ocr-service\config\*.yaml`）

```yaml
# config/OCR.yaml 与 config/layout_parsing.yaml 都要加
# 注意：安装包自带的 OCR / layout_parsing 模板里**没有** Serving 节
# （只有 PaddleOCR-VL*.yaml 自带），所以必须手工加
Serving:
  visualize: false
  extra:
    max_num_input_imgs: null      # ★ 解除"PDF 只处理前 10 页"

SubModules:
  TextDetection:
    model_name: PP-OCRv6_medium_det
  TextRecognition:
    model_name: PP-OCRv6_medium_rec
```

> 默认值实读：`DEFAULT_MAX_NUM_INPUT_IMGS = 10`（`paddlex/inference/serving/basic_serving/_pipeline_apps/_common/ocr.py:33`），经 `Serving.extra.max_num_input_imgs` 覆盖（同文件 `:69-70`）。
> 不改的后果：50 页扫描件**静默只入库前 10 页**——HTTP 200、任务 DONE、无任何报错。

### 9.3 服务端启动（**必须带 oneDNN 环境变量**）

```powershell
$env:PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT = "0"    # 规避 Paddle 3.3.x 的 PIR↔oneDNN 崩溃
paddlex --serve --pipeline D:\ocr-service\config\layout_parsing.yaml `
        --host 0.0.0.0 --port 8812 --device cpu
```

> 根因：PaddlePaddle 3.3.x 的 oneDNN PIR 转换缺陷（`ConvertPirAttribute2RuntimeAttribute not support pir::ArrayAttribute<pir::DoubleAttribute>`）。PaddleX 在 CPU 上默认 run_mode=`mkldnn`（`paddlex/inference/models/runners/paddle_static/config/pp_option.py:32-44`），且**忽略 `FLAGS_use_mkldnn`**；关闭开关即回落到 `run_mode="paddle"`。

---

## 10. 验收标准

| # | 场景 | 判据 |
|---|---|---|
| A1 | 文字型 PDF（两栏） | 段落顺序与人工阅读一致（不再左右交错）；`section_path` 非空且层级正确 |
| A2 | 文字型 PDF（含表格） | `table_data` 有行；无边框表/合并单元格能还原；NL 描述可被 BM25 命中 |
| A3 | 扫描型 PDF | 有文字 chunk；`ocr_avg_confidence` 非空；**页数完整**（不被截断到 10 页） |
| A4 | 混合型 PDF | 文字页与扫描页都被处理；页序与原文一致 |
| A5 | 扫描型 PDF（含图） | `figures_detected > 0` 且 `described > 0`；图内容可通过自然语言检索到 |
| A6 | 任意类型 + VLM 未配置 | 任务仍 DONE，但质量报告有 `图片理解已跳过：<原因>` warning |
| A7 | 版面服务停机 | 任务进 RETRYING→FAILED（**不是** DONE） |
| A8 | 换引擎重跑同一文档 | 秒传判据因指纹不同而不复用（见 §11.2 T3） |

---

## 11. 附录

### 11.1 密度判据实测数据（支撑 §6.1）

用 PyMuPDF 构造、pdfplumber 实测的一页标准文字 PDF（75 字符正文 + 1 张嵌入图）：

```
page1 chars=75  density=0.000150  images=1  boxes=[(72.0,200.0,372.0,400.0)]
```

对照阈值 `0.005`：正常正文页密度约为阈值的 **1/33**，而阈值实际要求每页 **2500 字符以上**。同一实验还测出：整页位图（扫描页）的 bbox 为 `(0, 0.2, 595.0, 841.8)`；对其施加现有 15% padding 得到 `(-89,-126,684,968)`，**越界抛 ValueError** 并被 `except: continue` 吞掉。

### 11.2 待实测确认清单

| # | 事项 | 方法 |
|---|---|---|
| T1 | `prunedResult` 各字段精确子键名 + `block_label` 全集 | 拿一篇真实文档调 `/layout-parsing`，响应落盘核对，回填 §4.3 |
| T2 | CPU 单页耗时与内存占用 | 5~10 页真实文档计时（**决定是否需要 GPU 机器**） |
| T3 | 秒传判据是否需并入引擎指纹 | 换引擎重传同一文件，观察是否被 MD5 秒传复用（`coordinator.py:94-128`） |
| T4 | 整篇 PDF 一次请求的稳定性 | 大页数（100+）PDF 的超时与内存表现；否则改为分批页区间 |
| T5 | `table_recognition` 是否需单独端点 | 先用 `/layout-parsing` 的 `table_res_list`；仅当质量不足才加端点 |

### 11.3 与现有设计的差异说明（供评审）

| 现有设计 | 本草案 | 理由 |
|---|---|---|
| §5.1 入库六步（解析→多模态→分块→增强→embedding→并行写入） | 解析前**插入版面结构化**；多模态改为**区域级** | 区域信息是"哪些块要送 VLM"的前提 |
| §5.3 三类 PDF 三条 workflow | **合并为一条**，页内分流 | 引擎自带逐页处理能力；三条 workflow 差别极小（实测 `pdf_text`/`pdf_scanned` 仅差一个 `quality_parse`） |
| §5.3 `reorder_columns` 输出 `adjacent_pair_accuracy` | **废弃该步骤** | 阅读顺序由引擎给出；度量改由引擎质量指标承担 |
| §5.3 表格"双轨"（NL + 结构化） | 保留，**来源改为引擎** | 文字层无法回答"行列归属" |
| §5.8 三阶段质量检测 | 判据换源（OCR 置信度来自引擎），新增 figure 覆盖率 | 现有 OCR 置信度取自本地 PaddleOCR，且图片路径写在 `raw_data` 导致取不到 |
| §9.2 两阶段写入 | 不变 | 与本草案正交 |

### 11.4 参考来源

- PaddleX 服务化部署指南：<https://raw.githubusercontent.com/PaddlePaddle/PaddleX/develop/docs/pipeline_deploy/serving.md>
- PaddleOCR 通用 OCR 产线（含 `/ocr` 请求/响应字段）：<https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/main/docs/version3.x/pipeline_usage/OCR.md>
- PaddlePaddle 3.3.0 oneDNN PIR 回归（本方案必须关闭 oneDNN 的原因）：<https://github.com/PaddlePaddle/Paddle/issues/77340>
- 社区实践（`enable_mkldnn=False` 与 PaddleX 忽略 `FLAGS_use_mkldnn` 的说明）：<https://github.com/anon-research-tools/intelligent-ocr/commit/cf3adf733b4915c018754a8dad42bdf2bb95620a>
- 同类生产集成参考（图片覆盖率计数、跨页块处理思路）：<https://raw.githubusercontent.com/infiniflow/ragflow/main/deepdoc/parser/mineru_parser.py>

### 11.5 本地方案实测环境（截至本文档）

```
主应用环境：python 3.10.7 / numpy 2.1.0 / pdfplumber 0.11.9 / pymilvus 3.0.1（**不新增任何包**）
OCR 服务：python(.venv) / paddlepaddle 3.3.1 / paddlex 3.7.2 / numpy 2.2.6 / 无 GPU
模型缓存：%USERPROFILE%\.paddlex\official_models\{PP-OCRv6_medium_det,PP-OCRv6_medium_rec,
          PP-LCNet_x1_0_doc_ori,PP-LCNet_x1_0_textline_ori,UVDoc}
```

---

## 12. 落地顺序与文档回写清单

**建议实施顺序**（每步可独立验收，失败可回退）

1. **修 VLM 接线**（§6.2）——独立于本草案，先做，否则图片理解永远是"没有或幻觉"
2. **换掉密度判据**（§6.1）——纯粹的对错修正，与引擎无关
3. **起 `/layout-parsing` 服务 + 核准响应字段**（§11.2 T1/T2）
4. **实现 `LayoutAdapter` + `PageBlock` 映射**，PDF 仅走"文字+版面"，暂不接 figure/table
5. **接 figure 区域 → VLM**（§5.1）
6. **接表格双轨**（§5.2）+ 工作流合并（§6.4）
7. **指标与指纹**（§8）、验收（§10）

**方案通过后需回写的现有文档**

| 文档 | 章节 | 回写内容 |
|---|---|---|
| `architecture.md` | §5.1、§5.3、§5.4、§5.8 | 六步流程图插入"版面结构化"；三类 PDF 合并；表格来源改为引擎；质量判据换源 |
| `architecture.md` | §2.1、§8.2、§8.4 | 适配器接口层增加"版面/结构引擎"一行 |
| `rag_framework_spec_v3.md` | §5.6、§7.1、§7.2 | `DocParserAdapter` 扩展与 `PageBlock` 契约；`workflows.yaml` 新的 PDF 步骤序列 |
| `rag_framework_spec_v3.md` | §11、§18 | 新增 `layout:` 配置段与示例 |
| `rag_framework_spec_v3.md` | §20、§22 | 行为约束（失败必须发声）、V2 增补 |
| `ui_spec_v3.md` | 配置页模型 tab | 版面服务卡片与「试解析」按钮（**不要**复用通用「测试连接」：整篇解析不是"连得上吗"的语义） |
| `trouble-shooting.md` | 槽位登记表 | 槽位数从 11 扩到 12/13；新增本方案相关的静默降级条目 |
| `README.md` | 部署章节 | 独立 OCR/版面服务的部署与启动步骤（含 oneDNN 环境变量） |

---

**文档结束**
