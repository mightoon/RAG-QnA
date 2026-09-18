"""
入库协调器（rag/ingestion/coordinator.py）

- submit：批次/任务创建 + MD5 秒传去重
- worker 池：并发消费（ingest.concurrency）
- 两阶段写入检查点：进程重启后从 MinIO 恢复文件断点续写
- 失败重试：指数退避（60s/300s/900s），超限置 failed
- 临时文档：轻量流水线（无 enrich/verify，30s 目标）
- 进度事件：TaskProgressEvent → ProgressBus → SSE
"""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rag.models import (DocumentMeta, IngestBatch, IngestStatus, IngestTask,
                        TaskProgressEvent, file_md5)
from rag.observability.logging import get_logger
from rag.pipeline.base import Pipeline, StepError, StepRegistry
from rag.pipeline.context import IngestContext
from rag.pipeline.engine import WorkflowRegistry

log = get_logger("rag.ingestion")

# 临时文档轻量序列（30s 目标：跳过 LLM 摘要与抽样验证）
_EPHEMERAL_STEPS = ["parse", "outline", "chunk", "embed", "write", "finalize"]


@dataclass
class IngestItem:
    """提交单元：文件名 + 本地路径（API 层已落盘临时文件）"""
    filename: str
    file_path: str
    file_size: int = 0
    content: bytes | None = None       # 服务器路径/CLI 场景可直传字节


