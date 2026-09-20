# 故障排查记录（Trouble Shooting）

本文件记录开发过程中定位并修复的疑难问题。每条记录按统一四段式展开：

| 段落 | 内容 |
|---|---|
| **现象** | 用户/运行时可观测到的表现，含原始数据 |
| **定位过程** | 如何一步步缩小范围，用了什么手段、得到什么中间结论 |
| **修复方式** | 最终代码改动（文件、关键片段） |
| **根因分析** | 底层机制层面的原因，解释"为什么会这样" |

> 环境：Windows / Python 3.10 / FastAPI + 自研 RAG 框架
> 特殊测试前提：**所有外部依赖均未启动**（MySQL、Redis、Milvus、Elasticsearch、Neo4j、MinIO 全部不可达）。
> 本机防火墙对上述端口为**静默丢包**（SYN 无响应）而非主动拒绝，因此连接失败表现为「等满超时」而不是「秒失败」——
> 这正是放大所有超时问题的关键外部条件。
>
> TS-007 起记录**配置热更新与重排（Rerank）链路**的问题，环境同上；
> 其中 TS-007/TS-008 的定位均采用「真实浏览器载荷复现」手段（Playwright 拦截请求 + 原样重放）。
> TS-011 的环境例外：日志中引用了**真实可达的 MySQL 8.0.46**（`192.168.100.239:3306`），
> 用于验证"错误码语义化"与"表单值直连探测"两条链路的正反两面。
>
> TS-012/TS-013 的环境例外：验证全程使用**临时配置副本 + 独立实例**
> （保存动作会真的写配置文件，避免污染 `customer/customer_config.yaml`），依赖可达性同上述前提。
>
> TS-017 的环境例外：`customer/customer_config.yaml` 用的是 `milvus`，本机 Milvus **不可达**
> （同 TS-001 的前提），因此该条以「离线行为实测 + 模块导入自省」为主，未依赖真实 Milvus；
> 同段位的 `qdrant` / `pgvector` 属同一配置段下的备选实现（`adapter` 切换），一并审计。
>
> TS-018 属**配置面/文案面**问题：全程离线，验证手段是直接调用 `_service_params` /
> `_param_row` 观察载荷，不需要任何真实服务。
>
> TS-019 的环境说明：本机**无 Redis**（同 TS-001 前提），故"真实建连"用
> **关闭端口 + 非法参数**触发，其余为离线断言（工厂 `connection_kwargs` / 原因翻译 /
> 载荷构造）。附带一个影响面更大的实测发现：**Windows 套接字错误文本按系统语言
> 本地化**（本机原文为 `Error 22 connecting to 127.0.0.1:6399. 远程计算机拒绝网络连接。.`，
> 不含任何英文关键词）—— 这直接决定了"异常翻译"的写法，见 TS-019 定位过程第 4 条。
>
> TS-014 的环境例外：与 TS-011 同一台**真实可达的 MySQL**（`192.168.100.239:3306`），
> 且其服务端 `skip_name_resolve=OFF` —— 每个**新连接**都要付一次反向 DNS 解析超时（实测 10s）。
> 这是该条最重要的前提：慢的是握手而非网络不通，因此"等满超时"被误读成了"不可达"。
> 复核（2026-09-18）：直接查服务端 `SELECT @@skip_name_resolve` 仍为 **OFF**，
> 即"在服务端改过但未生效"（`SET GLOBAL` 只对之后的新连接生效；写 my.cnf 需重启）。
> ⇒ 因此**首次冷建连的 10s 属服务端行为，客户端无法消除**；
> 客户端能做的是"同一份配置只付一次"（见修复方式 ④）。
>
> TS-023 的环境说明：全程离线，验证用**临时配置副本 + 独立临时目录**（主密钥文件随目录走，
> 不污染 `customer/.secrets.key`）。收尾时对工作区的 `customer/customer_config.yaml` 执行了一次
> **就地加密** —— 该文件里当时躺着一条**真实的 DeepSeek api_key**（未提交的工作区改动，
> 见 `git diff`），正是本条要防的场景；加密后该值以 `enc:v1:` 落盘，内存中仍是明文。

## 索引

| 编号 | 问题 | 涉及组件 |
|---|---|---|
| TS-001 | 应用冷启动耗时 74 秒（用户感知两次约 30 秒卡顿） | 全局 |
| TS-002 | MinIO 存储适配器构造阶段阻塞约 30 秒 | storage / urllib3 |
| TS-003 | Neo4j 图适配器的 30 秒级默认超时隐患（默认未启用，未触发） | knowledge_graph / neo4j driver |
| TS-004 | MySQL 元数据健康检查拖 5 秒 | mysql_meta / aiomysql |
| TS-005 | 健康检查与适配器构造串行执行，导致超时叠加 | container |
| TS-006 | Redis 健康检查卡死 36.5 秒，且 `asyncio.wait_for` 超时失效 | container / redis-py 8.1.0 |
| TS-007 | 配置页切换重排设备（CPU→GPU）保存不生效，接口 200 但 YAML 仍为 cpu | 配置热更新 / admin config |
| TS-008 | Cross-Encoder 每次查询重复加载模型（缓存挂在请求级 step 实例上） | rerank / pipeline 生命周期 |
| TS-009 | 本地重排静默降级（依赖未安装），"测试连接"只校验路径给出假信号 | rerank / 依赖环境 |
| TS-010 | 重排模型三种权重文件的取舍（safetensors / pytorch_model.bin / onnx） | rerank / 模型加载 |
| TS-011 | 服务「测试连接」测的是已保存配置而非表单值，且失败原因被吞成一句"失败" | MySQL 元数据 / 配置页连接测试 |
| TS-012 | 保存任一模块都整容器重建（保存 MySQL 会重连 ES/Milvus/Redis，并弹出无关服务的降级） | 配置热更新 / container / AdapterRegistry |
| TS-013 | 单段热应用的三个次生缺陷：假在线 / 降级键双命名残留 / Redis 客户端被快照后未同步 | container / memory_service / progress_bus |
| TS-014 | 「测试连接」通过但保存后自检报 MySQL 不可达（两条链路的超时预算不一致：无预算 vs 6s） | MySQL 元数据 / 容器自检 / 配置页探测 |
| TS-015 | 全文检索四处隐蔽缺陷：适配器根本构造不出来 / 「测试连接」测不出缺 IK 插件 / 原因被吞成"连接失败" / 超时又是用户配置项 | fulltext / ES 适配器 / 容器自检 / 配置页探测 |
| TS-016 | 改了自己的 ES 地址却报旧地址的超时：客户端/服务端大版本不匹配被"地址不可达"的文案掩盖 | requirements / ES 适配器 / 配置页探测 / 适配器生命周期 |
| TS-017 | 向量库四处隐蔽缺陷：集合名不合法要等到建集合才炸 / 改 Milvus 地址后必须重启进程 / 「测试连接」测的是已保存的旧地址 / 探测蹭运行连接 | vector_store 适配器（milvus/qdrant/pgvector） / 配置页探测 / 连接生命周期 |
| TS-018 | 配置页三处"界面说了不算"：向量库用户名未标 optional（与成对规则相反） / milvus 露出对它无效的超时 / 业务数据 (SQL) 只关入口不关能力 | 配置页载荷 / 参数可见性与标记 / 产品裁剪 |
| TS-019 | 缓存 (Redis) 四处隐蔽缺陷：探测测的是已保存的旧连接 / 降级后保存死锁 / 失败原因被吞成"连接失败" / 会话 TTL 冒充连接参数 | redis_cache（新建） / container / 配置页探测 / 界面口径 |
| TS-020 | 启动日志里的 RequestsDependencyWarning：requests 自带的依赖版本闸门被"只写下界"的 chardet 撞破 | requirements / pymilvus / tiktoken（间接引入 requests） |
| TS-021 | 关机时 Milvus / Qdrant / ES 被静默跳过：`shutdown()` 与 `_close_quietly()` 用了两套关闭入口查找规则 | container / 各适配器生命周期 |
| TS-022 | 换过向量模型后检索静默失准：同维不同源的向量住进同一个集合（服务全绿、不报错、RRF 也淘汰不掉） | vector_store 适配器 / 向量空间指纹（新增） / container / 入库与检索链路 / 监控页 |
| TS-023 | 配置页「测试连接」拿掩码当密钥（假 401、模型列表空）+ 保存把凭据明文写回 / 把留空当清空 | 配置文件凭据（新增加密） / 配置页载荷与探测 / 适配器降级链路 |
| 附录 | 优化前后指标对比 / 验证脚本 / 经验总结 / 适配器命名体系（组件名 ↔ 槽位 ↔ 注册名） | — |

---

## TS-001 应用冷启动耗时 74 秒（用户感知两次约 30 秒卡顿）

### 现象

- 执行 `create_app_from_env()` + `TestClient(app)` 冷启动，全部外部依赖不可达时，**总耗时 74 秒**。
- 用户主观感受：启动过程中出现**两次明显约 30 秒的卡死**，期间无任何日志输出，界面/接口长时间无响应。
- 启动最终能成功（依赖自动降级为本地实现），因此不是"启动失败"，而是"启动极慢"。
- 后续定位确认：这两次卡死分别对应 **MinIO（storage）构造 ~30s** 与 **Redis 健康检查 ~36.5s**，
  两者串行且此前均无日志输出，与用户"两次约 30 秒卡顿"的体感完全对应。

### 定位过程

1. **先建立可复现的量化基线**。写 `scripts/smoke_startup_time.py`，把"导入 + 应用工厂"与"lifespan 启动"分别计时，
   并打印降级组件列表，避免只凭手感描述"慢"。结果：`total: 74s`，`degraded(5)`。
2. **二分定位到"构造阶段 vs 自检阶段"**。在容器里临时加分阶段计时（`perf_counter` 打点 + `log.info`），得到关键结论：
   - 卡顿**不在** SQL 查询、不在路由注册、不在 pipeline 装配；
   - 而是集中在**适配器构造**（`ServiceContainer.__init__`）与**健康检查**（`initialize()`）两处；
   - 且表现为"单点等满一个很长的超时"，而非大量小耗时累加。
3. **逐依赖隔离计时**。分别只构造/只探测单个适配器，量出各自的超时量级；
   （注：`knowledge_graph` / `business_data` 默认 `enabled: false`，未参与本次基线，见 TS-003）
   - MinIO（storage）≈ **30s**
   - Redis（redis）≈ **36.5s**（并行化之后被单独暴露出来，见 TS-006）
   - MySQL（mysql_meta）≈ 5s
   - Milvus（vector_store）≈ 3s
4. 结论：`30s（MinIO）+ 36.5s（Redis）+ 5s（MySQL）+ 3s（Milvus）≈ 74.5s`，与实测 74 秒吻合
   ⇒ 启动耗时几乎全部来自**串行叠加的库默认超时**，且两个 30 秒量级的等待（MinIO、Redis）
   正好对应用户感知的"两次卡顿"。

### 修复方式

按"**先限制单点最坏耗时，再消除串行叠加**"两步走，最终在 `rag/container.py` 落地：

```python
# rag/container.py（现状）
# 1) 慢构造适配器并行构建：总耗时 = 最慢单项，而非各项之和
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=5, thread_name_prefix="adapter") as ex:
    f_storage  = ex.submit(self._create_core, "storage", ...)
    f_vector   = ex.submit(self._try_create, "vector_store", ...)
    f_fulltext = ex.submit(self._try_create, "fulltext", ...)
    f_graph    = ex.submit(self._try_create, "knowledge_graph", ...)
    f_business = ex.submit(self._try_create, "business_data", ...)
    self.storage = f_storage.result()
    ...
```

```python
# 2) 健康检查全部并行：总耗时 = 最慢单项
meta_ok, vec_ok, ft_ok, kg_ok, biz_ok, redis_ok = await asyncio.gather(
    _check_meta(),
    _check_optional("vector"), _check_optional("fulltext"),
    _check_optional("graph"), _check_optional("business"),
    _check_redis(),
)
```

配合各依赖的专项超时收紧（见 TS-002 ~ TS-004、TS-006）。

### 根因分析

三层原因叠加，缺一不可：

1. **库默认超时是"给长连接服务端"设计的，对启动探测来说过长。**
   存储/图数据库客户端默认 30 秒级连接超时，是假设"服务迟早会起来"；而启动自检需要的是"快速判定可达性"。
2. **失败形态被防火墙放大。** 静默丢包（DROP）而非拒绝（REJECT）意味着内核不立刻回 RST，
   客户端只能等满自己的连接超时；若为 REJECT，每次尝试都是毫秒级失败，问题几乎不可见。
3. **串行执行让"最坏情况"变成"各项最坏之和"。** 6 个依赖各自最坏 3~30 秒，串行就是累加；
   而它们之间**没有任何数据依赖**，天然适合并行。

---

## TS-002 MinIO 存储适配器构造阶段阻塞约 30 秒

### 现象

- 单独构造 storage 适配器（`MinioAdapter.__init__`）耗时 **约 30 秒**。
- 期间日志出现 `core_adapter_degraded ... ConnectTimeoutError(... connect timeout=2)`，最终降级为 `local_fs`。

### 定位过程

1. 先确认阻塞点在 `Minio(...)` 构造还是 `bucket_exists()/make_bucket()` 调用；
   结论：**是构造后的探测调用**（`Minio()` 构造本身不发网络请求）。
2. 关键证据来自降级日志：单次连接超时明明只有 **2 秒**，却总共花了 30 秒
   ⇒ 说明**不是单次超时长，而是被重试了多次**。
3. 读 minio-py 底层实现，确认它用 `urllib3.PoolManager`，而 `urllib3.Retry` 的默认值是 `total=3`，
   并带指数退避 —— `2s 连接超时 × 多次尝试 + 退避 sleep` 正好落在 30 秒量级。

### 修复方式

`rag/adapters/storage.py` —— 传入自定义 `PoolManager`，把单次超时收紧且**彻底关闭重试**：

```python
# rag/adapters/storage.py
self._client = Minio(
    config.endpoint,
    access_key=config.access_key,
    secret_key=config.secret_key,
    secure=config.secure,
    http_client=urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=2, read=10),
        retries=urllib3.Retry(total=0),   # 构造期探测不重试：失败即降级
    ),
)
```

### 根因分析

- **HTTP 客户端库的"重试 + 指数退避"默认值是为提升请求成功率设计的，但在"启动期可达性探测"场景下是纯损耗**：
  探测的目的就是尽快拿到"不可用"结论并降级，重试只会把 2 秒的失败放大成 30 秒。
- 这类默认值具有**隐蔽性**：代码里只写了 `connect=2`，读代码的人很难意识到还有一层 `total=3` 在乘数级放大耗时。
- 判据：**凡是"一次性探测/构造"路径，重试次数都应为 0 或 1**；重试只应保留在真正的业务请求路径上。

---

## TS-003 Neo4j 图适配器的 30 秒级默认超时隐患（默认未启用，未触发）

### 现象

- **本条不是本次实测中出现的卡顿，而是代码审查发现的同类隐患**：
  图数据库适配器默认未启用（`customer/customer_config.yaml` 中 `knowledge_graph.enabled: false`，
  `KnowledgeGraphConfig.enabled` 默认亦为 `False`），因此本次 74 秒基线中**不含 Neo4j 的耗时**
  （运行时日志无 neo4j 相关告警，`graph` 也未进入降级列表）。
- 风险在于：一旦用户在配置页启用图谱检索，其构造/探测路径会以 **30 秒量级**阻塞启动，与 TS-002 完全同类。
  ⇒ 属于"同类问题一次修干净"的预防性修复，而非事后补丁。

### 定位过程

1. TS-001 收敛到"库默认超时过长"这一根因后，**反向审计所有适配器的默认超时**，
   逐一核对驱动默认值，而不是只修"本次恰好被触发"的那几个。
2. 审计 neo4j driver，得到两个危险默认值：
   - `connection_timeout` 默认 **30s**（TCP 建连超时）
   - `connection_acquisition_timeout` 默认 **60s**（从连接池取连接的超时）
3. 进一步发现 `GraphDatabase.driver(...)` 是**惰性**的（构造时并不建立连接），
   意味着超时不会在构造期暴露，而是**推迟到第一次业务调用/健康检查**才触发
   —— 即"启动不卡、首个请求卡"，比构造期卡顿更难排查。
4. 因此必须在构造时就把连接类超时收紧，并对探测再套一层外层超时兜底（此处的兜底必要性由 TS-006 反证）。

### 修复方式

`rag/adapters/knowledge_graph.py` —— 显式收紧连接超时，并给探测再套一层外层超时兜底：

```python
# rag/adapters/knowledge_graph.py
self.config = config
# 连接超时收紧：driver 默认 30s，服务不可达时阻塞启动
self._driver = GraphDatabase.driver(
    config.uri, auth=(config.user, config.password),
    connection_timeout=2, connection_acquisition_timeout=3)
```

```python
# 健康检查：内层靠 driver 自身超时，外层再兜一层，防止实现细节导致的无界等待
async def health_check(self) -> bool:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(self._driver.verify_connectivity), timeout=4)
        return True
    except Exception:
        return False
```

### 根因分析

- **惰性连接的驱动会把"连接成本"从一个位置挪到另一个位置**：构造变便宜了，
  但首次真实交互会突然付出全量超时代价。排查时若只看构造耗时，会得出"这个适配器很快"的错误结论。
- 因此对惰性连接型驱动，**必须显式设置连接类超时**，否则超时会以"首次调用抖动"的形式在运行时出现。
- `asyncio.to_thread` + `asyncio.wait_for` 是标准兜底姿势：
  即使库内部超时机制失灵，外层也已封顶（此处的必要性由 TS-006 反证）。

---

## TS-004 MySQL 元数据健康检查拖 5 秒

### 现象

- 健康检查阶段，`mysql_meta` 单项耗时 **约 5 秒**，随后降级为内存模式（`meta_degraded_memory`）。
- 单看 5 秒不算"卡顿"，但在串行检查链里它直接贡献了 5 秒启动延迟，
  且优化到后期它**成为最长的一项**（约 3s，见附录）。

### 定位过程

1. 用 `asyncio.wait_for(self.meta.health_check(), timeout=6)` 包住检查后观察实际耗时，
   确认耗时来自 `create_async_engine` 建立连接（`pool_pre_ping` 触发实际建连），而非 DDL/SQL 慢。
2. 确认驱动为 `mysql+aiomysql`，连接建立超时由 `connect_timeout` 控制，
   而代码里**没有显式设置**，走的是驱动默认值（量级即实测的 5 秒）。

### 修复方式

`rag/adapters/mysql_meta.py` —— 建引擎时显式指定 `connect_timeout`
（该值现为模块常量 `CONNECT_TIMEOUT_SEC`，不作为用户配置项）：

```python
# rag/adapters/mysql_meta.py
self._engine = create_async_engine(
    dsn, pool_size=POOL_SIZE, pool_pre_ping=True,
    connect_args={"connect_timeout": CONNECT_TIMEOUT_SEC})
```

### 根因分析

- 与 TS-002/TS-003 同源：**依赖库默认超时对"启动探测"而言偏长**。
- 更本质的一点：本机防火墙对 3306 同样是静默丢包，所以 `connect_timeout` 是该阶段的**硬下限**，
  设多少就至少要等多少。该项无法靠"并行"或"少重试"压到毫秒级，
  只能靠"可控的短超时 + 并行掩盖"，这也是优化后仍保留 3 秒量级的原因。
- 取舍依据：`connect_timeout` 太小会在正常环境下误判（网络抖动即降级），
  3 秒是"错误降级率"与"启动延迟"之间的折中点。

---

## TS-005 健康检查与适配器构造串行执行，导致超时叠加

### 现象

- 启动耗时 = 各依赖超时的**算术和**（30 + 30 + 5 + 3 + …），而不是"最慢的那一项"。
- 表现为：每新增/启用一个不可达依赖，启动时间就再涨一截，总时长随依赖数量**线性增长**。

### 定位过程

1. 分阶段计时显示：适配器构造阶段内部是**逐行顺序**的 `_create_core/_try_create` 调用；
   健康检查阶段同理，是 `await` 一个再 `await` 下一个。
2. 审查这些调用之间的依赖关系：
   - `storage / vector / fulltext / graph / business` 五者**互不依赖**，都只读 `config`；
   - `meta / vector / fulltext / graph / business / redis` 六项检查同理，每项都是"独立探测 + 独立降级"；
   - **没有执行顺序要求**。
3. ⇒ 已具备并行化的充分条件（无共享可变状态、无先后约束），并行后总耗时从"求和"降为"取最大"。

### 修复方式

见 TS-001 代码片段：

- **构造阶段**：`ThreadPoolExecutor(max_workers=5)` 并行提交五个"网络连接型"适配器，`future.result()` 回收。
  （选线程池而非 asyncio，因为 `__init__` 是同步方法，且 minio / neo4j / milvus 的构造是同步阻塞 API。）
- **健康检查阶段**：`asyncio.gather(...)` 并行六项探测。
  （选 asyncio 而非线程池，因为检查本身已是 `async` 接口，且逐项都包了 `asyncio.wait_for` 上限。）

### 根因分析

- **"最坏情况"的量纲问题**：串行时最坏耗时是各依赖超时的**和**，并行之后变为**最大值**。
  依赖越多，差距越显著 —— 这是启动优化里收益最高、风险最低的一类结构性改动。
- 并行化的前提是**幂等与隔离**：各适配器只读取 `config`、各自写自己的字段，因此不存在竞态；
  `self.business.set_llm(self.llm)` 这类跨适配器装配被刻意**放在并行块之外**，
  保证线程池退出后再做串联。
- 反例警示：若不加区分地把有依赖关系的初始化也塞进线程池，会得到难以复现的偶发空指针/顺序错误。

---

## TS-006 Redis 健康检查卡死 36.5 秒，且 `asyncio.wait_for` 超时失效

### 现象

- 并行化改造完成后，其余各项均已降到秒级，但 `initialize()` 依然耗时 **36.78 秒**，
  单项计时显示 **`redis` 独占 36.66 秒**，成为唯一且绝对的主要瓶颈。
- 看似已有超时保护，却完全不起作用：

```python
# 明明写了 wait_for(timeout=6)，实际却等了 36.66 秒
r = await asyncio.wait_for(self.redis.ping(), timeout=6)
```

- 独立复现（`scripts/smoke_redis_iso.py`，临时脚本，已删除）：

```
redis-py version: 8.1.0
wait_for ping: 36.49s -> TimeoutError: Timeout connecting to server
bare ping:     37.63s -> TimeoutError
```

  ⇒ 套了 6 秒超时也只快 1 秒。**"超时保护形同虚设"** 是本问题最反直觉之处。

### 定位过程

1. **先证伪"是不是网络慢"**：裸调用 `ping()` 同样 37.63 秒 ⇒ 与 `wait_for` 无关，是 `ping` 自身耗时长。
2. **证伪"是不是我方配置太松"**：客户端已显式设置 `socket_connect_timeout=3, socket_timeout=5`，
   单次尝试上限只有 3 秒，却花了 36.5 秒 ⇒ 必然发生了**多次尝试**。
   反推：36.5s ÷ 3s ≈ 12 次，量级上指向"默认重试 10 次"。
3. **读 redis-py 8.1.0 源码求证**，三处证据链闭合：

   ```text
   redis/asyncio/client.py:286-291          # 客户端默认参数就是一个 Retry 对象
       retry: Retry = Retry(
           backoff=ExponentialWithJitterBackoff(
               base=DEFAULT_RETRY_BASE, cap=DEFAULT_RETRY_CAP),
           retries=DEFAULT_RETRY_COUNT,
       ),

   redis/_defaults.py:37-39
       DEFAULT_RETRY_COUNT = 10
       DEFAULT_RETRY_BASE  = 0.01   # 10ms
       DEFAULT_RETRY_CAP   = 1      # 1s

   redis/asyncio/connection.py:819-827      # 建连走重试循环
       await self.retry.call_with_retry(
           lambda: self.connect_check_health(check_health=True, retry_socket_connect=False),
           lambda error, failure_count: self.disconnect(...),
           with_failure_count=True)
   ```

   ⇒ 默认 **11 次尝试**：`11 × 3s（连接超时）+ 退避 sleep ≈ 36.5s`，与实测完全吻合。

4. **解释"为什么 `wait_for` 也拦不住"**：`wait_for` 到点后靠 `task.cancel()` 中断内部协程，
   而 redis-py 的重试循环 `call_with_retry` 对可重试异常做捕获后 **`continue` 重试**；
   取消信号在重试框架内被吞掉/转化为一次可重试失败，导致外层超时被"绕过"。
   佐证：最终抛出的是 redis 自己的 `TimeoutError: Timeout connecting to server`（重试全部耗尽后的产物），
   **而不是** `asyncio` 的 `TimeoutError`（`wait_for` 的产物）
   —— 说明 `wait_for` 的 6 秒计时器实际从未生效，协程是"自己跑完"的。
5. 附带确认失败形态：socket 层错误被归一化为 `TimeoutError` 而非 `ConnectionRefusedError`
   ⇒ 本机防火墙对 6379 为**静默丢包**，每次尝试都要等满 3 秒，把重试代价放大到 30 秒以上。

### 修复方式

`rag/container.py` —— 创建客户端时**显式禁用默认重试**（单次尝试、快速失败），
把"是否可用"的判定权与降级权交还给容器：

```python
# rag/container.py
import redis.asyncio as aioredis
from redis.backoff import NoBackoff
from redis.retry import Retry
# 显式禁用 redis-py 8.x 默认的 10 次指数重试：
# 服务不可达时默认重试会拖 ~36s（且会吞掉 wait_for 的取消），
# 这里改为单次尝试快速失败，由容器降级逻辑接管
self.redis = aioredis.Redis(
    host=config.redis.host, port=config.redis.port,
    password=config.redis.password or None,
    db=config.redis.db, decode_responses=True,
    socket_connect_timeout=3, socket_timeout=5,
    retry=Retry(NoBackoff(), 0))
```

效果：`initialize()` 从 **36.78s → 3.13s**（redis 项从 36.66s → 约 3s，
剩余 3 秒即 `socket_connect_timeout=3` 的单次硬下限）。

### 根因分析

1. **第三方库的"默认重试"是启动路径上的隐形乘数。**
   `socket_connect_timeout=3` 只约束"单次"，总耗时 = 单次 × 重试次数 + 退避；
   只读我方参数会严重低估最坏耗时。
