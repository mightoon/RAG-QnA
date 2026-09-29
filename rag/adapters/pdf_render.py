"""
PDF → 位图渲染（rag/adapters/pdf_render.py）

**这个模块存在的唯一理由：PDFium 不是线程安全的，而我们的渲染调用来自多个线程。**

实测（`tmp_selftest/t_pdfium_concurrency.py`，同一份 11.5MB PDF / 116 页，每进程只跑
一种模式，结果可复现）：

| 模式 | 失败 | 用时 |
|---|---|---|
| 顺序渲染 12 次（无线程） | **0/12** | 2.1s |
| 并发 12 个线程 **+ 本模块的锁** | **0/12** | 2.1s |
| 并发 12 个线程（不加锁） | **7~12/12** | — |

失败长这样：`MalformedPDFException: Failed to load document (PDFium: Data format error)`
（偶发还带 `PdfiumError: Failed to load page`）。更糟的是**一旦这样崩过，同一进程里
后续渲染也会持续失败**（PDFium 全局态被搞坏）—— 这正是用户看到的"分块里的图全裂、
日志里一条 PDFium 报错、点开页面位置的接口 500"。

触发场景很日常：详情页分块一屏 12 张缩略图，浏览器**并发**打 12 个请求，每个都在
`asyncio.to_thread` 里各自 `pdfplumber.open()` + `page.to_image()`。

所以约定：**所有 PDF 位图渲染都从这里走**（`PDFIUM_LOCK` 是进程内唯一那把锁，
入库解析与 Web 端点共用同一把，才能真的串起来）。加锁不额外花时间 —— 渲染本身是
CPU 密集的活，锁只是把调用排队（上表 2.1s vs 2.1s）。
"""
from __future__ import annotations

import io
import threading
import time

from rag.observability.logging import get_logger

log = get_logger("rag.pdf_render")

# 进程内唯一的 PDFium 串行锁（可重入：允许在持锁的调用里再调一次）
PDFIUM_LOCK = threading.RLock()

# 渲染失败后重试一次：偶发的 PDFium 抖动（服务刚起、字体缓存首次加载）值得一次重试，
# 但**不**在持锁状态下并发重试（那正是要避免的事）
_RETRY_DELAY = 0.15


class RenderError(RuntimeError):
    """渲染失败。

    `transient=True`（默认）：渲染器/库层面的失败（PDFium 抖动、字体缓存、内存），
    重试可能成功 → 端点应回 **503**（"稍后重试"）。
    `transient=False`：请求本身不对（页码越界、框退化、文件不是 PDF 结构）→
    端点应回 **4xx**（重试无用，别让用户以为"过一会儿就好"）。
    """

    def __init__(self, message: str, *, transient: bool = True):
        super().__init__(message)
        self.transient = transient


def _open(pdf_bytes: bytes):
    import pdfplumber
    return pdfplumber.open(io.BytesIO(pdf_bytes))


def render_page_png(pdf_bytes: bytes, page_no: int, *, dpi: int = 100,
                    box=None, pad: float = 3.0) -> bytes:
    """整页渲染成 PNG；`box`（PDF point [x0,y0,x1,y1]）给了就画红框标出来

    红框在**服务端**画：前端要画就得知道"渲染尺寸 ÷ PDF point 尺寸"的比例，而渲染
    按 dpi 走（同一页换 dpi 比例就变）；比例一旦不一致，框就偏移 —— 而偏移在界面上
    "看着像是对的"。这里渲染与画框用同一张图、同一个坐标系，框不可能偏。
    """
    dpi = min(max(int(dpi), 50), 200)

    def _do() -> bytes:
        from PIL import ImageDraw
        with PDFIUM_LOCK, _open(pdf_bytes) as pdf:
            if not (1 <= page_no <= len(pdf.pages)):
                raise RenderError(f"页码越界：{page_no}（共 {len(pdf.pages)} 页）",
                                  transient=False)
            page = pdf.pages[page_no - 1]
            img = page.to_image(resolution=dpi).original
            if box:
                scale = img.width / float(page.width or 1)
                x0 = max(0.0, (float(box[0]) - pad) * scale)
                y0 = max(0.0, (float(box[1]) - pad) * scale)
                x1 = min(float(img.width), (float(box[2]) + pad) * scale)
                y1 = min(float(img.height), (float(box[3]) + pad) * scale)
                if x1 > x0 and y1 > y0:
                    ImageDraw.Draw(img).rectangle([x0, y0, x1, y1],
                                                  outline=(220, 38, 38), width=3)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

    return _with_retry(_do, f"page={page_no} dpi={dpi}")


