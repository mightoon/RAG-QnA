# PDF 里的图像 / 表格 / 公式是怎么处理的（实现口径 + 实测）

> 适用范围：PDF（含扫描件）入库链路上的**非文字内容**——内嵌位图、矢量图、扫描页整页位图、
> 表格、公式、装饰性区域。内容以**代码为准**，每条结论都附实测数据（本环境 2026-09-28 实测）。
> 本文不涉及界面展示口径（那份在 `doc/ui_spec_v3.md`）；整体数据通路见 `doc/data_path.md`。
>
> 本环境的解析服务配置（`customer/customer_config.yaml` → `doc_parse`）：
>
> | 能力 | 地址 | 状态 |
> |---|---|---|
> | `ocr`（文字识别） | `http://192.168.100.237:18080` `/ocr` | ✅ 已配 |
> | `layout-parsing`（版面识别） | `http://192.168.100.237:18081` `/layout-parsing` | ✅ 已配 |
> | `table-recognition`（表格识别） | — | ❌ **未配**（只在"引擎给了框但没给内容"时才需要） |
> | `formula-recognition`（公式识别） | — | ❌ **未配**（同上） |
>
> 视觉模型（`config.vlm`）：`qwen38-27b`；`max_pages_per_request=10`（版面/OCR 分批上限）。

---

## 0. 先看链路顺序（理解后面一切的前提）

PDF 的入库链（`customer/workflows.yaml`，`pdf_text` / `pdf_scanned` / `pdf_hybrid`
三个键共用同一条链）：

```
detect_format → layout_parse → parse → region_enhance → quality_parse →
reorder_columns → table_extract → outline → vlm_caption → chunk → enrich → embed → write → verify → finalize
```

| 步骤 | 做什么 | 对图像意味着什么 |
|---|---|---|
| `layout_parse`（`ingest_parse.py:145`） | 整篇 PDF 按 **10 页/批**送 `/layout-parsing`，返回逐页 `parsing_res_list`（区块 + 类型 + 文本）、`layout_det_res`（区域框）、`overall_ocr_res`（逐行文字 + 置信度） | **唯一**能"看见"矢量图的入口（引擎先栅格化再检测） |
| `parse`（`ingest_parse.py:379`） | 先用 `/ocr` **预取**整篇逐页文字备用，再逐页决定用引擎的块还是本地的文字（见 §3.2） | 无论走哪条，**内嵌图对象照抽**（引擎区域没有图字节） |
| `region_enhance`（`ingest_parse.py:221`） | 给"只有框、没有内容"的**表格/公式**区域裁图 → `/table-recognition`、`/formula-recognition` | 本环境这两项能力未配 → 记 warning，区域内容以引擎文字为准 |
| `quality_parse`（`ingest_parse.py:459`） | OCR 置信度 / 空页率 / 乱码率 / 文档质量系数 | 扫描页的 OCR 质量在这里进报告 |
| `reorder_columns`（`ingest_parse.py:572`） | 双栏近似重排（**仅在没有版面引擎时**） | 有引擎时用引擎给的阅读顺序 |
| `table_extract`（`ingest_parse.py:695`） | 表格行列 → `ctx.tables` → MySQL `table_data`；表格块正文改写成自然语言 | 表格的"结构化"终点 |
| `vlm_caption`（`ingest_parse.py:621`） | 带 `image_bytes` 的 IMAGE 元素 → 视觉模型 → `raw_data["vlm_caption"]` | 图的"可检索化"终点；**必须在 chunk 之前** |
| `chunk`（`ingest_chunk.py`） | 图片元素拼成 `image_caption` 块（`ingest_chunk.py:293-303`） | 图上内容最终以**文字**进三库 |

**顺序上两条硬约束**（都写在 `workflows.yaml` 的注释里）：

- `layout_parse` 在 `parse` 之前 —— 阅读顺序、区块类型由引擎给定，本地解析器消费它；
- `vlm_caption` 在 `chunk` 之前 —— ChunkStep 读的是 `raw_data["vlm_caption"]`，排在后面等于图片描述永远进不了块。

