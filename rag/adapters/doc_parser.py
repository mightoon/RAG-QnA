"""
文档解析适配器（rag/adapters/doc_parser.py）

按扩展名路由：pdf / docx / xlsx / pptx / md / txt / html / 图片。
输出统一 ParsedDocument（元素列表，含类型/文本/坐标/元数据）。

PDF 三型路由（文字/扫描/混合）由 detect_scan_type 预检实现：
读前三页估算字符密度。扫描页走 OCR（PaddleOCR 可用时），
不可用时降级为低置信度空文本 + 质量告警。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from rag.models import (ContentType, ParsedDocument, ParsedElement)
from rag.observability.logging import get_logger

from .base import DocParserAdapter
from .registry import AdapterRegistry

log = get_logger("rag.parser")

# OCR 引擎懒加载（PaddleOCR 未安装时优雅降级）
_OCR_INSTANCE = None


def _get_ocr():
    global _OCR_INSTANCE
    if _OCR_INSTANCE is None:
        try:
            from paddleocr import PaddleOCR
            _OCR_INSTANCE = PaddleOCR(use_angle_cls=True, lang="ch",
                                      show_log=False)
        except Exception:
            _OCR_INSTANCE = False
    return _OCR_INSTANCE


class BaseParser(DocParserAdapter):

    def __init__(self, config=None):
        self.config = config

    @property
    def supported_extensions(self) -> set[str]:
        return set()

    async def parse(self, file_path: str, doc_id: str, filename: str,
                    tenant_id: str = "", collection: str = "default") -> ParsedDocument:
        return await asyncio.to_thread(self._parse_sync, file_path, doc_id,
                                       filename, tenant_id, collection)

    def _parse_sync(self, file_path: str, doc_id: str, filename: str,
                    tenant_id: str, collection: str) -> ParsedDocument:
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════
# PDF
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("pdf")
class PDFParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".pdf"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection):
        import pdfplumber
        from pypdf import PdfReader

        doc = ParsedDocument(doc_id=doc_id, filename=filename, file_type="pdf",
                             tenant_id=tenant_id, collection=collection)
        # ① TOC 书签
        try:
            reader = PdfReader(file_path)
            doc.page_count = len(reader.pages)
            doc.toc = self._extract_toc(reader)
        except Exception:
            reader = None

        # ② 预检扫描类型（前三页字符密度）
        scan_type = self._detect_scan_type(file_path)
        doc.scan_type = scan_type

        with pdfplumber.open(file_path) as pdf:
            doc.page_count = len(pdf.pages)
            for page_idx, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                density = len(text.strip()) / max(page.area, 1.0)
                if density < 0.005 or len(text.strip()) < 10:
                    # 低密度页：扫描页 → OCR
                    ocr_text, confidence = self._ocr_page(page)
                    if ocr_text:
                        doc.elements.append(ParsedElement(
                            content_type=ContentType.TEXT, text=ocr_text,
                            page_num=page_idx + 1,
                            metadata={"ocr": True, "ocr_confidence": confidence}))
                    else:
                        doc.elements.append(ParsedElement(
                            content_type=ContentType.TEXT, text="",
                            page_num=page_idx + 1,
                            metadata={"ocr": True, "ocr_confidence": 0.0,
                                      "blank": True}))
                    continue

                # 文字页：正文 + 表格 + 图片
                for line in text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    level = self._heading_level(line, doc.toc)
                    doc.elements.append(ParsedElement(
                        content_type=(ContentType.TITLE if level
                                      else ContentType.TEXT),
                        text=line, page_num=page_idx + 1,
                        metadata={"heading_level": level} if level else {}))

                for table in page.extract_tables():
                    if not table or len(table) < 2:
                        continue
                    doc.elements.append(ParsedElement(
                        content_type=ContentType.TABLE,
                        text=self._table_to_text(table),
                        page_num=page_idx + 1,
                        raw_data={"headers": table[0],
                                  "rows": table[1:],
                                  "row_count": len(table) - 1}))

                for img in (page.images or []):
                    try:
                        cropped = page.crop((img["x0"], img["top"],
                                             img["x1"], img["bottom"]))
                        im = cropped.to_image(resolution=150)
                        import io
                        buf = io.BytesIO()
                        im.original.save(buf, format="PNG")
                        doc.elements.append(ParsedElement(
                            content_type=ContentType.IMAGE, text="",
                            page_num=page_idx + 1,
                            bbox=[img["x0"], img["top"], img["x1"], img["bottom"]],
                            raw_data={"image_bytes": buf.getvalue(),
                                      "page_num": page_idx + 1}))
                    except Exception:
                        continue
        return doc

    @staticmethod
    def _extract_toc(reader) -> list[dict]:
        toc: list[dict] = []

        def walk(outlines, level=1):
            try:
                for item in outlines:
                    if isinstance(item, list):
                        walk(item, level + 1)
                    else:
                        page_num = None
                        try:
                            page_num = reader.get_destination_page_number(item) + 1
                        except Exception:
                            pass
                        toc.append({"level": level, "title": str(item.title),
                                    "page": page_num})
            except Exception:
                pass
        try:
            walk(reader.outline)
        except Exception:
            pass
        return toc

    @staticmethod
    def _detect_scan_type(file_path: str) -> str:
        """读前三页估算字符密度：text / scanned / hybrid"""
        import pdfplumber
        try:
            with pdfplumber.open(file_path) as pdf:
                pages = pdf.pages[:3]
                if not pages:
                    return "text"
                densities = []
                for p in pages:
                    t = p.extract_text() or ""
                    densities.append(len(t.strip()) / max(p.area, 1.0))
                avg = sum(densities) / len(densities)
                if all(d < 0.005 for d in densities):
                    return "scanned"
                if avg < 0.005:
                    return "hybrid"
                return "text"
        except Exception:
            return "text"

    def _ocr_page(self, page) -> tuple[str, float]:
        ocr = _get_ocr()
        if not ocr:
            return "", 0.0
        try:
            import io
            im = page.to_image(resolution=200)
            buf = io.BytesIO()
            im.original.save(buf, format="PNG")
            result = ocr.ocr(buf.getvalue(), cls=True)
            lines, confs = [], []
            for line_group in (result or []):
                for item in (line_group or []):
                    if item and len(item) >= 2:
                        txt, conf = item[1]
                        lines.append(txt)
                        confs.append(float(conf))
            text = "\n".join(lines)
            avg_conf = sum(confs) / len(confs) if confs else 0.0
            return text, avg_conf
        except Exception as e:
            log.warning("ocr_failed", error=str(e))
            return "", 0.0

    @staticmethod
    def _heading_level(line: str, toc: list[dict]) -> int | None:
        """标题识别：TOC 交叉验证 + 正则模式"""
        for item in toc:
            if line.strip() == item.get("title", "").strip():
                return item.get("level")
        m = re.match(r"^(第[一二三四五六七八九十百\d]+[章节篇])\s*(.*)", line)
        if m:
            return 1
        m = re.match(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+\S+", line)
        if m:
            return min(m.group(1).count(".") + 1, 4)
        return None

    @staticmethod
    def _table_to_text(table: list[list]) -> str:
        """表格 → '第N行：列A=值X' 自然语言描述"""
        if not table:
            return ""
        headers = [str(h or "").strip() for h in table[0]]
        lines = []
        for row in table[1:]:
            pairs = [f"{h}={v}" for h, v in zip(headers, row)
                     if h and v is not None and str(v).strip()]
            if pairs:
                lines.append("；".join(pairs))
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# Word
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("docx")
class DocxParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".docx", ".doc"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection):
        from docx import Document as DocxDocument
        doc = ParsedDocument(doc_id=doc_id, filename=filename, file_type="docx",
                             tenant_id=tenant_id, collection=collection)
        d = DocxDocument(file_path)
        page = 1
        for para in d.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            level = None
            if para.style and para.style.name.startswith("Heading"):
                try:
                    level = int(para.style.name.split()[-1])
                except ValueError:
                    level = 1
            doc.elements.append(ParsedElement(
                content_type=(ContentType.TITLE if level else ContentType.TEXT),
                text=text, page_num=page,
                metadata={"heading_level": level} if level else {}))
        for table in d.tables:
            rows = [[cell.text.strip() for cell in r.cells] for r in table.rows]
            if len(rows) >= 2:
                doc.elements.append(ParsedElement(
                    content_type=ContentType.TABLE,
                    text=PDFParser._table_to_text(rows), page_num=page,
                    raw_data={"headers": rows[0], "rows": rows[1:],
                              "row_count": len(rows) - 1}))
        doc.page_count = max(1, page)
        return doc


# ═══════════════════════════════════════════════════════════
# Excel / CSV
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("xlsx")
class ExcelParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".xlsx", ".xls", ".csv"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection):
        suffix = Path(file_path).suffix.lower()
        doc = ParsedDocument(doc_id=doc_id, filename=filename,
                             file_type="xlsx" if suffix != ".csv" else "csv",
                             tenant_id=tenant_id, collection=collection)
        if suffix == ".csv":
            self._parse_csv(file_path, doc)
        else:
            import openpyxl
            wb = openpyxl.load_workbook(file_path, read_only=True,
                                        data_only=True)
            for ws in wb.worksheets:
                rows = [[("" if c is None else str(c)) for c in row]
                        for row in ws.iter_rows(values_only=True)]
                self._emit_sheet(doc, ws.title, rows)
            doc.page_count = len(wb.worksheets)
        return doc

    def _parse_csv(self, file_path, doc):
        import csv
        rows = []
        with open(file_path, "r", encoding=self._detect_encoding(file_path),
                  newline="") as f:
            for row in csv.reader(f):
                rows.append([c.strip() for c in row])
        self._emit_sheet(doc, "Sheet1", rows)
        doc.page_count = 1

    @staticmethod
    def _detect_encoding(file_path: str) -> str:
        import chardet
        with open(file_path, "rb") as f:
            raw = f.read(65536)
        return chardet.detect(raw).get("encoding") or "utf-8"

    def _emit_sheet(self, doc: ParsedDocument, sheet_name: str,
                    rows: list[list[str]]):
        """每个 Sheet 独立处理；Sheet 名作一级 section 路径"""
        if not rows:
            return
        doc.elements.append(ParsedElement(
            content_type=ContentType.TITLE, text=sheet_name,
            metadata={"heading_level": 1, "sheet_name": sheet_name}))
        # 数据区（跳过空行）
        data_rows = [r for r in rows if any(str(c).strip() for c in r)]
        if len(data_rows) < 2:
            return
        headers = [str(h or "").strip() for h in data_rows[0]]
        body = data_rows[1:]
        # 统计列数值特征
        numeric_stats: dict[str, dict] = {}
        for col_idx, h in enumerate(headers):
            if not h:
                continue
            vals = []
            for r in body:
                try:
                    vals.append(float(str(r[col_idx]).replace(",", "")))
                except (ValueError, IndexError):
                    continue
            if len(vals) >= max(3, len(body) * 0.5):
                numeric_stats[h] = {"min": min(vals), "max": max(vals),
                                    "avg": round(sum(vals) / len(vals), 4)}
        # 分块发射（每 200 行一个元素，避免超大表撑爆内存）
        CHUNK_ROWS = 200
        for i in range(0, len(body), CHUNK_ROWS):
            sub = body[i:i + CHUNK_ROWS]
            text_lines = []
            for rn, row in enumerate(sub, start=i + 1):
                pairs = [f"{h}={v}" for h, v in zip(headers, row)
                         if h and str(v).strip()]
                if pairs:
                    text_lines.append(f"第{rn}行：" + "；".join(pairs))
            doc.elements.append(ParsedElement(
                content_type=ContentType.TABLE, text="\n".join(text_lines),
                metadata={"sheet_name": sheet_name,
                          "row_range": [i + 1, i + len(sub)]},
                raw_data={"headers": headers, "rows": sub,
                          "row_count": len(sub),
                          "numeric_stats": numeric_stats,
                          "table_index": i // CHUNK_ROWS}))


# ═══════════════════════════════════════════════════════════
# PPT
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("pptx")
class PPTParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".pptx", ".ppt"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection):
        from pptx import Presentation
        doc = ParsedDocument(doc_id=doc_id, filename=filename, file_type="pptx",
                             tenant_id=tenant_id, collection=collection)
        prs = Presentation(file_path)
        for idx, slide in enumerate(prs.slides, start=1):
            title = None
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    t = shape.text_frame.text.strip()
                    if not t:
                        continue
                    if shape == slide.shapes.title and title is None:
                        title = t
                    else:
                        texts.append(t)
                if shape.has_table:
                    rows = [[cell.text.strip() for cell in r.cells]
                            for r in shape.table.rows]
                    if len(rows) >= 2:
                        texts.append(PDFParser._table_to_text(rows))
            # Speaker Notes（语义最丰富）
            notes = ""
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes = slide.notes_slide.notes_text_frame.text.strip()
            if title:
                doc.elements.append(ParsedElement(
                    content_type=ContentType.TITLE, text=title, page_num=idx,
                    metadata={"heading_level": 1, "is_slide_title": True,
                              "slide_num": idx}))
            body = "\n".join(texts)
            if body:
                doc.elements.append(ParsedElement(
                    content_type=ContentType.TEXT, text=body, page_num=idx,
                    metadata={"slide_num": idx}))
            if notes:
                doc.elements.append(ParsedElement(
                    content_type=ContentType.TEXT, text=f"[备注] {notes}",
                    page_num=idx, metadata={"slide_num": idx, "is_notes": True}))
        doc.page_count = len(prs.slides.__iter__.__self__._sldIdLst) \
            if hasattr(prs.slides, "_sldIdLst") else len(list(prs.slides))
        return doc


# ═══════════════════════════════════════════════════════════
# Markdown
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("markdown")
class MarkdownParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".md", ".markdown"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection):
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        doc = ParsedDocument(doc_id=doc_id, filename=filename, file_type="md",
                             tenant_id=tenant_id, collection=collection)
        code_blocks: list[str] = []
        placeholder_idx = [0]

        def stash_code(m: re.Match) -> str:
            code_blocks.append(m.group(0))
            token = f"@@CODEBLOCK_{placeholder_idx[0]}@@"
            placeholder_idx[0] += 1
            return token

        text = re.sub(r"```.*?```", stash_code, text, flags=re.DOTALL)
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("@@CODEBLOCK_"):
                idx = int(re.search(r"\d+", stripped).group())
                doc.elements.append(ParsedElement(
                    content_type=ContentType.CODE, text=code_blocks[idx],
                    metadata={"language": self._code_lang(code_blocks[idx])}))
                continue
            m = re.match(r"^(#{1,6})\s+(.*)", stripped)
            if m:
                doc.elements.append(ParsedElement(
                    content_type=ContentType.TITLE, text=m.group(2).strip(),
                    metadata={"heading_level": len(m.group(1))}))
            else:
                doc.elements.append(ParsedElement(
                    content_type=ContentType.TEXT, text=stripped))
        doc.page_count = 1
        return doc

    @staticmethod
    def _code_lang(block: str) -> str:
        m = re.match(r"```(\w+)", block)
        return m.group(1) if m else ""


# ═══════════════════════════════════════════════════════════
# TXT
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("txt")
class TXTParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".txt", ".log"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection):
        import chardet
        with open(file_path, "rb") as f:
            raw = f.read()
        encoding = chardet.detect(raw).get("encoding") or "utf-8"
        text = raw.decode(encoding, errors="replace")
        doc = ParsedDocument(doc_id=doc_id, filename=filename, file_type="txt",
                             tenant_id=tenant_id, collection=collection)
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if para:
                doc.elements.append(ParsedElement(
                    content_type=ContentType.TEXT, text=para))
        doc.page_count = 1
        return doc


# ═══════════════════════════════════════════════════════════
# HTML
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("html")
class HTMLParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".html", ".htm"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection):
        from bs4 import BeautifulSoup
        raw = Path(file_path).read_bytes()
        soup = BeautifulSoup(raw, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        doc = ParsedDocument(doc_id=doc_id, filename=filename, file_type="html",
                             tenant_id=tenant_id, collection=collection)
        # 单次按文档顺序遍历，保证标题/段落/表格的原始版面顺序
        for tag in soup.find_all(
                ["h1", "h2", "h3", "h4", "h5", "h6",
                 "p", "li", "blockquote", "table"]):
            if tag.name.startswith("h") and len(tag.name) == 2:
                t = tag.get_text(strip=True)
                if not t:
                    continue
                doc.elements.append(ParsedElement(
                    content_type=ContentType.TITLE, text=t,
                    metadata={"heading_level": int(tag.name[1])}))
            elif tag.name == "table":
                rows = [
                    [td.get_text(strip=True)
                     for td in tr.find_all(["td", "th"])]
                    for tr in tag.find_all("tr")]
                if len(rows) >= 2:
                    doc.elements.append(ParsedElement(
                        content_type=ContentType.TABLE,
                        text=PDFParser._table_to_text(rows),
                        raw_data={"headers": rows[0], "rows": rows[1:],
                                  "row_count": len(rows) - 1}))
            else:
                # 段落：忽略表格内部节点避免与 TABLE 元素重复
                if tag.find_parent("table") is not None:
                    continue
                t = tag.get_text(" ", strip=True)
                if t:
                    doc.elements.append(ParsedElement(
                        content_type=ContentType.TEXT, text=t))
        doc.page_count = 1
        return doc


# ═══════════════════════════════════════════════════════════
# 图片（OCR + VLM 由 pipeline 的多模态步骤处理）
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("image")
class ImageParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection):
        doc = ParsedDocument(doc_id=doc_id, filename=filename, file_type="img",
                             tenant_id=tenant_id, collection=collection)
        ocr = _get_ocr()
        ocr_text, confidence = "", 0.0
        if ocr:
            try:
                result = ocr.ocr(file_path, cls=True)
                lines, confs = [], []
                for line_group in (result or []):
                    for item in (line_group or []):
                        if item and len(item) >= 2:
                            txt, conf = item[1]
                            lines.append(txt)
                            confs.append(float(conf))
                ocr_text = "\n".join(lines)
                confidence = sum(confs) / len(confs) if confs else 0.0
            except Exception as e:
                log.warning("image_ocr_failed", error=str(e))
        with open(file_path, "rb") as f:
            image_bytes = f.read()
        doc.elements.append(ParsedElement(
            content_type=ContentType.IMAGE, text=ocr_text,
            raw_data={"image_bytes": image_bytes, "page_num": 1,
                      "ocr_confidence": confidence}))
        doc.page_count = 1
        return doc
