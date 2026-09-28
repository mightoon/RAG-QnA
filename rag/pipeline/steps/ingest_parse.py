"""
入库步骤：解析与结构化（rag/pipeline/steps/ingest_parse.py）

- DetectFormatStep  格式自检（magic bytes，防伪装扩展名）
- LayoutParseStep   版面解析服务调用（PaddleX /layout-parsing，可降级）
- ParseStep         文档解析（按扩展名路由；扫描页 OCR 优先走 doc_parse 服务）
- RegionEnhanceStep 引擎区域补内容（表格/公式识别；按能力门禁，缺配即留痕降级）
- QualityParseStep  扫描件 OCR 质量检查（置信度/空页率/乱码率）
- OutlineStep       OutlineBuilder：标题层级栈 → section_path
- ReorderColumnsStep 双栏重排（仅本地解析路径需要；版面引擎已给阅读顺序）
- VLMCaptionStep    图片 VLM 描述生成（走 config.vlm 段，多级降级）
- TableExtractStep  表格原始数据收集（供 MySQL table_data 精确查询）
"""
from __future__ import annotations

import base64
import io
import re
from pathlib import Path

from rag.adapters.doc_parse import (DocParseBadInput, DocParseEndpointMissing,
                                    DocParseTransient, DocParseUnavailable,
                                    doc_parse_capability_for, make_client)
from rag.models import (ContentType, ParsedElement, QualityIssue, TableData)
from rag.observability.logging import get_logger
from rag.pipeline.base import PipelineStep, StepError, StepRegistry
from rag.pipeline.context import IngestContext

log = get_logger("rag.steps.parse")

# 走 OCR 的扩展名：PDF（逐页判有无文字层）+ 图片（整图）。
# 其余类型（md/txt/csv/html/docx/xlsx/pptx）由解析器直接读文件，**不能**送去 OCR ——
# 详见 ParseStep.execute 里的说明（实测 .md 会让整篇文档卡在重试里）。
_OCR_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")


def _path_safe(title: str) -> str:
    """标题进入 section_path 之前，把 ASCII 斜杠换成全角斜杠

    section_path 用 **ASCII "/"** 当层级分隔符（`"第3章/3.1节/3.1.2"`），
    而真实标题里就会带斜杠 —— 中文研报标题尤其常见：

      · 日期："三、9/22晨间最新催化：联盟扩张阶段开启"（实测踩到的就是它）
      · 并列："输入/输出"、"A/B 测试"

    这类标题拼进路径后，**所有按 "/" 取叶子的消费点都会劈错**：

      · 详情页大纲：段数被多算一级 → "三、…"显示成"二、…"下面的子项，
        叶子标题只剩"22晨间最新催化：…"（用户报的就是这个观感）；
      · 引用与检索提示里的 section（`models.py` / `query_generate.py` 同样取叶子）
        → 引用里显示的章节名被截断。

    在**源头**换掉这一个是唯一一处改动就能全修的办法：路径从此无歧义，
    而且中文语境里全角斜杠本来就更自然（显示上几乎看不出差别）。
    ⚠ 存量文档的路径是旧口径（标题里的斜杠是原样的），要等重解析才会变正确。
    """
    return _norm_title(title).replace("/", "／")


def _norm_title(title: str) -> str:
    """标题归一：折叠空白（比较/查表都用它，避免"换行 vs 空格"导致查不到）"""
    return " ".join(str(title or "").split())


# 编号型标题："5.1 …" / "3.2.1 …"（全角点也认）。分段只用 1~2 位数字：
# 这样 "2026.9 版本说明" 这种年份开头的标题不会被误判成二级。
_NUM_DOTTED = re.compile(r"^(\d{1,2}(?:[.．]\d{1,2}){1,3})(?=[^\d]|$)")
# 同一主号至少出现这么多次，才认为这篇文档真的在用编号分层。
# 护栏的必要性：研报里常被版面模型误判成"标题"的**数据标签**（"6.32亿人"）
# 也长得像编号，孤零零一个不该把层级带偏。
_MIN_SAME_MAJOR = 2


def _numbering_levels(titles: list[str]) -> dict[str, int]:
    """按标题自身的编号推断层级：{"5.1新增…": 2, "3.2.1 边界": 3, …}

    背景：版面服务的区块**只有 bbox/label/content 三个字段**，压根不给层级
    （实测），PDF 也常常没有书签 —— 于是所有标题都被当成 1 级，
    "一、" 与 "5.1" 并列，大纲看不出父子关系。字号也分不开（实测同篇文档里
    「五、」30.5px 与「1.1」29.9px 只差 0.6px）。

    编号是标题自己携带的结构证据（不是猜字号），规则确定：
      · 以 `N.M[.K]` 开头 → 层级 = 点数 + 1（`5.1` → 2、`3.2.1` → 3）；
      · 主号（第一个数字）在这篇文档里**至少出现两次**才算数（见上面的护栏）；
      · 其余标题不推断，沿用解析器给的层级。
    调用方只用它**加深**层级（`num > level` 时才采用），不会把已有层级改浅。
    """
    cands: dict[str, tuple[int, str]] = {}
    majors: dict[str, int] = {}
    for t in titles:
        s = _norm_title(t)
        m = _NUM_DOTTED.match(s)
        if not m:
            continue
        num = m.group(1).replace("．", ".")
        lvl = min(1 + num.count("."), OutlineStep.MAX_DEPTH)
        major = num.split(".")[0]
        cands[s] = (lvl, major)
        majors[major] = majors.get(major, 0) + 1
    return {t: lvl for t, (lvl, major) in cands.items()
            if majors.get(major, 0) >= _MIN_SAME_MAJOR}


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
        # 格式校验是**毫秒级**的，但它后面紧跟着版面解析（大文档要几十秒~几分钟），
        # 而版面步骤在完成前不报阶段 —— 不在这里推一次状态的话，任务会一直挂在
        # "排队中"，用户完全看不出"其实已经在解析了"（实测 96 页文档要等 1 分多钟）。
        await ctx.report("parsing", 0.02, "格式校验完成，开始解析")