---

## 1. 内嵌图像对象：最"标准"的一种，但**不是覆盖内容最多的一种**

**它是什么**：PDF 的 Image XObject，由 pdfplumber 的 `page.images` 读出 bbox（**PDF point** 坐标），
代码用 `page.crop(bbox).to_image(resolution=150)` 转成 PNG（`doc_parser.py:222-237`），
成为 `content_type=IMAGE` + `raw_data["image_bytes"]` 的元素，最终进 VLM，描述变成
`image_caption` 块的正文。

**实测：你们的文档里，"内嵌图像对象"绝大多数是装饰品**

| 文档 | 页数 | 内嵌图像对象 | 实际是什么 |
|---|---|---|---|
| 麦肯锡：数字化转型 | 116 | p1 有 4 张**占页 3%**；p6/p7/p9/p11/p25/p39/p57… 各 1–3 张**占页 0%** | logo、图标、分隔线 |
| Generative+UI | 22 | 17 个对象，多为**占页 5%~17%**（p2 有一个 49%） | 小图标 / 截图小块 |
| 大摩 Muse 框架解读 | 5 | 0 个 | 该文档没有内嵌图 |

⇒ 这条路是"最标准"的（对象模型里本来就有），**但它解决不了文书里真正的图表**：
真正的图表在你们这三篇里**多数不是内嵌位图**（见 §2、§3）。

---

## 2. 矢量画出来的图：visio / draw.io / mermaid / Excel 图表那一类

**它是什么**：导出成 PDF 后，图是**绘图指令**（线条、矩形、文字路径），
PDF 里**没有 Image 对象**，`page.images` 是空的。Excel 图表、Matplotlib、LaTeX/TikZ 同理。

**它如何被获取**：唯一入口是**版面引擎** —— 引擎先把页面栅格化成图片再做视觉检测，
所以照样给出 `figure` / `chart` 框（它**不关心**这张图是位图还是矢量）。

**实测（麦肯锡报告，116 页）**：50 多个**文字页**是"引擎图区 ≥1、PDF 图像对象 **0**"：

```
p13 引擎图区2/表格0 | PDF 图像对象 0     p17 引擎图区1 | 0
p14 引擎图区1 | 0                        p19 引擎图区1 | 0
p15 引擎图区1 | 0                        p35 引擎图区2 | 0
p16 引擎图区1 | 0                        p45 引擎图区2 | 0   …
这些页合计：引擎图区 66 个，而 PDF 内嵌图像对象是 0
```

**它如何被处理**（`doc_parser._fill_layout_region_bytes`，`doc_parser.py:268-325`）：

```
engine_bbox_to_points()   引擎像素框 → PDF point
                          倍率 = 引擎自报栅格尺寸 ÷ PDF 页尺寸（实测 144 DPI → 2.0 倍）
                          2% 容差；**框必须落在页内**，否则拒裁（宁可不裁，也不把整页当图送 VLM）
crop_page_region()        page.crop(box).to_image(resolution=150) → PNG 字节
```

裁出来的 PNG 与"抽内嵌图"拿到的字节**在下游完全同构** —— 同样进 VLM、同样变成
`image_caption` 块。**实测效果**：麦肯锡裁出 **66 张**，图题块 73 个、**73 个全部有 VLM 描述**
（其中 64 个还带图内文字），描述是真实内容：

```
p35: 图片描述：该图是一张柱状图（图8），展示了2012年中国及9个国家/地区"小企业的劳动生产率
     与全国平均水平之比"（%），其中中国（60-70%）和韩国（57%）显著低于虚线标示的79%基准线…
```

---

## 3. 整页就是一张大位图的扫描件：怎么处理

### 3.1 先判"这页有没有文本层"（**不是**看字符密度）

判据：`page.chars` 存在且去空白后长度 ≥ `_MIN_TEXT_CHARS`（20 字，`doc_parser.py:33`）。

