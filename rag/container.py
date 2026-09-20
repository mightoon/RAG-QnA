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

import time

from rag.adapters.base import (
    AuthAdapter, BusinessDataAdapter, DocParserAdapter, EmbeddingAdapter,
    FullTextSearchAdapter, KnowledgeGraphAdapter, LLMAdapter,
    MetaStoreAdapter, StorageAdapter, SynonymAdapter, VectorStoreAdapter,
)
from rag.adapters.redis_cache import (
    REDIS_HEALTH_BUDGET_SEC, make_client, redis_failure_reason,
)
from rag.adapters.registry import AdapterRegistry
from rag.config.models import AppConfig
from rag.observability.logging import get_logger
from rag.vector_space import judge_space, space_tag, tag_label

log = get_logger("rag.container")


class CoreDependencyError(RuntimeError):
    """遗留异常类型：核心依赖不可用（现已自动降级，不再抛出，保留兼容）"""


# 核心依赖失败/未配置时的本地降级实现（type → 降级注册名）
_CORE_FALLBACKS: dict[str, tuple[str, str]] = {
    "llm": ("llm", "mock"),
    "embedding": ("embedding", "mock"),
    "meta": ("meta", "memory"),
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
    "meta": ("meta", "meta", False),
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
    "meta": ("meta",),
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

# ── 运行期自愈（修好的依赖不必重启进程）──────────────────────
# 启动自检失败的段会被置为 None（功能关闭），此后**没有任何链路会再碰它**：
# 监控页读到 None 就一直复述启动时的结论，用户把 Milvus 修好也不会变绿，
# 除非他想到再去配置页保存一次。于是「修好了却还挂着降级」成了一个不可自解的
# 假故障 —— 后台自愈循环就是补上这个重试入口。
#
# 只重试「外部服务连不上」这类段：未配置（llm/embedding 没填 base_url）重试
# 一万次也还是那句「未配置」；storage/synonym/auth 的降级是本地实现，没有
# 可恢复的外部依赖。
# 公开（无前导下划线）：监控页要拿它判断"这一行失联后有没有人管"——自己再抄一份
# 名单，迟早会和这里对不上，然后在界面上表现成"显示在自动重连、其实没人重试"。
RECOVERABLE_SECTIONS: tuple[str, ...] = (
    "meta", "vector_store", "fulltext", "knowledge_graph",
    "business_data", "redis", "llm", "embedding",
)

# 后台自愈循环的间隔 / 同一段两次重试的最小间隔。每次重试都是真建连 + 真探测，
# 探得太频只会刷满日志、搅乱连接池，而外部依赖恢复通常是分钟级动作。
RECOVER_INTERVAL_SEC = 30.0
RECOVER_COOLDOWN_SEC = 20.0


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
        # 自愈状态：段名 → {"attempts": 重试次数, "lastAt": 上次重试时间戳}。
        # 既给后台循环做退避，也给监控页显示「已经自动重连过几次」——否则用户
        # 看到"还是不可用"只会以为程序没在管，又会来问同一个问题。
        self.recovery_stats: dict[str, dict] = {}
        # 运行监控快照：最近一次采样结果 + 采样时刻 + 单飞锁（懒建）。
        # 采样要逐个探真实依赖（最慢那项按它自己的预算走，可能是十几秒），
        # 属于"贵且结论对同一时刻的所有请求都一样"的东西 → 缓存 + 单飞：
        # 页面打开时前端紧接着取的那一次、以及多个标签页/连续刷新，共用一个结果，
        # 而不是各探一遍。TTL 很短（见 web/routes._MONITOR_SNAPSHOT_TTL_SEC），
        # 过期就重采，所以这里不是"拿旧数据糊弄"，只是不让同一瞬间重复探测。
        # 由 web 层读写（routes._monitor_snapshot_cached）。
        self.monitor_snapshot: dict | None = None
        self.monitor_snapshot_at: float = 0.0
        self._monitor_lock = None
        # 单段热应用互斥锁（懒建，理由见 _apply_lock_for）
        self._apply_lock = None
        # 向量空间指纹：collection → (写入是否允许, 检索是否可用, 原因)。由
        # sync_vector_space() 预先算好 —— enabled_paths() 每次问答都会被调用，
        # 不能在那里 await 向量库（详见 sync_vector_space）
        self._space_state: dict[str, tuple[bool, bool, str]] = {}
        # 已提示过的指纹告警（同一条原因只刷一次：入库是逐文档调用的）
        self._space_logged: set[str] = set()
        self.closed = False
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
        self.meta: MetaStoreAdapter = self._create_core(
            "meta", config.meta.adapter, config.meta)
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
            # 注：元数据库不做跨适配器的引擎/连接缓存 —— 每次构造都持有
            # 自己的引擎（见 rag/adapters/meta_mysql.py），代价是"重建适配器"
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
            import rag.adapters.meta_memory  # noqa: F401  触发内存 meta 注册
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
        import rag.adapters.meta_memory  # noqa: F401  触发内存 meta 注册

        config.llm.adapter = "mock"
        config.embedding.adapter = "mock"
        config.meta.adapter = "memory"
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
            mcfg = self.config.meta
            reason = (meta_reason[0] if meta_reason
                      else f"MySQL 不可达（{mcfg.host}:{mcfg.port}）")
            self.degraded["meta"] = (
                f"{reason}；元数据已降级为内存模式"
                "（数据不持久化，请在配置页补齐连接信息）")
            log.warning("meta_degraded_memory", host=mcfg.host,
                        port=mcfg.port, reason=reason[:200])
            import rag.adapters.meta_memory  # noqa: F401
            self.meta = AdapterRegistry.create("meta", "memory", None)
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
            else:
                # 结构就绪后立刻对齐向量空间指纹：新集合在这里打标，存量集合
                # 在这里被判"能否继续写入/检索"（见 sync_vector_space）
                try:
                    await self.sync_vector_space("default", stamp=True)
                except Exception as e:      # 指纹问题绝不阻断启动
                    log.warning("vector_space_sync_failed", error=str(e))
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

    # ── 运行期自愈 ──────────────────────────────────────────

    def monitor_lock(self):
        """监控快照单飞锁（懒建：容器可能在无事件循环的上下文构造，同 _apply_lock_for）

        作用不是"防止并发读"，而是防止并发**采**：同一时刻只允许一轮全量探测，
        后来者等这一轮的结果（见 routes._monitor_snapshot_cached 的二次判 TTL）。
        没有它的话，"打开监控页 + 前端立刻拉一次 /overview"就会把每个依赖连探两遍，
        而探测本身就是被监控对象的负载 —— 对一个已经掉线的 MySQL 连打两轮，
        正是这个页面最不该干的事。
        """
        import asyncio

        if self._monitor_lock is None:
            self._monitor_lock = asyncio.Lock()
        return self._monitor_lock

    def _apply_lock_for(self):
        """懒建单段热应用互斥锁

        配置页保存与后台自愈是同一个动作（drop 单例 → create → 装上 → 关旧
        实例），交错执行会出现「后完成的那个把对方刚装上的实例 close 掉，
        self.vector 指向一个已关闭的连接」，而 degraded 里没有记录 —— 界面
        显示在线、查询却报错，正是本项目一直在防的那类自相矛盾（TS-015）。
        锁不在 __init__ 里建：容器可能在无事件循环的上下文（脚本/测试）构造。
        """
        import asyncio

        if self._apply_lock is None:
            self._apply_lock = asyncio.Lock()
        return self._apply_lock

    def mark_section_down(self, section: str, label: str, reason: str) -> None:
        """外部探测（监控页每次刷新）发现某段运行期掉线 → 登记为降级

        为什么必须由使用方登记：容器只在**启动自检**与**配置保存**时探依赖，
        一个"启动时健康、之后掉线"的段不会留下任何痕迹 —— degraded 里没有它，
        _recovery_candidate() 的两个条件就都不成立（实例非空、且无降级记录），
        后台自愈会直接跳过它，于是那一行只能一直红着等重启。这份观察恰好只有
        监控页有（它每次刷新都带真实探测结论），故由它把结论写回容器。

        只登记 RECOVERABLE_SECTIONS 的段：其余段（storage/synonym/auth）没有
        自动重连链路，登记了就没人来清，会变成"修好了却还挂着降级"的新假故障。
        已有记录时直接返回：构造期/启动自检留下的原因更早也更具体（如"未配置，
        使用内置降级实现"），不能被后来的一句探测结论盖掉。
        """
        if section not in RECOVERABLE_SECTIONS or self.config.noconnection:
            return
        spec = SECTION_ADAPTERS.get(section)
        attr = spec[0] if spec else "redis"
        if getattr(self, attr, None) is None:
            return                     # 实例已被摘掉，启动自检的结论更准
        keys = SECTION_DEGRADED_KEYS.get(section, (section,))
        if any(k in self.degraded for k in keys):
            return
        # 文案与 initialize() 的降级记录同构（"XX不可用：原因…"），并点明这是
        # 运行期掉线、后台会自己重连 —— 否则用户会以为又得重启或重存配置
        self.degraded[keys[0]] = (
            f"{label}不可用：{reason}（运行期探测失败，后台自动重连中）")
        log.warning("section_marked_down", section=section, reason=reason[:200])

    def _recovery_candidate(self, section: str) -> bool:
        """这一段该不该重试：配置启用 + 当前确实处于失联状态

        「配置里已禁用」的段本来就没有实例（_try_create 直接返回 None），
        不重试，否则会一直白试；llm/embedding 的降级大多是"没填 base_url"，
        重试一万次也还是那句「未配置」，同样跳过。
        """
        cfg = getattr(self.config, section, None)
        if cfg is None or not bool(getattr(cfg, "enabled", True)):
            return False
        if section in ("llm", "embedding"):
            name = str(getattr(cfg, "adapter", "") or "")
            if name in _LOCAL_IMPL or \
                    not (getattr(cfg, "base_url", "") or "").strip():
                return False
        spec = SECTION_ADAPTERS.get(section)
        attr = spec[0] if spec else "redis"
        if getattr(self, attr, None) is None:
            return True
        keys = set(SECTION_DEGRADED_KEYS.get(section, (section,)))
        return bool(keys & set(self.degraded))

    async def _probe_section_candidate(self, section: str) -> tuple[bool, str]:
        """用当前配置现建一个**一次性**实例探一次，探通才值得装进容器

        为什么不直接 apply_section：它是"先装后检"，新实例装上之后还要等健康
        检查跑完（最长数秒）才可能被摘掉；这段窗口里 self.vector 非空，检索路
        会把它当成可用而去打一个连不上的 Milvus，用户查询直接报错。配置页保存
        只发生一次、可以接受；后台每 30 秒重试一次就成了常态。
        """
        spec = SECTION_ADAPTERS.get(section)
        if spec is None:               # redis 走 _apply_redis，它本身就是先探后装
            return True, ""
        _attr, atype, _optional = spec
        cfg = getattr(self.config, section)
        name = str(getattr(cfg, "adapter", "") or "")
        klass = AdapterRegistry.get_class(atype, name)
        if klass is None:
            return False, f"适配器未注册：{atype}/{name}"
        inst = None
        try:
            inst = klass(cfg)
            probe = getattr(inst, "health_probe", None)
            if probe is not None:
                ok, reason = await probe()          # 适配器自带预算（同配置页）
            else:
                import asyncio

                ok = bool(await asyncio.wait_for(
                    inst.health_check(), timeout=6))    # 同 initialize 兜底预算
                reason = ""
            return bool(ok), ("" if ok
                              else (reason or "健康检查未通过（适配器未给出原因）"))
        except Exception as e:
            return False, f"建连探测异常（{type(e).__name__}: {str(e)[:180]}）"
        finally:
            await _close_quietly(inst)

    async def recover_lost_sections(self) -> dict[str, str]:
        """运行期自愈：重试「配置启用但此刻失联」的段，修好的依赖自动回归

        由 rag.api.runtime 的后台循环周期性调用。成功即完成恢复：适配器装回
        容器、degraded 记录清空，检索路随 enabled_paths() 自动回来，无需重启。
        尽力而为 —— 单段失败只记日志（继续由 degraded 对外说明原因），绝不抛错
        把后台循环带走。
        """
        if self.closed or self.config.noconnection:
            return {}
        recovered: dict[str, str] = {}
        for section in RECOVERABLE_SECTIONS:
            if not self._recovery_candidate(section):
                continue
            stat = self.recovery_stats.setdefault(
                section, {"attempts": 0, "lastAt": 0.0})
            if time.time() - stat["lastAt"] < RECOVER_COOLDOWN_SEC:
                continue                      # 退避：见 RECOVER_COOLDOWN_SEC
            stat["attempts"] += 1
            stat["lastAt"] = time.time()
            try:
                ok, reason = await self._probe_section_candidate(section)
                if not ok:
                    log.info("section_recover_probe_failed", section=section,
                             attempts=stat["attempts"], reason=reason[:200])
                    continue
                await self.apply_section(section)
            except Exception as e:
                log.warning("section_recover_failed", section=section,
                            attempts=stat["attempts"],
                            error=f"{type(e).__name__}: {e}")
                continue
            spec = SECTION_ADAPTERS.get(section)
            attr = spec[0] if spec else "redis"
            if self._section_online(section):
                name = AdapterRegistry.name_of(
                    spec[1] if spec else "redis", getattr(self, attr, None))
                recovered[section] = name or section
                self.recovery_stats.pop(section, None)   # 已恢复，退避记录清零
                log.info("section_recovered", section=section, adapter=name,
                         attempts=stat["attempts"])
            else:
                log.info("section_recover_still_down", section=section,
                         attempts=stat["attempts"])
        return recovered

    async def apply_section(self, section: str, reconfigured: bool = False) -> None:
        """只热应用一个配置段：重建该段适配器，并单独自检。

        与 rebuild_container（整容器重建）的唯一区别就是"只管这一段"：
        其它服务的适配器、连接、健康检查、后台任务一律不碰 —— 保存 MySQL
        不会去重连 ES/Milvus/Redis，也不会因为它们此刻抖动就把对应功能关掉。
        前置条件：调用方已把新配置写进 self.config.<section>。

        reconfigured：这次是不是"用户刚保存了这一段的新配置"。同样的重建，
        两种来源对降级记录的处理不同（新配置下旧原因作废 / 后台重试保留最早
        那句），由调用方交代 —— 别让下层去猜，那正是状态机中间态的来源。
        """
        async with self._apply_lock_for():
            await self._apply_section_locked(section, reconfigured)

    async def _apply_section_locked(self, section: str,
                                    reconfigured: bool = False) -> None:
        if section == "redis":
            await self._apply_redis(reconfigured=reconfigured)
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
        if section in ("vector_store", "embedding"):
            # 空间指纹的结论建立在"向量库实例 + 向量模型"之上，这两段一换就
            # 全部作废（否则会拿着上一套模型的结论去拦/去放行），由随后的
            # _selfcheck_section → sync_vector_space 重算
            self._space_state.clear()
            self._space_logged.clear()
            self.degraded.pop("vector_space", None)
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

        if section == "meta":
            cfg = self.config.meta
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
                        from rag.adapters.meta_mysql import HEALTH_BUDGET_SEC
                        ok = await asyncio.wait_for(
                            self.meta.health_check(), timeout=HEALTH_BUDGET_SEC)
                except Exception:
                    ok = False
            if not ok:
                reason = reason or f"MySQL 不可达（{cfg.host}:{cfg.port}）"
                log.warning("meta_degraded_memory", host=cfg.host,
                            port=cfg.port, reason=reason[:200])
                self.degraded["meta"] = (
                    f"{reason}；元数据已降级为内存模式"
                    "（数据不持久化，请在配置页补齐连接信息）")
                import rag.adapters.meta_memory  # noqa: F401
                self.meta = AdapterRegistry.create("meta", "memory", None)
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
                # 换了向量库或换了 embedding（维度/模型可能都变了）→ 立刻重判
                # 向量空间：否则页面与检索路都还挂着上一套模型的结论
                await self.sync_vector_space("default", stamp=True)
            elif t_attr == "fulltext":
                await adapter.ensure_index("default")
            else:
                await adapter.ensure_schema()
        except Exception as e:
            log.warning("ensure_failed", section=section,
                        component=t_label, error=str(e))
            self.degraded[t_key] = f"{t_label}初始化失败（{e}），已关闭"
            setattr(self, t_attr, None)

    async def _apply_redis(self, reconfigured: bool = False) -> None:
        """Redis 段单段热应用：换客户端实例，并同步快照过它的服务

        reconfigured=True 表示 self.config.redis 刚被换成用户新保存的那份：
        此时降级文案必须**重写**，否则报的还是上一个地址（用户照着它去查一个
        已经不存在的配置）。后台重试则相反，配置没变，保留最早那句（见下）。
        """
        import asyncio

        if self.config.noconnection:
            return
        cfg = self.config.redis
        # 降级记录**探完才动**（成功才清）。先清后探会留出一段"记录没了、实例还
        # 挂着"的窗口：这段时间探到的仍是旧客户端，监控页刷一次就会重新登记，
        # 于是「降级」二字与它的说明在徽章 / 组件名之间来回跳 —— 而事实自始至终
        # 没变过。后台每 30 秒重试一次，这个窗口就每 30 秒出现一次（见 TS-019
        # 面孔 E：界面自相矛盾时先修状态机的中间态，别让界面去容忍它）。
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
            text = f"{reason}；会话/记忆已退化为进程内存"
            if reconfigured:
                # 配置刚被替换：旧文案里写着旧地址，留着就是误导。热应用只发生
                # 在用户点保存那一次，重写不会造成"每条 30 秒跳一次字"。
                self.degraded["redis"] = text
            else:
                # 后台重试：配置没变，启动自检 / 运行期探测写下的那句更早也更
                # 具体，每次重试都盖一遍会让同一条故障的文案在两种说法间跳字。
                self.degraded.setdefault("redis", text)
        else:
            self._clear_degraded("redis")
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
        # 置位后后台自愈不再往这个（即将被换掉的）容器里装新适配器：
        # AdapterRegistry 是进程级单例缓存，旧容器"复活"会去 drop 新容器刚建好
        # 的实例，留下一个谁都说不清归属的连接
        self.closed = True

    # ── 向量空间指纹（防"同维不同源"的向量混进同一个集合）──────
    # 判据是纯函数（rag/vector_space.py），这里只负责编排：读指纹 → 判定 →
    # 缓存结论 → 该拦的拦、该说的说。三条纪律：
    # · 写侧不一致 → **有意跳过**（不是让适配器抛错：那会被上层记成"写入失败"，
    #   文档状态变 PARTIAL、质量报告长期挂着一条假故障）；
    # · 读侧不一致 → 关掉向量路并给出可读原因（拿噪声去查，RRF 也淘汰不掉）；
    # · 拿不准（读不到指纹/行数、该类向量库没有载体）→ 放行 + 如实说明，
    #   绝不假装校验通过。

    def _embedding_is_local(self) -> bool:
        """当前向量模型是不是本地替身（mock / 未配置时的内置降级实现）"""
        if self.embedding is None:
            return False
        return AdapterRegistry.name_of("embedding", self.embedding) in _LOCAL_IMPL

    def _embedding_space(self) -> str | None:
        """当前向量模型的空间指纹；拿不到 → None（判据缺失一律放行，不误伤）"""
        emb = getattr(self, "embedding", None)
        if emb is None:
            return None
        ecfg = self.config.embedding
        dim = int(getattr(emb, "dim", 0)
                  or getattr(ecfg, "dim", 0) or 0)
        return space_tag(AdapterRegistry.name_of("embedding", emb) or "",
                         getattr(ecfg, "model", "") or "",
                         dim, is_local=self._embedding_is_local())

    def _note_space_once(self, key: str, msg: str) -> None:
        """同一条空间告警只记一次：入库是逐文档调用，重复刷只会淹没日志"""
        if key in self._space_logged:
            return
        self._space_logged.add(key)
        log.warning("vector_space_note", note=msg)

    async def sync_vector_space(self, collection: str = "default", *,
                                stamp: bool = False
                                ) -> tuple[bool, bool, str]:
        """读该 collection 的指纹 → 判「可写 / 可检索」并缓存结论

        结论同时反映到 degraded["vector_space"]（**只在真的拦了东西时**登记）：
        库本身是好的，坏的是"库里的向量与当前向量模型不同源"，挂到 vector_store
        名下会让监控页把两者混为一谈 —— 更要命的是那会触发后台每 30 秒重装一次
        向量库适配器（实例在线探针也过，永远修不好、永远在白试）。

        stamp=True：集合为空（或还没建）时顺手补打当前指纹 —— 只在**没有数据**
        可被误标时才写，绝不替既有数据认领来源。
        """
        vector = getattr(self, "vector", None)
        current = self._embedding_space()
        if vector is None or current is None:
            self._space_state[collection] = (True, True, "")
            return True, True, ""

        if not getattr(vector, "space_tag_supported", False):
            runtime = AdapterRegistry.name_of("vector_store", vector) or "?"
            extra = ("；本地替身产生的伪向量混进真实集合后无法检出，"
                     "演示时建议直接用 --noconnection 关掉向量库"
                     if self._embedding_is_local() else "")
            self._note_space_once(
                f"unsupported:{collection}",
                f"向量库实现 {runtime} 没有空间指纹载体：无法校验该集合里的向量与"
                f"当前向量模型（{tag_label(current)}）是否同源{extra}")
            self._space_state[collection] = (True, True, "")
            return True, True, ""

        try:
            stored = await vector.read_space_tag(collection)
            rows: int | None = None
            if stored != current:
                # 指纹一致时不必问行数（判据在此分支已经成立）：正常部署下
                # 这条路径不产生任何额外 RPC，检索前的同步也就近乎零成本
                rows = await vector.collection_rows(collection)
        except Exception as e:
            log.warning("vector_space_read_failed", collection=collection,
                        error=f"{type(e).__name__}: {e}"[:200])
            self._space_state[collection] = (True, True, "")
            return True, True, ""

        if stamp and stored != current and rows == 0:
            if await vector.write_space_tag(collection, current):
                log.info("vector_space_stamped", collection=collection,
                         space=current)
                stored = current
            else:
                self._note_space_once(
                    f"stamp_failed:{collection}",
                    f"集合 {collection} 还没有数据，但当前指纹写不进去："
                    "之后无法校验同源性，本地替身写入将按保守规则被拒")

        write_ok, read_ok, reason = judge_space(
            current, stored, rows, is_local=self._embedding_is_local())
        self._space_state[collection] = (write_ok, read_ok, reason)
        if collection == "default":
            self._publish_space(reason if not (write_ok and read_ok) else "")
        if write_ok and read_ok and reason:
            # 存量集合没有指纹、但按同源处理：行为没变，只是提示一次
            self._note_space_once(f"untagged:{collection}", reason)
        return write_ok, read_ok, reason

    def _publish_space(self, reason: str) -> None:
        """把默认集合的结论挂到 degraded（UI 的「降级记录」与状态灯用它）"""
        if reason:
            self.degraded["vector_space"] = reason
        else:
            self.degraded.pop("vector_space", None)

    def vector_space_ok(self, collection: str = "default") -> bool:
        """该 collection 的向量此刻可否用于检索（未判定过 → 放行）

        同步判据：结论由 sync_vector_space() 预先算好，这里只读缓存 ——
        enabled_paths() 每次问答都会被调到，不能在里面 await 向量库。
        """
        state = self._space_state.get(collection)
        return True if state is None else state[1]

    def vector_space_reason(self, collection: str = "default") -> str:
        """默认集合不可用/被限制时的可读原因（无则空串），供监控页说明"为什么" """
        state = self._space_state.get(collection)
        return "" if state is None else state[2]

    async def vector_read_ok(self, collection: str) -> bool:
        """读侧门禁：先同步指纹再给结论（用于按 collection 判定的检索路径）"""
        _, read_ok, _ = await self.sync_vector_space(collection)
        return read_ok

    async def vector_write_blocked(self, collection: str) -> str | None:
        """写侧门禁：返回非空字符串 = 本次应跳过写入，内容即可读原因

        为什么收口在调用方而不是适配器内部：适配器抛异常会被上层记成"写入
        失败"，文档状态变 PARTIAL、质量报告长期挂着一条假故障（同 embedding
        假降级那次）。"跳过"必须是**有意为之**且可区分的一件事。
        """
        if self.vector is None or self.embedding is None:
            return None
        try:
            # 先确保结构存在：新集合要在这里建出来才谈得上"打指纹"（否则
            # alter properties 打在一个还不存在的集合上，指纹永远打不上）
            await self.vector.ensure_collection(collection,
                                                self.embedding.dim)
        except Exception as e:
            log.warning("vector_space_ensure_failed", collection=collection,
                        error=f"{type(e).__name__}: {e}"[:200])
        write_ok, _, reason = await self.sync_vector_space(collection, stamp=True)
        if write_ok:
            return None
        return reason or "向量空间不一致：已跳过写入"

    def _vector_model_ready(self) -> bool:
        """向量模型此刻是否真的能出向量（向量路的第二个前置条件）

        向量检索 = 向量库 + 向量模型，任一不可用整条路都做不了：Milvus 连着、
        向量模型掉了，查询照样必失败。判据与监控页那个红点同源（同一份 degraded
        记录），所以不会再出现"页面一行红、检索路全绿"（TS-015 那类自相矛盾）。

        本地替身（mock，含"未配置 → 内置降级实现"）不算掉线：那是配置层面有意
        为之的运行模式，degraded 记录已写明；此时写入与检索由同一个替身完成，
        路径本身自洽，故仍算可用。
        """
        if self.embedding is None:
            return False
        if AdapterRegistry.name_of("embedding", self.embedding) in _LOCAL_IMPL:
            return True
        keys = set(SECTION_DEGRADED_KEYS.get("embedding", ("embedding",)))
        return not (keys & set(self.degraded))

    def enabled_paths(self) -> set[str]:
        """当前实际可用的检索路（配置启用 ∩ 该段真的在线）

        判据用 _section_online()（实例非空 **且** 本段没有降级记录），不是
        "实例非空"：可选依赖在启动自检失败时会被置 None（路自动关闭），但
        "启动时健康、运行期掉线"只会留下降级记录、实例仍在 —— 只看实例非空
        就等于向用户谎报一条已经做不了的检索路。

        向量路还要多一条：**库里的向量与当前向量模型同源**（vector_space_ok）。
        Milvus 连着、向量模型也能出向量，但集合装的是另一套空间的向量时，这条
        路查出来的东西是噪声 —— 照样算"不可用"（原因见 degraded["vector_space"]，
        监控页由 readiness.retrieval.lost 的 reason 说明）。
        """
        paths = set()
        if self._section_online("fulltext"):
            if self.config.retrieval.enable_kw_exact:
                paths.add("kw_exact")
            if self.config.retrieval.enable_bm25:
                paths.add("bm25")
        if (self._section_online("vector_store")
                and self.config.retrieval.enable_vector
                and self._vector_model_ready()
                and self.vector_space_ok()):
            paths.add("vector")
        if self._section_online("knowledge_graph") \
                and self.config.retrieval.enable_graph:
            paths.add("graph")
        if self._section_online("business_data") \
                and self.config.retrieval.enable_structured:
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
