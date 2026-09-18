"""
Prometheus 指标（rag/observability/metrics.py）

标准指标集；启用时经 Push Gateway 推送，Grafana 可视化。
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram


class Metrics:
    """全局单例指标集合"""

    def __init__(self) -> None:
        self.queries_total = Counter(
            "rag_queries_total", "查询总次数", ["intent", "status"])
        self.step_latency = Histogram(
            "rag_step_latency_seconds", "Pipeline 步骤延迟",
            ["pipeline", "step"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30))
        self.recall_scores = Histogram(
            "rag_recall_score", "召回分数分布",
            ["path"], buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0))
        self.kb_chunks = Gauge(
            "rag_kb_chunks_total", "知识库 Chunk 总数", ["collection"])
        self.active_sessions = Gauge(
            "rag_active_sessions", "活跃 Session 数")
        self.ingest_tasks_total = Counter(
            "rag_ingest_tasks_total", "入库任务总量", ["status"])
        self.ingest_duration = Histogram(
            "rag_ingest_duration_seconds", "入库各阶段耗时",
            ["stage"], buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800))
        self.consistency_issues = Counter(
            "rag_consistency_issues_total", "一致性巡检发现的问题数")
        self.consistency_repairs = Counter(
            "rag_consistency_repairs_total", "自动修复数")
        self.ephemeral_docs = Gauge(
            "rag_ephemeral_docs_active", "当前有效临时文档数")
        self.chunk_quality = Counter(
            "rag_chunk_quality_total", "Chunk 质量分布", ["bucket"])

    def observe_step(self, pipeline: str, step: str, seconds: float) -> None:
        self.step_latency.labels(pipeline=pipeline, step=step).observe(seconds)

    def observe_query(self, intent: str, status: str = "ok") -> None:
        self.queries_total.labels(intent=intent, status=status).inc()


metrics = Metrics()
