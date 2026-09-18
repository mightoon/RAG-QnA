"""
入库步骤：解析与结构化（rag/pipeline/steps/ingest_parse.py）

- ParseStep        文档解析（按扩展名路由，PDF 内含 OCR）
- QualityParseStep 扫描件 OCR 质量检查（置信度/空页率/乱码率）
- OutlineStep      OutlineBuilder：标题层级栈 → section_path
- VLMCaptionStep   图片 VLM 描述生成（多模态，可降级）
- TableExtractStep 表格原始数据收集（供 MySQL table_data 精确查询）
"""
from __future__ import annotations

import base64
import re

from rag.models import (ContentType, ParsedElement, QualityIssue, TableData)
from rag.observability.logging import get_logger
from rag.pipeline.base import PipelineStep, StepRegistry
from rag.pipeline.context import IngestContext

log = get_logger("rag.steps.parse")


@StepRegistry.register("detect_format")
class DetectFormatStep(PipelineStep):
    """magic bytes 校验：防止伪装文件格式（架构 5.3 首步，失败拒绝入库）"""

    _MAGIC: dict[str, list[bytes]] = {
        "pdf": [b"%PDF"],
        "docx": [b"PK\x03\x04"], "xlsx": [b"PK\x03\x04"],
        "pptx": [b"PK\x03\x04"],
        "png": [b"\x89PNG\r\n\x1a\n"],
        "jpg": [b"\xff\xd8\xff"], "jpeg": [b"\xff\xd8\xff"],
        "txt": [], "csv": [], "html": [], "md": [],
        "doc": [],                             # 旧格式走解析器自身校验
    }

    async def execute(self, ctx: IngestContext) -> None:
        import os
        ext = os.path.splitext(ctx.doc.filename)[1].lower().lstrip(".")
        magics = self._MAGIC.get(ext, [])
        if not magics:
            return                             # 无 magic 定义，放行
        try:
            with open(ctx.file_path, "rb") as f:
                head = f.read(16)
        except OSError as e:
            from rag.pipeline.base import StepError
            raise StepError("detect_format", f"无法读取文件: {e}",
                            retryable=False) from e
        if not any(head.startswith(m) for m in magics):
            from rag.pipeline.base import StepError
            log.warning("format_mismatch", filename=ctx.doc.filename,
                        ext=ext, head=head[:8].hex())
            ctx.add_warning(f"文件内容与扩展名 .{ext} 不符，拒绝入库")
            raise StepError("detect_format",
                            f"文件 magic bytes 与 .{ext} 不符（疑似伪装文件）",
                            retryable=False)


@StepRegistry.register("parse")
class ParseStep(PipelineStep):
    """文档解析：file_path → ParsedDocument"""

    async def execute(self, ctx: IngestContext) -> None:
        s = ctx.services
        await ctx.report("parsing", 0.05, "开始解析")
        parser = s.get_parser(ctx.doc.filename)
        ctx.parsed = await parser.parse(
            ctx.file_path, ctx.doc.doc_id, ctx.doc.filename,
            ctx.doc.tenant_id, ctx.doc.collection)
        ctx.doc.page_count = ctx.parsed.page_count
        ctx.doc.language = ctx.parsed.language
        ctx.task.total_pages = ctx.parsed.page_count
        ctx.meta["scan_type"] = ctx.parsed.scan_type
        # 质量报告初始化
        ctx.quality.doc_id = ctx.doc.doc_id
        await ctx.report("parsing", 0.15,
                         f"解析完成：{len(ctx.parsed.elements)} 个元素")


