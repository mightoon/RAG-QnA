"""
三层记忆服务（rag/services/memory.py）

- 短期记忆：最近 3-10 轮对话原文（Session 内）
- 工作记忆：滚动摘要 + 实体槽位 + 话题链（LLM 压缩）
- 长期记忆：用户画像（MySQL user_profiles，跨 Session）

存储：Redis（不可用时降级进程内存，重启丢失）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from rag.models import (ChatMessage, SessionState, UserContext,
                        WorkingMemory)
from rag.observability.logging import get_logger

log = get_logger("rag.memory")


class MemoryService:

    def __init__(self, container):
        self.s = container
        self.redis = container.redis
        self._prefix = f"{container.config.redis.prefix}session:"
        # 降级存储（Redis 不可用）
        self._local: dict[str, SessionState] = {}

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    # ── Session 生命周期 ───────────────────────────────────

    async def create_session(self, user: UserContext,
                             title: str = "新对话") -> SessionState:
        state = SessionState(user_id=user.user_id, tenant_id=user.tenant_id,
                             title=title)
        await self.save_session(state)
        return state

    async def load_session(self, session_id: str) -> SessionState | None:
        if self.redis is not None:
            try:
                raw = await self.redis.get(self._key(session_id))
                if raw:
                    return SessionState(**json.loads(raw))
            except Exception as e:
                log.warning("redis_load_failed", error=str(e))
        return self._local.get(session_id)

    async def save_session(self, state: SessionState) -> None:
        state.updated_at = datetime.utcnow()
        ttl = self.s.config.redis.session_ttl_hours * 3600
        if self.redis is not None:
            try:
                await self.redis.set(self._key(state.session_id),
                                     state.model_dump_json(), ex=ttl)
                return
            except Exception as e:
                log.warning("redis_save_failed", error=str(e))
        self._local[state.session_id] = state

    async def list_sessions(self, user_id: str,
                            limit: int = 50) -> list[SessionState]:
        """用户会话列表（降级模式下仅进程内）"""
        out: list[SessionState] = []
        if self.redis is not None:
            try:
                keys = await self.redis.keys(f"{self._prefix}*")
                for key in keys[:limit * 3]:
                    raw = await self.redis.get(key)
                    if raw:
                        st = SessionState(**json.loads(raw))
                        if st.user_id == user_id and not st.archived:
                            out.append(st)
            except Exception:
                pass
        else:
            out = [st for st in self._local.values()
                   if st.user_id == user_id and not st.archived]
        out.sort(key=lambda s: s.updated_at, reverse=True)
        return out[:limit]

    async def archive_session(self, session_id: str) -> None:
        st = await self.load_session(session_id)
        if st:
            st.archived = True
            await self.save_session(st)

    # ── 消息追加与裁剪 ─────────────────────────────────────

    async def append_message(self, session_id: str,
                             msg: ChatMessage) -> SessionState | None:
        st = await self.load_session(session_id)
        if st is None:
            return None
        st.short_term.append(msg)
        # 裁剪：保留最近 max_turns 轮（2 条/轮）
        max_msgs = self.s.config.memory.short_term_max_turns * 2
        if len(st.short_term) > max_msgs:
            st.short_term = st.short_term[-max_msgs:]
        await self.save_session(st)
        return st

    # ── 工作记忆 ───────────────────────────────────────────

    async def update_working(self, session_id: str,
                             working: WorkingMemory) -> None:
        st = await self.load_session(session_id)
        if st:
            st.working = working
            await self.save_session(st)

    async def compress_working(self, session_id: str,
                               new_turn: str) -> WorkingMemory | None:
        """滚动摘要压缩：摘要 + 新轮次 → 新摘要（token 超预算时触发）。
        turn_count 由 MemoryUpdateStep 统一递增，此处不重复计数。"""
        st = await self.load_session(session_id)
        if st is None:
            return None
        w = st.working
        budget = self.s.config.memory.working_summary_max_tokens
        combined = f"{w.summary}\n{new_turn}" if w.summary else new_turn
        if self.s.llm.count_tokens(combined) <= budget:
            w.summary = combined[-budget * 3:]
            await self.save_session(st)
            return w
        try:
            w.summary = await self.s.llm.generate([{
                "role": "user",
                "content": f"将以下对话历史压缩为不超过{budget}字的摘要，"
                           f"保留关键实体、结论与待办：\n{combined[:6000]}"}],
                task="summary", max_tokens=budget * 2)
            await self.save_session(st)
        except Exception as e:
            log.warning("working_compress_failed", error=str(e))
        return w

    # ── 长期记忆（用户画像）────────────────────────────────

    async def load_profile(self, user: UserContext):
        try:
            return await self.s.meta.get_profile(user.user_id, user.tenant_id)
        except Exception:
            return None

    async def save_profile(self, profile) -> None:
        try:
            await self.s.meta.upsert_profile(profile)
        except Exception as e:
            log.warning("profile_save_failed", error=str(e))

    # ── 话题向量（C17：cosine 话题跳转检测）─────────────────

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        import math
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1e-9
        nb = math.sqrt(sum(x * x for x in b)) or 1e-9
        return dot / (na * nb)

    async def detect_topic_shift(self, session: SessionState,
                                 query_vector: list[float]) -> bool:
        """与当前话题向量比较：余弦低于阈值 → 判定话题跳转"""
        w = session.working
        if not w.topic_vector:
            return False
        thr = self.s.config.memory.topic_switch_threshold
        return self._cosine(query_vector, w.topic_vector) < thr

    async def update_topic_vector(self, session_id: str,
                                  vector: list[float]) -> None:
        st = await self.load_session(session_id)
        if st:
            st.working.topic_vector = vector
            await self.save_session(st)

    # ── 归档清理 + 长期记忆画像合并 ─────────────────────────

    async def archive_idle_sessions(self) -> int:
        """无活动会话归档（后台定时任务调用）：
        归档时生成对话摘要 → 写入用户画像 archived_summaries 空间，
        供新会话按向量检索历史上下文。"""
        threshold = timedelta(
            minutes=self.s.config.memory.session_archive_after_minutes)
        count = 0
        states: list[SessionState] = []
        if self.redis is not None:
            try:
                keys = await self.redis.keys(f"{self._prefix}*")
                for key in keys:
                    raw = await self.redis.get(key)
                    if raw:
                        states.append(SessionState(**json.loads(raw)))
            except Exception:
                pass
        else:
            states = list(self._local.values())
        for st in states:
            if st.archived:
                continue
            if datetime.utcnow() - st.updated_at <= threshold:
                continue
            try:
                await self._archive_one(st)
                count += 1
            except Exception as e:
                log.warning("archive_failed",
                            session_id=st.session_id, error=str(e))
        return count

    async def _archive_one(self, st: SessionState) -> None:
        # 1) 生成本会话整体摘要
        history = "\n".join(
            f"{m.role}: {m.content}" for m in st.short_term[-20:])
        if not history.strip() and not st.working.summary:
            st.archived = True
            await self.save_session(st)
            return
        try:
            summary = await self.s.llm.generate([{
                "role": "user",
                "content": "请将以下对话压缩成 150 字以内的客观摘要，"
                           "突出主题、结论与关键实体：\n"
                           f"{(history or st.working.summary)[:6000]}"}],
                task="summary", max_tokens=300)
        except Exception:
            summary = (st.working.summary or history)[:200]
        # 2) 写入画像：归档摘要向量空间（供新会话检索相关历史）
        try:
            vector = await self.s.embedding.embed_query(summary) \
                if self.s.embedding else None
        except Exception:
            vector = None
        try:
            from rag.models import UserProfile
            profile = await self.load_profile(
                UserContext(user_id=st.user_id, tenant_id=st.tenant_id,
                            roles=[]))
            if profile is None:
                profile = UserProfile(user_id=st.user_id,
                                      tenant_id=st.tenant_id)
            archive_list = profile.custom.setdefault("archived_summaries", [])
            archive_list.append({
                "session_id": st.session_id,
                "title": st.title,
                "summary": summary,
                "vector": vector,
                "archived_at": datetime.utcnow().isoformat()})
            profile.custom["archived_summaries"] = archive_list[-20:]
            await self.save_profile(profile)
        except Exception as e:
            log.warning("archive_profile_merge_failed", error=str(e))
        st.archived = True
        await self.save_session(st)

    async def relevant_history(self, user: UserContext, query: str,
                               limit: int = 2) -> list[str]:
        """新会话开始：在归档摘要向量空间检索与当前问题最相关的历史上下文"""
        if not self.s.config.memory.long_term_enabled \
                or not self.s.embedding or not query:
            return []
        try:
            profile = await self.load_profile(user)
        except Exception:
            return []
        items = (profile.custom.get("archived_summaries")
                 if profile else None) or []
        if not items:
            return []
        try:
            qv = await self.s.embedding.embed_query(query)
        except Exception:
            return []
        scored = sorted(
            [(self._cosine(qv, it.get("vector") or []), it)
             for it in items if it.get("vector")],
            key=lambda x: x[0], reverse=True)
        return [f"【历史会话-{(s[1].get('title') or '')[:20]}】"
                f"{s[1]['summary']}"
                for s in scored[:limit] if s[0] >= 0.45]