@StepRegistry.register("layout_parse")
class LayoutParseStep(PipelineStep):
    """版面解析服务调用（PaddleX `/layout-parsing`）——入库链路的"版面结构化"入口。

    为什么单独成一步而不是塞进解析器：`DocParserAdapter._parse_sync` 是同步函数、
    跑在 `asyncio.to_thread` 里（见 `adapters/doc_parser.py`），而这是 HTTP 调用。
    放在步骤层 await 既天然解决了 async，也让"换引擎/换地址"不必动解析器
    （与 `architecture.md` §5.3 把 detect_layout 列为独立步骤一致）。

    产出写入 `ctx.meta["layout_pages"]`：逐页的
    `parsing_res_list / layout_det_res / overall_ocr_res / table_res_list / formula_res_list`。
    当前 `parse` 步骤消费其中的 `overall_ocr_res`（文字 + 逐行置信度）；
    `parsing_res_list`/`layout_det_res` 由后续的图区理解与表格双轨消费。
    """

    async def execute(self, ctx: IngestContext) -> None:
        s = ctx.services
        # 版面服务面向 PDF/图片；其它格式（docx/xlsx/md…）没有 PDF 页模型，直接跳过
        if not (ctx.doc.filename or "").lower().endswith((".pdf", ".png", ".jpg",
                                                          ".jpeg", ".bmp",
                                                          ".tiff", ".webp")):
            return
        cap = doc_parse_capability_for(s.config, "layout")
        # 门禁：没配 / 配错（端点 404、产线不符）都不发请求，且必须留痕 ——
        # 静默跳过会让人以为"版面引擎在生效"，而实际仍走 pdfplumber。
        ready = getattr(s, "doc_parse_ready", None)
        blocked = ready("layout") if ready is not None else None
        if blocked:
            # 版面引擎是**增强**，不是入库前提：配不上不该让整篇文档失败（PDF 仍能由
            # pdfplumber 产出文字）。但也绝不能无声跳过 —— 那会让人以为版面/表格/阅读
            # 顺序在生效。所以记成一条 info 级 issue 进质量报告，并降级继续。
            ctx.quality.issues.append(QualityIssue(
                stage="parse", severity="info", code="layout_service_unavailable",
                message=f"版面解析服务不可用，已降级为本地解析：{blocked}",
                action="degrade"))
            ctx.add_warning(f"版面解析已跳过：{blocked}")
            await ctx.report("parsing", 0.18,
                             f"版面解析已跳过：{blocked[:60]}")
            return
        data = Path(ctx.file_path).read_bytes()
        client = make_client(s.config, cap)
        # 每完成一批（默认 10 页/批）就把进度报上去：大文档整段要几十秒到几分钟，
        # 期间不报的话界面上只有"排队中/解析中"干等（实测 96 页 ≈ 1 分钟）。
        # 进度在 0.03 → 0.18 之间线性插值（属于"解析"阶段，不是分块）。
        async def _on_batch(done: int, total: int) -> None:
            ratio = (done / total) if total else 1.0
            await ctx.report("parsing", round(0.03 + 0.15 * ratio, 3),
                             f"版面解析 {done}/{total} 页")
        try:
            pages = await client.parse_layout(data, on_batch=_on_batch)
        except DocParseUnavailable as e:
            # 端点不存在 = 配置错（不是抖动）：重试三轮也不会自愈。记下结论让后续
            # 文档直接跳过，然后**降级继续**（版面是增强，不该让文档入库失败）。
            note = getattr(s, "note_doc_parse_failure", None)
            if note is not None:
                note(cap, str(e))
            ctx.quality.issues.append(QualityIssue(
                stage="parse", severity="info", code="layout_service_unavailable",
                message=f"版面解析服务不可用，已降级为本地解析：{e}",
                action="degrade"))
            ctx.add_warning(f"版面解析已跳过：{e}")
            return
        ctx.meta["layout_pages"] = pages
        ctx.meta["layout_engine"] = f"paddlex:{cap}"
        # 页数被服务端截断之类的结论必须**让用户看见**（见 doc_parse._pdf_batches）
        _surface_doc_parse_notes(ctx, client, "版面解析")
        total_chars = sum(
            sum(len(str(t)) for t in ((p.get("overall_ocr_res") or {})
                                      .get("rec_texts") or []))
            for p in pages)
        # 版面解析属于**解析**阶段（不是分块）：阶段名如实上报，后续 parse 步骤
        # 从这里往上继续推进（0.18 → 0.20…），进度与阶段都保持单调、语义一致。
        await ctx.report("parsing", 0.18,
                         f"版面解析完成：{len(pages)} 页 / {total_chars} 字符")


