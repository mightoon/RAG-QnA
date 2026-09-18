"""
Pipeline 上下文（rag/pipeline/context.py）

两类上下文分别服务于写侧（入库）与读侧（查询）流水线，
是步骤间传递中间产物的唯一数据总线。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from rag.models import (
    ChatMessage, DocumentMeta, IngestTask, ParsedDocument, QualityReport,
    QueryPlan, RetrievedChunk, SessionState, SourceReference, TableData,
    UserContext, ChunkMeta,
)


@dataclass
class IngestContext:
    """入库流水线上下文：一个文档从文件到五库落地的全过程状态"""

    task: IngestTask
    doc: DocumentMeta
    file_path: str
    services: Any                                # ServiceContainer

    # ── 中间产物（步骤按序填充）─────────────────────────────
    parsed: ParsedDocument | None = None
    chunks: list[ChunkMeta] = field(default_factory=list)
    chunk_texts: list[str] = field(default_factory=list)      # 与 chunks 对齐
    summaries: list[str | None] = field(default_factory=list)
    keywords: list[list[str]] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    embeddings: dict[str, list[float]] = field(default_factory=dict)
    tables: list[TableData] = field(default_factory=list)
    quality: QualityReport = field(
        default_factory=lambda: QualityReport(doc_id=""))
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    # 进度回调：coordinator 注入（更新任务状态 + 推送 SSE）
    progress_cb: Callable[[str, float, str], Awaitable[None]] | None = None

    async def report(self, status: str, progress: float, detail: str = "") -> None:
        if self.progress_cb:
            try:
                await self.progress_cb(status, progress, detail)
            except Exception:
                pass

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


@dataclass
class QueryContext:
    """查询流水线上下文：一次问答从问题到流式答案的全过程状态"""

    session: SessionState
    user: UserContext
    question: str
    services: Any                                # ServiceContainer

    # ── 中间产物 ────────────────────────────────────────────
    plan: QueryPlan | None = None
    candidates: dict[str, list[RetrievedChunk]] = field(default_factory=dict)
    merged: list[RetrievedChunk] = field(default_factory=list)
    reranked: list[RetrievedChunk] = field(default_factory=list)
    prompt_messages: list[dict] = field(default_factory=list)
    answer: str = ""
    sources: list[SourceReference] = field(default_factory=list)
    user_message: ChatMessage | None = None
    assistant_message: ChatMessage | None = None

    # SSE 事件队列：generate 步骤推 token，API 层消费
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    # 计时与调试信息
    timings: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    _t0: float = field(default_factory=time.monotonic)

    def tick(self, stage: str) -> None:
        self.timings[stage] = round(time.monotonic() - self._t0, 3)

    async def emit(self, event: dict) -> None:
        """推送 SSE 事件（type: token/sources/done/error/meta）"""
        await self.events.put(event)

    async def drain_events(self):
        """API 层消费事件直到收到 done/error 终止事件"""
        while True:
            event = await self.events.get()
            yield event
            if event.get("type") in ("done", "error"):
                break
