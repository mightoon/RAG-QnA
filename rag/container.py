"""
服务容器（rag/container.py）

全局单例：持有配置 + 全部适配器实例 + 核心服务。
启动原则：**任何外部依赖不可用都不阻断启动**。
- 可选依赖（向量库/ES/图谱/业务数据）失败 → 置 None，检索路自动关闭
- 核心依赖（LLM/Embedding/MySQL/认证/存储/同义词）失败或未配置
  → 自动降级为本地实现（mock / 内存 / dev / local_fs），记录到
  self.degraded 供 UI 展示，用户在配置页补齐后热重建生效
"""
from __future__ import annotations

from rag.adapters.base import (
    AuthAdapter, BusinessDataAdapter, DocParserAdapter, EmbeddingAdapter,
    FullTextSearchAdapter, KnowledgeGraphAdapter, LLMAdapter,
    MySQLMetaAdapter, StorageAdapter, SynonymAdapter, VectorStoreAdapter,
)
from rag.adapters.redis_cache import (
    REDIS_HEALTH_BUDGET_SEC, make_client, redis_failure_reason,
)
from rag.adapters.registry import AdapterRegistry
from rag.config.models import AppConfig
from rag.observability.logging import get_logger

log = get_logger("rag.container")


class CoreDependencyError(RuntimeError):
    """遗留异常类型：核心依赖不可用（现已自动降级，不再抛出，保留兼容）"""


# 核心依赖失败/未配置时的本地降级实现（type → 降级注册名）
_CORE_FALLBACKS: dict[str, tuple[str, str]] = {
    "llm": ("llm", "mock"),
    "embedding": ("embedding", "mock"),
    "mysql_meta": ("mysql_meta", "memory"),
    "auth": ("auth", "dev"),
    "storage": ("storage", "local_fs"),
    "synonym": ("synonym", "none"),
}

# 本地实现（无需 base_url，不算“未配置”）
_LOCAL_IMPL = {"mock", "memory", "dev", "local_fs", "none"}

# ── 单段热应用映射 ──────────────────────────────────────────
# 配置段名 → (容器属性, 适配器类型, 是否可选依赖)，口径与 initialize() 一致。
# 只列「自己独占一个适配器」的段：保存其中一段只需重建这一段；
# 不在表内的段（检索策略/权限/编排等无独立适配器的段）仍走整容器重建。
SECTION_ADAPTERS: dict[str, tuple[str, str, bool]] = {
    "mysql_meta": ("meta", "mysql_meta", False),
    "vector_store": ("vector", "vector_store", True),
    "fulltext": ("fulltext", "fulltext", True),
    "storage": ("storage", "storage", False),
    "knowledge_graph": ("graph", "knowledge_graph", True),
    "business_data": ("business", "business_data", True),
    "synonym": ("synonym", "synonym", False),
    "llm": ("llm", "llm", False),
    "embedding": ("embedding", "embedding", False),
}

# 段名 → 该段涉及的全部 degraded 键。历史原因同一组件存在两套键
# （vector/vector_store、graph/knowledge_graph），重建成功时两套都要清，
# 否则界面会出现「已经连上了却还挂着降级」的自相矛盾状态。
SECTION_DEGRADED_KEYS: dict[str, tuple[str, ...]] = {
    "mysql_meta": ("mysql_meta",),
    "vector_store": ("vector", "vector_store"),
    "fulltext": ("fulltext",),
    "storage": ("storage",),
    "knowledge_graph": ("graph", "knowledge_graph"),
    "business_data": ("business", "business_data"),
    "synonym": ("synonym",),
    "llm": ("llm",),
    "embedding": ("embedding",),
    "redis": ("redis",),
}

# 可选依赖的中文名（文案与 initialize() 保持一致）
_OPTIONAL_LABELS = {"vector": "向量库", "fulltext": "全文检索",
                    "graph": "知识图谱", "business": "业务数据"}