2. **框架级超时（`asyncio.wait_for`）不保证能中断"会吞取消的第三方重试循环"。**
   这是本次最深的教训：`wait_for` 的作用是**取消任务**，而"取消能否生效"取决于被调协程是否让取消穿透。
   当被调方内部是 `except <可重试异常>: continue` 结构的重试框架时，取消可能被吸收。
   ⇒ **不能把 `wait_for` 当作万能兜底**；真正的保障是让被调用方自身耗时**有界**（次数有限 + 单次超时有限）。
   判断信号很明确：**看最终抛出的异常类型** —— 若抛出的是库自己的 TimeoutError 而非 `asyncio.TimeoutError`，
   说明外层 `wait_for` 计时器没有生效。
3. **降级设计使"重试"在此处毫无价值**：Redis 不可用时系统的既定行为就是"降级为进程内存 + 在配置页提示用户"，
   重试 10 次不会改变结论，只会推迟降级、拖慢启动。
   ⇒ 判据：**"失败后必有确定性降级"的路径，不应重试。**

---

## TS-007 配置页切换重排设备（CPU→GPU）保存不生效

### 现象

- 配置页「模型」tab 的重排卡片里把设备从 CPU 切到 GPU 后保存：
  **接口返回 200、`updatedKeys` 含 `retrieval`、热应用（容器重建）也成功**，
  但 `customer/customer_config.yaml` 的 `rerank_device` 始终是 `cpu`，刷新后回显仍是 CPU。
- 最反直觉之处：**同一接口、同一内容，换个方式发结果就不同**——
  - 最小载荷 `{"retrieval": {"rerankDevice": "cuda"}}` 重放 → **能**写入 cuda；
  - 浏览器实际提交的完整载荷重放 → **不能**，仍是 cpu。
  ⇒ 问题不在"字段没传"，而在"完整载荷里的其他字段把新值冲掉了"。

### 定位过程

1. **先拿一手证据**：用 Playwright 打开配置页、切换 GPU 并保存，
   拦截 `POST /api/admin/config` 请求体落盘（约 8.8KB），确认 `retrieval.rerankDevice: cuda` **确实在载荷里**。
2. **排除"写入链路坏了"**：该请求体原样重放（httpx + 管理员 token）仍得
   `200 / updatedKeys 含 retrieval / 热重建成功`，但文件依旧 `cpu`
   ⇒ 链路是通的，问题在**内容被覆盖**。
3. **二分载荷定位字段**：逐步裁剪后重放，锁定 `specialGroups` 中 `key = "retrieval"` 的成员——
   只要包含它，新值必被冲掉。
4. **读后端归一化函数**（`rag/web/routes.py:_yaml_update_from_payload`）发现顺序缺陷：
   前段用专用字段**正确**构建 `update["retrieval"] = {"rerank_device": "cuda", ...}`，
   但末尾的 `specialGroups` 循环执行 `update[key] = group["paramsJson"]`，
   把这个键**整体覆盖**为页面初始化时 dump 的旧配置（`rerank_device: cpu`）。
5. **回溯源头的两份数据源**：`rag/web/static/js/config-data.js:buildPayload()` 同时提交
   - `retrieval`：radio/输入框等**专用控件**的值（新值 cuda）
   - `specialGroups`（含 retrieval 成员）：`paramsJson` 来自页面加载时的 YAML 快照（旧值 cpu）

   两者天然不同步（切换 radio 不回写 JSON 编辑框）⇒ 典型的**陈旧覆盖新鲜**。
6. **确认不能简单删掉该分组**：retrieval 其余 27 个参数（`bm25_top_k`、`final_top_n`、`rrf_k` 等）
   在页面上**只有 JSON 编辑框一个入口**，删掉即失去编辑能力。

### 修复方式

`rag/web/routes.py` —— 调整合并顺序，让专用字段以 **deep-merge 覆盖**通用 JSON：

```python
# rag/web/routes.py（现状）
    # 特殊配置域 JSON 合并（先处理；retrieval 的专用字段随后覆盖，
    # 避免页面初始化时的旧 JSON 整体冲掉专用控件的新值）
    for group in (payload.get("specialGroups") or []):
        key = group.get("key")
        if key in _SPECIAL_KEYS and isinstance(group.get("paramsJson"), dict):
            update[key] = group["paramsJson"]

    # 检索策略（专用字段优先于 specialGroups JSON 中的同名键）
    ret: dict = {}
    ...
    if ret:
        update["retrieval"] = _deep_merge(update.get("retrieval") or {}, ret)
```

效果：专用控件的新值**永远优先**（所见即所得）；JSON 编辑框里未被专用控件管理的 27 个参数**照常保存**。

验证：原样重放完整载荷 → `rerank_device: cuda` 落盘且 `bm25_top_k: 10` 保留；
Playwright 端到端复测 **17 项断言全部通过**（布局 / 默认 CPU / chips 点选 / GPU 切换保存 /
YAML 持久化 / 刷新回显 / 无 JS 错误）。

### 根因分析

1. **同一实体存在两个可写数据源时，必须显式定义优先级。** 此处 retrieval 同时被
   "专用控件"与"通用 JSON 编辑器"写入，后端采用"谁最后处理谁生效"的隐式规则，
   而 `specialGroups` 循环恰好最后 ⇒ 意外地把**快照数据**抬到最高优先级。
2. **通用 JSON 编辑器天然持有陈旧快照。** 其内容是"页面加载时的 YAML 副本"，
   专用控件代表"用户当前意图"；两者不同步时，覆盖方向决定是 bug 还是正确。
   ⇒ 判据：**结构化专用字段优先级应高于自由 JSON，且必须由后端兜底**（不能只靠前端自律）。
3. **"接口成功"不等于"配置生效"。** 本例 `updatedKeys` 确实含 `retrieval`（specialGroups 写入了它）、
   热重建也成功，接口层面一切正常 ⇒ **返回码与 `updatedKeys` 都无法证明"写入的是期望值"**，
   此类问题必须落到**文件内容 diff**。
4. 修复策略取舍：选"先通用后专用 + deep-merge"而非"删除 specialGroup"——
   后者会连带砍掉只有 JSON 入口的 27 个参数，属于用功能缺失换正确性。

---

## TS-008 Cross-Encoder 每次查询重复加载模型

### 现象

- 本地重排（BGE-Reranker Cross-Encoder）每次查询都重新构造模型：
  CPU 环境首次响应多出数秒到数十秒；GPU 环境反复加载权重、显存反复占用。
- 代码里**明明有缓存**（`self._ce_model` / `self._ce_key` 比对），但**从未命中**。

### 定位过程

1. 读 `rag/pipeline/steps/query_retrieve.py:RerankStep._cross_encoder_rerank`：
   缓存写在 `getattr(self, "_ce_model", None)` —— 挂在**实例属性**上。
2. 反查 step 实例生命周期（关键一步）：
   - `rag/api/routes/chat.py:62` 每次聊天请求都调用 `container.workflows.query_pipeline()`；
   - `rag/pipeline/engine.py:123` → `Pipeline("query", StepRegistry.build(self._query_workflow))`；
   - `rag/pipeline/base.py:64` → `steps.append(cls._steps[n]())`，**每次新建步骤实例**。
3. ⇒ 实例属性缓存随实例一同丢弃，**命中率恒为 0**，"缓存"等价于没有。
   次要缺陷同时暴露：`CrossEncoder(...)` 是同步构造，直接在事件循环里执行会**阻塞事件循环**。

### 修复方式

`rag/pipeline/steps/query_retrieve.py` —— 缓存提升为**进程级**（LRU + 锁 + 单飞），
且"加载 + 推理"整体放入工作线程：

```python
class _CrossEncoderCache:
    """进程级 CrossEncoder 缓存：跨 step 实例复用，LRU + 单飞加载"""
    def __init__(self, capacity: int = 2) -> None:
        self._capacity = max(1, int(capacity))
        self._cache: "OrderedDict[tuple[str, str], object]" = OrderedDict()
        self._lock = threading.Lock()

    def get_or_load(self, model_name: str, device: str):
        key = (model_name, device)
        with self._lock:                      # 锁内加载 = 单飞，避免并发重复加载
            model = self._cache.get(key)
            if model is not None:
                self._cache.move_to_end(key)  # LRU：命中即置新
                return model
            from sentence_transformers import CrossEncoder
            model = CrossEncoder(model_name, device=device)
            self._cache[key] = model
            while len(self._cache) > self._capacity:
                self._cache.popitem(last=False)
            return model

_CE_CACHE = _CrossEncoderCache(capacity=2)
```

```python
            # 进程级缓存复用模型：加载与推理都在工作线程完成，
            # 既不阻塞事件循环，也避免并发查询重复加载（single-flight）
            def _score() -> list:
                model = _CE_CACHE.get_or_load(model_name, device)
                return model.predict(pairs)
            scores = await _aio.to_thread(_score)
```

验证（注入假 CrossEncoder，不依赖真实模型与 torch）：

| 用例 | 结果 |
|---|---|
| 同 key 复用同一实例 | loads=1 |
| LRU 容量 2，插入第三个 key 淘汰最久未用 | 剩 `b,c` |
| 8 线程并发同 key（单飞） | loads=1、实例唯一 |
| **两个不同 RerankStep 实例（模拟每请求新建）** | **loads=1** |
| 分数写入 `metadata["ce_score"]` | 0.7 |

### 根因分析

1. **缓存作用域必须与被缓存资源的生命周期匹配。** `CrossEncoder` 是重资源（权重数百 MB~数 GB），
   需**跨请求存活**；而 `RerankStep` 是请求级短命对象。缓存挂在短命对象上等价于"随用随弃"。
   ⇒ 判据：**凡创建成本 ≫ 使用成本的资源，缓存必须放在比调用者更长寿的层级**（进程级/容器级）。
2. **key 设计要覆盖"配置热切换"这一维。** 以 `(model_name, device)` 为键，
   配置页改模型/设备后下一查询自然换用新对象，旧对象由 LRU 释放 —— 无需重启，也无需显式失效逻辑。
3. **锁的意义是"单飞"而非仅仅互斥。** 首次加载最耗时，若不加锁，
   N 个并发查询会触发 N 次加载（显存直接爆掉）；锁内加载保证"只加载一次、其余等待复用"。
4. **同步重活不能留在事件循环里。** 加载与 `predict` 都是 CPU/GPU 密集型同步调用，
   放入 `asyncio.to_thread` 后才不至于阻塞整个服务的其他请求。

---

## TS-009 本地重排静默降级（依赖未安装）与"测试连接"的假信号

### 现象

- 配置页重排卡片的「测试连接」显示 **"本地模型路径有效"**，chips 正常列出本地权重，保存也成功，
  但实际查询**不会**走本地 Cross-Encoder —— 一切看似正常，功能实际未启用。
- 运行环境实测：

```text
python: D:\Python\Python310\python.exe
sentence_transformers IMPORT FAIL: ModuleNotFoundError
torch                IMPORT FAIL: ModuleNotFoundError
transformers         IMPORT FAIL: ModuleNotFoundError
```

- `requirements.txt:32` 已声明 `sentence-transformers>=3.0   # Cross-Encoder 精排（BGE-Reranker，含 torch）`，
  但该环境**未安装**。

### 定位过程

1. 以 `importlib.metadata.version`（查分发元数据）与直接 `import`（查真实可导入性）**双重确认**
   三个库均缺失，排除"装了但元数据异常"。
2. 读重排链路 `RerankStep.execute` 的异常处理：CE 加载/推理失败 → `except` 记 `cross_encoder_failed`
   → 降级 `_llm_rerank`（LLM listwise 重排）→ 若 LLM 亦不可用才保 RRF 顺序。
   ⇒ **本地 CE 失败是"静默降级"**：调用方仍拿到正常结果，能力缺失只体现为一行日志。
3. 读测试连接实现（`rag/web/routes.py`，`kind == "rerank"` 分支）：
   只做 `models/` 目录枚举 + `Path(model).exists()` 路径校验，**全程不 import、不加载、不推理**：

```python
   if kind == "rerank":
       # 本地 Cross-Encoder：扫描工程 models/ 目录下的预置权重
       names = sorted(d.name for d in root.iterdir() ...)
       models = [f"models/{n}" for n in names]
       ...
       online = p.is_dir()
       message = "本地模型路径有效" if online else "本地路径不存在"
```

4. ⇒ **"路径存在"被当成了"可用"**，形成"配置全绿但运行降级"的隐蔽状态，
   且对用户没有任何可观测提示（页面无警告、接口无标记）。

### 修复方式

本条为**环境处置 + 改进建议**（未在本环境执行安装）：

1. 启用本地重排（必做）：

```bash
   pip install sentence-transformers   # 自动带 transformers/torch；GPU 需先按 CUDA 版本装 torch
```

   安装后按 TS-008 的缓存机制：首次查询加载 `model.safetensors`，之后常驻进程。
2. 消除假信号（建议）：
   - 重排「测试连接」在路径校验之外，追加**真实可加载性探测**（至少覆盖
     `import sentence_transformers` 与目标目录必备文件检查）；
   - 或在查询链路的降级事件（`cross_encoder_failed`）上做可观测性：日志告警 + 前端提示。
3. 回归判据（确认真的在用本地 CE）：日志出现首次加载记录、
   重排结果 metadata 出现 `ce_score`、响应延迟符合本机预期。

### 根因分析

1. **"资源存在"与"资源可用"是两个命题。** 文件系统层面的检查（`exists()`）无法回答
   "依赖是否齐全、权重能否解析、设备是否可用"。把前者当作后者，就会产生"绿灯下的故障"。
2. **多级降级设计的固有代价。** 为使主干链路始终可用，降级路径刻意静默
   （`RerankStep.stop_on_error = False`，异常仅记日志），这提升了可用性，
   却把"能力缺失"隐藏起来。⇒ **降级必须配套可观测性**，否则等于"悄悄降档"。
3. **依赖声明 ≠ 依赖安装。** `requirements.txt` 是意图声明，运行环境才是事实；
   对重量级可选依赖（torch 等）尤其容易长期处于"声明了但没装"的状态。
   ⇒ 排查时**先验证真实可导入性，再看依赖清单**。

---

## TS-010 重排模型三种权重文件的取舍（safetensors / pytorch_model.bin / onnx）

### 现象

- 提问：模型目录下同时存在 `pytorch_model.bin`、`model.safetensors`、`model.onnx` 三种文件，
  当前代码实际用哪种？另两种是否用不到？
- 目录实况（**两个目录构成不同，需分别核对**）：

```text
models/bge-reranker-base/     model.safetensors 1060.7MB
                              pytorch_model.bin 1060.7MB
                              onnx/model.onnx   1060.9MB
models/bge-reranker-v2-m3/    model.safetensors 2165.9MB   ← 只有这一种
```

### 定位过程

1. **审计业务代码是否指定格式**：在 `rag/` 全目录搜索 `onnx` / `backend=` /
   `model.safetensors` / `pytorch_model.bin` —— **0 命中**。
   ⇒ 业务代码只给出"模型名/目录"，权重格式完全由加载库决定：

```python
   model = CrossEncoder(model_name, device=device)
```

2. **确定加载库的选择规则**：`sentence_transformers` → `transformers.from_pretrained`，
   默认 `use_safetensors=None` 的语义是"**目录中存在 safetensors 就优先加载它**"，
   不存在时才回退 `pytorch_model.bin`。
3. **确认 onnx 分支未被激活**：CrossEncoder 默认使用 **torch 后端**；
   走 onnx 必须显式传 `backend="onnx"`（并安装 onnxruntime/optimum），代码中并无此参数；
   且 onnx 文件位于**子目录** `onnx/`，也不在自动发现路径上。
4. ⇒ `bge-reranker-v2-m3` 只可能是 safetensors；`bge-reranker-base` 在有 safetensors 的前提下
   同样走 safetensors，另两种闲置。

### 结论

| 文件 | 是否被使用 | 说明 |
|---|---|---|
| `model.safetensors` | ✅ **实际使用** | transformers 首选格式（存在即优先） |
| `pytorch_model.bin` | ❌ 闲置 | 被 safetensors 遮蔽；仅当删除 safetensors 或显式 `use_safetensors=False` 时才用 |
| `onnx/model.onnx` | ❌ 闲置 | 需显式 `backend="onnx"`（+ onnxruntime/optimum）；且位于子目录，不会被自动发现 |

处置建议：确认不使用 onnx 与 bin 回退时，可删除 `bge-reranker-base` 下的 `pytorch_model.bin`
与 `onnx/`，回收约 **2.1GB** 磁盘；**删除前先用一次真实重排验证 safetensors 可正常加载**。

（注：本条与 TS-009 叠加——当前环境连 `sentence_transformers` 都未安装，
三种权重实际上**一个都没被加载**，本地重排在静默降级。）

### 根因分析

1. **权重格式的选择权在"加载库的默认优先级"，不在业务代码。** 只要业务不显式指定
   `use_safetensors` / `backend`，就是库的默认规则在生效
   ⇒ 排查此类问题要读库的行为，而非读业务代码。
2. **多格式并存是为跨框架兼容与回退设计的**（torch 系 safetensors/bin + onnx 推理栈），
   但在固定调用栈（torch CrossEncoder）下，冗余格式退化为**纯磁盘占用**。
3. **同名不同物易误判**：`model.safetensors`（根目录）与 `onnx/model.onnx`（子目录）语义不同，
   且 `bge-reranker-base` 与 `bge-reranker-v2-m3` 的目录构成也不一致
   ⇒ 结论必须**按目录分别核对**，不能一概而论。

---

## TS-011 服务「测试连接」测的是已保存配置而非表单值，且失败原因被吞掉

### 现象

- 在配置页「服务」tab 填好 MySQL（`host=192.168.100.239`、`port=3306`、`user=rag`、
  **空密码**、`database=rag_meta`），点「测试连接」后**只有一句 `✗ 失败`**，
  既无错误码也无原因，无从下手。
- 服务端侧看起来一切正常，用户自证：

```text
netstat -an | grep 3306   →   tcp  0  0 0.0.0.0:3306  0.0.0.0:*  LISTEN
ip addr                   →   inet 192.168.100.239/24  (ens18)
```

- 用户随之提出三个具体疑问：
  1. 是否需要**手工先建**默认库 `rag_meta`？
  2. 字符集用默认的 `utf8mb4` 有没有问题？
  3. 到底为什么连不上？
- 后经直连探针确认，真实原因是服务端**授权缺失**（该账号无匹配来源 IP 的 host），
  与本机网络、端口、库均无关：

```text
TCP connect 192.168.100.239:3306 → OK          ← 网络/防火墙/监听 全部正常
[rag / 空密码 / 不指定库]        → FAIL errno=1130: "192.168.100.10' is not allowed to connect to this MySQL server"
[root / 空密码 / 不指定库]       → FAIL errno=1130: 同上
```

### 定位过程

1. **先看"测试连接"到底把什么送到了后端**。前端只上报 `kind/endpoint/apiKey/model`：

```67:70:rag/web/static/js/config-data.js
  async function testConnection(kind, endpoint, apiKey, model) {
    return API.post('/api/admin/health/test',
      { kind, endpoint, apiKey: apiKey || '', model: model || '' });
  }
```

   ⇒ 表单里的 `host/port/user/password/database` **一个都没传**；后端只能取
   **已保存配置构建的容器适配器**（`routes.py` 的 `c.meta`）。
   所以"填了表单→测→失败"测的其实是**旧配置**（`host: localhost`），
   用户看到的现象与自己的输入之间**根本没有因果关系**。

2. **再看失败信息为什么是空的**。适配器的健康检查把异常整个吞掉：

```788:794:rag/adapters/mysql_meta.py
    async def health_check(self) -> bool:
        try:
            async with self._session() as s:
                await s.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
```

   ⇒ 明确语义的 `errno`（授权 1130 / 库不存在 1049 / 认证 1045 / 网络 2003）
   在 `except` 处被**降维成一个 bool**，页面再没有任何信息可展示。
   上面那条 errno 1130 是我另写探针**直连**才拿到的，页面永远看不到。

3. **顺带发现"降级态假绿"**。MySQL 不可达时容器会降级为内存实现：

```python
    # container.initialize()：探测失败 → 换用 MemoryMetaStore，并记入 degraded
    degraded["mysql_meta"] = "MySQL 不可达（localhost:3306），元数据已降级为内存模式…"
```

   而 `MemoryMetaStore.health_check()` **恒返回 `True`**。于是"不传表单值"的测试
   会显示 `✓ 连接正常` —— **降级被报成了健康**（与 TS-009 同族：能力存在 ≠ 能力可用）。

4. **确认建库/字符集两个疑问的答案**（用户侧环境问题，非代码缺陷）：

   - DSN 里写死了库名，而全仓**只有 `CREATE TABLE IF NOT EXISTS`，没有 `CREATE DATABASE`**：

```180:182:rag/adapters/mysql_meta.py
        dsn = (f"mysql+aiomysql://{config.user}:{config.password}"
               f"@{config.host}:{config.port}/{config.database}"
               f"?charset={config.charset}")
```

     ⇒ 库必须**预先手工创建**；表不用建（`auto_create_tables: true` 会在首次使用时自动建）。
     注意顺序：修完 1130 会立刻撞上第二个错误 `errno 1049 Unknown database 'rag_meta'`。

   - `charset=utf8mb4` **没问题且是必需**：DSN 里的是**客户端**字符集，建表 DDL 显式写了
     `DEFAULT CHARSET=utf8mb4` 且未指定 COLLATE，排序规则由服务端版本默认
     （8.0 → `utf8mb4_0900_ai_ci`）⇒ 合法。切忌改成三字节的 `utf8`。

### 修复方式

分三处（后端适配器 → 后端接口 → 前端传参），并做了接口级与浏览器级双重验证。

**① 适配器：把失败原因"翻译"出来，而不是吞掉**（`rag/adapters/mysql_meta.py`）

- 新增 `_driver_errno()`：SQLAlchemy 会把驱动异常塞进 `orig`，`errno` 只在最内层 `args[0]`，
  需要沿异常链取出来；
- 新增 `_mysql_failure_reason()`：把 errno 翻译成**"该去服务端做什么"**：

| errno | 语义 | 页面给出的处置 |
|---|---|---|
| 1130 | 来源主机未被授权 | 报出来源 IP，提示 `CREATE USER '<用户>'@'<IP>'` + `GRANT` |
| 1049 | 目标库不存在 | 提示先 `CREATE DATABASE` |
| 1045 | 认证失败 | 提示用户名/密码与服务端账号不匹配 |
| 1044 | 账号无权访问该库 | 提示补 `GRANT` |
| 2003 | 无法建立连接 | 提示服务未监听该地址或端口被防火墙拦截 |
| 其他/无 | — | 回退为 `errno <code>` + 驱动原文（**绝不丢弃**） |

- `health_check()` 保留（兼容调用方），但其实现改为薄封装，真身是可读原因版：

```python
    async def health_detail(self) -> tuple[bool, str]:
        try:
            async with self._session() as s:
                await s.execute(text("SELECT 1"))
            return True, "MySQL 连接正常"
        except Exception as e:
            log.warning("mysql_health_failed", error=str(e)[:200])
            return False, _mysql_failure_reason(e)
```

- 新增 `close()`（`await self._engine.dispose()`），供容器热重建与临时探测"用完即弃"时释放连接池。

**② 接口：支持用表单当前值直连探测，并识别降级态**（`rag/web/routes.py`）

- 新增 `_probe_mysql_with_form(c, rows)`：以已保存配置为底、用表单行做 `model_copy(update=...)`
  现建一个**临时适配器**，测完立刻 `close()` —— 不写 YAML、不动运行中的容器：

```python
    overrides: dict = {}
    for row in rows:
        k = str(row.get("key") or "")
        if k not in base.model_fields:
            continue
        v = _coerce_param(row)
        if v is None:                     # 空值/掩码 → 沿用已保存值
            txt = "" if raw is None else str(raw).strip()
            if txt and txt != "******":
                return False, f"参数 {k} 取值非法：{txt}"
            continue
        overrides[k] = v
    store = MySQLMetaStore(base.model_copy(update=overrides))
    try:
        ok, message = await store.health_detail()
    finally:
        await store.close()
```

- 响应新增 `probedWithForm` 字段；`kind="meta"` 且有表单值时走直连探测；
- 其余服务不支持直连，**明确标注**，避免再次误解：
  `message += "（按已保存配置测试，保存后才生效）"`；
- **降级态按失败报**，不再被内存实现的 `True` 骗过：

```python
        deg_reason = (getattr(c, "degraded", None) or {}).get(
            _DEGRADED_KEY.get(kind, kind))
        if kind in ("meta", "mysql_meta") and rows:
            online, message = await _probe_mysql_with_form(c, rows)
            probed_with_form = True
        elif deg_reason:
            online, message = False, str(deg_reason)
```

- 同理，`elif adapter is not None and hasattr(adapter, "health_detail")` 时优先取可读原因。

**③ 前端：把表单行原样带上**（`config-data.js` / `config-ui.js`）

```javascript
  /* params：所在分组的 configParams 行（含 type），后端据此还原类型直连探测，
     使「改了表单但还没保存」也能测到真实值，而不是已保存的旧配置 */
  async function testConnection(kind, endpoint, apiKey, model, params) { … }
```

```javascript
          const r = await ConfigData.testConnection(
            g.testKind || g.key, g.endpoint || '', …,
            g.configParams || []);
```

**验证结果**（两个临时脚本，均已删除）

- 接口级 `scripts/_tmp_health_probe.py`：**18/18 PASS**，含 1130 报文抽 IP、
  真实服务端 1044/1045、掩码密码不误判、非法端口报错、老载荷（无 `params`）兼容；
- 浏览器级 `scripts/_tmp_ui_mysql_probe.py`（Playwright，真机场景）：**10/10 PASS**。
  该场景自带判别力：**已保存配置 `host: localhost`（不可达）**，表单改成真机地址。
  若前端仍只上报 `kind`，改表单后必然仍失败；实测：

```text
① 表单初值 localhost（= 已保存配置）  → ✗ 无法建立连接（errno 2003）… 'localhost'
② 表单改为 192.168.100.239（不保存）  → ✓ MySQL 连接正常
③ 表单改回 127.0.0.1                  → ✗ 无法建立连接（errno 2003）… '127.0.0.1'
④ 端口输入框 type=number              → UI 层即拦截非法值（后端 _coerce_param 为第二道防线）
⑤ 全程未点保存                        → YAML 中 mysql_meta.host 仍为 localhost（未被改写）
```

  ②与③的**结论随表单值翻转**，排除了"恒成功"的伪证；⑤证明"测试"与"保存"副作用分离。

