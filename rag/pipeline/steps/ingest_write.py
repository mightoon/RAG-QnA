"""
入库步骤：增强与写入（rag/pipeline/steps/ingest_write.py）

- EnrichStep   LLM 批量生成摘要/关键词/实体（失败降级正则提取）
- EmbedStep    子块向量化（bge-m3）
- WriteStep    两阶段写入：MySQL（必须）→ ES → Milvus → Graph，
               检查点逐库打钩，断点续写，单库失败降级不中断
- VerifyStep   入库后抽样验证（各库可检索性）
- FinalizeStep 状态落库 + 质量报告 + 通知
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime

from rag.models import (ChunkMeta, IngestStatus, QualityIssue)
from rag.observability.logging import get_logger
from rag.pipeline.base import PipelineStep, StepRegistry
from rag.pipeline.context import IngestContext

log = get_logger("rag.steps.write")

_ENRICH_PROMPT = """分析以下文本块，输出 JSON（不要其他内容）：
{{"summary": "不超过80字的摘要", "keywords": ["3-5个关键词/型号/专有名词"], "entities": ["文中出现的实体名"]}}

要求：三个字段都要有；summary 必须是一句非空的中文概括（描述这段讲了什么，
不要照抄原文整句），keywords 不足 3 个时给出你能确定的即可。