async def _close_quietly(inst) -> None:
    """尽力释放旧连接：换上新实例之后才调用，避免打断在途请求"""
    closer = getattr(inst, "aclose", None) or getattr(inst, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if hasattr(result, "__await__"):
            await result
    except Exception as e:
        log.warning("adapter_close_failed", error=str(e))


class ServiceContainer:
    """依赖注入容器：一处构建，处处引用"""

    def __init__(self, config: AppConfig):
        self.config = config
        # 降级记录：组件名 → 原因（UI 配置页展示，指导用户补齐配置）
        self.degraded: dict[str, str] = {}
        if config.noconnection:
            self._apply_noconnection(config)
        # 核心适配器（失败/未配置 → 自动降级为本地实现，不阻断启动）
        self.llm: LLMAdapter = self._create_core(
            "llm", config.llm.adapter, config.llm,
            unconfigured=(not (config.llm.base_url or "").strip()
                          and config.llm.adapter not in _LOCAL_IMPL))
        self.embedding: EmbeddingAdapter = self._create_core(
            "embedding", config.embedding.adapter, config.embedding,
            unconfigured=(not (config.embedding.base_url or "").strip()
                          and config.embedding.adapter not in _LOCAL_IMPL))
        self.meta: MySQLMetaAdapter = self._create_core(
            "mysql_meta", config.mysql_meta.adapter, config.mysql_meta)
        self.auth: AuthAdapter = self._create_core(
            "auth", config.auth.adapter, config.auth)
        self.synonym: SynonymAdapter = self._create_core(
            "synonym", config.synonym.adapter, config.synonym)

        # 慢构造适配器（网络连接型）并行构建：总耗时 = 最慢单项而非之和
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="adapter") as ex:
            f_storage = ex.submit(
                self._create_core, "storage",
                config.storage.adapter, config.storage)
            f_vector = ex.submit(
                self._try_create, "vector_store",
                config.vector_store.adapter, config.vector_store,
                config.vector_store.enabled)
            f_fulltext = ex.submit(
                self._try_create, "fulltext",
                config.fulltext.adapter, config.fulltext,
                config.fulltext.enabled)
            f_graph = ex.submit(
                self._try_create, "knowledge_graph",
                config.knowledge_graph.adapter, config.knowledge_graph,
                config.knowledge_graph.enabled)
            f_business = ex.submit(
                self._try_create, "business_data",
                config.business_data.adapter, config.business_data,
                config.business_data.enabled)
            self.storage: StorageAdapter = f_storage.result()
            self.vector: VectorStoreAdapter | None = f_vector.result()
            self.fulltext: FullTextSearchAdapter | None = f_fulltext.result()
            self.graph: KnowledgeGraphAdapter | None = f_graph.result()
            self.business: BusinessDataAdapter | None = f_business.result()
        if self.business is not None:
            self.business.set_llm(self.llm)

        # 文档解析器集合：{扩展名: 实例}
        self.parsers: dict[str, DocParserAdapter] = \
            AdapterRegistry.create_parsers(config)

        # Redis（记忆/会话/进度总线）
        # 客户端参数（超时/重试/认证口径）统一在 rag.adapters.redis_cache 里，
        # 与配置页「测试连接」共用同一份 —— 否则两边各用各的超时，就会出现
        # "页面测通了、保存却说不可用"（TS-014 / TS-015 / TS-019）
        self.redis = None
        if not config.noconnection:
            try:
                self.redis = make_client(config.redis)
            except Exception as e:
                log.warning("redis_init_failed", error=str(e))

        # 核心服务（延迟装配，在 initialize() 中创建）
        self.memory_service = None
        self.ephemeral_service = None
        self.notification_service = None
        self.entity_linker = None
        self.progress_bus = None
        self.consistency_checker = None
        self.workflows = None                   # WorkflowRegistry
        self.ingest_coordinator = None          # IngestionCoordinator

    @staticmethod
    def _try_create(adapter_type: str, name: str, config,
                    enabled: bool = True):
        if not enabled:
            return None
        try:
            return AdapterRegistry.create(adapter_type, name, config)
        except Exception as e:
            log.warning("optional_adapter_unavailable",
                        adapter_type=adapter_type, name=name, error=str(e))
            return None

    def _create_core(self, adapter_type: str, name: str, config,
                     unconfigured: bool = False):
        """核心适配器构造：失败/未配置 → 降级为本地实现，绝不抛错阻断启动"""
        try:
            if unconfigured:
                raise ValueError("未配置（base_url 为空）")
            # 注：mysql_meta 不做跨适配器的引擎/连接缓存 —— 每次构造都持有
            # 自己的引擎（见 rag/adapters/mysql_meta.py），代价是"重建适配器"
            # 即真实建连，换来的是探测结果永远反映此刻服务端的真实状态（TS-014）
            return AdapterRegistry.create(adapter_type, name, config)
        except Exception as e:
            fb_type, fb_name = _CORE_FALLBACKS.get(
                adapter_type, (adapter_type, "mock"))
            reason = ("未配置，使用内置降级实现" if unconfigured
                      else f"「{name}」初始化失败（{e}），已降级为本地实现")
            self.degraded[adapter_type] = reason
            log.warning("core_adapter_degraded",
                        adapter=adapter_type, configured=name,
                        fallback=fb_name, error=str(e))
            import rag.adapters.mocks  # noqa: F401        触发 mock 注册
            import rag.adapters.memory_meta  # noqa: F401  触发内存 meta 注册
            return AdapterRegistry.create(adapter_type, fb_name, config)

    @staticmethod
    def _apply_noconnection(config: AppConfig) -> None:
        """--noconnection 演示模式：外部依赖全部替换为本地实现

        - LLM / Embedding → mock（演示回复 + 确定性向量）
        - MySQL 元数据   → 内存实现（重启即失）
        - 认证           → dev（免认证）
        - 对象存储       → local_fs（本地目录）
        - 向量/全文/图谱/业务数据 → 禁用（检索路自动降级）
        - Redis          → 跳过（会话记忆自动降级为进程内存）
        """
        from rag.config.models import StorageConfig
        import rag.adapters.mocks  # noqa: F401        触发 mock 注册
        import rag.adapters.memory_meta  # noqa: F401  触发内存 meta 注册

        config.llm.adapter = "mock"
        config.embedding.adapter = "mock"
        config.mysql_meta.adapter = "memory"
        config.auth.adapter = "dev"
        config.storage = StorageConfig(adapter="local_fs", enabled=True)
        config.vector_store.enabled = False
        config.fulltext.enabled = False
        config.knowledge_graph.enabled = False
        config.business_data.enabled = False
        log.warning(
            "noconnection_mode_active",
            note="演示模式：LLM/向量库/ES/MySQL/Redis 均为本地 Mock，"
                 "数据不持久化，仅供界面联调")

    async def initialize(self) -> None:
        """启动自检 + 服务装配。所有依赖失败均自动降级，不抛错。
        健康检查全部并行执行（asyncio.gather），总耗时 = 最慢单项
        而非各依赖超时之和。"""
        import asyncio

        # 失败原因由适配器的带预算探测给出（超时 ≠ 不可达，见 TS-014 / TS-015）
        meta_reason: list[str] = []
        opt_reason: dict[str, str] = {}

        async def _check_meta() -> bool:
            probe = getattr(self.meta, "health_probe", None)
            if probe is None:              # 兼容不自带预算的适配器
                try:
                    return await asyncio.wait_for(
                        self.meta.health_check(), timeout=6)
                except Exception:
                    return False
            ok, reason = await probe()
            if not ok:
                meta_reason.append(reason)
            return ok

        async def _check_optional(attr: str) -> bool:
            adapter = getattr(self, attr)
            if adapter is None:
                return False
            probe = getattr(adapter, "health_probe", None)
            reason = ""
            try:
                if probe is not None:
                    # 适配器自带预算（MySQL / ES）：与配置页「测试连接」共用
                    # 同一口径，否则会出现"页面测通了、保存却说不可用"
                    ok, reason = await probe()
                else:
                    # 兜底分支也必须用同一份预算常量
                    ok = await asyncio.wait_for(
                        adapter.health_check(), timeout=6)
            except Exception as e:
                ok, reason = False, f"健康检查异常：{e}"
            if not ok:
                # 原因要一路带到 degraded → 配置页提示，不能只留在日志里
                # （否则用户只能看到一句无信息的"健康检查失败"，见 TS-011/015）
                opt_reason[attr] = reason or "健康检查未通过（适配器未给出原因）"
            return ok

        redis_reason: list[str] = []

        async def _check_redis() -> bool:
            if self.redis is None:
                return False
            try:
                await asyncio.wait_for(self.redis.ping(),
                                       timeout=REDIS_HEALTH_BUDGET_SEC)
                return True
            except Exception as e:
                # 原因要一路带到 degraded → 配置页提示：Redis 的"连不上"至少分
                # 认证失败 / DB 编号越界 / 地址端口不通 / 超时四类，只回一句
                # "Redis 不可达"等于把归因工作全丢给用户（TS-019 / TS-011）
                redis_reason.append(redis_failure_reason(e, self.config.redis))
                return False

        # 并行健康检查：meta + 可选依赖 + redis 同时探测
        meta_ok, vec_ok, ft_ok, kg_ok, biz_ok, redis_ok = await asyncio.gather(
            _check_meta(),
            _check_optional("vector"), _check_optional("fulltext"),
            _check_optional("graph"), _check_optional("business"),
            _check_redis(),
        )

        # 核心依赖失败降级为本地实现（不阻断启动）
        if not meta_ok:
            mcfg = self.config.mysql_meta
            reason = (meta_reason[0] if meta_reason
                      else f"MySQL 不可达（{mcfg.host}:{mcfg.port}）")
            self.degraded["mysql_meta"] = (
                f"{reason}；元数据已降级为内存模式"
                "（数据不持久化，请在配置页补齐连接信息）")
            log.warning("meta_degraded_memory", host=mcfg.host,
                        port=mcfg.port, reason=reason[:200])
            import rag.adapters.memory_meta  # noqa: F401
            self.meta = AdapterRegistry.create("mysql_meta", "memory", None)
        # 可选依赖失败降级
        for ok, attr, label in ((vec_ok, "vector", "向量库"),
                                (ft_ok, "fulltext", "全文检索"),
                                (kg_ok, "graph", "知识图谱"),
                                (biz_ok, "business", "业务数据")):
            adapter = getattr(self, attr)
            if adapter is not None and not ok:
                reason = opt_reason.get(attr) or f"{label}健康检查未通过"
                log.warning("optional_adapter_degraded", component=label,
                            reason=reason[:200])
                self.degraded[attr] = f"{label}不可用：{reason}，对应功能已关闭"
                setattr(self, attr, None)
        if self.redis is not None and not redis_ok:
            rcfg = self.config.redis
            reason = (redis_reason[0] if redis_reason
                      else f"Redis 不可达（{rcfg.host}:{rcfg.port}）")
            log.warning("redis_degraded", reason=reason[:200],
                        note="会话/记忆退化为进程内存")
            self.degraded["redis"] = (
                f"{reason}；会话/记忆已退化为进程内存"
                "（重启即失，请在配置页补齐连接信息）")
            self.redis = None

        # 确保存储结构（索引/collection/schema）——失败仅降级对应组件
        if self.vector is not None and self.embedding is not None:
            try:
                await self.vector.ensure_collection(
                    "default", self.embedding.dim)
            except Exception as e:
                log.warning("vector_ensure_failed", error=str(e))
                self.degraded["vector_store"] = f"向量库初始化失败（{e}），已关闭"
                self.vector = None
        if self.fulltext is not None:
            try:
                await self.fulltext.ensure_index("default")
            except Exception as e:
                log.warning("fulltext_ensure_failed", error=str(e))
                self.degraded["fulltext"] = f"全文检索初始化失败（{e}），已关闭"
                self.fulltext = None
        if self.graph is not None:
            try:
                await self.graph.ensure_schema()
            except Exception as e:
                log.warning("graph_ensure_failed", error=str(e))
                self.degraded["knowledge_graph"] = f"知识图谱初始化失败（{e}），已关闭"
                self.graph = None

        # 核心服务装配
        from rag.services.memory import MemoryService
        from rag.services.ephemeral import EphemeralService
        from rag.services.notifications import NotificationService
        from rag.services.entity_linker import EntityLinker
        from rag.services.progress import ProgressBus
        from rag.services.consistency import ConsistencyChecker

        self.memory_service = MemoryService(self)
        self.ephemeral_service = EphemeralService(self)
        self.notification_service = NotificationService(self)
        self.entity_linker = EntityLinker(self)
        self.progress_bus = ProgressBus(self.redis)
        self.consistency_checker = ConsistencyChecker(self)
        self.reranker = None    # 重排为 pipeline 内本地实现，无独立适配器

        # Pipeline 注册表 + 入库协调器
        from rag.pipeline.engine import WorkflowRegistry
        from rag.ingestion.coordinator import IngestionCoordinator

        self.workflows = WorkflowRegistry(self.config.workflows_file)
        self.ingest_coordinator = IngestionCoordinator(self, self.workflows)

        log.info("container_initialized",
                 vector=self.vector is not None,
                 fulltext=self.fulltext is not None,
                 graph=self.graph is not None,
                 business=self.business is not None,
                 redis=self.redis is not None,
                 degraded=dict(self.degraded))

    # ── 单段热应用（保存某一个服务只动这一个服务）──────────

    def _clear_degraded(self, section: str) -> None:
        for key in SECTION_DEGRADED_KEYS.get(section, (section,)):
            self.degraded.pop(key, None)

    def _section_online(self, section: str) -> bool:
        """本段是否真的连上：对象存在且本段没有降级记录

        （MySQL 降级后会挂一个内存实现的替身，光看"对象非空"会误判为在线）
        """
        spec = SECTION_ADAPTERS.get(section)
        attr = spec[0] if spec else "redis"
        if getattr(self, attr, None) is None:
            return False
        keys = set(SECTION_DEGRADED_KEYS.get(section, (section,)))
        return not (keys & set(self.degraded))

    async def apply_section(self, section: str) -> None:
        """只热应用一个配置段：重建该段适配器，并单独自检。

        与 rebuild_container（整容器重建）的唯一区别就是"只管这一段"：
        其它服务的适配器、连接、健康检查、后台任务一律不碰 —— 保存 MySQL
        不会去重连 ES/Milvus/Redis，也不会因为它们此刻抖动就把对应功能关掉。
        前置条件：调用方已把新配置写进 self.config.<section>。
        """
        if section == "redis":
            await self._apply_redis()
            return
        spec = SECTION_ADAPTERS.get(section)
        if spec is None:
            raise ValueError(f"不支持单段热应用的配置段: {section}")
        attr, atype, optional = spec
        cfg = getattr(self.config, section)
        name = str(getattr(cfg, "adapter", "") or "")
        unconfigured = (atype in ("llm", "embedding")
                        and not (getattr(cfg, "base_url", "") or "").strip()
                        and name not in _LOCAL_IMPL)
        # 先清降级记录：重建失败会由 _create_core / _selfcheck 重新登记
        self._clear_degraded(section)
        # 必须摘掉本段单例，否则 create 会直接返回带旧配置的旧实例
        old = AdapterRegistry.drop(atype, name)
        if optional:
            new = self._try_create(atype, name, cfg,
                                   getattr(cfg, "enabled", True))
        else:
            new = self._create_core(atype, name, cfg, unconfigured=unconfigured)
        setattr(self, attr, new)
        if atype == "business_data" and new is not None:
            new.set_llm(self.llm)          # 业务数据要借 LLM 改写查询
        elif atype == "llm" and self.business is not None:
            self.business.set_llm(new)     # 换了 LLM，同步给业务数据
        await self._selfcheck_section(section)
        await _close_quietly(old)
        log.info("section_applied", section=section, adapter=name,
                 online=self._section_online(section), degraded=dict(self.degraded))

    async def _selfcheck_section(self, section: str) -> None:
        """只给这一段做健康检查与结构校准（口径/文案同 initialize）"""
        import asyncio

        if section == "mysql_meta":
            cfg = self.config.mysql_meta
            ok, reason = False, ""
            if self.meta is not None:
                # 走适配器的带预算探测：与配置页「测试连接」同一口径，
                # 否则会出现"页面测通了、保存却说不可达"（TS-014）
                probe = getattr(self.meta, "health_probe", None)
                try:
                    if probe is not None:
                        ok, reason = await probe()
                    else:
                        # 兜底分支（无 health_probe 的适配器）也必须用同一份预算，
                        # 否则又会出现"两条链路判定不一致"（TS-014）
                        from rag.adapters.mysql_meta import HEALTH_BUDGET_SEC
                        ok = await asyncio.wait_for(
                            self.meta.health_check(), timeout=HEALTH_BUDGET_SEC)
                except Exception:
                    ok = False
            if not ok:
                reason = reason or f"MySQL 不可达（{cfg.host}:{cfg.port}）"
                log.warning("meta_degraded_memory", host=cfg.host,
                            port=cfg.port, reason=reason[:200])
                self.degraded["mysql_meta"] = (
                    f"{reason}；元数据已降级为内存模式"
                    "（数据不持久化，请在配置页补齐连接信息）")
                import rag.adapters.memory_meta  # noqa: F401
                self.meta = AdapterRegistry.create("mysql_meta", "memory", None)
            return

        attr, _atype, optional = SECTION_ADAPTERS[section]
        if optional:
            # 可选依赖才有「检查不过 → 功能关闭」这一步；
            # 核心适配器（llm/embedding/storage/synonym）与启动时一样
            # 只做构造降级，绝不因健康检查失败被置空
            inst = getattr(self, attr, None)
            if inst is None:               # 未启用 / 构造失败，无自检可做
                return
            probe = getattr(inst, "health_probe", None)
            reason = ""
            try:
                if probe is not None:
                    # 与配置页「测试连接」共用同一预算与提示口径（TS-014/015）
                    ok, reason = await probe()
                else:
                    ok = await asyncio.wait_for(inst.health_check(), timeout=6)
            except Exception as e:
                ok, reason = False, f"健康检查异常：{e}"
            if not ok:
                label = _OPTIONAL_LABELS.get(attr, section)
                reason = reason or f"{label}健康检查未通过"
                log.warning("optional_adapter_degraded", component=label,
                            section=section, reason=reason[:200])
                self.degraded[attr] = f"{label}不可用：{reason}，对应功能已关闭"
                setattr(self, attr, None)
                return
        elif section != "embedding":
            return                         # 其余核心段：构造成功即生效

        # 结构校准：失败只降级这一个组件（与启动时逐组件降级一致）
        target = None
        if section == "vector_store":
            target = ("vector", "vector_store", "向量库")
        elif section == "fulltext":
            target = ("fulltext", "fulltext", "全文检索")
        elif section == "knowledge_graph":
            target = ("graph", "knowledge_graph", "知识图谱")
        elif section == "embedding" and self.vector is not None:
            # 换了 embedding（维度可能变）→ 顺带校准向量库 collection
            target = ("vector", "vector_store", "向量库")
        if target is None:
            return
        t_attr, t_key, t_label = target
        adapter = getattr(self, t_attr, None)
        if adapter is None:
            return
        try:
            if t_attr == "vector":
                if self.embedding is None:
                    return
                await adapter.ensure_collection("default", self.embedding.dim)
            elif t_attr == "fulltext":
                await adapter.ensure_index("default")
            else:
                await adapter.ensure_schema()
        except Exception as e:
            log.warning("ensure_failed", section=section,
                        component=t_label, error=str(e))
            self.degraded[t_key] = f"{t_label}初始化失败（{e}），已关闭"
            setattr(self, t_attr, None)

    async def _apply_redis(self) -> None:
        """Redis 段单段热应用：换客户端实例，并同步快照过它的服务"""
        import asyncio

        if self.config.noconnection:
            return
        cfg = self.config.redis
        self._clear_degraded("redis")
        old = self.redis
        new = None
        try:
            new = make_client(cfg)
        except Exception as e:
            log.warning("redis_init_failed", error=str(e))
        ok = False
        reason = ""
        if new is not None:
            try:
                ok = bool(await asyncio.wait_for(
                    new.ping(), timeout=REDIS_HEALTH_BUDGET_SEC))
            except Exception as e:
                ok = False
                # 与容器自检同一份翻译：热应用失败也要给出"该怎么处置"的原因
                reason = redis_failure_reason(e, cfg)
        if not ok:
            await _close_quietly(new)
            new = None
            reason = reason or f"Redis 不可达（{cfg.host}:{cfg.port}）"
            log.warning("redis_degraded", reason=reason[:200],
                        note="会话/记忆退化为进程内存")
            self.degraded["redis"] = (
                f"{reason}；会话/记忆已退化为进程内存")
        self.redis = new
        # 快照过 redis 的地方要一并换掉：否则会话仍写旧连接、进度仍发旧通道
        mem = getattr(self, "memory_service", None)
        if mem is not None:
            mem.redis = new
            mem._prefix = f"{cfg.prefix}session:"
        bus = getattr(self, "progress_bus", None)
        if bus is not None:
            bus.redis = new
        await _close_quietly(old)
        log.info("section_applied", section="redis",
                 online=self._section_online("redis"))

    async def shutdown(self) -> None:
        """优雅关闭：释放连接池

        必须与 `_close_quietly()` 用**同一套查找规则**（先 `aclose` 再 `close`）：
        旧实现只找 `close`，而 Milvus / Qdrant / ES 三个适配器手里只有 `aclose`
        —— 于是关机时它们被**静默跳过**，只在进程退出时由解释器兜底打印
        `Unclosed client session` / `Unclosed connector`，gRPC 通道与 httpx 池
        一起留到进程结束（同 TS-016 第 5 条）。顺手把"释放失败"从静默吞掉改成
        告警：关机漏关是安静发生的故障，不发声就永远查不出来。
        """
        for closer in (self.meta, self.vector, self.fulltext, self.graph):
            await _close_quietly(closer)
        if self.redis is not None:
            try:
                await self.redis.aclose()
            except Exception:
                pass

    # ── 便捷访问 ───────────────────────────────────────────

    def enabled_paths(self) -> set[str]:
        """当前实际可用的检索路（配置启用 ∩ 适配器在线）"""
        paths = set()
        if self.fulltext is not None:
            if self.config.retrieval.enable_kw_exact:
                paths.add("kw_exact")
            if self.config.retrieval.enable_bm25:
                paths.add("bm25")
        if self.vector is not None and self.config.retrieval.enable_vector:
            paths.add("vector")
        if self.graph is not None and self.config.retrieval.enable_graph:
            paths.add("graph")
        if self.business is not None and self.config.retrieval.enable_structured:
            paths.add("structured")
        if self.config.retrieval.enable_ephemeral:
            paths.add("ephemeral")
        return paths

    def get_parser(self, filename: str) -> DocParserAdapter:
        from pathlib import Path
        ext = Path(filename).suffix.lower()
        parser = self.parsers.get(ext)
        if parser is None:
            raise ValueError(
                f"不支持的文件格式: {ext}，"
                f"支持: {sorted(self.parsers.keys())}")
        return parser
