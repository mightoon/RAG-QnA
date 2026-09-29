"""
文档解析服务客户端（rag/adapters/doc_parse.py）

PaddleX serving 把不同任务拆成不同 endpoint（`/ocr`、`/layout-parsing`、
`/table-recognition`、`/formula-recognition`），**一个服务实例只挂一条产线**，
所以"这条配置是什么" = 服务地址 + 处理能力（见 `config.models.DocParseConfig`）。

为什么走独立进程而不是在主应用装 paddle：`paddlepaddle` 依赖会经 `opencv` 把
numpy 拽回 1.x，而主应用的 `pandas`（`pymilvus` 硬依赖）是按 numpy 2 ABI 编译的
—— 一降级，向量库整条路都 import 失败。能力放服务里，两套依赖树物理隔离。

本模块只做三件事：把文件/图片送过去、把响应归一成主应用认识的形状、把失败
如实抛出来（**不吞异常**：吞掉就变成"文档入库成功但一个字都没有"）。

另外两件与"静默失败"直接相关的事也放在这里（口径属于引擎，不该散到业务层）：

1. **按页分批**（`_pdf_batches`）：服务端 `max_num_input_imgs` 默认 10，整篇 50 页
   PDF 送过去只会处理前 10 页且**返回 HTTP 200** —— 任务成功、页数少了 80%。
   所以这里按页切分送，并用"返回条数 vs 送过去的页数"自校验 + 二分重试。
2. **引擎像素坐标 → PDF point 的换算**（`engine_px_scale` + `engine_bbox_to_points`）：
   引擎栅格化后的 bbox 与 pdfplumber 的 point 不在同一坐标系。换算倍率取**引擎自报的
   栅格页尺寸 ÷ PDF 页尺寸**（两个都是确定量），并且要处理**引擎把栅格转正过**的情况
   （`doc_preprocessor_res.angle` ≠ 0 时框落在转正后的坐标系里 —— 不做逆变换就会
   "整页图彻底消失"，见 trouble-shooting TS-027），最后做"换算后是否落在页内"的结果
   校验，落页前**夹到页内**（PDF 的出血位框不该让整张图丢掉）——不拿猜的值去缩坐标
   （早期用 OCR 行高反推倍率，实测推出 2.93、真实 1.14，属于"看着有依据、其实全错"）。
"""
from __future__ import annotations

import asyncio
import base64
import io

import httpx

from rag.config.models import (DocParseConfig, doc_parse_capability,
                               doc_parse_endpoint_path, is_http_url)
from rag.observability.logging import get_logger

log = get_logger("rag.doc_parse")

# 各能力响应的**唯一字段根名**。它同时是"这个地址上跑的到底是哪条产线"的指纹：
# 配错能力时（例如把 layout_parsing 服务标成 ocr）HTTP 会 200，但字段根名对不上，
# 于是当场暴露，而不是等到入库才 404（见 probe 的用法）。
RESPONSE_ROOT = {
    "ocr": "ocrResults",
    "layout-parsing": "layoutParsingResults",
    "table-recognition": "tableRecResults",
    "formula-recognition": "formulaRecResults",
}

# 单次请求最多送几页。10 = 服务端 `max_num_input_imgs` 的默认值：超过它的页会**被
# 服务端静默丢掉**（HTTP 仍是 200），所以这里跟着它的默认值走，客户调大了服务端
# 也可以在 doc_parse 段里配大（`max_pages_per_request`）。
_DEFAULT_PDF_BATCH_PAGES = 10