@StepRegistry.register("region_enhance")
class RegionEnhanceStep(PipelineStep):
    """引擎区域补内容：把"只有框、没有内容"的区域按配置的能力补起来。

    处理两类区域（各由一项 doc_parse 能力承担，**没配就不发请求但一定留痕**）：

      · 表格（`table` → /table-recognition）：引擎只给框、没给结构 → 裁区域图
        走表格识别拿 HTML → 交给后面的 table_extract 转成行列进 `table_data`。
        不做这一步：引擎识别出的表格在"精确数值查询"里查不到（等价于没接引擎）。
      · 公式（`formula` → /formula-recognition）：引擎没给 `block_content` → 裁图
        识别成文本写回元素。不做这一步：公式完全不在正文里，按公式符号永远检索
        不到（而它明明在页面上）。

    figure 区域的 `image_bytes` 不在这里补 —— 解析器手上就有已打开的 pdfplumber
    页面，裁图在 `doc_parser._fill_layout_region_bytes` 一次做完（避免重复渲染）。

    为什么"缺配"必须留痕：识别能力是增强，跳过不该让文档入库失败；但如果不留痕，
    "库里检索不到这张表"与"这份文档本来就没有表"在质量报告里长得一模一样 ——
    用户只能靠猜。所以缺配记一条 info 级 issue + 一条 warning。
    """

    _CN = {"table": "表格", "formula": "公式"}

    async def execute(self, ctx: IngestContext) -> None:
        s = ctx.services
        if not ctx.parsed:
            return
        if not (ctx.doc.filename or "").lower().endswith(".pdf"):
            return                      # 只有 PDF 有"页面 + 区域框"的模型
        layout_pages = ctx.meta.get("layout_pages") or []
        if not layout_pages:
            return                      # 没走版面引擎：区域信息不存在，无从裁起

        table_els = []
        for e in ctx.parsed.elements:
            if e.content_type != ContentType.TABLE:
                continue
            rd = e.raw_data or {}
            if rd.get("rows"):
                continue                     # 已经结构化了（pdfplumber / Excel 分支）
            html = str(rd.get("table_html") or "")
            if html and _html_table_to_rows(html)[1]:
                continue                     # 引擎的 HTML 能用 → 不必再打一次服务
            # 走到这里有两类：引擎**只给框没给内容**（html 为空），或**给了 HTML
            # 但解析不出行列**（跨行跨列很乱、只有一层标签…）。后者以前不会进这里，
            # 结果是"引擎的 HTML 解析不出来 = 这张表没有任何结构"，而表格识别能力
            # 明明可用 —— 现在一并送过去，拿回来对比后再决定用哪份。
            table_els.append(e)
        formula_els = [e for e in ctx.parsed.elements
                       if (e.raw_data or {}).get("formula_region") and
                       not (e.text or "").strip()]
        if not table_els and not formula_els:
            return

        ready = getattr(s, "doc_parse_ready", None)
        plan: list[tuple[str, list[ParsedElement]]] = []
        for internal, els in (("table", table_els), ("formula", formula_els)):
            if not els:
                continue
            blocked = (ready(internal) if ready is not None
                       else f"未配置「{internal}」能力的服务地址")
            if blocked:
                cn = self._CN[internal]
                ctx.add_warning(
                    f"{cn}区域识别已跳过（{len(els)} 处）：{blocked}")
                ctx.quality.issues.append(QualityIssue(
                    stage="parse", severity="info",
                    code=f"doc_parse_{internal}_unavailable",
                    message=f"{cn}区域未做识别，已降级（内容以引擎文字为准）："
                            f"{blocked}",
                    action="degrade"))
                continue
            plan.append((internal, els))
        if not plan:
            await ctx.report("chunking", 0.25, "区域识别已跳过（能力未配置）")
            return

        done = {k: 0 for k, _ in plan}
        kept = {k: 0 for k, _ in plan}
        failed = {k: 0 for k, _ in plan}
        skipped = {k: 0 for k, _ in plan}
        import pdfplumber
        try:
            with pdfplumber.open(ctx.file_path) as pdf:
                for internal, els in plan:
                    for el in els:
                        # ⚠ 逐个区域**复查门禁**：同一次解析里，前面的区域失败会给该
                        # 能力开静默期，后面的区域就该直接跳过 —— 否则一篇有很多公式的
                        # 文档会为每个区域白打一次连接尝试 + 退避（实测 ≈3s/区域），
                        # 而所有区域的结论必然相同。规划阶段查一次是不够的。
                        if ready is not None and ready(internal):
                            skipped[internal] += 1
                            continue
                        # 三态：True=用了服务结果 / None=服务给了但**保留引擎结果**
                        # （不是失败）/ False=真失败
                        res = await self._one(ctx, s, pdf, layout_pages, el,
                                              internal)
                        if res is True:
                            done[internal] += 1
                        elif res is None:
                            kept[internal] += 1
                        else:
                            failed[internal] += 1
        except Exception as e:                              # noqa: BLE001
            log.warning("region_enhance_failed", doc_id=ctx.doc.doc_id,
                        error=f"{type(e).__name__}: {e}"[:200])
            ctx.add_warning(f"区域识别未完成（{type(e).__name__}），"
                            "内容以引擎文字为准")
            return

        summary = " / ".join(
            f"{self._CN[k]} {done[k]} 处" + (f"（另有 {kept[k]} 处保留引擎结果）"
                                            if kept.get(k) else "")
            for k in done if done[k] or kept.get(k))
        if summary:
            await ctx.report("chunking", 0.25, f"区域识别完成：{summary}")
        # 瞬时失败（服务 5xx/连不上）按类汇总成**一条**告警：一个公式多、服务又挂了的
        # 文档逐区域告警会刷出几十条重复内容，反而看不清
        transient = ctx.meta.get("region_transient") or {}
        for kind, reason in transient.items():
            more = (f"；静默期内另有 {skipped[kind]} 处直接跳过"
                    if skipped.get(kind) else "")
            ctx.add_warning(f"{self._CN.get(kind, kind)}区域识别暂时失败："
                            f"{reason[:140]}（服务恢复后重解析即可，"
                            f"未记为能力不可用）{more}")
        # 静默期是**上一次**（或上一篇文档）开的：这里的跳过没有对应的"本次失败"，
        # 单独说一句，免得用户以为区域识别压根没跑
        for kind, n in skipped.items():
            if n and kind not in transient:
                ctx.quality.issues.append(QualityIssue(
                    stage="parse", severity="info",
                    code=f"doc_parse_{kind}_cooldown_skipped",
                    message=f"{self._CN.get(kind, kind)}区域 {n} 处未做识别："
                            "该服务刚被判定为暂时不可用（静默期内不再重试，"
                            "到期自动恢复）", action="degrade"))
        lost = " / ".join(f"{self._CN[k]} {v} 处"
                          for k, v in failed.items() if v and k not in transient)
        if lost:
            # 失败也要说清楚"丢的是什么"，不能只说"部分失败"
            ctx.add_warning(f"区域识别未完成：{lost}"
                            "（该区域内容以引擎文字为准，可能缺结构或公式符号）")

    async def _one(self, ctx: IngestContext, s, pdf, layout_pages,
                   el: ParsedElement, internal: str) -> bool | None:
        """单个区域：裁图 → 识别 → 写回元素

        返回三态：True=采用服务结果；**None=保刻意留引擎结果**（服务给了，但引擎那份
        更完整 —— 这不该算失败）；False=真失败（裁不出图/服务没给内容/调用出错）。
        """
        from rag.adapters.doc_parse import crop_page_region
        from rag.adapters.layout import layout_adapter
        idx = (el.page_num or 0) - 1
        if not (0 <= idx < len(pdf.pages) and idx < len(layout_pages)):
            return False
        page = pdf.pages[idx]
        rd = el.raw_data if el.raw_data is not None else {}
        el.raw_data = rd                    # 保证写回的是同一个 dict
        # 元素 bbox 的单位：引擎给的（默认）是栅格像素，必须先换算；已由解析器
        # 换算过的（bbox_unit 标记）直接用 —— 混用两套坐标会把框切到别处。
        # 换算走适配层（`to_page_points`），换引擎不用改这里。
        if (el.metadata or {}).get("bbox_unit") == "pdf_point":
            box = list(el.bbox or [])
        else:
            box = layout_adapter().to_page_points(layout_pages[idx], el.bbox or [],
                                                  float(page.width), float(page.height))
        if not box:
            return False
        # 200 DPI：表格线/小字号公式要看清，150 会把细线糊掉（识别率下降）
        img = crop_page_region(page, box, resolution=200)
        if not img:
            return False

        def _ok(cap_name: str) -> None:
            """一次成功调用：让容器清掉该能力的静默期（服务恢复后立刻恢复正常）"""
            fn = getattr(s, "note_doc_parse_ok", None)
            if fn is not None and cap_name:
                fn(cap_name)

        try:
            cap = doc_parse_capability_for(s.config, internal)
            client = make_client(s.config, cap)
            if internal == "table":
                res = await client.recognize_table(img)
                html = str(res.get("table_html") or "")
                if not html:
                    return False
                # 服务给的结构**不一定要**：只有当它比引擎原有那份更能解析出行列时
                # 才替换。理由：引擎的 HTML 与页面文字是同一套阅读顺序，服务那份
                # 是重新识别出来的；能用就用引擎的（少一次"同一张表两套说法"）。
                new_rows = _html_table_to_rows(html)[1]
                old_html = str(rd.get("table_html") or "")
                old_rows = _html_table_to_rows(old_html)[1] if old_html else []
                _ok(cap)                    # 调用成功 → 清掉静默期/degraded
                if old_rows and len(old_rows) >= len(new_rows):
                    # 引擎那份更好：保留它，同时把服务结果留档备查（不算失败）
                    rd.setdefault("table_html_service", html)
                    return None
                rd["table_html"] = html
                if res.get("cells"):
                    rd["table_cells"] = res["cells"]
                if new_rows:
                    headers, rows = _html_table_to_rows(html)
                    rd["headers"], rd["rows"] = headers, rows
                el.metadata = {**(el.metadata or {}),
                               "table_source": "table-recognition"}
                return True
            res = await client.recognize_formula(img)
            text = str(res.get("text") or "").strip()
            if not text:
                return False
            _ok(cap)
            el.text = text
            el.metadata = {**(el.metadata or {}),
                           "formula_source": "formula-recognition"}
            return True
        except DocParseEndpointMissing as e:
            # 端点不存在/能力配错：重试不会自愈，记结论让后续文档不再打这个端点
            note = getattr(s, "note_doc_parse_failure", None)
            if note is not None:
                note(doc_parse_capability_for(s.config, internal), str(e))
            return False
        except DocParseTransient as e:
            # 5xx/超时：**本次**失败而已。绝不能记成"能力不可用" —— 实测公式识别
            # 服务 18083 就是这样：/health 200、推理连续 500，若把它缓存成不可用，
            # 服务恢复后本进程也永远不会再试（用户看到的还是绿的）。
            # 原因攒进 ctx.meta，由 execute 汇总成一条告警 + 记进 degraded。
            log.warning("region_recognize_transient", doc_id=ctx.doc.doc_id,
                        kind=internal, error=str(e)[:200])
            ctx.meta.setdefault("region_transient", {})[internal] = str(e)
            note = getattr(s, "note_doc_parse_degraded", None)
            if note is not None:
                note(doc_parse_capability_for(s.config, internal), str(e))
            return False
        except DocParseUnavailable as e:
            note = getattr(s, "note_doc_parse_failure", None)
            if note is not None:
                note(doc_parse_capability_for(s.config, internal), str(e))
            return False
        except Exception as e:                              # noqa: BLE001
            log.warning("region_recognize_failed", doc_id=ctx.doc.doc_id,
                        kind=internal, error=f"{type(e).__name__}: {e}"[:200])
            return False


