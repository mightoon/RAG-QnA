"""
同义词适配器（rag/adapters/synonym.py）

file_based：YAML / Excel 热加载（auto_reload_minutes 定时检查 mtime）
http_service：客户专有术语 API
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import yaml

from rag.config.models import SynonymConfig

from .base import SynonymAdapter
from .registry import AdapterRegistry


@AdapterRegistry.register("synonym", "file_based")
class FileBasedSynonym(SynonymAdapter):
    """
    YAML 格式：
        groups:
          - [华为, 华为技术有限公司, HUAWEI]
          - [交换机, 网络交换机, switch]
        normalize:
          口语词: 标准术语
    """

    def __init__(self, config: SynonymConfig):
        self.config = config
        self._groups: list[list[str]] = []
        self._expand_map: dict[str, list[str]] = {}
        self._normalize_map: dict[str, str] = {}
        self._mtime = 0.0
        self._loaded_at = 0.0
        self.reload()

    def reload(self) -> int:
        path = Path(self.config.file)
        if not path.exists():
            self._groups, self._expand_map, self._normalize_map = [], {}, {}
            return 0
        self._mtime = path.stat().st_mtime
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        self._groups = [g for g in data.get("groups", []) if len(g) >= 2]
        self._expand_map = {}
        for group in self._groups:
            for term in group:
                self._expand_map[term] = [t for t in group if t != term]
        self._normalize_map = dict(data.get("normalize", {}))
        self._loaded_at = time.time()
        return len(self._groups)

    def _maybe_auto_reload(self) -> None:
        interval = self.config.auto_reload_minutes * 60
        if interval > 0 and time.time() - self._loaded_at > interval:
            path = Path(self.config.file)
            if path.exists() and path.stat().st_mtime != self._mtime:
                self.reload()

    def expand(self, term: str) -> list[str]:
        self._maybe_auto_reload()
        return [term] + self._expand_map.get(term, [])

    def normalize(self, term: str) -> str:
        self._maybe_auto_reload()
        return self._normalize_map.get(term, term)

    def all_groups(self) -> list[list[str]]:
        return self._groups


@AdapterRegistry.register("synonym", "http_service")
class HTTPSynonym(SynonymAdapter):
    """对接客户专有术语 API：GET {url}?term=xxx → {"synonyms": [...], "canonical": "..."}"""

    def __init__(self, config: SynonymConfig):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.api_key}"} if config.api_key else {},
            timeout=10,
        )
        self._cache: dict[str, tuple[list[str], str]] = {}
        self._cache_ts: dict[str, float] = {}
        self._ttl = config.auto_reload_minutes * 60 or 1800

    async def _fetch(self, term: str) -> tuple[list[str], str]:
        import time as _t
        now = _t.time()
        if term in self._cache and now - self._cache_ts.get(term, 0) < self._ttl:
            return self._cache[term]
        try:
            resp = await self._client.get("/synonyms", params={"term": term})
            resp.raise_for_status()
            data = resp.json()
            result = (data.get("synonyms", []), data.get("canonical", term))
        except Exception:
            result = ([], term)
        self._cache[term] = result
        self._cache_ts[term] = now
        return result

    async def expand_async(self, term: str) -> list[str]:
        synonyms, _ = await self._fetch(term)
        return [term] + synonyms

    async def normalize_async(self, term: str) -> str:
        _, canonical = await self._fetch(term)
        return canonical

    # 同步接口（供 Pipeline 同步调用点）：退化为直通
    def expand(self, term: str) -> list[str]:
        return [term]

    def normalize(self, term: str) -> str:
        return term

    def reload(self) -> int:
        self._cache.clear()
        self._cache_ts.clear()
        return 0
