"""
入库步骤：增强与写入（rag/pipeline/steps/ingest_write.py）

- EnrichStep   LLM 批量生成摘要/关键词/实体（失败降级正则提取）
- EmbedStep    子块向量化（bge-m3）
- WriteStep    两阶段写入：MySQL（必须）→ ES → Milvus → Graph，
               检查点逐库打钩，断点续写，单库失败降级不中断
- VerifyStep   入库后抽样验证（各库可检索性）
- FinalizeStep 状态落库 + 质量报告 + 通知
"""
from __future__ import annotations

import asyncio
import json
import random
import re

from rag.models import (ChunkMeta, IngestStatus, QualityIssue)
from rag.observability.logging import get_logger
from rag.pipeline.base import PipelineStep, StepRegistry
from rag.pipeline.context import IngestContext

log = get_logger("rag.steps.write")

_ENRICH_PROMPT = """分析以下文本块，输出 JSON（不要其他内容）：
{{"summary": "不超过80字的摘要", "keywords": ["3-5个关键词/型号/专有名词"], "entities": ["文中出现的实体名"]}}

文本：
{text}"""


@StepRegistry.register("enrich")
class EnrichStep(PipelineStep):
    """LLM 批量增强：摘要（ES 高权重字段）/ 关键词 / 实体"""

    async def execute(self, ctx: IngestContext) -> None:
        cfg = ctx.services.config.pipeline
        if not ctx.chunks:
            return
        llm = ctx.services.llm
        n = len(ctx.chunks)
        ctx.summaries = [None] * n
        ctx.keywords = [[] for _ in range(n)]
        all_entities: dict[str, dict] = {}

        sem = asyncio.Semaphore(ctx.services.config.llm.max_concurrency)

        async def enrich_one(i: int) -> None:
            text = ctx.chunk_texts[i]
            if ctx.chunks[i].is_parent or len(text) < 30:
                ctx.keywords[i] = self._regex_keywords(text)
                return
            try:
                async with sem:
                    out = await llm.generate(
                        [{"role": "user",
                          "content": _ENRICH_PROMPT.format(text=text[:1500])}],
                        task="summary", temperature=0.1, max_tokens=200)
                data = json.loads(self._extract_json(out))
                ctx.summaries[i] = (data.get("summary") or "")[:200] or None
                ctx.keywords[i] = [str(k)[:32] for k in
                                   (data.get("keywords") or [])][:5]
                for e in (data.get("entities") or [])[:10]:
                    name = str(e).strip()
                    if 1 < len(name) <= 64:
                        all_entities[name] = {"name": name,
                                              "entity_type": "generic"}
            except Exception:
                ctx.keywords[i] = self._regex_keywords(text)

        await asyncio.gather(*(enrich_one(i) for i in range(n)),
                             return_exceptions=True)

        # 实体归一化（EntityLinker 规则阶段）+ 共现关系
        if cfg.enable_entities and all_entities:
            try:
                linked = await ctx.services.entity_linker.link_batch(
                    ctx.doc.tenant_id, list(all_entities.values()))
                ctx.entities = linked
            except Exception:
                ctx.entities = list(all_entities.values())
            ctx.relations = self._cooccurrence_relations(
                ctx.entities, ctx.chunk_texts)

        # 质量评分：摘要生成成功率
        ok = sum(1 for s in ctx.summaries if s)
        target = sum(1 for i in range(n) if not ctx.chunks[i].is_parent
                     and len(ctx.chunk_texts[i]) >= 30)
        if target and ok / target < 0.5:
            ctx.add_warning(f"摘要生成成功率低 ({ok}/{target})")
        await ctx.report("embedding", 0.5,
                         f"增强完成：{ok} 摘要 / {len(ctx.entities)} 实体")

    @staticmethod
    def _extract_json(text: str) -> str:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return m.group(0) if m else "{}"

    @staticmethod
    def _regex_keywords(text: str) -> list[str]:
        """降级关键词提取：英文词 + 中文 2-gram 高频"""
        words = re.findall(r"[A-Za-z][A-Za-z0-9\-_]{2,}", text)
        seen = list(dict.fromkeys(words))[:5]
        return seen

    @staticmethod
    def _cooccurrence_relations(entities: list[dict],
                                texts: list[str]) -> list[dict]:
        """共现关系：同一 chunk 中出现的实体对（图谱 V1 规则阶段）"""
        if len(entities) < 2:
            return []
        names = [e["name"] for e in entities]
        relations = []
        for text in texts:
            present = [n for n in names if n in text]
            for i in range(len(present)):
                for j in range(i + 1, len(present)):
                    relations.append({"source": present[i],
                                      "target": present[j],
                                      "relation": "CO_OCCURS"})
                    if len(relations) >= 200:
                        return relations
        return relations


