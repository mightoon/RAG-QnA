"""
文档路由（rag/api/routes/documents.py）

- POST   /api/documents/upload        多文件上传入库
- POST   /api/documents/upload-path   服务器路径入库（管理员）
- GET    /api/documents               文档列表
- GET    /api/documents/{doc_id}      文档详情
- DELETE /api/documents/{doc_id}      删除文档（管理员，五库清理）
- GET    /api/documents/{doc_id}/download  下载原文件
- GET    /api/tasks | /api/batches    任务/批次查询
- GET    /api/progress                入库进度 SSE
"""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import Response, StreamingResponse

from rag.container import ServiceContainer
from rag.ingestion.coordinator import IngestItem
from rag.models import IngestStatus, UserContext
from rag.observability.logging import get_logger

from ..deps import get_container, get_current_user, require_admin
from ..schemas import ServerPathRequest

log = get_logger("rag.api.documents")
router = APIRouter(tags=["documents"])

SUPPORTED_EXTS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt",
    ".md", ".markdown", ".txt", ".log", ".html", ".htm",
    ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp",
}


def _task_brief(tasks) -> list[dict]:
    return [{"task_id": t.task_id, "doc_id": t.doc_id, "filename": t.filename,
             "status": t.status.value} for t in tasks]


# ═══ 上传入库 ═════════════════════════════════════════════

@router.post("/api/documents/upload")
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    collection: str = Form("default"),
    allowed_roles: str = Form(""),
    user: UserContext = Depends(get_current_user),
):
    container: ServiceContainer = request.app.state.container
    cfg = container.config
    allowed = cfg.collections_for_roles(user.roles)
    if (collection not in allowed and "*" not in allowed
            and not user.is_admin):
        raise HTTPException(403, f"无权向知识库 {collection} 上传")

    max_bytes = cfg.max_upload_mb * 1024 * 1024
    tmp_dir = Path(tempfile.mkdtemp(prefix="rag_upload_"))
    items: list[IngestItem] = []
    total = 0
    try:
        for f in files:
            data = await f.read()
            total += len(data)
            if total > max_bytes:
                raise HTTPException(
                    400, f"上传总量超过 {cfg.max_upload_mb}MB 上限")
            ext = Path(f.filename or "file").suffix.lower()
            if ext not in SUPPORTED_EXTS:
                raise HTTPException(400, f"不支持的文件格式: {f.filename}")
            dest = tmp_dir / f"{uuid.uuid4().hex}{ext}"
            dest.write_bytes(data)
            items.append(IngestItem(filename=f.filename or dest.name,
                                    file_path=str(dest), file_size=len(data)))
    except HTTPException:
        for it in items:
            Path(it.file_path).unlink(missing_ok=True)
        tmp_dir.rmdir()
        raise

    roles = json.loads(allowed_roles) if allowed_roles.strip() else None
    batch, tasks = await container.ingest_coordinator.submit(
        items, tenant_id=user.tenant_id, collection=collection,
        user_id=user.user_id, source_type="upload", allowed_roles=roles)
    return {"batch_id": batch.batch_id, "total": batch.total,
            "tasks": _task_brief(tasks)}


@router.post("/api/documents/upload-path")
async def upload_path(req: ServerPathRequest, request: Request,
                      user: UserContext = Depends(require_admin)):
    """服务器路径入库：仅限 server_ingest_root 之下（防越权读取）"""
    container: ServiceContainer = request.app.state.container
    root = container.config.server_ingest_root
    if not root:
        raise HTTPException(403, "服务器路径入库未启用（server_ingest_root 未配置）")
    root_path = Path(root).resolve()

    files: list[Path] = []
    for p in req.paths:
        path = Path(p).resolve()
        if not str(path).startswith(str(root_path)):
            raise HTTPException(400, f"路径必须在 {root} 之下: {p}")
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            it = path.rglob("*") if req.recursive else path.glob("*")
            files += [f for f in it if f.is_file()]
        else:
            raise HTTPException(400, f"路径不存在: {p}")
    files = [f for f in files if f.suffix.lower() in SUPPORTED_EXTS]
    if not files:
        raise HTTPException(400, "未找到支持的文件")

    items = [IngestItem(filename=f.name, file_path=str(f),
                        file_size=f.stat().st_size) for f in files]
    batch, tasks = await container.ingest_coordinator.submit(
        items, tenant_id=user.tenant_id, collection=req.collection,
        user_id=user.user_id, source_type="server_path",
        allowed_roles=req.allowed_roles or None)
    return {"batch_id": batch.batch_id, "total": batch.total,
            "tasks": _task_brief(tasks)}


