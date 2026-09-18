"""
查询步骤：安全与理解（rag/pipeline/steps/query_understand.py）

- SecurityStep   安全校验（长度/敏感词/注入模式，违规直接拒答）
- MemoryLoadStep 加载会话记忆（短期 + 工作记忆 + 临时文档列表）
- UnderstandStep 查询理解：一次 LLM 调用产出 QueryPlan 五层次，
                 失败降级规则方案；EntityLinker 归一化 + 同义词扩展 +
                 意图路由权重 + 隐式元数据意图（V2.1）
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from rag.models import (IntentType, QueryEntity, QueryPlan)
from rag.observability.logging import get_logger
from rag.pipeline.base import PipelineStep, StepRegistry, StepError
from rag.pipeline.context import QueryContext

log = get_logger("rag.steps.understand")

_UNDERSTAND_PROMPT = """你是查询理解引擎。根据对话历史和当前问题，输出 JSON（不要其他内容）。

对话历史（可能为空）：
{history}

当前问题：{question}

输出 JSON 格式：
{{
  "standalone_query": "指代消解后的独立问题（无历史依赖时可与原问题相同）",
  "semantic_query": "剥离时间/文件类型等约束后的语义核心",
  "intent": "factual|relational|aggregation|procedural|comparative|chitchat",
  "confidence": 0.0,
  "entities": [{{"name": "实体名", "type": "product|person|org|model|generic"}}],
  "keywords": ["关键词"],
  "constraints": {{"date_from": null, "date_to": null, "file_types": []}},
  "implicit": {{"prefer_latest": false, "ephemeral_only": false, "section_keyword": null}},
  "rewrites": ["从其他角度改写的问题（0-2个）"],
  "sub_queries": ["复合问题拆解出的子问题（简单问题为空数组）"],
  "is_followup": false,
  "topic_switched": false
}}