@StepRegistry.register("parse")
class ParseStep(PipelineStep):
    """文档解析：file_path → ParsedDocument"""

    async def execute(self, ctx: IngestContext) -> None:
        s = ctx.services
        await ctx.report("parsing", 0.20, "开始解析")

        # ① 扫描页 OCR：优先用**已配置的 doc_parse 服务**，其次才是进程内 PaddleOCR。
        #    预取一次整篇结果交给解析器消费，避免解析器内部按页逐次调用远端。
        #
        # ⚠ 预取只对**需要 OCR 的类型**做（PDF 逐页 / 图片整图）。原实现是"非 PDF 一律
        #   当图片送 OCR"，于是 md/txt/csv/html/docx/xlsx/pptx 的字节被丢给
        #   /ocr 端点 → 服务端判 "Invalid input file"（实测 .md → HTTP 422）→
        #   这里抛的是**可重试**的 StepError → 文档反复重试后失败，永远入不了库。
        #   上传界面明明写着支持 TXT/MD/CSV/Word/Excel，实际全被这一步拦死。
        ocr_pages = None
        cap = doc_parse_capability_for(s.config, "ocr")
        ready = getattr(s, "doc_parse_ready", None)
        blocked = ready("ocr") if ready is not None else None
        name_l = (ctx.doc.filename or "").lower()
        need_ocr = name_l.endswith(_OCR_EXTS)
        if blocked:
            # 未配置/配错不是致命错误：扫描页回落到进程内 PaddleOCR（未装则为空文本）。
            # 但要留痕 —— 否则"扫描件入库成功却没有文字"无从归因。
            ctx.add_warning(f"OCR 服务已跳过：{blocked}")
        elif not need_ocr:
            log.debug("ocr_prefetch_skipped", filename=ctx.doc.filename,
                      reason="该类型由解析器直接读取，不需要 OCR 预取")
        else:
            try:
                data = Path(ctx.file_path).read_bytes()
                client = make_client(s.config, cap)
                if name_l.endswith(".pdf"):
                    ocr_pages = await client.ocr_pdf_pages(data)
                else:
                    one = await client.ocr_image(data)
                    ocr_pages = [one]
                ctx.meta["ocr_engine"] = f"paddlex:{cap}"
                # 页数被截断之类的结论必须让用户看见（见 doc_parse._pdf_batches）
                _surface_doc_parse_notes(ctx, client, "OCR")
            except DocParseUnavailable as e:
                # 端点不存在 = 配置错（不是抖动）：重试三轮也不会自愈，直接失败并
                # 把可处置的原因写给用户；文件本身被拒收（4xx）同理；
                # 其余（超时/5xx/连接）按可重试处理。
                if isinstance(e, (DocParseEndpointMissing, DocParseBadInput)):
                    raise StepError("parse", f"OCR 服务不可用：{e}",
                                    retryable=False) from e
                raise StepError("parse", f"OCR 服务失败：{e}",
                                retryable=True) from e

        parser = s.get_parser(ctx.doc.filename)
        try:
            ctx.parsed = await parser.parse(
                ctx.file_path, ctx.doc.doc_id, ctx.doc.filename,
                ctx.doc.tenant_id, ctx.doc.collection,
                ocr_pages=ocr_pages,
                layout_pages=ctx.meta.get("layout_pages"))
        except TypeError:
            # 解析器未接受新参数（如客户自定义实现）→ 退回原签名，不阻断入库
            ctx.parsed = await parser.parse(
                ctx.file_path, ctx.doc.doc_id, ctx.doc.filename,
                ctx.doc.tenant_id, ctx.doc.collection)
        ctx.doc.page_count = ctx.parsed.page_count
        ctx.doc.language = ctx.parsed.language
        ctx.task.total_pages = ctx.parsed.page_count
        ctx.meta["scan_type"] = ctx.parsed.scan_type
        # 文档级解析路径由**解析器**给出（页级判定在那里做），质量报告在这里起步就有
        # —— 不依赖 quality_parse 步骤：pdf_text / docx 等 workflow 并没有它，
        # 放在那边会让"这份文档走了哪条路"在这些类型上永远是空。
        ctx.quality.doc_id = ctx.doc.doc_id
        ctx.quality.doc_type = str(ctx.parsed.metadata.get("doc_type") or "")
        ctx.quality.used_ocr_pages = int(
            ctx.parsed.metadata.get("used_ocr_pages") or 0)
        # 图区裁图结果也要进质量报告：裁不出来的图 = 后面不会被 VLM 描述的内容
        _surface_figure_notes(ctx)
        await ctx.report("parsing", 0.22,
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
        # 文档级解析路径（供质量报告与运维判断"这份文档走了哪条路"）。
        # 主要赋值在 ParseStep（那里拿得到 parsed.metadata）；这里兜一次是为了
        # 兼容自建 pipeline 只挂了 quality_parse 的情形。
        if not ctx.quality.doc_type:
            ctx.quality.doc_type = str(ctx.parsed.metadata.get("doc_type") or "")
            ctx.quality.used_ocr_pages = int(
                ctx.parsed.metadata.get("used_ocr_pages") or 0)
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
        #    层级来源优先级：**编号**（标题自带的"5.1"，只在解析器没给更深的层级时生效）
        #    → 解析器给的 heading_level → 1 级。见 _numbering_levels 的说明。
        num_levels = _numbering_levels(
            [el.text for el in ctx.parsed.elements
             if el.content_type == ContentType.TITLE])
        stack: list[tuple[int, str]] = []       # [(level, title)]
        for el in ctx.parsed.elements:
            level = None
            if el.content_type == ContentType.TITLE:
                level = el.metadata.get("heading_level") or 1
                num = num_levels.get(_norm_title(el.text))
                if num and num > level:
                    level = num                 # 编号只**加深**，不改浅
                level = min(int(level), self.MAX_DEPTH)
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, _path_safe(el.text)[:64]))
            el.metadata["section_path"] = "/".join(
                t for _, t in stack)[:512] if stack else ""
            el.metadata["heading_level"] = level
        await ctx.report("chunking", 0.28, "大纲构建完成")

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
        if ctx.meta.get("layout_pages"):
            # 版面引擎已给阅读顺序：重排是**兜底**，有引擎时不要用近似阈值法
            # 去覆盖引擎的顺序（否则等于把好顺序改差）
            return
        by_page: dict[int, list[ParsedElement]] = {}
        for el in ctx.parsed.elements:
            by_page.setdefault(el.page_num or 0, []).append(el)
        reordered: list[ParsedElement] = []
        for page in sorted(by_page):
            els = by_page[page]
            # bbox 可能在这个字段，也可能只在 metadata 里（旧的解析器实现）；
            # 只读 metadata 会让整步永远跳过（元素上没有这个键 → 一直 continue）
            boxes = [_element_bbox(el) for el in els]
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
                b = _element_bbox(el) or [0, 0, 0, 0]
                col = 0 if float(b[0]) < 200 else 1
                return (col, float(b[1]))
            els = sorted(els, key=_key)
            reordered.extend(els)
            ctx.quality.issues.append(QualityIssue(
                stage="parse", severity="low", code="two_column_reorder",
                message=f"第 {page} 页按双栏版面重排", page_num=page,
                action="auto"))
        ctx.parsed.elements = reordered


