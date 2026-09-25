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

# 句边界：句末标点后的空白，或换行。**必须用捕获组把分隔符取出来**——丢掉它就是
# 英文单词粘连的根因（见 `_sentences_keep_seps`）。
_SENT_SPLIT_RE = re.compile(r"((?<=[。！？；!?;])\s+|\n+)")
# 中日韩字符/标点：两侧都是它时拼回不加空格（中文之间加空格反而破坏检索词）
_CJK_EDGE_RE = re.compile(r"[一-鿿㐀-䶿、。！？；：（）《》“”‘’]")


def _sentences_keep_seps(text: str) -> list[tuple[str, int, str]]:
    """切句 → [(句子, 起始偏移, 紧跟其后的分隔符)]

    分隔符**随前一句返回**，拼回时用它还原（规范化为一个空格）。原实现用
    `re.split(r"(?<=[。！？；!?;])\s*|\n", text)`：分隔符被正则吃掉、再 `"".join`
    拼回，于是换行与句后空格全部消失 —— 实测 20 个 text 块**无一例外**丢了换行
    （"a\\nlong-standing" → "along-standing"、"just a" → "justa"、
    "prompt. We" → "prompt.We"）。
    """
    out: list[tuple[str, int, str]] = []
    pos = 0
    for m in _SENT_SPLIT_RE.finditer(text):
        seg = text[pos:m.start()]
        if seg.strip():
            out.append((seg, pos, m.group(1)))
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        out.append((tail, pos, ""))
    return out


def _join_sents(parts: list[tuple[str, int, str]]) -> str:
    """把若干句拼回一段（用各自携带的分隔符；两侧都是中日韩字符时不补空格）

    注意分隔符挂在**前一句**上（`sep` 是"这句之后"的分隔符），所以要取
    `parts[i-1][2]` 而不是当前句的 —— 取错了就会出现"最后一句与前面粘在一起"。
    """
    out = ""
    for i, (seg, _start, _sep) in enumerate(parts):
        if i:
            prev_sep = parts[i - 1][2]
            if prev_sep and not (_CJK_EDGE_RE.search(out[-1:])
                                 and _CJK_EDGE_RE.search(seg[:1])):
                out += " "
        out += seg
    return out


