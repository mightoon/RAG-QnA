"""
进度总线（rag/services/progress.py）

入库任务进度事件 → SSE 推送。
Redis pub/sub 跨进程分发；降级模式进程内订阅者集合。
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import AsyncIterator

from rag.models import TaskProgressEvent
from rag.observability.logging import get_logger

log = get_logger("rag.progress")


class ProgressBus:

    def __init__(self, redis=None):
        self.redis = redis
        self._channel = "rag:progress"
        # 降级模式：进程内订阅者（tenant_id → 队列集合）
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    async def publish(self, event: TaskProgressEvent) -> None:
        """发布进度事件（Redis 优先，失败/无 Redis 走进程内）"""
        payload = event.model_dump_json()
        if self.redis is not None:
            try:
                await self.redis.publish(self._channel, payload)
                return
            except Exception as e:
                log.warning("progress_publish_failed", error=str(e))
        # 进程内分发
        dead: list[asyncio.Queue] = []
        for q in self._subscribers.get("*", set()):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers["*"].discard(q)

    async def subscribe(self) -> AsyncIterator[TaskProgressEvent]:
        """SSE 端点消费：yield 进度事件，直到消费者断开"""
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        if self.redis is not None:
            pubsub = self.redis.pubsub()
            await pubsub.subscribe(self._channel)
            try:
                # 心跳 + 事件混合迭代
                while True:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=15.0)
                    if msg and msg.get("type") == "message":
                        try:
                            yield TaskProgressEvent(**json.loads(
                                msg["data"]))
                        except Exception:
                            continue
                    else:
                        yield None            # 心跳占位（SSE keep-alive）
            finally:
                try:
                    await pubsub.unsubscribe(self._channel)
                    await pubsub.close()
                except Exception:
                    pass
        else:
            self._subscribers["*"].add(q)
            try:
                while True:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield TaskProgressEvent(**json.loads(payload))
            except asyncio.TimeoutError:
                yield None                    # 心跳占位
            finally:
                self._subscribers["*"].discard(q)
