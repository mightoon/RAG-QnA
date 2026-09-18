"""
LLM 适配器（rag/adapters/llm.py）

openai_compatible：任何 OpenAI Chat Completions 格式服务
（vLLM / Ollama / 国产模型兼容端点 / OpenAI 本体）。
task 参数路由到不同模型（rewrite_model / summary_model / 主模型）。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx

from rag.config.models import LLMConfig
from rag.observability.logging import get_logger

from .base import LLMAdapter
from .registry import AdapterRegistry

log = get_logger("rag.llm")


@AdapterRegistry.register("llm", "openai_compatible")
class OpenAICompatibleLLM(LLMAdapter):

    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.api_key}"} if config.api_key else {},
            timeout=config.timeout,
        )
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    def _model_for_task(self, task: str) -> str:
        cfg = self.config
        if task in ("rewrite", "intent", "extract") and cfg.rewrite_model:
            return cfg.rewrite_model
        if task in ("summary", "compress") and cfg.summary_model:
            return cfg.summary_model
        return cfg.model

    async def generate(
        self,
        messages: list[dict],
        task: str = "generate",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload = {
            "model": self._model_for_task(task),
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
            "stream": False,
        }
        async with self._semaphore:
            resp = await self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"] or ""

    async def stream_generate(
        self,
        messages: list[dict],
        task: str = "generate",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        payload = {
            "model": self._model_for_task(task),
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
            "stream": True,
        }
        async with self._semaphore:
            async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        import json
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        if delta.get("content"):
                            yield delta["content"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    async def health_check(self) -> bool:
        try:
            resp = await self._client.get("/models", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False


# vLLM 与 openai_compatible 协议一致，仅注册别名
AdapterRegistry._classes["llm"]["vllm"] = OpenAICompatibleLLM