为什么不用字符密度：密度是**尺度相关量**，A4 正常中文页的密度约为旧阈值 0.005 的 1/33，
用它当二分条件会把正常文字页判成扫描页 —— 那一页的标题层级、表格、图片元素**全部不再产出**。
判据现在是**"有没有文本层"**这个布尔量。

实测麦肯锡有 **8 页无文本层**：`p8 / p10 / p24 / p38 / p56 / p90 / p102 / p110`，
每页 **PDF 图像对象 1 个、占页面积 100~101%** —— 就是你说的"整页一张大位图"。

### 3.2 扫描页上的分工：**引擎为主、OCR 兜底**（不是"先 OCR 再版面"）

两步并行准备、逐页择优：

1. `layout_parse`（先跑）：整篇按 10 页/批送 `/layout-parsing`。**引擎内部自己会 OCR** ——
   它返回的文字块文本就是它识别出来的，同时给 `overall_ocr_res`（逐行文字 + 置信度）。
2. `parse`（后跑）：先用 `/ocr` 预取整篇逐页文字备用；然后**逐页**决定：
   - **引擎给了块** → 用引擎的块（阅读顺序、标题/表格/图类型都是引擎给的）。
     此时**不再叠加** pdfplumber 的逐行文字（两套文字同时进 chunk 会重复），
     **但内嵌图仍然照抽**（引擎区域没有图字节）。
   - **没有引擎块 且 文本层很薄** → 用预取的 OCR 结果造一个 TEXT 元素
     （`_ocr_element`，置信度写进 metadata，供 `quality_parse` 统计）。
   - 没有引擎块、但文本层够厚 → 走 pdfplumber 逐行文字（本地兜底路径）。

**所以"扫描页给谁"的答案是**：页面**先给版面服务**（它一次给出文字块 + 图/表区域框）；
`/ocr` 是**兜底/补充**（引擎没覆盖到的页，或没配版面服务时）。

⚠ 报告里的 `used_ocr_pages`（例如"mixed 型，OCR 8 页"）语义是"**有几页没有文本层**"，
**不等于**"调了几次 `/ocr` 端点" —— 那 8 页的文字其实是版面引擎自己 OCR 出来的。

### 3.3 从整页位图里认出什么，各自走哪条路

| 认出什么 | 引擎给的区块 | 后续处理 | 最终落库形式 |
|---|---|---|---|
| 文字 | `text` / `title`（`doc_title/paragraph_title`…） | 标题栈 → `section_path`；分块 | 正文块（三库） |
| 表格 | `table`（内容多为 HTML） | `table_extract` 解析行列 → `ctx.tables`；块正文改写成 `第N行：列=值；…` | MySQL `table_data` + 自然语言行文本 |
| 图表 / 照片 | `figure` / `chart` / `image` | 裁图 → VLM 描述 | `image_caption` 块正文 |
| 公式 | `formula` / `equation` | 引擎文本当正文；没内容时才走公式识别（本环境未配） | 普通 TEXT |
| 页眉 / 页脚 / 页码 / 脚注 | `header` / `footer` / `page_number` / `number` / `footnote` / `discarded` | **直接丢**（`doc_parse.py:186-187`） | 不入库 |
| 印章 | `seal` | 归 IMAGE → VLM | `image_caption` 块正文 |

**小图会另存吗？不会。** 图片字节只活在**内存**里（`raw_data["image_bytes"]`），
用途只有两个：与引擎区域配对去重、送 VLM。**MinIO 里只有原始 PDF 一份**，
裁出来的图不落盘、不入对象存储。落到库里的只有**文字**（`vlm_caption` →
`image_caption` 块的正文）。

**扫描页上"整页位图"与"引擎图区"的关系（实测，116 页麦肯锡报告）**：