**（环境侧）服务端一次性准备**（对应现象中的 1130/1049，与代码修复无关）

```sql
CREATE DATABASE IF NOT EXISTS rag_meta DEFAULT CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'rag'@'192.168.100.%' IDENTIFIED BY '';
GRANT ALL PRIVILEGES ON rag_meta.* TO 'rag'@'192.168.100.%';
FLUSH PRIVILEGES;
```

执行后复测：`server=8.0.46-0ubuntu0.24.04.4`、`current_user=rag@192.168.100.%`、
`rag_meta` 库已存在（表为空，首次使用自动建）。
另一个易误判点：MySQL 8 对"**无权访问的、不存在的库**"返回的是 `1044` 而不是 `1049`，
两者在本次修复中都有对应的处置提示，不能只按 1049 兜底。

### 根因分析

1. **"测试连接"的语义被悄悄替换了。** 它测的是**已保存配置构建的运行实例**，
   而用户以为测的是**眼前这份表单**。两者在"改了但没保存"时必然背离 ——
   这属于**影子配置**问题：界面上存在两份真相，而动作作用于旧的那份。
   配置类 UI 的测试动作，要么对当前输入求值（本次采用：临时对象、用完即弃），
   要么**显式声明**作用域（本次对不支持直连的服务追加"按已保存配置测试"）。
2. **`except: return False` 是信息丢失，不是错误处理。** MySQL 的 errno 自带明确语义，
   恰好覆盖"授权 / 建库 / 认证 / 权限 / 网络"这五类用户可自行处置的故障；
   压成 bool 之后，页面的可操作性归零，用户只能反过来问"为什么连不上"。
   **异常应当被翻译，而不是被吞掉。**
3. **降级实现与真实实现共享了"健康"语义。** 内存兜底的 `health_check()` 恒 `True`，
   于是"降级"在监视口径上等价于"健康"—— 与 TS-009 完全同构（能力存在 ≠ 能力可用）。
   凡是存在降级路径的组件，健康判定必须先读 **`degraded` 记录**，再谈适配器自检。
4. **探测动作必须与副作用解耦，且必须可证伪。** 本次验证特意构造了
   「已保存值不可达 + 表单值可达」的对照，并让结论随表单值**双向翻转**；
   只测"改完能成功"是不够的，那无法区分"真的用了表单值"还是"恰好已保存配置也可用"。

---

## TS-012 保存任一模块都整容器重建（保存 MySQL 会重连 ES/Milvus/Redis，并弹出无关服务的降级）

### 现象

- 在外部依赖大多不可达的环境里（Milvus / Elasticsearch / Redis 均不可达），在配置页**任意**模块点「保存」，
  都会触发**整容器重建**：
  - 保存「MySQL 元数据」时，向量库（Milvus）、全文检索（ES）、Redis 的连接**被一并重建**
    （`AdapterRegistry.clear_cache()` 全清 → `ServiceContainer` 全量重造 → 全部健康检查重跑）；
  - 后台任务（会话归档调度、一致性检查、入库协调器）被 `stop_background` / `start_background`
    **停掉再起**，且旧容器会被 `shutdown()` 释放连接池 —— 正在使用旧容器的在途请求存在被中断的风险；
  - 最直观的是**提示失真**：保存「检索策略」的返回是
    `{"applied": true, "scope": "container", "degraded": {embedding, storage, mysql_meta, redis}}`，
    页面据此提示「本模块 已保存；仍处降级：embedding、storage、mysql_meta、redis」
    ⇒ 用户读到的信息是"我才改了检索策略，怎么向量库/存储/MySQL/Redis 全降级了"，像是被自己改坏了。
- 关键点：这些降级**在保存之前就已存在**（属启动期既有状态），保存只是把它们**重新报了一遍**；
  但由于重建过程真的重连了所有服务，只要某个依赖此刻恰好抖动，原本可用的组件就会被**新写成降级**。

### 定位过程

1. **先从"提示为何牵扯别的服务"入手**：读保存接口 `rag/web/routes.py` → 热应用走
   `rag/api/runtime.py:rebuild_container()`，其返回 `{"degraded": dict(new_c.degraded)}`
   是**整个新容器的全量降级表**，前端 `config-ui.js` 又把它原样拼进 toast
   ⇒ 提示与"本次保存了什么"之间**没有因果关系**。
2. **再看重建的代价**：读 `rebuild_container()` 实现，确认"保存一段 = 重启整个后端装配"：
   `clear_cache()` 全清单例 → 新建容器并全量 `initialize()` → `stop_background/start_background` 停旧起新。
   而前端早已做到"只提交当前卡片一个分组"（`config-data.js:groupPayload`）
   ⇒ **前端在收敛作用域，后端在放大作用域**，两边粒度差了一个量级。
3. **量化单段重建所需的最小能力**：审计 `AdapterRegistry`，只有 `clear_cache()`（全清）与 `create()`（新建并缓存），
   **缺一个"只摘掉某一个 key"的操作** ⇒ 这是单段热应用的第一个必要改造点。
4. **确定"哪些段可以单段热应用"**：逐段梳理后得出判据 —— **该段是否独占一个适配器**。
   独占者：`mysql_meta / vector_store / fulltext / storage / knowledge_graph / business_data /
   synonym / llm / embedding`，加上 `redis`（独立客户端）；其余段（检索策略、权限、编排、记忆…）
   没有独立适配器，且存在跨适配器联动，必须保留整容器重建作为兜底。
5. **顺带暴露三个"单段路径必须自己处理"的坑**（记入 TS-013）：降级记录的双键不一致、
   MySQL 降级后"对象非空即在线"的误判、Redis 客户端被别的服务快照引用。

### 修复方式

四层配合，引入 `scope`（热应用作用范围）概念：**保存只改了一个独占适配器的段时，只重建这一段**。

**① 注册表：新增"摘单例"**（`rag/adapters/registry.py`）

```python
    @classmethod
    def drop(cls, adapter_type: str, name: str) -> Any:
        """摘掉指定适配器的单例缓存并返回旧实例（单段热重建用）"""
        return cls._instances.pop((adapter_type, name), None)
```

`drop` 与 `clear_cache` 的差别就是本次修复的核心：**只动这一个 key**，其它服务的实例与连接原样保留。

**② 容器：段 → 适配器映射 + `apply_section()`**（`rag/container.py`）

```python
# 段名 → (容器属性, 适配器类型, 是否可选依赖)，口径与 initialize() 一致
SECTION_ADAPTERS: dict[str, tuple[str, str, bool]] = {
    "mysql_meta": ("meta", "mysql_meta", False),
    "vector_store": ("vector", "vector_store", True),
    "fulltext": ("fulltext", "fulltext", True),
    "knowledge_graph": ("graph", "knowledge_graph", True),
    "llm": ("llm", "llm", False), ...
}

    async def apply_section(self, section: str) -> None:
        attr, atype, optional = SECTION_ADAPTERS[section]
        self._clear_degraded(section)                # 先清本段降级记录（失败会重新登记）
        old = AdapterRegistry.drop(atype, name)      # 只摘本段单例
        new = (self._try_create(...) if optional else self._create_core(...))
        setattr(self, attr, new)
        await self._selfcheck_section(section)       # 只自检本段
        await _close_quietly(old)                    # 换上之后才释放旧连接，不打断在途请求
```

- `_selfcheck_section()` 的口径与启动时的 `initialize()` **逐条对齐**：可选依赖"检查不过 → 只关这一个功能"；
  核心适配器（llm/embedding/storage/synonym）"构造成功即生效，绝不因健康检查失败被置空"
  （否则会出现"保存一次 LLM，反把 LLM 置空"的灾难）；
  `embedding` 变更会**顺带**校准向量库 collection（维度可能变），这是唯一一处刻意的跨段联动。

**③ 运行期：按"改了几段"决定作用范围**（`rag/api/runtime.py:apply_config_update`）

```python
    keys = [k for k in update if k in _SCOPED_SECTIONS]
    single = keys[0] if len(keys) == 1 and len(keys) == len(update) else None
    if single is None or old.config.noconnection:          # 多段/无独立适配器的段/演示模式
        result = await rebuild_container(app)
        return {"scope": "container", "degraded": dict(result.get("degraded") or {})}
    setattr(old.config, single, getattr(new_config, single))   # 只搬这一段，其它段保持内存现值
    await old.apply_section(single)
    affected = SECTION_DEGRADED_KEYS.get(single, (single,))
    return {"scope": single, "degraded": {k: v for k, v in old.degraded.items() if k in affected}}
```

单段路径**不再** `stop_background/start_background`，也**不再**做全量自检；
返回的 `degraded` 只筛本段相关键 ⇒ 保存 MySQL 永远不会弹出向量库降级。

**④ 前端：提示如实反映作用范围**（`rag/web/static/js/config-ui.js`）

```javascript
        const scoped = !!(res && res.scope && res.scope !== 'container');
        if (res && res.applied === false) {
          showToast(res.message || (who + ' 已保存，但热应用失败（重启后生效）'), 'warning');
        } else if (names.length) {
          showToast(who + ' 已保存；仍处降级：' + names.join('、'), 'warning');
        } else {
          showToast(scoped ? who + ' 已保存并重连，未影响其它服务'
                           : who + ' 已保存并热应用', 'success');
        }
```

另外保存控件建好即按基线判定一次 `disabled`（`refresh()`），避免"没改动也点保存"触发一次无谓重建。

**验证**（两个临时探针，均已删除；保存会真的写 YAML，故用临时配置副本 + 独立实例）

后端探针（12/12 通过）：保存 MySQL 时 `AdapterRegistry.create` **只被调用 `mysql_meta` / `mysql_meta.memory`**；
`meta/vector/fulltext/graph/business/llm/embedding/storage/synonym/redis/memory_service/progress_bus/ingest_coordinator`
对象实例**一个没换**；Redis 客户端与 key 前缀不变；**未重启后台任务**；其它段的降级记录原样保留；
无独立适配器的段（如 `chunking`）→ `scope=container`；未知段名报错。

真浏览器探针（12/12 通过）：

```text
[1] LLM 大模型  {"applied": true, "scope": "llm", "degraded": {}}
    提示：LLM 大模型 已保存并重连，未影响其它服务     （YAML 落盘已校验）
[2] 检索策略    {"scope": "container", "degraded": {embedding, storage, mysql_meta, redis}}
    提示：本模块 已保存；仍处降级：embedding、storage、mysql_meta、redis
[3] 保存后 /api/health 200，配置页仍可正常渲染
[4] 演示模式    {"scope": "container", "degraded": {}} → 本模块 已保存并热应用
```

第 [2] 项正是本次改动的动机：整容器路径会把无关组件的降级一并报出来，而单段路径下保存 LLM
**不再牵连**这些提示。

### 根因分析

1. **"配置粒度"与"装配粒度"不匹配。** 前端早已收敛到"只提交当前分组"，后端却一律整容器重建
   ⇒ 两边粒度差一个量级，多出来的重建范围就是"误伤"和"噪声提示"的来源。
   判据：**配置热更新的粒度应与配置段/适配器的归属对齐**，只有"无独立适配器的段"才退回全量重建。
2. **缓存失效接口的粒度，决定了上层能实现的更新粒度。** 注册表只有 `clear_cache()`（全清）
   这一个失效入口，下游就只可能全量重建；补一个 `drop()`，"部分重建"才成为可能。
3. **重启式热更新有三个隐性代价**：连接重置、后台任务抖动、**状态表被全量重算**。
   第三个正是提示失真的根因 —— `degraded` 是全量重算的产物，与"本次改了什么"没有因果关系，
   却被当成保存结果推给用户 ⇒ **状态反馈必须与动作作用域对齐**（返回 `scope` + 本段 `degraded`）。
4. **回退路径必须保留且显式。** 检索策略/权限/编排等段没有独立适配器、存在跨适配器联动，
   强行单段化会漏配；保留 `rebuild_container` 兜底，并用 `scope="container"` 如实告知用户"这次动了整体"。

---

## TS-013 单段热应用的三个次生缺陷：假在线 / 降级键双命名残留 / Redis 客户端被快照后未同步

### 现象

改造 TS-012（单段热应用）时暴露出来的三个"必须一起处理"的缺陷。共同主题是：
**只换了主引用，衍生的引用与状态记录没跟上**，于是"配置页显示已生效"与"运行实况"脱节。
若不处理，单段路径会比整容器重建**更容易骗人**。

- **(a) 假在线**：MySQL 不可达时容器会降级为内存实现（`MemoryMetaStore`，其 `health_check()` 恒 `True`）。
  单段热应用若以"对象非空"判成功，就会在 MySQL **根本没连上**的情况下提示"已保存并重连"。
- **(b) 降级键双命名残留**：同一组件在 `degraded` 里存在**两套键名**
  （`vector` 与 `vector_store`、`graph` 与 `knowledge_graph`）。重建成功后只清其中一个，
  界面会出现"已经连上了却还挂着降级"的自相矛盾状态（TS-011 那条"假绿"的反面）。
- **(c) 换了 Redis 客户端，别的服务仍用旧连接**：`memory_service` / `progress_bus` 在启动时把
  `container.redis` **快照**到了自己的属性上（`ProgressBus(self.redis)`、`mem.redis`、`mem._prefix`）。
  单段热应用只替换 `container.redis` 时，会话仍然写旧连接、进度仍然发旧通道
  ⇒ **配置页显示已生效，实际链路还在用旧 Redis**（`_prefix` 也仍是旧前缀，换了 `prefix` 配置同样不生效）。

### 定位过程

1. **假在线**：写单段探针"重建 MySQL 但让它连不上"，期望得到降级提示，初版实现却给出"已保存并重连"
   ⇒ 回看判定逻辑：`getattr(self, attr, None) is not None` 对**降级替身**同样成立
   （`memory_meta` 是合法对象，与 TS-011 的 `MemoryMetaStore.health_check() → True` 同源）。
   ⇒ 结论：健康判定必须先读 `degraded` 记录，再谈适配器自检。
2. **双键残留**：对照 `initialize()` 登记降级时用的键名，与容器属性/日志里用的键名不一致；
   全仓搜索 `degraded[` 确认两套命名并存是历史遗留。
   单段重建只清"自己那一个键"，于是清了一半 ⇒ 界面矛盾。
3. **Redis 快照**：搜索 `\.redis` 的全部引用面，除容器自身外只命中
   `memory_service` 与 `progress_bus` 两处：`ProgressBus(self.redis)` 是构造参数，
   `mem.redis` / `mem._prefix` 在 `MemoryService.__init__` 中赋值
   ⇒ 确定"换客户端必须同步这两处"，且 `_prefix` 要随新配置重算。

### 修复方式

`rag/container.py`：

```python
# 段名 → 该段涉及的全部 degraded 键：历史原因同一组件有两套键，
# 重建成功时两套都要清，否则会出现"已经连上了却还挂着降级"的矛盾状态
SECTION_DEGRADED_KEYS: dict[str, tuple[str, ...]] = {
    "vector_store": ("vector", "vector_store"),
    "knowledge_graph": ("graph", "knowledge_graph"),
    "mysql_meta": ("mysql_meta",), "redis": ("redis",), ...
}
```

```python
    def _section_online(self, section: str) -> bool:
        """本段是否真的连上：对象存在**且**本段没有降级记录
        （MySQL 降级后会挂一个内存实现的替身，光看"对象非空"会误判为在线）"""
        if getattr(self, attr, None) is None:
            return False
        return not (set(SECTION_DEGRADED_KEYS.get(section, (section,))) & set(self.degraded))
```

```python
    async def _apply_redis(self) -> None:
        """Redis 段单段热应用：换客户端实例，并同步快照过它的服务"""
        ...
        ok = bool(await asyncio.wait_for(new.ping(), timeout=6))
        if not ok:
            new = None
            self.degraded["redis"] = "Redis 不可达，会话/记忆退化为进程内存"
        self.redis = new
        # 快照过 redis 的地方要一并换掉：否则会话仍写旧连接、进度仍发旧通道
        mem = getattr(self, "memory_service", None)
        if mem is not None:
            mem.redis = new
            mem._prefix = f"{cfg.prefix}session:"
        bus = getattr(self, "progress_bus", None)
        if bus is not None:
            bus.redis = new
        await _close_quietly(old)          # 换上之后才释放旧客户端
```

配套：`_selfcheck_section()` 里只有**可选依赖**才走"检查不过 → 关闭功能"，
核心适配器（llm/embedding/storage/synonym）与启动口径一致，不被健康检查置空（见 TS-012 ②）。

**验证**：

- 单段重建 MySQL（不可达）时，返回的是降级原因，而**不是**"已保存并重连"（`_section_online` 口径生效）；
- 保存 MySQL 时 `memory_service.redis` 与 `_prefix` **保持不变**（单段路径不误动 Redis 链路）；
- 浏览器探针：保存 LLM 返回 `degraded: {}`，提示"已保存并重连，未影响其它服务"（见 TS-012 验证）。

### 根因分析

1. **"存在"不等于"可用"，这条判据在降级路径上必须二次确认。** 降级实现是**合法对象**：
   `MemoryMetaStore` 会正常应答 `health_check()`，因此任何"对象非空即在线"的判断都会假绿
   （与 TS-009/TS-011 同族）⇒ 在线 = 对象存在 **且** 无降级记录。
2. **同一实体的多种命名，是"部分修复"的直接来源。** 两套 degraded 键让"清除降级"这类操作
   天然容易只做一半，产生自相矛盾的状态 ⇒ 凡是"一份状态多种键名"的历史包袱，
   都应收敛到一张**显式的键映射表**上（本例 `SECTION_DEGRADED_KEYS`），而不是靠调用方记性。
3. **快照式依赖注入会把"换实现"变成"换一半"。** `ProgressBus(self.redis)` 这类构造期快照，
   把"当前实例"这件事**复制**到了别处；此后替换容器属性只改变了主入口，
   衍生对象仍指旧实例 ⇒ **凡被快照引用的可变资源，替换时必须枚举并同步所有快照点**，
   否则"配置已生效"只是主引用层面的假象。
4. 与 TS-012 的关系：单段热应用的**范围越小，越不能靠"重算一遍全局状态"兜底**——
   整容器重建会顺手把上述不一致全部抹平，单段路径则必须显式处理每一处状态同步。

---

## TS-014 「测试连接」通过但保存后自检报 MySQL 不可达（两条链路的超时预算不一致）

### 现象

- 配置页「MySQL 元数据」点**测试连接**：返回通过（`MySQL 连接正常`）。
- 紧接着点**保存**，控制台出现：

  ```text
  [info     ] config_applied
      degraded={'mysql_meta': 'MySQL 不可达（192.168.100.239:3306），元数据已降级为内存模式（数据不持久化，请在配置页补齐连接信息）'}
      scope=mysql_meta
  ```

- 但服务端**完全正常**：MySQL 8.0.46 可达，账号 / 库 / 表都能用（同 TS-011 的那台）。
  也就是说，降级不是因为"连不上"，而是因为**探测没等完就被判死**。
- 危害不止于提示自相矛盾：`scope=mysql_meta` 的单段热应用（TS-012）会把 `container.meta`
  换成 `MemoryMetaStore` ⇒ 元数据**真的写进进程内存**，重启即丢。
  用户眼里是"保存成功"，实际是"持久化被悄悄关掉了"。

### 定位过程

1. **先量"一条新连接到底要多久"**。写探针分三层计时（裸 TCP / SQLAlchemy 引擎 / 适配器）：

   ```text
   [1] 裸 TCP connect                   : 0.00s
   [2] connect_timeout=3  冷建连        : 10.02s  OK      ← 3s 没起作用，且成功了
   [3] 线上实现 首次                    : 10.01s  ok=True
   [3] 线上实现 同引擎第二次            : 0.00s   ok=True   ← 只有冷连接慢
   ```

   ⇒ 慢的是**建连**，且只在**冷**连接上发生；TCP 毫秒级完成，10 秒全花在
   **服务端问候包**（`skip_name_resolve=OFF` 的反向 DNS 解析超时）。

2. **再看两条链路各自的预算**：

   - 「测试连接」→ `rag/web/routes.py:_probe_mysql_with_form()` 直接
     `await store.health_detail()`，**没有任何外层预算** —— 愿意等 10s ⇒ 通过；
   - 保存自检 → `rag/container.py:_selfcheck_section("mysql_meta")` 是
     `asyncio.wait_for(self.meta.health_check(), timeout=6)` —— 6s 到点即判失败 ⇒ 写入 `degraded`。

   同一次建连，两条链路给了 **10s** 与 **6s** 两个预算，结论必然相反。
   （与 TS-011 同族：**探测口径与运行口径不统一**。）

3. **顺手证伪"把 connect_timeout 调小就能快"**：aiomysql 下 `connect_timeout`
   **只约束 TCP**（而 TCP 0.00s 就完成了），握手那 10 秒不受它约束
   —— 实测 `connect_timeout=3` 依然等满 10s 并成功；aiomysql 也不接受
   `read_timeout`（直接 `TypeError`）。⇒ 唯一能约束握手的边界是**外层预算**，
   而它必须 **≥ 10s**。这纠正了 TS-004 在此环境下的一个隐含假设
   （"`connect_timeout=3` 是硬下限"其实是错的）。

4. **复现对照**（同一轮探针）：

   ```text
   [4] 冷建连 + wait_for(6)   [容器口径] : 6.00s  FAIL TimeoutError
   [5] 冷建连 + wait_for(20)  [放宽预算] : 10.03s ok=True
   ```

   ⇒ 根因锁定为**预算**，而非可达性。

### 最终定位：服务端 `skip_name_resolve` 写错了配置段

10s 握手延迟的根因在**服务端**：该参数被写进了 `[mysql]` 段，而服务端只认 `[mysqld]`。

```ini
# ✗ 无效：这是 mysql 命令行客户端程序的段
[mysql]
skip_name_resolve=ON

# ✓ 生效：服务端进程只认这一段
[mysqld]
skip_name_resolve=ON
```

改到 `[mysqld]` 并重启后 `SELECT @@skip_name_resolve` 变为 ON，**新建连接不再做反向 DNS**，
10s 延迟消失。这也解释了定位过程中那个反直觉现象：`SET GLOBAL` 看似改过、查询也像改过，
新连接却依旧慢 —— **客户端层面无论如何都消除不了它**。

⇒ 判据：**服务端参数"改了没生效"，先确认它的生效域与生效时机**
（`[mysql]` / `[client]` 只管客户端程序；`SET GLOBAL` 只对之后的新连接生效且重启即失效）。

### 客户端修复（第一轮：预算统一）

**① 预算收敛为代码常量，不作为用户配置项**（`rag/adapters/mysql_meta.py`）

把预算做成配置项等于暗示用户"调大这个数"是解法（这正是本问题最早的误判），
而解法其实在服务端；用户也不该为此做选择：

```python
POOL_SIZE = 10                # 连接池容量
CONNECT_TIMEOUT_SEC = 3       # 只约束 TCP 建连
HEALTH_BUDGET_SEC = 12.0      # 健康检查 /「测试连接」的公共预算
```

**② 适配器：带预算的探测 + 可操作的超时原因**

```python
    async def health_probe(self) -> tuple[bool, str]:
        budget = HEALTH_BUDGET_SEC
        started = time.perf_counter()
        try:
            ok, reason = await asyncio.wait_for(self.health_detail(), timeout=budget)
        except (asyncio.TimeoutError, TimeoutError):
            elapsed = time.perf_counter() - started
            return False, mysql_timeout_reason(self._config, budget, elapsed)
```

超时文案与"不可达"明确分开（两者要用户做的事完全不同）：

```text
MySQL 建连超时（4.0s 超过预算 4s）：TCP 已通但服务端 192.168.100.239:3306 的
问候包迟迟未返回，常见于服务端在建连阶段做反向 DNS 解析（skip_name_resolve=OFF）
或网络链路抖动；可在 MySQL 服务端 [mysqld] 段设 skip_name_resolve=ON 后重启验证
```

`_build_engine()` 的 `connect_args` 用 `CONNECT_TIMEOUT_SEC`，并注明它只约束 TCP 建连。

**③ 两条链路统一改调 `health_probe()`**

- `rag/web/routes.py:_probe_mysql_with_form()`（配置页「测试连接」）
- `rag/container.py:_check_meta()`（启动自检）与 `_selfcheck_section("mysql_meta")`（保存自检）

后者还顺手让降级文案带上具体原因（不再硬编码"MySQL 不可达"）：

```python
                self.degraded["mysql_meta"] = (
                    f"{reason}；元数据已降级为内存模式"
                    "（数据不持久化，请在配置页补齐连接信息）")
```

**④（已撤回）建连复用池：同一份配置只付一次建连**

> ⚠ **本节方案已整体撤回，仅作排查思路留存。** 它针对的是"服务端
> `skip_name_resolve=OFF` ⇒ 每次冷建连都要 10s"这个前提下的重复付费；
> 服务端把参数改到 `[mysqld]` 段（见上）之后，"复用"省下的只有毫秒，
> 却带来一个实实在在的代价：**「测试连接」会退化成"回放旧结果"** ——
> 服务端刚改配置、刚宕机，页面都可能报"正常"，而它恰恰是用来判断
> "此刻能不能用"的入口。因此改为**一律真实建连**：每个适配器持有自己的
> 引擎、`close()` 真正 `dispose()`、探测用完即弃（见本节末尾"最终设计"）。

预算修好后"假降级"没有了，但**重复付费**还在：配置页的操作序列是"先测连接、再保存"，
两条路径各自建连，用户就要为同一份配置连付两次 10s（实测 `保存 10.02s`）。
根因是**所有权**：适配器各自 `create_async_engine`，谁也复用不了别人的连接；
而 `close()` 又会把引擎 dispose 掉，于是"保存时热重建适配器"必然重新握手。

改成"**池持有引擎、适配器借用**"：

```python
@AdapterRegistry.register("mysql_meta", "mysql_meta")
class MySQLMetaStore(MySQLMetaAdapter):
    def __init__(self, config: MySQLConfig, borrow_engine: bool = True):
        ...
        if borrow_engine:
            self._engine = engine_for(config)      # 借用池中引擎（连同其空闲连接）
            self._owns_engine = False
        else:                                      # 单测 / 一次性脚本要独立引擎时
            self._engine = _build_engine(config)
            self._owns_engine = True

    async def close(self) -> None:
        if self._owns_engine:          # 借来的引擎绝不能 dispose，
            await self._engine.dispose()   # 否则保存热重建后又得重新握手
```

