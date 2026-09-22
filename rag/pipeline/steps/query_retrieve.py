"""
检索步骤（rag/pipeline/steps/query_retrieve.py）

RetrieveStep：元数据前置过滤 + 多集合五路并行召回
  - 白名单失败时 fail-closed（空白名单），权限不下放；
  - 按 MySQL 中真实存在的逻辑集合检索（多物理索引/集合联合）；
  - 向量路支持 standalone/semantic + rewrites[:3]（+可选 sub_queries）多查询；
  - BM25 同义词独立查询按 0.85 降权合并；
  - ES/Milvus 过滤中注入 allowed_roles 作为数据层硬过滤兜底。

MergeStep：加权 RRF 跨路融合（route_weights）+ 临时文档加分 +
           chunk 质量分降权 + 指纹去重 + 语义去重（0.92）
RerankStep：重排（尊重 rerank_enabled / 开关）+ standalone_query 打分 +
           父子回补（命中子块取父块正文填 parent_content）
"""
from __future__ import annotations

import asyncio
import math
import threading
from collections import OrderedDict

from rag.config.models import (is_http_url, rerank_api_endpoint,
                               resolve_rerank_model_path)
from rag.models import RetrievedChunk
from rag.observability.logging import get_logger
from rag.pipeline.base import PipelineStep, StepRegistry, StepError
from rag.pipeline.context import QueryContext

log = get_logger(__name__)

EPHEMERAL_COLLECTION = "__ephemeral__"
_KW_FIELDS = ["content.kw_exact^2", "keywords^2"]


def _default_route_cfg(services) -> dict:
    r = services.config.retrieval
    unified = r.top_k_per_path
    return {
        "vector": {"top_k": r.vector_top_k or unified},
        "bm25": {"top_k": r.bm25_top_k or unified},
        "kw_exact": {"top_k": r.kw_exact_top_k or unified},
        "graph": {"enabled": r.enable_graph,
                  "top_k": unified,
                  "hops": r.graph_hops},
        "ephemeral": {"top_k": r.ephemeral_top_k or unified},
    }


# ──────────────────────────────────────────────────────────────────
# 融合公共逻辑（MergeStep 与 SelfEvalStep 复用）
# ──────────────────────────────────────────────────────────────────

def rrf_merge(ctx: QueryContext) -> list[RetrievedChunk]:
    """按 route_weights 加权 RRF 融合，含临时文档加分与质量分降权"""
    cfg = ctx.services.config.retrieval
    weights = (ctx.plan.route_weights if ctx.plan else {}) or {}
    scores: dict[str, float] = {}
    info: dict[str, RetrievedChunk] = {}
    paths_hit: dict[str, list[str]] = {}
    for path, chunks in ctx.candidates.items():
        if not chunks:
            continue
        w = float(weights.get(path, 1.0))
        for rank, c in enumerate(chunks):
            key = c.chunk_id
            contrib = w * 1.0 / (cfg.rrf_k + rank + 1)
            scores[key] = scores.get(key, 0.0) + contrib
            if key not in info:
                info[key] = c
                paths_hit[key] = []
            paths_hit[key].append(path)
    for key, c in info.items():
        sc = scores[key]
        if c.is_ephemeral:
            sc *= cfg.ephemeral_score_boost          # 临时文档 ×1.2
        q = max(0.2, min(1.0, float(c.quality_score or 1.0)))
        c.score = sc * q                              # 低质 chunk 降权
        c.metadata["contributing_paths"] = paths_hit.get(key, [])
        c.metadata["rrf_raw"] = scores[key]
    merged = sorted(info.values(), key=lambda c: c.score, reverse=True)
    return merged