```
每一页：PDF /Rotate=0、MediaBox 595×842pt（纵向）；引擎自报栅格 1191×1684px（纵向，2.0×）

p8 / p24 / p38 / p90 / p102 / p110
  pdfplumber 内嵌图对象 596×844pt（≈整页，但**框比页大 1.2pt**）
  → page.crop() 抛 ValueError（框不在页内）→ 该对象被静默跳过（无字节）
  引擎图区 bbox(px)=[0,1,1191,1683] → 换算后 [0,0,595,841]（整页）→ 按框裁图成功
  最终：1 个 image_caption 块，VLM 描述的是**整页**

p10 / p56   （同为整页照片页，但引擎的"方向分类"判错了）
  doc_preprocessor_res.angle = 90 / 270  ← 引擎检测前把栅格转了 90°
  引擎图区 bbox(px)=[0,1,1684,1191]（**宽高互换**，按旋转后的坐标系给框）
  而 dataInfo 仍报 1191×1684（旋转前的输入尺寸）→ 按 2.0 换算得 842×595pt
  → 超出页宽 595+2%容差(16.9) → 校验拒绝 → 该页图区裁不出来
  （修复前）最终：**该页没有任何 image_caption 块**
```

方向分布实测：`angle=0` 有 **114 页**，`angle=90` 只有 p10，`angle=270` 只有 p56。
**只有"裁图"受影响**：文字块的内容本身就是文本（不需要坐标），页码按元素顺序映射，
所以这两页的正文/大纲不受影响；受影响的只有"这张图是什么"这一条语义。

**这 2 页的后果是可检索性缺口，不是显示问题**（查真库实测，`doc_822b10d4be4f4d03`）：

- 8 个扫描页里 **6 页有 `image_caption` 块**（p8/24/38/90/102/110），**p10、p56 一页都没有**；
- p10/p56 没有文本层（引擎对这些页也只回 1 个 image 区块、0 字符），
  所以这两张整页照片**在索引里完全不存在** —— 无论用图语义还是正文都召不回
  （库里覆盖到 p10/p56 的块，只是"跨页正文块"的页码区间恰好盖住了它们）。

**缺口有多大：用"这两页照片的真实内容"去检索，实测两种翻车方式**（`tmp_selftest/t_lost_pages.py`
直接渲染这两页、调 VLM 得到真实描述，再拿描述去真 Milvus 检索）：

```
p56 真实内容：一位戴眼镜、穿白大褂的女性科研人员…用手指滑动一台平板电脑
  → top1 分数 0.823 命中 **p1** 的一条 image_caption（封面上 112×146pt、占页 3% 的小图，
    文字描述几乎一样，但**不是同一张图**：与 p1 四张小图的像素相关系数都 ≤0.16）
  → 表现是"高置信度、引用指到错页"，比"搜不到"更难发现

p10 真实内容：一位穿深色西装的男士边走边看手机…
  → top1 分数只有 0.490，且命中内容是无关的图 → 等于搜不到
```

这也说明：**图片的"可检索性"完全依赖那条描述文字**，描述一丢，召回就退化成
"找文字最像的另一张图"，给出来的还是看起来正常的错误引用。

**"配不上对"还有一种更常见的原因（本次实测新发现）**：pdfplumber 的内嵌图对象，
框常常比 MediaBox 大 0.05~1.3pt（导出工具的出血位），`page.crop()` 直接抛
`ValueError: Bounding box ... not within page`。这篇 116 页文档里共有 26 个内嵌图对象，
**修复前 8 个裁成功、18 个被静默跳过**：

| 被跳过的形状 | 页 | 后果 |
|---|---|---|
| 整页位图（596×844，出血 ~1.2pt） | p6/7/8/10/24/38/56/90/102/110（10 页） | 靠"引擎图区兜底"仍能出 `image_caption`（p10/p56 因旋转二次失败） |
| 竖细线（1~2pt 宽 × 844pt 高） | p9/11/25/39/57/91/103/111（8 页） | 装饰性细线，丢掉无损失 |

