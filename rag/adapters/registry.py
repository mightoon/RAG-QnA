"""
适配器注册中心（rag/adapters/registry.py）

启动时按配置实例化适配器并保持单例；
配置 adapter: "名称" 字段指定实现，运行时动态创建。
"""
from __future__ import annotations

from typing import Any, Type

from .base import (
    AuthAdapter, BusinessDataAdapter, DocParserAdapter, EmbeddingAdapter,
    FullTextSearchAdapter, KnowledgeGraphAdapter, LLMAdapter,
    MySQLMetaAdapter, StorageAdapter, SynonymAdapter, VectorStoreAdapter,
)

_ADAPTER_TYPES = {
    "llm": LLMAdapter,
    "embedding": EmbeddingAdapter,
    "vector_store": VectorStoreAdapter,
    "fulltext": FullTextSearchAdapter,
    "mysql_meta": MySQLMetaAdapter,
    "doc_parser": DocParserAdapter,
    "business_data": BusinessDataAdapter,
    "knowledge_graph": KnowledgeGraphAdapter,
    "synonym": SynonymAdapter,
    "auth": AuthAdapter,
    "storage": StorageAdapter,
}


class AdapterNotFoundError(ValueError):
    pass


class AdapterRegistry:
    """中央注册中心：类型 → {注册名: 实现类}"""

    _classes: dict[str, dict[str, type]] = {t: {} for t in _ADAPTER_TYPES}
    _instances: dict[tuple[str, str], Any] = {}

    @classmethod
    def register(cls, adapter_type: str, name: str):
        """装饰器：@AdapterRegistry.register("llm", "my_llm")"""
        def deco(klass: type):
            if adapter_type not in cls._classes:
                raise AdapterNotFoundError(f"未知适配器类型: {adapter_type}")
            cls._classes[adapter_type][name] = klass
            return klass
        return deco

    @classmethod
    def register_parser(cls, name: str):
        """DocParser 专用注册（按扩展名路由的解析器集合）"""
        return cls.register("doc_parser", name)

    @classmethod
    def create(cls, adapter_type: str, name: str, config: Any) -> Any:
        """按注册名实例化适配器（带单例缓存，key=(type,name)）"""
        key = (adapter_type, name)
        if key in cls._instances:
            return cls._instances[key]
        impls = cls._classes.get(adapter_type, {})
        if name not in impls:
            raise AdapterNotFoundError(
                f"适配器未注册: {adapter_type}/{name}，"
                f"可用: {list(impls.keys())}"
            )
        instance = impls[name](config)
        cls._instances[key] = instance
        return instance

    @classmethod
    def create_parsers(cls, config: Any) -> dict[str, DocParserAdapter]:
        """实例化所有 DocParser 实现 → {扩展名: 实例}"""
        parsers: dict[str, DocParserAdapter] = {}
        for name, klass in cls._classes["doc_parser"].items():
            instance = klass(config)
            for ext in instance.supported_extensions:
                parsers[ext] = instance
        return parsers

    @classmethod
    def get_class(cls, adapter_type: str, name: str) -> Type | None:
        """按注册名取实现类（不实例化、不进单例缓存）

        配置页「测试连接」要用**表单当前值**现建一个一次性实例：走 create()
        会命中单例缓存、拿到带旧配置的运行中实例，于是"改了地址再测"报的
        还是旧地址的状态（与 TS-016 同一个坑）。
        """
        return cls._classes.get(adapter_type, {}).get(name)

    @classmethod
    def list_implementations(cls, adapter_type: str) -> list[str]:
        return sorted(cls._classes.get(adapter_type, {}).keys())

    @classmethod
    def name_of(cls, adapter_type: str, instance: Any) -> str:
        """实例 → 注册名（「配置里写的是谁」≠「此刻跑的是谁」）

        容器降级后实例类型已经换人（http_embedding → mock、
        mysql_real → memory），状态展示若只读配置名，就会把本地 mock
        当成真实外部服务汇报 —— 监控页必须读运行名。
        """
        if instance is None:
            return ""
        for name, klass in cls._classes.get(adapter_type, {}).items():
            if type(instance) is klass:
                return name
        return ""

    @classmethod
    def drop(cls, adapter_type: str, name: str) -> Any:
        """摘掉指定适配器的单例缓存并返回旧实例（单段热重建用）。

        与 clear_cache 的区别：只动这一个 key，其它服务的适配器实例和
        连接原样保留 —— 保存 MySQL 不会顺带重建 ES/Milvus/Redis 的连接。
        """
        return cls._instances.pop((adapter_type, name), None)

    @classmethod
    def clear_cache(cls) -> None:
        """测试用：清空单例缓存"""
        cls._instances.clear()


# 触发内置实现注册（import 副作用）
def _load_builtin_implementations() -> None:
    from . import llm, embedding, auth, storage, synonym  # noqa: F401
    from . import vector_store, fulltext, mysql_meta       # noqa: F401
    from . import doc_parser, business_data, knowledge_graph  # noqa: F401


_load_builtin_implementations()
