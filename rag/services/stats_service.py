"""统计与反馈闭环服务（rag/services/stats_service.py）

反馈三闭环中的"路径 2"：定时汇总高频负反馈，
对被反复踩的 chunk 下调 quality_score（-0.05~0.1/次），
检索侧 MergeStep / Rerank 依据 quality_score 降权，形成闭环。
"路径 1"（同会话记忆注入）在 chat.feedback 路由落地。
"""
from __future__ import annotations

from collections import Counter

from rag.observability.logging import get_logger

log = get_logger("rag.services.stats")

_processed: set[str] = set()          # 已处理的反馈 ID（进程内去重）


async def adjust_quality_from_feedback(container,
                                       days: int = 7,
                                       min_hits: int = 2) -> int:
    """高频负反馈 chunk 降权。返回本次被调整的 chunk 数。"""
    meta = container.meta
    adjust = getattr(meta, "adjust_chunk_quality", None)
    if adjust is None:
        return 0
    fbs = await meta.list_negative_feedback(days=days, limit=500)
    counter: Counter[str] = Counter()
    for fb in fbs:
        if fb.feedback_id in _processed:
            continue
        _processed.add(fb.feedback_id)
        for src in fb.sources or []:
            cid = src.get("chunk_id")
            if cid:
                counter[cid] += 1
    updated = 0
    for cid, hits in counter.most_common(200):
        if hits < min_hits:
            continue
        delta = -min(0.1, 0.05 * hits)
        try:
            new_score = await adjust(cid, delta)
            if new_score is not None:
                updated += 1
                log.info("chunk_quality_adjusted", chunk_id=cid,
                         delta=delta, new_score=round(new_score, 3))
        except Exception as e:
            log.warning("chunk_quality_adjust_failed",
                        chunk_id=cid, error=str(e))
    return updated