# 探测用的最小**有效**文档：320x120 白底黑框 + 两行文字（约 2KB）
#
# 为什么不用 1x1 像素：PaddleX 产线对输入有尺寸/像素校验，1x1 会在**请求体校验**
# 阶段被拒（HTTP 422），于是"端点明明存在"被误报成不可用 —— 探测图必须是一张尺寸
# 与内容都像正常文档的图，否则测的是校验器而不是产线。实测：1x1 -> 422，本图 -> 200。
#
# 刻意**现画**而不是内嵌 base64 常量：常量既长又只能人工转录，抄错一次就是"探测永远
# 失败"这种难查的故障；PIL 本来就是本模块的既有依赖，现画的成本可以忽略。
def _probe_image_b64() -> str:
    import base64
    import io as _io

    from PIL import Image, ImageDraw
    im = Image.new("RGB", (320, 120), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([4, 4, 315, 115], outline=(0, 0, 0), width=2)
    d.text((24, 30), "RAG doc-parse probe", fill=(0, 0, 0))
    d.text((24, 62), "PROBE OK 0123456789", fill=(0, 0, 0))
    buf = _io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")



class DocParseUnavailable(RuntimeError):
    """文档解析服务不可用 / 未配置。调用方应据此重试或显式降级。"""


class DocParseEndpointMissing(DocParseUnavailable):
    """服务活着，但该能力端点不存在（HTTP 404）

    典型成因：PaddleX 一个实例只挂一条产线，配置里的「处理能力」与启动时的
    `--pipeline` 不一致（例如地址上跑的是 OCR，却配了一条 layout-parsing）。
    这类错误**重试不会自愈**：它不是抖动，是配置错。所以要能被上层单独识别，
    用来"快速失败 + 给出可处置的提示"，而不是让任务在退避队列里空转三轮。
    """


class DocParseBadInput(DocParseUnavailable):
    """4xx（404 之外）：请求或**文件本身**不被这个端点接受，重试多少次都一样

    实测：把 .md 的字节当图片送 `/ocr` → HTTP 422 `Invalid input file`。
    这类错误若按"可重试"处理，一篇文档要白跑三轮退避重试才失败，
    而且错误里只有服务端原始 JSON，用户看不出"是送错了类型"。
    """

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class DocParseTransient(DocParseUnavailable):
    """服务活着、但这**一次**推断失败（HTTP 5xx / 连不上 / 超时）

    为什么要与"端点不存在"分开：`DocParseEndpointMissing` 是配置错，重试永远不会好，
    可以放心地把能力标记成"本进程内不再尝试"；而 5xx 是**可恢复**的 —— 实测公式识别
    服务（18083）`/health` 一直 200、`/formula-recognition` 却连续 12 次全 500
    （换 4 张不同的图都一样），同一台机器上表格服务（18082）完全正常。把这种抖动
    当成"能力不可用"缓存下来，等于**一次抖动 = 整个进程周期内该能力永久降级**，
    而且用户看不到（配置页还是绿的）。
    """


# 业务侧的「内部能力名」→ doc_parse 端点能力名。
# 业务不必知道 PaddleX 把任务拆成了哪些 endpoint（以后它换名也不影响入库链路）。
CAPABILITY_FOR = {
    "layout": "layout-parsing",
    "ocr": "ocr",
    "table": "table-recognition",
    "formula": "formula-recognition",
}


def doc_parse_capability_for(config, internal: str):
    """内部能力名 → 已配置的能力名（未配置返回 None）

    入参是整份 AppConfig：调用点在 pipeline 步骤里，手上只有 ctx.services.config。
    """
    cfg = getattr(config, "doc_parse", None)
    if cfg is None:
        return None
    cap = CAPABILITY_FOR.get(internal)
    if not cap:
        return None
    return cap if doc_parse_available(cfg, cap) else None


def make_client(config, capability: str, timeout: float | None = None) -> "DocParseClient":
    """按能力建一个客户端（地址从配置库里反查）"""
    cfg = config.doc_parse
    base = resolve_base_url(cfg, capability)
    if not base:
        raise DocParseUnavailable(f"「{capability}」没有配置服务地址")
    return DocParseClient(base, capability,
                          timeout=timeout or getattr(cfg, "timeout", 300.0),
                          max_pages_per_request=getattr(
                              cfg, "max_pages_per_request", 0))


def resolve_base_url(cfg: DocParseConfig, capability: str) -> str:
    """按能力在配置库里找服务地址。

    这一段**没有 active 概念**（卡片 = 模型名，同名的多条 = 同一张卡片上的几枚能力
    徽标，见 `config.models.DocParseConfig`），所以按能力反查，而不是看 active_id。
    同一地址上配了多条能力时后写的胜出；地址非法（非 http(s)）视为未配置。
    """
    cap = doc_parse_capability(capability)
    top = str(cfg.base_url or "").strip()
    entries = list(cfg.models or [])
    # 库里没有对应能力、但段顶层地址与能力一致 → 用顶层（存量配置兼容）
    if not entries and is_http_url(top) \
            and doc_parse_capability(cfg.capability) == cap:
        return top
    for entry in entries:
        if doc_parse_capability((entry.params or {}).get("capability")) != cap:
            continue
        url = str(entry.base_url or "").strip()
        if is_http_url(url):
            return url
    if is_http_url(top) and doc_parse_capability(cfg.capability) == cap:
        return top
    return ""


def doc_parse_available(cfg: DocParseConfig, capability: str) -> bool:
    return bool(resolve_base_url(cfg, capability))


# ── 版面解析结果 → ParsedElement ─────────────────────────────
#
# 版面引擎相对 pdfplumber 的核心增量是**阅读顺序**与**区域类型**：pdfplumber 逐行
# 读文字会把两栏交错（本地重排只能用 x 阈值近似），而引擎已经排好序、也标好了
# 哪块是标题/表格/图/页眉。这里把 parsing_res_list 直接映射成解析器的中间表示，
# 于是下游的 OutlineStep / ChunkStep 不需要为"换个引擎"改一行。
_LAYOUT_TITLE_LABELS = {"doc_title", "paragraph_title", "title", "chart_title"}
_LAYOUT_TABLE_LABELS = {"table"}
_LAYOUT_IMAGE_LABELS = {"image", "figure", "chart", "seal", "header_image",
                        "footer_image"}
# 公式区域：引擎多数版本只给框与识别文本，个别版本给 latex。
# 归到 TEXT（而不是丢掉或单独造类型）：它就是要被检索的正文内容。
_LAYOUT_FORMULA_LABELS = {"formula", "equation", "formula_title"}
# 这些区域不进正文：页眉页脚/页码是版式噪声，混进 chunk 会污染检索
# （现有实现靠"前 200 字符指纹去重"兜，会误删合法的重复内容）
_LAYOUT_DROP_LABELS = {"header", "footer", "page_number", "discarded",
                       "number", "footnote"}


def _engine_page_px_raw(page: dict) -> tuple[float, float] | None:
    """`dataInfo.pages[i]` 报的**原始**引擎栅格页尺寸（不长短边归一化）

    它报的是**输入栅格**（引擎收到的那张图，方向与 PDF 页一致）。必须保留原始顺序：
    横竖搞反了倍率就算不出来（`engine_px_scale` 两个方向都试，但归一化会先毁掉信息）。
    """
    stamped = (page or {}).get("_engine_px")
    if isinstance(stamped, (list, tuple)) and len(stamped) >= 2:
        try:
            w, h = float(stamped[0]), float(stamped[1])
        except (TypeError, ValueError):
            w = h = 0.0
        if w > 0 and h > 0:
            return (w, h)
    ocr = (page or {}).get("overall_ocr_res") or {}
    for key in ("page_size", "page_shape", "input_shape"):
        v = ocr.get(key)
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            try:
                w, h = float(v[0]), float(v[1])
            except (TypeError, ValueError):
                continue
            if w > 0 and h > 0:
                return (w, h)
    for key in ("width", "page_width"):
        if key in (page or {}):
            w = float(page.get(key) or 0)
            h = float(page.get("height") or page.get("page_height") or 0)
            if w > 0 and h > 0:
                return (w, h)
    return None


def _engine_page_px(page: dict) -> tuple[float, float] | None:
    """从一页 layout 结果里取引擎渲染出的**像素**页尺寸（归一化成 w ≤ h）

    **首选 `_engine_px`**：由 `DocParseClient._stamp_engine_px` 从响应顶层
    `result.dataInfo.pages[i] = {width, height}` 抄过来的。这是唯一**实测存在**的
    来源（真机实测 page-3：dataInfo 给 1224×1584，PDF 页 612×792 → 恰好 2.0 倍，
    即引擎按 144 DPI 栅格化）。此前只在这里按候选键找 `page_size`/`width`/…，而
    实测这些键在页结果里**一个都不存在**（页结果只有 model_settings /
    parsing_res_list / doc_preprocessor_res / layout_det_res / overall_ocr_res），
    于是恒返回 None → 永不裁图 → 图区理解整条链静默失效（只留一条
    `layout_figure_crop_incomplete` 警告）。

    其余候选键继续保留：换引擎/服务端版本时仍可能命中，取不到才返回 None ——
    调用方据此退回"不换算"，而不是拿一个猜的值去缩所有坐标。
    """
    px = _engine_page_px_raw(page)
    if not px:
        return None
    return (px[0], px[1]) if px[0] <= px[1] else (px[1], px[0])


def _iou(a, b) -> float:
    """两个 [x0,y0,x1,y1] 框的交并比（任一为空或退化 → 0）

    用途：版面引擎给的 figure 区域与 pdfplumber 抽到的嵌入图对象往往是**同一张图**，
    靠它判定"是不是同一块"（见 doc_parser._dedupe_image_elements）。
    """
    try:
        ax0, ay0, ax1, ay1 = [float(v) for v in a[:4]]
        bx0, by0, bx1, by1 = [float(v) for v in b[:4]]
    except (TypeError, ValueError, IndexError):
        return 0.0
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


# ── 引擎坐标口径：像素 → PDF point ──────────────────────────
#
# 引擎（PaddleX）把每页先栅格化成位图再推理，`block_bbox` 是**那张位图的像素坐标**
# （实测 144 DPI：PDF 612×792 pt 的页，`dataInfo` 报 1224×1584，figure 框 x 可到
# 1091 而 PDF 只有 612 宽 → 恰好 2.0 倍）。
#
# ⚠ 这里有**两个**坐标系，只按倍率换算是不够的（实测踩过，见 trouble-shooting TS-027）：
#   · `dataInfo.pages[i]` 报的是**输入栅格**（引擎收到的那张图，方向 = PDF 页方向）；
#   · `block_bbox` 报的是**转正后**的栅格坐标 —— 文档方向分类一旦判出页面需要旋转
#     （`doc_preprocessor_res.angle` = 90/180/270），引擎先把图转正再检测，框就落在
#     "转过之后"的坐标系里，而 dataInfo 仍是输入尺寸。
# 麦肯锡那篇 116 页里 114 页 `angle=0`（两个坐标系重合，所以这个坑一直没暴露），
# p10/p56 是整页照片页、被判成需要转 90°/270°：框是 1684×1191（横），dataInfo 却是
# 1191×1684（竖）→ 换算结果 842pt 落进 595pt 宽的页里 → 校验拒绝 → **这两页的图
# 一个字都没进库**（连 image_caption 块都没有），而"图上内容搜不到"在质量报告里
# 看不出任何异常。所以换算分三步：倍率 → 按角度逆变换 → 落页校验 + 夹框。
#
# 校验不过就当"这个框不可用"（返回 None）：宁可退回"整页渲染"的粗粒度描述，
# 也不要把别的区域当成图裁给 VLM（那会产出一段看起来正常、其实与图无关的描述）。
#
# 倍率只有两个可靠来源：引擎自报的栅格页尺寸，与 PDF 的页尺寸（不拿 OCR 行高反推 ——
# 实测推出 2.93、真实 2.0，属于"看着有依据、其实全错"）。

def engine_px_scale(page: dict, page_w_pt: float, page_h_pt: float) -> float | None:
    """引擎栅格像素 → PDF point 的缩放倍率（取不到 / 不合理 → None）

    两个候选方向都试（引擎可能把尺寸报成 (h, w)）：同一张栅格图两个方向必然同倍率，
    差异过大说明取到的字段不是栅格尺寸 → 返回 None（不拿猜的值去缩所有坐标）。
    """
    px = _engine_page_px_raw(page)
    if not px or page_w_pt <= 0 or page_h_pt <= 0:
        return None
    for cand in (px, (px[1], px[0])):
        sx, sy = cand[0] / page_w_pt, cand[1] / page_h_pt
        if sx <= 0 or sy <= 0 or abs(sx - sy) / max(sx, sy) > 0.15:
            continue
        if not (0.2 <= sx <= 6.0):      # 200 DPI ≈ 2.78；超出这个区间一定是错值
            continue
        return (sx + sy) / 2.0
    return None


def engine_frame_angle(page: dict) -> int:
    """这一页引擎把栅格**转正了多少度**（0/90/180/270；取不到或不是直角按 0）

    来源是 `doc_preprocessor_res.angle`（PaddleX 的文档方向分类结果）。它不是 0 时，
    `block_bbox` 落在**转正之后**的坐标系里，而 `dataInfo` 仍报输入栅格尺寸 —— 两个
    坐标系差一个 90° 旋转（见本段顶部注释与 `_rotate_box_to_input`）。
    """
    pre = (page or {}).get("doc_preprocessor_res") or {}
    for key in ("angle", "rotate_angle", "rotation"):
        if key not in pre:
            continue
        try:
            a = int(round(float(pre.get(key) or 0))) % 360
        except (TypeError, ValueError):
            continue
        return a if a in (0, 90, 180, 270) else 0
    return 0


def _rotate_box_to_input(box, angle: int, wi: float, hi: float) -> list[float]:
    """把"转正后帧"的框转回**输入栅格**（单位 px）

    公式是**对着真值量出来的**，不是从文档推的：造一张"内容已知旋转 θ"的页送服务，
    用未旋转版的框顺时针转 θ 度当truth，再比 IoU（`tmp_selftest/t_angle_convention.py`）：

        内容旋转 90°  → 服务报 angle=90   本函数 IoU 0.953   （不做变换 0.120）
        内容旋转 180° → 服务报 angle=180  本函数 IoU 0.906   （不做变换 0.113）
        内容旋转 270° → 服务报 angle=270  本函数 IoU 0.903   （不做变换 0.722）
    """
    x0, y0, x1, y1 = [float(v) for v in box]
    if angle == 90:            # 转正 = 逆时针 90°
        return [wi - y1, x0, wi - y0, x1]
    if angle == 180:
        return [wi - x1, hi - y1, wi - x0, hi - y0]
    if angle == 270:           # 转正 = 顺时针 90°
        return [y0, hi - x1, y1, hi - x0]
    return [x0, y0, x1, y1]


def clamp_box_to_page(box, page_w_pt: float, page_h_pt: float) -> list[float]:
    """把框**夹到页内**（夹完退化则返回 []，语义是"这个框不可用"）

    为什么需要夹而不是让它抛：PDF 里内嵌图对象的框常常比 MediaBox 大
    0.05~1.3pt（导出工具留的出血位），pdfplumber 的 `page.crop()` 遇到这种框直接抛
    `ValueError: Bounding box … not within page`；原实现 `except: continue` 把它
    **静默跳过** —— 麦肯锡那篇 26 个内嵌图对象里 18 个走了这条路（其中 10 个是整页
    扫描图），"图没了"在库里只表现为"这页没有 image_caption"。夹一下只少 1pt 边缘
    像素，比整张图丢掉划算得多。
    """
    try:
        x0, y0, x1, y1 = [float(v) for v in box[:4]]
    except (TypeError, ValueError, IndexError):
        return []
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(float(page_w_pt), x1), min(float(page_h_pt), y1)
    if x1 - x0 < 4 or y1 - y0 < 4:              # 夹完只剩一条边：当不可用
        return []
    return [x0, y0, x1, y1]


def engine_bbox_to_points(page: dict, bbox, page_w_pt: float,
                          page_h_pt: float) -> list[float] | None:
    """引擎像素框 → PDF point 框（**落在页内**才返回，否则 None）

    两个候选按序试，取第一个能通过"落在页内"校验的：
      ① 按 `doc_preprocessor_res.angle` 逆变换（引擎转过栅格时**唯一正确**的那个）；
      ② 原样 —— angle=0（绝大多数页），或引擎报了角度但框其实还在输入坐标系里。
    两个都不过就返回 None，调用方据此跳过裁剪（少裁一张图，比把页眉/整页当成图
    交给 VLM 好）。返回的是**PDF point、已夹到页内**的框。
    """
    scale = engine_px_scale(page, page_w_pt, page_h_pt)
    if scale is None:
        return None
    try:
        nums = [float(v) for v in bbox[:4]]
    except (TypeError, ValueError, IndexError):
        return None
    x0, y0, x1, y1 = nums
    px = _engine_page_px_raw(page) or (page_w_pt * scale, page_h_pt * scale)
    wi, hi = px[0], px[1]
    angle = engine_frame_angle(page)

    cands: list[list[float]] = []
    if angle:
        # 转正后的帧：转 90/270 时宽高互换。先确认框落在**校正帧**内，再逆变换。
        fw, fh = (hi, wi) if angle in (90, 270) else (wi, hi)
        f0, f1 = sorted((x0, x1))
        g0, g1 = sorted((y0, y1))
        ftol = 0.02 * max(fw, fh)
        if (f0 >= -ftol and g0 >= -ftol
                and f1 <= fw + ftol and g1 <= fh + ftol):
            cands.append(_rotate_box_to_input([f0, g0, f1, g1], angle, wi, hi))
    cands.append([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)])

    tol = max(page_w_pt, page_h_pt) * 0.02     # 2% 容差：引擎框常贴边
    for cand in cands:
        bx0, by0, bx1, by1 = [v / scale for v in cand]
        bx0, bx1 = sorted((bx0, bx1))
        by0, by1 = sorted((by0, by1))
        if bx0 < -tol or by0 < -tol \
                or bx1 > page_w_pt + tol or by1 > page_h_pt + tol:
            continue
        box = clamp_box_to_page([bx0, by0, bx1, by1], page_w_pt, page_h_pt)
        if box:
            return box
    return None