def _split_keep_offsets(text: str, sep: str) -> list[tuple[str, int]]:
    """按 sep 切分并保留每段起始偏移（`str.split` 只给文本，偏移得自己走一遍）"""
    out: list[tuple[str, int]] = []
    pos = 0
    for m in re.finditer(re.escape(sep), text):
        out.append((text[pos:m.start()], pos))
        pos = m.end()
    out.append((text[pos:], pos))
    return out


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
            # 同文档近似重复（归一化指纹）→ 跳过（页眉页脚等机械重复）。
            # **表格不参与**：表格块是"结构化数据的载体"，两张不同表的前 200 字符
            # 完全可能一样（实测本文档 p5 与 p13 的两张 Method 对比表就是同一指纹），
            # 跳过其中一张 = 那张表既没正文块、也没有 chunk_id，结构行成了孤儿。
            fp = text.lower().replace(" ", "")[:200]
            if not is_parent and chunk_type != ChunkType.TABLE.value \
                    and fp in seen_fps:
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

        # ── 表格块 ↔ table_data 的配对：**按身份，不按序号** ───────────
        # 原实现用"第几个 TABLE 元素"配 `ctx.tables[table_idx]`，并在回填时取
        # `chunks[-1].chunk_id` —— 只要有一个表格元素没产出块（内容为空、或命中
        # 上面的重复指纹），后面所有表就整体错位一格，且**共用**上一张表的 chunk_id
        # （实测 p13 的 table_index=7 拿到了 table_index=6 的 chunk_id，8 行
        # table_data 只有 7 个不同 chunk_id）。改为：TableExtractStep 在元素上写下
        # 它在 `ctx.tables` 里的下标，这里按该下标精确回填；没产出块就留空字符串，
        # 绝不顶替别人的 id。
        def link_table(el: ParsedElement, cid: str) -> None:
            ti = (el.raw_data or {}).get("table_data_index")
            if isinstance(ti, int) and 0 <= ti < len(ctx.tables):
                ctx.tables[ti].chunk_id = cid or ""

        # ── 按标题分节 → 父块聚合 → 子块切分 ─────────────────
        section_buf: list[ParsedElement] = []
        section_path = ""

        def _el_at(ranges: list, offset: int,
                   fallback: ParsedElement) -> ParsedElement:
            """拼接文本里的偏移 → 它**真正所属的那个元素**

            用于给块取页码：原实现一律把 `section_buf[0]` 传进 emit，于是整节的父块
            与子块都记成"该节第一个元素所在的页"（实测 13 个可校验的 text 块里 8 个
            页码偏小 1–3 页，跨页小节的块全都压到首页）。

            偏移落在元素之间的 "\n" 分隔符上时归给**前面**那个元素（块的起始位置就在
            前一个元素的末尾）。
            """
            for start, end, el in ranges:
                if start <= offset < end:
                    return el
            best = fallback
            for start, end, el in ranges:
                if offset >= end:
                    best = el
                else:
                    break
            return best

        def flush_section():
            nonlocal section_buf, seq
            if not section_buf:
                return
            # 父块文本：节内 TEXT/TITLE 元素顺序拼接（"\n" 分隔，与历史一致）
            parts_in: list[ParsedElement] = [
                e for e in section_buf
                if e.content_type in (ContentType.TEXT, ContentType.TITLE)]
            parent_text = "\n".join(e.text for e in parts_in)
            if not parent_text.strip():
                section_buf = []
                return
            # 元素文本在 parent_text 中的偏移区间（与上面的 "\n".join 严格对应）
            ranges: list[tuple[int, int, ParsedElement]] = []
            pos = 0
            for e in parts_in:
                ranges.append((pos, pos + len(e.text), e))
                pos += len(e.text) + 1
            # 父块超限 → 按元素边界拆多个父块
            parent_parts = self._split_parent(
                parent_text, parent_max, ctx.services.llm.count_tokens)
            for part, p_start in parent_parts:
                if ctx.services.llm.count_tokens(part) < min_tokens \
                        and len(parent_parts) == 1:
                    continue                      # 过短节并入（此处独立节直接跳过）
                el0 = _el_at(ranges, p_start, section_buf[0])
                pid = emit(part, ChunkType.PARENT.value, el0,
                           is_parent=True)
                # 子块滑窗
                for child, c_start in self._split_child(
                        part, child_max, ctx.services.llm.count_tokens):
                    if ctx.services.llm.count_tokens(child) >= min_tokens:
                        # 子块自己的起始偏移 = 父块内偏移 + 父块在节内的偏移
                        cel = _el_at(ranges, p_start + c_start, el0)
                        emit(child, ChunkType.TEXT.value, cel,
                             parent_id=pid)
            section_buf = []

        for el in ctx.parsed.elements:
            path = el.metadata.get("section_path") or ""
            if path != section_path and section_buf:
                flush_section()
                section_path = path
            if el.content_type == ContentType.TABLE:
                # 表格：整表一块（不切分）
                link_table(el, emit(el.text, ChunkType.TABLE.value, el))
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
                      count_fn) -> list[tuple[str, int]]:
        """父块拆分：按段落边界聚合，超限在段落处切开

        返回 `[(文本, 该文本在入参 text 中的起始偏移)]`。偏移供调用方把父块/子块
        映射回它**真正所属的元素**（取页码用，见 `ChunkStep.execute._el_at`）。
        段落边界用 "\\n" 原样还原，所以每个 part 都是 text 的一段原样子串、偏移可加。
        """
        if count_fn(text) <= max_tokens:
            return [(text, 0)]
        parts: list[tuple[str, int]] = []
        buf: list[tuple[str, int]] = []
        buf_tokens = 0
        for para, start in _split_keep_offsets(text, "\n"):
            t = count_fn(para)
            if buf_tokens + t > max_tokens and buf:
                parts.append(("\n".join(p for p, _ in buf), buf[0][1]))
                buf, buf_tokens = [], 0
            buf.append((para, start))
            buf_tokens += t
        if buf:
            parts.append(("\n".join(p for p, _ in buf), buf[0][1]))
        return parts

    @staticmethod
    def _split_child(text: str, max_tokens: int,
                     count_fn) -> list[tuple[str, int]]:
        """子块滑窗：句子边界切分，相邻块重叠一句；返回 [(文本, 起始偏移)]

        拼回时**逐句还原分隔符**（见 `_join_sents`）：换行/句后空格不再丢失，
        英文不再粘连。
        """
        if count_fn(text) <= max_tokens:
            return [(text, 0)]
        children: list[tuple[str, int]] = []
        buf: list[tuple[str, int, str]] = []
        buf_tokens = 0
        for seg, start, sep in _sentences_keep_seps(text):
            t = count_fn(seg)
            if t > max_tokens:
                # 超长单句硬切
                if buf:
                    children.append((_join_sents(buf), buf[0][1]))
                    buf, buf_tokens = [], 0
                step = max_tokens * 3
                for i in range(0, len(seg), step):
                    children.append((seg[i:i + step], start + i))
                continue
            if buf_tokens + t > max_tokens and buf:
                children.append((_join_sents(buf), buf[0][1]))
                # 重叠：保留最后一句
                buf = [buf[-1]]
                buf_tokens = count_fn(buf[-1][0])
            buf.append((seg, start, sep))
            buf_tokens += t
        if buf:
            children.append((_join_sents(buf), buf[0][1]))
        return children
