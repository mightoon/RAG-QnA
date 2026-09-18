"""
API 请求/响应模型（rag/api/schemas.py）

响应直接复用 rag.models 中的模型（model_dump），
此处仅定义请求体与少量聚合响应。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ═══ 认证 ═════════════════════════════════════════════════

class TokenRequest(BaseModel):
    """测试签发 Token（生产环境应走 OIDC 由 IdP 签发）"""
    user_id: str
    username: str = ""
    roles: list[str] = Field(default_factory=lambda: ["user"])
    tenant_id: str = "default"
    email: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 0


# ═══ 会话与对话 ═══════════════════════════════════════════

class SessionCreateRequest(BaseModel):
    title: str = "新对话"


class ChatRequest(BaseModel):
    session_id: str | None = None           # 空 = 新建会话
    question: str
    # 显式元数据过滤（UI 设置，与角色权限取交集）
    collections: list[str] | None = None
    file_types: list[str] | None = None
    date_from: str | None = None            # ISO 8601
    date_to: str | None = None
    # 用户选择的检索路（None=系统默认；最终激活=系统启用∩勾选）
    retrieval_paths: list[str] | None = None
    stream: bool = True                     # False = 等待完整 JSON


class ChatResponse(BaseModel):
    """非流式模式响应"""
    session_id: str
    answer: str
    sources: list[dict] = Field(default_factory=list)
    timings: dict = Field(default_factory=dict)
    meta: dict = Field(default_factory=dict)
    blocked: bool = False


class FeedbackRequest(BaseModel):
    session_id: str
    message_id: str
    feedback: str                           # up / down
    reasons: list[str] = Field(default_factory=list)
    comment: str = ""


# ═══ 文档入库 ═════════════════════════════════════════════

class ServerPathRequest(BaseModel):
    """服务器路径入库（管理员）"""
    paths: list[str]                        # 文件或目录
    collection: str = "default"
    allowed_roles: list[str] = Field(default_factory=list)
    recursive: bool = False


class PromoteRequest(BaseModel):
    """临时文档转正"""
    collection: str = "default"