文本：
{text}"""

# 降级关键词要滤掉的英文停用词：它们在 ES 的 `keywords` 字段（keyword 精确匹配）
# 里只会制造噪声 —— 实测降级结果长这样：['Introduction','powerful','tools','are','often']
_EN_STOPWORDS = frozenset("""
a an the and or but if then than that this these those is are was were be been being
of to in on at by for with from as it its it's his her their our your my we you they
he she them us i not no nor can could should would may might must will shall do does
did done have has had how what when where which who whom why all any both each few
more most other some such only own same so too very just don now also into over under
about after before while during between within without through out up down off again
often various several many using used use also first second third new based show shows
shown presented present given well make makes made take takes get gets like need needs
see seen every less least much whose whether because since however therefore thus
e.g i.e etc per via among along across within toward towards upon
""".split())

# 型号/编号（bge-m3、qwen3、GPT-4、A.1.2 之类）与专有名词（GenerativeUI、DeepSeek）：
# 降级关键词优先取这两类 —— `keywords` 字段的用途就是"专有名词/型号精确匹配"
_MODEL_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z\-_]*\d[A-Za-z0-9\-_.]*\b")
_PROPER_TOKEN_RE = re.compile(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z0-9\-_]*\b")
_GENERIC_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-_]{3,}")

# 增强调用的**预算阶梯**基础两级：`(max_tokens, 送入的文本上限)`，逐级升级，成功后停止。
#
# 为什么不是"同预算重试"：失败原因几乎都是"推理模型的思考把额度吃光、答案还没开始写"
# （实测某块思考 1383 字符 × completion 全花在 reasoning 上、finish_reason=length），
# 同样的预算再试一次只会再被吃光一次。
#
# 实测（真实模型 deepseek-flash，逐级验证）：
#   · 512  → 6 个块里 2 个被思考吃满（empty）；
#   · 2048 → 又救回 3 个；仍有 1 个块思考写到 4925 字符、连 2048 都不够；
#   · 4096 + 全文 → 该块成功（思考 7000+ 字符，但答案写出来了）。
# 也试过"截短输入"这条捷径：2048 + 截到 600 字**仍然失败**（那次思考 7178 字符，
# 说明思考长度与输入长度并不相关），所以最后一级是"更大预算 + 适度截短"。
_ENRICH_LADDER_BASE: tuple[tuple[int, int], ...] = ((512, 1500), (2048, 1500))
# 从第二次起追加的约束（第一次保持原样，避免平白改变成功路径的 prompt）
_ENRICH_STRICT_SUFFIX = ("\n\n注意：直接输出那个 JSON 对象本身，"
                         "不要输出任何解释、分析或思考过程。")


def enrich_ladder(ceiling: int | None) -> list[tuple[int, int]]:
    """按**配置里的 llm.max_tokens**（ceiling）展开预算阶梯

    关键点：**最后一级用配置值**。之前这里写死了 512/2048/4096，而配置里明明是
    65536 —— 用户配的上限被代码里的硬编码小值挡在外面，"调大配置就能解决"根本不成立。
    推理模型写完思考就停（finish_reason=stop），把上限放开到配置值不会让正常块多烧
    token，只会让"思考特别长"的块有机会把答案写出来。

    ceiling 比基础两级还小时，尊重配置（不越界），阶梯自动收缩。
    """
    cap = int(ceiling or 0)
    rungs = [b for b, _ in _ENRICH_LADDER_BASE]
    if cap:
        rungs = [min(b, cap) for b in rungs]
        if cap > (rungs[-1] if rungs else 0):
            rungs.append(cap)
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    for i, b in enumerate(rungs):
        if b in seen or b <= 0:
            continue
        seen.add(b)
        # 最后一级把送入文本截到 800 字：给它最大预算的同时少喂点，双保险
        out.append((b, 800 if i == len(rungs) - 1 and len(rungs) > 1 else 1500))
    return out or [(512, 1500)]


@StepRegistry.register("enrich")
class EnrichStep(PipelineStep):
    """LLM 批量增强：摘要（ES 高权重字段）/ 关键词 / 实体"""

    async def execute(self, ctx: IngestContext) -> None:
        cfg = ctx.services.config.pipeline
        if not ctx.chunks:
            return
        llm = ctx.services.llm
        n = len(ctx.chunks)
        ctx.summaries = [None] * n
        ctx.keywords = [[] for _ in range(n)]
        all_entities: dict[str, dict] = {}

        sem = asyncio.Semaphore(ctx.services.config.llm.max_concurrency)
        # 预算阶梯按**配置里的上限**展开（最后一级就是 llm.max_tokens）：
        # 配置说"我有 65536 的空间"，代码就不该用小硬编码值把它挡在外面
        ladder = enrich_ladder(getattr(ctx.services.config.llm, "max_tokens", None))
        # 增强失败计数：这些块会退化成"正则关键词 + 无摘要"，必须让它在质量报告里
        # 看得见 —— 原实现 `except Exception:` 静默降级，只在末尾给一句笼统 warning，
        # 于是"为什么 45 个块只有 16 个有摘要"在日志与报告里都查不到原因。
        failed: list[tuple[str, str]] = []
        empty_reply = 0
        no_summary: list[str] = []

        async def enrich_one(i: int) -> None:
            nonlocal empty_reply
            text = ctx.chunk_texts[i]
            if ctx.chunks[i].is_parent or len(text) < 30:
                ctx.keywords[i] = self._regex_keywords(text)
                return
            prompt_base = _ENRICH_PROMPT
            data: dict = {}
            last_reply = ""
            try:
                # 预算阶梯：失败一次就升级预算（必要时适度截短输入），成功后立即停。
                # 见 _ENRICH_LADDER 的实测依据。
                for attempt, (budget, cut) in enumerate(ladder):
                    prompt = prompt_base.format(text=text[:cut])
                    if attempt:
                        prompt += _ENRICH_STRICT_SUFFIX
                    async with sem:
                        out = await llm.generate(
                            [{"role": "user", "content": prompt}],
                            task="summary", temperature=0.1,
                            max_tokens=budget,
                            # JSON 模式：让服务端保证 content 是合法 JSON。
                            # 靠 prompt 求模型吐 JSON 在推理模型上不可靠（它会先
                            # 思考一大段），这一条能直接消掉 JSONDecodeError。
                            response_format={"type": "json_object"})
                    last_reply = out or ""
                    if not last_reply.strip():
                        continue
                    data = json.loads(self._extract_json(last_reply))
                    if data.get("summary") or data.get("keywords"):
                        break
                    data = {}
                if not last_reply.strip():
                    # 200 但没有正文：思考吃满了预算。单独计数，便于与真故障区分
                    empty_reply += 1
                    ctx.keywords[i] = self._regex_keywords(text)
                    return
                if not data:
                    # 有正文但不是可用的 JSON（模型把思考/解释写进了 content）
                    no_summary.append(f"{ctx.chunks[i].chunk_id}: {last_reply[:160]}")
                    ctx.keywords[i] = self._regex_keywords(text)
                    return
                ctx.summaries[i] = (data.get("summary") or "")[:200] or None
                ctx.keywords[i] = self._clean_keywords(data.get("keywords"))
                if not ctx.summaries[i]:
                    # 合法 JSON 但缺摘要字段：把原始回复留一段样本，
                    # 否则"摘要成功率只有三成"这件事在日志里查不到任何线索
                    no_summary.append(f"{ctx.chunks[i].chunk_id}: "
                                      f"{last_reply[:160]}")
                for e in (data.get("entities") or [])[:10]:
                    name = str(e).strip()
                    if 1 < len(name) <= 64:
                        all_entities[name] = {"name": name,
                                              "entity_type": "generic"}
            except Exception as e:
                failed.append((ctx.chunks[i].chunk_id,
                               f"{type(e).__name__}: {str(e)[:120]}"))
                ctx.keywords[i] = self._regex_keywords(text)

        await asyncio.gather(*(enrich_one(i) for i in range(n)),
                             return_exceptions=True)

        # 实体归一化（EntityLinker 规则阶段）+ 共现关系
        if cfg.enable_entities and all_entities:
            try:
                linked = await ctx.services.entity_linker.link_batch(
                    ctx.doc.tenant_id, list(all_entities.values()))
                ctx.entities = linked
            except Exception:
                ctx.entities = list(all_entities.values())
            ctx.relations = self._cooccurrence_relations(
                ctx.entities, ctx.chunk_texts)

        # 质量评分：摘要生成成功率
        ok = sum(1 for s in ctx.summaries if s)
        target = sum(1 for i in range(n) if not ctx.chunks[i].is_parent
                     and len(ctx.chunk_texts[i]) >= 30)
        if failed:
            log.warning("enrich_llm_failed", doc_id=ctx.doc.doc_id,
                        failed=len(failed), target=target,
                        sample=[f"{cid}: {err}" for cid, err in failed[:3]])
        if empty_reply:
            log.warning("enrich_llm_empty_reply", doc_id=ctx.doc.doc_id,
                        empty=empty_reply, target=target,
                        ladder=[b for b, _ in ladder],
                        ceiling=getattr(ctx.services.config.llm, "max_tokens", None),
                        model=getattr(ctx.services.config.llm, "model", ""),
                        hint="推理模型的思考把 max_tokens 吃光、正文为空："
                             "这些块退化为正则关键词且无摘要"
                             "（已升到配置上限仍失败：换非推理模型，"
                             "或把 llm.max_tokens 调得更大）")
        if no_summary:
            log.warning("enrich_llm_no_summary", doc_id=ctx.doc.doc_id,
                        count=len(no_summary), target=target,
                        sample=[s[:120] for s in no_summary[:2]],
                        hint="模型没有给出摘要（可能返回了思考/解释而非 JSON，"
                             "或 JSON 里少了 summary）：换模型或调 prompt，"
                             "重跑不会自行改善")
        if target and ok / target < 0.5:
            detail = (f"（LLM 调用失败 {len(failed)} 块、空回复 {empty_reply} 块、"
                      f"返回无摘要 {len(no_summary)} 块）"
                      if (failed or empty_reply or no_summary) else "")
            ctx.add_warning(f"摘要生成成功率低 ({ok}/{target}){detail}")
            # 进质量报告：只有 warning 时它只出现在任务备注里，"入库质量"这一栏
            # 看不出摘要/关键词大半缺失（而 ES 的摘要高权重检索正依赖它们）
            ctx.quality.issues.append(QualityIssue(
                stage="enrich", severity="medium",
                code="summary_generation_low",
                message=(f"摘要生成成功率低：{ok}/{target} 块有摘要；"
                         f"LLM 失败 {len(failed)}、空回复 {empty_reply}、"
                         f"返回无摘要 {len(no_summary)}"),
                action="degrade"))
        await ctx.report("embedding", 0.5,
                         f"增强完成：{ok} 摘要 / {len(ctx.entities)} 实体")

    @staticmethod
    def _extract_json(text: str) -> str:
        """从模型回复里取出第一个**能解析**的 JSON 对象

        原实现 `re.search(r"\\{.*\\}", text, re.DOTALL)` 会贪婪地吞到最后一个
        `}`：模型多输出一个对象、或在 JSON 后面又跟了说明文字时，`json.loads`
        直接抛 JSONDecodeError，这一块的摘要/关键词就静默降级成正则结果
        （实测日志 `enrich_llm_failed ... JSONDecodeError: Extra data: line 3
        column 1` 就是这么来的，46 块里少掉的那批摘要有一部分属于此类）。

        改为花括号配对扫描：逐个候选试解析，返回第一个含预期字段的对象。
        顺序是「整段 → 各对象（正序）→ 各对象（倒序）」—— 推理模型常把"最终
        答案"放在最后，正序失败时倒序常能命中。
        """
        raw = (text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
        candidates: list[str] = []
        depth, start = 0, -1
        for i, ch in enumerate(raw):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(raw[start:i + 1])
                    start = -1
        for cand in [raw, *candidates, *reversed(candidates)]:
            try:
                data = json.loads(cand)
            except Exception:                      # noqa: BLE001
                continue
            if isinstance(data, dict) and any(
                    k in data for k in ("summary", "keywords", "entities")):
                return cand
        # 一个都解析不出来 → **返回空对象**，不要返回"破的那个候选"：
        # 后者会让调用方的 json.loads 抛 JSONDecodeError，把"模型没给 JSON"
        # 误报成"解析异常"（实测日志里就是这么错的）。
        return "{}"

    @staticmethod
    def _regex_keywords(text: str) -> list[str]:
        """降级关键词提取：型号/编号 → 专有名词 → 中文 2-gram → 长词（滤停用词）

        原实现直接取前 5 个英文词，实测降级结果是
        `['Introduction','powerful','tools','are','often']` —— 写进 ES 的
        `keywords`（keyword 类型、走精确匹配）等于往精确路里塞噪声。
        现在按"这个字段本来要存什么"（专有名词/型号）排序取词。
        """
        out: list[str] = []

        def add(w: str) -> bool:
            if w and w not in out:
                out.append(w)
            return len(out) >= 5

        for w in _MODEL_TOKEN_RE.findall(text):        # bge-m3 / qwen3 / GPT-4
            if add(w):
                return out
        for w in _PROPER_TOKEN_RE.findall(text):       # GenerativeUI / DeepSeek
            if w.lower() in _EN_STOPWORDS:
                continue
            if add(w):
                return out
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):   # 中文 2-gram
            for i in range(len(run) - 1):
                if add(run[i:i + 2]):
                    return out
        for w in _GENERIC_WORD_RE.findall(text):       # 兜底：长词、非停用词
            if w.lower() in _EN_STOPWORDS:
                continue
            if add(w):
                return out
        return out

    @staticmethod
    def _clean_keywords(raw: list) -> list[str]:
        """LLM 给的关键词也过一遍停用词：模型偶尔会回 "often/the" 这类词"""
        out: list[str] = []
        for k in raw or []:
            s = str(k).strip()[:32]
            if not s or s.lower() in _EN_STOPWORDS:
                continue
            if s not in out:
                out.append(s)
            if len(out) >= 5:
                break
        return out

    @staticmethod
    def _cooccurrence_relations(entities: list[dict],
                                texts: list[str]) -> list[dict]:
        """共现关系：同一 chunk 中出现的实体对（图谱 V1 规则阶段）"""
        if len(entities) < 2:
            return []
        names = [e["name"] for e in entities]
        relations = []
        for text in texts:
            present = [n for n in names if n in text]
            for i in range(len(present)):
                for j in range(i + 1, len(present)):
                    relations.append({"source": present[i],
                                      "target": present[j],
                                      "relation": "CO_OCCURS"})
                    if len(relations) >= 200:
                        return relations
        return relations


@StepRegistry.register("embed")
class EmbedStep(PipelineStep):
    """子块向量化（父块不参与 ANN 检索，不入向量库）"""

    async def execute(self, ctx: IngestContext) -> None:
        if not ctx.chunks or ctx.services.vector is None:
            return
        # 向量空间门禁：该集合里已有另一套向量空间的向量时**不写**（见
        # rag/vector_space.py）。必须在这里"有意跳过"而不是让适配器报错：
        # 抛出去会被记成写入失败 → 文档 PARTIAL、质量报告挂一条假故障。
        # 跳过之后 ctx.embeddings 为空，写阶段自然不碰向量库（cp.milvus 保持
        # False），verify 也会跳过 —— 这条链路上"没写"与"写失败"始终可区分。
        blocked = await ctx.services.vector_write_blocked(ctx.doc.collection)
        if blocked:
            ctx.meta["vector_space_blocked"] = blocked
            log.warning("embed_skipped_vector_space", doc_id=ctx.doc.doc_id,
                        collection=ctx.doc.collection, reason=blocked[:200])
            await ctx.report("embedding", 0.6, f"已跳过向量化：{blocked[:120]}")
            return
        # 收集所有非父块 (chunk_id, 参与向量化的文本)
        items = [(c.chunk_id, self._embed_text(ctx, i))
                 for i, c in enumerate(ctx.chunks) if not c.is_parent]
        batch = ctx.services.config.embedding.batch_size
        for lo in range(0, len(items), batch):
            sub = items[lo:lo + batch]
            try:
                vectors = await ctx.services.embedding.embed(
                    [t for _, t in sub])
                for (cid, _), vec in zip(sub, vectors):
                    ctx.embeddings[cid] = vec
            except Exception as e:
                raise RuntimeError(f"向量化失败: {e}") from e
        await ctx.report("embedding", 0.6,
                         f"向量化完成：{len(ctx.embeddings)} 块")

    @staticmethod
    def _embed_text(ctx: IngestContext, i: int) -> str:
        """子块参与向量化的文本：**关键词 + 摘要 + 正文**

        只嵌入正文时，"这段讲了什么 / 有没有提到降级策略"这类**抽象提问**命中不了
        它 —— 提问的措辞与正文不同构，而同构的那份文本（摘要）恰好没进向量。
        摘要由 enrich 步骤用 LLM 生成，本来就是"这段话的抽象说法"，把两者一起嵌入，
        抽象问法与具体词法问法就都落在这个向量附近。

        顺序把正文放最后：多数嵌入模型对文本尾部更敏感，正文才是主体；关键词/摘要
        是补充语境。

        拼法版本见 `rag.models.EMBED_TEXT_VERSION`（改动拼法**必须**同步改它：
        它进库指纹，否则同一集合里新旧拼法的向量并存且秒传判据看不出差异）。
        """
        text = ctx.chunk_texts[i]
        parts: list[str] = []
        kw = ctx.keywords[i] if i < len(ctx.keywords) else None
        if kw:
            parts.append("关键词：" + "、".join(kw))
        sm = ctx.summaries[i] if i < len(ctx.summaries) else None
        if sm:
            parts.append("摘要：" + sm)
        parts.append(text)
        return "\n".join(parts)


@StepRegistry.register("write")
class WriteStep(PipelineStep):
    """
    两阶段写入 + 检查点断点续写：
    阶段一（必须成功）：MySQL chunks_meta / table_data
    阶段二（尽力而为）：ES → Milvus → Graph，单库失败降级记 warning
    """

    @staticmethod
    def _doc_meta(ctx: IngestContext) -> dict:
        """写入 ES 的文档级字段（mapping 声明了、但 ChunkMeta 不携带的三项）

        `created_at` 必须是带时区的 ISO8601：ES 的 `date` 字段按
        `strict_date_optional_time` 解析，不带时区的一串会被当作 UTC 处理但容易
        因格式细节被拒；显式 "Z" 最稳。
        """
        d = ctx.doc
        created = getattr(d, "created_at", None) or datetime.utcnow()
        try:
            created_s = created.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:                          # 非 datetime 实现：不阻断写入
            created_s = ""
        return {"filename": d.filename, "file_type": d.file_type,
                "created_at": created_s}

    async def _prune_stale(self, ctx: IngestContext) -> list[str]:
        """算出"上一次入库留下的、本次没再产出的"旧块并从 MySQL 删掉

        背景：chunk_id = sha256(tenant:doc:seq:内容)，重跑（reingest / 改分块参数后
        重入库）只会**覆盖**同 id 的块。块数变少、顺序变化或某块内容变化时，旧块
        会永远留在三库里：检索命中得到、`chunks_meta` 里查不到、父块回补也接不上。

        顺序：**MySQL 写完立即算**（此时库里的集合 = 旧的 ∪ 新的，差集就是旧的），
        结果放 ctx.meta 供 ES/Milvus 在各自写成功后删同一批 id（避免"删了旧的、
        新的又没写进去"）。
        """
        s = ctx.services
        keep = {c.chunk_id for c in ctx.chunks}
        try:
            existing = set(await s.meta.list_chunk_ids(ctx.doc.doc_id))
        except Exception as e:                     # noqa: BLE001
            log.warning("prune_list_failed", doc_id=ctx.doc.doc_id,
                        error=f"{type(e).__name__}: {e}"[:200])
            return []
        stale = sorted(existing - keep)
        if not stale:
            return []
        try:
            removed = await s.meta.delete_chunks(stale)
        except Exception as e:                     # noqa: BLE001
            log.warning("prune_meta_failed", doc_id=ctx.doc.doc_id,
                        stale=len(stale), error=f"{type(e).__name__}: {e}"[:200])
            return []
        log.info("prune_stale_chunks", doc_id=ctx.doc.doc_id,
                 stale=len(stale), removed=removed,
                 keep=len(keep))
        ctx.add_warning(f"清理上次遗留的旧块 {len(stale)} 个（重跑未再产出）")
        ctx.meta["stale_chunk_ids"] = stale
        return stale

    async def _prune_store(self, ctx: IngestContext, store: str) -> None:
        """把 `_prune_stale` 算出的旧块 id 从检索库删掉（在各自写成功之后调用）

        失败只记 warning：旧块残留会让检索多召回几条过期内容，比"整篇文档判失败"
        轻得多；而这里抛异常会把已经写成功的库判成写失败（假故障）。
        """
        stale = list((ctx.meta or {}).get("stale_chunk_ids") or [])
        if not stale:
            return
        s = ctx.services
        try:
            if store == "es" and s.fulltext is not None:
                n = await s.fulltext.delete_by_ids(ctx.doc.collection, stale)
            elif store == "milvus" and s.vector is not None:
                n = await s.vector.delete_by_ids(ctx.doc.collection, stale)
            else:
                return
            log.info("prune_stale_store", store=store, doc_id=ctx.doc.doc_id,
                     stale=len(stale), removed=n)
        except Exception as e:                     # noqa: BLE001
            log.warning("prune_store_failed", store=store,
                        doc_id=ctx.doc.doc_id, stale=len(stale),
                        error=f"{type(e).__name__}: {e}"[:200])

    async def execute(self, ctx: IngestContext) -> None:
        s = ctx.services
        cp = ctx.task.checkpoint
        await ctx.report("writing", 0.65, "开始写入")

        # ── 阶段一：MySQL（元数据，必须）────────────────────
        if not cp.mysql:
            # 正文一并落 MySQL：供父子回补与一致性巡检修复取真文
            texts = {c.chunk_id: ctx.chunk_texts[i]
                     for i, c in enumerate(ctx.chunks)
                     if i < len(ctx.chunk_texts)}
            await s.meta.upsert_chunks(ctx.chunks, texts=texts)
            if ctx.tables:
                # **全部结构化表格都写**（原实现过滤 `if t.chunk_id`）：结构化行是
                # "精确数值查询"的数据源，不该因为正文块没生成（内容为空/被去重）而
                # 整行丢掉 —— 那种情况下 chunk_id 留空即可，行本身仍然有价值。
                await s.meta.upsert_table_data(ctx.tables)
            cp.mysql = True
            await s.meta.save_task(ctx.task)
            ctx.task.written_chunks = len(ctx.chunks)
            # 重跑（reingest / 手改参数后重入库）时清掉本次没再产出的旧块：
            # chunk_id 是确定性的，重跑只会**覆盖**同 id 的块；块数变少或顺序变化时
            # 剩下的旧块会永远留在三库里（检索命中得到、元数据却对不上）。
            await self._prune_stale(ctx)

        # ── 阶段二：ES / Milvus / Graph（可降级）────────────
        if not cp.es and s.fulltext is not None:
            try:
                await s.fulltext.upsert_chunks(
                    ctx.doc.collection, ctx.chunks, ctx.chunk_texts,
                    ctx.summaries, ctx.keywords, doc=self._doc_meta(ctx))
                cp.es = True
                await self._prune_store(ctx, "es")
            except Exception as e:
                ctx.add_warning(f"ES 写入失败: {e}")
                ctx.quality.issues.append(QualityIssue(
                    stage="post_write", severity="high", code="es_write_failed",
                    message=str(e)[:200], action="partial"))

        if not cp.milvus and s.vector is not None and ctx.embeddings:
            try:
                ids = list(ctx.embeddings.keys())
                id_set = set(ids)
                metas = []
                for c, text in zip(ctx.chunks, ctx.chunk_texts):
                    if c.chunk_id not in id_set:
                        continue
                    metas.append({
                        "doc_id": c.doc_id, "tenant_id": c.tenant_id,
                        "collection": c.collection, "chunk_type": c.chunk_type,
                        "text": text[:4000],
                        "title": ctx.doc.filename[:500],
                        "section_path": c.section_path or "",
                        "page_num": c.page_num or 0,
                        "figure_label": c.figure_label or "",
                        "figure_caption": c.figure_caption or "",
                        "storage_url": ctx.doc.storage_url or "",
                        "quality_score": c.quality_score,
                        "allowed_roles": c.allowed_roles or [],
                    })
                await s.vector.upsert(
                    ctx.doc.collection, ids,
                    [ctx.embeddings[i] for i in ids], metas)
                cp.milvus = True
                await self._prune_store(ctx, "milvus")
            except Exception as e:
                ctx.add_warning(f"Milvus 写入失败: {e}")
                ctx.quality.issues.append(QualityIssue(
                    stage="post_write", severity="high",
                    code="milvus_write_failed",
                    message=str(e)[:200], action="partial"))

        if not cp.graph and s.graph is not None and ctx.entities:
            try:
                await s.graph.upsert_entities(
                    ctx.doc.tenant_id, ctx.doc.doc_id, ctx.entities)
                if ctx.relations:
                    await s.graph.upsert_relations(
                        ctx.doc.tenant_id, ctx.doc.doc_id, ctx.relations)
                cp.graph = True
            except Exception as e:
                ctx.add_warning(f"图谱写入失败: {e}")

        await s.meta.save_task(ctx.task)
        await ctx.report("writing", 0.85, "写入完成")


@StepRegistry.register("verify")
class VerifyStep(PipelineStep):
    """入库后抽样验证：随机抽 chunk_id 核对 ES / Milvus 存在性"""

    async def execute(self, ctx: IngestContext) -> None:
        s = ctx.services
        sample_size = s.config.ingest.verify_sample_size
        child_ids = [c.chunk_id for c in ctx.chunks if not c.is_parent]
        if not child_ids:
            return
        sample = random.sample(child_ids, min(sample_size, len(child_ids)))
        sample_set = set(sample)

        if s.fulltext is not None and ctx.task.checkpoint.es:
            try:
                found = await s.fulltext.get_doc_chunk_ids(
                    ctx.doc.collection, ctx.doc.doc_id)
                missing = sample_set - found
                if missing:
                    ctx.add_warning(f"ES 抽样缺失 {len(missing)} 块")
            except Exception:
                pass
        if s.vector is not None and ctx.task.checkpoint.milvus:
            try:
                found = await s.vector.get_doc_chunk_ids(
                    ctx.doc.collection, ctx.doc.doc_id)
                missing = sample_set - found
                if missing:
                    ctx.add_warning(f"Milvus 抽样缺失 {len(missing)} 块")
            except Exception:
                pass


@StepRegistry.register("finalize")
class FinalizeStep(PipelineStep):
    """收尾：质量报告汇总 + 文档状态落库（done / partial）"""

    async def execute(self, ctx: IngestContext) -> None:
        s = ctx.services
        q = ctx.quality
        q.doc_id = ctx.doc.doc_id
        # Chunk 质量分布（基于 token 数、类型与质量分）
        for c in ctx.chunks:
            if c.is_parent:
                continue
            if c.token_count < 20 or c.quality_score < 0.3:
                q.low_quality += 1
            elif (c.chunk_type in ("table", "image_caption")
                  or c.token_count >= 100) and c.quality_score >= 0.6:
                q.high_quality += 1
            else:
                q.medium_quality += 1
        try:
            from rag.observability.metrics import metrics
            metrics.chunk_quality.labels(bucket="high").inc(q.high_quality)
            metrics.chunk_quality.labels(bucket="medium").inc(q.medium_quality)
            metrics.chunk_quality.labels(bucket="low").inc(q.low_quality)
        except Exception:
            pass
        # PARTIAL 的语义收窄：**只有"某个库没写成功"才算部分完成**（设计 §5.6）。
        # 原实现是 `degraded = bool(ctx.warnings)` —— 于是"标题与书签匹配率低""双栏
        # 重排""图片理解已跳过"这类**质量备注**也会把文档判成 PARTIAL，并让批次变成
        # partial_failed、触发一封失败通知。那是"全库写入成功却报部分完成"。
        # 质量备注走 issues（进质量报告），状态仍如实为 DONE。
        write_failed = any(i.code.endswith("_write_failed") for i in q.issues)
        status = IngestStatus.PARTIAL if write_failed else IngestStatus.DONE
        q.summary = (f"共 {len(ctx.chunks)} 块（高质 {q.high_quality} / "
                     f"中质 {q.medium_quality} / 低质 {q.low_quality}）"
                     + (f"；{q.doc_type} 型，OCR {q.used_ocr_pages} 页"
                        if q.doc_type else "")
                     + (f"；写入失败库 {sum(1 for i in q.issues if i.code.endswith('_write_failed'))}"
                        if write_failed else "")
                     + (f"；质量备注 {len(ctx.warnings)} 条"
                        if ctx.warnings else ""))

        report = q.model_dump(mode="json")
        # 解析/嵌入口径指纹：随质量报告一起落库，供**下次同文件重传**时判断
        # "引擎口径有没有变"（变了就不能 MD5 秒传，否则命中的是旧引擎产出的
        # chunk —— 索引压根没更新，用户却以为重新入库了）。
        # 见 rag/ingestion/coordinator.py 的 _ingest_fingerprint 与同处的比对逻辑。
        fp = (ctx.meta or {}).get("ingest_fingerprint")
        if fp:
            report["ingest_fingerprint"] = fp
        ctx.doc.quality_report = report
        ctx.doc.status = status
        ctx.doc.chunk_count = len(ctx.chunks)
        await s.meta.upsert_document(ctx.doc)
        ctx.task.quality_summary = {
            "high": q.high_quality, "medium": q.medium_quality,
            "low": q.low_quality, "warnings": ctx.warnings[:20],
            "ocr_avg_confidence": q.ocr_avg_confidence,
            "doc_type": q.doc_type, "used_ocr_pages": q.used_ocr_pages}
        ctx.task.status = status
        await s.meta.save_task(ctx.task)
        await ctx.report(status.value, 1.0, q.summary)
