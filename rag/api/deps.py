"""
FastAPI 依赖（rag/api/deps.py）
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from rag.adapters.base import AuthError
from rag.container import ServiceContainer
from rag.models import SessionState, UserContext


def get_container(request: Request) -> ServiceContainer:
    return request.app.state.container


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> UserContext:
    """Bearer Token → UserContext（认证中间件产物）

    **只读方法额外认 Cookie**（`rag_token`）：浏览器自己发起的请求带不了自定义头
    —— `<img src="/api/…">`、`<a href="/api/…" download>`、新窗口打开预览，
    这些请求只会带上 Cookie。原实现只看 `Authorization`，于是详情页分块里的
    「图区」缩略图一律 **401** → 显示成裂图 + alt 文本，而同一份数据用 JS `fetch`
    （带 Bearer 头）却能正常取到 —— 用户实测报的就是这个（见 TS-032）。

    为什么只给 GET/HEAD 开这个口子：Cookie 会随**跨站**请求自动带上，若写操作
    （POST/DELETE/PUT）也认它，等于把 CSRF 面摊到所有 `/api` 上。页面路由
    （`rag.web.routes._page_user`）一直是认 Cookie 的，这里保持同一套登录态，
    但把范围限制在"不会改数据"的方法上。
    """
    container: ServiceContainer = request.app.state.container
    if not authorization and request.method in ("GET", "HEAD"):
        cookie_token = request.cookies.get("rag_token")
        if cookie_token:
            authorization = "Bearer " + cookie_token
    # dev 认证 / 演示模式：无 Token 也放行（默认非管理员；
    # 仅显式设置 RAG_DEV_ALLOW_ADMIN=1 时 dev 模式才获得管理员身份）
    if (not authorization
            and container.config.auth.adapter == "dev"):
        import os
        user = await container.auth.verify("")
        user.is_admin = os.environ.get(
            "RAG_DEV_ALLOW_ADMIN", "").lower() in ("1", "true", "yes")
        return user
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer Token")
    token = authorization[7:].strip()
    try:
        user = await container.auth.verify(token)
    except AuthError as e:
        raise HTTPException(401, str(e)) from e
    user.is_admin = (container.config.is_admin_role(user.roles)
                     or "admin" in user.roles)
    return user


async def require_admin(
    user: UserContext = Depends(get_current_user),
) -> UserContext:
    if not user.is_admin:
        raise HTTPException(403, "需要管理员权限")
    return user


async def get_owned_session(container: ServiceContainer, user: UserContext,
                            session_id: str) -> SessionState:
    """加载会话并校验归属（本人或管理员）"""
    st = await container.memory_service.load_session(session_id)
    if st is None or (st.user_id != user.user_id and not user.is_admin):
        raise HTTPException(404, "会话不存在")
    return st
