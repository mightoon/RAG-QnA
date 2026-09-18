"""
入库步骤：分块（rag/pipeline/steps/ingest_chunk.py）

ChunkStep —— 父子分块策略：
- 父块：按标题分节聚合，≤ parent_max_tokens（语义完整单元，供上下文扩展）
- 子块：父块内按句子边界滑窗，≤ child_max_tokens（检索单元）
- 类型分派：text / table / image_caption / code
- 确定性 chunk_id：sha256(tenant:doc:seq:content_hash) → 幂等写入
- FigureLabelExtractor（V2.1）：图片关联 "图N" 标签与题注，支持溯源
"""
from __future__ import annotations

import re

from rag.models import (ChunkMeta, ChunkType, ContentType, ParsedElement)
from rag.observability.logging import get_logger
from rag.pipeline.base import PipelineStep, StepRegistry
from rag.pipeline.context import IngestContext
from rag.models import make_chunk_id

log = get_logger("rag.steps.chunk")

# "图 3-2"、"Figure 28"、"Fig. 5"
_FIGURE_PATTERN = re.compile(
    r"^(图|Figure|Fig\.?)\s*[\d\-\.]+\s*[:：\s]?\s*(.*)", re.I)


class FigureLabelExtractor:
    """V2.1 图题溯源：为 IMAGE 元素匹配最近的 '图N' 标签文本"""

    @staticmethod
    def extract(elements: list[ParsedElement]) -> None:
        for i, el in enumerate(elements):
            if el.content_type != ContentType.IMAGE:
                continue
            label, caption = "", ""
            # 向后找 2 个元素（题注通常在图下方）
            for j in range(i + 1, min(i + 3, len(elements))):
                nxt = elements[j]
                if nxt.content_type != ContentType.TEXT:
                    continue
                m = _FIGURE_PATTERN.match(nxt.text.strip())
                if m:
                    label = nxt.text.strip()[:32]
                    caption = (m.group(2) or "").strip()[:200]
                    break
            # 向前找 1 个元素（少数文档题注在图上方）
            if not label:
                for j in range(i - 1, max(i - 2, -1), -1):
                    prev = elements[j]
                    if prev.content_type != ContentType.TEXT:
                        continue
                    m = _FIGURE_PATTERN.match(prev.text.strip())
                    if m:
                        label = prev.text.strip()[:32]
                        caption = (m.group(2) or "").strip()[:200]
                        break
            if label:
                el.raw_data["figure_label"] = label
                el.raw_data["figure_caption"] = caption


_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")


def _info_density(text: str) -> float:
    """信息密度：有效信息字符（CJK/字母/数字）占比"""
    if not text:
        return 0.0
    informative = len(_CJK_RE.findall(text)) \
        + sum(1 for ch in text if ch.isascii() and ch.isalnum())
    total = sum(1 for ch in text if not ch.isspace())
    return informative / total if total else 0.0