所以 `filled=66 / failed=2` 的准确含义是：**66 处按引擎框裁成功、2 处（p10/p56）被坐标校验拒绝**。
这个数字可用生产函数在缓存的版面结果上离线复现（`tmp_selftest/t_repro_fill.py`，
结果与线上日志逐字一致），不需要重跑解析。

### 3.3.1 这两条路已经修掉了（本节的结论都改成"修好之后"）

| 问题 | 修法 | 落点 |
|---|---|---|
| 引擎转过栅格 → 框在"转正后"坐标系，而 `dataInfo` 报输入尺寸 → 换算必被拒 | 按 `doc_preprocessor_res.angle` **逆变换**回输入坐标系（90°: `[W-y1, x0, W-y0, x1]`；270°: `[y0, H-x1, y1, H-x0]`；180°: `[W-x1, H-y1, W-x0, H-y0]`），变换后再做落页校验 | `doc_parse.engine_bbox_to_points` / `_rotate_box_to_input` |
| 上一条之外**任何**换算失败 → 那张图彻底没有语义入口 | **退回整页渲染**兜底：换算不出来就渲染整页交给 VLM，描述粒度记为"整页"，并在质量报告里说明 | `doc_parser._fill_layout_region_bytes`（`figure_page_fallback`） |
| 内嵌图对象框出血 → `page.crop()` 抛异常 → **静默丢图** | 裁剪前把框**夹到页内**（`clamp_box_to_page`），夹框与跳过的数量进 metadata + 质量报告 | `doc_parse.clamp_box_to_page` / `doc_parser`（`image_bbox_clamped` / `image_bbox_skipped`） |

**逆变换的角度公式是量出来的，不是推的**：造一张"内容已知旋转 θ"的页送服务，用未旋转版的
框顺时针转 θ 当truth，再比 IoU（`tmp_selftest/t_angle_convention.py`）：

```
内容旋转 90°  → 服务报 angle=90   公式 IoU 0.953   （不做变换 0.120）
内容旋转 180° → 服务报 angle=180  公式 IoU 0.906   （不做变换 0.113）
内容旋转 270° → 服务报 angle=270  公式 IoU 0.903   （不做变换 0.722）
```

修复后在**同一份缓存版面上离线重跑**（`tmp_selftest/t_q1q2_parse.py`）：

```
figure_region_cropped 59（原 66：10 个整页内嵌图现在自己能裁出来，不再走引擎兜底）
figure_region_crop_failed 0（原 2：p10/p56）
image_bbox_clamped 10 / image_bbox_skipped 8（那 8 个是 1~2pt 的装饰细线，夹完退化）
p10 / p56：figure_bbox=[0,0,595.3,841.9]（整页），有图字节 → 会产出 image_caption 块
```

⚠ 注意：本篇文档里 p10/p56 是**被"夹框后的内嵌整页图"救回来的**（内嵌图与引擎区域
1:1 配对，保留带字节的那个）。角度逆变换那条路要单独验证 —— 把内嵌图拿掉、
只留引擎区域时，p10/p56 仍能裁出整页：`t_q1q2_parse.py` 里的第二段就是这个用例
（`{'filled': 1, 'angle_fixed': 1}`）。

### 3.4 表格 / 图表 / 公式各自的终点

- **表格**：`table` 块 →（引擎给了 HTML 就直接用）→ 行列 → **MySQL `table_data`**
  （只在这里；检索路不查它）+ 块正文改写成自然语言行文本
  （原实现直接把 HTML 当正文：实测 7 个表格块 2701 字符里 1730 字符是标签，占 64%，
  "某表某列是多少"这类问法根本没法命中）。
  若引擎**只给框、没给内容**、或**给的 HTML 解析不出行列** → `region_enhance` 裁区域图
  （**200 DPI**，表格细线要看清）→ `/table-recognition`；服务结果只有在能解析出更多行时
  才替换引擎那份。**该能力在本环境已按需停掉** → 走降级：记 info issue + warning
  「表格区域未做识别，已降级（内容以引擎文字为准）」，表的内容仍以引擎 HTML 为准。