池按**连接参数指纹**登记（`conn_key` = host / port / user / password / database /
charset / pool_size / connect_timeout）：

- 参数没变 ⇒ 探测、保存自检、运行期查询借到的是**同一个引擎、同一条空闲连接**；
- 参数变了（如改了用户名 / 密码）⇒ 指纹不同，**不会**误用旧连接。

两个刻意的取舍：

- **不做容量淘汰**：引擎是惰性建连的廉价对象，只有连通过的配置才真的持有连接；
  而淘汰一旦误伤容器正在用的引擎，会让运行期查询直接失败 —— 风险远大于收益。
- **不跨事件循环复用**：引擎的连接绑定在创建它的循环上（`engine_for` 比对
  `get_running_loop()`），换循环即弃用重建，否则必然报错
  （测试里连续 `asyncio.run` 就是这种情形）。

配套两处：

- 容器的 `shutdown()` 调 `pool_clear()` 统一释放（适配器的 `close()` 对借用引擎是空操作）；
- `health_probe()` 在"等满但没超预算"时也告警，把 10s 的成因直接写进日志 ——
  这类"慢但成功"最难察觉，因为**没有任何一条失败日志**：

  ```text
  [warning] mysql_slow_connect elapsed=10.03 host=192.168.100.239
    hint='TCP 早已连通，这 10.0 秒等的是服务端问候包：服务端 skip_name_resolve=OFF 时
          每个新连接都要做一次反向 DNS 解析并等满超时'
  ```

**验证**（探针 `scripts/_tmp_probe_budget_verify.py`，已删除）

```text
[A] 旧口径 wait_for(6)          : 6.01s TimeoutError → 判为不可达（复现现象）
[B] 新口径 health_probe()       : 10.03s ok=True     MySQL 连接正常
[C] 预算故意调成 4s             : 4.01s ok=False     建连超时 + skip_name_resolve 线索
[D] 真实容器 initialize()       : 14.73s meta=MySQLMetaStore，degraded 不含 mysql_meta
[D] apply_section(mysql_meta)   : 10.02s section_online=True，degraded 不含 mysql_meta  ← 用户那条日志的路径
[E] _probe_mysql_with_form      : 10.02s ok=True     MySQL 连接正常
```

[D] 的 `meta=MySQLMetaStore` 是关键：修复前这里会变成 `MemoryMetaStore`（数据只在内存）。
（[D]/[E] 的 10s 正是**修复前的重复付费**：预算一致之后，剩下的耗时就是同一份配置被建了两次连，
下一轮由 ④ 消除。）

第二轮探针 `scripts/_tmp_mysql_pool_verify.py`（已删除，真实容器 + 真库，单段热应用路径）：

```text
[1] 探测(冷)      : 10.03s ok=True  reused=False   ← 首次：服务端握手，客户端无法消除
[2] 保存(热应用)  :  0.00s meta=MySQLMetaStore     ← 修复前 10.02s
[2] 空闲连接保留  : checkedin=1                    ← 引擎与连接都跨"重建"存活
[3] 探测(热)      :  0.00s ok=True  reused=True
[4] 改用户名      : conn_key 不同 ⇒ has_idle_connection=False（不误用旧连接）
```

即：当时"10s 只可能出现在某份配置的第一次接触上"，此后该配置的测试、保存、
运行期查询全部 0 建连 —— 因为池把引擎与空闲连接都留了下来。

**最终设计（撤回复用池之后）**：每个适配器持有自己的引擎，探测一律真实建连、用完即弃；
"是否复用连接"这个状态本身也一并删除 —— 没有缓存，就没有"是否复用"可上报：

```python
    def __init__(self, config: MySQLConfig):
        self._config = config
        self._engine = _build_engine(config)      # 自己的引擎，不跨实例共享
        self._session = async_sessionmaker(self._engine, class_=AsyncSession,
                                           expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()              # 用完即弃，不留后台空闲连接
```

`rag/web/routes.py:_probe_mysql_with_form()` 随之简化：每次新建适配器、真实建连、
`finally` 里 `close()`；返回体的 `probeReused` 字段一并移除。

### 根因分析

1. **同一判据被两条链路用不同预算执行，是"假成功 / 假失败"的通用来源。**
   「测试连接」与运行期自检本应回答同一个问题（"这份配置现在能用吗"），
   却各自硬编码超时（无预算 vs 6s）⇒ 结论互相矛盾。
   ⇒ 判据：**凡"配置页测试"与"运行期使用"共用同一资源，必须共用同一预算常量**
   （本例收敛到 `rag/adapters/mysql_meta.py:HEALTH_BUDGET_SEC`）。
2. **选错了边界参数，收紧超时会完全无效。** `connect_timeout` 只管 TCP，握手耗时不受它约束，
   所以"把 3s 调得更小"救不了这个问题，必须**放大真正生效的那个边界**；
   更糟的是"3s 硬下限"这一错误直觉会让人把 10s 的耗时误读成"服务不可达"。
   ⇒ 判据：**设超时前先量出耗时的构成，把边界放在真正耗时的那一段上。**
3. **"不可达"是一个需要被拆分的结论。** 旧文案把"等满超时"与"服务拒绝 / 不存在"合并成一句"不可达"，
   用户据此会去查网络与账号，而真正该做的是在服务端修名称解析（本例正是如此）。
   ⇒ **错误文案要携带"下一步该动哪里"**（同 TS-011 的 errno 语义化）。
4. **降级是一次有代价的写入，不是无害的提示。** MySQL 降级后 `self.meta` 换成内存实现，
   用户"保存成功"的同时**持久化被悄悄关闭**。因此降级判据必须尽量少假阳性：
   宁可多等几秒，也不要因预算偏紧把可用服务写成降级 ——
   本例的代价是该段最多多等几秒（`initialize()` 实测 14.7s，其余来自
   embedding/storage/redis 的降级），因此预算是代码常量、且刻意取宽。
5. **"能用"与"只付一次"是两个问题，预算只管前者。** 预算统一只保证结论一致，
   "同一份配置被建两次连"得靠所有权收敛来解决（当时的做法是引入建连复用池）。
   ⇒ 判据：**"重建对象"是否廉价，取决于它背后昂贵资源的所有权是否收敛**；
   但所有权要不要收敛，取决于那个"昂贵"是否真实存在 —— 见第 7 条。
6. **缓存的前提是"被缓存的对象仍然代表当前状态"。** 复用连接 / 引擎会把「测试连接」
   变成"回放旧结果"：服务端刚改配置、刚宕机，页面都可能报"正常"（而它恰恰是用来
   判断"现在能不能用"的入口）。⇒ 判据：**只有"复用不影响结论正确性"的资源才值得缓存；
   健康检查这类"问当前状态"的链路必须真实访问。** 本例即因此撤回了复用池：
   每个适配器持有自己的引擎，`close()` 真正 `dispose()`，探测用完即弃。
7. **优化要针对"真实存在的昂贵"，而不是"看起来昂贵"。** 复用池确实消除了热路径上的
   重复握手，但它成立的前提是"服务端每次冷建连都要 10s" —— 这个前提本身是**故障**，
   而不是设计约束。故障修掉之后，缓存就只剩成本（掩盖实时状态、跨事件循环的复杂度）。
   ⇒ 判据：**为绕开故障而引入的机制，故障修好后要主动回收。**
8. **"慢但成功"必须自己发声。** 有预算兜底后，10s 属于"成功"分支，不会触发任何失败日志，
   用户只能干等、且无法判断该不该去查服务端 —— 所以慢路径要显式告警并给出成因
   （`mysql_slow_connect`）。反之，**只有失败才留下痕迹的系统，最难排查的就是这种"合法地慢"。**
9. **服务端参数"改了没生效"，先确认它的生效域与生效时机。** `skip_name_resolve=ON`
   写进 `[mysql]` 段对服务端毫无作用（那是 mysql 客户端程序的段），服务端只认 `[mysqld]`；
   `SET GLOBAL` 又只对之后的新连接生效、重启即失效。本例"改过、查过、现象却没变"，
   让人在客户端反复找原因。   ⇒ 判据：**改服务端参数后先验证生效域，再动客户端代码。**

---

## TS-015 全文检索四处隐蔽缺陷：适配器根本构造不出来 / 「测试连接」测不出缺 IK 插件 / 原因被吞 / 超时又是用户配置项

### 现象

- 配置页「全文检索 (Elasticsearch)」点**测试连接**：只回一句 `ES 连接失败`，
  没有地址、没有状态码、没有"下一步该动哪里"。
- 保存后控制台出现：

  ```text
  [warning  ] optional_adapter_degraded
      component=全文检索 reason=适配器未实例化（可能已降级或未配置）
  ```

  用户照这句去查"是不是没配置"，但 YAML 里 `fulltext.enabled: true`、
  `hosts: [http://localhost:9200]` 都在。
- 就算地址填对、`curl http://localhost:9200` 也通，**索引照样建不起来**：
  `ensure_index()` 的 mapping 写死了 `ik_max_word`，ES 缺 analysis-ik 插件时
  `indices.create` 返回 400 ⇒ 文档写入与 BM25 检索全部失败；
  而旧的健康检查只发 `GET /`，**对此一无所知，一直报"正常"**。
- 危害不止于提示难看：全文检索一降级，`container.fulltext` 被置 `None`，
  写入路与检索路**同时消失**，页面只说"对应功能已关闭"，用户找不到"为什么关"。

### 定位过程

1. **先问"适配器到底建出来没有"**。绕过容器直接构造：

   ```text
   >>> from rag.adapters.fulltext import ElasticsearchFTS
   >>> ElasticsearchFTS(FullTextConfig(hosts=["http://localhost:9200"]))
   ModuleNotFoundError: No module named 'elasticsearch.asyncio'
   ```

   构造函数里写的是 `from elasticsearch.asyncio import AsyncElasticsearch`
   —— 这个子模块路径在客户端 8.x / 9.x 里**并不存在**
   （实测安装的 9.4.1 包内为 `elasticsearch/_async/client/`，无顶层 `asyncio` 子模块）。
   ⇒ 构造必然抛错 ⇒ `_try_create()` 吞掉异常、只记一条
   `optional_adapter_unavailable` ⇒ 容器把整段判为"未实例化"，
   页面就只剩那句无信息提示。**异步客户端要从包顶导入**：
   `from elasticsearch import AsyncElasticsearch`。

2. **再问"测通了是不是真的能用"**。旧实现是：

   ```python
   async def health_check(self) -> bool:
       try:
           await self._client.info()
           return True
       except Exception:
           return False
   ```

   它只证明"集群进程活着、TCP 通、认证过"，**不证明这套 ES 能承载业务**：
   本适配器的 mapping 依赖中文分词器，"能连上"与"用不了"可以同时成立。
   ⇒ 判据：**健康检查必须覆盖"业务真正依赖的能力"，而不是最容易探测的那一项**
   （同 TS-009：只校验路径存在，就会给出"配置全绿但功能不可用"的假信号）。

3. **让自检与 mapping 使用同一个分词器名**。mapping 里是 `ik_max_word`，
   自检就必须用 `ik_max_word` 调 `indices.analyze` ——
   换成别的名字、或只查插件列表，都可能"自检过、建索引 400"。

4. **离线复现各分支**（假客户端注入 400 / 超时 / 401，不依赖真实 ES）：

   ```text
   [0] 真实构造路径            PASS  适配器能用真实客户端构造成功（9.4.1 的 AsyncElasticsearch）
   [1] 连接正常 + IK 可用       PASS  ES 9.4.1 连接正常，中文分词器 ik_max_word 可用
   [2] 连接正常但缺 IK 插件     PASS  未检测到中文分词器 ik_max_word：ES 缺少 …analysis-ik 插件…
   [2b] 探测专用客户端视图      PASS  单次超时=4s、max_retries=0；业务客户端超时=10s
   [3] 连接超时 / 认证失败       PASS  两类能分辨（"连接超时（4s 内无响应）" vs "认证失败（HTTP 401）"）
   [4] 探测超时               PASS  探测超时…已超过预算 0.2s（≠"不可达"）
   ```

   第 4 项是把预算临时改小（`HEALTH_BUDGET_SEC=0.2`）跑出来的：
   只有"真的会等满预算"才说明 `wait_for` 兜底生效。

### 修复方式

| # | 位置 | 修复 |
|---|---|---|
| ① | `fulltext.py.__init__` | 改为 `from elasticsearch import AsyncElasticsearch`，老版本回退分支保留 |
| ② | `fulltext.py._check_ik()` | 用 `IK_ANALYZER = "ik_max_word"` 调 `indices.analyze`；失败文案直接指向"装 IK 插件" |
| ③ | `fulltext.py.health_detail()` | 返回 `(是否可用, 原因)`：**集群连通 + 分词器可用**两项都过才算可用；异常经 `es_failure_reason()` 语义化 |
| ④ | `fulltext.py.health_probe()` | `asyncio.wait_for(health_detail(), HEALTH_BUDGET_SEC)`；超时 → `es_timeout_reason()`（与"连不上"区分）；"慢但成功" → `es_slow_probe` 告警 |
| ⑤ | `fulltext.py` 常量 | 超时不再可配：`DATA_TIMEOUT_SEC=10`（业务面与旧默认值一致）、`PROBE_REQUEST_TIMEOUT_SEC=4`、`HEALTH_BUDGET_SEC=10`、`SLOW_PROBE_SEC=3` |
| ⑥ | `fulltext.py._probe_client()` | 探测走 `client.options(request_timeout=4, max_retries=0)`：**探测不重试**，业务请求仍用默认重试 |
| ⑦ | `fulltext.py.__init__` | `basic_auth` 改为"用户名或密码任一非空才带"，兼容 ES 未开启安全认证 |
| ⑧ | `config/models.py` + `customer_config.yaml` | `FullTextConfig` 删除 `timeout` 字段（并注明理由），客户配置同步删除 |
| ⑨ | `web/routes.py` | 「测试连接」优先调 `health_probe()`（与容器自检共用预算）；`_OPTIONAL_PARAM_KEYS` 增加 `username`（未开认证时用户名也可留空） |
| ⑩ | `web/routes.py` | `fulltext` 段新增「前提条件」面板：给出 IK 安装命令与 `_analyze` 验证命令 |
| ⑪ | `container.py` | `_check_optional()` / `_selfcheck_section()` 优先调 `health_probe()`，并把原因写入 `degraded[attr]`（不再只留在日志里） |

修复后的降级提示自带成因（原句"适配器未实例化"已不再出现）：

```text
fulltext: 全文检索不可用：未检测到中文分词器 ik_max_word：ES 缺少 elasticsearch-analysis-ik
          插件（或插件版本与 ES 版本不一致、未被加载）…，对应功能已关闭
```

### 根因分析

1. **"构造失败"与"未配置"是两种故障，绝不能共用一句提示。** `_try_create()` 用
   `except Exception` 吞掉构造异常后只剩一条日志，容器对外只说"适配器未实例化"，
   用户据此会去查配置项，而真正的问题是**代码里的导入路径错了**。
   ⇒ 判据：**降级提示必须携带原始异常**（本例 `ModuleNotFoundError: elasticsearch.asyncio`），
   否则"未配置"这句看似合理的解释会把排查带偏（同 TS-011 的"失败"二字）。
2. **"探得通"不等于"用得了"：健康检查的覆盖面要与业务依赖对齐。**
   ES 的 `GET /` 与业务实际用到的能力（中文分词 → 建索引 → 写入 → 检索）之间隔着
   插件与版本，只测最外层就会给出"合法地错"的绿灯。
   ⇒ 判据：**自检项应当是"业务失败时会踩到的那些前置条件"，且用与业务完全相同的参数去验证**
   （同一个分词器名，而不是"名字相近"的那个）。
3. **异常必须翻译，不能吞掉；且要按"处置方向"分类。** DNS 失败 / 拒绝连接 / 401 /
   403 / 证书错误五类的下一步动作完全不同，旧实现一律压成 `False`，
   等于把"可操作信息"降维成"不可操作布尔"（与 TS-011 对 MySQL errno 语义化同源）。
   ⇒ 判据：`except` 分支要么给出可展示的原因，要么明确说明为什么不需要。
4. **超时预算不是配置项，是判定口径的一部分。** `FullTextConfig.timeout` 是用户可填的，
   于是「测试连接」与运行期自检很容易各读各的值 —— 这正是 TS-014 的同一个坑换了个服务。
   用户把超时调大也解决不了连不上；把它调小反而会把可用服务误判为降级。
   ⇒ 判据：**凡是"用于得出结论"的边界参数，一律收敛为代码常量并让所有链路共用**
   （`DATA_TIMEOUT_SEC` 与探测预算分离，保证业务面行为与旧默认值一致）。
5. **探测路径不要重试，业务路径才需要重试。** 重试会把"4s 该给出的结论"拖成
   数十秒，还会把外层 `wait_for` 的取消吞掉（TS-006 的同一机理）。
   ⇒ 判据：**"一次性探测/构造"路径一律 `max_retries=0`，由外层预算兜底并报"探测超时"**。
6. **"等满超时"与"立刻被拒"必须给不同文案。** 前者多为静默丢包、错网段、TLS 握手卡住；
   后者是服务没在监听 —— 处置方向相反（TS-014 第 3 条的复用）。
7. **认证参数的可选性是"服务端配置"决定的，前端要能表达这种可选。**
   ES 未开安全认证时用户名与密码都该留空，而 `_OPTIONAL_PARAM_KEYS` 原先只放行
   `password`，用户被迫给一个不存在的账号填个值。
   ⇒ 判据：**参数的"必填/选填"应对齐服务端的真实契约**，而不是照抄某个部署形态的样例。
8. **同一份配置的"前置条件"要写在用户看得见的地方。** IK 插件是服务端责任，
   但只有应用知道"我依赖它、且依赖的是哪个名字"。把它做成配置页上的可复制命令，
   比写在日志或文档里有效得多 —— 这类"跨团队前提"是最容易在交付后失踪的信息。

---

## TS-016 改了自己的 ES 地址却报旧地址的超时：客户端/服务端大版本不匹配被"地址不可达"的文案掩盖

### 现象

- 把配置页「全文检索」的 `hosts` 从 `http://localhost:9200` 改成真实的
  `http://192.168.100.239:9200`，点「测试连接」得到：

  ```text
  全文检索不可用：连接超时（4s 内无响应）：地址或端口不可达、被防火墙静默丢包（DROP），
  https 还可能是证书握手卡住；原始信息：ConnectionTimeout('Connection timed out during request')，
  对应功能已关闭（按已保存配置测试，保存后才生效）
  ```

- 服务端自证是好的，任何一处都不像"不可达"：

  ```text
  root@mm-k8s:/data/elasticsearch# netstat -na |grep 9200
  tcp6  0  0 :::9200                :::*                  LISTEN
  tcp6  0  0 192.168.100.239:9200   192.168.100.239:52202 ESTABLISHED
  ```

  于是排查被这条文案锁定在"地址 / 端口 / 防火墙"上。

### 定位过程

1. **先量 TCP，别先改代码。** 在跑应用的那台机器上直接连：

   ```text
   9200 OK   0.00s ('192.168.100.239', 9200)
   3306 OK   0.00s ('192.168.100.239', 3306)
   ```

   秒开，与已知可用的 MySQL 端口表现一致；`urllib.request.getproxies()` 为空
   ⇒ **不是网络、不是防火墙、也不是代理**。

2. **再问"这句超时到底打的是谁"。** 读 `customer/customer_config.yaml`：
   `hosts` 仍是 `http://localhost:9200` —— **改动没保存**，所以那句 4s 超时
   是打 localhost 打出来的。用应用同一条异步探测路径复现：

   ```text
   http://localhost:9200        FAIL 4.01s  ConnectionTimeout('Connection timed out during request')  ← 用户看到的
   http://192.168.100.239:9200  FAIL 0.00s  BadRequestError 'media_type_header_exception'           ← 真实故障
   ```

   ⇒ 判据：**观测到的现象与"我以为我改的东西"不一致时，先确认测试打的是哪一份配置。**

3. **真打到目标地址是"立刻 400"，不是超时。** 打印响应体：

   ```text
   BadRequestError(400, 'media_type_header_exception',
     'Invalid media-type value on headers [Content-Type, Accept]',
     'Accept version must be either version 8 or 7, but found 9. '
     'Accept=application/vnd.elasticsearch+json; compatible-with=9')
   ```

   同一地址用 `curl` / 裸 `urllib` 都是 200 ⇒ 服务端没问题，**是客户端发的头它不认**。

4. **版本对表。** 服务端 `8.19.10`，客户端 `elasticsearch-py 9.4.1`。
   根因在依赖声明：`requirements.txt` 写的是 `elasticsearch[async]>=8.13`，
   **只有下界没有上界**，于是按"取最新"装到了 9.x。而 ES 客户端与服务端是
   **同大版本绑定**的：9.x 客户端默认发 `compatible-with=9`，8.x 服务端只认 8 / 7，
   于是**每一个**请求（含 `GET /`）都被 400 拒掉。

### 修复方式

| # | 位置 | 修复 |
|---|---|---|
| ① | `requirements.txt` | 锁 `elasticsearch[async]>=8.13,<9`，注释写明"大版本必须与服务端一致、9.x 会被 8.x 服务端逐请求 400" |
| ② | `fulltext.py.es_failure_reason()` | 新增分支：`400 + media_type_header_exception / Accept version must be` ⇒ 直接报"客户端与服务端大版本不一致"、给出 `pip install "elasticsearch[async]>=8.13,<9"`，并**明确排除**网络 / 防火墙 / IK 三个嫌疑 |
| ③ | `fulltext.py._check_ik()` | 归因加排他性依据：只有报错正文里出现 `ik_max_word` 才判"缺插件"，其余异常一律交回通用翻译 |
| ④ | `fulltext.py.aclose()` | 补上关闭入口（容器按 `aclose` / `close` 查找，此前找不到就静默跳过） |
| ⑤ | `web/routes.py._probe_fulltext_with_form()` | fulltext 的「测试连接」改为**用表单当前值真实建连**（与 MySQL 同源）；hosts 缺 `http://` 时在入口点明 |
| ⑥ | `web/routes.py.health_test` | `kind == "fulltext"` 且带表单参数时走 ⑤ |
| ⑦ | `web/routes.py._SERVICE_HINTS["fulltext"]` | 前置条件里的 IK 安装示例由写死 `9.4.1` 改为 `<ES版本>`，并注明"必须与服务端完全一致" |

修复后同一场景的结论（不再出现"地址不可达"）：

```text
ES 8.19.10 连接正常，中文分词器 ik_max_word 可用
```

验证（离线假客户端 + **真实 ES 8.19.10**，15 项断言）：

```text
[1] 大版本不匹配的 400 → 可操作文案              PASS ×3
[2] 不再把大版本 400 误报成「缺 IK 插件」          PASS ×4
[3] 「测试连接」改用表单当前值（已保存 localhost）  PASS ×2   ES 8.19.10 连接正常，中文分词器 ik_max_word 可用
[4] 地址缺协议的入口提示                          PASS ×1
[5] aclose() 真的释放连接池                       PASS ×4
```

第 3 组是最有价值的一项：**已保存配置里是 `localhost`（必然失败），表单里是新地址，
探测必须成功** —— 这正是用户被误导的那个场景；第 5 组用 spy 证明 `aclose()`
确实转发到客户端，且容器 `_close_quietly()` 能复用它。

### 根因分析

1. **依赖只写下界，等于把"未来所有大版本"都声明成兼容。** `>=8.13` 在几个月后
   解析成 9.4.1，而 ES 客户端 / 服务端是**同一大版本绑定**的（每个大版本换一次
   `compatible-with` 头）。⇒ 判据：**与外部服务同生命周期的客户端依赖要写区间
   （`>=x,<x+1`），并在注释里写明"为什么必须是这个区间"**；
   `pymilvus` / `neo4j` 是同类风险项。
2. **"改了没生效"必须能被一眼看出来，否则用户会拿一个错的观测去推理。**
   本例叠加了两个独立问题（旧地址 + 版本不匹配），而"地址不可达"的文案把注意力
   全部引到网络上。⇒ 判据：**探测类接口必须用"用户此刻提交的值"**；
   测的不是用户改的那份配置，结论就没有意义（同 TS-011 / TS-014）。
3. **文案再详细，只要描述的故障没发生，就是"合法地错"。** 旧文案把"4s 无响应、
   防火墙 DROP、TLS 卡住"讲得很具体，但那次请求的真实结局是 **0.0x 秒被 400 拒**。
   越详细的错误描述，越会**替用户排除掉正确方向**。
   ⇒ 判据：报错要先说清"实际观察到了什么"（耗时 / 状态码 / 响应体），再谈推论。
4. **归因必须绑定"该故障独有且必然出现"的证据。** `_check_ik()` 把任何非 401/403
   异常都判成"缺 IK 插件"，于是大版本 400 被写成"请安装 IK 插件" —— 用户去装一个
   本没问题的插件，而且**装完依旧报同样的错**。
   ⇒ 判据：证据不排他就不下结论，回落到不假设原因的通用翻译（与 TS-011 同源）。
5. **"能连上"的对象必须有一个关闭入口。** 适配器没有 `aclose` / `close`，
   容器的 `_close_quietly()` 又是"找不到就静默跳过"，于是每保存一次配置
   （fulltext 段热重建）就留下一个永不回收的连接池，`shutdown()` 同样释放不了。
   ⇒ 判据：**凡持有连接 / 线程 / 句柄的适配器都要提供关闭入口，并用测试证明
   它真的被调用**（"静默跳过"的地方就是泄漏的藏身处，同 TS-013 的假在线）。

---

## TS-017 向量库四处隐蔽缺陷：集合名不合法要等到建集合才炸 / 改 Milvus 地址后必须重启进程 / 「测试连接」测的是已保存的旧地址 / 探测蹭运行连接

### 现象

- **改了 Milvus 地址后保存**：接口 `applied: true`、`scope: vector_store`，但返回的 `degraded`
  里出现向量库、`container.vector` 被置 `None` ⇒ **向量写入与检索同时消失**
  （同 TS-015 的"整段关闭"，页面只说"对应功能已关闭"，不说为什么）。
  更糟的是**只有重启进程才能恢复**：把地址改回原值再保存也救不回来。
