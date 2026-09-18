"""
管理路由（rag/api/routes/admin.py，均需管理员权限）

- GET  /api/admin/stats           知识库统计 + 组件状态
- GET  /api/admin/workflows       流水线结构描述
- POST /api/admin/consistency/run 手动触发一致性巡检
- GET  /api/admin/feedback        负向反馈列表（低质召回分析）
- POST /api/admin/synonyms/reload 同义词热更新
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from rag.container import ServiceContainer
from rag.models import UserContext

from ..deps import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])


@router.get("/stats")
async def stats(request: Request):
    container: ServiceContainer = request.app.state.container
    return {
        "collections": await container.meta.collection_stats(
            container.config.tenant_id),
        "retrieval_paths": sorted(container.enabled_paths()),
        "workflows": container.workflows.describe(),
        "components": {
            "vector": container.vector is not None,
            "fulltext": container.fulltext is not None,
            "graph": container.graph is not None,
            "business": container.business is not None,
            "redis": container.redis is not None,
            "storage": container.storage is not None,
        },
    }


@router.get("/workflows")
async def workflows(request: Request):
    container: ServiceContainer = request.app.state.container
    return container.workflows.describe()


@router.post("/consistency/run")
async def run_consistency(request: Request):
    container: ServiceContainer = request.app.state.container
    return await container.consistency_checker.run_check()


@router.get("/feedback")
async def negative_feedback(request: Request, days: int = 30,
                            limit: int = Query(100, le=500)):
    container: ServiceContainer = request.app.state.container
    fbs = await container.meta.list_negative_feedback(days=days, limit=limit)
    return [fb.model_dump(mode="json") for fb in fbs]


@router.post("/synonyms/reload")
async def reload_synonyms(request: Request):
    container: ServiceContainer = request.app.state.container
    if container.synonym is None:
        return {"ok": False, "error": "同义词服务未启用"}
    count = container.synonym.reload()
    return {"ok": True, "groups": count}


# ── 检索路管理 ────────────────────────────────────────────

@router.get("/retrieval-paths")
async def retrieval_paths(request: Request):
    """检索路清单：名称、默认权重、是否启用（供前端勾选 UI）"""
    container: ServiceContainer = request.app.state.container
    r = container.config.retrieval
    enabled = set(container.enabled_paths())
    desc = {
        "vector": "语义向量检索（embedding 相似度）",
        "bm25": "全文检索（ES BM25）",
        "kw_exact": "关键词精确匹配",
        "graph": "知识图谱多跳遍历",
        "ephemeral": "会话临时文档（上传文件时自动激活）",
    }
    defaults = r.default_route_weights or {}
    return {"paths": [
        {"name": k, "description": desc.get(k, ""),
         "enabled": k in enabled or k == "ephemeral",
         "default_weight": defaults.get(k)}
        for k in ("vector", "bm25", "kw_exact", "graph", "ephemeral")]}


# ── 文档巡检报告 ──────────────────────────────────────────

@router.get("/documents/{doc_id}/quality")
async def document_quality(request: Request, doc_id: str):
    """单文档质量/巡检报告"""
    container: ServiceContainer = request.app.state.container
    doc = await container.meta.get_document(doc_id)
    if doc is None:
        return {"ok": False, "error": "文档不存在"}
    return {"doc_id": doc.doc_id, "filename": doc.filename,
            "status": doc.status.value, "chunk_count": doc.chunk_count,
            "quality_report": doc.quality_report}


# ── 配置查看（脱敏）──────────────────────────────────────

_SECRET_KEYS = ("password", "secret", "token", "api_key", "access_key")


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: ("******" if any(s in k.lower() for s in _SECRET_KEYS)
                    else _sanitize(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


@router.get("/config")
async def get_config(request: Request):
    """当前运行配置（敏感字段脱敏，只读）"""
    container: ServiceContainer = request.app.state.container
    return _sanitize(container.config.model_dump(mode="json"))
