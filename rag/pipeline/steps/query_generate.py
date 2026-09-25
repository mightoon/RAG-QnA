"""
查询步骤：生成与收尾（rag/pipeline/steps/query_generate.py）

- GenerateStep     流式生成（SSE token 推送 + 来源溯源）
- FaithfulnessStep 忠实度自评：低分触发二轮检索重生成，仍低加免责声明
- MemoryUpdateStep 三层记忆更新（短期/工作/话题链/实体槽位）
"""
from __future__ import annotations

import json
import re

from rag.models import (ChatMessage, EntitySlot, MessageRole,
                        SourceReference)
from rag.observability.logging import get_logger
from rag.pipeline.base import PipelineStep, StepRegistry
from rag.pipeline.context import QueryContext

log = get_logger("rag.steps.generate")

_NO_CONTEXT_ANSWER = "知识库中未找到与您问题相关的内容。请尝试换个问法，或确认相关文档已入库。"


@StepRegistry.register("generate")
class GenerateStep(PipelineStep):

    async def execute(self, ctx: QueryContext) -> None:
        s = ctx.services
        cfg = s.config.prompts
        plan = ctx.plan

        def _fit(text: str, budget: int) -> str:
            """按 token 预算裁剪文本（按近似字符比例截断）"""
            if not text or budget <= 0:
                return ""
            n = s.llm.count_tokens(text)
            if n <= budget:
                return text
            ratio = budget / max(1, n)
            return text[:max(64, int(len(text) * ratio))]

        # ── 上下文构建（父块正文优先：子块命中回取父块）────────
        context_budget = 4000
        context_blocks, sources = [], []
        if ctx.reranked:
            used = 0
            for i, c in enumerate(ctx.reranked, start=1):
                body = c.parent_content or c.text
                location = " · ".join(
                    x for x in [c.title, c.section_path,
                                f"第{c.page_num}页" if c.page_num else None,
                                c.figure_label] if x)
                block = f"[{i}] {location}\n{body}"
                per = min(1500, max(0, context_budget - used))
                block = _fit(block, per)
                if not block:
                    break
                used += s.llm.count_tokens(block)
                context_blocks.append(block)
                sources.append(SourceReference(
                    ref_id=str(i), chunk_id=c.chunk_id, doc_id=c.doc_id,
                    title=c.title, section=(c.section_path or "").split("/")[-1]
                    or None, section_path=c.section_path,
                    page_num=c.page_num, figure_label=c.figure_label,
                    figure_caption=c.figure_caption,
                    storage_url=c.storage_url, chunk_type=c.chunk_type))
        ctx.sources = sources
        # 原文预览 URL 出链（前 5 条，best-effort）
        if s.storage is not None:
            for src in sources[:5]:
                if src.storage_url and not src.preview_url:
                    try:
                        src.preview_url = await s.storage.preview_url(
                            src.storage_url)
                    except Exception:
                        pass

        # ── Prompt 组装（分段 token 预算）────────────────────
        system = cfg.system_prompt
        if cfg.enable_cot:
            system += "\n请先在内部推理，再给出简洁准确的最终答案。"
        profile_text = _fit(ctx.meta.get("user_profile", ""), 400)
        history_arc = ctx.meta.get("related_history") or []
        memory_parts = []
        if ctx.session.working.summary:
            memory_parts.append(
                f"对话背景摘要：{_fit(ctx.session.working.summary, 400)}")
        if history_arc:
            memory_parts.append("相关历史会话：" + "；".join(
                _fit(h, 300) for h in history_arc[:2]))
        for m in ctx.session.short_term[-2:]:
            role = "用户" if m.role == MessageRole.USER else "助手"
            memory_parts.append(f"{role}: {_fit(m.content, 200)}")
        memory_text = "\n".join(memory_parts)

        user_content = ""
        if profile_text:
            user_content += f"—— 用户画像 ——\n{profile_text}\n\n"
        if memory_text:
            user_content += f"—— 对话历史 ——\n{memory_text}\n\n"
        if context_blocks:
            user_content += ("—— 参考资料（引用时标注 [编号]）——\n"
                             + "\n\n".join(context_blocks) + "\n\n")
        else:
            user_content += "—— 参考资料 ——\n（无相关检索结果）\n\n"
        user_content += f"—— 当前问题 ——\n{ctx.question}"

        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user_content}]

        # ── 流式生成 ──────────────────────────────────────
        answer_parts: list[str] = []
        try:
            async for token in s.llm.stream_generate(
                    messages, task="chat",
                    temperature=cfg.temperature,
                    max_tokens=cfg.max_answer_tokens):
                answer_parts.append(token)
                await ctx.emit({"type": "token", "text": token})
        except Exception:
            if not answer_parts:
                # 流式失败 → 非流式兜底
                if not ctx.reranked:
                    ctx.answer = _NO_CONTEXT_ANSWER
                    await ctx.emit({"type": "token", "text": ctx.answer})
                else:
                    raise
        ctx.answer = "".join(answer_parts) or ctx.answer

        # ── 消息与来源事件 ────────────────────────────────
        ctx.user_message = ChatMessage(
            session_id=ctx.session.session_id, role=MessageRole.USER,
            content=ctx.question)
        ctx.assistant_message = ChatMessage(
            session_id=ctx.session.session_id, role=MessageRole.ASSISTANT,
            content=ctx.answer, sources=sources,
            retrieval_paths={p: len(c) for p, c in ctx.candidates.items()})
        await ctx.emit({"type": "sources", "sources": [
            s_ref.model_dump(mode="json") for s_ref in sources]})
        ctx.tick("generated")