class IngestionCoordinator:

    def __init__(self, container, workflows: WorkflowRegistry):
        self.s = container
        self.workflows = workflows
        self.queue: asyncio.Queue[IngestTask] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._task_files: dict[str, str] = {}       # task_id → 本地文件路径
        self._temp_dirs: set[str] = set()

    # ── 生命周期 ───────────────────────────────────────────

    async def start(self) -> None:
        n = self.s.config.ingest.concurrency
        for i in range(n):
            self._workers.append(
                asyncio.create_task(self._worker(f"ingest-{i}")))
        await self.resume_incomplete()
        log.info("coordinator_started", workers=n)

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        for d in self._temp_dirs:
            Path(d).unlink(missing_ok=True)

    # ── 提交 ───────────────────────────────────────────────

    async def submit(self, items: list[IngestItem], tenant_id: str,
                     collection: str, user_id: str = "",
                     source_type: str = "upload",
                     allowed_roles: list[str] | None = None
                     ) -> tuple[IngestBatch, list[IngestTask]]:
        batch = IngestBatch(tenant_id=tenant_id, collection=collection,
                            total=len(items), source_type=source_type,
                            status="running")
        await self.s.meta.save_batch(batch)
        created: list[IngestTask] = []

        for item in items:
            task = IngestTask(
                batch_id=batch.batch_id, tenant_id=tenant_id,
                collection=collection, filename=item.filename,
                file_type=Path(item.filename).suffix.lstrip(".").lower(),
                submitted_by=user_id, source_type=source_type)
            # 读取内容与 MD5
            content = item.content
            if content is None:
                content = Path(item.file_path).read_bytes()
            item.file_size = len(content)
            md5 = file_md5(content)

            # MD5 秒传：同租户已有完成文档 → 复用 storage_url 与索引
            existing = await self.s.meta.find_doc_by_md5(md5, tenant_id)
            doc = DocumentMeta(
                doc_id=task.task_id.replace("task", "doc") or task.task_id,
                tenant_id=tenant_id, collection=collection,
                filename=item.filename,
                file_type=task.file_type or "txt",
                file_size=item.file_size, file_md5=md5,
                allowed_roles=allowed_roles or [], created_by=user_id)
            task.doc_id = doc.doc_id
            task.file_md5 = md5

            # 秒传仅当权限视图一致（角色集合相同），否则必须重新入库
            same_roles = existing is not None and sorted(
                existing.allowed_roles or []) == sorted(allowed_roles or [])
            if existing is not None and same_roles:
                doc.storage_url = existing.storage_url
                doc.status = IngestStatus.DONE
                # chunk 计数以自身 chunks_meta 为真实口径（白名单映射走
                # file_md5 关联到源文档物理 chunk），此处如实记 0
                doc.chunk_count = existing.chunk_count
                doc.page_count = existing.page_count
                doc.quality_report = {"deduplicated": True,
                                      "source_doc_id": existing.doc_id,
                                      "roles_match": True}
                task.status = IngestStatus.DONE
                task.total_chunks = existing.chunk_count
                task.written_chunks = existing.chunk_count
                task.quality_summary = {"deduplicated": True}
                task.completed_at = datetime.utcnow()
                await self.s.meta.upsert_document(doc)
                await self.s.meta.save_task(task)
                created.append(task)
                await self._emit_progress(task, 1.0, "MD5 秒传完成")
                continue
            # 同文件不同权限视图 → 正常入库；同名不同 md5 → 旧版软删除
            await self._supersede_old_versions(
                tenant_id, collection, item.filename, md5, exclude=doc.doc_id)

            await self.s.meta.upsert_document(doc)
            await self.s.meta.save_task(task)
            created.append(task)
            self._task_files[task.task_id] = item.file_path
            await self.queue.put(task)

        await self.s.meta.recompute_batch(batch.batch_id)
        return batch, created

    # ── Worker ─────────────────────────────────────────────

    async def _worker(self, name: str) -> None:
        while True:
            task = await self.queue.get()
            try:
                await self._process(task)
            except Exception as e:
                log.error("worker_unexpected_error", worker=name,
                          task_id=task.task_id, error=str(e))
                await self._fail_task(task, str(e))
            finally:
                self.queue.task_done()

    async def _process(self, task: IngestTask) -> None:
        s = self.s
        task.started_at = task.started_at or datetime.utcnow()
        doc = await s.meta.get_document(task.doc_id, task.tenant_id)
        if doc is None:
            await self._fail_task(task, "文档元数据缺失")
            return

        # ── 阶段 0：MinIO 原始文件上传（检查点）────────────
        file_path = self._task_files.get(task.task_id)
        if file_path is None or not Path(file_path).exists():
            file_path = await self._restore_from_storage(task, doc)
            if file_path is None:
                await self._fail_task(task, "文件不可得（本地与对象存储均缺失）")
                return
            self._task_files[task.task_id] = file_path

        if not task.checkpoint.minio:
            try:
                content = Path(file_path).read_bytes()
                url = await s.storage.put(
                    f"{task.tenant_id}/{doc.doc_id}/{doc.filename}", content)
                doc.storage_url = url
                await s.meta.upsert_document(doc)
                task.checkpoint.minio = True
                await s.meta.save_task(task)
            except Exception as e:
                await self._retry_or_fail(task, f"对象存储上传失败: {e}")
                return

        # ── Pipeline 选择 ──────────────────────────────────
        if task.source_type == "ephemeral":
            pipeline = Pipeline("ingest:ephemeral",
                                StepRegistry.build(_EPHEMERAL_STEPS))
        else:
            scan_type = "text"
            if (task.file_type or "").lower() == "pdf":
                scan_type = self._quick_scan_check(file_path)
            pipeline = self.workflows.ingest_pipeline(
                task.filename, scan_type)

        ctx = IngestContext(task=task, doc=doc, file_path=file_path,
                            services=s)
        ctx.progress_cb = self._make_progress_cb(task)

        try:
            await pipeline.run(ctx)
        except StepError as e:
            await self._retry_or_fail(task, str(e), e.retryable)
            return

        # 成功（done / partial 由 finalize 步骤设置）
        task.completed_at = datetime.utcnow()
        try:
            from rag.observability.metrics import metrics
            metrics.ingest_tasks_total.labels(
                status=task.status.value).inc()
        except Exception:
            pass
        if task.status in (IngestStatus.DONE, IngestStatus.PARTIAL):
            asyncio.create_task(self._cleanup_superseded(
                task.tenant_id, task.collection, task.filename))
        await s.meta.save_task(task)
        if task.batch_id:
            await s.meta.recompute_batch(task.batch_id)
            batch = await s.meta.get_batch(task.batch_id)
            if batch and batch.status in ("done", "partial_failed"):
                await self._notify_batch(batch)
        self._cleanup_local(self._task_files.pop(task.task_id, None))

    @staticmethod
    def _cleanup_local(file_path: str | None) -> None:
        """删除 API 上传/断点恢复产生的临时本地文件（仅系统临时目录内，
        服务器路径入库的源文件不受影响）"""
        if not file_path:
            return
        try:
            p = Path(file_path)
            if p.exists() and str(p).startswith(tempfile.gettempdir()):
                p.unlink(missing_ok=True)
        except Exception:
            pass

    # ── 版本管理（软删除）─────────────────────────────────

    async def _supersede_old_versions(self, tenant_id: str, collection: str,
                                      filename: str, file_md5: str,
                                      exclude: str) -> None:
        """覆盖上传：同（租户/集合/文件名）且 md5 不同的已完成旧版标为
        SUPERSEDED（软删除），物理清理由新版写入成功后异步执行。"""
        try:
            docs = await self.s.meta.list_documents(
                tenant_id, collection=collection, limit=200)
        except Exception:
            return
        for d in docs:
            if (d.doc_id != exclude and d.filename == filename
                    and d.file_md5 != file_md5
                    and d.status in (IngestStatus.DONE, IngestStatus.PARTIAL)):
                try:
                    await self.s.meta.update_doc_status(
                        d.doc_id, IngestStatus.SUPERSEDED)
                    log.info("doc_superseded", doc_id=d.doc_id,
                             filename=filename)
                except Exception:
                    pass

    async def _cleanup_superseded(self, tenant_id: str, collection: str,
                                  filename: str) -> None:
        """新版本写入成功后：物理删除被 SUPERSEDED 的旧版（三库 + 存储）"""
        try:
            docs = await self.s.meta.list_documents(
                tenant_id, collection=collection,
                status=IngestStatus.SUPERSEDED, limit=100)
        except Exception:
            return
        for d in docs:
            if d.filename != filename:
                continue
            try:
                if self.s.vector is not None:
                    await self.s.vector.delete_by_doc(d.collection, d.doc_id)
                if self.s.fulltext is not None:
                    await self.s.fulltext.delete_by_doc(d.collection, d.doc_id)
                if d.storage_url and self.s.storage is not None:
                    await self.s.storage.delete(d.storage_url)
                await self.s.meta.delete_document(d.doc_id, d.tenant_id)
                log.info("superseded_cleaned", doc_id=d.doc_id)
            except Exception as e:
                log.warning("superseded_cleanup_failed",
                            doc_id=d.doc_id, error=str(e))

    # ── 重试与失败 ─────────────────────────────────────────

    async def _retry_or_fail(self, task: IngestTask, message: str,
                             retryable: bool = True) -> None:
        task.error_message = message[:1000]
        backoffs = self.s.config.ingest.retry_backoff_seconds
        if retryable and task.retry_count < task.max_retries:
            task.retry_count += 1
            task.status = IngestStatus.RETRYING
            await self.s.meta.save_task(task)
            await self._emit_progress(task, 0.0,
                                      f"第 {task.retry_count} 次重试排队")
            delay = backoffs[min(task.retry_count - 1, len(backoffs) - 1)]
            asyncio.create_task(self._delayed_requeue(task, delay))
        else:
            await self._fail_task(task, message)

    async def _delayed_requeue(self, task: IngestTask, delay: int) -> None:
        await asyncio.sleep(delay)
        # 重试前刷新任务状态（可能已被人工取消）
        fresh = await self.s.meta.get_task(task.task_id)
        if fresh and fresh.status not in (IngestStatus.FAILED,
                                          IngestStatus.DONE,
                                          IngestStatus.SUPERSEDED):
            await self.queue.put(fresh)

    async def _fail_task(self, task: IngestTask, message: str) -> None:
        task.status = IngestStatus.FAILED
        task.error_message = message[:1000]
        task.completed_at = datetime.utcnow()
        try:
            from rag.observability.metrics import metrics
            metrics.ingest_tasks_total.labels(status="failed").inc()
        except Exception:
            pass
        await self.s.meta.save_task(task)
        if task.doc_id:
            await self.s.meta.update_doc_status(
                task.doc_id, IngestStatus.FAILED)
        if task.batch_id:
            await self.s.meta.recompute_batch(task.batch_id)
        await self._emit_progress(task, 1.0, "失败", error=message)
        self._task_files.pop(task.task_id, None)

    # ── 断点恢复 ───────────────────────────────────────────

    async def resume_incomplete(self) -> None:
        """启动时恢复 24h 内所有未完成任务（文件从 MinIO 取回）。
        断在 PARSING/CHUNKING/EMBEDDING/WRITING 的孤儿任务统一重归队：
        chunk_id 确定性幂等，重跑整链路不会产生重复数据。"""
        try:
            tasks = await self.s.meta.find_incomplete_tasks()
        except Exception:
            return
        for task in tasks:
            if task.status in (IngestStatus.FAILED, IngestStatus.DONE,
                               IngestStatus.SUPERSEDED):
                continue
            if task.status != IngestStatus.RETRYING:
                task.status = IngestStatus.RETRYING
                try:
                    await self.s.meta.save_task(task)
                except Exception:
                    continue
            await self.queue.put(task)
            log.info("task_resumed", task_id=task.task_id)

    async def _restore_from_storage(self, task: IngestTask,
                                    doc: DocumentMeta) -> str | None:
        """从对象存储恢复文件到本地临时路径"""
        if not doc.storage_url:
            return None
        try:
            content = await self.s.storage.get(doc.storage_url)
            suffix = Path(doc.filename).suffix or ".bin"
            fd, path = tempfile.mkstemp(suffix=suffix,
                                        prefix=f"resume_{task.task_id}_")
            self._temp_dirs.add(path)
            with open(fd, "wb") as f:
                f.write(content)
            return path
        except Exception as e:
            log.warning("restore_failed", task_id=task.task_id,
                        error=str(e))
            return None

    # ── 进度与通知 ─────────────────────────────────────────

    def _make_progress_cb(self, task: IngestTask):
        async def cb(status: str, progress: float, detail: str) -> None:
            try:
                task.status = IngestStatus(status)
            except ValueError:
                pass
            await self.s.meta.save_task(task)
            await self._emit_progress(task, progress, detail)
        return cb

    async def _emit_progress(self, task: IngestTask, progress: float,
                             detail: str, error: str | None = None) -> None:
        if self.s.progress_bus is not None:
            try:
                await self.s.progress_bus.publish(TaskProgressEvent(
                    task_id=task.task_id, batch_id=task.batch_id,
                    doc_id=task.doc_id, filename=task.filename,
                    status=task.status, progress=progress,
                    stage_detail=detail, error=error))
            except Exception:
                pass

    async def _notify_batch(self, batch: IngestBatch) -> None:
        if self.s.notification_service is not None:
            try:
                await self.s.notification_service.notify_ingest_result(batch)
            except Exception:
                pass

    # ── 工具 ───────────────────────────────────────────────

    @staticmethod
    def _quick_scan_check(file_path: str) -> str:
        """PDF 快速预检：前 3 页有无文本层（决定 workflow 路由）"""
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            for page in reader.pages[:3]:
                if len((page.extract_text() or "").strip()) > 50:
                    return "text"
            return "scanned"
        except Exception:
            return "text"
