"""
认证适配器（rag/adapters/auth.py）

jwt：自签 HS256（无外部 JWT 依赖，手写编解码）
oidc：标准 OIDC（占位，需现场对接 IDP）
dev：开发模式，跳过认证（默认管理员上下文）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import httpx

from rag.config.models import AuthConfig
from rag.models import UserContext

from .base import AuthAdapter, AuthError
from .registry import AdapterRegistry


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


@AdapterRegistry.register("auth", "jwt")
class JWTAuth(AuthAdapter):

    def __init__(self, config: AuthConfig):
        self.config = config

    def _sign(self, payload: dict) -> str:
        header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        body = _b64url_encode(json.dumps(payload, ensure_ascii=False).encode())
        msg = f"{header}.{body}".encode()
        sig = hmac.new(self.config.jwt_secret.encode(), msg,
                       hashlib.sha256).digest()
        return f"{header}.{body}.{_b64url_encode(sig)}"

    async def issue_token(self, user_id: str, roles: list[str],
                          tenant_id: str, extra: dict | None = None) -> str:
        now = int(time.time())
        payload = {
            "sub": user_id,
            "roles": roles,
            "tenant_id": tenant_id,
            "iat": now,
            "exp": now + self.config.token_expire_hours * 3600,
            "jti": uuid.uuid4().hex,
        }
        if extra:
            payload.update(extra)
        return self._sign(payload)

    async def verify(self, token: str) -> UserContext:
        try:
            header, body, sig = token.split(".")
            msg = f"{header}.{body}".encode()
            expected = hmac.new(self.config.jwt_secret.encode(), msg,
                                hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _b64url_decode(sig)):
                raise AuthError("签名无效")
            payload = json.loads(_b64url_decode(body))
            if payload.get("exp", 0) < time.time():
                raise AuthError("Token 已过期")
            return UserContext(
                user_id=payload["sub"],
                roles=payload.get("roles", []),
                tenant_id=payload.get("tenant_id", "default"),
                email=payload.get("email"),
            )
        except AuthError:
            raise
        except Exception as e:
            raise AuthError(f"Token 解析失败: {e}") from e


@AdapterRegistry.register("auth", "dev")
class DevAuth(AuthAdapter):
    """开发模式：任何 token 均通过，返回固定管理员上下文"""

    def __init__(self, config: AuthConfig):
        self.config = config

    async def verify(self, token: str) -> UserContext:
        # token 形如 "user:roles:tenant" 可自定义，否则用默认
        if token and token.count(":") == 2:
            uid, roles, tenant = token.split(":")
            return UserContext(user_id=uid, roles=roles.split(",") or ["user"],
                               tenant_id=tenant or "default", is_admin="admin" in roles)
        return UserContext(
            user_id=self.config.dev_user_id,
            username="开发者",
            roles=list(self.config.dev_roles),
            tenant_id=self.config.dev_tenant_id,
            is_admin=True,
        )

    async def issue_token(self, user_id: str, roles: list[str],
                          tenant_id: str, extra: dict | None = None) -> str:
        return f"{user_id}:{','.join(roles)}:{tenant_id}"


@AdapterRegistry.register("auth", "oidc")
class OIDCAuth(AuthAdapter):
    """OIDC 单点登录：本地校验 JWT 或调用 userinfo 端点"""

    def __init__(self, config: AuthConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=10)

    async def verify(self, token: str) -> UserContext:
        issuer = self.config.oidc_issuer
        try:
            resp = await self._client.get(
                f"{issuer.rstrip('/')}/.well-known/openid-configuration")
            resp.raise_for_status()
            oidc_cfg = resp.json()
            userinfo_url = oidc_cfg["userinfo_endpoint"]
            resp = await self._client.get(
                userinfo_url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            info = resp.json()
            return UserContext(
                user_id=str(info.get("sub", "")),
                username=info.get("name", info.get("preferred_username", "")),
                roles=info.get("roles", info.get("groups", [])),
                tenant_id=info.get("tenant", "default"),
                email=info.get("email"),
            )
        except AuthError:
            raise
        except Exception as e:
            raise AuthError(f"OIDC 验证失败: {e}") from e

    async def issue_token(self, user_id: str, roles: list[str],
                          tenant_id: str, extra: dict | None = None) -> str:
        raise AuthError("OIDC 模式下由 IdP 签发 token，不支持本地签发")
