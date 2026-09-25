"""
版面引擎适配层（rag/adapters/layout.py）

**这一层解决的是"换引擎要改业务代码"的问题。**

版面引擎（当前是 PaddleX `/layout-parsing`，见 `doc_parse.py`）的区块模型是引擎
私有的：label 怎么命名、`block_bbox` 是什么坐标系、返回结构包了几层。主应用真正
需要的只有两件事：

1. **这一页有哪些内容块**（类型 / 文本 / 顺序）→ `page_elements()`
2. **这个框在 PDF 上到底对应哪块区域**（按框裁图要用）→ `to_page_points()`

所以业务侧（`doc_parser` 的 PDF 解析、`region_enhance` 步骤）只依赖这里的接口：
换引擎 = 新增一个 `LayoutAdapter` 子类 + 在 `_ADAPTERS` 注册，**不需要动解析器和
步骤**。配置里 `doc_parse.layout_engine` 决定用哪个（默认 `paddlex`）。

⚠ 两件事**不要**跨坐标系做（历史上踩过）：
  · 图片去重比较 IoU：引擎像素框 vs pdfplumber point 框，比出来必然是"不是同一张"
    → 去重走顺序配对（`doc_parser._dedupe_image_elements`）；
  · 用 OCR 行高反推倍率：实测推出 2.93、真实 2.0，属于"看着有依据、其实全错"。
    `to_page_points` 换成用**引擎自报的栅格页尺寸 ÷ PDF 页尺寸**求倍率，并校验
    换算结果落在页内，校验不过一律返回 None（调用方跳过几何操作）。栅格页尺寸的
    唯一实测来源是响应顶层的 `result.dataInfo.pages[i]`（由 `DocParseClient`
    盖章到每页结果的 `_engine_px` 上，见 `doc_parse._stamp_engine_px`）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from rag.observability.logging import get_logger

log = get_logger("rag.adapters.layout")


class LayoutAdapter(ABC):
    """版面引擎 → 主应用元素模型的适配层"""

    name = "base"

    @abstractmethod
    def page_elements(self, page: dict, *, page_num: int) -> list:
        """引擎单页结果 → ParsedElement 列表（列表顺序即阅读顺序）"""

    @abstractmethod
    def to_page_points(self, page: dict, bbox,
                       page_w_pt: float, page_h_pt: float) -> list[float] | None:
        """引擎 bbox → PDF point 框；**不可换算时返回 None**（调用方跳过几何操作）"""


class PaddleXLayout(LayoutAdapter):
    """PaddleX `/layout-parsing`：映射与坐标换算都委托 `doc_parse` 里的实现

    刻意不在这个类里重写一遍映射逻辑：`blocks_to_elements` / `engine_bbox_to_points`
    是唯一实现（带注释说明坐标口径与校验），这里只做"接口转接"，避免两份实现漂移。
    """

    name = "paddlex"

    def page_elements(self, page: dict, *, page_num: int) -> list:
        from rag.adapters.doc_parse import blocks_to_elements
        return blocks_to_elements(page, page_num=page_num)

    def to_page_points(self, page: dict, bbox,
                       page_w_pt: float, page_h_pt: float) -> list[float] | None:
        from rag.adapters.doc_parse import engine_bbox_to_points
        return engine_bbox_to_points(page, bbox, page_w_pt, page_h_pt)


_ADAPTERS: dict[str, LayoutAdapter] = {PaddleXLayout.name: PaddleXLayout()}

# 当前生效的适配器（进程级单例）：由 container 依据配置在启动时设定。
# 不做成"到处传 config"是因为解析器在 `asyncio.to_thread` 里跑、步骤在协程里跑，
# 两边都要用同一份口径，传参会把 config 渗透到每个角落且容易传成不同的值。
_active: LayoutAdapter = _ADAPTERS[PaddleXLayout.name]


def layout_adapter() -> LayoutAdapter:
    """取当前生效的版面引擎适配器"""
    return _active


def set_layout_adapter(adapter: LayoutAdapter) -> None:
    """替换当前适配器（新增引擎时在注册表里加一项，再由配置选中）"""
    global _active
    if adapter is None:
        return
    _active = adapter
    log.info("layout_adapter_set", adapter=adapter.name)


def configure_layout_adapter(config) -> LayoutAdapter:
    """按配置（`doc_parse.layout_engine`）选定适配器并设为生效

    未知引擎名**不静默回落**：回落会让"配置里写了 my-engine 却一直在用 paddlex"
    变成看不见的事（解析结果看起来正常，只是用的不是客户以为的引擎）。这里记
    warning 后回落，保证入库不中断，但日志/监控里能看见。
    """
    dp = getattr(config, "doc_parse", None) if config is not None else None
    name = str(getattr(dp, "layout_engine", "") or "").strip().lower()
    if not name:
        return _active
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        log.warning("layout_engine_unknown", configured=name,
                    fallback=_active.name,
                    available=sorted(_ADAPTERS.keys()),
                    effect="按回落的适配器继续解析（配置未生效）")
        return _active
    set_layout_adapter(adapter)
    return adapter
