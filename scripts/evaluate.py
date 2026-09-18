"""离线评测脚本（scripts/evaluate.py）

用法：
    python scripts/evaluate.py --cases eval_cases.json --top-k 20

评测集格式（JSON 数组）：
    [{"question": "...", "expect_doc": "手册.pdf",     # 期望命中文档名（子串匹配）
      "expect_keywords": ["超时", "重试"]}, ...]       # 或期望关键词（命中任一）

指标：Recall@K（top-K 命中期望文档/关键词的问题占比）、
平均/分位延迟、忠实度均值（若开启）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rag.container import ServiceContainer  # noqa: E402
from rag.models import MessageRole, SessionState, UserContext  # noqa: E402
from rag.pipeline.context import QueryContext  # noqa: E402


async def evaluate(cases: list[dict], top_k: int) -> dict:
    container = ServiceContainer()
    await container.initialize()

    user = UserContext(user_id="eval", tenant_id=container.config.tenant_id,
                       roles=["admin"])
    hits, latencies, faiths = 0, [], []
    for i, case in enumerate(cases, 1):
        session = SessionState(session_id=f"eval-{i}",
                               user_id=user.user_id, tenant_id=user.tenant_id)
        ctx = QueryContext(session=session, user=user,
                           question=case["question"], services=container)
        t0 = time.perf_counter()
        try:
            pipeline = container.workflows.query_pipeline()
            timeout = container.config.pipeline.total_timeout_seconds
            await pipeline.run_with_timeout(ctx, timeout)
        except Exception as e:
            print(f"  [{i}] ERROR {e}")
            continue
        lat = (time.perf_counter() - t0) * 1000
        latencies.append(lat)

        expect_doc = (case.get("expect_doc") or "").lower()
        kws = [k.lower() for k in case.get("expect_keywords") or []]
        top = ctx.merged[:top_k]
        hit = False
        for c in top:
            title = (c.title or "").lower()
            text = (c.text or "").lower()
            if expect_doc and expect_doc in title:
                hit = True
                break
            if kws and any(k in text for k in kws):
                hit = True
                break
        hits += int(hit)
        if ctx.meta.get("faithfulness") is not None:
            faiths.append(ctx.meta["faithfulness"])
        print(f"  [{i}] {'HIT ' if hit else 'MISS'} {lat:7.0f}ms "
              f"{case['question'][:40]}")

    await container.shutdown()
    n = len(latencies) or 1
    lat_sorted = sorted(latencies)
    report = {
        "total": len(cases),
        "recall@%d" % top_k: round(hits / len(cases), 4) if cases else 0,
        "latency_avg_ms": round(mean(latencies), 1) if latencies else 0,
        "latency_p95_ms": round(lat_sorted[int(0.95 * (n - 1))], 1)
        if latencies else 0,
        "faithfulness_avg": round(mean(faiths), 3) if faiths else None,
    }
    return report


async def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 检索/问答离线评测")
    ap.add_argument("--cases", required=True, help="评测集 JSON 文件")
    ap.add_argument("--top-k", type=int, default=20)
    args = ap.parse_args()
    with open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)
    report = await evaluate(cases, args.top_k)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