- **图表**：**没有独立的"图表识别"能力**。图表走 figure → VLM，提示词是
  「用一句话描述这张图片的内容，如果是图表请说明图表类型和数据要点」
  （`ingest_parse.py:616`），所以描述里会出现"柱状图 / 瀑布图 / 环形图"这类判断，
  但**图表的数值不会被抽成结构化数据**（只有表格进 `table_data`）。
- **公式**：`formula / equation` 归到 TEXT（目的是可检索）；引擎没给内容时才走
  `/formula-recognition`（同样已按需停掉 → 记 info issue + warning 降级）。

### 3.5 一张图从页面到检索的完整链路（7 步）

```
① 页面 → 引擎（栅格化 + 视觉检测）给出图区框；pdfplumber 给出内嵌图对象
   （内嵌对象的框超出页边界时 `page.crop()` 会抛异常 → 这类**不产生元素**，见 §3.3）
② 去重配对：同页按**顺序** 1:1 配对，保留带字节的那个，合并引擎侧的题注文字
   （不能用 bbox 比 IoU：两套坐标系差 2 倍，跨坐标系比只会得出"不是同一张"的错误结论）
③ 没配上的引擎区域按框裁图（给 VLM 用 150 DPI；表格区域另有 200 DPI 路径）；
   按面积标出粒度：≥85% 页面 → figure_scope="page"（正文会带「（整页图）」）
④ vlm_caption：IMAGE 元素 → 视觉模型 → raw_data["vlm_caption"]（≤500 字，并发 4，task=vision 关思考）
   · 近空白图区（灰度标准差 < 3）**不发请求**（实测占本文档图元素 55%）
   · 回来后**去壳**：清掉 `**最终答案：**` 这类提示词/思考残留再入库
⑤ 图题溯源：FigureLabelExtractor 在 IMAGE 前后 1–2 个元素里找 "图N/表N" 开头的行
   → figure_label / figure_caption（ingest_chunk.py:28-60）
⑥ chunk：拼成 image_caption 块正文 = 图题 + 「图片描述：[（整页图）]…」+「图片文字：…」
   （ingest_chunk.py:320-345）→ 入库（这是图上内容**唯一可检索**的形式）
⑦ 读侧：块的 figure_bbox 落 MySQL → 来源抽屉/详情页按框现裁图、整页图上画红框（§3.3.1）
```

---

## 4. 还有哪些情况要处理

### 4.1 已经处理的

- **页眉 / 页脚 / 页码 / 脚注**（label `header/footer/page_number/number/footnote/discarded`）
  → **直接丢**：混进正文会污染检索（靠"前 200 字符指纹去重"兜会误删合法的重复内容）。
- **印章（`seal`）、页眉页脚里的图（`header_image` / `footer_image`）** → 归 IMAGE → 送 VLM。
- **双栏版面**：有引擎时用引擎的阅读顺序；**没有引擎时** `reorder_columns` 按 bbox
  近似重排，并记一条 issue（有引擎时**不**去覆盖引擎顺序，否则等于把好顺序改差）。
- **同一张图被两个来源都看到**：引擎 figure 区域 ↔ pdfplumber 内嵌图，按顺序配对去重。
- **一页里"引擎给块"与"pdfplumber 抽图"同时发生**：文字只用引擎的（避免双份），
  图仍然抽（引擎区域没有字节）。
- **Word / PPT 里的图**：各自解析器从包里抽 part，同样进 VLM；Excel 没有图。
- **`figure_region_cropped` / `layout_figure_crop_incomplete` 留痕**：
  裁出多少张记为 info 级 issue，裁不出来多少记为 warning
  （`ingest_parse.py:833-846`），文案里说明后果——"这些图不会被图片理解描述"。

### 4.2 目前没有处理 / 处理得不够的（实测发现，待决定）