@StepRegistry.register("embed")
class EmbedStep(PipelineStep):
    """子块向量化（父块不参与 ANN 检索，不入向量库）"""

    async def execute(self, ctx: IngestContext) -> None:
        if not ctx.chunks or ctx.services.vector is None:
            return
        # 收集所有非父块 (chunk_id, text)
        items = [(c.chunk_id, ctx.chunk_texts[i])
                 for i, c in enumerate(ctx.chunks) if not c.is_parent]
        batch = ctx.services.config.embedding.batch_size
        for lo in range(0, len(items), batch):
            sub = items[lo:lo + batch]
            try:
                vectors = await ctx.services.embedding.embed(
                    [t for _, t in sub])
                for (cid, _), vec in zip(sub, vectors):
                    ctx.embeddings[cid] = vec
            except Exception as e:
                raise RuntimeError(f"向量化失败: {e}") from e
        await ctx.report("embedding", 0.6,
                         f"向量化完成：{len(ctx.embeddings)} 块")


@StepRegistry.register("write")
class WriteStep(PipelineStep):
    """
    两阶段写入 + 检查点断点续写：
    阶段一（必须成功）：MySQL chunks_meta / table_data
    阶段二（尽力而为）：ES → Milvus → Graph，单库失败降级记 warning
    """

    async def execute(self, ctx: IngestContext) -> None:
        s = ctx.services
        cp = ctx.task.checkpoint
        await ctx.report("writing", 0.65, "开始写入")

        # ── 阶段一：MySQL（元数据，必须）────────────────────
        if not cp.mysql:
            # 正文一并落 MySQL：供父子回补与一致性巡检修复取真文
            texts = {c.chunk_id: ctx.chunk_texts[i]
                     for i, c in enumerate(ctx.chunks)
                     if i < len(ctx.chunk_texts)}
            await s.meta.upsert_chunks(ctx.chunks, texts=texts)
            if ctx.tables:
                await s.meta.upsert_table_data(
                    [t for t in ctx.tables if t.chunk_id])
            cp.mysql = True
            await s.meta.save_task(ctx.task)
            ctx.task.written_chunks = len(ctx.chunks)

        # ── 阶段二：ES / Milvus / Graph（可降级）────────────
        if not cp.es and s.fulltext is not None:
            try:
                await s.fulltext.upsert_chunks(
                    ctx.doc.collection, ctx.chunks, ctx.chunk_texts,
                    ctx.summaries, ctx.keywords)
                cp.es = True
            except Exception as e:
                ctx.add_warning(f"ES 写入失败: {e}")
                ctx.quality.issues.append(QualityIssue(
                    stage="post_write", severity="high", code="es_write_failed",
                    message=str(e)[:200], action="partial"))

        if not cp.milvus and s.vector is not None and ctx.embeddings:
            try:
                ids = list(ctx.embeddings.keys())
                id_set = set(ids)
                metas = []
                for c, text in zip(ctx.chunks, ctx.chunk_texts):
                    if c.chunk_id not in id_set:
                        continue
                    metas.append({
                        "doc_id": c.doc_id, "tenant_id": c.tenant_id,
                        "collection": c.collection, "chunk_type": c.chunk_type,
                        "text": text[:4000],
                        "title": ctx.doc.filename[:500],
                        "section_path": c.section_path or "",
                        "page_num": c.page_num or 0,
                        "figure_label": c.figure_label or "",
                        "figure_caption": c.figure_caption or "",
                        "storage_url": ctx.doc.storage_url or "",
                        "quality_score": c.quality_score,
                        "allowed_roles": c.allowed_roles or [],
                    })
                await s.vector.upsert(
                    ctx.doc.collection, ids,
                    [ctx.embeddings[i] for i in ids], metas)
                cp.milvus = True
            except Exception as e:
                ctx.add_warning(f"Milvus 写入失败: {e}")
                ctx.quality.issues.append(QualityIssue(
                    stage="post_write", severity="high",
                    code="milvus_write_failed",
                    message=str(e)[:200], action="partial"))

        if not cp.graph and s.graph is not None and ctx.entities:
            try:
                await s.graph.upsert_entities(
                    ctx.doc.tenant_id, ctx.doc.doc_id, ctx.entities)
                if ctx.relations:
                    await s.graph.upsert_relations(
                        ctx.doc.tenant_id, ctx.doc.doc_id, ctx.relations)
                cp.graph = True
            except Exception as e:
                ctx.add_warning(f"图谱写入失败: {e}")

        await s.meta.save_task(ctx.task)
        await ctx.report("writing", 0.85, "写入完成")


