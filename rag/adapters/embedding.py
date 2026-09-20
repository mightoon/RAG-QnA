"""
Embedding 适配器（rag/adapters/embedding.py）

http_embedding：自建 BGE/text2vec HTTP 服务（POST /embeddings，OpenAI 格式）
openai_embedding：OpenAI 官方及兼容端点

探测口径与 MySQL（TS-014）/ ES（TS-015）同源：health_detail() 给"下一步该动
哪里"，health_probe() 给它套上与容器自检、配置页「测试连接」共用的同一份预算。
"""
from __future__ import annotations

import asyncio
import time

import httpx

from rag.config.models import EmbeddingConfig
from rag.observability.logging import get_logger

from .base import EmbeddingAdapter
from .registry import AdapterRegistry

log = get_logger("rag.adapters.embedding")

# ── 探测预算（代码常量，刻意不作为用户配置项）─────────────────────
# 与 MySQL / ES 同源（TS-014 / TS-015）：把"连不上要等多久"交给用户填，只会让人
# 以为调大它就能解决连接问题。收敛成常量后，容器自检、监控页、配置页
# 「测试连接」三条链路共用同一份预算，才不会出现"页面测通了、监控说不可用"。
# 取值偏宽是刻意的：向量模型掉线不会让进程起不来（不像 meta 那样降级换实现），
# 但误判一次整条向量检索路就会被摘掉，代价远大于多等几秒。
HEALTH_BUDGET_SEC = 10.0
# 单次探测超过该阈值即告警（不改判定，只把"合法地慢"说清楚）
SLOW_PROBE_SEC = 3.0

# OpenAI 兼容的向量化路径：base_url 已含 /v1，这里只追加 /embeddings
EMBEDDINGS_PATH = "/embeddings"


def embedding_failure_reason(e: Exception, config: EmbeddingConfig) -> str:
    """把 httpx 的异常翻译成「下一步该动哪里」

    health_check() 此前把一切异常吞成 False（同 TS-011 / TS-014 的毛病）：
    监控页与配置页只能报一句"健康检查未通过（适配器未给出原因）"，而同一个
    "不可达"至少分四类，处置动作完全不同 —— 端口没起、路径写错（404）、
    鉴权失败、模型加载中，只有说清楚用户才知道该改哪里。
    """
    base = config.base_url or "（未配置 base_url）"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        try:
            body = (e.response.text or "").replace("\n", " ").strip()[:150]
        except Exception:
            body = ""
        if code == 404:
            # 自建向量服务最常见的坑：base_url 少写或多写 /v1，于是打到了
            # 一个不存在的路径上 —— 报"连接失败"会让人去查网络，方向全错
            return (f"{base} 返回 HTTP 404：端点路径不对，应为 "
                    f"{base.rstrip('/')}{EMBEDDINGS_PATH}（OpenAI 兼容路径，"
                    "检查 base_url 是否含 /v1）。原始信息：" + (body or "（无响应体）"))
        if code in (401, 403):
            return (f"{base} 返回 HTTP {code}：鉴权失败（检查 api_key）。"
                    "原始信息：" + (body or "（无响应体）"))
        return (f"{base} 返回 HTTP {code}：服务端拒绝请求。原始信息："
                + (body or "（无响应体）"))
    if isinstance(e, httpx.ConnectTimeout):
        return (f"连接 {base} 超时：地址/端口不通或服务未启动"
                "（也可能是防火墙静默丢包）")
    if isinstance(e, httpx.TimeoutException):
        # 超时配置项是 embedding.timeout：对端已经连上但不吐结果，
        # 常见于模型首次加载 / 推理队列积压（不是网络问题）
        return (f"{base} 已连上但未在 {config.timeout:g}s 内返回："
                "模型可能正在加载或推理过载（该值是 embedding.timeout）")
    if isinstance(e, httpx.ConnectError):
        return (f"无法连接 {base}：地址/端口不通或服务未启动。原始信息："
                + str(e).replace("\n", " ").strip()[:150])
    return f"{type(e).__name__}: {str(e).replace(chr(10), ' ').strip()[:180]}"