- **「测试连接」测的是旧地址**：表单里已经填好新地址，返回的却是旧地址的超时 / 不可达提示
  ⇒ 与 TS-011 / TS-014 / TS-016 完全同族（探测打的是"已保存配置"，不是用户此刻的输入）。
  叠加在向量路上后果更重：向量路一旦被关闭，页面**永远**只能复述构造期的旧原因，
  用户改对了地址也测不过 —— 而保存又被"测试连接必须通过"gate 住，形成**死锁**。
- **命名不合法要到建集合才炸**：知识域名带中文（如 `政策`）、或 `collection_prefix` 含短横线时，
  保存与「测试连接」都"看起来正常"，失败发生在启动期 `ensure_collection`，
  而它会把**整条向量路**关掉。用户拿到的结论是"向量库不可用"，看不出是名字的问题。
- **半套凭据被静默当匿名连接**：只填用户名、密码留空时，pymilvus 并不会报错，
  而是不发认证头；服务端开了认证时只回一句
  `code=2 illegal connection params or server unavailable` —— 同一句话同时覆盖
  "地址错 / 服务没起 / 认证失败"三种故障，照它去查防火墙必然跑偏（同 TS-011 的"失败"二字）。
- **换 adapter 后数据"消失"**：`vector_store` 段由 `milvus` / `qdrant` / `pgvector` 三个 adapter
  共用（`adapter` 字段切换），而三者此前的集合命名规则**并不一致**（见定位过程 1）
  ⇒ 切 adapter 后指向的是另一套集合 / 表。
- 环境前提：`customer_config.yaml` 用的是 `milvus` 且本机不可达（同 TS-001），
  因此本条的验证以「离线行为实测 + 模块导入自省」为主，未依赖真实 Milvus。

### 定位过程

1. **先核对三个 adapter 的命名口径**：读 `rag/adapters/vector_store.py` 发现
   `MilvusVectorStore` 用 `collection_prefix + 逻辑集合名`（`self._prefix`），
   而 `QdrantVectorStore` / `PgVectorStore` **直接用逻辑集合名**，前缀被完全忽略
   ⇒ 同一份配置换个 adapter 就落到另一套集合上，且**没有任何一处校验**名字合法性。
   Milvus 的标识符规则是"字母或下划线开头、其余为字母数字下划线、≤255"，
   中文 / 短横线 / 数字开头都会在**建集合那一步**才抛错。
2. **复现"改地址后只能重启"**：pymilvus 的 alias 是**进程级全局**的，
   `connections.connect(alias="default", host=B)` 在 `default` 已连接且配置不同时时
   直接抛 `ConnectionConfigException`（`Alias of 'default' already creating connections,
   but the configure is not the same as passed in`）；而全仓**没有任何 `disconnect`**
   ⇒ `_try_create()` 吞掉异常 → `self.vector = None` → 向量路被关闭。改回原地址同样失败
   （旧连接仍在），所以表现为"必须重启"。
3. **再看「测试连接」为什么帮不上忙**：旧实现取的是 `c.vector`（**已保存配置**构建的容器
   适配器），而且分支顺序是 `deg_reason`（降级态）优先 ⇒ 向量路被关闭后，
   无论表单填什么，返回的都是构造期那句原因。这与 TS-016 第 3 组（"已保存 localhost / 表单新地址
   必须成功"）是同一个判据，只是这里还多了一层**死锁**：测不过 ⇒ 保存被 gate 住 ⇒ 永远修不回来。
4. **审计 pgvector 的两处独立缺陷**：库名由 `collection_prefix.rstrip("_")` **反推**
   ——把"命名前缀"当成"库名"用，前缀一改就去找一个不存在的库，报的还是 psycopg 的
   `database ... does not exist`；另外 `search()` 在 `with` 块退出、连接已关闭后仍读
   `cur.description`（结果本就没被用到，是纯粹的死代码与潜在 `ProgrammingError`）。
5. **确认探测实例会泄漏**：三个 adapter 都没有关闭入口，而 `Probe` 路径需要"真实建连、
   用完即弃"；milvus 每次 `connections.connect` 都会新建 handler 与 gRPC 通道，
   qdrant 每次新建 `httpx.AsyncClient` ⇒ 每点一次「测试连接」/ 每保存一次配置就攒一个不回收的连接
   （与 TS-016 第 5 条同源：**持有连接的适配器必须有可被发现的关闭入口**）。
6. **顺带确认"慢但成功"无处发声**：有预算兜底后，慢响应属于成功分支，
   不产生任何失败日志，用户只能干等（同 TS-014 第 8 条 `mysql_slow_connect`）。
7. **核对配置面**：`VectorStoreConfig` 里没有独立的 `database` 字段（pgvector 的库名无处可填，
   才被迫从前缀反推），而 `timeout` 是用户可填项 —— 又一处"判定口径变成用户配置项"（同 TS-014 / TS-015）。

### 修复方式

改动集中在四层：**命名校验 → 连接生命周期 → 探测口径 → 配置页展示**。

| # | 位置 | 修复 |
|---|---|---|
| ① | `vector_store.py` | 新增模块级 `full_name()` / `name_error()` / `prefix_error()`：**三个 adapter 共用同一份命名规则**，非法名字在入口（构造 / 取集合名）就被拒，错误文案直接说明"只允许字母数字下划线、不能以数字开头，中文/短横线/空格/点号都不行" |
| ② | `vector_store.py` | 新增 `credential_error()`：用户名与密码**必须成对**（都填或都留空），只填一个直接拒绝并解释 pymilvus 的匿名降级行为 |
| ③ | `vector_store.py` | 新增 `ensure_connection()` / `remove_connection()`：按 `(host, port, user, password)` **签名**管理 alias。地址/凭据变了 → 先摘掉旧连接（顺带释放旧 gRPC 通道）再重连；同签名直接复用（旧行为每保存一次都新建 handler 且不 close） |
| ④ | `vector_store.py` | 新增 `MilvusVectorStore.probe()`：用**独立 alias**（`PROBE_ALIAS = "rag_probe"`）建一次性探测实例，**绝不动运行期的 `default` 连接**；`aclose()` 只释放探测连接（运行期连接由替换后的新实例继续持有） |
| ⑤ | `vector_store.py` | `health_detail()` / `health_probe()` / `health_check()` 三件套：所有分支都给原因；探测带 `HEALTH_BUDGET_SEC = 8.0` 预算并与容器自检**共用**；`SLOW_PROBE_SEC = 3.0` 以上告警；`PROBE_REQUEST_TIMEOUT_SEC = 5.0` 随 `GetVersion` 下发 |
| ⑥ | `vector_store.py` | 新增 `milvus_failure_reason()`：把 `code=2` 的笼统文案拆成**连接 / 认证 / 权限 / 超时**四类（认证类还额外去看 `__cause__` 里的 `UNAUTHENTICATED`） |
| ⑦ | `vector_store.py` | 所有 `Collection(...)` / `utility.*` 补 `using=self.alias`（原先一律默认 `default`，探测实例会串到运行连接上）；已有集合的向量维度与当前 embedding 不一致时告警，不静默改结构 |
| ⑧ | `vector_store.py` | Qdrant：补前缀与命名校验、`health_detail()` / `health_probe()`、`aclose()`（释放 httpx 客户端） |
| ⑨ | `vector_store.py` | pgvector：库名改为**独立配置项** `database`（不再从前缀反推）、表名走 `_table()` 校验、探测原因区分「库不存在 / 认证失败 / 连不上」、删除连接关闭后读 `cur.description` 的死代码 |
| ⑩ | `config/models.py` | `VectorStoreConfig` 新增 `database: str = "rag"`（pgvector 的库；库必须预先创建，应用只建表不建库） |
| ⑪ | `adapters/registry.py` | 新增 `get_class()`：探测需要**实现类**来建一次性实例，而 `create()` 会命中旧配置的单例缓存 |
| ⑫ | `web/routes.py` | 新增 `_probe_vector_with_form()`：按表单**当前值**真实建连探测（milvus 走 `klass.probe()` 独立 alias），`finally` 里 `aclose()`；并在 `health_test` 中接线 —— **位置刻意放在 `deg_reason` 分支之前**（见下） |
| ⑬ | `web/routes.py` | 展示面：`_SERVICE_HIDDEN_BY_ADAPTER` 让 `database` 仅 pgvector 可见、`_VECTOR_LABELS` 让分组标题跟随实际 adapter（`向量库 (Milvus)` / `(Qdrant)` / `(pgvector)`）、`_SERVICE_HINTS["vector_store"]` 补前提条件面板 |

关键片段一：**命名与凭据校验必须在入口拦**（`vector_store.py`）

```python
IDENT_MAX_LEN = 255
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def full_name(prefix: str, name: str) -> str:
    """逻辑集合名 → 物理集合名（prefix + name）"""
    return f"{prefix or ''}{name}"

def name_error(full: str) -> str | None:
    """校验物理集合名：合法返回 None，否则返回可读原因

    为什么必须在入口拦：名字不合法时 Milvus 只在**建集合**那一步抛错，而启动期
    ensure 失败会把整条向量路关掉（container.initialize 里 vector 置空）——
    用户只看到"向量库不可用"，根本看不出是中文域名或短横线惹的祸。
    """
```

关键片段二：**alias 是进程级全局的，重连前必须先摘旧连接**（`vector_store.py`）

```python
_CONN_SIGNATURE: dict[str, tuple[str, str, str, str]] = {}

def ensure_connection(config: VectorStoreConfig,
                      alias: str = DEFAULT_ALIAS) -> None:
    """按配置建立（或复用）连接；地址/凭据变了先摘掉旧连接再重连"""
    from pymilvus import connections
    sig = _signature(config)
    if connections.has_connection(alias):
        if _CONN_SIGNATURE.get(alias) == sig:
            return                                  # 同签名复用，避免每次保存新建 handler
        remove_connection(alias)                    # 不同签名：摘掉旧连接（含 gRPC 通道）
    connections.connect(alias=alias, host=config.host, port=str(config.port),
                        user=config.user or "", password=config.password or "")
    _CONN_SIGNATURE[alias] = sig
```

关键片段三：**探测与运行连接分离**（`vector_store.py`）

```python
DEFAULT_ALIAS = "default"
PROBE_ALIAS = "rag_probe"        # 配置页探测专用：绝不能让"测一个新地址"摘掉运行中的连接

    @classmethod
    def probe(cls, config: VectorStoreConfig) -> "MilvusVectorStore":
        """建一个**一次性**探测实例：用独立 alias，不碰运行期连接"""
        return cls(config, alias=PROBE_ALIAS)

    async def aclose(self) -> None:
        """释放探测连接

        运行期连接（default）不在这里摘：_close_quietly 是"换上新实例之后才调用"的，
        若顺手 remove default，正在用它的新实例会当场断线。
        """
        if self.alias != DEFAULT_ALIAS:
            await asyncio.to_thread(remove_connection, self.alias)
```

> **补记（2026-09-18，pymilvus 3.1 迁移）**：本条的 ③④⑦ 与下面「关键片段二 / 三」
> 描述的是 **alias 方案**（`ensure_connection` / `remove_connection` / `PROBE_ALIAS`）。
> 该方案已在迁移中整体移除，但**结论不变、且被更强地满足**——alias 是进程级全局资源，
> 迁移后改为 `MilvusClient(dedicated=True)`：每次探测是一条**专属连接**，`aclose()`
> 里 `close()` 会真正释放 gRPC 通道，运行期连接由适配器实例自己持有（惰性建、双检锁），
> 不再有"全局 alias 被换地址摘掉"这类跨实例耦合。验收口径同第 4 条判据：
> 探测与运行是两套实例、两套连接、两套生命周期。

关键片段四：**表单值直连探测，且必须排在"降级态复述"之前**（`web/routes.py`）

```python
        elif kind == "vector" and rows:
            # 同理：host 改了没保存时，旧行为测的是旧地址。且必须在 deg_reason
            # 之前 —— 向量路一旦被关闭（adapter 置 None），旧分支只会复述构造期
            # 的原因，用户改对了地址也永远测不过 → 保存被 gate 挡住
            online, message = await _probe_vector_with_form(c, rows)
            probed_with_form = True
```

**验证**（未落盘临时脚本，用内联 `python -c` 探针）：

```text
[1] py_compile：vector_store.py / routes.py / registry.py / config/models.py   PASS
[2] 模块真实导入：rag.web.routes 可导入且含 _probe_vector_with_form              PASS
[3] 命名校验行为：'rag_default'→合法；全角中文 / 短横线前缀 / 数字开头 → 给出具体原因  PASS
[4] 凭据成对校验：仅 user → 拒绝并解释会退化成匿名连接；user+password → 放行        PASS
[5] 配置默认值：VectorStoreConfig().database == 'rag'，可被 database='vec' 覆盖     PASS
[6] 接口自省：三个 adapter 类各自的 ensure_collection / upsert / search /
    delete_by_doc / delete_by_ids / get_doc_chunk_ids / health_detail /
    health_probe / health_check / aclose 均在类上（milvus 另有 probe）           PASS
```

（第 6 项是刻意做的：三个 adapter 共用同一个配置段与同一套调用面，
漏实现任何一个方法都会在**切 adapter 之后**才暴露，属"配置面共通、实现面各自为政"的典型坑。）

### 根因分析

1. **同一份配置段下的多个实现，必须共用同一套命名与校验契约。** `vector_store` 用 `adapter`
   切换 milvus / qdrant / pgvector，但三者的集合命名各写各的（前两个只用逻辑名、milvus 加前缀），
   ⇒ 用户切 adapter 后"数据消失"（其实是落到另一套集合上）。
   ⇒ 判据：**多实现共用一个配置段时，配置项的语义必须收敛到同一份模块级函数上**
   （`full_name` / `name_error` / `prefix_error`），而不是各 adapter 各自解释。
2. **校验点离失败点越远，排查成本越高。** 名字非法本可以在**入口**用一句可读原因拒掉，
   却因为没人校验，被推迟到启动期建集合时才抛错，而那条路径的失败后果是"整条向量路关闭"
   ⇒ 错误的**放大倍数**与校验点的位置成反比（同 TS-015 第 1 条）。
3. **进程级全局资源（pymilvus alias）让"换配置"变成"不可逆操作"。** alias 是全局单例，
   同 alias 换地址直接抛错，而代码从不摘旧连接 ⇒ 一次失败的保存就把向量路钉死在 `None`，
   且**无法通过再保存一次恢复**（旧连接始终还在）。
   ⇒ 判据：**凡"名字指向全局单例"的资源，重建前必须先摘旧、再建新**，
   并按配置签名判断"该复用还是该重建"（签名相同才复用，否则误用旧连接）。
4. **探测绝不能蹭运行实例。** 三处独立原因叠加：alias 是全局的（探测会把运行连接摘掉）、
   `AdapterRegistry.create()` 会命中旧配置的缓存（测的是旧地址）、运行实例被替换后
   正在承载在途请求（提前 `close()` 会打断业务）。
   ⇒ 判据：**"探测"与"运行"必须是两套实例、两套连接、两套生命周期**，
   探测实例用完即弃（`probe()` + `aclose()`），运行实例由容器按"换上新实例之后再释放旧的"处理。
5. **探测口径与运行口径必须统一，且判定分支的顺序不能反。** `health_probe()` 是两条链路
   共用的入口（同 TS-014 / TS-015）；更要紧的是 `health_test` 里
   **表单值探测必须排在"降级态复述"之前** —— 否则"适配器为 None"这一既有结论会
   无条件覆盖用户的每一次尝试，形成"改不对，也测不了，更存不了"的闭环。
   ⇒ 判据：**状态复述（缓存结论）永远排在实时探测之后**；缓存只能作为探测不可用时的兜底。
6. **"资源存在"的判据要跟着资源种类走：连接型资源必须提供关闭入口。**
   三个 adapter 原先都没有 `aclose()`，而容器 `_close_quietly()` 是"找不到就静默跳过"
   ⇒ 每次热重建都留下一条 gRPC 通道 / 一个 httpx 客户端（同 TS-016 第 5 条）。
   ⇒ 判据：**凡持有连接 / 线程 / 句柄的适配器都要有可被发现的关闭入口，并用探针证明它真被调用**。
7. **"慢但成功"要自己发声，半套凭据要在入口拒绝。** 前者是"有预算兜底后不产生失败日志"的
   固有盲区（`vector_slow_probe`）；后者是"库静默降级成匿名连接"造成的归因错误
   —— 一句 `code=2 illegal connection params or server unavailable` 同时覆盖
   地址 / 服务 / 认证三类故障，不翻译就只能靠用户猜。
   ⇒ 判据：**异常要按"处置方向"翻译（`milvus_failure_reason`），且能提前判定的配置错误一律提前拒。**

---

## TS-018 配置页三处"界面说了不算"：向量库用户名未标 optional（与成对规则相反） / milvus 露出对它无效的超时 / 业务数据 (SQL) 只关入口不关能力

### 现象

- **向量库卡片里「密码」标着 optional，「用户名」却没有** ⇒ 界面读起来是"用户名必填、
  密码可选"，而实际规则恰好相反：适配器要求两者**成对**（都填或都留空 = 匿名连接），
  只填一个会在构造期被拒绝（TS-017 第 ⑤ 条）。
  即：界面的暗示与后端的判定**正好反着**，用户按界面理解去填，必然撞上那句拒绝。
- **vector_store 卡片里有「超时(秒)」**。milvus 用户把它从 10 改成 60 再点「测试连接」，
  结果毫无变化：milvus 的探测预算在代码常量里（`HEALTH_BUDGET_SEC` /
  `PROBE_REQUEST_TIMEOUT_SEC`），配置里的这个值**只有 qdrant 会读**（httpx 客户端超时）。
  与 TS-014 / TS-015 同源：把判定口径做成用户配置项，只会让人以为"调大它就能连上"。
- **「业务数据 (SQL)」模块从配置页消失**，但 `customer_config.yaml` 里的 `business_data` 段
  仍在、容器也仍在初始化它。现象就是"配置文件里有、页面上没有" —— 后来人很容易把它当成
  一次遗漏（或 bug）顺手加回来，或反过来怀疑"是不是配置没生效"。

### 定位过程

1. **先找 optional 标记的唯一来源**：`_param_row()` 里只有一处
   `"optional": k in _OPTIONAL_PARAM_KEYS`，而该集合是**全局按参数键**定义的
   `{"password", "username"}`。向量库的用户名字段叫 `user`（不是 `username`），
   所以它从来没被标过 —— 不是漏写文案，而是**标记机制只能按参数键、无法按分组区分**：
   直接往集合里加 `user`，会连带把 MySQL 的用户名也标成可留空。
   核实字段名：`VectorStoreConfig.user`（向量库/MySQL）、`FullTextConfig.username`（ES）。
2. **再找"参数是否展示"的机制**：由 `_service_params()` 决定，其中已有"按 adapter 隐藏"
   的通道（`_SERVICE_HIDDEN_BY_ADAPTER`，TS-017 为 `database` 建的那张表），
   只是 `timeout` 从未进表 —— 于是它对"不读它的 adapter"照样可见。
3. **核实 timeout 到底谁在读**：`QdrantVectorStore.__init__` 用它建 httpx 客户端；
   `MilvusVectorStore` 的探测预算来自代码常量、连 `ensure_connection` 都不传超时；
   `PgVectorStore` 的 DSN 里也不含超时。（⇒ pgvector 的可见性列为**遗留待决**，见末尾。）
4. **核实业务数据段的实情**：`BusinessDataConfig` 仍是 `AppConfig` 的字段、
   `container.initialize()` 仍创建 `self.business`、`enabled_paths()` 仍会因它加入
   `structured` 检索路径 ⇒ 这次去掉的只是**配置页入口**，不是能力本身。
   同时确认 `_SERVICE_SECTIONS` 在保存路径里被当作白名单用
   （`service_keys = {k for k, _, _ in _SERVICE_SECTIONS}`），所以从它里面移除即等于
   关掉该分组的编辑入口，无需再加别的判断。

### 修复方式

| # | 位置 | 修复 |
|---|---|---|
| ① | `routes.py` `_param_row` | 增加分组入参 `group`，标记来源改为「全局参数键 ∪ 本分组追加」（`_OPTIONAL_BY_GROUP = {"vector_store": {"user"}}`）—— 只影响向量库，MySQL 的用户名语义保持不变 |
| ② | `routes.py` `_SERVICE_HIDDEN_BY_ADAPTER` | `vector_store` 增加 `"timeout": {"milvus"}`：milvus 下不渲染「超时(秒)」，也不回写（YAML 原值保持不动） |
| ③ | `routes.py` `_SERVICE_SECTIONS` | 移除 `business_data` 分组：配置页不再有该卡片，保存路径也不再接受它的分组；`BusinessDataConfig` 字段与容器初始化**刻意保留** |

关键片段：**标记的粒度跟着分组走，而不是跟着参数键一刀切**

```python
# 允许留空的参数：UI 在输入框右侧标注 optional
_OPTIONAL_PARAM_KEYS = frozenset({"password", "username"})

# 按分组追加「可留空」的参数：向量库的 user 与 password 是**成对**规则
# （都填或都留空 = 匿名连接，只填一个会被适配器在构造期拒绝）。原先只给 password
# 标了 optional，界面读起来像"用户名必填、密码可选"，与实际规则正好相反。
_OPTIONAL_BY_GROUP = {"vector_store": frozenset({"user"})}
```

```python
    optional = (k in _OPTIONAL_PARAM_KEYS
                or k in (_OPTIONAL_BY_GROUP.get(group or "") or ()))
```

关键片段：**参数可见性由"当前 adapter 是否真读它"决定**

```python
_SERVICE_HIDDEN_BY_ADAPTER = {
    "vector_store": {
        "database": {"milvus", "qdrant"},
        # timeout 实际只有 qdrant 读取（httpx 客户端）；milvus 的探测预算由代码常量
        # 管理（HEALTH_BUDGET_SEC / PROBE_REQUEST_TIMEOUT_SEC），配置里的这个值
        # 对它没有任何作用 —— 留在界面上只会让人以为"调大它就能连上"。
        "timeout": {"milvus"},
    },
}
```

**验证**（离线，直接调用载荷构造函数）：

```text
ORDER  ['mysql_meta', 'storage', 'redis', 'fulltext', 'vector_store',
        'knowledge_graph', 'synonym']                      # business_data 已移除
MILVUS [enabled, host, port, user(optional), password(optional),
        collection_prefix]                                 # timeout 不再出现
MILVUS user optional = True / password optional = True      # 成对，界面与规则一致
ES     [hosts, username(optional), password(optional), index_prefix]   # 未受影响
MySQL  user 仍未标 optional                                 # 分组粒度生效，未被连带
```

### 根因分析

1. **界面标记的"粒度"必须与规则的"粒度"对齐。** 规则是**成对**的（用户名 + 密码一起留空），
   标记却是**按单个参数键**给的。只要两个字段键名不同（`user` vs `password`）、
   或者同一个键在不同分组下语义不同（MySQL 的 `user` 是必填、向量库的 `user` 可留空），
   一张全局表就必然说错其中一边。
   ⇒ 判据：**凡"成对 / 互斥 / 分组相关"的规则，标记要么成对给、要么按分组给**，不能按参数键一刀切。
2. **展示一个"对本 adapter 无效"的参数，等于给出一条错误的因果线索**（同 TS-014 第 6 条、
   TS-015 第 7 条）。用户看到「超时(秒)」就会认为连不上是它造成的，于是 10 → 60 反复试，
   而这条链路根本不读这个值 —— 试错成本全由用户承担。
   ⇒ 判据：**参数可见性应由"当前 adapter 是否真的读它"决定**，而不是"配置段里恰好有这个字段"。
3. **产品裁剪只做在入口层时，必须留下文字说明。** 去掉的是配置页入口，而字段、容器初始化、
   检索路径都还在 ⇒ "配置文件里有、页面上没有"这种**刻意的不对称**，在没有说明的情况下
   与"漏配"完全无法区分（同 TS-017 第 1 条：多处入口的不对称是缺陷的温床，即使这次的不对称是故意的）。
   ⇒ 判据：**刻意制造的不对称要在代码注释与文档里同时写清"范围到哪、为什么"**。

> **遗留待决**：`timeout` 目前只在 milvus 下隐藏，而 `PgVectorStore` 也不读它（DSN 无超时项）。
> 若要彻底消除"无效参数可见"，应把隐藏集合扩成 `{"milvus", "pgvector"}`，
> 或干脆把该字段从 `VectorStoreConfig` 里删掉（改由代码常量管理，同 TS-014 / TS-015 对
> MySQL、ES 超时的处置）。本次按需求只处理 milvus，未擅自扩大范围。

---

## TS-019 缓存 (Redis) 四处隐蔽缺陷：探测测的是已保存的旧连接 / 降级后保存死锁 / 失败原因被吞成"连接失败" / 会话 TTL 冒充连接参数

### 现象

Redis 卡片有「测试连接」按钮，且保存按钮受它 gate（`saveGate: 'test'`，字段一改
`tested` 即清零）。在这个前提下，同一个按钮表现出两副**互相矛盾**的面孔：

- **面孔 A（测得过、存不下）**：把 `host` 从 `localhost` 改成 `192.168.100.239`，
  点「测试连接」→ `✓ 3ms`、弹"Redis 连接正常"、保存按钮变亮；点保存 → 提示里出现
  Redis 相关降级。用户的实际观测（"测试说通"）与结局（"保存后说不可用"）相反，
  而两条链路都"有据可查"：探测打的是 `customer_config.yaml` 里那份**旧配置**。
- **面孔 B（测不了、存不了）**：Redis 曾经不可达（`degraded["redis"]` 有记录）时，
  无论把地址/密码改成什么，点「测试连接」永远返回那句构造期的旧原因
  （"Redis 不可达，会话/记忆退化为进程内存"）⇒ 保存按钮恒灰 ⇒ **只能手改
  `customer_config.yaml` 并重启进程**。用户改对了也没有任何出路。
- **面孔 C（原因不可处置）**：失败时只有两种文案 —— `连接失败: <原始异常>` 或
  `Redis 不可达`。而"Redis 连不上"至少分四类、处置方向完全不同：密码错（改密码）、
  DB 编号越界（改编号）、地址/端口不通（查服务与防火墙）、超时（查网络丢包）。
  照着"不可达"去查防火墙，是最常见的跑偏方向。
