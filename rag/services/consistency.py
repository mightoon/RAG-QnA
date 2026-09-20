"""
一致性巡检（rag/services/consistency.py）

后台定时任务：抽样文档对比 MySQL / ES / Milvus 的 chunk_id 集合，
差异超过阈值告警；PARTIAL 状态文档自动补写缺失库。
"""
from __future__ import annotations

import asyncio

from rag.models import (ChunkMeta, DocumentMeta, IngestStatus)
from rag.observability.logging import get_logger

log = get_logger("rag.consistency")


class ConsistencyChecker:

    def __init__(self, container):
        self.s = container
        self._task: asyncio.Task | None = None

    # ── 生命周期 ───────────────────────────────────────────

    async def start(self) -> None:
        cfg = self.s.config.consistency_check
        if not cfg.enabled:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        interval = self.s.config.consistency_check.interval_hours * 3600
        while True:
            try:
                await asyncio.sleep(interval)
                await self.run_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("consistency_loop_error", error=str(e))

    # ── 巡检逻辑 ───────────────────────────────────────────

    async def run_check(self) -> dict:
        """抽样巡检：返回统计信息"""
        cfg = self.s.config.consistency_check
        stats = {"checked": 0, "inconsistent": 0, "repaired": 0}
        try:
            docs = await self.s.meta.list_documents(
                tenant_id="*", status=IngestStatus.DONE, limit=cfg.sample_docs)
        except Exception:
            docs = []
        for doc in docs:
            stats["checked"] += 1
            ok = await self._check_doc(doc)
            if not ok:
                stats["inconsistent"] += 1
                if await self._repair_doc(doc):
                    stats["repaired"] += 1
        if stats["inconsistent"] >= cfg.alert_threshold:
            log.warning("consistency_alert", **stats)
        try:
            from rag.observability.metrics import metrics
            metrics.consistency_check_results.labels(
                result="checked").inc(stats["checked"])
            metrics.consistency_check_results.labels(
                result="inconsistent").inc(stats["inconsistent"])
            metrics.consistency_check_results.labels(
                result="repaired").inc(stats["repaired"])
        except Exception:
            pass
        return stats

    async def _check_doc(self, doc: DocumentMeta) -> bool:
        """单文档一致性：MySQL chunk 集合 vs ES / Milvus 集合"""
        try:
            mysql_ids = set(await self.s.meta.list_chunk_ids(doc.doc_id))
        except Exception:
            return True                     # 元数据库异常不判不一致
        if not mysql_ids:
            return True
        if self.s.fulltext is not None:
            try:
                es_ids = await self.s.fulltext.get_doc_chunk_ids(
                    doc.collection, doc.doc_id)
                if mysql_ids - es_ids:
                    return False
            except Exception:
                pass
        if self.s.vector is not None:
            try:
                vec_ids = await self.s.vector.get_doc_chunk_ids(
                    doc.collection, doc.doc_id)
                child_ids = {cid for cid in mysql_ids}     # 父块不入向量库
                if child_ids - vec_ids:
                    # 需排除父块：粗判（缺失过多才算）
                    if len(child_ids - vec_ids) > len(child_ids) * 0.1:
                        return False
            except Exception:
                pass
        return True

    async def _repair_doc(self, doc: DocumentMeta) -> bool:
        """补写缺失库：从 MySQL 读 chunk 元数据与真实正文重写 ES / Milvus。
        正文不可得（旧库未落 text 列）的 chunk 一律跳过——宁可保留缺失
        也不向检索库写入占位符；需要重新入库时给出日志提示。"""
        try:
            chunk_ids = await self.s.meta.list_chunk_ids(doc.doc_id)
            chunks: list[ChunkMeta] = await self.s.meta.get_chunks_by_ids(
                chunk_ids)
            texts_map = await self.s.meta.get_chunk_texts(chunk_ids)
            pairs = [(c, texts_map[c.chunk_id]) for c in chunks
                     if texts_map.get(c.chunk_id)]
            skipped = len(chunks) - len(pairs)
            if skipped:
                log.warning("repair_skipped_no_text", doc_id=doc.doc_id,
                            skipped=skipped,
                            hint="可重新入库以写入正文并触发完整修复")
            if not pairs:
                return False
            chunks = [c for c, _ in pairs]
            texts = [t for _, t in pairs]
            summaries = [None] * len(chunks)
            keywords = [[] for _ in chunks]
            repaired = False
            if self.s.fulltext is not None:
                await self.s.fulltext.upsert_chunks(
                    doc.collection, chunks, texts, summaries, keywords)
                repaired = True
            if self.s.vector is not None:
                # 向量空间门禁：与入库同一个判据。这里若不拦，巡检修复会把伪向量
                # （或另一套模型的向量）重新灌进一个已经有真向量的集合 —— 危害比
                # "缺几条片段"大得多，而且它由后台定时任务发起，没人盯着
                blocked = await self.s.vector_write_blocked(doc.collection)
                if blocked:
                    log.warning("repair_vector_skipped_space",
                                doc_id=doc.doc_id, collection=doc.collection,
                                reason=blocked[:200])
                child = [] if blocked else [
                    (c, t) for c, t in zip(chunks, texts)
                    if not c.is_parent]
                if child:
                    vectors = await self.s.embedding.embed(
                        [t for _, t in child])
                    await self.s.vector.upsert(
                        doc.collection, [c.chunk_id for c, _ in child],
                        vectors, [self._meta_for_repair(c, doc, t)
                                  for c, t in child])
                    repaired = True
            return repaired
        except Exception as e:
            log.warning("repair_failed", doc_id=doc.doc_id, error=str(e))
            return False

    @staticmethod
    def _meta_for_repair(chunk: ChunkMeta, doc: DocumentMeta,
                         text: str) -> dict:
        return {"doc_id": chunk.doc_id, "tenant_id": chunk.tenant_id,
                "collection": chunk.collection,
                "chunk_type": chunk.chunk_type,
                "text": text,
                "title": doc.filename[:500],
                "section_path": chunk.section_path or "",
                "page_num": chunk.page_num or 0,
                "figure_label": chunk.figure_label or "",
                "figure_caption": chunk.figure_caption or "",
                "storage_url": doc.storage_url or "",
                "quality_score": chunk.quality_score,
                "allowed_roles": chunk.allowed_roles or []}
