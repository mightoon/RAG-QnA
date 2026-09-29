"""
图区 / 整页渲染的 Web 侧封装（rag/web/figure_render.py）

三件事，都是被用户实测的故障逼出来的（TS-033）：

1. **PDFium 串行**：真正的渲染在 `rag.adapters.pdf_render` 里，那里有一把进程内唯一的
   锁。这里只调用，不自己开 pdfplumber —— 详情页一屏 12 张缩略图并发时，不加锁实测
   7~12/12 失败。
2. **别把同一份 PDF 拉十几次**：一篇 96 页文档 11.5MB，一屏 12 张缩略图原来是
   "12 次 MinIO 下载 + 12 次解析"。这里按 doc 缓存字节（TTL + 条数上限），并缓存
   渲染结果 PNG（同一张图被反复看：换 dpi、来回切页签、重开弹窗）。
3. **失败要能说出原因**：端点拿到 `RenderError` 时回 503 + 原因（而不是让异常冒到
   ASGI 变 500 堆栈），前端据此提示"稍后重试"。
"""
from __future__ import annotations

import threading
import time

from rag.adapters.pdf_render import (RenderError, crop_region_png,
                                     crop_via_page_png, image_stddev,
                                     render_page_png)
from rag.observability.logging import get_logger

log = get_logger("rag.web.figure")

# 灰度标准差低于它就算"空白图"（真实图实测都 > 20；空白区域是 0.0）
_BLANK_STD = 3.0

# ── PDF 字节缓存：按 doc 存，键是 storage_url（解析后同一篇文档的对象名不变）──
_PDF_TTL = 300.0            # 秒：一篇文档被反复看图的时间窗
_PDF_MAX = 4                # 最多几篇（每篇可能十几 MB，别把内存吃光）
_pdf_cache: dict[str, tuple[bytes, float]] = {}
_pdf_lock = threading.Lock()

# ── 渲染结果缓存：同一张图被反复请求时省掉一次 PDFium 渲染 ──
_PNG_TTL = 600.0
_PNG_MAX = 64               # 12 张缩略图 + 弹窗整页图，留一倍余量
_png_cache: dict[tuple, tuple[bytes, float]] = {}
_png_lock = threading.Lock()


def _get_pdf(key: str) -> bytes | None:
    with _pdf_lock:
        hit = _pdf_cache.get(key)
        if hit and hit[1] > time.monotonic():
            return hit[0]
        _pdf_cache.pop(key, None)
    return None


def _put_pdf(key: str, data: bytes) -> None:
    with _pdf_lock:
        _pdf_cache[key] = (data, time.monotonic() + _PDF_TTL)
        # 超上限时丢最旧的（按到期时间）
        while len(_pdf_cache) > _PDF_MAX:
            oldest = min(_pdf_cache, key=lambda k: _pdf_cache[k][1])
            _pdf_cache.pop(oldest, None)


async def load_pdf(c, doc) -> bytes:
    """取原始 PDF 字节（带缓存）。缓存键用 storage_url，取不到就现拉。"""
    key = str(getattr(doc, "storage_url", "") or "")
    if key:
        hit = _get_pdf(key)
        if hit is not None:
            return hit
    data = await c.storage.get(doc.storage_url)
    if key:
        _put_pdf(key, data)
    return data


def _cache_get(key: tuple) -> bytes | None:
    with _png_lock:
        hit = _png_cache.get(key)
        if hit and hit[1] > time.monotonic():
            return hit[0]
        _png_cache.pop(key, None)
    return None


def _cache_put(key: tuple, png: bytes) -> None:
    with _png_lock:
        _png_cache[key] = (png, time.monotonic() + _PNG_TTL)
        while len(_png_cache) > _PNG_MAX:
            oldest = min(_png_cache, key=lambda k: _png_cache[k][1])
            _png_cache.pop(oldest, None)


def page_png(doc_id: str, pdf_bytes: bytes, page_no: int, *, dpi: int = 100,
             box=None, pad: float = 3.0) -> bytes:
    """整页（可选画红框）→ PNG。命中缓存直接返回。"""
    key = ("page", doc_id, page_no, int(dpi),
           tuple(round(float(v), 2) for v in box) if box else None,
           round(float(pad), 2))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    png = render_page_png(pdf_bytes, page_no, dpi=dpi, box=box, pad=pad)
    _cache_put(key, png)
    return png


def figure_png(doc_id: str, pdf_bytes: bytes, page_no: int, box, *,
               dpi: int = 150) -> bytes:
    """按图区框裁图 → PNG（命中缓存直接返回）

    **裁出空白时会用另一条路径复核一次**（用户实测"少量图显示成白页、同一页的
    '看页面位置'却是对的"，见 TS-034）。两种情况在这一步长得一样：
      · 这块**真的**是空白 —— 引擎会在正文页上标出 0.2~0.4% 的小区域当 figure，
        裁出来整块纯白（本篇实测 12 处，VLM 那步已跳过它们）；
      · 这一次**渲染失败**但没抛异常 —— PDFium 状态异常时会返回一张空白栅格。
    分不清就复核：`crop_via_page_png`（整页渲染后在像素空间裁，与"看页面位置"同一
    条路）有内容就用它，仍是空白就如实返回空白。复核只在裁出空白时发生
    （12/48 的图块），代价可忽略。
    """
    key = ("crop", doc_id, page_no, int(dpi),
           tuple(round(float(v), 2) for v in box))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    png = crop_region_png(pdf_bytes, page_no, box, dpi=dpi)
    if image_stddev(png) < _BLANK_STD:
        alt = crop_via_page_png(pdf_bytes, page_no, box, dpi=dpi)
        if image_stddev(alt) >= _BLANK_STD:
            log.warning("figure_crop_blank_recovered", doc_id=doc_id,
                        page=page_no, dpi=dpi,
                        note="按框裁出空白但整页渲染有内容：已改用整页渲染裁切")
            png = alt
    _cache_put(key, png)
    return png


def invalidate(doc_id: str | None = None) -> None:
    """清缓存（重解析后块换了、或测试要可重复时用）"""
    with _png_lock:
        if doc_id is None:
            _png_cache.clear()
        else:
            for k in [k for k in _png_cache if k[1] == doc_id]:
                _png_cache.pop(k, None)
    if doc_id is None:
        with _pdf_lock:
            _pdf_cache.clear()


__all__ = ["load_pdf", "page_png", "figure_png", "invalidate", "RenderError"]