_VLM_PROMPT = ("用一句话描述这张图片的内容，"
               "如果是图表请说明图表类型和数据要点。")

# VLM 描述里的**提示词/思考残留**：实测麦肯锡那篇的图题正文里出现
#   `**视觉引擎已解析图片，内容描述如下：**`、`**最终答案：**`、`**一句话描述：**`、
#   `**内容描述：**`、`**图片内容如下：**`
# 它们是模型把"任务说明"或"思考小结"一起吐了出来。**必须清掉**：这段文字会作为
# `image_caption` 块的正文进 MySQL/ES/Milvus，既污染检索（用户搜"图片"会命中一堆
# "内容描述如下"），也让引用里出现莫名的加粗标题。
_LEAD_LABEL_RE = re.compile(
    r"^\s*(?:\*\*|__)?\s*(?:视觉引擎已解析图片[^：:*\n]{0,12}"
    r"|最终答案|一句话(?:描述|总结)|图片?(?:内容)?描述|内容描述|图片内容如下"
    r"|描述|答案|总结)\s*(?:如下)?\s*[:：]\s*(?:\*\*|__)?\s*")
_BOLD_RE = re.compile(r"\*\*|__")


def clean_vlm_caption(text: str) -> tuple[str, bool]:
    """清掉 VLM 描述开头/正文里的提示词与思考残留 → (干净文本, 是否改过)

    只做"去壳"，不做改写：**能删的只有那些一眼看就是壳的东西** ——
      ① 开头的标签式前缀（`**最终答案：**`、`内容描述：`…），可能连着出现几层；
      ② 正文里的 markdown 加粗标记（`**xx**` → `xx`）。
    描述内容本身一个字都不动：宁可留着一点噪声，也不能把"图上写了什么"改掉。
    """
    raw = (text or "").strip()
    if not raw:
        return "", False
    out = raw
    for _ in range(4):                      # 前缀可能叠好几层
        stripped = _LEAD_LABEL_RE.sub("", out, count=1).strip()
        if stripped == out:
            break
        out = stripped
    out = _BOLD_RE.sub("", out).strip()
    out = re.sub(r"^[\s:：\-—]+", "", out).strip()
    return out, (out != raw)