@StepRegistry.register("chunk")
class ChunkStep(PipelineStep):

    # 严重解析问题的文档整体降权系数（quality_parse 写入 quality.document_score）
    async def execute(self, ctx: IngestContext) -> None:
        if not ctx.parsed:
            return
        cfg = ctx.services.config.pipeline
        parent_max = cfg.chunk_parent_max_tokens
        child_max = cfg.chunk_child_max_tokens
        min_tokens = cfg.min_chunk_tokens

        FigureLabelExtractor.extract(ctx.parsed.elements)

        chunks: list[ChunkMeta] = []
        texts: list[str] = []
        seq = 0
        table_idx = 0                      # ctx.tables 回填游标
        doc_multiplier = float(
            getattr(ctx.quality, "document_score", 1.0) or 1.0)
        doc_multiplier = max(0.1, min(1.0, doc_multiplier))
        seen_fps: set[str] = set()

        def _quality(text: str) -> float:
            density = _info_density(text)
            q = (0.4 + 0.6 * density) * doc_multiplier
            return round(max(0.05, min(1.0, q)), 3)

        def emit(text: str, chunk_type: str, el: ParsedElement,
                 is_parent: bool = False, parent_id: str | None = None) -> str:
            nonlocal seq
            text = text.strip()
            if not text:
                return ""
            # 同文档近似重复（归一化指纹）→ 跳过（页眉页脚等机械重复）
            fp = text.lower().replace(" ", "")[:200]
            if not is_parent and fp in seen_fps:
                return ""
            seen_fps.add(fp)
            cid = make_chunk_id(ctx.doc.tenant_id, ctx.doc.doc_id, seq, text)
            seq += 1
            chunks.append(ChunkMeta(
                chunk_id=cid, doc_id=ctx.doc.doc_id,
                tenant_id=ctx.doc.tenant_id, collection=ctx.doc.collection,
                chunk_type=chunk_type, is_parent=is_parent,
                parent_chunk_id=parent_id, page_num=el.page_num,
                section_path=el.metadata.get("section_path") or None,
                quality_score=_quality(text),
                figure_label=el.raw_data.get("figure_label") or None,
                figure_caption=el.raw_data.get("figure_caption") or None,
                token_count=ctx.services.llm.count_tokens(text),
                allowed_roles=list(ctx.doc.allowed_roles)))
            texts.append(text)
            return cid

        # ── 按标题分节 → 父块聚合 → 子块切分 ─────────────────
        section_buf: list[ParsedElement] = []
        section_path = ""

        def flush_section():
            nonlocal section_buf, seq
            if not section_buf:
                return
            # 父块文本：节内 TEXT 元素顺序拼接
            parent_text = "\n".join(
                e.text for e in section_buf
                if e.content_type in (ContentType.TEXT, ContentType.TITLE))
            if not parent_text.strip():
                section_buf = []
                return
            # 父块超限 → 按元素边界拆多个父块
            parent_parts = self._split_parent(
                parent_text, parent_max, ctx.services.llm.count_tokens)
            for part in parent_parts:
                if ctx.services.llm.count_tokens(part) < min_tokens \
                        and len(parent_parts) == 1:
                    continue                      # 过短节并入（此处独立节直接跳过）
                pid = emit(part, ChunkType.PARENT.value, section_buf[0],
                           is_parent=True)
                # 子块滑窗
                for child in self._split_child(
                        part, child_max, ctx.services.llm.count_tokens):
                    if ctx.services.llm.count_tokens(child) >= min_tokens:
                        emit(child, ChunkType.TEXT.value, section_buf[0],
                             parent_id=pid)
            section_buf = []

        for el in ctx.parsed.elements:
            path = el.metadata.get("section_path") or ""
            if path != section_path and section_buf:
                flush_section()
                section_path = path
            if el.content_type == ContentType.TABLE:
                # 表格：整表一块（不切分）
                emit(el.text, ChunkType.TABLE.value, el)
                # 回填 TableData.chunk_id
                if table_idx < len(ctx.tables):
                    ctx.tables[table_idx].chunk_id = chunks[-1].chunk_id \
                        if chunks else ""
                    table_idx += 1
            elif el.content_type == ContentType.IMAGE:
                # 图片题注块：figure_label + caption + OCR/VLM 描述
                caption_parts = []
                if el.raw_data.get("figure_label"):
                    caption_parts.append(el.raw_data["figure_label"])
                if el.raw_data.get("figure_caption"):
                    caption_parts.append(el.raw_data["figure_caption"])
                if el.raw_data.get("vlm_caption"):
                    caption_parts.append(f"图片描述：{el.raw_data['vlm_caption']}")
                if el.text:
                    caption_parts.append(f"图片文字：{el.text}")
                if caption_parts:
                    emit("。".join(caption_parts),
                         ChunkType.IMAGE.value, el)
            elif el.content_type == ContentType.CODE:
                emit(el.text, ChunkType.CODE.value, el)
            elif el.content_type in (ContentType.TEXT, ContentType.TITLE):
                if el.metadata.get("is_notes"):    # PPT 备注并入正文
                    section_buf.append(el)
                elif el.content_type == ContentType.TEXT:
                    section_buf.append(el)
                else:
                    section_buf.append(el)          # 标题保留在父块文本中
        flush_section()

        ctx.chunks = chunks
        ctx.chunk_texts = texts
        ctx.task.total_chunks = len(chunks)
        await ctx.report("chunking", 0.35, f"分块完成：{len(chunks)} 块")

    # ── 切分算法 ─────────────────────────────────────────

    @staticmethod
    def _split_parent(text: str, max_tokens: int,
                      count_fn) -> list[str]:
        """父块拆分：按段落边界聚合，超限在段落处切开"""
        if count_fn(text) <= max_tokens:
            return [text]
        parts, buf, buf_tokens = [], [], 0
        for para in text.split("\n"):
            t = count_fn(para)
            if buf_tokens + t > max_tokens and buf:
                parts.append("\n".join(buf))
                buf, buf_tokens = [], 0
            buf.append(para)
            buf_tokens += t
        if buf:
            parts.append("\n".join(buf))
        return parts

    @staticmethod
    def _split_child(text: str, max_tokens: int,
                     count_fn) -> list[str]:
        """子块滑窗：句子边界切分，相邻块重叠一句"""
        if count_fn(text) <= max_tokens:
            return [text]
        # 分句（中英文标点）
        sentences = re.split(r"(?<=[。！？；!?;])\s*|\n", text)
        sentences = [s for s in sentences if s.strip()]
        children, buf, buf_tokens = [], [], 0
        for sent in sentences:
            t = count_fn(sent)
            if t > max_tokens:
                # 超长单句硬切
                if buf:
                    children.append("".join(buf))
                    buf, buf_tokens = [], 0
                for i in range(0, len(sent), max_tokens * 3):
                    children.append(sent[i:i + max_tokens * 3])
                continue
            if buf_tokens + t > max_tokens and buf:
                children.append("".join(buf))
                # 重叠：保留最后一句
                buf, buf_tokens = [buf[-1]], count_fn(buf[-1])
            buf.append(sent)
            buf_tokens += t
        if buf:
            children.append("".join(buf))
        return children
