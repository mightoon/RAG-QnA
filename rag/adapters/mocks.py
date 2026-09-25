"""
本地 Mock 适配器（rag/adapters/mocks.py）

--noconnection 演示模式使用：
- MockLLMAdapter      无 LLM 服务时返回演示回复（按 task 分发，
                      JSON 类调用返回合法结构触发正常解析路径）
- MockEmbeddingAdapter 字符桶确定性向量（相同文本→相同向量，
                      共享字符越多越相似，检索演示有一定意义）

也可在配置中显式指定 adapter: mock 使用。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from typing import AsyncIterator

from .base import EmbeddingAdapter, LLMAdapter, SynonymAdapter
from .registry import AdapterRegistry

_DEMO_NOTICE = (
    "\n\n---\n⚠ 当前为 --noconnection 演示模式：未连接 LLM / 向量库 / "
    "Elasticsearch / MySQL / Redis，本回答由内置 Mock 生成，"
    "仅用于界面联调与流程验证。"
)


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            return str(m["content"])
    return ""


@AdapterRegistry.register("llm", "mock")
class MockLLMAdapter(LLMAdapter):
    """离线演示 LLM：按 task 返回结构合法的模拟输出"""

    def __init__(self, config=None):
        self._delay = getattr(config, "timeout", 0) and 0.02 or 0.02

    async def generate(self, messages: list[dict], task: str = "generate",
                       temperature: float | None = None,
                       max_tokens: int | None = None,
                       response_format: dict | None = None,
                       thinking: bool | None = None) -> str:
        text = _last_user_text(messages)
        if task == "rewrite":
            # 查询理解：返回合法 QueryPlan JSON（子查询分解处解析为
            # dict 失败会安全降级为不分解）
            return json.dumps({
                "intent": "factual",
                "standalone_query": text[:200],
                "semantic_query": text[:200],
                "constraints": {},
            }, ensure_ascii=False)
        if task == "summary":
            # 入库增强期望 JSON；记忆压缩期望纯文本 —— 按 prompt 嗅探
            prompt_all = " ".join(str(m.get("content", "")) for m in messages)
            if ("关键词" in prompt_all or "keywords" in prompt_all.lower()
                    or "JSON" in prompt_all):
                snippet = text[:80].replace("\n", " ")
                return json.dumps({
                    "summary": f"（演示摘要）{snippet}…",
                    "keywords": self._mock_keywords(text),
                    "entities": [],
                }, ensure_ascii=False)
            return f"（演示摘要）用户询问了：{text[:100]}"
        if task == "evaluate":
            return "0.95"
        # chat / generate / 其他
        question = text[:200] or "（空提问）"
        return (f"【演示回答】收到提问：「{question}」。"
                f"当前系统运行在 --noconnection 演示模式，"
                f"检索与生成组件均为本地 Mock 实现。"
                f"连接真实服务后（LLM / Milvus / Elasticsearch / MySQL / Redis），"
                f"此处将返回基于知识库检索的真实回答。{_DEMO_NOTICE}")

    async def stream_generate(self, messages: list[dict],
                              task: str = "generate",
                              temperature: float | None = None,
                              max_tokens: int | None = None
                              ) -> AsyncIterator[str]:
        full = await self.generate(messages, task, temperature, max_tokens)
        # 按 3 字符分块流式输出，模拟打字机效果
        for i in range(0, len(full), 3):
            yield full[i:i + 3]
            await asyncio.sleep(0.02)

    @staticmethod
    def _mock_keywords(text: str) -> list[str]:
        """简单高频字词提取（演示用）"""
        words = [w for w in text.split() if len(w) >= 2][:5]
        return words or (list(text[:3]) if text else [])

    async def health_check(self) -> bool:
        return True


@AdapterRegistry.register("embedding", "mock")
class MockEmbeddingAdapter(EmbeddingAdapter):
    """离线演示向量化：字符桶确定性向量

    每个字符按 ord 值落入固定维度桶累加后归一化 ——
    相同文本 → 相同向量；共享字符越多 → 余弦相似度越高，
    使演示模式下的向量检索仍有大致相关性。
    """

    def __init__(self, config=None):
        self._dim = int(getattr(config, "dim", 0) or 1024)

    @property
    def dim(self) -> int:
        return self._dim

    def _vector(self, text: str) -> list[float]:
        v = [0.0] * self._dim
        for ch in text:
            v[ord(ch) % self._dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v))
        if norm > 0:
            v = [x / norm for x in v]
        else:
            v[0] = 1.0
        return v

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    async def health_check(self) -> bool:
        return True


@AdapterRegistry.register("synonym", "none")
class NullSynonym(SynonymAdapter):
    """空同义词表：核心依赖降级占位（文件缺失/服务不可用时使用）"""

    def __init__(self, config=None):
        self.config = config

    def expand(self, term: str) -> list[str]:
        return [term]

    def normalize(self, term: str) -> str:
        return term

    def reload(self) -> int:
        return 0

    def all_groups(self) -> list[list[str]]:
        return []


def mock_md5(text: str) -> str:
    """演示模式 MD5（与真实流程无关，保持工具完整性）"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()