# ═══ 文档查询/删除 ═════════════════════════════════════════

@router.get("/api/documents")
async def list_documents(
    request: Request,
    collection: str | None = None,
    status: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    user: UserContext = Depends(get_current_user),
):
    container: ServiceContainer = request.app.state.container
    st: IngestStatus | None = None
    if status:
        try:
            st = IngestStatus(status)
        except ValueError:
            raise HTTPException(400, f"无效状态: {status}") from None
    # 拉取该范围全量（上限 1000），支持前端分页环境变量
    docs = await container.meta.list_documents(
        user.tenant_id, collection, st, 1000, 0)
    if st is None:
        # 默认排除回收站与已被覆盖的旧版本
        docs = [d for d in docs if d.status.value not in ("deleted", "superseded")]
    total = len(docs)
    page = docs[offset:offset + limit]
    return {"docs": [d.model_dump(mode="json") for d in page],
            "total": total, "hasMore": offset + limit < total}


@router.get("/api/documents/{doc_id}")
async def get_document(doc_id: str, request: Request,
                       user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    doc = await container.meta.get_document(doc_id, user.tenant_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    return doc.model_dump(mode="json")


@router.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: str, request: Request,
                          user: UserContext = Depends(require_admin)):
    container: ServiceContainer = request.app.state.container
    doc = await container.meta.get_document(doc_id, user.tenant_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if doc.status == IngestStatus.DELETED:
        return {"ok": True, "status": "deleted"}
    # 软删除：进入回收站；物理清理由 /api/documents/{id}/permanent 执行
    await container.meta.update_doc_status(doc_id, IngestStatus.DELETED)
    return {"ok": True, "status": "deleted"}


@router.get("/api/documents/{doc_id}/download")
async def download(doc_id: str, request: Request,
                   user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    doc = await container.meta.get_document(doc_id, user.tenant_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if not doc.storage_url:
        raise HTTPException(404, "原文件不存在")
    content = await container.storage.get(doc.storage_url)
    return Response(
        content, media_type="application/octet-stream",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(doc.filename)}"})


# ═══ 任务/批次/进度 ════════════════════════════════════════

@router.get("/api/tasks")
async def list_tasks(request: Request, batch_id: str | None = None,
                     limit: int = Query(50, le=200),
                     user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    tasks = await container.meta.list_tasks(user.tenant_id, batch_id=batch_id,
                                            limit=limit)
    return [t.model_dump(mode="json") for t in tasks]


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str, request: Request,
                   user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    task = await container.meta.get_task(task_id, user.tenant_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task.model_dump(mode="json")


@router.get("/api/batches")
async def list_batches(request: Request, limit: int = Query(50, le=200),
                       user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    batches = await container.meta.list_batches(user.tenant_id, limit=limit)
    return [b.model_dump(mode="json") for b in batches]


@router.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str, request: Request,
                    user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    batch = await container.meta.get_batch(batch_id, user.tenant_id)
    if batch is None:
        raise HTTPException(404, "批次不存在")
    return batch.model_dump(mode="json")


@router.get("/api/progress")
async def progress(request: Request, batch_id: str | None = None,
                   user: UserContext = Depends(get_current_user)):
    """入库进度 SSE（可用 batch_id 过滤）"""
    container: ServiceContainer = request.app.state.container
    bus = container.progress_bus
    if bus is None:
        raise HTTPException(503, "进度总线不可用（未配置 Redis）")

    async def stream():
        async for ev in bus.subscribe():
            if ev is None:
                yield ": keep-alive\n\n"
                continue
            if batch_id and ev.batch_id != batch_id:
                continue
            yield f"data: {ev.model_dump_json()}\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