def text_dedup(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """快速指纹道：前 120 字符归一化指纹"""
    seen: set[str] = set()
    out: list[RetrievedChunk] = []
    for c in chunks:
        fp = c.text.strip().lower().replace(" ", "")[:120]
        if fp in seen:
            continue
        seen.add(fp)
        out.append(c)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


async def semantic_dedup(ctx: QueryContext,
                         chunks: list[RetrievedChunk],
                         cap: int = 60) -> list[RetrievedChunk]:
    """语义去重：embedding 两两余弦 ≥ dedup_similarity 视为重复，保留高分者"""
    cfg = ctx.services.config.retrieval
    if not ctx.services.embedding or len(chunks) <= 1:
        return chunks
    head = chunks[:cap]
    tail = chunks[cap:]
    try:
        vectors = await ctx.services.embedding.embed(
            [c.text[:512] for c in head])
    except Exception as e:
        log.warning("semantic_dedup_embed_failed", error=str(e))
        return chunks
    keep: list[int] = []
    for i, c in enumerate(head):
        dup = False
        for j in keep:
            if _cosine(vectors[i], vectors[j]) >= cfg.dedup_similarity:
                dup = True
                break
        if not dup:
            keep.append(i)
    return [head[i] for i in keep] + tail


async def merge_candidates(ctx: QueryContext) -> None:
    """融合 + 双重去重 + 截断；结果写入 ctx.merged"""
    merged = rrf_merge(ctx)
    merged = text_dedup(merged)
    merged = await semantic_dedup(ctx, merged)
    final_cap = (ctx.services.config.retrieval.final_top_n or 6) * 2
    ctx.merged = merged[:final_cap]
    await ctx.emit({"type": "meta", "stage": "merged",
                    "candidates": len(ctx.merged)})
    # 召回分数指标：各路 top1 归一化观测
    try:
        from rag.observability.metrics import metrics
        for path, lst in ctx.candidates.items():
            if not lst:
                continue
            mx = max((c.score for c in lst), default=0.0) or 1.0
            metrics.recall_scores.labels(path=path).observe(
                min(1.0, max(0.0, lst[0].score / mx)))
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────


class RetrieveStep(PipelineStep):

    async def execute(self, ctx: QueryContext) -> None:
        s = ctx.services
        plan = ctx.plan
        if plan is None:
            raise StepError("retrieve", "QueryPlan 未生成", retryable=False)
        route_cfg = _default_route_cfg(s)
        weights = plan.route_weights or {}
        # 向量路的开启条件之一是"库里的向量与当前向量模型同源"，那份结论由
        # sync_vector_space 预先算好（enabled_paths 是同步判据，不能 await 向量库）。
        # 指纹一致时这里只读缓存 + 比一次字符串，正常部署没有额外 RPC，但它保证
        # 判据永远反映"此刻"（例如刚在配置页换过向量模型）。
        if getattr(s, "vector", None) is not None:
            await s.sync_vector_space("default")
        # ── 系统启用 ∩ 用户选择 ∩ LLM 分配 ─────────────────────
        sys_enabled = set(s.enabled_paths())
        weights = {k: v for k, v in weights.items()
                   if k in sys_enabled or k == "ephemeral"}
        user_paths = ctx.meta.get("user_paths")
        if user_paths:
            chosen = set(user_paths)
            # 临时文档路由：会话内有临时文档时始终激活，不受用户勾选限制
            if ctx.session.ephemeral_doc_ids:
                chosen.add("ephemeral")
            weights = {k: (v if k in chosen else 0.0)
                       for k, v in weights.items()}
        active = [p for p, w in weights.items() if w > 0]
        mf = plan.metadata_filter
        if not active:
            log.info("no_active_routes")
            return

        # ── 目标物理集合（多集合联合检索）──────────────────────────
        target_cols = await self._target_collections(ctx, mf)
        ctx.meta["target_collections"] = target_cols
        filter_base = {"tenant_id": ctx.user.tenant_id,
                       "allowed_roles": list(ctx.user.roles or [])}

        # ── 元数据前置过滤（白名单）；失败 fail-closed ────────────────
        whitelist: list[str] | None = None
        ephemeral_allowed = EPHEMERAL_COLLECTION in (mf.collections or ["*"]) \
            or "*" in (mf.collections or [])
        if "ephemeral" not in active:
            try:
                whitelist = await s.meta.query_chunk_ids(
                    ctx.user.tenant_id,
                    collections=[c for c in (mf.collections or [])
                                 if c != EPHEMERAL_COLLECTION] or None,
                    file_types=mf.file_types,
                    date_from=mf.date_from, date_to=mf.date_to,
                    allowed_roles=list(ctx.user.roles or []),
                    extra_hints=mf.extra_hints or None)
            except Exception as e:
                log.error("metadata_prefilter_failed", error=str(e))
                whitelist = []           # fail-closed：权限不下放
                ctx.meta["prefilter_failed"] = True
            if not whitelist:
                log.info("chunk_whitelist_empty")
                return
            filter_base["chunk_ids"] = whitelist
            ctx.meta["whitelist"] = whitelist

        tasks = [
            self._route(ctx, name, weights[name], filter_base,
                        target_cols, route_cfg)
            for name in active
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, StepError):
                log.error("retrieve_route_error", error=str(r))

    async def _target_collections(self, ctx: QueryContext, mf) -> list[str]:
        """解析本次查询需要覆盖的物理集合（非临时）"""
        s = ctx.services
        allowed = [c for c in (mf.collections or [])
                   if c != EPHEMERAL_COLLECTION]
        if "*" in allowed or not allowed:
            try:
                cols = await s.meta.list_collections(ctx.user.tenant_id)
            except Exception as e:
                log.warning("list_collections_failed", error=str(e))
                cols = []
            cols = [c for c in cols if c != EPHEMERAL_COLLECTION]
            return cols or ["default"]
        return allowed

    async def _route(self, ctx: QueryContext, name: str, weight: float,
                     filter_base: dict, target_cols: list[str],
                     route_cfg: dict) -> None:
        s = ctx.services
        plan = ctx.plan
        timeout = s.config.retrieval.route_timeout_seconds
        try:
            coro = {
                "vector": lambda: self._vector(ctx, filter_base, target_cols,
                                                route_cfg["vector"]),
                "bm25": lambda: self._bm25(ctx, filter_base, target_cols,
                                            route_cfg["bm25"]),
                "kw_exact": lambda: self._kw(ctx, filter_base, target_cols,
                                              route_cfg["kw_exact"]),
                "graph": lambda: self._graph(ctx, route_cfg.get("graph", {})),
                "ephemeral": lambda: self._ephemeral(ctx, filter_base,
                                                      route_cfg["ephemeral"]),
            }[name]()
            chunks = await asyncio.wait_for(coro, timeout=timeout)
            ctx.candidates[name] = chunks
            log.info("route_done", route=name, hits=len(chunks))
        except asyncio.TimeoutError:
            log.warning("route_timeout", route=name, timeout=timeout)
        except KeyError:
            log.error("unknown_route", route=name)
        except Exception as e:
            log.error("route_failed", route=name, error=str(e))

    # ── 向量路：多改写分别向量化，跨集合合并 ─────────────────────
    async def _vector(self, ctx: QueryContext, filter_base: dict,
                      target_cols: list[str], cfg: dict
                      ) -> list[RetrievedChunk]:
        s = ctx.services
        plan = ctx.plan
        if not s.embedding or not s.vector:
            return []
        queries = [plan.semantic_query]
        if s.config.pipeline.enable_query_rewrite:
            for t in (plan.rewrites or [])[:3]:
                if t and t != plan.semantic_query and t not in queries:
                    queries.append(t)
        if s.config.pipeline.enable_sub_query and plan.sub_queries:
            for t in plan.sub_queries:
                if t and t not in queries:
                    queries.append(t)
        queries = queries[:5]

        vectors = await asyncio.gather(
            *[s.embedding.embed_query(q) for q in queries])
        top_k = cfg.get("top_k", 20)

        async def _one_collection(col: str) -> list[RetrievedChunk]:
            # 逐集合过向量空间门禁：这个集合里的向量与当前向量模型不同源时，
            # 宁可不查（ANN 永远返回 top_k，RRF 又只按 rank 计票，噪声会真的
            # 挤掉正确答案）。按集合判而不是整条路判：一套部署里可能只有某个
            # 知识域的集合是旧的，其它集合照常可用。
            if not await s.vector_read_ok(col):
                log.warning(
                    "vector_search_skipped_space",
                    collection=col,
                    reason=s.vector_space_reason(col)[:200])
                return []
            hits: list[RetrievedChunk] = []
            for vec in vectors:
                f = dict(filter_base)
                rs = await s.vector.search(
                    col, vec, top_k=top_k, filter=f)
                hits.extend(rs)
            return hits

        per_col = await asyncio.gather(
            *[_one_collection(c) for c in target_cols],
            return_exceptions=True)
        best: dict[str, RetrievedChunk] = {}
        for rs in per_col:
            if isinstance(rs, Exception):
                log.warning("vector_search_collection_failed",
                            error=str(rs))
                continue
            for c in rs:
                if c.chunk_id not in best or c.score > best[c.chunk_id].score:
                    best[c.chunk_id] = c
        out = sorted(best.values(), key=lambda c: c.score, reverse=True)
        for i, c in enumerate(out):
            c.rank = i + 1
        return out[:top_k]

    # ── BM25 路：摘要 1.5 权重；同义词独立查询 ×0.85 降权合并 ──────────
    async def _bm25(self, ctx: QueryContext, filter_base: dict,
                    target_cols: list[str], cfg: dict
                    ) -> list[RetrievedChunk]:
        s = ctx.services
        plan = ctx.plan
        if not s.fulltext:
            return []
        top_k = cfg.get("top_k", 30)
        index = self._joined_index(s, target_cols)
        main = await s.fulltext.search(
            index, plan.semantic_query, top_k=top_k,
            filter=filter_base)
        # 同义词独立查询（×0.85），逐组发起后按分合并
        syn_groups: list[list[str]] = []
        if isinstance(plan.synonyms, dict):
            syn_groups = [v for v in plan.synonyms.values() if v]
        elif isinstance(plan.synonyms, list):
            syn_groups = [v for v in plan.synonyms if v]
        for syn_terms in syn_groups[:3]:
            if not syn_terms:
                continue
            try:
                alt = await s.fulltext.search(
                    index, " ".join(syn_terms), top_k=top_k,
                    filter=filter_base)
            except Exception as e:
                log.warning("bm25_synonym_search_failed", error=str(e))
                continue
            for c in alt:
                c.score = c.score * 0.85
            by_id = {c.chunk_id: c for c in main}
            for c in alt:
                cur = by_id.get(c.chunk_id)
                if cur is None or c.score > cur.score:
                    by_id[c.chunk_id] = c
            main = sorted(by_id.values(), key=lambda c: c.score,
                          reverse=True)[:top_k]
        for i, c in enumerate(main):
            c.rank = i + 1
        return main

    async def _kw(self, ctx: QueryContext, filter_base: dict,
                  target_cols: list[str], cfg: dict) -> list[RetrievedChunk]:
        s = ctx.services
        plan = ctx.plan
        if not s.fulltext:
            return []
        terms = list(plan.keywords or [])
        terms += [e.original for e in (plan.entities or [])
                  if e.original][:5]
        if not terms:
            return []
        index = self._joined_index(s, target_cols)
        return await s.fulltext.search_exact(
            index, terms, top_k=cfg.get("top_k", 20), filter=filter_base)

    async def _graph(self, ctx: QueryContext, cfg: dict
                     ) -> list[RetrievedChunk]:
        s = ctx.services
        plan = ctx.plan
        if not s.graph or not cfg.get("enabled"):
            return []
        docs = await s.graph.traverse(
            ctx.user.tenant_id,
            [e.original for e in (plan.entities or []) if e.original],
            max_depth=2)
        return docs[: cfg.get("top_k", 10)]

    async def _ephemeral(self, ctx: QueryContext, filter_base: dict,
                         cfg: dict) -> list[RetrievedChunk]:
        s = ctx.services
        plan = ctx.plan
        tmp_ids = ctx.session.ephemeral_doc_ids
        if not tmp_ids or not s.fulltext:
            return []
        try:
            wl = await s.meta.query_chunk_ids(
                ctx.user.tenant_id, collections=[EPHEMERAL_COLLECTION])
            wl = [cid for cid in wl if any(
                cid.startswith(f"{tid}|") for tid in tmp_ids)] or None
        except Exception:
            wl = None
        f = dict(filter_base)
        f.pop("chunk_ids", None)
        if wl:
            f["chunk_ids"] = wl
        else:
            # 白名单不可得时 fail-closed，避免跨会话泄露
            f["chunk_ids"] = []
        index = self._joined_index(s, [EPHEMERAL_COLLECTION])
        rs = await s.fulltext.search(
            index, plan.semantic_query,
            top_k=cfg.get("top_k", 20), filter=f)
        for c in rs:
            c.is_ephemeral = True
        return rs

    @staticmethod
    def _joined_index(services, cols: list[str]) -> str:
        """多逻辑集合 → 多物理索引串（ES）；单集合原样返回"""
        if len(cols) == 1:
            return cols[0]
        idx = getattr(services.fulltext, "index_names", None)
        if callable(idx):
            return idx(cols)
        return ",".join(cols)


class MergeStep(PipelineStep):

    async def execute(self, ctx: QueryContext) -> None:
        await merge_candidates(ctx)


class _CrossEncoderCache:
    """进程级 CrossEncoder 缓存：跨 step 实例复用，LRU + 单飞加载

    背景：query_pipeline() 每次请求都会重建 step 实例，模型若挂在实例属性上，
    每次查询都要重新加载权重（CPU 数秒起、GPU 反复占用显存）。此处提升为进程级，
    以 (model_name, device) 为键，配置热切换时自动换用新模型。
    """

    def __init__(self, capacity: int = 2) -> None:
        self._capacity = max(1, int(capacity))
        self._cache: "OrderedDict[tuple[str, str], object]" = OrderedDict()
        self._lock = threading.Lock()

    def get_or_load(self, model_name: str, device: str):
        """取模型；未命中则锁内加载（single-flight），超容淘汰最久未用"""
        key = (model_name, device)
        with self._lock:
            model = self._cache.get(key)
            if model is not None:
                self._cache.move_to_end(key)
                return model
            from sentence_transformers import CrossEncoder  # noqa
            model = CrossEncoder(model_name, device=device)
            self._cache[key] = model
            while len(self._cache) > self._capacity:
                self._cache.popitem(last=False)
            return model


_CE_CACHE = _CrossEncoderCache(capacity=2)


# 远程重排的 HTTP 预算：与本地加载不同，这里必然要等一次网络往返。给足它
# （服务端可能正在冷启模型），但不能无限等 —— 超时由 RerankStep 兜住，
# 退到 LLM 重排（三级退化的第二级）
_RERANK_HTTP_TIMEOUT_SEC = 30.0


def _rerank_scores_from_payload(data: dict, n: int) -> list[float]:
    """rerank 响应体 → 按文档下标排好的分数（认识 Cohere/Jina 两套字段名）

    契约：{"results":[{"index":i,"relevance_score":s}, …]}（Jina/Cohere 一致），
    少数实现回 data/score。拿不回某一条就留 0：下游 rerank_threshold 会把它滤掉，
    与 LLM 重排的退化口径一致。
    """
    items = (data or {}).get("results") or (data or {}).get("data") or []
    scores = [0.0] * n
    if not isinstance(items, list):
        return scores
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            i = int(it.get("index", it.get("document_index")))
            if not 0 <= i < n:
                continue
            scores[i] = float(it.get("relevance_score", it.get("score")))
        except (TypeError, ValueError):
            continue
    return scores


async def _remote_rerank_scores(url: str, model: str, pairs: list) -> list[float]:
    """远程重排服务打分：POST {地址}/rerank（Cohere/Jina/vLLM/TEI 一致的契约）

    {"model":…, "query":…, "documents":[…], "top_n":n}
        → {"results":[{"index":i,"relevance_score":s}, …]}

    整批一次请求（不是逐条）：重排本身就是"一个 query 对 N 个文档"，逐条打会白付
    N 次往返。抛异常交给 RerankStep：那里会退到 LLM 重排，不会让一次网络抖动
    把整条问答打掉。model 只在填了才带 —— 有的服务只跑一份权重，不认这个字段。
    """
    import httpx
    query = pairs[0][0] if pairs else ""
    docs = [t for _, t in pairs]
    payload: dict = {"query": query, "documents": docs, "top_n": len(docs)}
    if model:
        payload["model"] = model
    async with httpx.AsyncClient(timeout=_RERANK_HTTP_TIMEOUT_SEC) as hc:
        resp = await hc.post(rerank_api_endpoint(url), json=payload)
        resp.raise_for_status()
        data = resp.json() or {}
    return _rerank_scores_from_payload(data, len(docs))


class RerankStep(PipelineStep):
    stop_on_error = False

    async def execute(self, ctx: QueryContext) -> None:
        s = ctx.services
        cfg = s.config.retrieval
        if not ctx.merged:
            return
        final_n = cfg.final_top_n or 6
        if not cfg.rerank_enabled:
            ctx.reranked = ctx.merged[:final_n]
            await self._fill_parent_content(ctx)
            return

        query = (ctx.plan.standalone_query if ctx.plan else None) \
            or ctx.question
        try:
            reranked = await self._cross_encoder_rerank(
                ctx, query, ctx.merged)
        except Exception as e:
            log.warning("cross_encoder_failed", error=str(e))
            try:
                reranked = await self._llm_rerank(ctx, query, ctx.merged)
            except Exception as e2:
                log.warning("llm_rerank_failed", error=str(e2))
                reranked = list(ctx.merged)  # 三级退化：保持 RRF 顺序
        thr = cfg.rerank_threshold
        if thr is not None:
            filtered = [c for c in reranked
                        if c.metadata.get("ce_score", c.score) >= thr]
            if len(filtered) >= 3:          # 超 less 时不过滤
                reranked = filtered
        ctx.reranked = reranked[:final_n]
        await self._fill_parent_content(ctx)
        await ctx.emit({"type": "meta", "stage": "reranked",
                        "count": len(ctx.reranked)})

    async def _cross_encoder_rerank(self, ctx: QueryContext, query: str,
                                    chunks: list[RetrievedChunk]):
        """精排：本机 Cross-Encoder 或远程重排服务（取自 retrieval 配置，可热切换）

        「模型路径/API」一栏两义，这里就是分流点：填 http(s) 地址 = 那台远程重排
        服务（打它的 /rerank），填目录 = 本机权重（模型与设备都从配置取）。
        """
        s = ctx.services
        pairs = []
        for c in chunks:
            scoring_text = c.parent_content or c.text
            pairs.append((query, scoring_text[:1000]))
        cfg = s.config.retrieval
        # 字段对齐：配置页保存的 rerank_model_dir / rerank_model / rerank_device。
        # 「模型路径/API」与「模型ID」在界面上是两行（选目录 → 选模型），本机加载要
        # 的是拼好的完整目录；那一栏留空时模型ID 本身就是完整引用（HuggingFace ID）
        target = resolve_rerank_model_path(
            getattr(cfg, "rerank_model_dir", ""),
            cfg.rerank_model or "BAAI/bge-reranker-v2-m3")
        if is_http_url(target):
            # 远程重排：这一批 query-doc 交给它打分。地址补全与「测试模型」共用同一个
            # 函数（models.rerank_api_endpoint）—— 配置页测过的地址就是这里打的地址
            scores = await _remote_rerank_scores(target, cfg.rerank_model, pairs)
        else:
            scores = await s.llm.score_pairs(pairs) if hasattr(
                s.llm, "score_pairs") else None
        if scores is None:
            model_name = target
            device = str(getattr(cfg, "rerank_device", "cpu") or "cpu").lower()
            if device == "cuda":
                try:
                    import torch
                    if not torch.cuda.is_available():
                        log.warning("rerank_device_fallback", chosen="cuda",
                                    reason="CUDA 不可用", fallback="cpu")
                        device = "cpu"
                except Exception:
                    device = "cpu"
            # 进程级缓存复用模型：加载与推理都在工作线程完成，
            # 既不阻塞事件循环，也避免并发查询重复加载（single-flight）
            def _score() -> list:
                model = _CE_CACHE.get_or_load(model_name, device)
                return model.predict(pairs)

            import asyncio as _aio
            scores = await _aio.to_thread(_score)
        for c, sc in zip(chunks, scores):
            c.metadata["ce_score"] = float(sc)
            c.score = float(sc)
        return sorted(chunks, key=lambda c: c.score, reverse=True)

    async def _llm_rerank(self, ctx: QueryContext, query: str,
                          chunks: list[RetrievedChunk]):
        """CE 不可用时的 LLM listwise 重排"""
        s = ctx.services
        lines = [f"[{i}] {c.text[:300]}" for i, c in enumerate(chunks)]
        prompt = (
            "根据以下候选片段与问题的相关性，输出最相关片段的编号列表"
            "（JSON 数组，最多 6 个，按相关性降序）。\n\n"
            f"问题：{query}\n\n候选：\n" + "\n".join(lines))
        out = await s.llm.generate(
            [{"role": "user", "content": prompt}], task="rewrite",
            response_format={"type": "json_object"})
        import json as _json
        try:
            data = _json.loads(out.text)
            ids = data.get("ids") or data.get("ranking") or []
            order = [int(x) for x in ids
                     if str(x).lstrip("-").isdigit() and 0 <= int(x) < len(chunks)]
        except Exception:
            order = []
        if not order:
            return list(chunks)
        seen = set()
        out_chunks: list[RetrievedChunk] = []
        n = len(chunks)
        for pos, i in enumerate(order):
            if i in seen:
                continue
            seen.add(i)
            c = chunks[i]
            c.metadata["ce_score"] = 1.0 - pos / max(1, n)
            c.score = c.metadata["ce_score"]
            out_chunks.append(c)
        for i, c in enumerate(chunks):
            if i not in seen:
                c.metadata.setdefault("ce_score", 0.0)
                c.score = 0.0
                out_chunks.append(c)
        return out_chunks

    # ── 父子回取：命中子块 → 拉父块正文喂给生成（A-B13）──────────────
    async def _fill_parent_content(self, ctx: QueryContext) -> None:
        s = ctx.services
        metas: dict[str, str] = {}
        try:
            chunk_ids = [c.chunk_id for c in ctx.reranked]
            cms = await s.meta.get_chunks_by_ids(chunk_ids)
            parent_ids = [cm.parent_chunk_id for cm in cms
                          if cm.parent_chunk_id]
            if parent_ids:
                metas = await s.meta.get_chunk_texts(
                    list(set(parent_ids)))
            by_id = {cm.chunk_id: cm for cm in cms}
            for c in ctx.reranked:
                cm = by_id.get(c.chunk_id)
                if cm and cm.parent_chunk_id:
                    parent_txt = metas.get(cm.parent_chunk_id)
                    if parent_txt and parent_txt != c.text:
                        c.parent_content = parent_txt
        except Exception as e:
            log.warning("parent_fetch_failed", error=str(e))


StepRegistry.register("retrieve")(RetrieveStep)
StepRegistry.register("merge")(MergeStep)
StepRegistry.register("rerank")(RerankStep)