@StepRegistry.register("verify")
class VerifyStep(PipelineStep):
    """入库后抽样验证：随机抽 chunk_id 核对 ES / Milvus 存在性"""

    async def execute(self, ctx: IngestContext) -> None:
        s = ctx.services
        sample_size = s.config.ingest.verify_sample_size
        child_ids = [c.chunk_id for c in ctx.chunks if not c.is_parent]
        if not child_ids:
            return
        sample = random.sample(child_ids, min(sample_size, len(child_ids)))
        sample_set = set(sample)

        if s.fulltext is not None and ctx.task.checkpoint.es:
            try:
                found = await s.fulltext.get_doc_chunk_ids(
                    ctx.doc.collection, ctx.doc.doc_id)
                missing = sample_set - found
                if missing:
                    ctx.add_warning(f"ES 抽样缺失 {len(missing)} 块")
            except Exception:
                pass
        if s.vector is not None and ctx.task.checkpoint.milvus:
            try:
                found = await s.vector.get_doc_chunk_ids(
                    ctx.doc.collection, ctx.doc.doc_id)
                missing = sample_set - found
                if missing:
                    ctx.add_warning(f"Milvus 抽样缺失 {len(missing)} 块")
            except Exception:
                pass


@StepRegistry.register("finalize")
class FinalizeStep(PipelineStep):
    """收尾：质量报告汇总 + 文档状态落库（done / partial）"""

    async def execute(self, ctx: IngestContext) -> None:
        s = ctx.services
        q = ctx.quality
        q.doc_id = ctx.doc.doc_id
        # Chunk 质量分布（基于 token 数、类型与质量分）
        for c in ctx.chunks:
            if c.is_parent:
                continue
            if c.token_count < 20 or c.quality_score < 0.3:
                q.low_quality += 1
            elif (c.chunk_type in ("table", "image_caption")
                  or c.token_count >= 100) and c.quality_score >= 0.6:
                q.high_quality += 1
            else:
                q.medium_quality += 1
        try:
            from rag.observability.metrics import metrics
            metrics.chunk_quality.labels(bucket="high").inc(q.high_quality)
            metrics.chunk_quality.labels(bucket="medium").inc(q.medium_quality)
            metrics.chunk_quality.labels(bucket="low").inc(q.low_quality)
        except Exception:
            pass
        degraded = bool(ctx.warnings)
        status = IngestStatus.PARTIAL if degraded else IngestStatus.DONE
        q.summary = (f"共 {len(ctx.chunks)} 块（高质 {q.high_quality} / "
                     f"中质 {q.medium_quality} / 低质 {q.low_quality}）"
                     + (f"；降级项 {len(ctx.warnings)}" if degraded else ""))

        ctx.doc.quality_report = q.model_dump(mode="json")
        ctx.doc.status = status
        ctx.doc.chunk_count = len(ctx.chunks)
        await s.meta.upsert_document(ctx.doc)
        ctx.task.quality_summary = {
            "high": q.high_quality, "medium": q.medium_quality,
            "low": q.low_quality, "warnings": ctx.warnings[:20],
            "ocr_avg_confidence": q.ocr_avg_confidence}
        ctx.task.status = status
        await s.meta.save_task(ctx.task)
        await ctx.report(status.value, 1.0, q.summary)
