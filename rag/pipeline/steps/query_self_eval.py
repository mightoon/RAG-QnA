"""
检索质量自评步骤（rag/pipeline/steps/query_self_eval.py）

位于 MergeStep 之后、RerankStep 之前（spec §8）。
流程：
  1. 取融合结果的代表性最高分（向量路原始余弦分）
  2. 若低于 self_eval_threshold 且迭代次数未达上限：
     - 按失败原因导向改写（LLM 生成更具体的改写 + HyDE 虚拟答案）
     - 二轮召回（向量用 HyDE 向量、BM25 用改写文本）
     - 加入候选后重新融合（保留更高分结果）
  3. 结果写入 meta（self_eval 轨迹），供审计事件与评测使用
"""
from __future__ import annotations

import json

from rag.models import RetrievedChunk
from rag.observability.logging import get_logger
from rag.pipeline.base import PipelineStep, StepRegistry
from rag.pipeline.context import QueryContext
from rag.pipeline.steps.query_retrieve import merge_candidates

log = get_logger(__name__)

_HYDE_PROMPT = (
    "你是一个检索改写专家。用户问题的首轮检索结果质量不足。"
    "请直接写出一段 100~200 字的“理想答案草稿”（不要求真实，"
    "用于向量相似检索：HyDE）；并用一行给出改写后的检索查询。\n"
    "输出 JSON：{\"hyde\": \"...\", \"rewrite\": \"...\"}\n\n"
    "问题：{q}"
)


@StepRegistry.register("self_eval")
class SelfEvalStep(PipelineStep):
    stop_on_error = False

    async def execute(self, ctx: QueryContext) -> None:
        s = ctx.services
        cfg = s.config.retrieval
        if not plan_or_none(ctx) or not ctx.merged:
            return
        threshold = cfg.self_eval_threshold
        max_iter = max(1, cfg.self_eval_max_iterations or 1)
        rounds = 0
        while rounds < max_iter:
            top = self._representative_score(ctx)
            if top >= threshold:
                break
            rounds += 1
            log.info("self_eval_low_score", round=rounds, top=top,
                     threshold=threshold)
            try:
                gained = await self._second_round(ctx)
            except Exception as e:
                log.warning("self_eval_round_failed", error=str(e))
                break
            if not gained:
                break
        if rounds:
            await merge_candidates(ctx)          # 重新融合
        ctx.meta["self_eval_rounds"] = rounds
        if rounds:
            await ctx.emit({"type": "meta", "stage": "self_eval",
                            "rounds": rounds})

    def _representative_score(self, ctx: QueryContext) -> float:
        """评估当前融合结果质量：取向量路命中的最高原始相似度"""
        vector_hits = ctx.candidates.get("vector") or []
        if vector_hits:
            return max(float(c.score or 0.0) for c in vector_hits)
        # 向量路未启用：以关键词路命中数量近似
        if ctx.candidates.get("kw_exact") or ctx.candidates.get("bm25"):
            return 0.6
        return 0.0

    async def _second_round(self, ctx: QueryContext) -> bool:
        """失败导向改写 + HyDE + 二轮召回；返回是否有新候选"""
        s = ctx.services
        plan = ctx.plan
        # 1) 生成改写 + HyDE 文本
        q = (plan.semantic_query or plan.standalone_query
             or ctx.question or "")
        try:
            out = await s.llm.generate(
                [{"role": "user", "content": _HYDE_PROMPT.format(q=q)}],
                task="rewrite",
                response_format={"type": "json_object"})
            data = json.loads(out.text)
        except Exception as e:
            log.warning("self_eval_rewrite_failed", error=str(e))
            return False
        hyde = str(data.get("hyde") or "").strip()
        rewrite = str(data.get("rewrite") or "").strip()
        gained = False

        filter_base = {"tenant_id": ctx.user.tenant_id,
                       "allowed_roles": list(ctx.user.roles or [])}
        whitelist = ctx.meta.get("whitelist")
        if whitelist is not None:
            filter_base["chunk_ids"] = whitelist
        target_cols = ctx.meta.get("target_collections") or ["default"]

        # 2) HyDE 向量二轮（高相似度区域扩展召回）
        if hyde and s.embedding and s.vector:
            try:
                vec = await s.embedding.embed_query(hyde)
                for col in target_cols:
                    top_k = (s.config.retrieval.vector_top_k
                             or s.config.retrieval.top_k_per_path) * 2
                    rs = await s.vector.search(
                        col, vec, top_k=top_k, filter=filter_base)
                    if rs:
                        tag = f"vector_hyde_{len(ctx.candidates)}"
                        self._tag_path(rs, tag)
                        ctx.candidates[tag] = rs
                        gained = True
            except Exception as e:
                log.warning("self_eval_vector_failed", error=str(e))
        # 3) 改写文本 BM25 二轮
        if rewrite and s.fulltext:
            try:
                idx = getattr(s.fulltext, "index_names", None)
                index = idx(target_cols) if callable(idx) and len(target_cols) > 1 \
                    else target_cols[0]
                top_k = (s.config.retrieval.bm25_top_k
                         or s.config.retrieval.top_k_per_path) * 2
                rs = await s.fulltext.search(
                    index, rewrite, top_k=top_k, filter=filter_base)
                if rs:
                    tag = f"bm25_rewrite_{len(ctx.candidates)}"
                    self._tag_path(rs, tag)
                    ctx.candidates[tag] = rs
                    gained = True
            except Exception as e:
                log.warning("self_eval_bm25_failed", error=str(e))
        return gained

    @staticmethod
    def _tag_path(chunks: list[RetrievedChunk], path: str) -> None:
        for c in chunks:
            c.metadata["route"] = path
            c.is_ephemeral = c.is_ephemeral


def plan_or_none(ctx: QueryContext):
    return ctx.plan