- **面孔 D（参数讲了错的话）**：卡片里 6 个参数中，`session_ttl_hours`（界面写作
  「会话 TTL(小时)」）**与连接毫无关系**（只管会话键的过期秒数、`×3600` 作为 `EX`，
  且填 0 会让每次保存都 `invalid expire time` 并静默退化为进程内存），却和 host/port
  并排出现，是用户唯一会去调的"旋钮"；而真正决定"读写落在哪个键空间"的
  `db`（DB 编号）与 `prefix`（键前缀）没有任何说明 —— 尤其 `db`：它一旦写进配置，
  后续所有会话读写都用它，与已有数据不在同一个库时表现为"连得上但会话是空的"。

### 定位过程

1. **先穷举 Redis 的"建连口径"总共有几处**：`container.initialize()`（建实例）、
   `container._apply_redis()`（单段热应用建新实例）、`container._check_redis()`
   （自检 ping，超时 6s）、`routes.health_test`（配置页探测）。
   前两处各自手写了一份 `aioredis.Redis(host=…, socket_connect_timeout=3,
   socket_timeout=5, retry=Retry(NoBackoff(), 0))`，第三处硬编码 `timeout=6`，
   第四处**根本没有建连** —— 口径被复制了 3 份、探测链路则完全是另一套。
2. **核实 `health_test` 的分支顺序**：`kind == "vector"` 与 `kind == "storage"`
   都已经有"表单值直连"分支，且注释里都明写"**必须在 `deg_reason` 之前**"
   （TS-016 / TS-017 的产物）；而 `kind == "redis"` 落在 `elif deg_reason:` **之后**，
   且用的是 `c.redis`（已保存配置的实例）⇒ 面孔 A + 面孔 B 是同一个结构问题
   （"事实来源错 + 优先级错"）的两面，而不是两个 bug。
3. **核实前端 gate 与字段清零**：`config-data.js` 的 `testConnection()` 已经把
   `g.configParams` 整组作为 `params` 上送（TS-011 时改的），后端只需消费；
   `config-ui.js` 里 `const needTest = g.saveGate === 'test'` 与
   `touch()` 中的 `if (needTest && tested) tested = false` 确认了"测试是保存的唯一前置"，
   面孔 B 的死锁由此成立。
4. **实测失败原文**：本机无 Redis，对 `127.0.0.1:6399` 建连抛出的原文是
   `Error 22 connecting to 127.0.0.1:6399. 远程计算机拒绝网络连接。.`
   ⇒ **套接字错误文本是按操作系统语言本地化的**，"connection refused" /
   "actively refused" 这类英文关键词一个都不出现。这条不只影响本次改动：
   凡是靠关键词匹配做"异常翻译"的代码，在中文 Windows 上都会静默落到兜底分支，
   退回成"一句没用的话"（与面孔 C 同一后果）。
5. **核实界面参数的作用域**：`_service_params()` 已有"隐藏参数"通道（TS-018 建的
   `_SERVICE_HIDDEN_PARAMS`），`session_ttl_hours` 只是从未进表；`db` / `prefix`
   的标签来自 `_PARAM_LABELS`，其中 `bucket` 早在 MinIO 卡片用过
   「Bucket（默认）」这种"标出作用域"的写法 ⇒ 面孔 D 属于同一类"文案没写作用域"。

### 修复方式

| # | 位置 | 修复 |
|---|---|---|
| ① | **新增** `rag/adapters/redis_cache.py` | 把建连口径收敛成单一来源：`REDIS_CONNECT_TIMEOUT_SEC=3` / `REDIS_SOCKET_TIMEOUT_SEC=5` / `REDIS_HEALTH_BUDGET_SEC=6.0` / `REDIS_SLOW_PROBE_SEC=2.0`；`make_client(cfg)` 统一工厂；`probe(cfg)` 建连 → `PING` → **用完即 `aclose()`**；`redis_failure_reason(e, cfg)` 八类翻译 |
| ② | `container.py` 三处 | `initialize()` / `_apply_redis()` / `_check_redis()` 改用 `make_client()` 与 `REDIS_HEALTH_BUDGET_SEC`；ping 异常经 `redis_failure_reason()` 翻译后写入 `degraded["redis"]`（原来只有一句"Redis 不可达"） |
| ③ | `routes.py` 新增 `_probe_redis_with_form()` | 逐行解析表单参数（掩码 `******` → 沿用已保存值；**空串 = 有意改成匿名连接**，真按空值测），`type(base).model_validate({**base.model_dump(), **overrides})` 校验后调 `probe()` |
| ④ | `routes.py` `health_test` | 增加 `elif kind == "redis" and rows:` 并**排在 `elif deg_reason:` 之前**；保留"无表单参数"时的旧兜底分支，但失败原因同样走翻译 |
| ⑤ | `routes.py` 界面口径 | `db` → 「DB 编号（默认）」、`prefix` → 「键前缀（默认）」；新增 Redis 卡片 ⓘ「前提条件」（无需预建资源 + ACL 命令面 + DB 范围 + `maxmemory-policy`）；`session_ttl_hours` 加入 `_SERVICE_HIDDEN_PARAMS`（不渲染也不回写，YAML 原值不动） |

关键片段：**建连口径收敛到一处，探测用完即弃**

```python
# rag/adapters/redis_cache.py
REDIS_CONNECT_TIMEOUT_SEC = 3      # 建连超时（socket_connect_timeout）
REDIS_SOCKET_TIMEOUT_SEC = 5       # 读写超时（socket_timeout）
REDIS_HEALTH_BUDGET_SEC = 6.0      # 自检 / 配置页探测共用的总预算
REDIS_SLOW_PROBE_SEC = 2.0         # "慢但成功"的发声阈值

def make_client(cfg):
    """按配置建一个 redis.asyncio 客户端（只建对象，不建连、不 ping）"""
    ...
    # 显式禁用 redis-py 8.x 默认的 10 次指数重试（同 TS-006）
    return aioredis.Redis(..., retry=Retry(NoBackoff(), 0))
```

```python
async def probe(cfg) -> tuple[bool, str]:
    client = None
    try:
        client = make_client(cfg)
        await asyncio.wait_for(client.ping(), timeout=REDIS_HEALTH_BUDGET_SEC)
    except (asyncio.TimeoutError, TimeoutError):
        return False, f"探测超时（超过 {REDIS_HEALTH_BUDGET_SEC:g}s 无响应）：…"
    except Exception as e:
        return False, redis_failure_reason(e, cfg)
    finally:
        # 探测客户端是一次性的：真实建连之后必须释放，否则每点一次"测试连接"
        # 就攒下一个不回收的连接池（同 TS-016 第 5 条）
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
```

关键片段：**失败原因按"处置方向"翻译，中英关键词双覆盖**

```python
    # 4) 网络层。
    #    注意：这里的关键词必须**英文 + 中文**都要覆盖 —— Windows 套接字错误文本
    #    是按系统语言本地化的（实测本机报 "…远程计算机拒绝网络连接。"，一个英文关键词
    #    都不含），只按英文匹配会全部落到兜底分支、又退回"一句无用的话"。
    if ("connection refused" in low or "actively refused" in low
            or "no connection could be made" in low or "connect call failed" in low
            or "10061" in low or "拒绝网络连接" in raw or "拒绝" in raw):
        return f"无法连接 {where}：连接被拒绝（服务未启动或端口不对）（{raw}）"
```

关键片段：**探测打"用户此刻填的值"，且排在状态复述之前**

```python
        elif kind == "redis" and rows:
            # 同理（TS-019）：Redis 一旦降级，旧分支只会复述构造期的原因，
            # 用户改对了地址也永远测不过 → 保存被 gate 挡住 → 只能手改 YAML 重启
            online, message = await _probe_redis_with_form(c, rows)
            probed_with_form = True
        elif deg_reason:
```

```python
    # 这里走一次真正校验（不是 model_copy：它不做类型校验，会把 "abc" 直接塞给
    # 客户端，最后表现成一次莫名其妙的连接超时）
    try:
        cfg = type(base).model_validate({**base.model_dump(), **overrides})
    except Exception as e:
        return False, f"参数校验未通过：{str(e)[:180]}"
    return await redis_probe(cfg)
```

**验证**（离线，26 项断言全通过；本机无 Redis，故"真实建连"用关闭端口与非法参数来触发）：

```text
factory host/port/db = ('192.168.100.239', 6379, 3)   password 透传 / decode_responses=True
factory connect/socket timeout = (3, 5)   retry 次数=0   空密码 -> None（匿名连接）
translate auth      -> 认证失败：密码不正确（或被 ACL 用户禁用）（AUTH failed: WRONGPASS…）
translate noauth    -> 服务端要求认证，但未填密码（NOAUTH…）
translate db越界    -> DB 编号 3 超出服务端 databases 范围（DB index is out of range）
translate 拒绝(中文实测) -> 无法连接 192.168.100.239:6379：连接被拒绝（服务未启动或端口不对）
translate 不可达 / dns / 超时 / 未知 -> 各自落到对应分支，未知异常保留类型名
probe 关闭端口       -> 无法连接 127.0.0.1:6399：连接被拒绝（…）（含原始异常原文）
probe db=-1          -> DB 编号不能为负数（当前 -1）
表单值优先           -> YAML 是 10.0.0.1，报错指向 127.0.0.1:6399        # 面孔 A 已修
非法 db（int 字段填 abc）-> 参数 db 取值非法：abc
db 字符串进入校验     -> 参数校验未通过：1 validation error for RedisConfig
空表单 / 空密码       -> 前者提示"未提供可用参数"，后者按匿名连接真去连
redis 分组可见参数    -> ['host', 'port', 'password', 'db', 'prefix']     # TTL 已隐藏
标签                 -> db='DB 编号（默认）' / prefix='键前缀（默认）'
health_test 分支顺序 -> form(4295) < degraded(4541)                       # 面孔 B 已修
```

### 根因分析

1. **同一依赖的"建连口径"被复制到多处，就等于没有口径。** Redis 是三处手写参数 +
   一处根本没建连；只要有一处不同（本次是"完全没有"），两边的结论就会不一致，
   而且都"有据可查"。同 TS-014（探测预算 vs 自检预算）、TS-015（探测 vs 业务）。
   ⇒ 判据：**一个外部依赖只能有一个建连工厂与一组超时常量**，谁需要客户端都来调它。
2. **分支的"顺序"就是"优先级"，改动必须成组核对。** `storage` / `vector` 两处
   在 TS-016 / TS-017 中已经改成了"实时探测在前、状态复述在后"，Redis 却因为
   "不在那次需求范围内"而漏掉 —— 于是同一个函数里三种服务三套优先级。
   ⇒ 判据：**凡按 `kind` 分派的逻辑，改一处就要把 `_SERVICE_SECTIONS` 逐项对照一遍**
   （尤其是"探测 / 复述"这类成对分支），否则缺陷会以"缺一个 elif"的形式长期潜伏。
3. **异常翻译依赖的关键词会随环境变（这次是操作系统语言）。** 实测原文
   `…远程计算机拒绝网络连接。` 不含任何英文关键词 ⇒ 只写英文匹配的翻译表在中文
   Windows 上等于没写；而"兜底分支"又恰好长得像一句正常的错误提示，肉眼很难发现它没生效。
   ⇒ 判据：**翻译表要按"性质维度"穷举（认证/编号/网络/超时），关键词中英双写、
   且始终附原始异常原文**；验证时不能只看"有文案返回"，要断言它命中了**预期分支**。
4. **参数出现在某张卡片里，就等于对用户宣告"它影响连接"。** `session_ttl_hours`
   与连接无关、`db` / `prefix` 的作用域又没写清，用户自然会去调"唯一可调的那个"
   （而它调小了反而更坏）。同 TS-018 第 2 条（milvus 露出它对它无效的 `timeout`）。
   ⇒ 判据：**卡片上的每个参数都要能回答"它影响什么"；答不上来的不进连接卡片，
   有作用域的（默认值 / 命名空间 / 运行期口径）在标签或 ⓘ 里写明。**
5. **降级记录是配置页提示的输入源，它写什么用户就只能看到什么。** 原实现把四类故障
   压成一句"Redis 不可达"，于是"配置页提示"与"日志"同时失去了可处置性（同 TS-011
   的"失败"二字、TS-013 的假在线）。
   ⇒ 判据：**凡是会被复述给用户的字符串，生成时就要带上"该怎么处置"**。

> **遗留待决**：① `PING` 覆盖不到 ACL 的命令级权限（`KEYS` 被禁 → 会话列表为空；
> `PUBLISH` 被禁 → 多 worker 收不到进度）与 `maxmemory-policy` 淘汰会话键，
> 这两项只能在服务端确认 —— 已写进卡片 ⓘ，未做代码校验。
> ② `session_ttl_hours` 仍无范围校验（填 0/负数会 `invalid expire time` 并静默退化为
> 进程内存），本次只把它从界面移除，未在 `RedisConfig` 上加 `gt=0`。
> ③ 旧前端（不带 `params`）仍走"按已保存配置测试"的兜底分支，属兼容路径。

> **实测续记（2026-09-18）：一次真实命中"只绑回环"的案例，以及由此改进的文案**
>
> 应用在 `192.168.100.10`（Windows），host 填 `192.168.100.239` 后点「测试连接」，
> 报本文档修复后的那句"连接被拒绝"。分层取证后结论是**服务端只绑了回环**：
>
> | 观测 | 手段 | 结果 | 排除了什么 |
> |---|---|---|---|
> | 目标机在线、同子网 | `ping 192.168.100.239` | 通，TTL=64，<1ms | 网络不通 / 路由错误 |
> | 目标机别的服务可达 | `connect_ex(…,3306)` | rc=0，17ms | 整机防火墙封禁 |
> | 目标机 6379 | `connect_ex(…,6379)` | rc=10061，约 2s | 静默丢包（丢包应表现为超时） |
> | 应用本机 6379 | `netstat -ano \| findstr :6379` | 无输出 | "Redis 其实在应用本机" |
> | 服务端自看 6379 | 运维侧 `netstat -na \| grep 6379` | 仅 `127.0.0.1:6379` 与 `::1:6379` LISTEN | —— **即成因** |
>
> **"服务端 netstat 一切正常"与"外部连不上"并不矛盾**：`netstat` 只说"有一行在听"，
> 真正决定能否从别的机器连上的是那一行的**本地地址列**。只绑 `127.0.0.1` / `::1` 时，
> 服务端自己 `redis-cli` 完全可用，外部一律收到 RST ⇒ 「连接被拒绝」。
>
> 处置（在 192.168.100.239 上）：
>
> ```bash
> # /etc/redis/redis.conf
> bind 192.168.100.239 127.0.0.1     # 只放给应用所在网段；图省事可用 0.0.0.0 -::1
> requirepass <强密码>                # 放开监听就别裸奔：Redis 7 在"非回环监听 + 无密码"下
>                                    # 会以 protected-mode 拒绝外部连接（那时报的是服务端错误，
>                                    # 不再是"连接被拒绝"）
> sudo systemctl restart redis
> ss -lntp | grep 6379               # 应出现 192.168.100.239:6379 或 0.0.0.0:6379
> ```
>
> Docker 起的话对应 `-p 127.0.0.1:6379:6379` → 改成 `-p 192.168.100.239:6379:6379`
> （或 `-p 0.0.0.0:6379:6379`）后重建容器 —— 这种写法在服务端 `netstat` 里长得和
> redis.conf 只绑回环**一模一样**。
>
> 由此改进两处文案（都在"该给处置方向"的位置上，属本条第 5 条判据的直接应用）：
>
> - `redis_failure_reason()` 的"拒绝"分支：由"服务未启动或端口不对"扩成
>   "服务未启动 / 端口不对 / **只绑定了 127.0.0.1** 三者之一"，并直接给出下一步动作
>   （`ss -lntp | grep <port>` 看监听地址列）。原文案的致命处在于：用户看服务端
>   `netstat` 明明有监听，于是**排除了所有选项**、反而更迷茫。
> - Redis 卡片 ⓘ：前提条件加第 5 项（监听地址要放开给应用所在网段），notes 增加
>   "已放开 0.0.0.0 仍被拒"时的后续排查顺序（防火墙 → protected-mode）。

> **实测续记（2026-09-20）：监控页「降级」二字时有时无 —— `_apply_redis` 先清记录后探连通留下的空窗**
>
> 现象（用户报告）：缓存不可达时，运行监控页那一行"一会儿挂组件名、一会儿挂降级
> 徽章"，**「降级」二字有时整个消失**；除本条目已修的配置页三处外，这是第五处。
>
> 定位：`_apply_redis()` 开头是 `self._clear_degraded("redis")`，此后才 `make_client` +
> `ping`（预算 `REDIS_HEALTH_BUDGET_SEC`）—— 即"记录已经没了、旧实例还挂在容器上、
> 探测尚未出结论"的三不像中间态。这段窗口里监控页若刷新，快照里的 `c.degraded` 为空
> ⇒ 降级徽章与它的原因一起消失，只剩"该启用却没连上"的探测结论挂在**组件名**上；
> 下一轮 30s 后探测结束、记录重新登记 ⇒ 说明又跳回**徽章**上。事实自始至终没变过，
> 变的只是状态机的中间态。实测（`10.255.255.1` 触发连接超时）：探测窗口 **3.03~3.05s**，
> 期间 20ms 采样 95 次，记录缺失 95 次；而监控页默认 10s 刷新一次 ⇒ **约每三次刷新
> 就撞上一次**，与用户"时有时无"的观测频率一致。
>
> 修复（`rag/container.py`）：① 降级记录**探完才动**，成功才 `_clear_degraded`；
> ② 失败时不再每次重试都改写文案 —— 改为 `setdefault`，保留启动自检/运行期探测
> 写下的那句（它更早也更具体，如"…重启即失，请在配置页补齐连接信息"）；
> ③ 但"用户刚保存新配置"必须重写（旧文案里写着旧地址，照着它去查会查到不存在的
> 配置）⇒ `apply_section(section, reconfigured=False)` 显式传递这一位，配置保存路径
> （`api/runtime.py`）传 `True`，后台自愈传默认值。**同一段重建，两种来源对"旧原因
> 还算不算数"的判断不同，应由调用方交代，不该让下层去猜** —— 猜，正是中间态的产地。
>
> 验证（离线，走公开入口 `apply_section`）：后台重试 2 次各耗时 3.03s / 3.05s、采样
> 95 次，降级记录**缺失 0 次**、文案逐字不变；改成新地址后热应用 ⇒ 文案里的地址随之
> 变成新地址（旧地址不残留）；换回旧地址再热应用 ⇒ 再次跟随。界面侧（真实服务 +
> 浏览器）复测：15 个 ⓘ 无裁剪祖先、注册名列悬停即展开、移除焦点即收起。
>
> ⇒ 判据：**凡是"先清状态、再重建"的代码，都要问一句"清掉之后、重建出结论之前，
> 这个状态被谁读到了？"** 读它的人（这里是监控页/配置页）只会把中间态如实播出去；
> 界面出现自相矛盾时，先修状态机的中间态，而不是让界面去容忍它（同 TS-013 的假在线、
> TS-011 的分支优先级）。
>
> 同轮附带一处：**缓存行第二列是全表唯一一格 '—'**。根因不在渲染，在数据来源 ——
> 其余行都是"实例不在 → 回落到配置里的注册名"，而 Redis 这个槽位只有一个实现、
> `RedisConfig` 里根本没有 `adapter` 字段，`AdapterRegistry.name_of("redis", …)` 也不认它
> （redis 不是注册表里的适配器类型），两个来源同时取空。修法：监控快照对这一行固定报
> `redis`（它确实是外部依赖，不在 `_MONITOR_LOCAL_IMPLS` 里，徽章照常按真实实现上色）。
> 实测（真实服务 + Chrome，演示/真实两种模式各一遍）：11 行第二列**全部有注册名**、
> 缓存行与其它行同一套 `badge badge-default`、悬停即展开（内容分别落在
> "配置端点（当前未使用）：192.168.100.239:6379/0" 与 "端点：192.168.100.239:6379/0"，
> 后者说明本轮实测时 Redis 已按上面那份处置真的连上了）。

---

## TS-020 启动日志里的 RequestsDependencyWarning：requests 自带的依赖版本闸门被"只写下界"的 chardet 撞破

### 现象

- 进程运行中（首次导入 `pymilvus` / `tiktoken` 时）在 stderr 打出一条：

```text
D:\Python\Python310\lib\site-packages\requests\__init__.py:109: RequestsDependencyWarning:
urllib3 (2.7.0) or chardet (7.6.0)/charset_normalizer (2.1.1) doesn't match a supported version!
  warnings.warn(
```

- 功能没有任何异常：检索、探测、入库全部正常 ⇒ 极易被当成噪声长期忽略。

### 定位过程

1. **先看是谁在判、判的是什么**：读 `requests/__init__.py` 的 `check_compatibility()`，它只做两件事
   —— urllib3 要求 `major >= 1`（无上界）、以及 `(3, 0, 2) <= chardet < (6, 0, 0)`。
   本机 urllib3 2.7.0 过关，**唯一越界的是 chardet 7.6.0**。
2. **文案里列了三个库，实际只有一个不合格**：逻辑是 `if chardet_version: ... elif charset_normalizer_version:`，
   即**装了 chardet 就不再校验 charset_normalizer**。所以这句"urllib3 (…) or chardet (…)/charset_normalizer (…)"
   只是把所有版本拼在一起，并不表示三者都有问题（同 TS-011 的"一句话覆盖三种故障"）。
3. **确认触发者**：实测 `import pymilvus` 后 `'requests' in sys.modules == True`；另有 `tiktoken.read_file()`
   （仅在 BPE 词表未命中缓存、走 http 下载时执行 `import requests`）。而**本仓代码自身零处导入 requests**
   （全仓 grep 无命中）⇒ 这条告警只会出现在"别人的 import 时刻"，光看日志位置根本定位不到。
4. **核对版本窗口**：本机 requests 2.31.0 的 chardet 上界是 `< 6.0.0`，而 `requirements.txt` 只写
   `chardet>=5.2`（**无上界**）⇒ pip 装到 7.6.0 完全合法。逐版本核对 requests 源码：
   2.31.0 / 2.32.5 均为 `chardet < 6.0.0`，**2.33.1 / 2.34.2 放宽为 `< 8.0.0`** ⇒ 阈值就在 2.33。
5. **评估真实影响面**：requests 真正用到 chardet 的地方只有 `Response.apparent_encoding`
   （`chardet.detect(...)["encoding"]`），而 `chardet.detect` 在 7.6.0 上签名与返回结构未变
   （实测返回 `utf-8`）⇒ **这不是故障，是"版本窗口不匹配"信号**，但它混在启动日志里冒充错误。

### 修复方式

| # | 位置 | 修复 |
|---|---|---|
| ① | `requirements.txt` | 新增 `requests>=2.33,<3`：它不是直接调用方（经 pymilvus / tiktoken 间接引入），但"import 期自查并往 stderr 打告警"这件事让它必须被显式声明、并写明下界由来 |
| ② | `requirements.txt` | `chardet>=5.2` → `chardet>=5.2,<8`：上界取 requests 认可的窗口，避免将来装到 chardet 8 再复现同一条告警 |
| ③ | 运行环境 | `pip install -U "requests>=2.33,<3"` → requests 2.34.2（certifi 随之小幅更新） |

验证（用 `-W error` 把任何 warning 升级成异常，避免"看不见就当没有"）：

```text
[1] python -W error -c "import requests, pymilvus, tiktoken"                       PASS（无告警）
[2] requests 唯一用到 chardet 的路径 Response.apparent_encoding → utf-8             PASS
[3] 本仓 doc_parser 口径 chardet.detect(...).get("encoding") → utf-8               PASS
[4] 逐版本核对 requests 源码：2.31.0 / 2.32.5 = chardet<6；2.33.1 / 2.34.2 = chardet<8
```

> 注：升级时 pip 另报 `geoip2 4.6.0 requires urllib3<2.0.0,>=1.25.2`，与本项目无关
> —— 该库不在 `requirements.txt` 中，是同一个全局 Python 环境里其他工具装的，且该冲突在本条修复前就已存在。

### 根因分析

1. **依赖只写下界，等于替别人声明了"未来所有版本都兼容"。** 与 TS-016 同一条判据：`chardet>=5.2`
   让 pip 合法装到 7.6.0，而**同一环境里的 requests 只认 `< 6`**。冲突由第三方库在 import 期判定，
   本仓既不声明、也无从感知。⇒ 判据：**凡"另一个库会检查它"的依赖，上界要按那个库的窗口写，并注明依据**。
2. **第三方库的 import 期告警会伪装成本应用的缺陷。** 本仓零处导入 requests，告警却落在启动日志里；
   不追到 `import pymilvus` 这一步，就只能怀疑自己的代码。⇒ 判据：**告警/报错的归属由"谁触发了这次
   import"决定，而不是由它出现在哪个日志文件里决定**（同 TS-011 / TS-014 的"同一句话覆盖多种故障"）。
3. **"功能正常 + 文案吓人"是最容易被长期忽略的一类问题。** 它不阻塞任何流程，却会污染"启动日志应当干净"
   这一最基础的判据 —— 一旦红字常驻，真出问题时也就没人再看了。⇒ 判据：**应然基线（启动无告警）要能
   长期保持，噪声要顺手清掉，而不是习惯它。**

---

## TS-021 关机时 Milvus / Qdrant / ES 被静默跳过：`shutdown()` 与 `_close_quietly()` 用了两套关闭入口查找规则

### 现象

- 进程退出（停服务 / 冒烟脚本跑完）时 stderr 出现解释器兜底打印：

```text
python : Unclosed client session
client_session: <aiohttp.client.ClientSession object at 0x…>
Unclosed connector
connections: ['[(<aiohttp.client_proto.ResponseHandler object at 0x…>, 366744.546)]']
connector: <aiohttp.connector.TCPConnector object at 0x…>
```

- 只在 `fulltext=True`（配了 ES）时出现；服务功能不受影响，所以长期没被当成问题。

### 定位过程

1. **先排除"没走到 shutdown"**：冒烟脚本的 lifespan 已正常打印 `api_stopped` ⇒ `container.shutdown()`
   确实被调用了，问题在 shutdown **内部**。
2. **对比两条关闭路径**：热重建用的 `_close_quietly()` 是
   `getattr(inst, "aclose", None) or getattr(inst, "close", None)`（两种入口都找）；
   而 `shutdown()` 只写 `getattr(closer, "close", None)`。
