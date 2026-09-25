"""
入库协调器（rag/ingestion/coordinator.py）

- submit：批次/任务创建 + 内容 MD5 去重（秒传 / 待用户确认 / 强制新建）
- worker 池：并发消费（ingest.concurrency）
- 两阶段写入检查点：进程重启后从 MinIO 恢复文件断点续写
- 失败重试：指数退避（60s/300s/900s），超限置 failed
- 临时文档：轻量流水线（无 enrich/verify，30s 目标）
- 进度事件：TaskProgressEvent → ProgressBus → SSE
- 重复上传确认：暂存 → 用户决定（覆盖重跑 / 新建 / 跳过）
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rag.models import (DocumentMeta, IngestBatch, IngestStatus, IngestTask,
                        TaskProgressEvent, file_md5, new_id)
from rag.observability.logging import get_logger
from rag.pipeline.base import Pipeline, StepError, StepRegistry
from rag.pipeline.context import IngestContext
from rag.pipeline.engine import WorkflowRegistry

log = get_logger("rag.ingestion")

# 临时文档轻量序列（30s 目标：跳过 LLM 摘要与抽样验证）
_EPHEMERAL_STEPS = ["parse", "outline", "chunk", "embed", "write", "finalize"]

# "待用户确认"的上传暂存超时：超过即丢弃文件（避免大文件长期占临时目录）。
# 取 30 分钟：足够用户看清弹框内容再做决定，又不至于把盘占住。
STAGED_TTL_SEC = 1800


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
        # 待用户确认的重复上传：token → {文件、批次上下文、超时时间}
        self._staged: dict[str, dict] = {}

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
        for token in list(self._staged.keys()):
            self.discard_staged(token)
        for d in self._temp_dirs:
            Path(d).unlink(missing_ok=True)

    # ── 提交 ───────────────────────────────────────────────

    async def submit(self, items: list[IngestItem], tenant_id: str,
                     collection: str, user_id: str = "",
                     source_type: str = "upload",
                     allowed_roles: list[str] | None = None,
                     dedup: str = "auto",
                     ) -> tuple[IngestBatch, list[IngestTask], list[dict]]:
        """提交入库。

        `dedup` 决定"内容已经入库过"时怎么办（同一文件按 **内容 MD5** 判定，
        与文件名/作者/时间等属性无关）：

          · `"auto"`  —— 老行为：命中秒传就直接复用（只写文档别名，不重新解析）。
                         服务器路径导入、CLI、临时文档等**没有人可交互**的入口用这个。
          · `"ask"`   —— 命中则**什么都不写**，把它作为一条"待用户确认"的候选
                         返回（见返回值第三项），由前端弹框让用户决定。上传接口默认。
          · `"force"` —— 完全忽略秒传，一律作为**新文档**完整入库。

        返回 `(batch, 本次创建的任务, 待确认的重复项)`；后两项在 `dedup!="ask"` 时
        第三项恒为空。
        """
        batch = IngestBatch(tenant_id=tenant_id, collection=collection,
                            total=len(items), source_type=source_type,
                            status="running")
        # **批次行推迟到"确实有任务要入队"时再落库**：整批文件全部命中
        # "内容已入库、等用户确认"（或全部被跳过）时，一个任务都不会产生，
        # 却先写一行 batch → 它永远不会被推进，界面上永远是"进行中"的幽灵批次。
        # 先建对象（batch_id 要发给任务行），等第一个任务真要写时再 save。
        batch_saved = False
        created: list[IngestTask] = []
        duplicates: list[dict] = []

        async def _ensure_batch() -> None:
            nonlocal batch_saved
            if not batch_saved:
                await self.s.meta.save_batch(batch)
                batch_saved = True

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

            # 同内容文档查找：秒传复用、或让用户确认（按 dedup 策略决定）
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

            # 秒传仅当①同一集合②权限视图一致（角色集合相同）③解析口径一致，
            # 否则必须重新入库
            #
            # ① 为什么必须比集合：物理索引/集合名 = 前缀 + `collection`，块只存在于
            # 它被写入的那个集合里。同租户换集合上传同一份文件时若命中秒传，新文档
            # 会"秒传成功"（status=done、chunk_count 照抄源文档）却在目标集合里
            # **一条块都没有** —— 检索永远命中不到，而界面上看不出任何异常。
            same_collection = existing is not None and \
                existing.collection == collection
            same_roles = existing is not None and sorted(
                existing.allowed_roles or []) == sorted(allowed_roles or [])
            fingerprint = self._ingest_fingerprint(self.s)
            existing_fp = ((existing.quality_report or {}).get(
                "ingest_fingerprint") if existing else None) or ""
            # 口径比对：只有"两边都有指纹且不一致"才拦下来。历史文档没有指纹
            # （本次改动之前入库的）→ 不改变既有行为，只记一条日志。
            same_engine = not (existing_fp and fingerprint
                               and existing_fp != fingerprint)
            if existing is not None and not same_engine:
                log.info("md5_dedup_engine_changed",
                         doc_id=existing.doc_id, old=existing_fp,
                         new=fingerprint,
                         effect="解析/OCR/版面/VLM/嵌入口径已变更，重新入库")
            if existing is not None and not same_collection:
                log.info("md5_dedup_collection_changed", doc_id=existing.doc_id,
                         src_collection=existing.collection,
                         dst_collection=collection,
                         effect="同文件换集合上传：不做秒传，按新集合重新入库")
            # ── 用户确认闸门（dedup="ask"）──────────────────────────
            # 只要**这个集合里已经有同内容的文档**，就先问一句再动手：
            # 同一份内容被用户主动重传，通常意味着"要按当前口径重跑一遍"，
            # 而秒传会静默复用旧结果 —— 用户看到"一瞬间完成"却不知道库没动。
            # 判定只看内容 MD5（文件名/作者/时间等属性都不参与），
            # 角色或口径不一致时也照样问，把"要不要覆盖/新建"的决定权交回用户。
            if existing is not None and same_collection:
                if dedup == "ask":
                    # 命中的可能是**秒传别名**（它自己没有块，块计数是照抄源文档的）
                    # → 弹框与后续重建都应指向物理源文档，否则用户看到的是
                    # "一篇没有内容的文档"，重建也会落在错的对象上。
                    phys = await self._resolve_physical(existing, tenant_id)
                    duplicates.append({
                        "md5": md5,
                        "filename": item.filename,
                        "collection": collection,
                        "doc_id": phys.doc_id,
                        "alias_doc_id": (existing.doc_id
                                         if phys.doc_id != existing.doc_id
                                         else None),
                        "existing_filename": phys.filename,
                        "uploaded_at": (phys.created_at.isoformat()
                                        if getattr(phys, "created_at", None)
                                        else ""),
                        "updated_at": (phys.updated_at.isoformat()
                                       if getattr(phys, "updated_at", None)
                                       else ""),
                        "chunk_count": phys.chunk_count,
                        "page_count": phys.page_count,
                        "version": getattr(phys, "version", 1) or 1,
                        "same_roles": same_roles,
                        "same_engine": same_engine,
                        "engine_changed": bool(existing_fp and fingerprint
                                               and existing_fp != fingerprint),
                    })
                    continue
            if same_collection and same_roles and same_engine and dedup != "force":
                doc.storage_url = existing.storage_url
                doc.status = IngestStatus.DONE
                # chunk 计数以自身 chunks_meta 为真实口径（白名单映射走
                # file_md5 关联到源文档物理 chunk），此处如实记 0
                doc.chunk_count = existing.chunk_count
                doc.page_count = existing.page_count
                doc.quality_report = {"deduplicated": True,
                                      "source_doc_id": existing.doc_id,
                                      "roles_match": True,
                                      "ingest_fingerprint": fingerprint}
                task.status = IngestStatus.DONE
                task.total_chunks = existing.chunk_count
                task.written_chunks = existing.chunk_count
                task.quality_summary = {"deduplicated": True}
                task.completed_at = datetime.utcnow()
                await _ensure_batch()
                await self.s.meta.upsert_document(doc)
                await self.s.meta.save_task(task)
                created.append(task)
                if not existing_fp:
                    log.info("md5_dedup_no_fingerprint",
                             doc_id=existing.doc_id,
                             note="历史文档未记录解析口径，本次不做口径比对")
                await self._emit_progress(task, 1.0, "MD5 秒传完成")
                continue
            # 同文件不同权限视图 → 正常入库；同名不同 md5 → 旧版软删除
            await self._supersede_old_versions(
                tenant_id, collection, item.filename, md5, exclude=doc.doc_id)

            await _ensure_batch()
            await self.s.meta.upsert_document(doc)
            await self.s.meta.save_task(task)
            created.append(task)
            self._task_files[task.task_id] = item.file_path
            await self.queue.put(task)

        if batch_saved:
            await self.s.meta.recompute_batch(batch.batch_id)
        if duplicates:
            log.info("upload_duplicates_await_confirm",
                     batch_id=batch.batch_id, duplicates=len(duplicates),
                     batch_saved=batch_saved, collection=collection)
        return batch, created, duplicates

    # ── 重复文件确认（暂存 → 确认/丢弃）─────────────────────

    @staticmethod
    def _unlink_staged(path: str) -> None:
        """删除暂存文件 —— **只删系统临时目录里的**

        暂存的上传来自网页上传（`tempfile.mkdtemp(prefix="rag_upload_")`），删掉它
        天经地义。但如果哪天有调用方把"服务器上的真实文件路径"传进来（例如
        `upload-path` 的源文件、或测试脚本直接拿素材路径当暂存项），无脑 unlink 会
        把原始资料删掉 —— 这正是本次开发中真实踩到的坑（测试脚本删掉了素材 PDF，
        所幸对象存储里还有一份）。所以这里按目录兜底：不在临时目录内的一律不删。
        """
        if not path:
            return
        try:
            p = Path(path)
            if not str(p.resolve()).startswith(str(Path(tempfile.gettempdir()))):
                log.warning("staged_unlink_refused", path=str(p),
                            reason="不在系统临时目录内，拒绝删除（避免误删原始文件）")
                return
            p.unlink(missing_ok=True)
        except Exception as e:                          # noqa: BLE001
            log.warning("staged_unlink_failed", path=str(path),
                        error=f"{type(e).__name__}: {e}"[:120])

    async def _resolve_physical(self, doc: DocumentMeta,
                                tenant_id: str) -> DocumentMeta:
        """秒传别名 → 物理源文档

        别名是"同一份内容重复上传"时留下的文档行，它**自己没有块**（块计数照抄源
        文档）。任何"按文档重建/统计内容"的动作都必须落在物理源文档上，否则要么
        重建出一套重复内容，要么给用户看一篇空文档。
        """
        qr = doc.quality_report if isinstance(doc.quality_report, dict) else {}
        src_id = qr.get("source_doc_id")
        if not qr.get("deduplicated") or not src_id:
            return doc
        src = await self.s.meta.get_document(str(src_id), tenant_id)
        return src or doc

    def _purge_staged(self) -> None:
        """清掉超时未确认的暂存上传（连同它们的临时文件）"""
        now = time.time()
        for token in list(self._staged.keys()):
            st = self._staged[token]
            if now - st["created_at"] > STAGED_TTL_SEC:
                self._staged.pop(token, None)
                for it in st["items"]:
                    self._unlink_staged(it.file_path)
                log.info("staged_upload_expired", token=token,
                         waited=f"{int(now - st['created_at'])}s")

    def stage_uploads(self, staged: list[dict], tenant_id: str, collection: str,
                      user_id: str,
                      allowed_roles: list[str] | None) -> str:
        """把"内容已存在、等用户确认"的上传暂存起来（文件留在本地临时目录）

        `staged` 每项：`{"item": IngestItem, "duplicate": {...}}`。
        文件**不重复上传**：字节已经在临时目录里，确认时直接从那里入库。
        超时（`STAGED_TTL_SEC`）未确认即丢弃，避免临时文件长期占盘。
        """
        self._purge_staged()
        token = new_id("stage")
        self._staged[token] = {
            "created_at": time.time(), "tenant_id": tenant_id,
            "collection": collection, "user_id": user_id,
            "allowed_roles": allowed_roles or [],
            "items": [s["item"] for s in staged],
            "duplicates": [s["duplicate"] for s in staged],
        }
        return token

    def get_staged(self, token: str,
                   tenant_id: str = "") -> dict | None:
        self._purge_staged()
        st = self._staged.get(token)
        if st is None:
            return None
        if tenant_id and st["tenant_id"] != tenant_id:
            return None
        return st

    def discard_staged(self, token: str, tenant_id: str = "") -> int:
        """用户取消：丢掉暂存文件，返回丢弃的文件数"""
        st = self.get_staged(token, tenant_id)
        if st is None:
            return 0
        self._staged.pop(token, None)
        for it in st["items"]:
            self._unlink_staged(it.file_path)
        log.info("staged_upload_discarded", token=token, files=len(st["items"]))
        return len(st["items"])

    async def confirm_staged(self, token: str, decisions: dict[str, str],
                             tenant_id: str = "", user_id: str = "",
                             ) -> tuple[list[IngestTask], int, int]:
        """按用户选择处理暂存的重复文件 → (任务列表, 跳过数, 未匹配数)

        `decisions`：`{md5 或文件名: "reingest" | "new" | "skip"}`，缺省视为 `skip`
        （宁可什么都不做，也不要替用户猜）。

          · `reingest` —— **覆盖原文档**：沿用原 `doc_id` 重跑解析链，先清旧块再写
            新块，不产生重复内容。角色等"新信息"按本次上传的取值更新。
          · `new`      —— 作为**新文档**入库（会与已有文档内容重复，慎用）。
          · `skip`     —— 丢弃这次上传。
        """
        st = self.get_staged(token, tenant_id)
        if st is None:
            return [], 0, 0
        self._staged.pop(token, None)          # 一次性令牌：确认即失效
        roles = st["allowed_roles"]
        tasks: list[IngestTask] = []
        skipped = 0
        unmatched = 0
        for item, dup in zip(st["items"], st["duplicates"]):
            action = (decisions.get(dup["md5"])
                      or decisions.get(item.filename) or "skip").lower()
            if action == "reingest":
                task = await self.reingest_document(
                    dup["doc_id"], st["tenant_id"], user_id or st["user_id"],
                    allowed_roles=roles, local_file=item.file_path,
                    source_type="reupload")
                if task is None:
                    unmatched += 1
                else:
                    self._task_files[task.task_id] = item.file_path
                    tasks.append(task)
                    continue                    # 文件已交给重跑任务，不删
            elif action == "new":
                _, created, _ = await self.submit(
                    [item], tenant_id=st["tenant_id"],
                    collection=st["collection"], user_id=user_id or st["user_id"],
                    source_type="reupload", allowed_roles=roles, dedup="force")
                tasks.extend(created)
                continue                        # 文件已交给新任务，不删
            else:
                skipped += 1
            self._unlink_staged(item.file_path)
        log.info("staged_upload_confirmed", token=token, tasks=len(tasks),
                 skipped=skipped, unmatched=unmatched)
        return tasks, skipped, unmatched


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

        # 重建索引 / 重新上传：版本号 +1。文档详情页显示的 "v1/v2" 就是这个字段，
        # 而它原先从创建起再没被改过 —— 重建了三次也还是 v1，用户看不出"这是第几版"。
        if task.source_type in ("reingest", "reupload"):
            doc.version = int(getattr(doc, "version", 1) or 1) + 1

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
        # 口径指纹随上下文走：finalize 步骤会把它写进质量报告，
        # 下次同文件重传时用来判断"引擎口径有没有变"（见 _ingest_fingerprint）
        ctx.meta["ingest_fingerprint"] = self._ingest_fingerprint(s)

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

    # ── 手动重试 / 重建（UI 入口）──────────────────────────

    async def requeue_task(self, task_id: str) -> IngestTask | None:
        """人工重试一个已失败/部分完成的任务，返回刷新后的任务。

        与自动重试（_delayed_requeue）的区别：**不动 retry_count**。自动重试的
        计数是"系统替用户重试了几次"的账，人工点一次重试不该消耗它 —— 否则用户
        连点两次就把退避配额用光，第三次只能改数据库才能再试。
        但也不能让用户无限重试：仍在重试队列里（retrying/pending）的任务直接拒绝，
        避免同一条任务在队列里排两份。
        """
        task = await self.s.meta.get_task(task_id)
        if task is None:
            return None
        if task.status not in (IngestStatus.FAILED, IngestStatus.PARTIAL):
            return None
        # 取回文件：优先本地临时件，其次对象存储（worker 收尾时会清本地件）
        file_path = self._task_files.get(task.task_id)
        if file_path is None or not Path(file_path).exists():
            doc = await self.s.meta.get_document(task.doc_id, task.tenant_id) \
                if task.doc_id else None
            if doc is None:
                return None
            file_path = await self._restore_from_storage(task, doc)
            if file_path is None:
                return None
        self._task_files[task.task_id] = file_path
        task.status = IngestStatus.PENDING
        task.error_message = None
        task.completed_at = None
        await self.s.meta.save_task(task)
        if task.doc_id:
            await self.s.meta.update_doc_status(task.doc_id, IngestStatus.PENDING)
        await self.queue.put(task)
        await self._emit_progress(task, 0.0, "已由管理员重新入队")
        log.info("task_requeued_by_hand", task_id=task.task_id)
        return task

    async def reingest_document(self, doc_id: str, tenant_id: str,
                                user_id: str = "",
                                allowed_roles: list[str] | None = None,
                                local_file: str | None = None,
                                source_type: str = "reingest",
                                ) -> IngestTask | None:
        """按文档重建索引（内容解析链路重跑，不换 doc_id、不重新秒传）。

        为什么不复用 submit()：submit 会给同一份文件算出一个**新 doc_id**，而
        chunk_id 里含 doc_id（`make_chunk_id(tenant, doc, seq, hash)`）—— 新 doc_id
        意味着全新的一批 chunk，旧 chunk 不会因为"内容相同"而被覆盖，只会变成孤儿
        （ES/Milvus 里查得到、MySQL 里没有）。所以重建必须**沿用原 doc_id**：
        同 seq 同内容 → 同 chunk_id → upsert 覆盖，天然幂等。

        也不走 MD5 秒传：重建的语义就是"内容不变但解析链路要重跑"（换了版面引擎、
        改了分块参数、上次解析质量差）。秒传会直接判 DONE 返回，等于什么都没做。

        `allowed_roles` 非 None 时用它**覆盖**文档原有角色：用户重新上传时可能改了
        权限勾选，重建就该按"这次上传的新信息"入库（否则改了也没生效）。
        `local_file` 给"文件已经在本地"的调用方（如重复上传确认）复用，省一次
        对象存储往返；`source_type` 用于区分入口（`reingest` / `reupload`）。
        """
        doc = await self.s.meta.get_document(doc_id, tenant_id)
        if doc is None:
            return None
        if doc.status == IngestStatus.SUPERSEDED:
            return None                     # 已被新版本覆盖，重建旧版没有意义
        # 秒传别名文档：它自己没有块（chunk 计数是照抄源文档的），重建它会在
        # **同一个集合里**再产出一整套同内容的块 —— 原件那套还在，检索就命中双份。
        # 因此把重建目标改到物理源文档（质量报告里记着 source_doc_id）。
        qr = doc.quality_report if isinstance(doc.quality_report, dict) else {}
        if qr.get("deduplicated") and qr.get("source_doc_id"):
            src = await self._resolve_physical(doc, tenant_id)
            if src.doc_id == doc.doc_id:            # 源文档已不存在
                log.warning("reingest_alias_source_missing", doc_id=doc_id,
                            source_doc_id=str(qr.get("source_doc_id")),
                            effect="源文档不存在，重建请求被忽略")
                return None
            log.info("reingest_retarget_alias", alias_doc_id=doc_id,
                     source_doc_id=src.doc_id,
                     reason="秒传别名没有自己的块，重建必须落在物理文档上")
            doc = src
            doc_id = src.doc_id

        file_path = ""
        fname = doc.filename or (doc.doc_id + ".bin")
        if local_file and Path(local_file).exists():
            # 调用方手上已有文件（重复上传确认：字节就在暂存临时目录里），
            # 不再走对象存储往返 —— 大文件上这一步省的是实打实的时间
            file_path = local_file
        elif self.s.storage is not None and doc.storage_url:
            try:
                content = await self.s.storage.get(doc.storage_url)
                suffix = Path(fname).suffix or ".bin"
                fd, path = tempfile.mkstemp(suffix=suffix,
                                            prefix=f"reingest_{doc.doc_id}_")
                self._temp_dirs.add(path)
                with open(fd, "wb") as f:
                    f.write(content)
                file_path = path
            except Exception as e:
                log.warning("reingest_fetch_failed", doc_id=doc.doc_id,
                            error=str(e))
        if not file_path:
            await self.s.meta.update_doc_status(doc_id, IngestStatus.FAILED)
            return None

        # 本次上传携带的新信息：角色勾选按本次取值覆盖（口径指纹由 finalize 重新记录）
        if allowed_roles is not None:
            doc.allowed_roles = list(allowed_roles)
            doc.updated_at = datetime.utcnow()

        task = IngestTask(tenant_id=tenant_id, collection=doc.collection,
                          filename=fname, doc_id=doc.doc_id,
                          file_md5=doc.file_md5, file_type=doc.file_type,
                          source_type=source_type, submitted_by=user_id)
        # 原文件已在对象存储里（doc.storage_url 有效）→ 跳过 MinIO 那一段检查点
        task.checkpoint.minio = True
        if allowed_roles is not None:
            await self.s.meta.upsert_document(doc)
        await self.s.meta.update_doc_status(doc_id, IngestStatus.PENDING)
        await self.s.meta.save_task(task)
        self._task_files[task.task_id] = file_path
        await self.queue.put(task)
        await self._emit_progress(task, 0.0, "已按文档重建入队")
        log.info("doc_reingested", doc_id=doc_id, task_id=task.task_id,
                 source_type=source_type,
                 roles_changed=allowed_roles is not None)
        return task

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
        """PDF 快速预检：**有没有文本层** → text / scanned / hybrid（决定 workflow）

        判据是"页面有没有文本层"，不是字符密度：密度是尺度相关量，正常中文页的密度
        约为旧阈值的 1/33，用它会(1)把文字页判成扫描页(2)只在文字页上判——两头都会
        把能用的内容丢掉。这里采样首尾各 3 页：全有文本层 → text、全无 → scanned、
        混合 → **hybrid**（以前 `_quick_scan_check` 只返回 text/scanned，`pdf_hybrid`
        是条永远走不到的死 workflow，混合型文档被当成"纯文字"直接跳过 OCR 质检）。

        采样判偏不丢内容：真正的逐页分流在解析器里（`doc_parser._parse_sync` 每页都判
        "有没有文本层"再决定要不要 OCR），这里只决定用哪条步骤链，所以采样保守即可。
        """
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            n = len(reader.pages)
            if not n:
                return "text"
            idx = sorted({0, 1, 2, n - 1, n - 2, n - 3} & set(range(n)))
            marks: list[bool] = []
            for i in idx:
                try:
                    text = (reader.pages[i].extract_text() or "").strip()
                except Exception:                       # noqa: BLE001
                    text = ""
                marks.append(len(text) > 20)            # >20 字符 = 这页有文本层
            if all(marks):
                return "text"
            if not any(marks):
                return "scanned"
            return "hybrid"
        except Exception:
            return "text"

    @staticmethod
    def _ingest_fingerprint(s) -> str:
        """**影响入库内容**的引擎口径指纹（换了口径 → 不允许秒传）

        MD5 秒传只认文件字节：同一份文件在"换了 OCR 服务 / 版面服务 / VLM /
        嵌入模型"之后再传，命中的是**旧引擎产出的 chunk** —— 索引压根没更新，
        用户却以为重新入库了一次（旧口味的解析结果被当成新的，界面无任何提示）。

        所以把口径拼成一个短指纹：入库时记进质量报告，下次同文件重传时比对；
        两边都有指纹且不一致 → 正常重新入库。口径没变才省下这次重跑。
        指纹不是安全凭据，只是"同一批解析参数"的等价性标记，取 sha256 前 16 位即可。
        """
        import hashlib
        from rag.adapters.doc_parse import resolve_base_url
        from rag.models import CHUNK_BUILD_VERSION, EMBED_TEXT_VERSION
        # 参与向量化的文本拼法也算"口径"：改了拼法（如加入摘要/关键词）之后，
        # 旧向量与新向量不可比，必须让重传触发重新入库（见 EMBED_TEXT_VERSION）
        parts: list[str] = [f"embed_text={EMBED_TEXT_VERSION}",
                            # 块正文/元数据的生成逻辑版本（分块切分、表格块正文、页码
                            # 归属、图区裁剪…）：见 CHUNK_BUILD_VERSION 的说明
                            f"chunk_build={CHUNK_BUILD_VERSION}"]
        dp = getattr(s.config, "doc_parse", None)
        if dp is not None:
            for cap in ("ocr", "layout-parsing"):
                parts.append(f"{cap}={resolve_base_url(dp, cap) or '-'}")
            parts.append(f"pdf_batch={getattr(dp, 'max_pages_per_request', 0)}")
        for name in ("vlm", "embedding"):
            sec = getattr(s.config, name, None)
            if sec is None:
                continue
            for attr in ("base_url", "model"):
                v = getattr(sec, attr, None)
                if v:
                    parts.append(f"{name}.{attr}={v}")
        if not parts:
            return ""
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