def figure_is_blank(image_bytes: bytes, *, size: int = 64, std_max: float = 3.0) -> bool:
    """这张图是不是"近空白"（整块纯色/白底）→ 用来**跳过 VLM 调用**

    实测依据（麦肯锡 116 页、77 张图）：引擎会在正文页上标出 0.2~0.4% 页面的小区域当
    figure，裁出来**整块纯白**（64×64 灰度标准差 0.0，灰度范围 255~255）——
    这样的区域有 **42 个**，占这张文档全部图元素的 55%。它们每一个都要花一次 VLM 调用，
    换回一段「完全空白的白色图片…」进库，是纯噪声（用户实测就看到了这句）。
    真正的图标准差都在 20 以上，阈值取 3 有 7 倍余量，不会误伤。
    """
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(image_bytes)).convert("L")
        px = list(im.resize((size, size)).getdata())
    except Exception:                                        # noqa: BLE001
        return False                       # 读不出来就当"不是空白"，照旧送 VLM
    if not px:
        return False
    mean = sum(px) / len(px)
    var = sum((v - mean) ** 2 for v in px) / len(px)
    return (var ** 0.5) < std_max


@StepRegistry.register("vlm_caption")
class VLMCaptionStep(PipelineStep):
    """
    图片描述生成：IMAGE 元素 → 自然语言描述（写入 raw_data["vlm_caption"]）。

    视觉模型独立成段（config.vlm），**不再借 config.llm**：LLM 是文本模型，把带图
    消息发给它只有两种结局 —— 服务端拒绝（降级成"只剩 OCR 文本"）或服务端收下却
    忽略图片（产出一段与图无关的"描述"入库）。后者是静默的错误数据，比前者更坏，
    所以必须走真正的 VLM 段。

    三级降级，每一级都留痕（→ ctx.warnings → 质量报告）：
      1. vlm 段未配置 / 未探测通过 → 不发请求 + warning「图片理解已跳过」
      2. 调用异常（超时/4xx/空响应）→ warning「图片描述失败」，保留 OCR 文本
      3. 没有 image_bytes 的元素  → 不进本步（内容由 OCR/题注承担）
    仍受 pipeline.enable_vlm 开关控制。
    """

    async def execute(self, ctx: IngestContext) -> None:
        if not ctx.parsed or not ctx.services.config.pipeline.enable_vlm:
            return
        images = [e for e in ctx.parsed.elements
                  if e.content_type == ContentType.IMAGE and
                  e.raw_data.get("image_bytes")]
        if not images:
            return
        # ① 近空白图直接跳过：纯白/纯色区域描述不出任何内容，只会把
        #    「完全空白的白色图片…」这类噪声写进检索库（实测占本篇图元素的 55%）。
        #    只在**没有别的文字**时才算"白送一次调用"：若该区域还有 OCR/引擎文字，
        #    元素照旧出块，只是不再问 VLM。
        blank = [e for e in images if figure_is_blank(e.raw_data["image_bytes"])]
        if blank:
            for e in blank:
                e.metadata = {**(e.metadata or {}), "vlm_skipped": "blank_region"}
            ctx.meta["vlm_blank_skipped"] = len(blank)
            images = [e for e in images if e not in blank]
            ctx.quality.issues.append(QualityIssue(
                stage="parse", severity="info", code="vlm_blank_region_skipped",
                message=f"{len(blank)} 处图区几乎是空白（纯色区域），未做图片理解"
                        "：这些区域描述不出内容，只会往检索库里写一条噪声",
                action="auto"))
        if not images:
            await ctx.report("chunking", 0.3,
                             f"图片理解已跳过（{len(blank)} 张为空白区域）")
            return

        # 门禁：没配 VLM 就不发请求（也避免把 mock 的假描述写进向量库）
        ready = getattr(ctx.services, "vlm_ready", None)
        blocked = ready() if ready is not None else None
        if blocked:
            ctx.add_warning(f"图片理解已跳过：{blocked}")
            await ctx.report("chunking", 0.3,
                             f"图片理解已跳过（{len(images)} 张）：{blocked[:60]}")
            return

        vlm = getattr(ctx.services, "vlm", None) or ctx.services.llm
        vlm_cfg = getattr(vlm, "config", None)
        sem = _Semaphore(getattr(vlm_cfg, "max_concurrency", 4) or 4)
        cleaned_total: list[int] = []       # 清理过提示词残留的图数（见下方 issue）

        async def describe(el: ParsedElement) -> None:
            b64 = base64.b64encode(el.raw_data["image_bytes"]).decode()
            try:
                async with sem:
                    caption = await vlm.generate([{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _VLM_PROMPT},
                            {"type": "image_url", "image_url":
                                {"url": f"data:image/png;base64,{b64}"}},
                        ]}],
                        # `task="vision"` 让"思考策略"把它当结构化短任务处理
                        # （auto 下关闭思考）：看图写描述不需要绕一圈思考，
                        # 关掉更快，也不会出现"思考吃满额度、描述为空"。
                        task="vision",
                        # 上限给够：万一关不掉思考（网关不支持），推理模型仍会先输出
                        # 思考，150 这种小值会让它满额截断、content 为空 —— 图片描述
                        # 就白做了（内容空但文档照样入库，属于最难发现的那类静默失败）
                        max_tokens=getattr(vlm_cfg, "max_tokens", None) or 512)
                # ② 去壳：提示词/思考残留（`**最终答案：**` 之类）不能进检索库。
                #    清完为空说明模型只吐了壳 → 当"没描述出来"，如实记 warning。
                cleaned, changed = clean_vlm_caption(caption or "")
                if changed:
                    cleaned_total.append(1)
                if not cleaned:
                    ctx.add_warning("图片描述为空（模型只回了提示词残留），"
                                    "该图按无描述入库")
                    return
                el.raw_data["vlm_caption"] = cleaned[:500]
                if changed:
                    el.metadata = {**(el.metadata or {}),
                                   "vlm_caption_cleaned": True}
            except Exception as e:
                # 记 warning 而不是只写 debug：否则"图没被理解"与"图本来就没内容"
                # 在质量报告里无法区分（原实现就是这个盲区）
                ctx.add_warning(f"图片描述失败：{str(e)[:120]}")
                log.warning("vlm_caption_failed", doc_id=ctx.doc.doc_id,
                            error=f"{type(e).__name__}: {e}"[:200])

        import asyncio
        await asyncio.gather(*(describe(el) for el in images),
                             return_exceptions=True)
        done = sum(1 for e in images if e.raw_data.get("vlm_caption"))
        await ctx.report("chunking", 0.3,
                         f"图片描述完成：{done}/{len(images)}")
        if cleaned_total:
            # 让"模型不老实"这件事在质量报告里可见（换模型/换提示词后能立刻看到变化）
            ctx.quality.issues.append(QualityIssue(
                stage="parse", severity="info", code="vlm_caption_cleaned",
                message=f"{len(cleaned_total)} 条图片描述清掉了提示词残留"
                        "（如 `**最终答案：**`）后才入库",
                action="auto"))


