"""
对话路由（rag/api/routes/chat.py）

- POST /api/chat            问答（SSE 流式 / 非流式 JSON）
- GET  /api/sessions        会话列表
- POST /api/sessions        新建会话
- GET  /api/sessions/{id}   会话详情（含消息历史）
- DELETE /api/sessions/{id} 归档会话
- POST /api/feedback        消息反馈（赞/踩 + 原因）
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from rag.container import ServiceContainer
from rag.models import (FeedbackType, MessageFeedback, MessageRole,
                        UserContext)
from rag.observability.logging import get_logger
from rag.pipeline.context import QueryContext

from ..deps import get_container, get_current_user, get_owned_session
from ..schemas import (ChatRequest, ChatResponse, FeedbackRequest,
                       SessionCreateRequest)

log = get_logger("rag.api.chat")
router = APIRouter(prefix="/api", tags=["chat"])


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


# ═══ 问答 ═════════════════════════════════════════════════

@router.post("/chat")
async def chat(req: ChatRequest, request: Request,
               user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container

    # ── 会话加载/创建 ──────────────────────────────────
    if req.session_id:
        session = await get_owned_session(container, user, req.session_id)
    else:
        session = await container.memory_service.create_session(
            user, title=(req.question[:30] or "新对话"))

    # ── 查询上下文 + 显式元数据过滤（UI 设置）──────────
    ctx = QueryContext(session=session, user=user, question=req.question,
                       services=container)
    ctx.meta["explicit_filter"] = {
        k: v for k, v in {
            "collections": req.collections, "file_types": req.file_types,
            "date_from": req.date_from, "date_to": req.date_to,
        }.items() if v
    }
    if req.retrieval_paths:
        ctx.meta["user_paths"] = [p.lower() for p in req.retrieval_paths]
    pipeline = container.workflows.query_pipeline()
    timeout = container.config.pipeline.total_timeout_seconds

    # start 事件先入队（保证在流水线事件之前）
    ctx.events.put_nowait({"type": "start",
                           "session_id": session.session_id,
                           "title": session.title})

    async def _run() -> None:
        try:
            await pipeline.run_with_timeout(ctx, timeout)
        except Exception as e:
            log.warning("query_pipeline_error",
                        session_id=session.session_id, error=str(e))
        finally:
            # 兜底终态事件：正常路径 done 已被消费，此事件被忽略；
            # 异常路径保证 SSE 流必然终止
            ctx.events.put_nowait({"type": "done", "forced": True,
                                   "session_id": session.session_id})

    task = asyncio.create_task(_run())

    # ── 非流式：聚合完整答案 ────────────────────────────
    if not req.stream:
        answer_parts: list[str] = []
        sources: list[dict] = []
        meta: dict = {}
        timings: dict = {}
        blocked = False
        try:
            async for ev in ctx.drain_events():
                et = ev.get("type")
                if et == "token":
                    answer_parts.append(ev.get("text", ""))
                elif et == "sources":
                    sources = ev.get("sources", [])
                elif et == "meta":
                    meta.update({k: v for k, v in ev.items() if k != "type"})
                elif et == "done":
                    timings = ev.get("timings", {})
                    blocked = bool(ev.get("blocked"))
                    meta.update(ev.get("meta", {}))
                elif et == "error":
                    raise HTTPException(502, ev.get("message", "查询失败"))
        finally:
            if not task.done():
                task.cancel()
        return ChatResponse(
            session_id=session.session_id,
            answer="".join(answer_parts) or ctx.answer,
            sources=sources, timings=timings, meta=meta, blocked=blocked)

    # ── 流式 SSE ────────────────────────────────────────
    async def event_stream():
        try:
            async for ev in ctx.drain_events():
                yield _sse(ev)
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ═══ 会话管理 ═════════════════════════════════════════════

@router.get("/sessions")
async def list_sessions(request: Request,
                        user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    sessions = await container.memory_service.list_sessions(user.user_id)
    return [{
        "session_id": s.session_id, "title": s.title,
        "created_at": s.created_at, "updated_at": s.updated_at,
        "message_count": len(s.short_term),
        "ephemeral_count": len(s.ephemeral_doc_ids),
    } for s in sessions]


@router.post("/sessions", status_code=201)
async def create_session(req: SessionCreateRequest, request: Request,
                         user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    st = await container.memory_service.create_session(user, title=req.title)
    return {"session_id": st.session_id, "title": st.title}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request,
                      user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    st = await get_owned_session(container, user, session_id)
    return st.model_dump(mode="json")


@router.delete("/sessions/{session_id}")
async def archive_session(session_id: str, request: Request,
                          user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    await get_owned_session(container, user, session_id)
    await container.memory_service.archive_session(session_id)
    return {"ok": True}


# ═══ 反馈 ═════════════════════════════════════════════════

@router.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request,
                   user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    st = await get_owned_session(container, user, req.session_id)
    try:
        fb_type = FeedbackType(req.feedback)
    except ValueError:
        raise HTTPException(400, "feedback 取值必须是 up / down") from None

    # 从会话消息中补充 query / answer / sources 上下文
    query_text, answer_text, src, paths = "", "", [], {}
    msg = next((m for m in st.short_term
                if m.message_id == req.message_id), None)
    if msg is not None:
        answer_text = msg.content
        src = [s.model_dump(mode="json") for s in msg.sources]
        paths = msg.retrieval_paths
        idx = st.short_term.index(msg)
        if idx > 0 and st.short_term[idx - 1].role == MessageRole.USER:
            query_text = st.short_term[idx - 1].content

    fb = MessageFeedback(
        session_id=req.session_id, message_id=req.message_id,
        tenant_id=user.tenant_id, user_id=user.user_id, feedback=fb_type,
        query=query_text, answer=answer_text, sources=src,
        reasons=req.reasons, comment=req.comment, retrieval_paths=paths)
    await container.meta.save_feedback(fb)

    if msg is not None:
        msg.feedback = fb_type
        # 路径 1：同会话短期记忆注入负反馈，Prompt 组装时规避同类错误
        if fb_type == FeedbackType.DOWN:
            note = (f"[负反馈] 用户对问题「{query_text[:60]}」的回答不满意"
                    f"（原因：{','.join(req.reasons) or '未说明'}）")
            joined = f"{st.working.summary}\n{note}" \
                if st.working.summary else note
            st.working.summary = joined[-1200:]
        await container.memory_service.save_session(st)
    return {"ok": True, "feedback_id": fb.feedback_id}
