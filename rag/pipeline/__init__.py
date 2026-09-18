"""
Pipeline 引擎（rag/pipeline/）

- context: IngestContext / QueryContext 数据总线
- base:    PipelineStep / StepRegistry / Pipeline
- engine:  WorkflowRegistry（文档类型路由）
- steps:   入库与查询步骤实现（import 副作用注册）
"""
from .base import Pipeline, PipelineStep, StepError, StepRegistry
from .context import IngestContext, QueryContext
from .engine import WorkflowRegistry

# 触发步骤注册（入库 + 查询）
from . import steps  # noqa: F401,E402

__all__ = [
    "Pipeline", "PipelineStep", "StepError", "StepRegistry",
    "IngestContext", "QueryContext", "WorkflowRegistry",
]