@StepRegistry.register("faithfulness")
class FaithfulnessStep(PipelineStep):
    """忠实度自评：低于阈值触发二轮检索重生成（一次），仍低追加声明"""

    _DISCLAIMER = "\n\n（提示：以上回答基于检索内容生成，建议核对原始文档。）"

    async def execute(self, ctx: QueryContext) -> None:
        s = ctx.services
        cfg = s.config.pipeline
        if not cfg.enable_faithfulness:
            return
        if not ctx.answer or not ctx.reranked:
            return
        threshold = cfg.faithfulness_threshold
        score = await self._evaluate(ctx)
        ctx.meta["faithfulness"] = score
        await ctx.emit({"type": "meta", "faithfulness": score})
        if score >= threshold:
            return
        # ── 二轮：放宽检索重生成一次；仍低于阈值 → 追加免责声明 ──
        log.info("faithfulness_low", score=score, retry=True)
        try:
            await self._second_round(ctx)
            score2 = await self._evaluate(ctx)
            ctx.meta["faithfulness"] = score2
            await ctx.emit({"type": "meta", "faithfulness": score2})
            if score2 >= threshold:
                return
        except Exception as e:
            log.warning("second_round_failed", error=str(e))
        if not ctx.answer.endswith(self._DISCLAIMER):
            ctx.answer += self._DISCLAIMER
            if ctx.assistant_message:
                ctx.assistant_message.content = ctx.answer
            await ctx.emit({"type": "token", "text": self._DISCLAIMER})

    async def _evaluate(self, ctx: QueryContext) -> float:
        context = "\n".join(c.text[:800] for c in ctx.reranked[:5])
        try:
            out = await ctx.services.llm.generate([{
                "role": "user",
                "content": "评估回答对给定资料的忠实度，只输出 0 到 1 的小数"
                           "（1=完全有据可依，0=完全无据）。\n\n"
                           f"资料：\n{context[:3000]}\n\n回答：\n{ctx.answer[:2000]}"}],
                task="rewrite", temperature=0.0,
                # 10 这个值是按"非推理模型只需要吐一个小数"估的：推理模型会先把
                # 额度花在思考上，正文为空 → 这里永远拿到 None → 忠实度恒为 1.0，
                # 自评/二轮检索静默失效。512 对"输出一个小数"足够，且适配器在
                # 识别到推理端点后会再加一份思考额度。
                max_tokens=512)
            m = re.search(r"[01](?:\.\d+)?", out)
            return float(m.group(0)) if m else 1.0
        except Exception:
            return 1.0                      # 评估失败不阻塞

    async def _second_round(self, ctx: QueryContext) -> None:
        """放宽 top_k 二轮检索 + 重生成（非流式，一次性替换答案）"""
        s = ctx.services
        plan = ctx.plan
        query = plan.semantic_query or plan.standalone_query
        # 向量空间不一致时这一路直接不查（连向量都不必算）：多召回一批噪声
        # 只会让"放宽检索"这一步把答案带偏，而这正是二轮检索的目的
        if not await s.vector_read_ok("default"):
            log.warning("second_round_vector_skipped_space",
                        reason=s.vector_space_reason()[:200])
            return
        query_vec = await s.embedding.embed_query(query)
        hits = await s.vector.search("default", query_vec,
                                     top_k=s.config.retrieval.vector_top_k * 2,
                                     filter={"tenant_id": ctx.user.tenant_id})
        extra = [h for h in hits
                 if h.chunk_id not in {c.chunk_id for c in ctx.reranked}]
        if not extra:
            return
        context = "\n".join(
            f"[{i}] {c.text[:1200]}"
            for i, c in enumerate((ctx.reranked + extra)[:8]))
        pcfg = s.config.prompts
        out = await s.llm.generate([
            {"role": "system", "content": pcfg.system_prompt},
            {"role": "user",
             "content": f"参考资料：\n{context}\n\n问题：{ctx.question}"}],
            task="chat", temperature=pcfg.temperature,
            max_tokens=pcfg.max_answer_tokens)
        if out.strip():
            ctx.answer = out.strip()
            if ctx.assistant_message:
                ctx.assistant_message.content = ctx.answer


