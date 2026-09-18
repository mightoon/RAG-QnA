"""
Workflow 注册表（rag/pipeline/engine.py）

V2 文档类型路由：不同格式的文档走不同的入库 Pipeline（步骤序列），
序列定义在 customer/workflows.yaml，客户可自定义增删步骤。
查询 Pipeline 全局唯一。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from rag.observability.logging import get_logger

from .base import Pipeline, StepRegistry

log = get_logger("rag.engine")

# 文档类型 → workflow 名的映射规则（pdf 按扫描类型细分）
_PDF_WORKFLOWS = {"text": "pdf_text", "scanned": "pdf_scanned",
                  "hybrid": "pdf_hybrid"}
_TYPE_WORKFLOWS = {
    "docx": "docx", "xlsx": "xlsx", "csv": "xlsx", "pptx": "pptx",
    "md": "md", "txt": "txt", "html": "html", "img": "image",
}
# 解析器输出的 file_type（ParsedDocument.file_type）与扩展名的对应
_EXT_TO_TYPE = {
    ".pdf": "pdf", ".docx": "docx", ".doc": "docx", ".xlsx": "xlsx",
    ".xls": "xlsx", ".csv": "csv", ".pptx": "pptx", ".ppt": "pptx",
    ".md": "md", ".markdown": "md", ".txt": "txt", ".log": "txt",
    ".html": "html", ".htm": "html", ".png": "img", ".jpg": "img",
    ".jpeg": "img", ".bmp": "img", ".tiff": "img", ".webp": "img",
}


class WorkflowRegistry:
    """从 workflows.yaml 加载步骤序列，按文档类型路由构建 Pipeline"""

    def __init__(self, yaml_path: str | Path = "customer/workflows.yaml"):
        self._path = Path(yaml_path)
        self._ingest_workflows: dict[str, list[str]] = {}
        self._query_workflow: list[str] = []
        self._mtime: float = 0.0
        self._load()

    def _maybe_reload(self) -> None:
        """热重载：workflows.yaml 变更后自动重载（失败则保留旧配置）"""
        try:
            if not self._path.exists():
                return
            mtime = self._path.stat().st_mtime
            if mtime > self._mtime:
                self._load()
                log.info("workflows_reloaded", path=str(self._path))
        except Exception as e:
            log.warning("workflows_reload_failed", error=str(e))

    def _load(self) -> None:
        if not self._path.exists():
            # 缺省内置序列（与内置 workflows.yaml 一致）
            self._ingest_workflows = {
                "default": self._default_ingest(),
            }
            self._query_workflow = self._default_query()
            log.warning("workflows_file_missing", path=str(self._path),
                        note="使用内置默认序列")
            return
        with open(self._path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        self._ingest_workflows = data.get("ingest_workflows", {})
        self._query_workflow = data.get(
            "query_workflow", self._default_query())
        # 保证 default 存在
        if "default" not in self._ingest_workflows:
            self._ingest_workflows["default"] = self._default_ingest()
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            pass
        log.info("workflows_loaded", count=len(self._ingest_workflows))

    @staticmethod
    def _default_ingest() -> list[str]:
        return ["detect_format", "parse", "outline", "chunk", "enrich",
                "embed", "write", "verify", "finalize"]

    @staticmethod
    def _default_query() -> list[str]:
        return ["security", "memory_load", "understand", "retrieve",
                "merge", "self_eval", "rerank", "generate",
                "faithfulness", "memory_update"]

    # ── 入库路由 ───────────────────────────────────────────

    def workflow_name_for(self, filename: str, scan_type: str = "text") -> str:
        """文件名（或 file_type）→ workflow 名"""
        doc_type = _EXT_TO_TYPE.get(
            filename if filename.startswith(".") else
            Path(filename).suffix.lower(), None)
        if doc_type is None:
            # 允许直接传 file_type（如 "pdf"）
            doc_type = filename.lower() if filename.lower() in \
                _TYPE_WORKFLOWS else None
        if doc_type == "pdf":
            return _PDF_WORKFLOWS.get(scan_type, "pdf_text")
        return _TYPE_WORKFLOWS.get(doc_type, "default")

    def ingest_pipeline(self, filename: str,
                        scan_type: str = "text") -> Pipeline:
        self._maybe_reload()
        name = self.workflow_name_for(filename, scan_type)
        steps = self._ingest_workflows.get(name)
        if not steps:
            log.warning("workflow_not_found", name=name, fallback="default")
            name, steps = "default", self._ingest_workflows["default"]
        return Pipeline(f"ingest:{name}", StepRegistry.build(steps))

    # ── 查询 ───────────────────────────────────────────────

    def query_pipeline(self) -> Pipeline:
        self._maybe_reload()
        return Pipeline("query", StepRegistry.build(self._query_workflow))

    def describe(self) -> dict:
        return {"ingest": {k: v for k, v in self._ingest_workflows.items()},
                "query": self._query_workflow}