def crop_page_region(page, bbox_pt, resolution: int = 150) -> bytes | None:
    """按 PDF point 框裁剪页面区域 → PNG 字节（失败返回 None，不抛）

    与 doc_parser 抽内嵌图用的是同一套（pdfplumber `page.crop().to_image()`），
    于是"引擎框裁出来的图"与"嵌入图"在下游完全同构（同样交给 VLM 描述）。
    裁剪前先**夹到页内**：出血位框不该让整张图丢掉（见 `clamp_box_to_page`）。

    ⚠ 全程持 `pdf_render.PDFIUM_LOCK`：`to_image()` 的后端是 PDFium，**它不是线程
    安全的**。入库侧有 4 个 worker、Web 侧一屏 12 张缩略图并发渲染，不加锁实测
    7~12/12 抛 `MalformedPDFException`，而且崩过之后同进程会持续失败（见 TS-033）。
    """
    from rag.adapters.pdf_render import PDFIUM_LOCK
    try:
        with PDFIUM_LOCK:
            box = clamp_box_to_page(bbox_pt,
                                    float(getattr(page, "width", 0) or 0),
                                    float(getattr(page, "height", 0) or 0))
            if not box:
                return None
            x0, y0, x1, y1 = box
            im = page.crop((x0, y0, x1, y1)).to_image(resolution=resolution)
            buf = io.BytesIO()
            im.original.save(buf, format="PNG")
            return buf.getvalue()
    except Exception as e:                                  # noqa: BLE001
        log.warning("crop_page_region_failed",
                    error=f"{type(e).__name__}: {e}"[:160])
        return None


