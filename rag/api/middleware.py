"""API 中间件（rag/api/middleware.py）：简单滑动窗口限流"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    """按 IP 的滑动窗口限流（默认 120 req/min，可用
    RAG_RATE_LIMIT_PER_MINUTE 覆盖；健康检查等豁免）"""

    EXEMPT_PREFIXES = ("/api/health", "/metrics", "/docs", "/openapi")

    def __init__(self, app, per_minute: int | None = None):
        super().__init__(app)
        env = os.environ.get("RAG_RATE_LIMIT_PER_MINUTE", "")
        self.per_minute = per_minute or (int(env) if env.isdigit() else 120)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if self.per_minute <= 0:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in self.EXEMPT_PREFIXES):
            return await call_next(request)
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试"})
        q.append(now)
        return await call_next(request)
