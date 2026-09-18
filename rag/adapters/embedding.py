"""
Embedding 适配器（rag/adapters/embedding.py）

http_embedding：自建 BGE/text2vec HTTP 服务（POST /embeddings，OpenAI 格式）
openai_embedding：OpenAI 官方及兼容端点
"""
from __future__ import annotations

import asyncio

import httpx

from rag.config.models import EmbeddingConfig

from .base import EmbeddingAdapter
from .registry import AdapterRegistry


@AdapterRegistry.register("embedding", "http_embedding")
class HTTPEmbedding(EmbeddingAdapter):

    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.api_key}"} if config.api_key else {},
            timeout=config.timeout,
        )
        self._semaphore = asyncio.Semaphore(4)

    async def _call(self, texts: list[str], prefix: str = "") -> list[list[float]]:
        if prefix:
            texts = [prefix + t for t in texts]
        results: list[list[float]] = []
        batch = self.config.batch_size
        for i in range(0, len(texts), batch):
            sub = texts[i:i + batch]
            payload = {"model": self.config.model, "input": sub}
            async with self._semaphore:
                resp = await self._client.post("/embeddings", json=payload)
                resp.raise_for_status()
                data = resp.json()
            # 按 index 排序保证顺序
            items = sorted(data["data"], key=lambda d: d["index"])
            results.extend(item["embedding"] for item in items)
        if getattr(self.config, "normalize", True):
            results = [self._l2(v) for v in results]
        return results

    @staticmethod
    def _l2(vec: list[float]) -> list[float]:
        """显式 L2 归一化：Milvus IP 度量等价于余弦相似度的前提"""
        import math
        norm = math.sqrt(sum(x * x for x in vec))
        if norm <= 0:
            return vec
        return [x / norm for x in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._call(texts)

    async def embed_query(self, text: str) -> list[float]:
        vecs = await self._call([text], prefix=self.config.query_prefix)
        return vecs[0]

    @property
    def dim(self) -> int:
        return self.config.dim

    async def health_check(self) -> bool:
        try:
            vec = await self.embed_query("ping")
            return len(vec) == self.config.dim
        except Exception:
            return False


# OpenAI 官方端点协议相同，注册别名
AdapterRegistry._classes["embedding"]["openai_embedding"] = HTTPEmbedding
