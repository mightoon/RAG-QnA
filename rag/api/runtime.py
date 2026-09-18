"""
运行时容器管理（rag/api/runtime.py）

- 后台任务启停（入库 worker / 临时文档清理 / 一致性巡检 / 定时调度）
- apply_config_update：配置页保存后的热应用。只改了一个服务的配置就
  只重建那个服务（其余服务的连接/后台任务不动）；涉及多段或无法单段
  处理的段才整容器重建
- rebuild_container：按 YAML 整容器热重建；新容器构建失败时保留旧容器
  继续运行，绝不中断服务
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from rag.adapters.registry import AdapterRegistry
from rag.config.loader import load_config
from rag.container import (
    SECTION_ADAPTERS, SECTION_DEGRADED_KEYS, ServiceContainer,
)
from rag.observability.logging import get_logger

log = get_logger("rag.runtime")

# 可单段热应用的配置段：每个都独占一个适配器（或 Redis 客户端），
# 保存其中之一不必牵动别的服务
_SCOPED_SECTIONS = frozenset(SECTION_ADAPTERS) | {"redis"}


async def _periodic_scheduler(container: ServiceContainer) -> None:
    """定时任务：空闲会话归档（摘要→画像）、负反馈 chunk 质量降权"""
    mem_cfg = container.config.memory
    while True:
        await asyncio.sleep(max(60, 15 * 60))
        try:
            archived = await container.memory_service.archive_idle_sessions()
            if archived:
                log.info("archive_done", count=archived)
        except Exception as e:
            log.warning("archive_task_failed", error=str(e))
        try:
            from rag.services.stats_service import adjust_quality_from_feedback
            updated = await adjust_quality_from_feedback(container)
            if updated:
                log.info("quality_adjust_done", count=updated)
        except Exception as e:
            log.warning("quality_adjust_failed", error=str(e))


async def start_background(app, container: ServiceContainer) -> None:
    """启动容器附带的后台任务"""
    await container.ingest_coordinator.start()
    await container.ephemeral_service.start_cleanup_loop()
    await container.consistency_checker.start()
    app.state.scheduler_task = asyncio.create_task(
        _periodic_scheduler(container))


async def stop_background(app, container: ServiceContainer) -> None:
    """停止后台任务并释放容器连接（各步尽力而为，不互相阻断）"""
    task = getattr(app.state, "scheduler_task", None)
    if task is not None:
        task.cancel()
        app.state.scheduler_task = None
    for stopper in (getattr(container, "consistency_checker", None),
                    getattr(container, "ingest_coordinator", None)):
        if stopper is None:
            continue
        try:
            await stopper.stop()
        except Exception as e:
            log.warning("background_stop_failed", error=str(e))
    try:
        await container.shutdown()
    except Exception as e:
        log.warning("container_shutdown_failed", error=str(e))


def _resolve_config_path(container: ServiceContainer) -> Path:
    path = Path(container.config._config_path or "customer/customer_config.yaml")
    return path if path.is_absolute() else Path.cwd() / path


async def apply_config_update(app, update: dict,
                              cfg_path: Path | None = None) -> dict:
    """保存后的热应用：能只动一段就只动一段。

    只改了一个「独占适配器的服务段」（MySQL/向量库/ES/图谱/业务数据/
    存储/同义词/LLM/Embedding/Redis）时，只重建那一个适配器并单独自检：
    其它服务的连接、健康检查、后台任务全都不碰。涉及多段、或是不带独立
    适配器的段（检索策略/权限/编排…）才回退到整容器重建。

    返回 {"scope": 段名 | "container", "degraded": {...}}；
    degraded 只报本次真正涉及组件的降级情况，避免出现"保存 MySQL
    却弹出向量库降级"这种看起来像动了别的服务的提示。
    """
    old: ServiceContainer = app.state.container
    keys = [k for k in update if k in _SCOPED_SECTIONS]
    single = keys[0] if len(keys) == 1 and len(keys) == len(update) else None
    if single is None or old.config.noconnection:
        result = await rebuild_container(app)
        return {"scope": "container",
                "degraded": dict(result.get("degraded") or {})}

    path = cfg_path or _resolve_config_path(old)
    new_config = load_config(path)
    # 只把刚保存的这一段搬到运行中的配置上：其它段保持内存里的现值不动
    setattr(old.config, single, getattr(new_config, single))
    await old.apply_section(single)
    affected = SECTION_DEGRADED_KEYS.get(single, (single,))
    degraded = {k: v for k, v in old.degraded.items() if k in affected}
    log.info("config_applied", scope=single, degraded=degraded)
    return {"scope": single, "degraded": degraded}


async def rebuild_container(app) -> dict:
    """按配置文件热重建容器（配置页保存后调用）。

    流程：读取 YAML → 清空适配器单例缓存 → 构建新容器并自检 →
    切换 app.state.container → 停旧起新后台任务。
    新容器构建失败时抛出异常，旧容器保持运行不受影响。
    """
    old: ServiceContainer = app.state.container
    cfg_path = _resolve_config_path(old)
    new_config = load_config(cfg_path)
    new_config.noconnection = old.config.noconnection

    # 适配器按 (type,name) 单例缓存；清空后新容器按新配置重建连接
    AdapterRegistry.clear_cache()
    try:
        new_c = ServiceContainer(new_config)
        await new_c.initialize()
    except Exception as e:
        try:
            await new_c.shutdown()
        except Exception:
            pass
        log.error("rebuild_failed_keep_old", error=str(e), path=str(cfg_path))
        raise

    await stop_background(app, old)
    app.state.container = new_c
    await start_background(app, new_c)
    log.info("container_rebuilt", degraded=list(new_c.degraded),
             path=str(cfg_path))
    return {"degraded": dict(new_c.degraded)}