3. **核对各适配器实际有哪些关闭入口**：`MilvusVectorStore.aclose` / `QdrantVectorStore.aclose` /
   `ESFulltextAdapter.aclose` 三个**只有 `aclose`**，`MySQLMetaStore.close` 只有 `close`
   ⇒ 旧 shutdown 只关掉了 MySQL，另外三个全部被跳过，且因为写的是 `if close:` 连一条日志都没有。
4. **发现文档与代码不一致**：`ESFulltextAdapter.aclose()` 的 docstring 明写
   "容器热重载 / 关机 / 一次性探测都会调用" —— 关机这条从未真正发生过。
5. **确认 aiohttp 的归属**：它由 `elasticsearch[async]` 引入（`elastic_transport` 用它做传输）、
   会话由 `AsyncElasticsearch` 持有，所以 `Unclosed client session` 只可能来自 ES 客户端没被 `close()`。

### 修复方式

| # | 位置 | 修复 |
|---|---|---|
| ① | `container.py.shutdown()` | 改为直接调用 `_close_quietly()`：与热重建**共用同一套查找规则**（先 `aclose` 再 `close`，None 直接跳过） |
| ② | `container.py.shutdown()` | 释放失败由静默 `except: pass` 改为 `_close_quietly()` 里的 `adapter_close_failed` 告警 —— 关机漏关本就是"安静发生"的故障，不发声就查不出来 |

验证：

```text
[1] 假适配器（只有 close / 只有 aclose / None 三种）→ shutdown 关闭序列 [1, 2, 3]
    两种入口都被调用、None 被跳过                                                   PASS
[2] scripts/smoke_startup_time.py 全流程 → Unclosed / connector / adapter_close_failed 命中数 0   PASS
[3] 启动耗时未受影响：import+app-factory 0.9s，startup(lifespan) 1.5s，total 2.4s     PASS
```

### 根因分析

1. **同一个"释放资源"的语义被两条路径各写一遍，规则必然漂移。** 热重建路径补上了 `aclose` 支持，
   关机路径没跟上；而适配器的关闭入口命名（`close` / `aclose`）本来就不统一。
   ⇒ 判据：**同一语义只允许有一处实现**（`_close_quietly`），其它调用点复用它；
   "按名字找方法"的代码尤其不能复制粘贴出第二份。
2. **静默兜底会把故障变成不可观测。** `if close:` 为假时既不关也不报，`except: pass` 又把失败吞掉
   ⇒ "关机漏关连接池"一路安静地活到进程退出，只剩解释器那句 `Unclosed client session` 无人认领。
   ⇒ 判据：**跳过与失败都要能发声**（同 TS-017 第 7 条：慢但成功也要自己发声）。
3. **文档写对了不等于代码对了。** `aclose()` 的 docstring 把"关机也会调用"写得斩钉截铁，
   但没有任何一处验证过 —— 描述行为的文字必须由一条可执行的判据兜底（本例是验证 [1]）。

---

## TS-022 换过向量模型后检索静默失准：同维不同源的向量住进同一个集合

### 现象

- 一台机器先以「无外部依赖」模式跑过（向量模型未配置 → 内置本地替身按哈希出**伪向量**，
  维度仍是配置里的 1024），之后配好真实向量模型（同为 1024 维）保存并热应用。
- 表现**完全不像故障**：
  - 保存成功、监控页全绿、`degraded` 为空、日志无一条 warning；
  - 问答不报错，向量检索路照常走，top_k 永远有结果；
  - 但答案开始变差，且**换个问法就不稳定**：同一件事有时答对，有时是一段毫不相关的内容；
  - 迷惑点在于正确文档其实就在库里（BM25 能检到），只是被向量路的噪声挤下去了。
- 另一种同样无感的触发：把 1024 维的 A 模型换成 1024 维的 B 模型（维度没变，库侧不报错）。
- 只在「改过 embedding 配置」的部署里出现；先配模型再入库的全新部署一切正常。

### 定位过程

1. **先按"检索链路配置问题"排查**：`enable_vector` / 各路权重 / top_k / RRF 融合 —— 全部符合预期，
   `lost` 路径为空。⇒ 不是"路没走对"，而是"路走对了、数据不对"。
2. **构造对照探针**：向同一个集合先写一批 A 来源的向量、再写一批 B 来源（不同模型、同维度）的向量，
   然后查询。观察到 ANN **无论数据多脏都一定返回 top_k** —— 检索器没有"这件事我做不了"的表达，
   失败以"结果很烂"的形式出现，而不是以异常出现。
3. **核对库侧有没有兜底**：`describe_collection` 的 dimension 与配置一致（1024），
   所以 Milvus 不会报 dimension mismatch —— **同维不同源在库侧完全无痕**；集合里也没有任何位置
   记录"这些向量是哪来的"。⇒ 判据的输入（来源）根本不存在，只能主动写进去。
4. **算一遍融合侧的影响**：RRF 只按名次计票、不看分数，于是"分数不可比"的噪声与正确答案拿到
   **同票**，靠名次或分数阈值都淘汰不掉。⇒ 只能在入口拦（不查 / 不写），出口没有可用判据。
5. **反查污染从哪进来**：入库写向量、自评 HyDE、二轮检索、一致性巡检的"自动修复"四处都会写/查向量，
   而它们都不知道"当前向量模型是什么"；`embedding` 段热应用只重建适配器，
   **不会回头看库里已有向量的来源**。

### 修复方式

| # | 位置 | 修复 |
|---|---|---|
| ① | `rag/vector_space.py`（新增） | 纯函数判据：`space_tag`（`v1\|实现\|模型\|维度\|real/local`）、`parse_tag`、`tag_label`（转人话）、`judge_space` → `(可写, 可检索, 原因)` |
| ② | `rag/adapters/base.py` | 向量库基类新增可选能力：`space_tag_supported`（默认 `False`）/ `read_space_tag` / `write_space_tag` / `collection_rows` —— 实现不了就如实说"无法校验"，不假装通过 |
| ③ | `rag/adapters/vector_store.py` | Milvus 实现：指纹落在 **collection properties**（建集合后仍可 `alter` 写入；`description` 只在建集合那一次能写），读取结果按物理集合名缓存，写成功即刷新 |
| ④ | `rag/container.py` | `sync_vector_space()` 算结论并缓存 + 空集合补打指纹；三个对外口径：`vector_write_blocked()`（`async`，写侧，把原因交回调用方）、`vector_space_ok()` / `vector_space_reason()`（**同步**读缓存 —— `enabled_paths()` 每次问答都会被调用，不能在那里 `await` 向量库）、`degraded["vector_space"]`（界面） |
| ⑤ | 五处调用点 | 入库写向量、检索 `_vector`、自评 HyDE、二轮检索、一致性巡检修复 —— 动手前**都问同一句** `vector_write_blocked` / `vector_read_ok`，不允许各自判断 |
| ⑥ | 启动自检 / 热应用后 | 同步重判（`sync_vector_space(stamp=True)`）并刷新 `degraded`；`vector_store` / `embedding` 段一换即作废旧结论（否则会拿上一套模型的结论去拦/去放行） |
| ⑦ | 监控页 / 配置页 | `readiness.retrieval.lost[].reason` 说明"为什么少查一路"；`degraded` 键名映射出「向量空间（库内向量与当前向量模型）」，并写明**这类记录不会随重连自动消失** |

验证（`tmp_vs_check.py`，34 项断言全通过）：

```text
[1] 纯函数判据（4 项）   指纹可解析 / 本地替身不记模型名 / 两个 mock 部署不受模型名影响
                        / 真实模型空模型名 ≠ 本地替身                              PASS
[2] judge_space 分支（6 项）空集合一律放行 / 同源放行 / 同维不同模型→拦
                        / 本地替身遇存量无指纹→拦 / 真实模型遇存量无指纹→放行+提示
                        / 拿不到行数→保守拦                                        PASS
[3] 载体读写（3 项）     空集合可写 / 空集合补打了当前指纹 / ensure_collection 先于打标  PASS
[4] 容器编排（7 项）     同源可写·可检索·degraded 干净 / 不同源停写·关向量路·登记
                        degraded / enabled_paths 不含 vector                       PASS
[5] 监控页（2 项）       lost 给出原因 / vector_store 自身不算掉线                    PASS
[6] 存量集合分档（5 项） 本地替身→停写且关路 / 真实模型→放行但提示重跑入库
                        / 提示不算降级记录 / 存量集合仍可检索 / 无载体实现放行        PASS
[7] 入库链路（4 项）     不同源时不写向量 / 记下跳过原因 / 不抛异常
                        / 同源时正常向量化                                        PASS
```

### 根因分析

1. **"向量可检索"这个判据少了一维：来源。** 一直把"库连着 + 模型能出向量"当成"向量可查"，
   而真正的前提是**同源**（同实现 + 同模型 + 同维度）。ANN 的契约是"一定返回 top_k"，
   所以缺这一维不会报错，只会答错。⇒ 判据必须包含"数据来自哪里"，不能只判"通道通不通"。
2. **出口没有可用的判据，防护只能在入口。** RRF 只按 rank 计票，会把"分数不可比"的噪声
   洗成与正确答案同票；靠分数阈值、靠重排都救不回来。⇒ 凡"融合阶段已无法辨别真假"的问题，
   必须在**写入前**判（同 TS-013 的思路：判据前置）。
3. **污染不可逆，所以宁停勿写。** 伪向量/异源向量一旦进库就与真向量不可区分，
   事后既检不出也删不干净（只能整集合重建）。⇒ 判据取"保守优先"：拿不准行数按有数据算，
   拿不准来源按不同源算。
4. **拦截不能伪装成失败。** 让适配器抛异常会被上层记成"写入失败"→ 文档状态变 PARTIAL、
   质量报告长期挂一条假故障。⇒ "有意跳过"必须是可区分的状态（`vector_write_blocked` 把原因
   交回调用方，写入阶段自然不碰向量库），这条链路上"没写"与"写失败"始终分得清（同 TS-017）。
5. **降级记录的键必须对应"能自动恢复的东西"。** 把"库里的向量不同源"记到 `vector_store` 名下，
   会触发后台每 30 秒重装一次向量库适配器 —— 永远修不好、永远在白试。⇒ 前提类问题用自己的键
   （`vector_space`），并在界面上如实说明"这类记录不会自动消失"。
6. **判据缺失不能算通过。** 载体不支持（Qdrant / pgvector 无 collection properties）时放行，
   但必须**发声**说明"无法校验"；同理，改造前写入的存量集合没有指纹时不能一刀切停掉
   （真实模型遇存量按既有行为放行 + 提示重跑入库，本地替身则必须停）。
   ⇒ **"通过"必须是判据成立，而不是判据查不到**（同 TS-013 的"在线 = 对象存在 且 无降级记录"）。

---

## TS-023 配置页「测试连接」拿掩码当密钥（假 401，模型列表空）+ 保存把凭据明文写回 / 把留空当清空

### 现象

- 配置页把已保存的凭据以 `******` 回填输入框；用户不动它直接点「测试连接」，请求就带着这 6 个星号发出去：
  - 模型端点回 401，界面表现是"地址能访问、却没有模型列表"（探测本身还判定为"端点可达"，因为 401 < 500）；
  - MySQL / Redis / MinIO / ES 同理：拿星号或用空口令去连，报出来的是一次**假的**"认证失败"。
- 更严重且**不可逆**的是保存：用户只是想改同段里的别的字段（例如 `base_url`），回传的 `******` 会被
  当成新值写进 YAML —— 真凭据被 6 个星号永久覆盖，页面上再也看不到、也恢复不了。
- 与之相对的另一半：凭据字段回填空串时，若被当成"清空"，同样会把真凭据抹掉；
  而空串本来是有意义的合法值（清空 MySQL 密码 = 改成匿名连接）。
- 同时暴露的存量问题：`customer/customer_config.yaml` 里的 `api_key` / `password` / `secret_key`
  一直**明文落盘**，而这份文件会被贴进工单、提交进私有仓库、打包进交付物。

### 定位过程

1. **追「测试连接」的取值链路**：前端 `config-ui.js` 读 `apiKeyRow.value` → `ConfigData.testConnection`
   → 后端 `health_test`；而这个输入框的初值来自 `_param_row` 里的 `_redact` 输出 —— 也就是说
   **输入框里放的就是掩码本身**。⇒ 掩码同时被当成"显示占位"和"待提交的真值"。
2. **追保存链路**：`_yaml_update_from_payload` → `_deep_merge(raw, update)` → `yaml.safe_dump` 直接写盘；
   `_coerce_param` 对 `"******"` 不做区分，于是星号顺利进入 `update`。⇒ 同一份 `_redact` 产物
   既供"显示"又供"编辑/回传"，语义被混用，两条链路各自出错。
3. **全仓搜写盘点**：`yaml.safe_dump` 只有 `loader.ConfigLoader.save` 与 `routes.save_config` 两处
   （前者当时没有任何调用方），可判定加密/解密必须落在这两处，而不能在读侧打补丁。
4. **追配置读取**：`load_config` 把 YAML 解析结果直接交给适配器；内存中必须保持明文，
   否则所有模型/数据库/缓存适配器都要跟着改。⇒ 明文/密文的转换只能发生在**磁盘边界**。
5. **用探针验证"留空"的语义**：`_coerce_param` 对空串返回 `""`（有意保留的有效值），对掩码返回 `None`；
   凭据字段若不区分二者，探测会拿空口令去建连，把"其实连得上"报成"认证失败" ——
   与星号问题一样是假信号，只是更难看出来源。

### 修复方式

| # | 位置 | 修复 |
|---|---|---|
| ① | `rag/config/secrets.py`（新增） | Fernet 加密原语；主密钥来源 `RAG_SECRET_KEY` > `<配置目录>/.secrets.key`（首次自动生成、`chmod 0600`）；精确匹配的凭据名单 `SENSITIVE_KEYS`；`is_untouched()` 把 `None`/空串/掩码归一为"未修改"；`encrypt_tree` / `decrypt_tree`（幂等；解不开的字段置空并记 `config_secret_undecryptable`） |
| ② | `rag/config/loader.py` | 新增 `merge_config_file()` 作为**唯一**写盘入口：读盘 → `decrypt_tree` → 归一段名 → 合并 → `_restore_sensitive` → `encrypt_tree` 写盘，返回明文 `merged` 供热应用；`load_config` 在环境变量插值前先解密；`_SENSITIVE_FIELDS = SENSITIVE_KEYS \| {"access_key"}` |
| ③ | `rag/web/routes.py` | `save_config` 改为调用 `merge_config_file`（不再自己 `safe_dump`）；`_param_row` 凭据字段回传 `value=""` + `hasValue` 标记；`_blank_secret_masks` 把下发 JSON 里的掩码换成空串；`_redact` 与 `SENSITIVE_KEYS` 对齐（补上 `token` / `dsn`）；新增 `_form_scalar_overrides()` 统一 5 个探测函数对"留空/掩码"的处理；`health_test` 在凭据留空时回落已保存凭据；顺带修掉 `_probe_mysql_with_form` 两处 3 元组返回（`-, -, False` 会让调用方 2 元组解包崩溃） |
| ④ | `rag/web/static/js/config-ui.js` | 凭据输入框按 `hasValue` 显示占位「已保存（留空表示不修改）」/「未配置（填写后保存）」，让"留空"有明确含义 |
| ⑤ | `.gitignore` / `requirements.txt` | 忽略 `.secrets.key`；显式声明 `cryptography>=42.0`（此前只是 pymilvus 等的间接依赖，直接调用方必须自己声明） |

验证（临时自检脚本，5 组断言全通过）：

```text
[1] 载入解密 -> 内存明文                                            PASS
[2] 加密落盘 + 留空/掩码不覆盖 + 不误伤非凭据
    （max_tokens 保持明文、access_key 不加密、${HOOK:-default} 原样保留）
    （内存 merged 仍是明文，热应用不受影响）                          PASS
[3] 回读再次解密                                                    PASS
[4] 加密幂等（已加密值不会被二次加密）                              PASS
[5] 密钥不匹配 -> 字段置空、其余字段照常（降级链路）                PASS
```

### 根因分析

1. **"掩码"被同时当作显示值与真值使用。** `_redact` 的产物既直接渲染进输入框，又作为表单回传的
   初值参与写盘 —— 一个字符串承担两种语义，必然在某条链路上出错（显示尚可，回传必错）。
   ⇒ 显示占位必须与"可提交的值"分开：凭据字段在界面上**没有**真值可回填，只给一个 `hasValue` 标记。
2. **"未修改"这个状态没有被表达。** 表单只能表达"有值 / 无值"，无法表达"这项我没动"；
   回填 `******` 与回填空串，其实是同一个"未修改"的两种伪装写法。⇒ `is_untouched()` 把
   `None`/空串/掩码归一成一种状态，是这次修复的地基（同 TS-017/TS-019：先有可判定的状态，再谈行为）。
3. **凭据的"落盘形态"与"使用形态"必须解耦，且只能在磁盘边界转换。** 适配器要明文、磁盘要密文；
   转换一旦散落在读侧或写侧就会漏 —— 本次正好漏在 `routes.save_config` 这条独立写盘链路上。
   ⇒ 写盘收敛到 `merge_config_file` 一个入口、读盘收敛到 `load_config`，其余代码对"存的是什么"无感。
4. **"解不开"不能静默降级成"密文当明文"。** 密钥跟着机器走（`RAG_SECRET_KEY` / `.secrets.key`），
   换机器、丢密钥是常态。把 `enc:v1:...` 原样交给适配器，换来的是"用错密钥去请求"的远程 401，
   比"根本没配"更难排查。⇒ 解不开即置空，复用既有的"缺配置 → 降级"链路（同 TS-009/TS-016：
   不要用假值去换一次远程错误）。
5. **同一份"敏感字段名单"曾存在多份拷贝。** 加密、界面打码、保存时留空不覆盖原先各有一份近似名单，
   随时会漂移（会出现"落了盘的字段界面却明文回显"）。⇒ 三者收敛到 `SENSITIVE_KEYS` 一处，
   并明确排除两个易误伤的项：`access_key`（身份标识而非密文，MinIO/S3 控制台本就明文展示）与
   `sensitive_fields`（它是"哪些列要打码"的名字清单，本身不是凭据，标成密码框会让它无法编辑）。
6. **顺带暴露的既有缺陷：探测函数的返回值没有统一契约。** `_probe_mysql_with_form` 两处返回 3 元组、
   其余同类函数返回 2 元组，而调用方按 2 元组解包 —— 一条"参数非法"的输入就能把它变成 `ValueError`，
   而不是一次可读的失败原因。⇒ 契约统一为 `(bool, str)`，失败原因始终能落到界面上。

---

## 附录

### 优化前后指标对比

端到端实测（`scripts/smoke_startup_time.py`，外部依赖全部不可达的最坏情况）：

| 阶段 | 优化前 | 优化后 | 说明 |
|---|---|---|---|
| 模块导入 + 应用工厂 | ~1.0s | 0.9s | 无实质变化 |
| 适配器构造 | **~33s** | **4.5s** | MinIO ~30s + Milvus ~3s **串行** → 并行 + MinIO 关重试 |
| 健康检查 | **~41s** | **3.1s** | Redis 36.5s + MySQL ~5s **串行** → `asyncio.gather` 并行；Redis 关默认重试后降至 ~3s |
| **合计** | **74s** | **8.5s** | 降幅约 88% |

> 注：`knowledge_graph`、`business_data` 默认未启用，不参与上表；
> 其超时亦已在本次一并收紧（见 TS-003），确保将来启用时不会重现同类卡顿。

优化后各阶段（`smoke_startup_time.py` 实测）：`import+app-factory: 0.9s` / `startup(lifespan): 7.7s` / `total: 8.5s`。

剩余耗时均为**外部条件决定的硬下限**：MinIO 2s + Milvus 3s（构造并行，取 3s 量级）、MySQL 3s（健康检查，
`connect_timeout=3`）。依赖可达时这些探测都是毫秒级。

### 相关脚本

| 脚本 | 状态 | 用途 |
|---|---|---|
| `scripts/smoke_startup_time.py` | 保留 | 启动耗时回归守卫（断言 < 15s） |
| `scripts/smoke_ui.py` / `scripts/smoke_login.py` | 保留 | UI 与登录回归 |
| `smoke_probe.py` / `smoke_check_timing.py` / `smoke_redis_iso.py` / `smoke_startup_stages.py` | 已删除 | 本次专用的临时分解/复现脚本 |
| `scripts/_tmp_ui_probe2.py` | 已删除 | 配置页重排链路端到端探针（Playwright，17 项断言） |
| `scripts/_tmp_ce_cache_test.py` | 已删除 | CrossEncoder 缓存复用 / LRU / 并发单飞 验证（注入假模型） |
| `scripts/_tmp_api_test.py` / `_tmp_replay.py` / `_tmp_mock_models.py` / `_tmp_dbg.py` / `_tmp_body.json` | 已删除 | 配置接口定位期的临时脚本与浏览器载荷转储 |
| `scripts/_tmp_mysql_probe.py` | 已删除 | 从应用所在机直连 MySQL，分离「TCP/握手/认证/目标库」四层，取得 errno 1130 |
| `scripts/_tmp_health_probe.py` | 已删除 | 「测试连接」接口验证：errno 语义化映射 + 表单值直连探测（18 项断言） |
| `scripts/_tmp_ui_mysql_probe.py` | 已删除 | 配置页「服务」tab 测试连接端到端探针（Playwright，10 项断言，含 YAML 未被改写校验） |
| `scripts/_tmp_section_apply_probe.py` | 已删除 | 单段热应用验证：只重建本段适配器，不动其它实例 / Redis / 后台任务（12 项断言） |
| `scripts/_tmp_browser_save_probe.py` | 已删除 | 配置页保存提示端到端探针（Playwright，12 项断言，临时配置副本 + 独立实例） |
| `scripts/_tmp_mysql_cold_probe.py` | 已删除 | MySQL 冷/热建连耗时分层计时（裸 TCP / 引擎 / 适配器 + 预算对照） |
| `scripts/_tmp_probe_budget_verify.py` | 已删除 | 探测与自检预算一致性验证（5 项断言，含真实容器 `apply_section` 路径） |
| `scripts/_tmp_mysql_dns_diag.py` | 已删除 | 建连分层计时（TCP / 首字节）+ 服务端 `skip_name_resolve` 现值查询 |
| `scripts/_tmp_mysql_pool_verify.py` | 已删除 | 探测与保存共享同一引擎验证（冷探测 10s → 保存 0.00s、空闲连接存活、参数变更不误用） |
| `scripts/_tmp_es_health_probe.py` | 已删除 | 全文检索自检验证（离线假客户端）：真实构造路径 / 缺 IK 插件 / 异常语义化 / 探测预算与不重试（16 项断言） |
| `scripts/_tmp_es_live_probe.py` | 已删除 | 降级到 8.x 客户端后的**真实 ES** 全链路验证（版本自检 / `options()` 可用性 / health_probe / 建索引含 IK mapping / 写入 / BM25 / 精确检索 / 按文档删除，9 项断言） |
| `scripts/_tmp_es_form_probe_verify.py` | 已删除 | 大版本 400 语义化 / 不再误报缺 IK 插件 / 表单值直连探测 / 地址缺协议提示 / `aclose()` 释放连接池（15 项断言，含真实 ES 8.19.10） |
| （TS-017 无落盘脚本） | — | 向量库改动以**内联 `python -c` 探针**验证：命名/凭据校验行为、配置默认值、三 adapter 接口自省、模块导入 |
| （TS-018 无落盘脚本） | — | 配置页载荷以**内联 `python -c` 探针**验证（`_service_params` / `_param_row` 直调）：分组顺序、optional 标记的分组粒度、按 adapter 隐藏的 `timeout`、`py_compile` |
| `tmp_verify_redis.py` | 已删除 | TS-019 验证（26 项断言）：工厂口径（超时/重试/空密码）、八类原因翻译（含 Windows 本地化文本）、`probe` 真实建连与 `aclose`、表单值优先、`model_validate` 校验、分组可见参数与标签、`health_test` 分支顺序 |
| `tmp_vs_check.py`（仓库根） | 已删除 | TS-022 验证（34 项断言）：指纹纯函数与 `tag_label`、`judge_space` 六个分支、Milvus `collection properties` 读/写/补打、容器三口径与 `degraded` 登记、`enabled_paths` 摘掉向量路、监控页 `lost` 原因、存量集合两档处置、入库步骤"有意跳过" |

### 经验总结

1. **启动路径上，凡外部依赖的构造与探测，一律显式设置短超时**，不要依赖库默认值（常见默认值 30s 起）。
2. **"一次性探测/构造"路径不要重试**（`total=0` / `retries=0`）；
   重试是业务请求路径的优化手段，不是启动自检的手段。
3. **无依赖关系的初始化一律并行**：最坏耗时从"求和"降为"取最大"，收益随依赖数量线性放大。
4. **`asyncio.wait_for` 不是万能兜底**：它只能取消任务，管不住吞掉取消的第三方重试循环；
   真正的边界来自被调用方自身的"次数上限 × 单次超时上限"。
5. **看异常类型辨真伪**：外层 `wait_for` 生效时应抛 `asyncio.TimeoutError`；
   若抛的是库自身的超时异常，说明外层计时器被绕过。
6. **降级即终止**：只要失败后存在确定性降级，就不要重试——重试只会推迟降级并拖慢启动。
7. **静默丢包比拒绝更危险**：防火墙 DROP 会把"毫秒级失败"伪装成"30 秒卡顿"，
   排查时务必先确认失败形态（`ConnectionRefusedError` vs `TimeoutError`）。
8. **多数据源写入必须显式定优先级**：同一配置项若同时被"结构化控件"与"自由 JSON 编辑器"写入，
   优先级应由后端兜底（结构化 > 快照 JSON），不能依赖前端自律；
   且 **`200` + `updatedKeys` 正常无法证明"写入的是期望值"**，必须落到文件内容 diff（见 TS-007）。
9. **缓存作用域要与资源生命周期匹配**：重资源（模型/连接池）应缓存在比"请求级对象"更长寿的层级；
   key 需覆盖热切换维度（模型名 + 设备）；加载要用锁做**单飞**，并移出事件循环（见 TS-008）。
10. **降级必须配可观测性**："资源存在"不等于"资源可用"——文件校验、路径校验通不过运行时的依赖/加载检查，
    静默降级会制造"配置全绿但功能未启用"的假象（见 TS-009）；
    权重格式等加载细节由**库的默认优先级**决定，读库行为而非读业务代码（见 TS-010）。
