"""
Pipeline 基础设施（rag/pipeline/base.py）

- PipelineStep：步骤基类（入库/查询共用）
- StepRegistry：步骤注册中心（装饰器注册，按名构建）
- Pipeline：有序步骤序列执行器
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Type

from rag.observability.logging import get_logger

from .context import IngestContext, QueryContext

log = get_logger("rag.pipeline")


class StepError(Exception):
    """步骤执行失败（入库侧由 Coordinator 捕获进入重试/降级流程）"""

    def __init__(self, step: str, message: str, retryable: bool = True):
        self.step = step
        self.retryable = retryable
        super().__init__(f"[{step}] {message}")


class PipelineStep(ABC):
    """
    步骤基类。
    - name: 注册名（workflows.yaml 引用）
    - optional: True 时失败仅记 warning 不中断（渐进式降级）
    """
    name: str = "base"
    optional: bool = False

    @abstractmethod
    async def execute(self, ctx: IngestContext | QueryContext) -> None:
        """执行步骤，直接读写 ctx 上的中间产物"""


class StepRegistry:
    """步骤注册中心：name → 步骤类"""

    _steps: dict[str, Type[PipelineStep]] = {}

    @classmethod
    def register(cls, name: str):
        def deco(klass: Type[PipelineStep]):
            klass.name = name
            cls._steps[name] = klass
            return klass
        return deco

    @classmethod
    def build(cls, names: list[str]) -> list[PipelineStep]:
        steps = []
        for n in names:
            if n not in cls._steps:
                raise KeyError(f"Pipeline 步骤未注册: {n}，"
                               f"可用: {sorted(cls._steps.keys())}")
            steps.append(cls._steps[n]())
        return steps

    @classmethod
    def available(cls) -> list[str]:
        return sorted(cls._steps.keys())


class Pipeline:
    """有序步骤序列执行器"""

    def __init__(self, name: str, steps: list[PipelineStep]):
        self.name = name
        self.steps = steps

    async def run(self, ctx: IngestContext | QueryContext) -> None:
        """顺序执行；optional 步骤失败降级，其余失败抛 StepError"""
        import time
        from rag.observability.metrics import metrics
        pipeline = self.name.split(":", 1)[0]       # ingest / query
        for step in self.steps:
            t0 = time.perf_counter()
            try:
                if isinstance(ctx, QueryContext):
                    ctx.tick(f"step:{step.name}:start")
                await step.execute(ctx)
                if isinstance(ctx, QueryContext):
                    ctx.tick(f"step:{step.name}:end")
                log.debug("pipeline_step_done", pipeline=self.name,
                          step=step.name)
            except StepError:
                metrics.observe_step(pipeline, step.name,
                                     time.perf_counter() - t0)
                raise
            except Exception as e:
                metrics.observe_step(pipeline, step.name,
                                     time.perf_counter() - t0)
                if step.optional:
                    log.warning("pipeline_step_degraded",
                                pipeline=self.name, step=step.name,
                                error=str(e))
                    if isinstance(ctx, IngestContext):
                        ctx.add_warning(f"步骤 {step.name} 降级: {e}")
                else:
                    raise StepError(step.name, str(e)) from e
            metrics.observe_step(pipeline, step.name,
                                 time.perf_counter() - t0)

    async def run_with_timeout(self, ctx: QueryContext,
                               timeout: float) -> None:
        """查询流水线整体超时保护 + 异常事件推送"""
        try:
            await asyncio.wait_for(self.run(ctx), timeout=timeout)
        except StepError as e:
            # 已有终止事件（如 security 拒答）则不再推送
            if not ctx.answer:
                await ctx.emit({"type": "error", "message": str(e)})
            raise
        except asyncio.TimeoutError:
            # 超时时若已有部分答案则优雅收尾，否则报错
            if ctx.answer:
                await ctx.emit({"type": "done", "timeout": True})
            else:
                await ctx.emit({"type": "error",
                                "message": "查询处理超时，请稍后重试"})
                raise StepError("pipeline", "查询整体超时", retryable=False)

    def describe(self) -> list[str]:
        return [s.name for s in self.steps]