@StepRegistry.register("quality_parse")
class QualityParseStep(PipelineStep):
    """解析质量检查（扫描/混合型 PDF）：OCR 置信度、空页率、乱码率"""

    async def execute(self, ctx: IngestContext) -> None:
        if not ctx.parsed:
            return
        ocr_confs, blank, total = [], 0, 0
        for el in ctx.parsed.elements:
            if el.metadata.get("ocr"):
                total += 1
                conf = el.metadata.get("ocr_confidence", 0.0)
                ocr_confs.append(conf)
                if el.metadata.get("blank") or conf < 0.5:
                    blank += 1
                    ctx.quality.issues.append(QualityIssue(
                        stage="parse", severity="medium" if conf >= 0.3 else "high",
                        code="ocr_low_conf",
                        message=f"OCR 置信度低 ({conf:.2f})",
                        page_num=el.page_num, action="warn"))
        if total:
            avg = sum(ocr_confs) / len(ocr_confs)
            ctx.quality.ocr_avg_confidence = round(avg, 3)
            ctx.quality.blank_page_ratio = round(blank / total, 3)
            if avg < 0.6:
                ctx.add_warning(f"OCR 平均置信度偏低: {avg:.2f}")
        # 乱码检测：非常用字符占比
        all_text = "".join(e.text for e in ctx.parsed.elements
                           if e.content_type == ContentType.TEXT)
        if all_text:
            garbled = sum(1 for c in all_text if ord(c) > 0xFFFF or
                          (0xE000 <= ord(c) <= 0xF8FF))  # 私有区
            ctx.quality.garbled_ratio = round(garbled / len(all_text), 4)
        # 文档级质量系数（chunk 质量分整体乘数）
        score = 1.0
        if (ctx.quality.ocr_avg_confidence or 1.0) < 0.5:
            score *= 0.7
        if (ctx.quality.blank_page_ratio or 0.0) > 0.3:
            score *= 0.8
        if (ctx.quality.garbled_ratio or 0.0) > 0.05:
            score *= 0.7
        ctx.quality.document_score = round(max(0.3, score), 3)
        if ctx.quality.document_score < 1.0:
            ctx.add_warning(
                f"文档解析质量系数 {ctx.quality.document_score}，"
                "chunk 分将整体降权")


@StepRegistry.register("outline")
class OutlineStep(PipelineStep):
    """
    OutlineBuilder：维护标题层级栈，
    为每个元素生成 section_path（"第3章/3.1节/3.1.2"）。
    """
    MAX_DEPTH = 4

    async def execute(self, ctx: IngestContext) -> None:
        if not ctx.parsed:
            return
        # 1) TOC 交叉验证先行：校正孤立标题层级，再计算栈（顺序修复 D21）
        if ctx.parsed.toc:
            self._calibrate_with_toc(ctx.parsed)
            toc_titles = {i.get("title", "").strip() for i in ctx.parsed.toc
                          if i.get("title")}
            extracted = {el.text.strip() for el in ctx.parsed.elements
                         if el.content_type == ContentType.TITLE}
            # 交叉验证告警：提取标题与书签覆盖率过低
            if len(toc_titles) >= 5:
                hit = len(toc_titles & extracted)
                if hit / len(toc_titles) < 0.3:
                    ctx.add_warning(
                        f"标题与 PDF 书签匹配率低 "
                        f"({hit}/{len(toc_titles)})，大纲可能不完整")
        # 2) 标题层级栈 → section_path
        stack: list[tuple[int, str]] = []       # [(level, title)]
        for el in ctx.parsed.elements:
            level = None
            if el.content_type == ContentType.TITLE:
                level = el.metadata.get("heading_level") or 1
                level = min(int(level), self.MAX_DEPTH)
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, el.text.strip()[:64]))
            el.metadata["section_path"] = "/".join(
                t for _, t in stack)[:512] if stack else ""
            el.metadata["heading_level"] = level
        await ctx.report("chunking", 0.2, "大纲构建完成")

    @staticmethod
    def _calibrate_with_toc(parsed) -> None:
        toc_titles = {item.get("title", "").strip()
                      for item in parsed.toc if item.get("title")}
        for el in parsed.elements:
            if (el.content_type == ContentType.TITLE and
                    el.text.strip() in toc_titles and
                    not el.metadata.get("heading_level")):
                el.metadata["heading_level"] = 1


