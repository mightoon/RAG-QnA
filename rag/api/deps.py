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
    """Bearer Token → UserContext（认证中间件产物）"""
    container: ServiceContainer = request.app.state.container
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
