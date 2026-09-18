"""
临时文档服务（rag/services/ephemeral.py）

Session 临时文档：用户对话中上传，仅本会话可见，
TTL 2 小时自动清理，轻量流水线 30s 目标。
支持 promote：临时 → 正式知识库。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from rag.models import DocumentMeta, IngestStatus
from rag.observability.logging import get_logger

log = get_logger("rag.ephemeral")


class EphemeralService:

    def __init__(self, container):
        self.s = container
        self._cleanup_task: asyncio.Task | None = None

    # ── 上传与查询 ─────────────────────────────────────────

    async def add_file(self, session_id: str, filename: str,
                       file_path: str, file_size: int,
                       user_id: str, tenant_id: str) -> dict:
        """提交临时文档入库（轻量流水线）"""
        cfg = self.s.config.ephemeral
        if not cfg.enabled:
            return {"ok": False, "error": "临时文档功能未启用"}
        if file_size > cfg.max_file_size_mb * 1024 * 1024:
            return {"ok": False,
                    "error": f"文件超过 {cfg.max_file_size_mb}MB 上限"}
        # 每会话数量上限检查
        docs = await self.list_docs(session_id)
        if len(docs) >= cfg.max_files_per_session:
            return {"ok": False,
                    "error": f"每会话最多 {cfg.max_files_per_session} 个临时文件"}

        from rag.ingestion.coordinator import IngestItem
        batch, tasks = await self.s.ingest_coordinator.submit(
            [IngestItem(filename=filename, file_path=file_path,
                        file_size=file_size)],
            tenant_id=tenant_id, collection="__ephemeral__",
            user_id=user_id, source_type="ephemeral")
        # 会话关联（写入 SessionState）
        st = await self.s.memory_service.load_session(session_id)
        if st:
            for t in tasks:
                if t.doc_id and t.doc_id not in st.ephemeral_doc_ids:
                    st.ephemeral_doc_ids.append(t.doc_id)
            await self.s.memory_service.save_session(st)
        return {"ok": True, "batch_id": batch.batch_id,
                "task_ids": [t.task_id for t in tasks]}

    async def list_docs(self, session_id: str) -> list[DocumentMeta]:
        st = await self.s.memory_service.load_session(session_id)
        if not st or not st.ephemeral_doc_ids:
            return []
        out = []
        for doc_id in st.ephemeral_doc_ids:
            doc = await self.s.meta.get_document(doc_id, st.tenant_id)
            if doc:
                out.append(doc)
        return out

    async def remove_doc(self, session_id: str, doc_id: str) -> dict:
        """从会话移除临时文档并清理五库数据"""
        st = await self.s.memory_service.load_session(session_id)
        if not st or doc_id not in (st.ephemeral_doc_ids or []):
            return {"ok": False, "error": "临时文档不存在"}
        doc = await self.s.meta.get_document(doc_id, st.tenant_id)
        if doc:
            await self._delete_ephemeral_doc(doc)
        st.ephemeral_doc_ids.remove(doc_id)
        await self.s.memory_service.save_session(st)
        return {"ok": True}

    # ── 转正 ───────────────────────────────────────────────

    async def promote(self, session_id: str, doc_id: str,
                      collection: str) -> dict:
        """临时文档转正式：改 collection + 重新写入正式索引"""
        st = await self.s.memory_service.load_session(session_id)
        if not st:
            return {"ok": False, "error": "会话不存在"}
        doc = await self.s.meta.get_document(doc_id, st.tenant_id)
        if doc is None or doc_id not in st.ephemeral_doc_ids:
            return {"ok": False, "error": "临时文档不存在"}
        doc.collection = collection
        doc.status = IngestStatus.PENDING
        await self.s.meta.upsert_document(doc)
        # 重新走正式流水线（文件已在 MinIO）
        from rag.models import IngestTask
        task = IngestTask(tenant_id=st.tenant_id, collection=collection,
                          filename=doc.filename, doc_id=doc_id,
                          file_md5=doc.file_md5, file_type=doc.file_type,
                          source_type="ephemeral_promote",
                          submitted_by=st.user_id)
        task.checkpoint.minio = True          # 文件已在对象存储
        await self.s.meta.save_task(task)
        await self.s.ingest_coordinator.queue.put(task)
        # 从会话临时清单移除，并清理 __ephemeral__ 旧索引副本，
        # 避免转正后同 chunk_id 在两个集合双份共存
        if doc_id in (st.ephemeral_doc_ids or []):
            st.ephemeral_doc_ids.remove(doc_id)
            await self.s.memory_service.save_session(st)
        try:
            if self.s.fulltext is not None:
                await self.s.fulltext.delete_by_doc("__ephemeral__", doc_id)
            if self.s.vector is not None:
                await self.s.vector.delete_by_doc("__ephemeral__", doc_id)
        except Exception as e:
            log.warning("promote_cleanup_ephemeral_failed",
                        doc_id=doc_id, error=str(e))
        return {"ok": True, "task_id": task.task_id}

    # ── TTL 清理 ───────────────────────────────────────────

    async def start_cleanup_loop(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        """每 10 分钟清理过期临时文档（含五库数据）"""
        while True:
            try:
                await asyncio.sleep(600)
                await self.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("ephemeral_cleanup_error", error=str(e))

    async def cleanup_expired(self) -> int:
        ttl = timedelta(hours=self.s.config.ephemeral.ttl_hours)
        cutoff = datetime.utcnow() - ttl
        removed = 0
        try:
            docs = await self.s.meta.list_documents(
                tenant_id="*", collection="__ephemeral__", limit=500)
        except Exception:
            return 0
        for doc in docs:
            if doc.created_at < cutoff:
                await self._delete_ephemeral_doc(doc)
                removed += 1
        if removed:
            log.info("ephemeral_cleaned", removed=removed)
        try:
            from rag.observability.metrics import metrics
            metrics.ephemeral_docs.set(max(0, len(docs) - removed))
        except Exception:
            pass
        return removed

    async def _delete_ephemeral_doc(self, doc: DocumentMeta) -> None:
        try:
            if self.s.fulltext is not None:
                await self.s.fulltext.delete_by_doc(
                    doc.collection, doc.doc_id)
            if self.s.vector is not None:
                await self.s.vector.delete_by_doc(
                    doc.collection, doc.doc_id)
            await self.s.meta.delete_document(doc.doc_id, doc.tenant_id)
        except Exception as e:
            log.warning("ephemeral_delete_failed", doc_id=doc.doc_id,
                        error=str(e))
