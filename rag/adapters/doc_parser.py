"""
文档解析适配器（rag/adapters/doc_parser.py）

按扩展名路由：pdf / docx / xlsx / pptx / md / txt / html / 图片。
输出统一 ParsedDocument（元素列表，含类型/文本/坐标/元数据）。

PDF 三型路由（文字/扫描/混合）由 detect_scan_type 预检实现：
按"**页面有没有文本层**"判定（不是字符密度 —— 密度是尺度相关量，正常中文页的
密度只有旧阈值的 1/33，会把文字页误判成扫描页，见 _detect_scan_type 注释）。
扫描页走 OCR（优先 doc_parse 服务，其次进程内 PaddleOCR），不可用时降级为空文本
+ 质量告警。

引擎（doc_parse /layout-parsing）的区块与 pdfplumber 的结果在这里合流：
文本/标题/表格按引擎的阅读顺序与类型产出，图片先做顺序配对去重，再给"只有框、
没有图字节"的区域按框裁图（见 _fill_layout_region_bytes）。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from rag.models import (ContentType, ParsedDocument, ParsedElement)
from rag.observability.logging import flush_parse_noise, get_logger

from .base import DocParserAdapter
from .registry import AdapterRegistry

log = get_logger("rag.parser")

# 一页"文本层薄到不可用"的下限：低于它就当作扫描页交给 OCR。
# 刻意用**绝对字符数**而不是字符密度 —— 密度是尺度相关量，见 PDFParser._parse_sync 的注释。
_MIN_TEXT_CHARS = 20


def _convert_legacy_doc(file_path: str) -> str:
    """旧版 .doc（OLE 复合文档）→ .docx，返回新路径

    python-docx 只能读 OOXML，直接喂 .doc 会抛一句难以定位的 PackageNotFoundError。
    这里用本机 Word 做一次静默转换（Windows 常见部署形态）；转换不可用时给出**可处置**
    的报错，而不是让异常从库内部冒上来（与"失败必须发声"一致）。
    """
    src = Path(file_path)
    if src.suffix.lower() != ".doc":
        return file_path
    out = src.with_suffix(".docx")
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore
        pythoncom.CoInitialize()
        try:
            app = win32com.client.Dispatch("Word.Application")
            app.Visible = False
            try:
                doc = app.Documents.Open(str(src.resolve()))
                doc.SaveAs2(str(out.resolve()), FileFormat=16)   # 16 = wdFormatXMLDocument
                doc.Close(False)
            finally:
                app.Quit()
        finally:
            pythoncom.CoUninitialize()
        if out.exists():
            log.info("legacy_doc_converted", src=str(src), dst=str(out))
            return str(out)
    except Exception as e:
        log.warning("legacy_doc_convert_failed", src=str(src),
                    error=f"{type(e).__name__}: {e}"[:200])
    raise ValueError(
        "该文件是旧版 .doc（OLE 复合格式），无法直接解析："
        "请用 Word 另存为 .docx 后重新上传"
        "（若服务器装有 Word，本系统会尝试自动转换，当前转换未成功）")

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
                    tenant_id: str = "", collection: str = "default",
                    ocr_pages: list[dict] | None = None,
                    layout_pages: list[dict] | None = None) -> ParsedDocument:
        """解析入口（签名与 DocParserAdapter.parse 兼容，多两个可选预取参数）。

        ocr_pages / layout_pages 由 pipeline 步骤**预取**（走 doc_parse 服务）后传入：
        远端结果在这里被消费，解析器保持同步、无状态，也不必自己持有 HTTP 客户端。
        两者都可以是 None —— 那就是"没配服务"的降级路径（见 ParseStep）。
        """
        return await asyncio.to_thread(self._parse_sync, file_path, doc_id,
                                       filename, tenant_id, collection,
                                       ocr_pages, layout_pages)

    def _parse_sync(self, file_path: str, doc_id: str, filename: str,
                    tenant_id: str, collection: str,
                    ocr_pages: list[dict] | None = None,
                    layout_pages: list[dict] | None = None) -> ParsedDocument:
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════
# PDF
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("pdf")
class PDFParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".pdf"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection,
                    ocr_pages=None, layout_pages=None):
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

        # ② 预检扫描类型（文档级摘要；逐页判定见下）
        scan_type = self._detect_scan_type(file_path)
        doc.scan_type = scan_type

        # 版面引擎产出的块总数（诊断用：为 0 说明引擎没给 parsing_res_list）
        if layout_pages:
            log.info("layout_blocks_received",
                     pages=len(layout_pages),
                     blocks=sum(len((pg or {}).get("parsing_res_list") or [])
                                for pg in layout_pages))
        pages = list(ocr_pages or [])
        page_types: list[str] = []
        with pdfplumber.open(file_path) as pdf:
            doc.page_count = len(pdf.pages)
            for page_idx, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                stripped = text.strip()
                # ⚠ 判据是「页面有没有文本层」，**不是字符密度**。
                # 旧实现用 density = len(text)/page.area < 0.005：一页 500~1500 字的
                # 正常中文 A4 密度约 0.00015（阈值的 1/33），于是正常文字页被判成扫描页，
                # 走 OCR 分支后 continue —— 标题层级、表格、图片元素**全部不再产出**。
                # 密度是尺度相关量，把它当二分条件是用法错误。
                has_text_layer = bool(getattr(page, "chars", None)) and len(stripped) >= 1
                thin_text = not has_text_layer or len(stripped) < _MIN_TEXT_CHARS
                page_types.append("scan" if thin_text else "text")

                # ① 版面引擎给的区块优先：它已经排好阅读顺序、标好标题/表格/图类型。
                #    走这条路时不再产出 pdfplumber 的逐行文字（两套文字同时进 chunk
                #    会重复），但**继续抽嵌入图**——引擎区域没给图字节，裁图仍靠这里。
                used_layout = False
                if layout_pages and page_idx < len(layout_pages):
                    from rag.adapters.layout import layout_adapter
                    # ⚠ 引擎框的口径与 pdfplumber 不同（按约 82 DPI 栅格化后的
                    # **像素**坐标，x 会超出 A4 的 595 宽）。所以**不要**拿引擎 bbox
                    # 与 pdfplumber bbox 做几何比较；图片去重改用顺序配对
                    # （见 _dedupe_image_elements）。文本/标题只用阅读顺序，与坐标无关。
                    # 走适配层而非直接调 blocks_to_elements：换引擎时这里不用改。
                    els = layout_adapter().page_elements(layout_pages[page_idx],
                                                         page_num=page_idx + 1)
                    if els:
                        doc.elements.extend(els)
                        used_layout = True

                # ② 没有版面结果且这一页文字太薄 → OCR（预取结果，其次空块告警）
                if not used_layout and thin_text:
                    ocr = pages[page_idx] if page_idx < len(pages) else None
                    doc.elements.append(
                        self._ocr_element(ocr or {}, page_idx + 1))
                    continue

                # ③ pdfplumber 逐行文字（无引擎时的路径，或引擎只给了少量区块的页）
                if not used_layout:
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

                # ④ 表格：引擎给了 parsing_res_list 的 table 块时**不再叠加** pdfplumber
                #    的 extract_tables（两条都跑会得到双份或不一致的数据）
                if not used_layout:
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
        # 文档级类型：全文字 / 全扫描 / 混合（供质量报告与运维判断走了哪条路）
        doc.metadata["page_types"] = page_types
        doc.metadata["doc_type"] = (
            "text" if page_types and all(t == "text" for t in page_types)
            else "scanned" if page_types and all(t == "scan" for t in page_types)
            else "mixed")
        doc.metadata["used_ocr_pages"] = sum(1 for t in page_types if t == "scan")
        # 图片去重：**版面引擎的 figure 区域**与**pdfplumber 的嵌入图对象**常常是
        # 同一张图（引擎按视觉区域给框，pdfplumber 按 PDF 图像对象给框）。两条都留
        # 会得到两个几乎相同的 image_caption chunk → 同一张图被 VLM 描述两次、检索
        # 时双份命中。判据用 bbox IoU，保留**带 image_bytes 的那个**（它能真的裁图，
        # 引擎框只有坐标）。
        doc.elements = self._dedupe_image_elements(doc.elements)
        # ⑤ 引擎 figure 区域的**图字节补齐**（在去重之后做：与嵌入图配对上的那些
        #    已经有 pdfplumber 的图字节，剩下的才是"引擎看到了、PDF 图像对象里
        #    找不到"的区域 —— 扫描件里的图正是这一类）。
        filled, failed = self._fill_layout_region_bytes(
            file_path, doc.elements, layout_pages)
        doc.metadata["figure_region_cropped"] = filled
        doc.metadata["figure_region_crop_failed"] = failed
        if failed:
            log.warning("layout_figure_crop_incomplete",
                        doc_id=doc_id, filled=filled, failed=failed,
                        note="缺图字节的 figure 区域不会进 VLM 描述")
        # pdfplumber/pdfminer 在这一趟里会对"字体描述符不规范"的页面逐条告警
        # （实测 96 页文档 7688 条），日志里真正要看的都被淹掉。这里把攒下的
        # 计数汇总成一条（过滤装在 observability/logging 里），既不刷屏也不丢信息。
        flush_parse_noise(f"{doc.filename} pages={doc.page_count}")
        return doc

    @staticmethod
    def _fill_layout_region_bytes(file_path: str, elements: list,
                                  layout_pages: list[dict] | None) -> tuple[int, int]:
        """给"引擎给了区域、但没有图字节"的 IMAGE 元素按框裁图 → (成功, 失败)

        为什么需要这一步：引擎的 figure 区域与 pdfplumber 的嵌入图先按顺序配对，
        配对上的用 pdfplumber 的字节（能真裁图）；**配不上的剩下的**（典型是扫描件
        —— 整页是一张位图，pdfplumber 看不到其中的"照片"对象）只有框、没有字节，
        VLM 拿不到输入 → 图上内容永远不可检索，而质量报告里看不出任何异常。

        坐标换算只认适配层的 `to_page_points`（引擎自报栅格尺寸 ÷ PDF 页尺寸 + 落页
        校验）；换算不出来就**不裁**，把那部分计进 `failed` 让上层可见 —— 宁可少
        描述一张图，也不要把页眉/整页当图交给 VLM（那会产出一段看起来正常的错误描述）。
        """
        from rag.adapters.doc_parse import crop_page_region
        from rag.adapters.layout import layout_adapter
        if not layout_pages:
            return 0, 0
        by_page: dict[int, list] = {}
        for el in elements:
            if el.content_type != ContentType.IMAGE:
                continue
            rd = el.raw_data or {}
            if rd.get("image_bytes") or not rd.get("layout_region"):
                continue
            by_page.setdefault(el.page_num or 0, []).append(el)
        if not by_page:
            return 0, 0
        filled = failed = 0
        import pdfplumber
        try:
            with pdfplumber.open(file_path) as pdf:
                for page_num, els in by_page.items():
                    idx = page_num - 1
                    if not (0 <= idx < len(pdf.pages)
                            and idx < len(layout_pages)):
                        failed += len(els)
                        continue
                    page = pdf.pages[idx]
                    for el in els:
                        box = layout_adapter().to_page_points(layout_pages[idx],
                                                              el.bbox or [],
                                                              float(page.width),
                                                              float(page.height))
                        data = crop_page_region(page, box) if box else None
                        if not data:
                            failed += 1
                            continue
                        el.raw_data["image_bytes"] = data
                        el.bbox = box          # 换成 PDF point：与嵌入图同一口径
                        el.metadata = {**(el.metadata or {}),
                                       "figure_crop": "layout_bbox"}
                        filled += 1
        except Exception as e:                              # noqa: BLE001
            log.warning("layout_figure_crop_failed",
                        error=f"{type(e).__name__}: {e}"[:200])
            return filled, failed + sum(len(v) for v in by_page.values())
        return filled, failed

    @staticmethod
    def _dedupe_image_elements(elements: list) -> list:
        """合并指向同一张图的 IMAGE 元素

        **判据刻意不用 bbox**：版面容器的框与 pdfplumber 的框**不在同一坐标系**
        （同一张图：引擎按 144 DPI 栅格化出的像素框 vs pdfplumber 的 PDF point 框，
        倍率实测 2.0 —— 见 `doc_parse._engine_page_px`）。跨坐标系比 IoU 只会得出
        "不是同一张"的错误结论，而且不会报错 —— 于是同一张
        图被 VLM 描述两次、检索命中双份。（用文本行框反推那个比值也不可靠：实测
        推出来的 2.93 与实际 2.0 相差近一半，属于"看着有依据、其实全错"。）

        所以改用**顺序配对**：同一页上，引擎的 figure 块按阅读顺序排，pdfplumber 的
        嵌入图按版面位置排，两者数量取较小值一一对应 —— 同一份文档里"引擎看到的图"
        与"PDF 里的图像对象"本就是同一批。pdfplumber 侧带 image_bytes（能真裁图），
        引擎侧带 caption/文字，合并时两边的信息都留，只留一个元素。
        数量不等时多出来的保持独立（宁可多一个块，也不要把两张不同的图合成一张）。
        """
        out: list = []
        # 按页收集：引擎侧（无 bytes）与本地侧（有 bytes）各自的 IMAGE 元素
        by_page: dict[int, list] = {}
        for el in elements:
            if el.content_type == ContentType.IMAGE:
                by_page.setdefault(el.page_num or 0, []).append(el)
        paired: set[int] = set()          # id() 集合：已被合并掉的元素
        for page, els in by_page.items():
            engine = [e for e in els if (e.metadata or {}).get("source") == "layout"]
            local = [e for e in els if not (e.metadata or {}).get("source")]
            if not engine or not local:
                continue
            for eng, loc in zip(engine, local):
                # 保留 pdfplumber 的元素（它有 image_bytes，VLM 只认它），
                # 把引擎侧的 caption/文字并过来
                if not (loc.text or "").strip() and eng.text:
                    loc.text = eng.text
                if not loc.bbox and eng.bbox:
                    loc.bbox = eng.bbox
                loc.metadata = {**(loc.metadata or {}),
                                "figure_source": "layout+embedded"}
                paired.add(id(eng))
        if not paired:
            return elements
        return [e for e in elements if id(e) not in paired]

    @staticmethod
    def _ocr_element(ocr: dict, page_num: int) -> ParsedElement:
        """OCR 结果 → ParsedElement（置信度写 metadata，消费方 QualityParseStep 读它）"""
        text = (ocr or {}).get("text") or ""
        conf = float((ocr or {}).get("avg_confidence") or 0.0)
        meta = {"ocr": True, "ocr_confidence": conf}
        if not text.strip():
            meta["blank"] = True
        return ParsedElement(content_type=ContentType.TEXT, text=text,
                             page_num=page_num, metadata=meta)

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
        """文档级扫描类型：text / scanned / hybrid —— **按"有没有文本层"判，不用密度**

        判据与逐页判定保持一致（见 `_parse_sync` 里那段注释）：字符密度是尺度相关量，
        A4 正常中文页的密度约为旧阈值 0.005 的 1/33 —— 用它当二分条件会把正常文字页
        判成扫描页（旧实现因此在文字页上 continue，标题/表格/图全都不再产出）。

        采样而非全量：只取文档两端的几页。全量 `extract_text` 对数百页文档是分钟级
        开销，而这一步只给路由与质量报告一个**文档级摘要** —— 真正的逐页分流在
        `_parse_sync` 里做（那里每页都判），所以采样偏保守不会漏内容。
        """
        import pdfplumber
        try:
            with pdfplumber.open(file_path) as pdf:
                n = len(pdf.pages)
                if not n:
                    return "text"
                idx = sorted({0, 1, 2, n - 1, n - 2, n - 3} & set(range(n)))
                has_text = []
                for i in idx:
                    t = (pdf.pages[i].extract_text() or "").strip()
                    has_text.append(len(t) >= _MIN_TEXT_CHARS)
                if all(has_text):
                    return "text"
                if not any(has_text):
                    return "scanned"
                return "hybrid"
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

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection,
                    ocr_pages=None, layout_pages=None):
        # .doc 是复合二进制格式，python-docx 读不了 → 先转 .docx（失败则给出可处置的
        # 报错，而不是让 python-docx 抛一句难以定位的 PackageNotFoundError）
        if Path(file_path).suffix.lower() == ".doc":
            file_path = _convert_legacy_doc(file_path)
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
        # 内嵌图片：Word 的图片是 package part（不在 paragraphs 里），
        # 不显式取就永远没有 IMAGE 元素 → 下游 vlm_caption 无输入、图内容不可检索
        doc.elements.extend(self._extract_images(d, page))
        doc.metadata["doc_type"] = "text"
        doc.page_count = max(1, page)
        return doc

    @staticmethod
    def _extract_images(d, page: int) -> list[ParsedElement]:
        """从 docx package 里取所有内嵌图片（失败逐张跳过，不影响文本入库）"""
        out: list[ParsedElement] = []
        pkg = getattr(getattr(d, "part", None), "package", None)
        for part in (getattr(pkg, "parts", None) or []):
            try:
                ct = str(getattr(part, "content_type", "") or "")
                if not ct.startswith("image/"):
                    continue
                blob = part.blob
                if not blob:
                    continue
                out.append(ParsedElement(
                    content_type=ContentType.IMAGE, text="", page_num=page,
                    raw_data={"image_bytes": blob,
                              "image_ext": ct.split("/")[-1],
                              "part": str(getattr(part, "partname", ""))}))
            except Exception:
                continue
        return out


# ═══════════════════════════════════════════════════════════
# Excel / CSV
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("xlsx")
class ExcelParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".xlsx", ".xls", ".csv"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection,
                    ocr_pages=None, layout_pages=None):
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

    @staticmethod
    def _classify_sheet(headers: list[str], body: list[list[str]]) -> str:
        """Sheet 类型判定：data（数据表）/ pivot（透视表）/ summary（汇总表）

        判据只看结构特征，不依赖任何模型（设计 §5.3 要求"每个 Sheet 独立处理并
        区分类型"，原先完全没有这一步）：
          - summary：行数极少且几乎全是数字 → 汇总/指标表
          - pivot  ：首列有大量重复值（分组维度）+ 列数偏多 → 透视/交叉表
          - data   ：其余（规整的行记录）
        """
        if not body or not headers:
            return "data"
        rows_n, cols_n = len(body), len(headers)

        def _numeric_ratio(r: list[str]) -> float:
            vals = [c for c in r if str(c).strip()]
            if not vals:
                return 0.0
            ok = 0
            for c in vals:
                try:
                    float(str(c).replace(",", "").replace("%", ""))
                    ok += 1
                except ValueError:
                    pass
            return ok / len(vals)

        if rows_n <= 3:
            ratios = [_numeric_ratio(r) for r in body]
            if ratios and sum(ratios) / len(ratios) >= 0.6:
                return "summary"
        first_col = [str(r[0]).strip() for r in body if r and str(r[0]).strip()]
        if first_col:
            distinct_ratio = len(set(first_col)) / len(first_col)
            if distinct_ratio < 0.6 and cols_n >= 4:
                return "pivot"
        return "data"

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
        sheet_type = self._classify_sheet(headers, body)
        # Sheet 名作一级路径；sheet_type 供后续步骤区分数据表/透视表/汇总表
        try:
            doc.elements[-1].metadata["sheet_type"] = sheet_type
        except Exception:
            pass
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

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection,
                    ocr_pages=None, layout_pages=None):
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
                # 内嵌图片（流程图/架构图常整张作为图片贴进来）：
                # 不取就没有 IMAGE 元素 → vlm_caption 无输入 → 图上内容不可检索
                try:
                    if shape.shape_type is not None and getattr(
                            shape, "image", None) is not None:
                        blob = shape.image.blob
                        if blob:
                            doc.elements.append(ParsedElement(
                                content_type=ContentType.IMAGE, text="",
                                page_num=idx,
                                raw_data={"image_bytes": blob,
                                          "image_ext": shape.image.ext,
                                          "slide_num": idx}))
                except Exception:
                    pass
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
        doc.metadata["doc_type"] = "text"
        return doc


# ═══════════════════════════════════════════════════════════
# Markdown
# ═══════════════════════════════════════════════════════════

@AdapterRegistry.register_parser("markdown")
class MarkdownParser(BaseParser):

    @property
    def supported_extensions(self) -> set[str]:
        return {".md", ".markdown"}

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection,
                    ocr_pages=None, layout_pages=None):
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

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection,
                    ocr_pages=None, layout_pages=None):
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

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection,
                    ocr_pages=None, layout_pages=None):
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

    def _parse_sync(self, file_path, doc_id, filename, tenant_id, collection,
                    ocr_pages=None, layout_pages=None):
        doc = ParsedDocument(doc_id=doc_id, filename=filename, file_type="img",
                             tenant_id=tenant_id, collection=collection)
        # OCR 来源优先级：doc_parse 服务（预取）→ 进程内 PaddleOCR（未配服务时的兜底）
        ocr_text, confidence = "", 0.0
        if ocr_pages:
            first = ocr_pages[0] or {}
            ocr_text = str(first.get("text") or "")
            confidence = float(first.get("avg_confidence") or 0.0)
        else:
            ocr = _get_ocr()
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
            raw_data={"image_bytes": image_bytes, "page_num": 1},
            # 置信度写在 metadata：消费方 QualityParseStep 读的是 metadata["ocr_confidence"]。
            # 原实现写在 raw_data，于是图片的 OCR 置信度永远进不了质量报告（口径不一致）。
            metadata={"ocr": True, "ocr_confidence": confidence,
                      "blank": not ocr_text.strip()}))
        doc.metadata["doc_type"] = "scanned" if ocr_text else "text"
        doc.metadata["used_ocr_pages"] = 1
        doc.page_count = 1
        return doc
