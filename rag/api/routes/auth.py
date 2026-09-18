"""
认证路由（rag/api/routes/auth.py）

- POST /api/auth/token  测试签发 Token（生产环境由 OIDC IdP 签发）
- GET  /api/auth/me     当前用户信息
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from rag.container import ServiceContainer
from rag.models import UserContext

from ..deps import get_container, get_current_user
from ..schemas import TokenRequest, TokenResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/token", response_model=TokenResponse)
async def issue_token(req: TokenRequest,
                      container: ServiceContainer = Depends(get_container)):
    if container.config.auth.adapter == "oidc":
        raise HTTPException(400, "OIDC 模式下请从 IdP 获取 Token")
    try:
        token = await container.auth.issue_token(
            req.user_id, req.roles, req.tenant_id,
            extra={"username": req.username, "email": req.email})
    except Exception as e:
        raise HTTPException(500, f"Token 签发失败: {e}") from e
    return TokenResponse(
        access_token=token,
        expires_in=container.config.auth.token_expire_hours * 3600)


@router.get("/me")
async def whoami(user: UserContext = Depends(get_current_user)):
    return user.model_dump()