@StepRegistry.register("reorder_columns")
class ReorderColumnsStep(PipelineStep):
    """双栏版面重排（D21）：依据元素 bbox 检测两栏布局，
    同页元素按（栏号, y0）重新排序；无 bbox 的解析结果自动跳过。"""

    async def execute(self, ctx: IngestContext) -> None:
        if not ctx.parsed:
            return
        by_page: dict[int, list[ParsedElement]] = {}
        for el in ctx.parsed.elements:
            by_page.setdefault(el.page_num or 0, []).append(el)
        reordered: list[ParsedElement] = []
        for page in sorted(by_page):
            els = by_page[page]
            boxes = [el.metadata.get("bbox") for el in els]
            if len(els) < 8 or any(b is None or len(b) < 4 for b in boxes):
                reordered.extend(els)
                continue
            xs = sorted(float(b[0]) for b in boxes if b)
            # 判定双栏：x0 在页面中部聚成两簇
            mid_x = [x for x in xs if x < 0.4 * max(xs + [1])]
            left = [x for x in xs if x <= 100]
            right = [x for x in xs if 250 <= x <= 350]
            if len(left) < 4 or len(right) < 4:
                reordered.extend(els)
                continue
            def _key(el: ParsedElement):
                b = el.metadata.get("bbox") or [0, 0, 0, 0]
                col = 0 if float(b[0]) < 200 else 1
                return (col, float(b[1]))
            els = sorted(els, key=_key)
            reordered.extend(els)
            ctx.quality.issues.append(QualityIssue(
                stage="parse", severity="low", code="two_column_reorder",
                message=f"第 {page} 页按双栏版面重排", page_num=page,
                action="auto"))
        ctx.parsed.elements = reordered


@StepRegistry.register("vlm_caption")
class VLMCaptionStep(PipelineStep):
    """
    图片 VLM 描述生成：IMAGE 元素 → 自然语言描述（写入 raw_data["vlm_caption"]）。
    LLM 不支持多模态时降级为 OCR 文本。受 pipeline.enable_vlm 开关控制。
    """

    async def execute(self, ctx: IngestContext) -> None:
        if not ctx.parsed or not ctx.services.config.pipeline.enable_vlm:
            return
        images = [e for e in ctx.parsed.elements
                  if e.content_type == ContentType.IMAGE and
                  e.raw_data.get("image_bytes")]
        if not images:
            return
        sem = _Semaphore(ctx.services.config.llm.max_concurrency)

        async def describe(el: ParsedElement) -> None:
            b64 = base64.b64encode(el.raw_data["image_bytes"]).decode()
            try:
                async with sem:
                    caption = await ctx.services.llm.generate([{
                        "role": "user",
                        "content": [
                            {"type": "text",
                             "text": "用一句话描述这张图片的内容，"
                                     "如果是图表请说明图表类型和数据要点。"},
                            {"type": "image_url", "image_url":
                                {"url": f"data:image/png;base64,{b64}"}},
                        ]}], task="summary", max_tokens=150)
                el.raw_data["vlm_caption"] = caption.strip()[:500]
            except Exception as e:
                log.debug("vlm_caption_failed", error=str(e))
                # 降级：仅用 OCR 文本

        import asyncio
        await asyncio.gather(*(describe(el) for el in images),
                             return_exceptions=True)


@StepRegistry.register("table_extract")
class TableExtractStep(PipelineStep):
    """
    表格原始数据收集：TABLE 元素 raw_data → TableData 列表。
    chunk_id 由 ChunkStep 生成后回填（元素顺序一致）。
    """

    async def execute(self, ctx: IngestContext) -> None:
        if not ctx.parsed:
            return
        for el in ctx.parsed.elements:
            if el.content_type != ContentType.TABLE:
                continue
            rd = el.raw_data or {}
            ctx.tables.append(TableData(
                chunk_id="",                       # ChunkStep 回填
                doc_id=ctx.doc.doc_id,
                tenant_id=ctx.doc.tenant_id,
                table_index=rd.get("table_index", 0),
                page_num=el.page_num,
                headers=[str(h) for h in (rd.get("headers") or [])],
                row_data=[[str(c) for c in row]
                          for row in (rd.get("rows") or [])],
                row_count=rd.get("row_count",
                                 len(rd.get("rows") or [])),
                numeric_stats=rd.get("numeric_stats") or {}))


# ── 工具 ───────────────────────────────────────────────

class _Semaphore:
    """asyncio.Semaphore 惰性包装（避免事件循环未运行时构造报错）"""

    def __init__(self, n: int):
        self._n = max(1, n)
        self._sem = None

    async def __aenter__(self):
        import asyncio
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._n)
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()
        return False