1. ~~**图区裁图失败 = 图上内容不可检索**~~ → **已修**（见 §3.3.1：角度逆变换 + 整页兜底）。
2. ~~**内嵌图对象"框出血"导致静默丢图**~~ → **已修**（夹到页内 + 夹框/跳过计数进报告）。
3. ~~**`figure_region_cropped` 文案不准确**~~ → **已改**（"矢量图、出血被夹过的位图、
   扫描整页图都算"，不再只说"通常是扫描件"）。
4. ~~**VLM 描述里混进提示词/思考残留**~~ → **已修**（入库前去壳：`clean_vlm_caption`，
   实测重解析前 6 条；见 `data_path` §8.38 ①）。
5. ~~**扫描页的"图"= 整页**~~ → **已如实标注**（≥85% 页面记 `figure_scope="page"`，
   块正文写成「图片描述：（整页图）…」，实测 10 处；粒度本身无法再细分 ——
   扫描页整页就是一张位图，见 `data_path` §8.38 ③）。
6. ~~**引擎会把装饰性区域标成 figure**~~ → **已过滤**（近空白图区不问 VLM：
   实测 42 个纯白 0.2~0.4% 区域、占全部图元素 55%，重解析前库里有 30 条
   「完全空白的白色图片…」噪声块；见 `data_path` §8.38 ②）。
7. **表格里的图 / 跨页表**：实测**没有真案例**，暂不动（三篇文档 57 个表块所在页的
   图像对象数全是 0；相邻页的表表头全不同，属彼此独立的小表）。要做"表内小图单独理解"
   需要表格识别服务给出的单元格框，而该服务在本环境已按需停掉。详见 `data_path` §8.38 ④。
8. **`used_ocr_pages` 的语义容易误读** → **已改**：详情页这一项显示成「无文本层 N 页」，
   悬停说明写明"文字由版面引擎自己 OCR，`/ocr` 只是兜底"。

### 4.3 表格 / 公式两项能力（本环境已配置）用起来之后的实况

```
18082 /table-recognition   probe ok，响应根名 tableRecResults，真区域图能出 HTML
18083 /formula-recognition probe 曾 ok，之后**每次推断都 HTTP 500**（4 张图 × 3 次）
                           而 /health 一直 200（"假绿"）
```

- 表格识别的真实响应结构（实测键树）：

  ```
  prunedResult
    ├─ overall_ocr_res.{rec_texts, rec_scores, rec_polys, rec_boxes}
    └─ table_res_list[0].{pred_html, cell_box_list, table_ocr_pred}
  ```

  原实现按候选键名深挖，HTML 恰好挖到了（`pred_html` 在候选里），但**单元格框永远取不到**
  （取的是 `prunedResult.cells`/`table_cells`，实测两个键都不存在）—— 现在按实测路径取，
  同时把单元格文字与框一并返回（供后续"表内小图"用）。
- **表格区识别的触发条件扩了一条**：以前只在"引擎只给框、没给 HTML"时才送服务；
  现在"引擎给了 HTML 但解析不出行列"也送（否则等于"引擎解析不出来 = 这张表没有结构"，
  而能力明明可用）。服务结果**不无条件覆盖**引擎的：只有当它能解析出更多行时才替换，
  否则保留引擎那份并把服务结果留档在 `raw_data["table_html_service"]`（记 `kept`，不算失败）。
- **公式服务 5xx 必须是"瞬时失败"**：原实现把任何 `DocParseUnavailable` 都写进
  "本进程不再尝试"的缓存 → 一次 500 = 该能力永久降级，而配置页还是绿的。
  现在 5xx/超时归 `DocParseTransient`：进 `degraded`（监控页可见）、逐类汇总成一条告警、
  **下一份文档照常重试**（`container.note_doc_parse_degraded`）。
- ⚠ 公式识别的字段名（`formula_res_list[*].rec_formula`）**只核对过空结果响应的键名**
  —— 服务恢复后要用真公式图再核一次，别把"调用成功但取不到内容"当正常。

---

## 5. 小结：四类"图"的落地方式