@StepRegistry.register("table_extract")
class TableExtractStep(PipelineStep):
    """
    表格原始数据收集 + 表格块正文改写：TABLE 元素 → TableData 列表（供 MySQL 精确查询）

    两种来源：
      - pdfplumber/Excel：raw_data 里已带 headers/rows；
      - 版面引擎：给的是 HTML（raw_data["table_html"]），这里解析出行列结构。
        不解析的话"引擎识别出的表格"仍然进不了结构化查询（等价于没接引擎）。

    两件事在这里一次做完：
      ① 把行列结构收进 `ctx.tables`，并在**元素上写下它在列表里的下标**
         （`raw_data["table_data_index"]`）——ChunkStep 按这个下标回填 chunk_id。
         原实现按"第几个 TABLE 元素"顺序配对，任何一个表没产出块就会整体错位、
         共用上一张表的 chunk_id（实测 8 张表只有 7 个不同 chunk_id）。
      ② 把表格块的**正文**从原始 HTML 改写成自然语言行文本（`第N行：列=值；…`）。
         原实现直接把 `<html><body><table>…` 当正文入库：实测 7 个表格块 2701 字符里
         1730 字符是标签（64%），"某表某列是多少"这类问法在 ES/向量库里根本没有可
         命中的自然语言文本（结构化行只在 MySQL，而检索路不查 table_data）。
         原始 HTML 保留在 `raw_data["table_html"]` 里，排查与二次解析都不丢。
    """

    async def execute(self, ctx: IngestContext) -> None:
        if not ctx.parsed:
            return
        idx = 0
        for el in ctx.parsed.elements:
            if el.content_type != ContentType.TABLE:
                continue
            rd = el.raw_data or {}
            headers = [str(h) for h in (rd.get("headers") or [])]
            rows = [[str(c) for c in row] for row in (rd.get("rows") or [])]
            if not rows and rd.get("table_html"):
                headers, rows = _html_table_to_rows(str(rd["table_html"]))
            if not rows:
                continue
            idx += 1
            # 正文改写：HTML（引擎产线）与"列=值"式拼接（pdfplumber/docx）统一成
            # 自然语言行文本 `第N行：列=值；…`。
            # **跳过已经是该格式的**：Excel 分支按 200 行切片发射，行号是**表内绝对
            # 行号**（metadata.row_range），重写会把它打回 1 并丢掉切片语义。
            current = (el.text or "").strip()
            already_nl = bool(el.metadata.get("row_range")) \
                or bool(re.match(r"^第\d+行：", current))
            natural = table_to_natural_text(headers, rows)
            if natural and not already_nl:
                if "<t" in current.lower():
                    rd.setdefault("table_html", current)   # 原始 HTML 留档
                el.text = natural
            ctx.tables.append(TableData(
                chunk_id="",                       # ChunkStep 按 table_data_index 回填
                doc_id=ctx.doc.doc_id,
                tenant_id=ctx.doc.tenant_id,
                table_index=rd.get("table_index", idx),
                page_num=el.page_num,
                headers=headers,
                row_data=rows,
                row_count=rd.get("row_count", len(rows)),
                numeric_stats=rd.get("numeric_stats") or {}))
            # 身份标记：ChunkStep 靠它把块 id 回填到**这一次 append 进去的那一行**
            el.raw_data["table_data_index"] = len(ctx.tables) - 1


def table_to_natural_text(headers: list[str], rows: list[list[str]],
                          max_rows: int = 200) -> str:
    """表格行列 → 自然语言行文本（`第N行：列=值；列=值`）

    与 Excel 分支（`doc_parser._emit_sheet`）用同一套格式：同一份知识库里"表格长什么
    样"应当只有一种读法，检索命中的措辞才不会因来源格式而异。
    """
    lines: list[str] = []
    for rn, row in enumerate(rows[:max_rows], start=1):
        pairs = [f"{h}={v}" for h, v in zip(headers, row)
                 if str(h).strip() and str(v).strip()]
        if pairs:
            lines.append(f"第{rn}行：" + "；".join(pairs))
    if len(rows) > max_rows:
        lines.append(f"（其余 {len(rows) - max_rows} 行略）")
    return "\n".join(lines)