def crop_region_png(pdf_bytes: bytes, page_no: int, box, *,
                    dpi: int = 150) -> bytes:
    """按 PDF point 框裁一块 → PNG（图区缩略图用）"""
    dpi = min(max(int(dpi), 60), 300)

    def _do() -> bytes:
        from rag.adapters.doc_parse import clamp_box_to_page
        with PDFIUM_LOCK, _open(pdf_bytes) as pdf:
            if not (1 <= page_no <= len(pdf.pages)):
                raise RenderError(f"页码越界：{page_no}（共 {len(pdf.pages)} 页）",
                                  transient=False)
            page = pdf.pages[page_no - 1]
            b = clamp_box_to_page(box, float(page.width), float(page.height))
            if not b:
                raise RenderError("图区框不可用（贴边/退化）", transient=False)
            img = page.crop((b[0], b[1], b[2], b[3])).to_image(resolution=dpi)
            buf = io.BytesIO()
            img.original.save(buf, format="PNG")
            return buf.getvalue()

    return _with_retry(_do, f"page={page_no} dpi={dpi}")


def _with_retry(fn, tag: str) -> bytes:
    """执行渲染；失败重试一次；仍失败则抛 RenderError（带原始原因，便于日志与人看）"""
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return fn()
        except RenderError:
            raise
        except Exception as e:                                  # noqa: BLE001
            last = e
            log.warning("pdf_render_failed", tag=tag, attempt=attempt,
                        error=f"{type(e).__name__}: {str(e)[:160]}")
            if attempt == 1:
                time.sleep(_RETRY_DELAY)
    raise RenderError(f"{type(last).__name__}: {str(last)[:200]}") from last


def crop_via_page_png(pdf_bytes: bytes, page_no: int, box, *,
                      dpi: int = 150) -> bytes:
    """**整页渲染后在像素空间裁**（"看页面位置"那条路）→ PNG

    与 `crop_region_png`（pdfplumber 的 `page.crop().to_image()`）是两条独立路径，
    用来互相复核：实测有"按框裁出白图、同一页整页渲染却是对的"的情形
    （PDFium 状态异常时可能**不抛异常**而是给一张空白栅格），所以 `figure_render`
    发现裁出来是空白时会用这条路再渲一次（见 TS-034）。
    """
    dpi = min(max(int(dpi), 60), 200)

    def _do() -> bytes:
        with PDFIUM_LOCK, _open(pdf_bytes) as pdf:
            if not (1 <= page_no <= len(pdf.pages)):
                raise RenderError(f"页码越界：{page_no}（共 {len(pdf.pages)} 页）",
                                  transient=False)
            page = pdf.pages[page_no - 1]
            img = page.to_image(resolution=dpi).original
            scale = img.width / float(page.width or 1)
            x0 = max(0.0, float(box[0]) * scale)
            y0 = max(0.0, float(box[1]) * scale)
            x1 = min(float(img.width), float(box[2]) * scale)
            y1 = min(float(img.height), float(box[3]) * scale)
            if x1 - x0 < 2 or y1 - y0 < 2:
                raise RenderError("图区框不可用（贴边/退化）", transient=False)
            out = img.crop((round(x0), round(y0), round(x1), round(y1)))
            buf = io.BytesIO()
            out.save(buf, format="PNG")
            return buf.getvalue()

    return _with_retry(_do, f"page={page_no} dpi={dpi} (via full page)")


def image_stddev(png: bytes, size: int = 64) -> float:
    """PNG 的灰度标准差（判断"是不是一张空白图"用）—— 读不出来返回 -1"""
    try:
        from PIL import Image, ImageStat
        im = Image.open(io.BytesIO(png)).convert("L").resize((size, size))
        return float(ImageStat.Stat(im).stddev[0])
    except Exception:                                           # noqa: BLE001
        return -1.0


def render_probe(pdf_bytes: bytes, page_no: int = 1, dpi: int = 50) -> tuple[bool, str]:
    """自检用：能不能渲染这一页（返回 (ok, 原因)）"""
    try:
        render_page_png(pdf_bytes, page_no, dpi=dpi)
        return True, "ok"
    except Exception as e:                                      # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:160]}"
