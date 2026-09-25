"""
内存元数据适配器（rag/adapters/meta_memory.py）

槽位 meta / 注册名 memory：--noconnection 演示模式与 MySQL 降级时使用，
数据存进程字典，重启即失。也可在配置中显式指定 `meta.adapter: memory`
（如单元测试、轻量演示）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from rag.models import (ChunkMeta, DocumentMeta, IngestBatch, IngestStatus,
                        IngestTask, MessageFeedback, TableData, UserProfile)
from rag.observability.logging import get_logger

from .base import MetaStoreAdapter
from .registry import AdapterRegistry

log = get_logger("rag.adapters.meta_memory")


@AdapterRegistry.register("meta", "memory")
class InMemoryMetaAdapter(MetaStoreAdapter):
    """进程内存版元数据库（演示/测试）"""

    def __init__(self, config=None):
        self._docs: dict[str, DocumentMeta] = {}          # doc_id → doc
        self._chunks: dict[str, ChunkMeta] = {}           # chunk_id → chunk
        self._doc_chunks: dict[str, list[str]] = {}       # doc_id → [chunk_id]
        self._tables: dict[str, TableData] = {}
        self._tasks: dict[str, IngestTask] = {}           # task_id → task
        self._batches: dict[str, IngestBatch] = {}        # batch_id → batch
        self._feedbacks: dict[str, MessageFeedback] = {}
        self._profiles: dict[tuple[str, str], UserProfile] = {}  # (uid,tenant)
        self._entities: dict[str, list[dict]] = {}        # tenant → entities

        self._texts: dict[str, str] = {}                  # chunk_id → 正文

    # ── 文档元数据 ─────────────────────────────────────────

    async def upsert_document(self, doc: DocumentMeta) -> None:
        old = self._docs.get(doc.doc_id)
        if old is not None and old.status == IngestStatus.DELETED:
            # 与 MySQL 侧同一道兜底：回收站里的行不接受 upsert 带来的状态，
            # 否则入库收尾会把"已删除"的文档悄悄写回 done（用户看到文档自己回来了）
            doc.status = IngestStatus.DELETED
            doc.prev_status = old.prev_status
            doc.deleted_at = old.deleted_at
        self._docs[doc.doc_id] = doc

    async def update_doc_status(self, doc_id: str, status: IngestStatus,
                                chunk_count: int | None = None) -> None:
        doc = self._docs.get(doc_id)
        if doc:
            doc.status = status
            if chunk_count is not None:
                doc.chunk_count = chunk_count

    async def get_document(self, doc_id: str,
                           tenant_id: str) -> DocumentMeta | None:
        doc = self._docs.get(doc_id)
        return doc if doc and doc.tenant_id == tenant_id else None

    async def list_documents(self, tenant_id: str,
                             collection: str | None = None,
                             status: IngestStatus | None = None,
                             limit: int = 100, offset: int = 0
                             ) -> list[DocumentMeta]:
        out = [d for d in self._docs.values()
               if d.tenant_id == tenant_id
               and (collection is None or d.collection == collection)
               and (status is None or d.status == status)]
        out.sort(key=lambda d: d.created_at, reverse=True)
        return out[offset:offset + limit]

    async def delete_document(self, doc_id: str, tenant_id: str) -> None:
        doc = self._docs.get(doc_id)
        if doc and doc.tenant_id == tenant_id:
            self._docs.pop(doc_id, None)
            for cid in self._doc_chunks.pop(doc_id, []):
                self._chunks.pop(cid, None)
                self._texts.pop(cid, None)
            # 表格结构化行同属该文档，一并清掉（MySQL 侧没有外键可级联，这里也不留）
            for tid in [k for k, t in self._tables.items() if t.doc_id == doc_id]:
                self._tables.pop(tid, None)

    async def soft_delete_document(self, doc_id: str,
                                   tenant_id: str) -> str | None:
        """移入回收站：只改状态，内容一概不动（恢复才能是"原样回来"）"""
        doc = self._docs.get(doc_id)
        if doc is None or doc.tenant_id != tenant_id:
            return None
        if doc.status == IngestStatus.DELETED:
            return doc.prev_status or IngestStatus.DONE.value
        prev = doc.status.value
        doc.prev_status = prev
        doc.status = IngestStatus.DELETED
        doc.deleted_at = datetime.utcnow()
        return prev

    async def restore_document(self, doc_id: str,
                               tenant_id: str) -> str | None:
        doc = self._docs.get(doc_id)
        if doc is None or doc.tenant_id != tenant_id:
            return None
        if doc.status != IngestStatus.DELETED:
            return doc.status.value
        # 老数据（本次改动之前删的）没有 prev_status：按"有没有块"兜底，
        # 绝不再无条件写 done（那会把 failed/partial 洗成"已完成"）
        back = doc.prev_status or (IngestStatus.DONE.value if doc.chunk_count
                                   else IngestStatus.FAILED.value)
        doc.status = IngestStatus(back)
        doc.prev_status = None
        doc.deleted_at = None
        return back

    async def find_doc_by_md5(self, file_md5: str,
                              tenant_id: str) -> DocumentMeta | None:
        for d in self._docs.values():
            if d.tenant_id == tenant_id and d.file_md5 == file_md5:
                return d
        return None

    # ── Chunk 元数据 ───────────────────────────────────────

    async def upsert_chunks(self, chunks: list[ChunkMeta],
                            texts: dict[str, str] | None = None) -> None:
        for c in chunks:
            self._chunks[c.chunk_id] = c
            self._doc_chunks.setdefault(c.doc_id, []).append(c.chunk_id)
        for cid, txt in (texts or {}).items():
            if txt:
                self._texts[cid] = txt

    async def get_chunk_texts(self, chunk_ids: list[str]) -> dict[str, str]:
        return {cid: self._texts[cid] for cid in chunk_ids
                if cid in self._texts}

    async def list_collections(self, tenant_id: str) -> list[str]:
        cols = {d.collection for d in self._docs.values()
                if d.tenant_id == tenant_id
                and d.status in (IngestStatus.DONE, IngestStatus.PARTIAL)}
        return sorted(cols) or ["default"]

    async def adjust_chunk_quality(self, chunk_id: str, delta: float,
                                   floor: float = 0.05) -> float | None:
        c = self._chunks.get(chunk_id)
        if c is None:
            return None
        c.quality_score = max(floor, min(1.0, c.quality_score + delta))
        return c.quality_score

    async def query_chunk_ids(
        self, tenant_id: str,
        collections: list[str] | None = None,
        file_types: list[str] | None = None,
        date_from: Any = None, date_to: Any = None,
        allowed_roles: list[str] | None = None,
        extra_hints: dict | None = None,
    ) -> list[str]:
        hints = extra_hints or {}
        whitelist = hints.get("chunk_ids")
        result = []
        for c in self._chunks.values():
            doc = self._docs.get(c.doc_id)
            if doc is None or doc.tenant_id != tenant_id:
                continue
            if collections and doc.collection not in collections:
                continue
            if file_types and (doc.file_type or "") not in file_types:
                continue
            if date_from and doc.created_at < date_from:
                continue
            if date_to and doc.created_at > date_to:
                continue
            if allowed_roles and doc.allowed_roles:
                if not set(allowed_roles) & set(doc.allowed_roles):
                    continue
            if whitelist is not None and c.chunk_id not in whitelist:
                continue
            result.append(c.chunk_id)
            if len(result) >= 50000:
                break
        return result

    async def list_chunk_ids(self, doc_id: str) -> list[str]:
        return list(self._doc_chunks.get(doc_id, []))

    async def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[ChunkMeta]:
        return [self._chunks[cid] for cid in chunk_ids if cid in self._chunks]

    async def delete_chunks(self, chunk_ids: list[str]) -> int:
        """按 chunk_id 批量删除（重跑清理旧块用），返回删除条数"""
        n = 0
        for cid in chunk_ids or []:
            c = self._chunks.pop(cid, None)
            if c is None:
                continue
            n += 1
            self._texts.pop(cid, None)
            ids = self._doc_chunks.get(c.doc_id) or []
            if cid in ids:
                ids.remove(cid)
        return n

    # ── 表格数据 ───────────────────────────────────────────

    async def upsert_table_data(self, tables: list[TableData]) -> None:
        """按 doc 整体替换（与 MySQL 实现同一语义：重跑不叠加重复行）

        键必须是 `chunk_id`（`TableData` 的字段之一）。原实现用 `t.table_id` ——
        模型里根本没有这个字段，走内存兜底（MySQL 不可用时的 `_CORE_FALLBACKS`）
        时每写一次表格就 AttributeError，整篇文档判失败。
        同一 chunk 可能对应多行（切片/多表同块），因此键取
        `(chunk_id, table_index, page_num)`，不做有损覆盖。
        """
        for doc_id in {t.doc_id for t in tables if t.doc_id}:
            for k in [k for k, v in self._tables.items() if v.doc_id == doc_id]:
                self._tables.pop(k, None)
        for i, t in enumerate(tables):
            key = f"{t.chunk_id}|{t.table_index}|{t.page_num}|{i}"
            self._tables[key] = t

    # ── 入库任务/批次 ──────────────────────────────────────

    async def save_task(self, task: IngestTask) -> None:
        self._tasks[task.task_id] = task

    async def get_task(self, task_id: str,
                       tenant_id: str = "") -> IngestTask | None:
        t = self._tasks.get(task_id)
        if t is None:
            return None
        if tenant_id and t.tenant_id != tenant_id:
            return None
        return t

    async def list_tasks(self, tenant_id: str,
                         collection: str | None = None,
                         status: IngestStatus | None = None,
                         limit: int = 20, offset: int = 0,
                         batch_id: str | None = None
                         ) -> list[IngestTask]:
        out = [t for t in self._tasks.values()
               if t.tenant_id == tenant_id
               and (collection is None or t.collection == collection)
               and (status is None or t.status == status)
               and (batch_id is None or t.batch_id == batch_id)]
        out.sort(key=lambda t: t.task_id, reverse=True)
        return out[offset:offset + limit]

    async def find_by_md5(self, file_md5: str,
                          tenant_id: str) -> IngestTask | None:
        for t in self._tasks.values():
            if (t.tenant_id == tenant_id and t.file_md5 == file_md5
                    and t.status == IngestStatus.DONE):
                return t
        return None

    async def find_incomplete_tasks(self) -> list[IngestTask]:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        incomplete = (IngestStatus.PENDING, IngestStatus.PARSING,
                      IngestStatus.CHUNKING, IngestStatus.EMBEDDING,
                      IngestStatus.WRITING, IngestStatus.RETRYING)
        return [t for t in self._tasks.values()
                if t.status in incomplete
                and t.created_at and t.created_at > cutoff]

    async def save_batch(self, batch: IngestBatch) -> None:
        self._batches[batch.batch_id] = batch

    async def get_batch(self, batch_id: str) -> IngestBatch | None:
        return self._batches.get(batch_id)

    async def list_batches(self, tenant_id: str, limit: int = 20,
                           offset: int = 0) -> list[IngestBatch]:
        out = [b for b in self._batches.values() if b.tenant_id == tenant_id]
        out.sort(key=lambda b: b.created_at, reverse=True)
        return out[offset:offset + limit]

    async def recompute_batch(self, batch_id: str) -> IngestBatch | None:
        batch = self._batches.get(batch_id)
        if batch is None:
            return None
        tasks = [t for t in self._tasks.values() if t.batch_id == batch_id]
        batch.total = len(tasks)
        batch.succeeded = sum(
            1 for t in tasks
            if t.status in (IngestStatus.DONE, IngestStatus.PARTIAL))
        batch.failed = sum(1 for t in tasks
                           if t.status == IngestStatus.FAILED)
        batch.pending = sum(1 for t in tasks
                            if t.status in (IngestStatus.PENDING,
                                            IngestStatus.PARSING,
                                            IngestStatus.CHUNKING,
                                            IngestStatus.EMBEDDING,
                                            IngestStatus.WRITING,
                                            IngestStatus.RETRYING))
        if batch.total == 0:
            batch.status = "pending"
        elif batch.failed == 0 and batch.pending == 0:
            batch.status = "done"
        elif batch.succeeded + batch.failed == batch.total:
            batch.status = "partial_failed"
        else:
            batch.status = "running"
        return batch

    # ── 反馈 ───────────────────────────────────────────────

    async def save_feedback(self, fb: MessageFeedback) -> None:
        self._feedbacks[fb.feedback_id] = fb

    async def list_negative_feedback(self, days: int = 30,
                                     limit: int = 1000
                                     ) -> list[MessageFeedback]:
        cutoff = datetime.utcnow() - timedelta(days=days)
        out = [f for f in self._feedbacks.values()
               if f.feedback.value == "down" and f.created_at >= cutoff]
        out.sort(key=lambda f: f.created_at, reverse=True)
        return out[:limit]

    # ── 用户画像 ───────────────────────────────────────────

    async def upsert_profile(self, profile: UserProfile) -> None:
        self._profiles[(profile.user_id, profile.tenant_id)] = profile

    async def get_profile(self, user_id: str,
                          tenant_id: str) -> UserProfile | None:
        return self._profiles.get((user_id, tenant_id))

    # ── 实体表 ─────────────────────────────────────────────

    async def list_entities(self, tenant_id: str,
                            limit: int = 50000) -> list[dict]:
        return self._entities.get(tenant_id, [])[:limit]

    async def upsert_entities(self, tenant_id: str,
                              entities: list[dict]) -> None:
        existing = {e["name"]: e
                    for e in self._entities.get(tenant_id, [])}
        for e in entities:
            existing[e["name"]] = e
        self._entities[tenant_id] = list(existing.values())

    # ── 统计 ───────────────────────────────────────────────

    async def collection_stats(self, tenant_id: str) -> list[dict]:
        stats: dict[str, dict] = {}
        for d in self._docs.values():
            if d.tenant_id != tenant_id:
                continue
            s = stats.setdefault(
                d.collection, {"collection": d.collection, "documents": 0,
                               "chunks": 0, "failed": 0})
            s["documents"] += 1
            s["chunks"] += len(self._doc_chunks.get(d.doc_id, []))
            if d.status == IngestStatus.FAILED:
                s["failed"] += 1
        return sorted(stats.values(), key=lambda s: s["collection"])

    async def health_check(self) -> bool:
        return True
