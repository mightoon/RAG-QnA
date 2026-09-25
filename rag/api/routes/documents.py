"""
文档路由（rag/api/routes/documents.py）

- POST   /api/documents/upload        多文件上传入库（内容重复时返回待确认清单）
- POST   /api/documents/upload/confirm 确认重复文件的处理方式（覆盖重跑/新建/跳过）
- POST   /api/documents/upload/discard 放弃本次待确认的上传
- POST   /api/documents/upload-path   服务器路径入库（管理员）
- GET    /api/documents               文档列表
- GET    /api/documents/{doc_id}      文档详情
- DELETE /api/documents/{doc_id}      删除文档（管理员；= 移入回收站，可恢复）
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

from fastapi import (APIRouter, Body, Depends, File, Form, HTTPException,
                     Query, Request, UploadFile)
from fastapi.responses import Response, StreamingResponse

from rag.container import ServiceContainer
from rag.ingestion.coordinator import STAGED_TTL_SEC, IngestItem
from rag.models import ACTIVE_TASK_STATUSES, IngestStatus, UserContext, file_md5
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
    out = []
    for t in tasks:
        qs = getattr(t, "quality_summary", None) or {}
        dedup = bool(isinstance(qs, dict) and qs.get("deduplicated"))
        out.append({"task_id": t.task_id, "doc_id": t.doc_id,
                    "filename": t.filename, "status": t.status.value,
                    "dedup": dedup})
    return out


# ═══ 上传入库 ═════════════════════════════════════════════

@router.post("/api/documents/upload")
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    collection: str = Form("default"),
    allowed_roles: str = Form(""),
    duplicate_action: str = Form("ask"),
    user: UserContext = Depends(get_current_user),
):
    """多文件上传入库。

    `duplicate_action` 决定"这批文件里有的内容已经入库过"时怎么处理：

      · `ask`（默认）— **不写任何库**，把重复的那几个文件连同"已存在文档"的信息
        返回（`duplicates` + `staging_token`），由前端弹框让用户决定；非重复的
        文件照常入库。用户确认后调 `/api/documents/upload/confirm`。
      · `auto`       — 老行为：命中秒传直接复用（不重新解析）。
      · `force`      — 一律当新文档完整入库。

    判定"同一文件"只看**内容 MD5**（与文件名、作者、修改时间等属性无关）。
    """
    container: ServiceContainer = request.app.state.container
    cfg = container.config
    allowed = cfg.collections_for_roles(user.roles)
    if (collection not in allowed and "*" not in allowed
            and not user.is_admin):
        raise HTTPException(403, f"无权向知识库 {collection} 上传")
    action = (duplicate_action or "ask").strip().lower()
    if action not in ("ask", "auto", "force"):
        action = "ask"

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

    # allowed_roles 由前端以 JSON 数组上送；兼容历史客户端可能发的逗号分隔串
    # （原实现直接 json.loads，畸形输入会抛 JSONDecodeError → 整个上传 500）
    roles = None
    if allowed_roles.strip():
        try:
            parsed = json.loads(allowed_roles)
            if isinstance(parsed, list):
                roles = [str(r) for r in parsed if str(r).strip()]
            elif isinstance(parsed, str):
                roles = [r.strip() for r in parsed.split(",") if r.strip()]
        except json.JSONDecodeError:
            roles = [r.strip() for r in allowed_roles.split(",") if r.strip()]
        if roles == []:
            roles = None
    batch, tasks, duplicates = await container.ingest_coordinator.submit(
        items, tenant_id=user.tenant_id, collection=collection,
        user_id=user.user_id, source_type="upload", allowed_roles=roles,
        dedup=action)
    # 没有任务入队时不返回 batch_id：那种情况下根本没有批次行（见 submit 里
    # "批次行推迟到确实有任务时再落库"），返回一个查不到的 id 只会误导调用方
    resp = {"batch_id": (batch.batch_id if tasks else None), "total": batch.total,
            "tasks": _task_brief(tasks), "duplicates": duplicates}
    if duplicates:
        # 重复的文件**没有**写任何库，它们的字节还留在临时目录里等确认 ——
        # 存一个一次性 token，确认时直接用这批文件，不让用户重传一遍。
        by_md5 = {d["md5"]: d for d in duplicates}
        staged = [{"item": it, "duplicate": by_md5[md5]}
                  for it, md5 in zip(items, [_item_md5(it) for it in items])
                  if md5 in by_md5]
        resp["staging_token"] = container.ingest_coordinator.stage_uploads(
            staged, tenant_id=user.tenant_id, collection=collection,
            user_id=user.user_id, allowed_roles=roles)
        resp["expires_in"] = STAGED_TTL_SEC
    return resp


def _item_md5(item: IngestItem) -> str:
    """暂存条目与 items 的对应关系靠内容 MD5（同名文件不会互相顶替）"""
    try:
        return file_md5(Path(item.file_path).read_bytes())
    except Exception:                              # noqa: BLE001
        return ""


@router.post("/api/documents/upload/confirm")
async def upload_confirm(
    request: Request,
    payload: dict = Body(...),
    user: UserContext = Depends(get_current_user),
):
    """确认"已存在的文件"怎么处理。

    请求体：`{"staging_token": "...", "decisions": {"<md5>|<文件名>": "reingest"|"new"|"skip"}}`
    未给出决定的文件按 `skip` 处理（宁可什么都不做，也不替用户猜）。
    """
    container: ServiceContainer = request.app.state.container
    token = str(payload.get("staging_token") or "")
    if not token:
        raise HTTPException(400, "缺少 staging_token")
    raw = payload.get("decisions") or {}
    decisions: dict[str, str] = {}
    if isinstance(raw, dict):
        decisions = {str(k): str(v) for k, v in raw.items()}
    elif isinstance(raw, list):                    # 兼容 [{md5, action}] 形式
        for d in raw:
            if isinstance(d, dict) and d.get("md5"):
                decisions[str(d["md5"])] = str(d.get("action") or "skip")
    staged = container.ingest_coordinator.get_staged(token, user.tenant_id)
    if staged is None:
        raise HTTPException(404, "待确认的上传不存在或已过期，请重新上传")
    tasks, skipped, unmatched = await container.ingest_coordinator.confirm_staged(
        token, decisions, tenant_id=user.tenant_id, user_id=user.user_id)
    if unmatched:
        log.warning("upload_confirm_unmatched", token=token, count=unmatched)
    return {"batch_id": (tasks[0].batch_id if tasks else None),
            "tasks": _task_brief(tasks), "skipped": skipped,
            "unmatched": unmatched}


@router.post("/api/documents/upload/discard")
async def upload_discard(
    request: Request,
    payload: dict = Body(...),
    user: UserContext = Depends(get_current_user),
):
    """放弃本次待确认的上传（清掉临时文件，不入库）"""
    container: ServiceContainer = request.app.state.container
    token = str(payload.get("staging_token") or "")
    if not token:
        raise HTTPException(400, "缺少 staging_token")
    n = container.ingest_coordinator.discard_staged(token, user.tenant_id)
    return {"ok": True, "discarded": n}


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
    # 服务器路径导入是管理员批量操作，没有人守着弹框 → 命中同内容直接秒传复用
    batch, tasks, _dups = await container.ingest_coordinator.submit(
        items, tenant_id=user.tenant_id, collection=req.collection,
        user_id=user.user_id, source_type="server_path",
        allowed_roles=req.allowed_roles or None, dedup="auto")
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
    """删除 = **移入回收站**（不是彻底删除）

    只把 `documents.status` 置为 `deleted` 并记 `deleted_at`/`prev_status`：
    块、向量、全文索引、对象存储里的原文件**一概不动** —— 这样「恢复」才是原样
    回来，不用重新解析。回收站里的文档不参与检索（元数据前置过滤只放
    `status IN ('done','partial')` 的文档，见 meta_mysql.query_chunk_ids）。
    真正落到五个库的清理在 `POST /api/documents/purge`（回收站里勾选后触发）。
    """
    container: ServiceContainer = request.app.state.container
    doc = await container.meta.get_document(doc_id, user.tenant_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if doc.status == IngestStatus.DELETED:
        return {"ok": True, "status": "deleted", "prevStatus": doc.prev_status}
    pending = await _active_task_for(container, doc_id, user.tenant_id)
    if pending:
        # 正在入库/重解析的文档不能删：收尾那一步（ingest_write 最后 upsert_document）
        # 会把它的状态写回 done，删完下一秒它又出现在列表里，用户以为"删除失效"。
        raise HTTPException(
            409, f"文档正在处理中（{pending}），请等任务结束后再删除")
    prev = await container.meta.soft_delete_document(doc_id, user.tenant_id)
    return {"ok": True, "status": "deleted", "prevStatus": prev}


async def _active_task_for(container: ServiceContainer, doc_id: str,
                           tenant_id: str) -> str:
    """该文档是否有活动态任务（返回状态名，没有则空串）"""
    try:
        tasks = await container.meta.list_tasks(tenant_id, limit=200)
    except Exception:                                   # noqa: BLE001
        return ""            # 查不到任务行不拦（宁可放行，也不能因为巡检失败而删不掉）
    for t in tasks:
        st = t.status.value if hasattr(t.status, "value") else str(t.status)
        if t.doc_id == doc_id and st in ACTIVE_TASK_STATUSES:
            return st
    return ""


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