11. **异常要翻译，不要吞掉**：`except: return False` 会把带明确语义的错误码（如 MySQL 的
    1130/1049/1045/1044/2003，恰好覆盖授权/建库/认证/权限/网络五类**用户可自行处置**的故障）
    降维成不可操作的布尔；健康检查应返回 `(bool, 原因)`（见 TS-011）。
12. **"测试连接"必须对用户当前输入求值**：测已保存配置的运行实例，会把"填了没保存"误判成"配置有问题"；
    若无法直连（部分服务不支持），必须显式声明作用域，避免用户对着一份**影子配置**排查。
13. **验证要可证伪**：只验证"改完能成功"无法区分"真的用了新值"还是"旧值恰好也可用"——
    应构造对照并让结论**双向翻转**（见 TS-011 的 ①②③）。
14. **配置热更新的粒度应与"配置段 ↔ 适配器"的归属对齐**：独占适配器的段只需重建那一个适配器；
    无独立适配器的段（检索策略/权限/编排）才回退全量重建。
    缓存失效接口的粒度（`drop` vs `clear_cache`）直接决定上层能实现的更新粒度（见 TS-012）。
15. **状态反馈必须与动作作用域对齐**：热更新返回的 `degraded` 只应包含本次涉及的组件，
    否则"保存 A 却弹出 B 降级"会被读成"我把 B 改坏了"；
    且回退路径（`scope=container`）与单段路径两类结果都要有探针覆盖（见 TS-012）。
16. **换了主引用，还要换衍生的快照引用**：被构造期快照走的资源（`ProgressBus(self.redis)`、
    `mem._prefix`）会在替换后仍指向旧实例，让"配置已生效"变成假象；
    同时"在线"必须满足**对象存在 且 无降级记录**（降级替身也是合法对象，见 TS-013）。
17. **探测口径必须与运行口径统一**：「测试连接」与运行期自检若各用各的超时，就会出现
    "测得过、存不下"（或反过来），且两条结论都"有据可查"，极难排查；
    预算应收敛到**同一个代码常量**（见 TS-014），而不是散落的魔法数字。
    配套判据：**先量出耗时构成再设超时** —— `connect_timeout` 只管 TCP，
    握手慢只能靠外层预算覆盖，一味调小反而把可用服务写成降级。
18. **"问当前状态"的链路必须真实访问，不能复用缓存连接**：把「测试连接」接到一条
    早就建好的空闲连接上，它就会退化成"回放旧结果" —— 服务端刚改配置、刚宕机，
    页面都可能报"正常"（TS-014 曾引入建连复用池，后因此整体撤回）。
    判据：**只有"复用不影响结论正确性"的资源才值得缓存。**
19. **为绕开故障而引入的机制，故障修好后要主动回收**：复用池成立的前提是
    "服务端每次冷建连都要 10s"，而这个前提本身就是故障而非设计约束；
    服务端修正后，它只剩成本（掩盖实时状态、跨事件循环的复杂度，TS-014）。
20. **"合法地慢"要自己发声**：有预算兜底后，10s 属于成功路径，不产生任何失败日志，
    用户只能干等且无从判断该不该查服务端。给慢路径单独告警并写清成因
    （`mysql_slow_connect` 直接点出名称解析），比事后加日志便宜得多。
21. **服务端参数"改了没生效"，先确认生效域与生效时机**：`skip_name_resolve` 写进
   `[mysql]`（客户端程序段）对服务端毫无作用，服务端只认 `[mysqld]`；
   `SET GLOBAL` 只对新连接生效且重启即失效。改完先验证 `SELECT @@...`，
   再去怀疑客户端代码。
22. **"构造失败"与"未配置"是两种故障，不能共用一句提示**：`except Exception` 吞掉构造异常后
   只剩一条日志，对外报"适配器未实例化"，会把用户推去查配置项，而真因在代码里的导入路径
   （`elasticsearch.asyncio` 子模块在 8.x/9.x 里并不存在，见 TS-015）。
   判据：**降级提示必须携带原始异常**。
23. **"探得通"不等于"用得了"：健康检查的覆盖面要与业务依赖对齐**：ES 的 `GET /` 不覆盖
   IK 分词插件，缺插件时"能连上"与"建不了索引"同时成立 ⇒ 自检必须用**与业务完全相同的参数**
   验证那些"业务失败时会踩到的前置条件"（同一个分词器名），而不是最容易探的那一项（TS-015）。
24. **探测路径不重试，业务路径才重试**：重试会把"4s 该给出的结论"拖成数十秒，并吞掉外层
   `wait_for` 的取消（同 TS-006）；探测一律 `max_retries=0`，由预算兜底并报"探测超时"（TS-015）。
25. **参数的"必填/选填"要对齐服务端的真实契约**：ES 未开安全认证时用户名与密码都该留空，
   前端只放行 `password` 就会逼用户给不存在的账号填值（TS-015）；
   反过来，"跨团队前置条件"（如 IK 插件）只有应用知道，要写在**配置页上**而不是日志里。
26. **与外部服务同生命周期的客户端依赖要写区间**：`elasticsearch>=8.13` 会在几个月后
   解析成 9.4.1，而 ES 客户端/服务端同大版本绑定（9.x 客户端发 `compatible-with=9`，
   8.x 服务端只认 8/7，于是每个请求都被 400 拒）。判据：**写 `>=x,<x+1` 并在注释里
   写明理由**（TS-016）；`pymilvus` / `neo4j` 是同类风险项。
27. **"我改了却报旧值"，先确认探测打的是哪份配置**：`hosts` 改成新地址但没保存时，
   「测试连接」打的是已保存的旧地址 —— 用户拿到的观测与他的改动无关
   （TS-011 / TS-014 / TS-016）。判据：**探测类接口一律用"用户此刻提交的值"**。
28. **报错要说"实际观察到了什么"（耗时/状态码/响应体），再谈推论**：
   "4s 无响应、防火墙丢包"讲得再具体，若真实结局是"0.0x 秒被 400 拒"，
   这条详细的文案只会**替用户排除掉正确方向**（TS-016）。
29. **归因必须有排他性证据，否则不下结论**：把任何异常都判成"缺 IK 插件"，
   会让人去装一个没问题的插件、且装完还报同样的错；只有报错正文里出现
   `ik_max_word` 才判"缺插件"，其余回落到通用翻译（TS-015 / TS-016）。
30. **凡持有连接/线程/句柄的适配器都要有可被发现的关闭入口**：容器按
   `aclose`/`close` 查找、找不到就静默跳过，于是每次热重建都泄漏一个连接池，
   `shutdown()` 也释放不了。判据：**关闭入口要有测试证明它真的被调用**（TS-016）。
31. **多实现共用一个配置段时，配置项语义要收敛到同一份模块级校验**：`vector_store`
   用 `adapter` 切 milvus / qdrant / pgvector，而三者各写各的集合命名（有的加
   `collection_prefix`、有的不加），切 adapter 后表现为"数据消失"；
   且非法名字（中文 / 短横线）要等到建集合才炸，而那条路径会把**整条向量路**关掉
   ⇒ 校验点越靠近入口越好（TS-017）。
32. **指向全局单例的资源，重建前必须先摘旧再建新**：pymilvus 的 alias 是进程级全局的，
   同 alias 换地址直接抛 `ConnectionConfigException`，而代码从不 `disconnect`
   ⇒ 改地址保存一次即把向量路钉死，且**再保存也恢复不了**（只能重启进程）。
   判据：**按配置签名判断复用还是重建，重建前显式摘旧**（TS-017）。
33. **"探测"与"运行"必须是两套实例、两套连接**：alias 全局（探测会摘掉运行连接）、
   `AdapterRegistry.create()` 命中旧配置缓存（测的是旧地址）、提前关闭会打断在途请求，
   三处独立原因指向同一结论；探测实例用完即弃（`probe()` + `aclose()`）（TS-017）。
34. **状态复述必须排在实时探测之后**：`health_test` 里若让"适配器为 None"这类缓存结论
   优先返回，那么向量路一旦被关闭，用户改对了地址也永远测不过 —— 而保存又被"测试通过"
   gate 住，形成**改不对 / 测不了 / 存不了**的死锁（TS-017，TS-016 第 3 组的加强版）。
35. **能提前判定的配置错误，一律提前拒绝**：pymilvus 只在用户名与密码**都非空**时才发认证头，
   只填一个会被静默当成匿名连接，出错时只回一句 `code=2 illegal connection params
   or server unavailable`（同时覆盖地址 / 服务 / 认证三类故障）⇒ 半套凭据应在入口拒绝，
   库的笼统错误要按**处置方向**翻译（TS-017 / TS-011）。
36. **界面标记的粒度要跟规则的粒度对齐**：规则是"用户名与密码成对留空"，标记却按单个
   参数键全局给（`{"password", "username"}`），而向量库的字段叫 `user` ⇒ 界面上只有密码
   标着 optional，读起来正好与规则相反。同时提醒：**同一个键在不同分组下语义可能不同**
   （MySQL 的 `user` 必填、向量库的 `user` 可留空），所以这类标记要按分组追加，
   不能直接往全局集合里塞键（TS-018）。
37. **参数可见性应由"当前 adapter 是否真的读它"决定**，而不是"配置段里恰好有这个字段"：
   `timeout` 只有 qdrant 读（httpx 客户端），却出现在 milvus 的卡片里，用户便把它
   10 → 60 反复试 —— 一条根本不生效的线索比没有线索更贵（TS-018 / TS-014 / TS-015）。
38. **刻意制造的不对称（页面没有、配置里有）必须写清范围与原因**：产品裁剪若只做在
   入口层，字段、容器初始化、检索路径都还在，"漏配"与"有意为之"从现象上无法区分；
   代码注释与文档要同时说明"关到哪一层、为什么"（TS-018 / TS-017 第 1 条）。
39. **建连口径必须收敛到单一工厂**：Redis 的客户端参数被手写在三处、探测链路却根本
   没建连，于是"页面测得过、保存说不可用"（TS-019 / TS-014 / TS-015）。
   判据：**一个外部依赖只有一个建连入口 + 一组模块级超时常量**，谁要客户端都来调它。
40. **按 `kind` 分派的分支，顺序就是优先级，改一处必须成组核对**：`storage` / `vector`
   都已改成"实时探测在前、状态复述在后"，Redis 因"不在上次需求范围"漏掉，同一函数里
   并存三套优先级 ⇒ 表现为"缺一个 elif"式的长期潜伏缺陷（TS-019 / TS-017 第 34 条）。
   判据：**改分派逻辑时，把 `_SERVICE_SECTIONS` 逐项对照一遍**。
41. **异常翻译的关键词会随环境变，"兜底分支"长得像正常提示最难发现**：Windows 套接字
   错误文本按系统语言本地化（实测 `…远程计算机拒绝网络连接。` 不含任何英文关键词），
   只写英文关键词的翻译表在中文环境等于没写（TS-019）。
   判据：**按性质维度（认证/编号/网络/超时）穷举、关键词中英双写、始终附异常原文；
   验证要断言"命中了预期分支"，而不是"有文案返回"**。
42. **卡片上的每个参数都要能回答"它影响什么"**：`session_ttl_hours` 与连接无关却和
   host/port 并排（用户唯一会去拧的旋钮，拧小反而更坏），`db` / `prefix` 的作用域又
   没写清；同 TS-018 的"露出对它无效的超时"。
   判据：**答不上来的参数不进连接卡片；有作用域的（默认值/命名空间/运行期口径）
   在标签或 ⓘ 里写明"写入配置文件后即被后续读写使用"这类关键语义。**
43. **"服务在跑"与"外部连得上"是两个独立事实，判据是监听地址列而不是"有没有这一行"**：
   Redis 只绑 `127.0.0.1` / `::1` 时，服务端 `netstat`/`redis-cli` 一切正常，外部一律
   收到 RST ⇒「连接被拒绝」（TS-019 实测续记）。所以"服务未启动或端口不对"这类
   二选一的文案会**替用户排除掉正确方向**（他会说"服务明明起着、端口也没错"，
   然后无路可走）。⇒ 判据：**拒绝类文案要覆盖"只绑回环"这一成因，并直接给出
   `ss -lntp | grep <port>` 这个能一眼看穿的动作**；Docker 的 `-p 127.0.0.1:6379:6379`
   与 redis.conf 只绑回环在服务端看起来完全相同，排查时不要按"是不是容器"分流。

### 适配器命名体系：组件名 ↔ 槽位 ↔ 注册名

> 本节是一份**速查参考**（不是故障复盘）。项目里所有"名字对不上"的故障都源于这一层：
> 注册名一度叫 `mysql_meta`、监控页 badge 显示的是 `openai_compatible`、配置页标题写着
> `(MySQL)` 而实际连的是别的后端 —— 同一个组件身上有三层名字，混用就会自相矛盾。

#### 1. 三层名字，各回答一个问题

| 名字 | 代码里的存在形式 | 回答的问题 | 谁在用 |
|---|---|---|---|
| **组件名** | 界面中文名：配置页 `_MODEL_SECTIONS` / `_SERVICE_SECTIONS`，监控页 `_monitor_service_specs`（`rag/web/routes.py`） | "用户看到的这是什么能力" | 模板 / `monitor.js`，**仅用于展示** |
| **槽位**（`adapter_type`） | `_ADAPTER_TYPES` 的键，共 11 个（`rag/adapters/registry.py:17-32`） | "实现的是哪一种**能力契约**"（对应 `rag/adapters/base.py` 里的 ABC） | 注册表键；`create` / `get_class` / `drop` / `name_of` 的第一个参数；`_CORE_FALLBACKS`；`SECTION_ADAPTERS` |
| **注册名**（`name`） | `_classes[槽位][注册名] = 实现类`（`registry.py:42`） | "是这个能力的**哪一份实现**" | YAML 里该段的 `adapter:` 取值、监控页 badge、热重建的 `drop` |
| *（附加）容器属性* | `c.llm` / `c.vector` / `c.graph` / `c.business` / `c.parsers` … | "上层此刻该读哪个对象" | 所有 routes / pipeline / services |
| *（附加）配置段名* | YAML 顶层键 | "用户能编辑哪一段" | `AppConfig` 字段名、`SECTION_ADAPTERS`、配置页载荷 |

**为什么三者不能合并**：注册表的数据结构是 `{槽位: {注册名: 类}}`。如果注册名直接复述槽位名
（槽位 `embedding` → 注册名也叫 `embedding`），这个内层字典就永远只可能有一个 key ——
一个槽位一份实现，"可插拔"退化成"写死"。而 `vector_store` 下要同时放 `milvus/qdrant/pgvector`、
`meta` 下要放 `mysql/memory`、`fulltext` 下要放 `elasticsearch/opensearch`，
这份"多实现"能力**只能由注册名承担**。

历史上确实踩过这个坑：元数据库的**段名、槽位名、注册名一度都叫 `mysql_meta`**，后果是监控页
badge（显示注册名）只能写出 `mysql_meta` —— 读者既看不出后端是哪个数据库，也看不出它是不是
本地替身（见 `registry.py:22-24` 与 `rag/config/models.py:19-22`）。现统一为：配置段 `meta`、
槽位 `meta`、注册名 `mysql`。

> 判据：**槽位名回答"是什么"，注册名回答"是哪一份"，两者都不许复述对方。**

#### 2. 注册：三种方式，合计 32 个注册名

| 方式 | 数量 | 写法 | 位置 |
|---|---|---|---|
| 装饰器 | 21 | `@AdapterRegistry.register("槽位", "注册名")` | `registry.py:45-53` |
| 解析器专用 | 8 | `@AdapterRegistry.register_parser("pdf")`（等价于 `register("doc_parser", ...)`） | `registry.py:55-58` |
| 别名直写 | 3 | `AdapterRegistry._classes["槽位"]["别名"] = 同一个类` | `llm.py:105`、`embedding.py:79`、`fulltext.py:481` |

装饰器注册点清单（改注册名时按这张表逐个核对）：

| 槽位 | 注册名 → 文件:行 |
|---|---|
| `llm` | `openai_compatible` → `llm.py:24`；`mock` → `mocks.py:37` |
| `embedding` | `http_embedding` → `embedding.py:19`；`mock` → `mocks.py:100` |
| `meta` | `mysql` → `meta_mysql.py:274`；`memory` → `meta_memory.py:23` |
| `vector_store` | `milvus` → `vector_store.py:240`；`qdrant` → `549`；`pgvector` → `689` |
| `fulltext` | `elasticsearch` → `fulltext.py:152` |
| `knowledge_graph` | `neo4j` → `knowledge_graph.py:17`；`nebula` → `117` |
| `business_data` | `sqlalchemy` → `business_data.py:23` |
| `synonym` | `file_based` → `synonym.py:21`；`http_service` → `77`；`none` → `mocks.py:137` |
| `auth` | `jwt` → `auth.py:35`；`dev` → `87`；`oidc` → `113` |
| `storage` | `minio` → `storage.py:30`；`local_fs` → `storage.py:97` |
| `doc_parser` | `pdf` → `doc_parser.py:64`；`docx` → `255`；`xlsx` → `298`；`pptx` → `392`；`markdown` → `448`；`txt` → `500`；`html` → `528`；`image` → `581` |

三个别名都是"协议合并"的产物，与同行的主名**指向同一个类**（不是新实现）：

| 别名 | 主名 | 为什么能合 |
|---|---|---|
| `vllm` | `openai_compatible` | 自建 vLLM 也讲 OpenAI `/chat/completions` 方言 |
| `openai_embedding` | `http_embedding` | 自建向量服务（TEI / vLLM 起的 bge 系列）也讲 OpenAI `/embeddings` 方言 |
| `opensearch` | `elasticsearch` | OpenSearch 复用 ES 的 `_search` / `_bulk` 协议实现 |

**按需加载**：`_load_builtin_implementations()`（`registry.py:131-137`）预载 11 个模块；
`mocks.py`（`mock`×2、`none`）与 `meta_memory.py`（`memory`）**不在预载列表**，
只在容器降级 / `--noconnection` 路径按需 import 时才入库。由此产生一个可观测的副作用：
健康运行时 `list_implementations("meta") == ["mysql"]`，降级过一次之后才是 `["memory", "mysql"]`。
这不是缺陷 —— 降级替身本来就是"用过之后才存在"的实现。

#### 3. 实例化与反查：五个入口，用途各不相同

| 入口 | 是否进单例缓存 | 用途与注意点 |
|---|---|---|
| `create(type, name, config)` | 是，key=`(type, name)` | 容器构造。**热重建前必须先 `drop`**，否则命中缓存拿到的是带旧配置的旧实例（TS-012） |
| `drop(type, name)` | 摘掉单个 key | 单段热应用。只动这一段，不牵连别的服务的连接与实例（TS-012 / TS-013） |
| `get_class(type, name)` | 否 | 配置页「测试连接」用**表单当前值**现建一次性实例（TS-011 / TS-017） |
| `create_parsers(config)` | 否（每次新建） | 遍历 `doc_parser` 注册项 → `{扩展名: 实例}`；同一实例可占多个扩展名 |
| `name_of(type, instance)` | 按类反查 | **实例 → 注册名**，监控页 badge 的口径所在 |

`name_of` 是"配置名 ≠ 运行名"的落地点：容器降级后实例类型已经换人（`http_embedding → mock`、
`mysql → memory`），状态展示若读配置名，就会把本地 mock 当成真实外部服务汇报。
已知取舍：它按类反查的是**首个**注册名，所以别名会被"纠正"成主名 —— 配置写
`adapter: vllm` 时 badge 显示 `openai_compatible`（同一个类，信息不算错，但不是用户写下的那个词）。

#### 4. 名字的四套变体（"对不上"的地方都在这里）

1. **段名 ↔ 容器属性**：`vector_store → c.vector`、`knowledge_graph → c.graph`、
   `business_data → c.business`。`SECTION_ADAPTERS`（`rag/container.py:51-61`）是唯一权威映射。
2. **degraded 台账两套键**：属性名（`vector`/`graph`/`business`）与段名
   （`vector_store`/`knowledge_graph`/`business_data`）并存，`SECTION_DEGRADED_KEYS`
   （`container.py:66-76`）负责"重建成功时两套都清"，否则界面会出现"已经连上了却还挂着降级"（TS-013）。
3. **历史别名兼容层**：`LEGACY_SECTION_ALIASES` / `LEGACY_ADAPTER_ALIASES`
   （`config/models.py:26-27`）+ loader 的 `_migrate_legacy_sections`（`config/loader.py:23-38`），
   段名 `mysql_meta → meta`、注册名 `mysql_meta → mysql`；请求层还留着旧 kind 的映射
   （`routes.py:199-205` 的 `_DEGRADED_KEY`、`routes.py:1770-1776` 的 `attr_map`），
   因为浏览器里缓存的旧页面仍会带旧 kind 发请求。
4. **没有槽位的服务**：`redis` 段有配置页卡片、有容器属性 `c.redis`、有 degraded 键，
   但**没有适配器槽位、也没有注册名**（客户端由 `rag/adapters/redis_cache.make_client` 直连）。
   所以任何"按槽位查表"的代码（如 `_section_label`）对 redis 必须能安全退回无方言标题。

页面入口的分布由这三层决定：配置页 = `_MODEL_SECTIONS`（llm/embedding）+
`_SERVICE_SECTIONS`（meta/storage/redis/fulltext/vector_store/knowledge_graph/synonym）；
`business_data`（只关配置页入口、能力仍在，见 TS-018）与 `auth` **只在监控页**出现；
`doc_parser` 无页面入口。

展示口径的分工（两者刻意不同）：配置页卡片的参数列表**不含 `adapter`**（实现不可改），
所以标题必须带方言名（`_section_label`，如"元数据库 (MySQL)"）；监控页每行都有实现 badge，
标题就只写到槽位为止（"元数据库"），否则同一行会出现两份口径。

#### 5. 全量对照表（组件名 ↔ 槽位 ↔ 注册名）

| 组件名（配置页） | 组件名（监控页） | 槽位 | 注册名（YAML `adapter:` 取值） | 容器属性 | 配置段 | 页面入口 | 失败时的去向 |
|---|---|---|---|---|---|---|---|
| LLM 大模型 | LLM 大模型 | `llm` | `openai_compatible`（别名 `vllm`）、`mock` | `c.llm` | `llm` | 配置页·模型 | 降级 `mock` |
| 向量模型 (Embedding) | 向量模型 | `embedding` | `http_embedding`（别名 `openai_embedding`）、`mock` | `c.embedding` | `embedding` | 配置页·模型 | 降级 `mock` |
| 元数据库 | 元数据库 | `meta` | `mysql`、`memory` | `c.meta` | `meta` | 配置页·服务 | 降级 `memory` |
| 全文检索 | 全文检索 | `fulltext` | `elasticsearch`（别名 `opensearch`） | `c.fulltext` | `fulltext` | 配置页·服务 | 关闭（`None`） |
| 向量库 | 向量库 | `vector_store` | `milvus`、`qdrant`、`pgvector` | `c.vector` | `vector_store` | 配置页·服务 | 关闭（`None`） |
| 知识图谱 | 知识图谱 | `knowledge_graph` | `neo4j`、`nebula` | `c.graph` | `knowledge_graph` | 配置页·服务 | 关闭（`None`） |
| —（无编辑入口） | 业务数据库 | `business_data` | `sqlalchemy` | `c.business` | `business_data` | 仅监控页 | 关闭（`None`） |
| 同义词表 | 同义词表 | `synonym` | `file_based`、`http_service`、`none` | `c.synonym` | `synonym` | 配置页·服务 | 降级 `none` |
| 对象存储 | 对象存储 | `storage` | `minio`、`local_fs` | `c.storage` | `storage` | 配置页·服务 | 降级 `local_fs` |
| 缓存 (Redis) | 缓存 | **无槽位** | **无注册名**（`redis_cache.make_client` 直连） | `c.redis` | `redis` | 配置页·服务 | `None`（会话/记忆退化为进程内存） |
| —（无连接测试卡） | 认证服务 | `auth` | `jwt`、`dev`、`oidc` | `c.auth` | `auth` | 仅监控页 | 降级 `dev` |
| —（无页面入口） | —（不在监控列表） | `doc_parser` | `pdf`、`docx`、`xlsx`、`pptx`、`markdown`、`txt`、`html`、`image` | `c.parsers`（`{扩展名: 实例}`） | 无（解析器直接拿整份 config） | 无 | 该格式解析失败 |

"失败时的去向"来自两处：带降级替身的取 `_CORE_FALLBACKS`（`container.py:35-42`，
即 `llm/embedding/meta/auth/storage/synonym`），其余可选依赖走 `_try_create` → 置 `None` 关闭。
`_LOCAL_IMPL = {mock, memory, dev, local_fs, none}`（`container.py:45`）表示"不需要 `base_url`
的本地实现"，用来把"未配置"与"构造失败"区分开。

`doc_parser` 的注册名 ↔ 扩展名（`create_parsers` 按 `supported_extensions` 展开）：

| 注册名 | 覆盖扩展名 |
|---|---|
| `pdf` | `.pdf` |
| `docx` | `.docx` `.doc` |
| `xlsx` | `.xlsx` `.xls` `.csv` |
| `pptx` | `.pptx` `.ppt` |
| `markdown` | `.md` `.markdown` |
| `txt` | `.txt` `.log` |
| `html` | `.html` `.htm` |
| `image` | `.png` `.jpg` `.jpeg` `.bmp` `.tiff` `.webp` |

#### 6. 维护规则（改动前先看这五条）

1. **新增实现**：装饰器注册即可被 `adapter:` 引用；若该槽位对用户可选（向量库/全文检索/图谱），
   还要同步配置页可选项与文档。
2. **新增别名**：只有当别名与主名**指向同一个类**时才允许直写 `_classes`；协议一旦分化，
   必须拆成独立类 + 装饰器注册（否则"测试连接"与版本自检会打错分支）。
3. **改名**：段名 / 槽位 / 注册名三处必须一起改，并保留 `LEGACY_*_ALIASES` 兼容层，
   否则存量 YAML 与浏览器缓存载荷会直接 `AdapterNotFoundError`（TS-011 同类问题）。
4. **展示名**：槽位名只说明"这是哪类能力"，实现名一律由 `name_of(运行实例)` 给出；
   页面上任何"看起来像实现名"的文案都必须来自这一份数据，不能写死。
5. **按槽位查表的代码**（`_section_label`、`_DEGRADED_KEY`、`SECTION_ADAPTERS`）
   必须容忍两类例外：无槽位的 `redis`、以及历史 kind（`mysql_meta` / `vector` / `graph` / `business`）。