# ── PDF 分页送服务 ──────────────────────────────────────────

def pdf_page_count(data: bytes) -> int:
    """PDF 页数（读不到返回 0 → 调用方退回"整篇一次送"）"""
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:                                       # noqa: BLE001
        return 0


def split_pdf_pages(data: bytes, start: int, count: int) -> bytes:
    """取 [start, start+count) 页拼成新 PDF（失败抛异常，由调用方决定退路）"""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    for i in range(start, min(start + count, len(reader.pages))):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _deep_find(obj, keys: tuple[str, ...], max_depth: int = 4) -> str:
    """在嵌套 dict/list 里按候选键名找第一个非空字符串值

    「表格识别」的响应结构在不同 PaddleX 版本里包了几层（`prunedResult` 里可能是
    `html`、`table_html`，也可能再套一层 `table_res_list`）—— 与其赌某一种，不如
    按候选键名深挖一层，挖不到就返回空串（调用方据此保留原有文本表格，不中断）。
    """
    if max_depth <= 0 or obj is None:
        return ""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            got = _deep_find(v, keys, max_depth - 1)
            if got:
                return got
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            got = _deep_find(v, keys, max_depth - 1)
            if got:
                return got
    return ""


def blocks_to_elements(page: dict, *, page_num: int = 1) -> list:
    """一页的 parsing_res_list → list[ParsedElement]（阅读顺序即列表顺序）

    ⚠ **坐标口径**：引擎给的 `block_bbox` 是**它自己栅格化后的像素坐标**（实测
    144 DPI：PDF 612×792 pt 的页，`dataInfo` 报 1224×1584，即 2.0 倍），与
    pdfplumber 的 PDF point **不可直接比较**。这里原样保留引擎坐标（元素上存的是
    引擎口径）；要去几何用途（按框裁图）必须先用 `engine_bbox_to_points` 换算，
    它靠"引擎自报的栅格页尺寸 ÷ PDF 页尺寸"求倍率并校验结果落在页内 —— 唯一可靠
    的换算路径。
    图片**去重**不用坐标（跨坐标系比 IoU 只会得出"不是同一张"的错误结论），走
    顺序配对，见 doc_parser._dedupe_image_elements。

    返回的是 rag.models.ParsedElement；导入放在函数内，避免本模块（适配器）与
    模型模块在导入期互相牵扯。
    """
    from rag.models import ContentType, ParsedElement

    out: list[ParsedElement] = []
    keep_empty = _LAYOUT_IMAGE_LABELS | _LAYOUT_FORMULA_LABELS
    for block in (page.get("parsing_res_list") or []):
        if not isinstance(block, dict):
            continue
        label = str(block.get("block_label") or "").strip().lower()
        text = str(block.get("block_content") or "").strip()
        bbox = block.get("block_bbox")
        if label in _LAYOUT_DROP_LABELS:
            continue
        # 图 / 公式区域允许"有框无字"（引擎对纯图、纯公式常不给 block_content）。
        # 丢掉它们就是丢内容：后面按框裁图 → VLM 描述 / 公式识别正是为它们准备的
        if not text and label not in keep_empty:
            continue
        if label in _LAYOUT_TITLE_LABELS:
            # 引擎的标题层级（block 里若有 level/heading_level 就采用，否则按 1 级）。
            # 宁可全判一级也不猜：OutlineStep 会用标题栈拼 section_path，猜错层级
            # 比层级缺失更难排查。
            level = block.get("heading_level") or block.get("level") or 1
            try:
                level = max(1, min(int(level), 4))
            except (TypeError, ValueError):
                level = 1
            out.append(ParsedElement(
                content_type=ContentType.TITLE, text=text, page_num=page_num,
                bbox=bbox, metadata={"heading_level": level,
                                     "source": "layout", "block_label": label}))
        elif label in _LAYOUT_TABLE_LABELS:
            out.append(ParsedElement(
                content_type=ContentType.TABLE, text=text, page_num=page_num,
                bbox=bbox,
                # table_html 供后续结构化入 table_data；没有结构时下游退回文本
                raw_data={"table_html": text, "block_label": label},
                metadata={"source": "layout", "block_label": label}))
        elif label in _LAYOUT_IMAGE_LABELS:
            out.append(ParsedElement(
                content_type=ContentType.IMAGE, text=text, page_num=page_num,
                bbox=bbox,
                # layout_region：标记"这是引擎给的区域、还没有图字节"——
                # doc_parser 按框裁图 / region_enhance 按能力补内容时据此筛选
                raw_data={"layout_region": True},
                metadata={"source": "layout", "block_label": label}))
        elif label in _LAYOUT_FORMULA_LABELS:
            out.append(ParsedElement(
                content_type=ContentType.TEXT, text=text, page_num=page_num,
                bbox=bbox,
                raw_data={"formula_region": True},
                metadata={"source": "layout", "block_label": label}))
        else:
            out.append(ParsedElement(
                content_type=ContentType.TEXT, text=text, page_num=page_num,
                bbox=bbox, metadata={"source": "layout", "block_label": label}))
    return out