@StepRegistry.register("memory_update")
class MemoryUpdateStep(PipelineStep):

    async def execute(self, ctx: QueryContext) -> None:
        s = ctx.services
        mem = s.memory_service
        session_id = ctx.session.session_id
        # 短期记忆
        if ctx.user_message:
            await mem.append_message(session_id, ctx.user_message)
        if ctx.assistant_message:
            await mem.append_message(session_id, ctx.assistant_message)
        # 工作记忆：滚动摘要
        turn = f"用户：{ctx.question}\n助手：{ctx.answer[:400]}"
        await mem.compress_working(session_id, turn)
        # 话题链 + 实体槽位
        st = await mem.load_session(session_id)
        if st is not None:
            w = st.working
            topic = (ctx.plan.semantic_query or ctx.question)[:64]
            if ctx.plan.topic_switched:
                w.topic_chain = [topic]
            else:
                w.topic_chain = (w.topic_chain + [topic])[-10:]
            if ctx.meta.get("topic_vector"):
                w.topic_vector = ctx.meta["topic_vector"]
            if ctx.plan:
                known = {slot.name for slot in w.entity_slots}
                for e in ctx.plan.entities[:5]:
                    if e.original not in known:
                        w.entity_slots.append(EntitySlot(
                            name=e.original,
                            value=e.canonical or e.original,
                            slot_type="entity", confirmed=e.linked))
                w.entity_slots = w.entity_slots[-20:]
            w.turn_count += 1
            await mem.save_session(st)
        # 终止事件（API 层 drain 收尾）
        await ctx.emit({"type": "done",
                        "timings": ctx.timings,
                        "meta": {k: v for k, v in ctx.meta.items()
                                 if isinstance(v, (str, int, float, bool))}})
        try:
            from rag.observability.metrics import metrics
            intent = (ctx.plan.intent.value if ctx.plan and
                      getattr(ctx.plan, "intent", None) else "unknown")
            metrics.observe_query(str(intent), "ok")
        except Exception:
            pass
