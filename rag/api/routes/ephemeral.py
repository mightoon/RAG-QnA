"""
临时文档路由（rag/api/routes/ephemeral.py）

- POST   /api/sessions/{sid}/ephemeral            上传临时文档
- GET    /api/sessions/{sid}/ephemeral            临时文档列表
- POST   /api/sessions/{sid}/ephemeral/{doc}/promote  转正入库
- DELETE /api/sessions/{sid}/ephemeral/{doc}      移除临时文档
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from rag.container import ServiceContainer
from rag.models import UserContext

from ..deps import get_current_user, get_owned_session
from ..schemas import PromoteRequest
from .documents import SUPPORTED_EXTS

router = APIRouter(prefix="/api/sessions/{session_id}/ephemeral",
                   tags=["ephemeral"])


@router.post("")
async def upload_ephemeral(session_id: str, request: Request,
                           file: UploadFile = File(...),
                           user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    await get_owned_session(container, user, session_id)
    cfg = container.config.ephemeral

    data = await file.read()
    if len(data) > cfg.max_file_size_mb * 1024 * 1024:
        raise HTTPException(400, f"文件超过 {cfg.max_file_size_mb}MB 上限")
    if len(data) == 0:
        raise HTTPException(400, "文件为空")
    ext = Path(file.filename or "file").suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise HTTPException(400, f"不支持的文件格式: {file.filename}")

    tmp = Path(tempfile.mkdtemp(prefix="rag_eph_")) / f"{uuid.uuid4().hex}{ext}"
    tmp.write_bytes(data)
    result = await container.ephemeral_service.add_file(
        session_id, file.filename or tmp.name, str(tmp), len(data),
        user.user_id, user.tenant_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "上传失败"))
    return result


@router.get("")
async def list_ephemeral(session_id: str, request: Request,
                         user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    await get_owned_session(container, user, session_id)
    docs = await container.ephemeral_service.list_docs(session_id)
    return [d.model_dump(mode="json") for d in docs]


@router.post("/{doc_id}/promote")
async def promote(session_id: str, doc_id: str, req: PromoteRequest,
                  request: Request,
                  user: UserContext = Depends(get_current_user)):
    """临时文档转正：目标知识库需管理员或角色授权"""
    container: ServiceContainer = request.app.state.container
    await get_owned_session(container, user, session_id)
    allowed = container.config.collections_for_roles(user.roles)
    if (req.collection not in allowed and "*" not in allowed
            and not user.is_admin):
        raise HTTPException(403, f"无权写入知识库 {req.collection}")
    result = await container.ephemeral_service.promote(
        session_id, doc_id, req.collection)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "转正失败"))
    return result


@router.delete("/{doc_id}")
async def remove_ephemeral(session_id: str, doc_id: str, request: Request,
                           user: UserContext = Depends(get_current_user)):
    container: ServiceContainer = request.app.state.container
    await get_owned_session(container, user, session_id)
    result = await container.ephemeral_service.remove_doc(session_id, doc_id)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "临时文档不存在"))
    return result