def _html_table_to_rows(html: str) -> tuple[list[str], list[list[str]]]:
    """HTML 表格 → (headers, rows)；解析失败返回空（不抛，表格结构拿不到不该中断入库）

    **colspan 与 rowspan 都要展开**，否则后续列整体错位 —— 而错位是**静默**的：
    行列都还在、行数也对，只是每一行的"列=值"对应关系错了。实测麦肯锡 p87 那张表
    的第一格就是 `<td rowspan="19">医院…</td>`（跨 19 行），原实现只展开 colspan，
    于是第 2 行起整体左移一列，"哪一年对应哪个数值"全错，而质量报告里看不出异常。
    """
    if not html or "<t" not in html.lower():
        return [], []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        trs = soup.find_all("tr")
        grid: list[list[str]] = []
        # 跨行占位：pending[(row_idx, col_idx)] = 文本（还在往下延伸的 rowspan）
        pending: dict[tuple[int, int], str] = {}
        for row_idx, tr in enumerate(trs):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            row: list[str] = []
            col = 0
            # 先把上一行 rowspan 留下来的格子补上
            while (row_idx, col) in pending:
                row.append(pending.pop((row_idx, col)))
                col += 1
            for cell in cells:
                while (row_idx, col) in pending:      # 被跨行格挡住的位置
                    row.append(pending.pop((row_idx, col)))
                    col += 1
                text = cell.get_text(" ", strip=True)
                try:
                    cspan = max(1, int(cell.get("colspan") or 1))
                except (TypeError, ValueError):
                    cspan = 1
                try:
                    rspan = max(1, int(cell.get("rowspan") or 1))
                except (TypeError, ValueError):
                    rspan = 1
                for k in range(cspan):
                    row.append(text if k == 0 else "")
                    for r in range(1, rspan):        # 往下逐行占位
                        pending[(row_idx + r, col + k)] = "" if k else text
                col += cspan
            # 收尾：本行右侧可能还有 pending（上一行的跨行格比本行单元格宽）
            while (row_idx, col) in pending:
                row.append(pending.pop((row_idx, col)))
                col += 1
            grid.append(row)
        if len(grid) < 2:
            return [], []
        width = max(len(r) for r in grid)
        grid = [r + [""] * (width - len(r)) for r in grid]
        return grid[0], grid[1:]
    except Exception:
        return [], []


# ── 工具 ───────────────────────────────────────────────

def _element_bbox(el: ParsedElement):
    """元素 bbox：优先字段，其次 metadata（历史解析器只往 metadata 里写）"""
    b = getattr(el, "bbox", None)
    if b and len(b) >= 4:
        return b
    b = (el.metadata or {}).get("bbox")
    return b if b and len(b) >= 4 else None


def _surface_doc_parse_notes(ctx: IngestContext, client, label: str) -> None:
    """把 doc_parse 客户端记下的"必须让用户看见"的结论转成质量报告项

    典型是**页数被服务端截断**：服务端 `max_num_input_imgs` 小于一批的页数时，它
    返回 HTTP 200 但只处理前几页 —— 少掉的内容在入库侧完全看不出来（页数在界面上
    看起来就是这份文档本来的页数）。客户端已按更小批次重试并把结论记在 `notes`，
    这里落成 warning + issue（warning 会进质量报告与任务列表，用户能看见）。
    """
    for note in list(getattr(client, "notes", []) or []):
        ctx.add_warning(f"{label}：{note}")
        ctx.quality.issues.append(QualityIssue(
            stage="parse", severity="medium", code="doc_parse_page_truncated",
            message=f"{label}：{note}", action="warn"))


def _surface_figure_notes(ctx: IngestContext) -> None:
    """图区裁图结果进质量报告（裁出来多少 / 兜底多少 / 裁不出来多少）

    数字同时写进 `quality.figure_stats`（详情页的「图表」一行与图区说明要读它）——
    只记 issue 的话，页面上想显示"整页图 N 张""空白跳过 M 处"就得去解析 issue 文案。
    """
    md = ((ctx.parsed.metadata if ctx.parsed else None) or {})
    filled = int(md.get("figure_region_cropped") or 0)
    failed = int(md.get("figure_region_crop_failed") or 0)
    fallback = int(md.get("figure_page_fallback") or 0)
    angle_fixed = int(md.get("figure_angle_corrected") or 0)
    clamped = int(md.get("image_bbox_clamped") or 0)
    skipped_obj = int(md.get("image_bbox_skipped") or 0)
    page_scope = int(md.get("figure_page_scope") or 0)
    ctx.quality.figure_stats = {
        "cropped": filled, "crop_failed": failed, "page_fallback": fallback,
        "angle_corrected": angle_fixed, "bbox_clamped": clamped,
        "bbox_skipped": skipped_obj, "page_scope": page_scope,
    }
    if filled:
        ctx.quality.issues.append(QualityIssue(
            stage="parse", severity="info", code="figure_region_cropped",
            message=f"按版面区域裁出 {filled} 张图（引擎给了框、PDF 里没有对应"
                    "图像对象：矢量图、出血被夹过的位图、扫描整页图都算）",
            action="auto"))
    if angle_fixed:
        # 引擎的文档方向分类把页面转正过：不说清楚的话，别人看到"某些页的框跟
        # 别的页不同源"会以为是算错了（麦肯锡那篇 116 页里只有 2 页）
        ctx.quality.issues.append(QualityIssue(
            stage="parse", severity="info", code="figure_angle_corrected",
            message=f"{angle_fixed} 处图区按引擎的旋转角（doc_preprocessor_res."
                    "angle）逆变换后裁出（引擎检测前把栅格转正过，不逆变换就会"
                    "超出页面而整张图丢失）", action="auto"))
    if page_scope:
        ctx.quality.issues.append(QualityIssue(
            stage="parse", severity="info", code="figure_page_scope",
            message=f"{page_scope} 处图区的粒度是**整页**（扫描页/整页照片页）：这些"
                    "描述含整页内容，入库时已标成「（整页图）」，不是单张插图",
            action="auto"))
    if fallback:
        # 说清楚后果：图有了语义描述，但**粒度是整页**（"哪张图"的定位精度下降）
        ctx.add_warning(f"{fallback} 处图区坐标换算不可用，已退回整页渲染做图片理解"
                        "（描述粒度是整页，不是那张图的实际范围）")
    if clamped:
        ctx.quality.issues.append(QualityIssue(
            stage="parse", severity="info", code="image_bbox_clamped",
            message=f"{clamped} 个内嵌图对象的框超出页面边界（PDF 导出常见的出血位），"
                    "已夹到页内后抽取", action="auto"))
    skipped = int(md.get("image_bbox_skipped") or 0)
    if skipped:
        ctx.add_warning(f"{skipped} 个内嵌图对象无法抽取（框退化到没有面积）"
                        "：这些图不进图片理解")
    if failed:
        # 说清楚后果：这些图不会被 VLM 描述，即图上内容不可检索
        ctx.add_warning(f"{failed} 处图区未裁出（坐标不可换算或无对应图像），"
                        "这些图不会被图片理解描述")


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
