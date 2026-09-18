"""
FastAPI 应用工厂（rag/api/app.py）

lifespan：构建容器 → 自检（失败组件自动降级，不阻断启动）→
启动后台任务（入库 worker / 临时文档清理 / 一致性巡检）→ 优雅关闭。
配置页保存后经 rag.api.runtime.rebuild_container 热重建容器生效。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from rag.config.loader import load_config
from rag.config.models import AppConfig
from rag.container import ServiceContainer
from rag.observability.logging import get_logger

log = get_logger("rag.api")


def create_app(config: AppConfig | None = None) -> FastAPI:
    if config is None:
        config = load_config(
            os.environ.get("RAG_CONFIG", "customer/customer_config.yaml"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .runtime import start_background, stop_background
        container = ServiceContainer(config)
        await container.initialize()   # 失败组件自动降级，不抛错
        app.state.container = container

        # 后台任务
        await start_background(app, container)

        log.info("api_started", app=config.app_name, version=config.version,
                 degraded=dict(container.degraded))
        try:
            yield
        finally:
            await stop_background(app, container)
            log.info("api_stopped")

    app = FastAPI(title=config.app_name, version=config.version,
                  lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
        expose_headers=["Content-Disposition"])
    from .middleware import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.error("api_unhandled_error", path=request.url.path,
                  error=str(exc))
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})

    # 路由
    from .routes import admin, auth, chat, documents, ephemeral
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(documents.router)
    app.include_router(ephemeral.router)
    app.include_router(admin.router)

    # Web UI（页面 + 静态资源 + UI 补充 API）
    try:
        from rag.web.routes import register_ui
        register_ui(app)
    except Exception as e:
        log.warning("ui_register_failed", error=str(e))

    @app.get("/api/health", tags=["system"])
    async def health(request: Request):
        """基础健康检查（公开）：组件布尔状态"""
        c: ServiceContainer = request.app.state.container
        return {
            "status": "ok",
            "app": config.app_name,
            "version": config.version,
            "retrieval_paths": sorted(c.enabled_paths()),
            "degraded": c.degraded,
            "components": {
                "vector": c.vector is not None,
                "fulltext": c.fulltext is not None,
                "graph": c.graph is not None,
                "business": c.business is not None,
                "redis": c.redis is not None,
            },
        }

    @app.get("/api/health/full", tags=["system"])
    async def health_full(request: Request):
        """完整健康检查：逐组件 health_check + 延迟（仅管理员）"""
        import time
        from .deps import get_current_user  # 延迟导入避免环
        c: ServiceContainer = request.app.state.container
        try:
            user = await get_current_user(
                request, request.headers.get("authorization"))
        except Exception:
            return JSONResponse(status_code=401,
                                content={"detail": "未认证"})
        if not user.is_admin:
            return JSONResponse(status_code=403,
                                content={"detail": "需要管理员权限"})
        comps: dict[str, dict] = {}
        adapters = {
            "meta": c.meta, "vector": c.vector, "fulltext": c.fulltext,
            "graph": c.graph, "storage": c.storage, "llm": c.llm,
            "embedding": c.embedding, "reranker": c.reranker,
        }
        for name, ad in adapters.items():
            if ad is None:
                comps[name] = {"available": False}
                continue
            t0 = time.perf_counter()
            try:
                ok = await ad.health_check()
                comps[name] = {
                    "available": bool(ok),
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
            except Exception as e:
                comps[name] = {"available": False, "error": str(e)[:200]}
        ok_all = all(v.get("available") for v in comps.values()
                     if v.get("available") is not None)
        return {"status": "ok" if ok_all else "degraded",
                "components": comps}

    @app.get("/metrics", tags=["system"])
    async def metrics():
        """Prometheus 抓取端点"""
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def create_app_from_env() -> FastAPI:
    """uvicorn --reload 工厂模式入口
    （RAG_CONFIG 指定配置路径，RAG_NOCONNECTION=1 启用演示模式）"""
    import os
    config = load_config(
        os.environ.get("RAG_CONFIG", "customer/customer_config.yaml"))
    config.noconnection = os.environ.get("RAG_NOCONNECTION") == "1"
    return create_app(config)