def embedding_timeout_reason(config: EmbeddingConfig, budget: float,
                             elapsed: float) -> str:
    """整体预算（HEALTH_BUDGET_SEC）用尽时的原因：区分"超时"与"不可达"

    MySQL / ES 的同一份文案（mysql_timeout_reason / es_timeout_reason）都刻意
    不写成"不可达"：预算内没返回与连不上是两回事，前者往往是模型在加载。
    """
    base = config.base_url or "（未配置 base_url）"
    return (f"{base} 在 {budget:g}s 预算内没有返回（已等 {elapsed:.1f}s）："
            "探测发的是真实的 embed_query，正常毫秒级 —— 对端可能在加载模型、"
            "推理队列积压，或地址指向了只做端口转发却没起服务的代理。"
            "该预算由代码固定，embedding.timeout 只约束业务请求"
            f"（当前 {config.timeout:g}s）")


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
                resp = await self._client.post(EMBEDDINGS_PATH, json=payload)
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

    async def health_detail(self) -> tuple[bool, str]:
        """返回 (是否可用, 可读原因)：失败原因不再被吞掉

        探测走一次真实的 embed_query（含 query_prefix / model / 维度校验），
        这样"测通了"就意味着向量检索路真的能用，而不只是"端口开着"——
        与 ES 把中文分词器一起验掉是同一个道理（TS-015）。
        """
        try:
            vec = await self.embed_query("ping")
        except Exception as e:
            log.warning("embedding_health_failed", base_url=self.config.base_url,
                        model=self.config.model, error=str(e)[:200])
            return False, embedding_failure_reason(e, self.config)
        dim = len(vec)
        if dim != self.config.dim:
            # 维度不一致不是"连不上"，而是配置与向量库对不上：写入/检索都会失败
            return False, (f"返回向量维度 {dim} 与配置 dim={self.config.dim} 不一致"
                           f"（模型 {self.config.model}）：embedding.dim 必须与"
                           "向量库 collection 的维度一致，否则写入与检索都会失败")
        return True, (f"{self.config.base_url} 连接正常"
                      f"（模型 {self.config.model} · 维度 {dim}）")

    async def health_check(self) -> bool:
        ok, _ = await self.health_detail()
        return ok

    async def health_probe(self) -> tuple[bool, str]:
        """带预算的健康检查：容器自检 / 监控页 / 配置页「测试连接」共用同一口径

        与 MySQL / ES 同源（TS-014 / TS-015）。此前本适配器没有 health_probe，
        监控页只能拿 health_check() 外包一层自己的 6s 预算，于是同一个"模型
        加载中"会在这三个界面得到三种结论。
        """
        started = time.perf_counter()
        try:
            ok, reason = await asyncio.wait_for(self.health_detail(),
                                                timeout=HEALTH_BUDGET_SEC)
        except (asyncio.TimeoutError, TimeoutError):
            elapsed = time.perf_counter() - started
            log.warning("embedding_health_timeout", budget=HEALTH_BUDGET_SEC,
                        elapsed=round(elapsed, 2), base_url=self.config.base_url)
            return False, embedding_timeout_reason(self.config, HEALTH_BUDGET_SEC,
                                                   elapsed)
        elapsed = time.perf_counter() - started
        if ok and elapsed >= SLOW_PROBE_SEC:
            # "慢但成功"不会走任何失败分支，必须自己发声（同 mysql_slow_connect）
            log.warning("embedding_slow_probe", elapsed=round(elapsed, 2),
                        base_url=self.config.base_url,
                        hint="向量化响应明显偏慢：检查模型是否在 CPU 上推理、"
                             "是否有并发任务抢占了服务队列")
        return ok, reason


# OpenAI 官方端点协议相同，注册别名
AdapterRegistry._classes["embedding"]["openai_embedding"] = HTTPEmbedding