def layout_pages_to_elements(pages: list[dict]) -> list:
    """逐页转换；页内没有 parsing_res_list 时退回 overall_ocr_res 的文字行"""
    out: list = []
    for i, page in enumerate(pages or [], start=1):
        els = blocks_to_elements(page, page_num=i)
        if not els:
            els = _ocr_lines_to_elements((page or {}).get("overall_ocr_res") or {},
                                         page_num=i)
        out.extend(els)
    return out


def _ocr_lines_to_elements(ocr_res: dict, *, page_num: int) -> list:
    from rag.models import ContentType, ParsedElement

    texts = [str(t) for t in (ocr_res.get("rec_texts") or [])]
    if not texts:
        return []
    body = "\n".join(t for t in texts if t.strip())
    if not body.strip():
        return []
    scores = [float(s) for s in (ocr_res.get("rec_scores") or [])]
    return [ParsedElement(
        content_type=ContentType.TEXT, text=body, page_num=page_num,
        metadata={"ocr": True, "source": "layout",
                  "ocr_confidence": (sum(scores) / len(scores)) if scores else 0.0})]


class DocParseClient:
    """一个能力的 HTTP 客户端（每次调用现建，轻量、无需常驻连接）"""

    # 预处理开关与它们的请求字段名。**默认全部不发**（见 _payload 的说明）：
    # 这些开关在服务端"没部署对应模型"时会直接 500，而 PaddleX 的 OCR / layout
    # 产线**默认已带文档方向分类与扭曲矫正**（实测响应里的 doc_preprocessor_res
    # 就写着 use_doc_orientation_classify=True / use_doc_unwarping=True），
    # 我们再显式传一遍没有增量收益，只会多一个能配错/能踩坑的旋钮。
    PREPROCESS_FLAGS = {
        "useDocOrientationClassify": "文档方向分类",
        "useDocUnwarping": "扭曲矫正",
        "useTextlineOrientation": "文本行方向",
    }
    # 已确认该端点不支持、**不再发送**的开关：键是 base_url+endpoint，值是开关名集合。
    # 类级缓存：客户端是"每次调用现建"的，实例级缓存等于没有缓存。
    _unsupported: dict[str, set[str]] = {}

    def __init__(self, base_url: str, capability: str,
                 timeout: float = 300.0, api_key: str = "",
                 max_pages_per_request: int = 0):
        self.base_url = base_url.rstrip("/")
        self.capability = doc_parse_capability(capability)
        self.endpoint = doc_parse_endpoint_path(self.capability)
        self.timeout = timeout
        self._headers = ({"Authorization": f"Bearer {api_key}"}
                         if api_key else {})
        self._key = self.base_url + self.endpoint
        # 最近一次 _payload 的原始输入（重试时按原始字节重建请求体）
        self._raw_data: bytes = b""
        self._file_type: int = 0
        # 单次请求最多送几页（0 → 用默认）。见 _pdf_batches。
        self.max_pages_per_request = (max(1, int(max_pages_per_request))
                                      if max_pages_per_request else
                                      _DEFAULT_PDF_BATCH_PAGES)
        # 本次调用过程中**必须让用户看见**的结论（页数被截断等）。
        # 客户端是"每次调用现建"的，所以调用方读完即可，不需要清理。
        self.notes: list[str] = []
        # 最近一次响应的 `result.dataInfo`（引擎栅格页尺寸的**唯一实测来源**）
        self.last_data_info: dict = {}

    def unsupported_flags(self) -> set[str]:
        return set(self._unsupported.get(self._key) or ())

    def mark_unsupported(self, flag: str, reason: str = "") -> None:
        """记下某端点不支持某开关（之后不再发送，也不再刷警告）"""
        cur = self._unsupported.setdefault(self._key, set())
        if flag not in cur:
            cur.add(flag)
            log.warning("doc_parse_flag_unsupported", endpoint=self._key,
                        flag=flag, note=(reason or "")[:160],
                        effect="该端点不再发送此预处理开关")

    # ── 调用 ────────────────────────────────────────────────

    async def _post_once(self, url: str, payload: dict) -> "httpx.Response":
        async with httpx.AsyncClient(timeout=self.timeout) as hc:
            return await hc.post(url, json=payload, headers=self._headers)

    async def _post(self, payload: dict) -> dict:
        """POST 能力端点，带两级容错

        ① 传输层抖动（连接重置/读超时/服务刚崩过一瞬）→ 退避重试两次。
           实测遇到过 PaddleX 服务在收到首个真实 PDF 时崩一次、随后恢复：
           这类抖动不该把整篇文档判失败（"服务不可达"与"服务说不行"是两回事）。
        ② 预处理开关导致 500（服务端没部署对应模型）→ 去掉全部开关重试一次，
           并记一条 warning（预处理是增强项，不能因为可选模型缺失就让解析失败）。
        """
        url = self.base_url + self.endpoint
        delays = (1.0, 2.0)
        last: Exception | None = None
        resp = None
        for attempt in range(len(delays) + 1):
            try:
                resp = await self._post_once(url, payload)
                break
            except Exception as e:                     # 传输层
                last = e
                if attempt < len(delays):
                    log.warning("doc_parse_transport_retry",
                                endpoint=url, attempt=attempt + 1,
                                error=f"{type(e).__name__}: {str(e)[:120]}")
                    await asyncio.sleep(delays[attempt])
        if resp is None:
            raise DocParseTransient(
                f"文档解析服务不可达（{url}）："
                f"{type(last).__name__}: {str(last)[:150]}") from last

        if resp.status_code == 500 and any(payload.get(k)
                                           for k in self.PREPROCESS_FLAGS):
            # 服务端缺某个预处理模型 → 500。逐字段找**具体是哪一个**并记进端点级
            # 缓存（之后不再发送），再用同一份"不带开关"的载荷重试一次。
            # 为什么定位到字段而不是"整个去掉"：三个开关各需服务端部署**不同**的
            # 模型，一股脑去掉会把服务端其实支持的能力也丢掉；缓存之后不再复发，
            # 也不会每次调用都刷一条警告。
            await self._locate_unsupported_flags(url, payload)
            # 重试载荷**从原始字节重建**（payload["file"] 是 base64 文本，
            # 直接 b64decode 再交给 _payload 会多一次无谓往返，且解码失败会抛到
            # 这条错误处理路径之外）
            retry_payload = self._payload(self._raw_data, file_type=self._file_type,
                                          visualize=bool(payload.get("visualize")))
            try:
                resp = await self._post_once(url, retry_payload)
            except Exception as e:
                raise DocParseTransient(
                    f"文档解析服务不可达（{url}）："
                    f"{type(e).__name__}: {str(e)[:150]}") from e

        if resp.status_code == 404:
            raise DocParseEndpointMissing(
                f"服务上没有 {self.endpoint} 这个端点（HTTP 404）：该地址上跑的"
                f"不是「{self.capability}」产线。PaddleX 一个服务实例只挂一条产线，"
                "请用 `--pipeline` 起对应产线的服务，或把这条配置的「处理能力」"
                "改成该服务实际提供的能力")
        if resp.status_code != 200:
            # 4xx = 这次的请求/文件不被接受（送错类型、文件损坏、尺寸不合规…）：
            # 重试不会变好，单独抛一类让上层直接失败并说清原因（见 DocParseBadInput）
            if 400 <= resp.status_code < 500:
                raise DocParseBadInput(
                    f"文档解析服务拒收该文件（HTTP {resp.status_code}）"
                    f"（{self.base_url}{self.endpoint}）：{resp.text[:200]}",
                    status=resp.status_code)
            # 5xx = 服务活着但这次推断失败：**可恢复**，上层不据此永久停用该能力
            raise DocParseTransient(
                f"文档解析服务返回 HTTP {resp.status_code}"
                f"（{self.base_url}{self.endpoint}）：{resp.text[:200]}")
        body = resp.json()
        if body.get("errorCode"):
            raise DocParseTransient(
                f"文档解析服务返回错误 {body.get('errorCode')}："
                f"{str(body.get('errorMsg'))[:200]}")
        result = body.get("result") or {}
        # 记下本次响应的 dataInfo（含**引擎栅格页尺寸**）：`_root()` 只取结果列表，
        # 这个信息在列表之外，丢掉它就没法把引擎像素框换算成 PDF point（见
        # `_engine_page_px`）。每次 _post 覆盖，随后的 _stamp_engine_px 立即消费。
        self.last_data_info = result.get("dataInfo") or {}
        return result

    def _stamp_engine_px(self, items: list[dict]) -> list[dict]:
        """把 dataInfo 里的引擎栅格页尺寸盖到每一页结果上（键 `_engine_px`）

        为什么盖在页字典里而不是另开一个返回值：这个尺寸一路要走到
        `LayoutAdapter.to_page_points(page, …)`（按框裁图），而那里只拿得到"这一页的
        结果字典"。随页携带 = 不新增参数、不依赖调用方记得转交。

        `dataInfo.pages[i]` 与**本次请求**的页序一一对应；`_pdf_batches` 的分批/二分
        重试都是"一次请求内索引对齐"，所以在每个请求返回后立刻盖章是安全的。

        ⚠ 盖在 `prunedResult` 里（而不是外层 item）：`parse_layout` 返回的是
        `dict(item["prunedResult"])`，盖在外层会在那一刻被丢掉。
        """
        pages = (self.last_data_info or {}).get("pages") or []
        for i, item in enumerate(items or []):
            if not isinstance(item, dict) or i >= len(pages):
                continue
            wh = pages[i] or {}
            try:
                w = float(wh.get("width") or 0)
                h = float(wh.get("height") or 0)
            except (TypeError, ValueError):
                continue
            if w > 0 and h > 0:
                inner = item.get("prunedResult")
                (inner if isinstance(inner, dict) else item)["_engine_px"] = [w, h]
        return items

    async def _locate_unsupported_flags(self, url: str, payload: dict) -> None:
        """逐个试探请求体里的预处理开关，把触发 500 的记进端点级缓存

        为什么逐个试而不是一次性全去掉：三个开关各自需要服务端部署**不同**的模型，
        把"服务端其实支持"的也一起关掉就白丢能力。找到之后记住，之后不再发送，
        也不会每次调用都刷一条警告。
        """
        for flag in [k for k in self.PREPROCESS_FLAGS if payload.get(k)]:
            probe = dict(payload)
            for other in self.PREPROCESS_FLAGS:
                probe.pop(other, None)
            probe[flag] = True
            try:
                r = await self._post_once(url, probe)
            except Exception:
                continue                     # 传输层问题不归因到开关
            if r.status_code == 500:
                self.mark_unsupported(
                    flag, f"单独发送 {flag}=true 时服务端返回 HTTP 500"
                          f"（大概率未部署对应的预处理模型）")
            else:
                # 单独发没问题 → 说明是"组合"才炸，无法归因到单个字段。
                # 这种情况不猜，留着让调用方按 need 决定（当前默认根本不发开关）。
                log.info("doc_parse_flag_ok_alone", endpoint=url, flag=flag,
                         status=r.status_code)

    def _root(self, result: dict) -> list[dict]:
        key = RESPONSE_ROOT.get(self.capability, "ocrResults")
        items = result.get(key)
        if not isinstance(items, list):
            raise DocParseUnavailable(
                f"响应里没有 {key}：这个地址上跑的可能不是「{self.capability}」产线"
                "（请检查该条配置的「处理能力」与实际启动的 --pipeline 是否一致）")
        return items

    def _payload(self, data: bytes, *, file_type: int,
                 visualize: bool = False) -> dict:
        """构造请求体，并**记住原始字节**（重试时按原始数据重建，见 _post 分支②）

        **默认不发任何预处理开关**，理由有二（都来自实测）：
          · PaddleX 的 OCR / layout 产线**默认已开**文档方向分类与扭曲矫正
            （实测响应里 `doc_preprocessor_res.model_settings` 就是两个 True），
            再显式传一遍没有增量收益；
          · 这些开关在服务端未部署对应模型时会返回 **500**（实测该 layout 服务的
            `useTextlineOrientation` 必 500）。带着它发等于每次调用都"先失败一次
            再重试"——既浪费一次往返，又在日志里刷一条看着像故障的警告。
        """
        self._raw_data, self._file_type = data, file_type
        return {
            "file": base64.b64encode(data).decode("ascii"),
            "fileType": file_type,          # 0=PDF，1=图像
            "visualize": visualize,
        }

    async def ocr_image(self, image: bytes) -> dict:
        """单张图片 OCR → {"text": str, "avg_confidence": float, "lines": [...]}"""
        items = self._root(await self._post(
            self._payload(image, file_type=1)))
        return self._normalize_ocr(items[0] if items else {})

    async def ocr_pdf_pages(self, pdf: bytes) -> list[dict]:
        """整篇 PDF OCR → 逐页结果（服务端按页返回，顺序即页序）

        **按页分批**送（见 `_pdf_batches`）：服务端 `max_num_input_imgs` 默认 10，
        整篇送过去只会返回前 10 页且 HTTP 200 —— 少了的页在入库侧表现为"文档就
        这么长"，无从归因。
        """
        items = await self._pdf_batches(pdf)
        return [self._normalize_ocr(it) for it in items]

    @staticmethod
    def _normalize_ocr(item: dict) -> dict:
        pruned = (item or {}).get("prunedResult") or {}
        texts = [str(t) for t in (pruned.get("rec_texts") or [])]
        scores = [float(s) for s in (pruned.get("rec_scores") or [])]
        lines = list(zip(texts, scores))
        return {
            "text": "\n".join(texts),
            "avg_confidence": (round(sum(scores) / len(scores), 4)
                               if scores else 0.0),
            "lines": lines,
            "chars": sum(len(t) for t in texts),
        }

    async def parse_layout(self, pdf: bytes, on_batch=None) -> list[dict]:
        """版面解析 → 逐页 {parsing_res_list, layout_det_res, overall_ocr_res, ...}

        原样返回 prunedResult（不做字段改名）：上层拿到的键名与 PaddleX 官方文档
        一致，排查问题时不必再对照一层映射。字段含义见
        `doc/pdf_layout_vlm_design_draft.md` §4.1。
        **按页分批**送，理由同 `ocr_pdf_pages`。

        `on_batch(done_pages, total_pages)`：可选进度回调（**异步**）。
        为什么需要它：一份 96 页的文档要分 10 批送、每批 6~7 秒，整段是静默的
        —— 用户在界面上看到的是"排队中"卡住一两分钟，然后突然跳状态。
        回调让调用方能把"已解析 N/M 页"报上去。
        """
        items = await self._pdf_batches(pdf, on_batch=on_batch)
        return [dict((it or {}).get("prunedResult") or {}) for it in items]

    async def _pdf_batches(self, pdf: bytes, on_batch=None) -> list[dict]:
        """整篇 PDF 分批调用 → 拼接后的逐页原始 items

        为什么必须分批：PaddleX serving 的 `max_num_input_imgs` 默认 **10**，一次
        送 50 页过去它只处理前 10 页并返回 HTTP 200 —— 入库"成功"、页数少 80%，
        属于最难发现的一类静默失败（页数在界面上看起来就是文档本来的页数）。

        两层保险：
          ① 只按 `max_pages_per_request` 送，不赌服务端上限；
          ② **自校验**：某一批返回的条数少于该批页数，说明服务端上限比我们以为的
             更小 → 把这一批二分重试，直到对齐或退到单页。结论同时记进 `self.notes`
             （调用方转成质量报告里的可见告警）。
        """
        total = pdf_page_count(pdf)
        if total <= 0:
            # 页数读不出来（加密/结构异常）：退回整篇一次送，行为与历史一致
            return self._stamp_engine_px(
                self._root(await self._post(self._payload(pdf, file_type=0))))

        async def run(start: int, count: int) -> list[dict]:
            chunk = pdf if count == total else \
                split_pdf_pages(pdf, start, count)
            items = self._stamp_engine_px(
                self._root(await self._post(self._payload(chunk,
                                                          file_type=0))))
            if count > 1 and len(items) < count:
                self.note(f"第 {start + 1}-{start + count} 页只返回 "
                          f"{len(items)} 页结果，已按更小批次重试"
                          "（服务端 max_num_input_imgs 小于 "
                          f"{self.max_pages_per_request}）")
                log.warning("doc_parse_page_truncation", endpoint=self._key,
                            start=start + 1, sent=count, got=len(items),
                            effect="按更小批次二分重试")
                mid = count // 2
                return (await run(start, mid)) + \
                    (await run(start + mid, count - mid))
            return items

        out: list[dict] = []
        for start in range(0, total, self.max_pages_per_request):
            count = min(self.max_pages_per_request, total - start)
            out.extend(await run(start, count))
            # 每批完成后回调一次（异步）：调用方据此把进度报到界面。
            # 回调自身出错不能带走入库 —— 进度上报是增强，不是前提。
            if on_batch is not None:
                try:
                    await on_batch(len(out), total)
                except Exception as e:                 # noqa: BLE001
                    log.warning("doc_parse_batch_cb_failed",
                                error=f"{type(e).__name__}: {e}"[:160])
        if len(out) < total:
            # 单页都拿不回来：只能如实说"少了几页"，不能假装这篇文档就这么长
            self.note(f"共 {total} 页，服务端只返回 {len(out)} 页结果"
                      "（缺失的页不会入库）")
            log.warning("doc_parse_pages_missing", endpoint=self._key,
                        total=total, got=len(out))
        return out

    def note(self, message: str) -> None:
        """记一条**要给用户看**的结论（去重，避免分批重试时刷屏）"""
        if message and message not in self.notes:
            self.notes.append(message)

    # ── 表格 / 公式区域识别（可选能力）─────────────────────────

    async def recognize_table(self, image: bytes) -> dict:
        """表格区域图 → {"table_html", "cells", "cell_texts", "cell_boxes"}

        用途：版面引擎只给了框（或有文字但没结构）的表格，用这一能力把区域图再走
        一遍表格识别，拿到 HTML 供 TableExtractStep 转成行列进 table_data。
        未配置该能力时调用方根本不发请求（见 region_enhance 步骤的门禁）。

        **字段名是拿真响应核对的**（18082，实测键树）：

            prunedResult
              ├─ overall_ocr_res.{rec_texts, rec_scores, rec_polys, rec_boxes}
              └─ table_res_list[0]
                   ├─ pred_html        ← HTML 在这里
                   ├─ cell_box_list    ← 每个单元格的框（39 个）
                   └─ table_ocr_pred.{rec_texts, …}

        原实现只按候选键名在整棵结果树里深挖，HTML 恰好挖到了（`pred_html` 在候选
        列表里），但 `cells` 取的是 `prunedResult.cells` / `table_cells` —— 实测这两个
        键一个都不存在，于是**永远是空列表**。单元格框是"表格里的图"唯一的定位依据，
        所以这里按实测路径取。
        """
        items = self._root(await self._post(
            self._payload(image, file_type=1)))
        first = (items[0] if items else {}) or {}
        pruned = first.get("prunedResult") or first
        res0: dict = {}
        for r in (pruned.get("table_res_list") or []):
            if isinstance(r, dict) and r:
                res0 = r
                break
        html = str(res0.get("pred_html") or "").strip() \
            or _deep_find(pruned, ("table_html", "html", "structure", "pred_html"))
        cells = res0.get("cell_box_list") or pruned.get("cells") \
            or pruned.get("table_cells") or []
        ocr = res0.get("table_ocr_pred") or {}
        return {
            "table_html": html,
            "cells": cells,
            # 单元格文字与框同序：拼不出 HTML 时还能按框位置组行（见 TableExtractStep）
            "cell_texts": [str(t) for t in (ocr.get("rec_texts") or [])],
            "cell_boxes": list(ocr.get("rec_boxes") or []),
        }

    async def recognize_formula(self, image: bytes) -> dict:
        """公式区域图 → {"text": str}（latex 或纯文本）

        用途：引擎只给框、没给 block_content 的公式区域 —— 不做这一步，公式就
        完全不在正文里（用户按公式里的符号检索永远检索不到）。

        响应形状按 PaddleX 公式识别产线的实测约定：结果在
        `prunedResult.formula_res_list[*].rec_formula`（一张区域图可能识别出多个
        公式，按顺序全部拼起来）；`_deep_find` 保留为换版本时的兜底。

        ⚠ 本环境 18083 目前 `/health` 200 但每次推断都 HTTP 500（换 4 张图 × 3 次
        全部失败），所以这条路径的字段名**只做过空结果响应的核对**（那时
        `formula_res_list` 是空列表、键名可见）；等服务恢复正常后要用真公式图再核
        一次，别把"调用成功但取不到内容"当成正常。
        """
        items = self._root(await self._post(
            self._payload(image, file_type=1)))
        first = (items[0] if items else {}) or {}
        pruned = first.get("prunedResult") or first
        parts: list[str] = []
        for r in (pruned.get("formula_res_list") or []):
            if not isinstance(r, dict):
                continue
            t = str(r.get("rec_formula") or r.get("latex") or "").strip()
            if t:
                parts.append(t)
        text = "\n".join(parts)
        if not text:
            text = _deep_find(pruned, ("rec_formula", "latex", "formula",
                                       "formula_text", "text"))
        if not text:                     # 再兜一层：某些版本只回 OCR 文本行
            texts = [str(t) for t in (pruned.get("rec_texts") or []) if t]
            text = "\n".join(texts)
        return {"text": text}

    # ── 探测（「测试模型」按钮与容器自检共用）──────────────────

    async def probe(self) -> tuple[bool, str, bool]:
        """真打一次能力端点，并用**响应字段根名**校验产线是否选对

        返回 (ok, 原因, definitive)：
          definitive=True  —— 结论确定，重试不会改变（端点 404、字段根名不符）
          definitive=False —— 服务端 5xx/超时/连不上，属可恢复，**不该被长期缓存**
                              （实测遇到过 layout 服务偶发 500：把它记成"不可用"会让
                              整个进程周期内不再尝试，等于把一次抖动放大成永久降级）

        为什么不只探 `/health`：PaddleX 每个服务都有存活探针，它只证明"服务活着"。
        一个实例只挂一条产线，能力选错时 /health 照样 200、界面照样绿 —— 直到入库
        调用真正的端点才 404。所以这里探真实端点，并按字段根名确认产线身份
        （同族缺陷见 trouble-shooting 的 TS-009 / TS-011 / TS-017）。
        """
        key = RESPONSE_ROOT.get(self.capability, "ocrResults")
        try:
            result = await self._post(self._payload(
                base64.b64decode(_probe_image_b64()), file_type=1))
        except DocParseEndpointMissing as e:
            return False, str(e), True
        except DocParseUnavailable as e:
            return False, str(e), False
        if not isinstance(result.get(key), list):
            got = "、".join(k for k in result.keys()) or "空"
            return False, (f"端点可达但响应字段不是 {key}（实际：{got}）——"
                           f"这个地址上跑的不是「{self.capability}」产线，"
                           "请核对启动时的 --pipeline 与这条配置的「处理能力」"), True
        return True, f"{self.endpoint} 可用，响应字段 {key} 已校验", True