意图说明：
- factual 事实查询（型号/参数/定义）
- relational 关系推理（A和B的关系/影响）
- aggregation 聚合统计（多少/平均/汇总）
- procedural 步骤流程（如何做/操作步骤）
- comparative 对比分析（A和B哪个/差异）
- chitchat 闲聊（问候/无关话题）"""

_INJECTION_PATTERNS = [
    r"忽略(之前|以上|上面)(的)?(所有)?(指令|提示|规则)",
    r"(泄露|显示|输出|打印)(你的)?(系统)?(提示|指令|prompt)",
    r"system\s*prompt", r"你(是|的)(系统|初始)(提示|指令)",
]


@StepRegistry.register("security")
class SecurityStep(PipelineStep):

    async def execute(self, ctx: QueryContext) -> None:
        cfg = ctx.services.config.pipeline
        q = ctx.question.strip()
        if not q:
            await self._block(ctx, "问题不能为空")
        if len(q) > cfg.security_max_query_length:
            await self._block(
                ctx, f"问题长度超过上限 {cfg.security_max_query_length} 字符")
        for word in cfg.security_blocked_words:
            if word and word in q:
                await self._block(ctx, "问题包含不允许的内容")
                return
        for pat in _INJECTION_PATTERNS:
            if re.search(pat, q, re.I):
                await self._block(ctx, "检测到提示注入尝试，已拦截")
                return

    @staticmethod
    async def _block(ctx: QueryContext, reason: str) -> None:
        ctx.answer = "抱歉，您的问题无法处理。请调整表述后重试。"
        ctx.meta["blocked"] = reason
        await ctx.emit({"type": "token", "text": ctx.answer})
        await ctx.emit({"type": "done", "blocked": True, "reason": reason})
        # retryable=False：直接终止流水线（事件流已优雅收尾）
        raise StepError("security", reason, retryable=False)


@StepRegistry.register("memory_load")
class MemoryLoadStep(PipelineStep):

    async def execute(self, ctx: QueryContext) -> None:
        mem = ctx.services.memory_service
        st = await mem.load_session(ctx.session.session_id)
        if st is not None:
            # 保留 API 层可能设置的 ephemeral 请求参数
            st.ephemeral_doc_ids = (st.ephemeral_doc_ids or [])
            ctx.session = st
        # 长期记忆：用户画像 + 与当前问题相关的归档会话摘要（B15/C18）
        try:
            profile = await mem.load_profile(ctx.user)
            if profile:
                parts = []
                if profile.professional_background:
                    parts.append(f"专业背景：{profile.professional_background}")
                if profile.preferred_format:
                    parts.append(f"偏好回答格式：{profile.preferred_format}")
                if profile.common_products:
                    parts.append("常用产品：" + "、".join(
                        profile.common_products[:10]))
                if parts:
                    ctx.meta["user_profile"] = "；".join(parts)
        except Exception as e:
            log.debug("profile_load_failed", error=str(e))
        try:
            history = await mem.relevant_history(ctx.user, ctx.question)
            if history:
                ctx.meta["related_history"] = history
        except Exception as e:
            log.debug("relevant_history_failed", error=str(e))
        ctx.tick("memory_loaded")


# 意图 → 默认路由权重（软路由）
_DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "factual":     {"vector": 0.9, "bm25": 0.7, "kw_exact": 0.5},
    "relational":  {"graph": 0.9, "vector": 0.7, "bm25": 0.5},
    "aggregation": {"structured": 0.9, "bm25": 0.6, "vector": 0.4},
    "procedural":  {"vector": 0.9, "bm25": 0.6},
    "comparative": {"vector": 0.9, "bm25": 0.6},
    "chitchat":    {},
}


@StepRegistry.register("understand")
class UnderstandStep(PipelineStep):

    async def execute(self, ctx: QueryContext) -> None:
        plan = await self._llm_understand(ctx)
        if plan is None:
            plan = self._rule_understand(ctx)
        # ── 合并 API 层显式过滤（UI 设置）与角色权限 ────────
        self._merge_explicit_filter(ctx, plan)
        # ── 实体归一化 + 同义词扩展 ────────────────────────
        try:
            linked = await ctx.services.entity_linker.link_batch(
                ctx.user.tenant_id,
                [{"name": e.original, "entity_type": e.entity_type}
                 for e in plan.entities])
            plan.entities = [QueryEntity(
                original=e["name"], canonical=e.get("canonical"),
                entity_type=e.get("entity_type", "generic"),
                linked=e.get("linked", False)) for e in linked]
        except Exception:
            pass
        try:
            plan.synonyms = ctx.services.synonym.expand_batch(
                plan.keywords + [e.original for e in plan.entities])
        except Exception:
            plan.synonyms = {}
        # ── 路由权重 ──────────────────────────────────────
        weights = dict(_DEFAULT_WEIGHTS.get(plan.intent.value, {}))
        if any(e.linked for e in plan.entities):
            weights["kw_exact"] = min(1.0, weights.get("kw_exact", 0) + 0.4)
        if plan.metadata_filter.merged_hints().get("ephemeral_only"):
            weights["ephemeral"] = 0.9
        if ctx.session.ephemeral_doc_ids and "ephemeral" not in weights:
            weights["ephemeral"] = 0.6          # 会话有临时文档时加权
        plan.route_weights = weights
        plan.session_id = ctx.session.session_id
        # ── 话题跳转向量检测（C17）：余弦 < 阈值 → 重置实体槽位/话题向量 ──
        try:
            embed = ctx.services.embedding
            if embed is not None:
                qvec = await embed.embed_query(
                    plan.semantic_query or plan.standalone_query)
                mem = ctx.services.memory_service
                shifted = await mem.detect_topic_shift(ctx.session, qvec)
                if shifted:
                    plan.topic_switched = True
                    ctx.session.working.entity_slots = []
                    log.info("topic_shift_detected",
                             session_id=ctx.session.session_id)
                ctx.meta["topic_vector"] = qvec
        except Exception as e:
            log.debug("topic_vector_failed", error=str(e))
        ctx.plan = plan
        await ctx.emit({"type": "plan", "intent": plan.intent.value,
                        "confidence": plan.confidence,
                        "entities": [e.original for e in plan.entities],
                        "routes": weights})

    # ── 显式过滤合并（安全下限：角色权限不可被 LLM 绕过）──

    @staticmethod
    def _merge_explicit_filter(ctx: QueryContext, plan: QueryPlan) -> None:
        """API 层显式过滤（UI 设置，ctx.meta['explicit_filter']）∩ 角色权限
        → plan.metadata_filter。LLM 输出的过滤条件在此基础上只收紧不放宽。"""
        explicit = ctx.meta.get("explicit_filter") or {}
        mf = plan.metadata_filter
        allowed = ctx.services.config.collections_for_roles(ctx.user.roles)
        if explicit.get("collections"):
            req = [c for c in explicit["collections"]
                   if c in allowed or "*" in allowed]
            mf.collections = req or allowed
        else:
            mf.collections = allowed
        if explicit.get("file_types"):
            mf.file_types = list(explicit["file_types"])
        for k in ("date_from", "date_to"):
            if explicit.get(k):
                try:
                    setattr(mf, k, datetime.fromisoformat(str(explicit[k])))
                except ValueError:
                    pass
        mf.allowed_roles = ctx.user.roles or []

    # ── LLM 理解 ─────────────────────────────────────────

    async def _llm_understand(self, ctx: QueryContext) -> QueryPlan | None:
        history = ""
        for m in ctx.session.short_term[-4:]:
            role = "用户" if m.role == "user" else "助手"
            history += f"{role}: {m.content[:200]}\n"
        if ctx.session.working.summary:
            history += f"[摘要] {ctx.session.working.summary[:300]}\n"
        try:
            out = await ctx.services.llm.generate([{
                "role": "user",
                "content": _UNDERSTAND_PROMPT.format(
                    history=history or "（无）", question=ctx.question)}],
                task="rewrite", temperature=0.0, max_tokens=600)
            data = json.loads(self._extract_json(out))
        except Exception as e:
            log.debug("llm_understand_failed", error=str(e))
            return None
        try:
            intent = IntentType(data.get("intent", "factual"))
        except ValueError:
            intent = IntentType.FACTUAL
        cons = data.get("constraints") or {}
        impl = data.get("implicit") or {}
        plan = QueryPlan(
            original_query=ctx.question,
            standalone_query=data.get("standalone_query") or ctx.question,
            semantic_query=data.get("semantic_query") or ctx.question,
            intent=intent,
            confidence=float(data.get("confidence", 0.6)),
            keywords=[str(k) for k in (data.get("keywords") or [])][:10],
            rewrites=[str(r) for r in (data.get("rewrites") or [])][:2],
            sub_queries=[str(s) for s in (data.get("sub_queries") or [])][:4],
            is_followup=bool(data.get("is_followup")),
            topic_switched=bool(data.get("topic_switched")),
            entities=[QueryEntity(
                original=str(e.get("name", ""))[:64],
                entity_type=str(e.get("type", "generic"))[:32])
                for e in (data.get("entities") or [])
                if e.get("name")][:8])
        # 约束（时间/文件类型）
        for k in ("date_from", "date_to"):
            v = cons.get(k)
            if v:
                try:
                    from datetime import datetime
                    setattr(plan.constraints, k,
                            datetime.fromisoformat(str(v)))
                except ValueError:
                    pass
        plan.constraints.file_types = [
            str(t).lower().lstrip(".")
            for t in (cons.get("file_types") or [])][:5]
        # 隐式元数据意图（V2.1）
        from rag.models import ImplicitMetaIntent
        plan.metadata_filter._extra_hints = {
            "prefer_latest": bool(impl.get("prefer_latest")),
            "ephemeral_only": bool(impl.get("ephemeral_only")),
            "section_keyword": impl.get("section_keyword"),
        }
        return plan

    @staticmethod
    def _extract_json(text: str) -> str:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return m.group(0) if m else "{}"

    # ── 规则降级 ─────────────────────────────────────────

    @staticmethod
    def _rule_understand(ctx: QueryContext) -> QueryPlan:
        q = ctx.question
        # 简单意图规则
        if len(q) < 8 and re.search(r"^(你好|您好|hi|hello|在吗)", q, re.I):
            intent = IntentType.CHITCHAT
        elif re.search(r"多少|几个|平均|汇总|统计|总计", q):
            intent = IntentType.AGGREGATION
        elif re.search(r"关系|影响|导致|关联|依赖", q):
            intent = IntentType.RELATIONAL
        elif re.search(r"如何|怎么|步骤|流程|怎样|操作", q):
            intent = IntentType.PROCEDURAL
        elif re.search(r"区别|差异|对比|哪个好|比较|vs", q, re.I):
            intent = IntentType.COMPARATIVE
        else:
            intent = IntentType.FACTUAL
        # 关键词：英文词 + 中文 2-gram
        words = re.findall(r"[A-Za-z][A-Za-z0-9\-_]{1,}", q)
        return QueryPlan(
            original_query=q, standalone_query=q, semantic_query=q,
            intent=intent, confidence=0.4,
            keywords=list(dict.fromkeys(words))[:8],
            entities=[], is_followup=False)
