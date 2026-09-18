"""
实体链接器（rag/services/entity_linker.py）

规则阶段实现：
- 词典归一化：别名 → 规范名（MySQL entities 表 + 同义词表）
- 启动加载 + 定期刷新（30 分钟）
- 嵌套匹配优先最长别名
"""
from __future__ import annotations

import time

from rag.observability.logging import get_logger

log = get_logger("rag.entity_linker")


class EntityLinker:

    def __init__(self, container):
        self.s = container
        self._alias_map: dict[str, str] = {}     # 别名(含规范名) → 规范名
        self._types: dict[str, str] = {}         # 规范名 → entity_type
        self._loaded_at: float = 0.0
        self._tenant: str | None = None

    async def maybe_reload(self, tenant_id: str) -> None:
        """30 分钟缓存过期后重载词典"""
        if (self._tenant == tenant_id and self._alias_map and
                time.time() - self._loaded_at < 1800):
            return
        try:
            rows = await self.s.meta.list_entities(tenant_id)
        except Exception:
            rows = []
        alias_map: dict[str, str] = {}
        types: dict[str, str] = {}
        for row in rows:
            canonical = row.get("canonical") or row.get("name")
            name = row.get("name")
            if not canonical or not name:
                continue
            alias_map[name] = canonical
            alias_map[canonical] = canonical
            types[canonical] = row.get("entity_type", "generic")
        # 同义词表并入（口语 → 标准术语）
        try:
            for group in self.s.synonym.all_groups():
                if not group:
                    continue
                std = group[0]
                for alias in group:
                    alias_map.setdefault(alias, std)
        except Exception:
            pass
        self._alias_map = alias_map
        self._types = types
        self._tenant = tenant_id
        self._loaded_at = time.time()
        log.info("entity_dict_loaded", entries=len(alias_map))

    def link(self, name: str) -> tuple[str, bool]:
        """单实体归一化：返回 (规范名, 是否命中)"""
        canonical = self._alias_map.get(name)
        if canonical:
            return canonical, True
        return name, False

    async def link_batch(self, tenant_id: str,
                         entities: list[dict]) -> list[dict]:
        """批量归一化：[{name, entity_type}] → 补充 canonical/linked"""
        await self.maybe_reload(tenant_id)
        out = []
        seen: set[str] = set()
        for e in entities:
            name = e.get("name", "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            canonical, linked = self.link(name)
            out.append({
                "name": name,
                "canonical": canonical,
                "entity_type": self._types.get(canonical,
                                               e.get("entity_type", "generic")),
                "linked": linked,
            })
        return out

    def known_canonicals(self) -> list[str]:
        return sorted(set(self._alias_map.values()))