| 情形 | PDF 里有图像对象？ | 引擎看见？ | 字节从哪来 | 谁理解它 | 落库形式 |
|---|---|---|---|---|---|
| 内嵌位图（截图/照片/logo） | ✅ | ✅ `image` / `figure` | pdfplumber 抽对象（150 DPI） | VLM | `image_caption` 块正文 |
| 矢量图（visio / draw.io / mermaid / Excel 图表） | ❌ | ✅ `figure` / `chart` | **按引擎框裁渲染页**（150 DPI） | VLM | `image_caption` 块正文 |
| 扫描页整页位图 | ✅（只有整页一张，但常因出血裁不出来） | ✅ `figure`（整页） | 先试 pdfplumber 的整页对象，失败则由引擎整页框裁 | OCR 出文字 + VLM 描述整页 | 正文块 + `image_caption` 块 |
| 表格 | —（多为矢量或随扫描页） | ✅ `table` | 引擎 HTML；缺内容时才裁图走表格识别 | 表格识别 / HTML 解析 | MySQL `table_data` + 自然语言行文本 |
| 公式 | —（多为矢量） | ✅ `formula` | 引擎文本；缺内容时才裁图走公式识别 | 公式识别 | 普通 TEXT |

一句话：**位图靠 PDF 对象抽、矢量图靠引擎框裁、扫描页靠引擎自己的 OCR + 整页描述、
表格靠引擎 HTML（或表格识别）、所有"图"最终都以文字（`image_caption` 块）进三库**。

---

## 附：本文引用的代码位置

| 关注点 | 位置 |
|---|---|
| PDF 入库链与顺序约束 | `customer/workflows.yaml:62-84`（`pdf_text`/`scanned`/`hybrid`） |
| 版面解析步骤（分批 + 进度） | `rag/pipeline/steps/ingest_parse.py:145-217` |
| 区域补内容（表格/公式，含缺配留痕） | `rag/pipeline/steps/ingest_parse.py:221-375` |
| 解析步骤（OCR 预取 + 逐页择优） | `rag/pipeline/steps/ingest_parse.py:379-457` |
| VLM 描述 | `rag/pipeline/steps/ingest_parse.py:616-691` |
| 表格抽取与正文改写 | `rag/pipeline/steps/ingest_parse.py:695-772` |
| 图区留痕（issue/warning） | `rag/pipeline/steps/ingest_parse.py:833-846` |
| PDF 逐页解析（文本层判定、内嵌图抽取） | `rag/adapters/doc_parser.py:130-266` |
| 图区字节补齐（按框裁图 + 整页兜底） | `rag/adapters/doc_parser.py:291-378` |
| 图元素去重配对 | `rag/adapters/doc_parser.py:380-421` |
| 扫描类型判定（采样） | `rag/adapters/doc_parser.py:459-477` |
| 引擎区块 label 白名单 | `rag/adapters/doc_parse.py:182-192` |
| 引擎像素框 → PDF point（角度逆变换 + 落页校验 + 夹框） | `rag/adapters/doc_parse.py:280-400` |
| 按框裁页（PDF point，先夹到页内） | `rag/adapters/doc_parse.py:403-424` |
| 表格/公式区域识别（触发条件与三态） | `rag/pipeline/steps/ingest_parse.py:220-420` |
| HTML 表格 → 行列（colspan/rowspan 展开） | `rag/pipeline/steps/ingest_parse.py:830-890` |
| 图区坐标随块落库 | `rag/pipeline/steps/ingest_chunk.py:159-200`、`rag/adapters/meta_mysql.py`（`figure_bbox` 列） |
| 图区端点（现裁 / 整页高亮） | `rag/web/routes.py`（`doc_chunk_figure` / `doc_page_image`） |
| 图题溯源 + 图片块正文拼接 | `rag/pipeline/steps/ingest_chunk.py:23-75`、`:320-335` |
| 能力白名单（ocr/layout/table/formula） | `rag/config/models.py:100-105` |
| 本环境配置 | `customer/customer_config.yaml:261-305` |
