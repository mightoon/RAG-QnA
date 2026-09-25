"""
结构化日志（rag/observability/logging.py）

structlog 输出 JSON，每条日志携带 session_id / tenant_id / step_name 等上下文字段，
便于在 ELK / Splunk 中按维度过滤。
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


class _NoiseCollector(logging.Filter):
    """把第三方库的**已知良性警告**收集起来，而不是逐条刷屏

    现状（实测：一份 96 页的 PDF）：`pdfplumber` 底层的 pdfminer 会对"字体描述符里
    缺 FontBBox / 资源表不规范"的页面逐次告警 —— 单篇文档 **7688 条**（其中
    FontBBox 766 条），把真正需要看的日志全部淹掉。

    这些告警对我们**没有可操作性**：正文来自版面引擎（PaddleX），pdfminer 只用来
    判断有无文本层、抽嵌入图/表格；缺 bbox 时它退化为零矩形，不影响取字。
    真正的问题（解析失败、乱码）由我们自己的质量检查（乱码率/空页率/置信度）负责。

    所以：WARNING 一律丢弃并计数，ERROR 及以上照常放行；计数由
    `flush_parse_noise()` 在每个文档解析结束后汇总成**一条**日志。
    """

    def __init__(self) -> None:
        super().__init__()
        self.counts: dict[str, int] = {}
        self.last: str = ""

    # 只处理这几族日志（按**记录名**判定，不依赖挂在哪一层）
    PREFIXES = ("pdfminer", "pdfplumber")

    def filter(self, record: logging.LogRecord) -> bool:      # noqa: A003
        # ⚠ 必须挂在 **handler** 上：Python 的日志传播只对 handler 应用过滤，
        # 挂在 logger（哪怕是父 logger）上，子 logger 发出来的记录也不会经过它。
        if not record.name.startswith(self.PREFIXES):
            return True                                       # 其它日志一律放行
        if record.levelno >= logging.ERROR:
            return True                                       # 真错误照常打印
        key = f"{record.name}"
        self.counts[key] = self.counts.get(key, 0) + 1
        try:
            self.last = record.getMessage()[:160]
        except Exception:
            pass
        return False

    def drain(self) -> tuple[dict[str, int], str]:
        out, last = dict(self.counts), self.last
        self.counts.clear()
        self.last = ""
        return out, last


_parse_noise = _NoiseCollector()


def _install_noise_filters() -> None:
    """把聚合过滤器挂到当前所有 root handler 上（`configure_logging` 调用）

    挂 handler 而不是 logger 的原因见 `_NoiseCollector.filter` 的注释 —— 这一条
    踩过：挂在 `pdfminer` logger 上完全不生效，日志照旧刷屏。
    另外把该族的级别显式设成 WARNING：DEBUG/INFO 本来也不打印，显式化避免
    "哪天有人在别处开了 DEBUG" 又把噪声放出来。
    """
    root = logging.getLogger()
    for h in root.handlers:
        if _parse_noise not in h.filters:
            h.addFilter(_parse_noise)
    for name in _NoiseCollector.PREFIXES:
        lg = logging.getLogger(name)
        if _parse_noise not in lg.filters:
            lg.addFilter(_parse_noise)
        if lg.level == logging.NOTSET:
            lg.setLevel(logging.WARNING)


def flush_parse_noise(context: str = "") -> dict[str, int]:
    """把攒下的第三方解析噪声汇总成一条日志（每个文档解析完调用一次）

    返回**本次汇总的计数**（`{logger 名: 条数}`，没有噪声时为空 dict）——
    便于调用方/自测直接断言，而不必去解析日志文本。
    """
    counts, last = _parse_noise.drain()
    total = sum(counts.values())
    if not total:
        return {}
    structlog.get_logger("rag.parse").info(
        "pdf_parse_warnings_suppressed", total=total, by_logger=counts,
        sample=last, context=context,
        hint="pdfminer 对字体/资源不规范的良性告警（正文取自版面引擎，"
             "缺 FontBBox 只影响它自己的字形框），已在进程内聚合，不再逐条打印")
    return counts


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """进程级日志初始化（应用启动时调用；配置热更新后会再调一次）

    ⚠ 这个函数**必须有人调用**，否则 structlog 跑在**默认配置**上：默认配置不做
    级别过滤（INFO/DEBUG 全打），于是 `observability.log_level` / `log_json`
    两个配置项变成摆设。本项目就曾经整个生命周期都没调用过它 ——
    后果实测：应用上下文里 `log.debug(...)` 照样输出、控制台也不是 JSON
    （见 `doc/data_path.md` §8.28）。现在的调用点：`rag/api/app.py:create_app`
    与 `rag/api/runtime.py:rebuild_container`（配置页改级别后立即生效）。

    可重复调用：级别/格式按最后一次生效；噪声过滤器只在没挂过时挂。
    """
    lv = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=lv)
    # basicConfig 在 root 已有 handler 时**直接返回**（连 level 都不设），
    # 而 uvicorn/测试框架常常已经装过 handler —— 所以这里显式再设一次级别，
    # 否则"配置页把级别从 DEBUG 调回 INFO"不会生效。
    logging.getLogger().setLevel(lv)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        # 时间戳用**本机时区**、与 structlog 默认格式一致（`2026-09-25 20:00:47`）：
        # fmt="iso" 打的是 UTC（`2026-09-25T12:00:47Z`），接上日志配置后控制台时间
        # 会突然比用户习惯的少 8 小时 —— 用户是照着控制台时间对日志的，别改这个口径。
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer = (structlog.processors.JSONRenderer(ensure_ascii=False)
                if json_output else structlog.dev.ConsoleRenderer())
    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(lv),
        # ⚠ 必须是 False：True 会把"第一次 get_logger 时的配置"钉死在
        # BoundLoggerLazyProxy 里，配置热更新（改级别/改格式）就对老 logger 失效
        cache_logger_on_first_use=False,
    )
    # 第三方解析库的良性告警聚合（见 _NoiseCollector）
    _install_noise_filters()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
