"""
Web UI 路由与模板（rag/web/routes.py）

1. 页面路由（Jinja）：/chat /knowledge /knowledge/docs/{id} /config /monitor /login
2. UI 补充 API：集合列表、检索路、会话重命名、任务重试、文档 chunks/内容/恢复/彻底删除
3. 管理 API：配置读取/保存、连接测试、解析预览、运行监控快照

注册：在 rag/api/app.py 的 create_app 中调用 register_ui(app)。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from rag.adapters.registry import AdapterRegistry
from rag.api.deps import get_current_user, require_admin
from rag.config.models import (DOC_PARSE_CAPABILITIES,
                               LEGACY_SECTION_ALIASES, MODEL_ENTRY_FIELDS,
                               ModelEntry, clean_model_ids,
                               doc_parse_capability,
                               doc_parse_capability_label,
                               doc_parse_endpoint_path, is_http_url,
                               model_param_fields, new_entry_id,
                               rerank_api_endpoint, resolve_rerank_model_path)
from rag.config.secrets import (MASK, SENSITIVE_KEYS, is_display_dots,
                                is_untouched)
from rag.container import (
    RECOVER_INTERVAL_SEC, RECOVERABLE_SECTIONS, SECTION_DEGRADED_KEYS,
    ServiceContainer,
)
from rag.models import IngestStatus, UserContext
from rag.observability.logging import get_logger

log = get_logger("rag.web")

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

pages_router = APIRouter(tags=["pages"])
ui_router = APIRouter(prefix="/api", tags=["ui"])
admin_ui_router = APIRouter(prefix="/api/admin", tags=["admin-ui"])

_PATH_DESC = {
    "vector": "语义向量检索", "bm25": "全文检索", "kw_exact": "关键词精确匹配",
    "graph": "知识图谱多跳遍历", "ephemeral": "会话临时文档",
    "structured": "结构化数据检索",
}


def _lost_path_reason(c: ServiceContainer, path: str) -> str:
    """检索路「配了却不可用」的原因；**只补别处看不出来的那几种**

    服务行本身红着的（连不上 / 未初始化）不必在这里重复：那一行有自己的
    message，口径也统一。真正需要说清的是**服务行全绿、检索路却关着**的情况
    —— 向量空间不一致、或向量模型未就绪。不给原因的话，用户看到"5/6 就绪"
    只会以为系统在无缘无故少查一路。
    """
    if path == "vector":
        if getattr(c, "vector", None) is not None and not c.vector_space_ok():
            return c.vector_space_reason()
        if any(k in c.degraded
               for k in SECTION_DEGRADED_KEYS.get("embedding", ("embedding",))):
            return "向量模型不可用，向量检索已关闭"
    return ""


def _container(request: Request) -> ServiceContainer:
    return request.app.state.container


class _LoginRequired(Exception):
    """页面未认证：由全局 handler 302 重定向到 /login"""


async def _page_user(request: Request) -> UserContext:
    """页面级用户依赖：
    - dev 模式自动放行
    - 优先读 Authorization 头（程序化访问）；浏览器导航无该头 → 读登录页写入的 Cookie
    - 均无 → 302 /login（浏览器场景），API 场景仍返回 401 JSON
    """
    import os
    from rag.adapters.base import AuthError
    container: ServiceContainer = request.app.state.container
    authorization = request.headers.get("authorization")
    accept = request.headers.get("accept", "")
    if not authorization:
        cookie_token = request.cookies.get("rag_token")
        if cookie_token:
            authorization = "Bearer " + cookie_token
    if not authorization and container.config.auth.adapter == "dev":
        user = await container.auth.verify("")
        user.is_admin = os.environ.get(
            "RAG_DEV_ALLOW_ADMIN", "").lower() in ("1", "true", "yes")
        return user
    if not authorization or not authorization.startswith("Bearer "):
        # 浏览器导航（Accept: text/html）→ 跳登录页；API 调用 → 401
        if "text/html" in accept:
            raise _LoginRequired()
        raise HTTPException(401, "缺少 Bearer Token")
    token = authorization[7:].strip()
    try:
        user = await container.auth.verify(token)
    except AuthError as e:
        if "text/html" in accept:
            raise _LoginRequired() from e   # 浏览器：token 失效 → 跳登录页
        raise HTTPException(401, str(e)) from e
    user.is_admin = (container.config.is_admin_role(user.roles)
                     or "admin" in user.roles)
    return user


def _user_view(user: UserContext) -> dict:
    name = user.username or user.user_id
    return {"userId": user.user_id, "name": name,
            "roles": list(user.roles or []), "isAdmin": bool(user.is_admin)}


def _redact(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            # 子串规则兜住各种方言命名；精确命中 SENSITIVE_KEYS 再补一刀，
            # 否则 token / dsn 这类不含上述子串的凭据会被明文下发（TS-023）
            if kl in SENSITIVE_KEYS or any(s in kl for s in
                                           ("password", "secret", "api_key")):
                out[k] = MASK if v not in (None, "", {}) else v
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _role_views(container: ServiceContainer) -> list[dict]:
    return sorted({p.role for p in container.config.permissions})


def _collection_names(container: ServiceContainer, user: UserContext) -> list[str]:
    accessible = set(container.config.collections_for_roles(user.roles))
    if "*" in accessible:
        known = {c for p in container.config.permissions for c in p.collections}
    else:
        known = accessible
    known.discard("*")
    return sorted(known) or ["default"]


# ───────── 配置归一化（前端统一载荷 ↔ YAML 局部更新）─────────

_SPECIAL_KEYS = ("retrieval", "chunking", "prompts", "ingest", "ephemeral", "memory",
                 "pipeline", "security", "document_parsing", "notification",
                 "consistency_check", "observability", "sql_agent", "hybrid_sql_agent",
                 "verification", "verification_refine")

# 服务依赖域：UI 分组键（= YAML 键）→ (中文名, 连接测试 kind)
# 元组的先后顺序**就是页面上的排布顺序**（前端按行填充 3 列网格），所以这里的
# 顺序按「先配好基础设施 → 再配检索与存储后端 → 最后辅助表」来排：
#   第 1 行 元数据库 / 对象存储 / 缓存
#   第 2 行 全文检索 / 向量库 / 知识图谱
#   第 3 行 同义词表
# 注意：business_data（业务数据库）**刻意不在列** —— 它的 YAML 段与容器初始化
# 都保留（结构化检索仍可用），只是不再提供配置页编辑入口。
_SERVICE_SECTIONS = (
    ("meta", "元数据库", "meta"),
    ("storage", "对象存储", "storage"),
    ("redis", "缓存 (Redis)", "redis"),
    ("fulltext", "全文检索", "fulltext"),
    ("vector_store", "向量库", "vector"),
    ("knowledge_graph", "知识图谱", "graph"),
    ("synonym", "同义词表", "synonym"),
)

# ── 标题里的方言名（仅配置页）──────────────────────────────────
# 一个配置段可能被多个后端实现共用（元数据库 mysql/memory；向量库
# milvus/qdrant/pgvector；全文检索 elasticsearch/opensearch……），
# 配置页的分组标题必须跟着**实际配置的实现**走：写死 (MySQL) 时，只要把 adapter
# 换成别的后端，页面就会显示「元数据库 (MySQL)」而卡片配的是 pgsql。
# 键是注册名（@AdapterRegistry.register 的第二个参数），查不到就退回不带方言
# 的槽位名。此表未登记的段（缓存/认证/LLM…）标题与实现无关，原样保留。
#
# 监控页**不用**这张表：那里每行都挂着实现 badge（且降级后如实变成
# mock/memory），标题再拼一个方言名就成了同一行的第二份口径 —— 标题说
# 「元数据库 (MySQL)」、badge 说 memory。所以监控页标题只写到槽位为止，
# 实现名交给 badge 承担（见 _monitor_service_specs）。
_SECTION_LABELS: dict[str, dict[str, str]] = {
    "meta": {"mysql": "元数据库 (MySQL)"},
    "storage": {"minio": "对象存储 (MinIO)", "local_fs": "对象存储 (本地目录)"},
    "fulltext": {"elasticsearch": "全文检索 (Elasticsearch)",
                 "opensearch": "全文检索 (OpenSearch)"},
    "vector_store": {"milvus": "向量库 (Milvus)", "qdrant": "向量库 (Qdrant)",
                     "pgvector": "向量库 (pgvector)"},
    "knowledge_graph": {"neo4j": "知识图谱 (Neo4j)",
                        "nebula": "知识图谱 (NebulaGraph)"},
}


def _section_label(section: str, label: str, *adapters: str) -> str:
    """段标题加方言名：按传入顺序取第一个命中的，都不命中用调用方给的无方言标题

    仅配置页使用：配置页卡片的参数列表里不含 adapter（实现不可改），标题是唯一
    说明「这张卡片管的是哪个实现」的地方，所以必须带方言名。
    """
    labels = _SECTION_LABELS.get(section)
    if not labels:
        return label
    for name in adapters:
        hit = labels.get(str(name or "").lower())
        if hit:
            return hit
    return label


# 连接测试 kind → 容器 degraded 记录的组件键（两套命名并不完全一致）
# "mysql_meta" 是历史 kind：改名为 meta 后仍保留映射，浏览器里缓存的旧页面
# 还会带着它发请求，不能让它静默失效
_DEGRADED_KEY = {
    "meta": "meta", "mysql_meta": "meta",
    "vector": "vector_store", "vector_store": "vector_store",
    "graph": "knowledge_graph", "knowledge_graph": "knowledge_graph",
    "business": "business_data", "business_data": "business_data",
    "redis": "redis", "storage": "storage", "fulltext": "fulltext",
}

# 模型域：UI 分组键（= YAML 键）→ (中文名, 可编辑扁平参数)
# 仅展示用户需要关心的参数；timeout/并发/批大小等由代码默认值管理
# 顺序 = 用户填写顺序：模型名 → API 地址 → API Key → 模型ID → 其余。
# display_name 排在最前：它不连任何东西，只是这张卡片的"名字"，
# 值会同步显示在标题上（前端按 key 认它，见 config-ui.js 的 syncNameTag）。
# API 地址（base_url）不在这张表里 —— 它是卡片上的独立控件（见 _normalize_config
# 的 "endpoint"），由前端插到 display_name 与 api_key 之间。
# api_key 排在 model 之前：填完地址与 Key 才能去服务端问"有哪些模型"
# （前端在两者之间插一行「获取模型ID」，见 config-ui.js 的 injectModelPicker）。
# rewrite_model 不下发到界面：它不是"必配项"，留一个空输入框只会让人以为
# 不填就改写失效（真实行为是缺省跟随主模型）。YAML 键与字段都保留 —— 老配置里
# 写了照旧生效，且它不在保存载荷里，保存其他项也不会把它抹掉。
_MODEL_SECTIONS = (
    ("llm", "LLM 大模型",
     ("display_name", "api_key", "model", "temperature", "max_tokens")),
    # VLM 视觉模型：与 llm 完全同构（同一个 OpenAI 兼容对话接口，只是消息里能带
    # 图），所以它的段描述就在这里多一行 —— 模型库、获取模型ID、测试模型、写入
    # YAML 全部自动继承（见 models.ModelEntry 与 _write_model_section 的通用分支）。
    # 界面上它与 llm **共用右侧那一份表单**（标题写「大模型」），两块库上下叠着：
    # 点哪一块的卡片就是在编哪一段（见 config-ui.js 的 SHARED_FORM_LIBS）
    ("vlm", "VLM 视觉模型",
     ("display_name", "api_key", "model", "temperature", "max_tokens")),
    ("embedding", "向量模型 (Embedding)",
     ("display_name", "api_key", "model", "dim", "query_prefix")),
    # 文档解析（Doc-Parse）：一台 PaddleX 服务 + 一项处理能力。
    # 界面上只填「模型名 → API 地址 → 处理能力」三样：
    #   - 没有 api_key（PaddleX serving 默认不带鉴权）；
    #   - **没有「模型ID」这一行**，连带没有「可用模型 / 获取模型ID」那一行（它挂在
    #     模型ID 行上方，见 config-ui.js 的 injectModelPicker）—— 一台 PaddleX 服务
    #     按能力拆 endpoint，没有"调用哪个模型"这种选择，条目身份由后端生成的 id
    #     承担（见 models.new_entry_id）。这几处界面口径由 _MODEL_SECTION_UI_FLAGS
    #     下发，入库接口也按字段表判断要不要模型ID（见 model_library_upsert）。
    #   - 「处理能力」是下拉（见 _ENUM_PARAM_OPTIONS），值 → PaddleX 的 endpoint 路径
    #     （见 models.DOC_PARSE_CAPABILITIES）；「测试模型」测的是服务的 /health
    # 它与另外几段的库口径也不同（见 model_library_upsert / _entry_view）：
    # **一张卡片 = 一个模型名（一台 PaddleX 服务）**，同一台服务上的几项能力是这张
    # 卡片上的几枚徽标（库里的几条配置，同名 + 同地址 + 不同能力），不是什么"多套
    # 可切换的配置"——所以这一段没有"设为 active"，卡片恒为 active
    ("doc_parse", "文档解析（Doc-Parse）",
     ("display_name", "capability")),
)

# 「模型库」一共四段：上面三段 + 重排模型。它们的库都支持多套已测通的配置并存、
# 可切 active、可删，但重排有一段不同 —— **存储位置**：它是本机 Cross-Encoder 权重
# 目录，没有服务地址、没有凭据可存，库里也就没有 base_url/api_key（见
# models._sync_rerank_library），所以它挂在 retrieval 段上，不是这里的顶层段。
# 模型库接口用 _RerankStore 把它伪装成同样的"段"，流程一行都不用改。
_MODEL_SECTION_KEYS = frozenset(k for k, _, _ in _MODEL_SECTIONS)
_MODEL_SECTION_PARAMS = {k: params for k, _, params in _MODEL_SECTIONS}
# 重排段的行顺序：模型名 → 模型路径/API → 模型ID → 推理设备（前端在 model 前插
# 「获取模型ID」，在末尾补「测试模型」）。它没有**独立**的「API 地址」行：地址那
# 一半已并进「模型路径/API」——填目录 = 本机 Cross-Encoder，填 http(s) 地址 =
# 那台远程重排服务（见 models.is_http_url / rerank_api_endpoint）。所以这一段的
# base_url/api_key 始终为空，前端也就不画那一行（见 _normalize_config 的
# noEndpoint 与 config-ui.js 的 injectEndpoint）
# 「模型路径/API」是权重目录或服务地址、「模型ID」是目录名或服务要的模型名：
# 分两行才好按目录取候选（见 list_model_ids / health_test 的 kind == "rerank" 分支）
_RERANK_PARAMS = ("display_name", "model_dir", "model", "device")
_MODEL_SECTION_PARAMS["rerank"] = _RERANK_PARAMS

# 模型段的界面口径（按段名下发到前端，不落盘）：有些段缺某些行，不是"字段值为空"
# 而是**根本不该画**——值空着还能填，行画出来了用户就会以为必须填。
#   noModelId：没有「模型ID」这一行，连带不画「可用模型 / 获取模型ID」（同段表单里
#     那个入口挂在模型ID 行上方）。文档解析就是这一类：一台 PaddleX 服务按能力拆
#     endpoint，没有"调用哪个模型"可选，条目身份由后端生成的 id 承担 —— 前端据此
#     跳过模型ID 的必填校验、入库时也不带 modelIds（见 config-ui.js 的 buildPayload）
#   endpointPlaceholder：「API 地址」的占位提示。默认那句写着"留空则降级为内置
#     Mock"，对本段是错的（文档解析没有 Mock 替身，地址必填）
#   capabilityBadges：卡片按「模型名」归并 —— 一张卡片摆的是同一个模型名下的几枚
#     「处理能力」徽标（库里同名的几条配置），点徽标=编辑那条、徽标尾部的 ×=删掉
#     那项能力（见 config-ui.js 的 renderCapabilityLibrary）；点卡片本身不进编辑
#     （一张卡片上有多条，点卡片说不清要编哪一条）
_MODEL_SECTION_UI_FLAGS = {
    "doc_parse": {
        "noModelId": True,
        "capabilityBadges": True,
        "endpointPlaceholder":
            "http://host:8080（PaddleX 服务根地址，测试走它的 /health）",
        "displayNamePlaceholder": "如：PaddleX OCR 服务 / 版面解析服务",
    },
}

_PARAM_LABELS = {
    "base_url": "API 地址", "api_key": "API Key", "model": "模型ID",
    "display_name": "模型名",
    "device": "推理设备",                     # 重排模型：Cross-Encoder 加载设备
    # 重排模型：本机权重目录，或远程重排服务地址（两义共用这一栏，故名字写全）
    "model_dir": "模型路径/API",
    # 文档解析：处理能力 → PaddleX 的一个 endpoint 路径（见 _ENUM_PARAM_OPTIONS）
    "capability": "处理能力",
    "temperature": "温度", "max_tokens": "最大 Token",
    "timeout": "超时(秒)", "max_concurrency": "并发数", "dim": "向量维度",
    "batch_size": "批大小", "query_prefix": "查询前缀",
    "host": "主机", "port": "端口", "user": "用户名", "password": "密码",
    "database": "数据库", "enabled": "启用状态",
    "hosts": "地址列表", "username": "用户名", "index_prefix": "索引前缀(默认)",
    "endpoint": "端点", "access_key": "Access Key", "secret_key": "Secret Key",
    "bucket": "Bucket（默认）", "secure": "HTTPS", "local_root": "本地根目录",
    "uri": "URI", "max_hops": "最大跳数", "dsn": "DSN 连接串",
    "db": "DB 编号（默认）",
    "session_ttl_hours": "会话 TTL(小时)", "prefix": "键前缀（默认）",
    "file": "同义词文件", "auto_reload_minutes": "自动重载(分钟)",
    "charset": "字符集", "auto_create_tables": "自动建表",
    "verify_certs": "校验证书", "schema_file": "Schema 文件",
    "allowed_tables": "允许表", "max_rows": "最大行数", "url": "服务地址",
    "collection_prefix": "集合前缀（默认）", "sensitive_fields": "敏感字段",
}

# 精确匹配的凭据字段名（避免 max_tokens 因含 "token" 被误判为密码）：
# 直接复用落盘加密集合 SENSITIVE_KEYS。三者（加密 / 界面打码 / 保存时留空不覆盖）
# 必须同源，否则会出现"落了盘的字段界面却明文回显"这类漏洞。
# 刻意不包含：
#   - access_key：身份标识而非密文（MinIO/S3 控制台里也明文展示），_redact 的
#     子串规则本就不遮它；标成密码框只会让人看不清自己填的是哪个 AK，
#     还会招来浏览器密码管理器。真正的凭据是 secret_key。
#   - sensitive_fields：它是"哪些列要打码"的名字清单（business_data 的配置），
#     本身不是凭据；当密码框会让它无法编辑，保存时还会被空值抹掉。
_SECRET_PARAM_KEYS = SENSITIVE_KEYS

# 凭据行（_param_row 的 reveal）的两种下发口径：
#
#   默认 —— 不下发真值，按 TS-023 只给 hasValue（存过没有）与 valueLen（存了多长），
#   前端据此画等量圆点当"已存"的占位（占位是纯显示，不作为值回传）。服务段、以及
#   模型段里除 api_key 之外的凭据（数据库密码、Secret Key…）都走这条。
#
#   reveal=True —— 下发**解密后的真值**。只有**模型段**传：模型卡片与模型库条目。
#   因为模型卡片上的「测试模型」「获取模型ID」「存入模型库」都直接读书框里的文本，
#   框里的内容**就是**发出去/存下来的那把钥匙。只给占位会留一个假象：用户删掉几个
#   字符后剩下的那串圆点会被当成"没改过"、回落到这条自己的钥匙 —— 框里明明改坏了、
#   测试却照样通过（用户报的就是这一幕）。给了真值，改坏它 = 真把钥匙改坏了，测试
#   如实失败。代价是这把钥匙会出现在页面内存里：它是管理员自己填进去的凭据，配置台
#   本身也是 require_admin（登录后才可达），所以只对模型段认这个代价。
#
# 服务段绝不传 reveal：同义词表里也有个叫 api_key 的参数，但它走的是"留空/圆点 =
# 沿用原值"的老路，没有上面那个假象 —— 按**键名**放行会把凭据多送一份到页面，
# 所以这里由调用点显式决定（见 _normalize_config 的模型段与 _entry_view）。

# 下拉型参数：键 → 选项（值 + 显示文案）。取值只有少数几种、且后端按白名单归一
# （填错只会静默回落到缺省项）的参数一律给下拉：让用户在合法取值里选，而不是
# 猜后端认哪几个字符串。选项由后端下发 —— 前端不再维护第二份"有哪些能力"的清单。
_ENUM_PARAM_OPTIONS: dict[str, list[dict]] = {
    # 文档解析的处理能力：值就是 PaddleX 的 endpoint 名（见 DOC_PARSE_CAPABILITIES）
    "capability": [{"v": k, "label": label}
                   for k, (label, _path) in DOC_PARSE_CAPABILITIES.items()],
}

# 允许留空的参数：UI 在输入框右侧标注 optional
# （MySQL 免密账号；ES 未开启安全认证时用户名/密码都不用填）
_OPTIONAL_PARAM_KEYS = frozenset({"password", "username"})

# 按分组追加「可留空」的参数：向量库的 user 与 password 是**成对**规则
# （都填或都留空 = 匿名连接，只填一个会被适配器在构造期拒绝）。原先只给 password
# 标了 optional，界面读起来像"用户名必填、密码可选"，与实际规则正好相反。
_OPTIONAL_BY_GROUP = {"vector_store": frozenset({"user"})}

# 服务分组「前置条件」提示（标题旁 ⓘ 悬浮显示）：仅列需要用户在服务端预先准备的服务
_SERVICE_HINTS = {
    "meta": {
        "title": "前提条件（需先在 MySQL 服务端执行）",
        "code": "\n".join([
            "CREATE DATABASE IF NOT EXISTS rag_meta DEFAULT CHARACTER SET utf8mb4;",
            "CREATE USER IF NOT EXISTS 'rag'@'192.168.100.%' IDENTIFIED BY '<你的密码>';",
            "GRANT ALL PRIVILEGES ON rag_meta.* TO 'rag'@'192.168.100.%';",
            "FLUSH PRIVILEGES;",
        ]),
        "notes": [
            "数据库必须预先创建；表结构由应用自动创建，无需手工建表。",
            "若连接要等约 10 秒才建立：服务端 skip_name_resolve=OFF 会触发反向 DNS 超时，"
            "在 my.cnf 的 [mysqld] 下设 skip_name_resolve=ON 并重启，即可降到毫秒级。",
        ],
    },
    "vector_store": {
        "title": "前提条件（需先在向量库服务端准备）",
        "code": "\n".join([
            "# Milvus：集合名为 <集合前缀><知识域名>（默认 rag_default），",
            "#   由应用自动创建；账号需具备建集合/建索引/load/读写权限。",
            "#   未开启认证时用户名与密码**都留空** —— pymilvus 只在两者",
            "#   都非空时才启用认证，只填一个会被静默当成匿名连接。",
            "# pgvector：库必须预先创建（应用只建表、不建库），",
            "#   并安装 pgvector 扩展（首次建表会自动 CREATE EXTENSION vector）。",
        ]),
        "notes": [
            "集合前缀只允许字母、数字与下划线（按 Milvus 的命名规则从严），"
            "改它等于换一整套集合，旧数据不会迁移。",
            "「测试连接」按表单当前值直连探测，连通性与认证一起校验；"
            "集合名非法（中文 / 短横线）会在配置阶段就被拒绝，不必等到入库。",
        ],
    },
    "fulltext": {
        "title": "前提条件（需先在 Elasticsearch 服务端准备）",
        "code": "\n".join([
            "# 索引由应用自动创建（rag_<集合名>），无需手工建索引；",
            "# 但中文检索依赖 analysis-ik 插件，缺它时建索引会 400：",
            "# 1) 安装插件：版本必须与你 ES 服务端的版本**完全一致**",
            "#    （服务端 8.19.10 就下 8.19.10；插件大版本与 ES 不一致会加载失败）",
            "bin/elasticsearch-plugin install \\",
            "  https://github.com/infinilabs/analysis-ik/releases/download/v<ES版本>/elasticsearch-analysis-ik-<ES版本>.zip",
            "# 2) 重启 ES，再验证分词器已注册：",
            "curl 'http://localhost:9200/_analyze?analyzer=ik_max_word&text=%E4%B8%AD%E6%96%87'",
        ]),
        "notes": [
            "「测试连接」会同时校验集群连通性与 ik_max_word 分词器：缺插件时本服务"
            "被判为不可用并关闭功能（保存后提示里会写明原因）。",
            "集群未开启安全认证时，用户名与密码都留空即可。",
            "地址列表每项都要带协议（http://ip:9200）；index_prefix 是物理索引名前缀，"
            "改它等于换一整套索引，旧数据不会迁移。",
        ],
    },
    "redis": {
        "title": "前提条件（Redis 侧无需预先创建任何资源）",
        "code": "\n".join([
            "# 不需要预建库/表/索引：会话键由应用首次写入时自动创建；",
            "# 需要服务端满足的是下面五项 ——",
            "#   1) 密码正确（requirepass / ACL 用户；留空 = 不带 AUTH）；",
            "#   2) ACL 允许 PING / SET / GET / KEYS / PUBLISH / SUBSCRIBE；",
            "#   3) DB 编号在服务端 databases 范围内（默认 0~15）；",
            "#   4) maxmemory-policy 别把会话键当冷数据淘汰，",
            "#      否则会话会“莫名丢失”（allkeys-lru / volatile-lru 都可能）；",
            "#   5) 监听地址要放开给应用所在网段 —— 只绑 127.0.0.1 时，从别的机器",
            "#      连一律“连接被拒绝”，而在服务端 netstat 看却“一切正常”。",
        ]),
        "notes": [
            "「测试连接」按表单当前值真实建连（含认证与 SELECT DB），测通即可保存；"
            "但 PING 覆盖不到 ACL 的命令级权限与淘汰策略，这两项需在服务端确认。",
            "报“连接被拒绝”而服务端确认 Redis 在跑时，先看监听地址："
            "`ss -lntp | grep 6379` 若只见 127.0.0.1 / ::1，改 redis.conf 的 bind"
            "（Docker 则改发布端口的绑定地址）并重启；若已放开 0.0.0.0 仍被拒，"
            "再查服务端防火墙，以及 Redis 7 在无密码 + 非回环监听下的 protected-mode 拒绝。",
            "DB 编号写入配置文件后即成为运行期使用的编号：所有会话读写都走它，"
            "请与已有数据所在的库保持一致，否则会出现“连得上但会话是空的”。",
            "键前缀是会话键的命名空间（rag:session:<id>）；改它等于换一套键空间，"
            "旧会话不会再被读到（数据仍在 Redis 里）。",
            "Redis 不可达时会话/记忆退化为进程内存（重启即失），保存后页面会给出具体原因。",
        ],
    },
}

# 服务分组中不再对用户暴露、改由代码恒定管理的参数
# （隐藏开关必须同时写死取值，否则界面与配置文件会各说各话）
_SERVICE_FIXED_PARAMS = {"meta": {"auto_create_tables": True}}

# 服务分组内的参数显示顺序：只列需要「挪位置」的键，未列出的按模型字段顺序追加在后。
# storage 的 secure(HTTPS) 与 endpoint 共同决定连接方式（协议头由 secure 拼出），
# 被 access_key/secret_key 隔开时会让人以为 HTTPS 只影响认证那几项。
_SERVICE_PARAM_ORDER = {
    "storage": ("enabled", "endpoint", "secure", "access_key", "secret_key",
                "bucket"),
}

# 不在配置页展示的服务参数：既不渲染也不参与保存（YAML 里的原值保持不动）。
# storage 的 local_root 只在 adapter=local_fs 下才有意义、preview_url_ttl 目前
# 没有任何代码读取，两者却因「按配置段平铺」出现在对象存储(MinIO) 分组里。
# 不用 _SERVICE_FIXED_PARAMS：那会把值写死回默认，覆盖 local_fs 用户自定义的目录。
_SERVICE_HIDDEN_PARAMS = {
    "storage": ("local_root", "preview_url_ttl"),
    # redis 的 session_ttl_hours 同理：它是"会话键多久过期"的运行期口径，由配置
    # 文件的既有值管理（默认 48h）。放在界面上只会让人以为"调大它就能连上"，
    # 而改小/改 0 反而会让每次保存都报 invalid expire time 并静默退化为进程内存。
    "redis": ("session_ttl_hours",),
}

# 只对特定 adapter 有意义的参数：服务段 → {参数键: 该参数无意义的 adapter 集合}。
# vector_store 是多个 adapter 共用同一个配置段（milvus / qdrant / pgvector），
# database 只被 pgvector 使用 —— 不隐藏的话 Milvus 用户会以为"还有个库名没填"，
# 从而怀疑配置不完整。隐藏同时意味着保存时不回写，切到 pgvector 仍保留原值。
_SERVICE_HIDDEN_BY_ADAPTER = {
    "vector_store": {
        "database": {"milvus", "qdrant"},
        # timeout 实际只有 qdrant 读取（httpx 客户端）；milvus 的探测预算由代码常量
        # 管理（HEALTH_BUDGET_SEC / PROBE_REQUEST_TIMEOUT_SEC），配置里的这个值
        # 对它没有任何作用 —— 留在界面上只会让人以为"调大它就能连上"。
        "timeout": {"milvus"},
    },
}


def _service_params(sect: dict, group: str) -> list[tuple[str, object]]:
    """展平一个服务分组的标量参数：先剔除不展示的键，再按声明顺序重排

    sorted 是稳定排序：未出现在 _SERVICE_PARAM_ORDER 里的键保持原有相对顺序。
    """
    hidden = set(_SERVICE_HIDDEN_PARAMS.get(group) or ())
    adapter = str(sect.get("adapter") or "").lower()
    for k, useless_for in (_SERVICE_HIDDEN_BY_ADAPTER.get(group) or {}).items():
        if adapter in useless_for:
            hidden.add(k)
    items = [(k, v) for k, v in sect.items() if k not in hidden]
    order = _SERVICE_PARAM_ORDER.get(group) or ()
    if not order:
        return items
    rank = {k: i for i, k in enumerate(order)}
    return sorted(items, key=lambda kv: rank.get(kv[0], len(rank)))


def _param_row(k: str, v, group: str | None = None,
               plain_value: object = None, reveal: bool = False) -> dict:
    """标量配置项 → 前端行描述（带类型，保存时按类型还原 YAML 标量）

    group 用于按分组追加「可留空」标记（见 _OPTIONAL_BY_GROUP）。
    plain_value：该字段**未脱敏**的原值，只有凭据行用得上（见下方 valueLen）。
    reveal：凭据行是否下发**解密后的真值**。只有模型段传 True（理由见上方那段注释），
    服务段一律不传 —— 那里的凭据继续走"留空/圆点 = 沿用原值"。
    """
    if isinstance(v, bool):
        typ = "bool"
    elif isinstance(v, int):
        typ = "int"
    elif isinstance(v, float):
        typ = "float"
    elif isinstance(v, list):               # 如 ES hosts / allowed_tables
        typ = "list"
        v = ", ".join(str(x) for x in v)
    else:
        typ = "str"
        v = "" if v is None else str(v)
    optional = (k in _OPTIONAL_PARAM_KEYS
                or k in (_OPTIONAL_BY_GROUP.get(group or "") or ()))
    secret = k in _SECRET_PARAM_KEYS
    # 凭据类字段不回传值 —— 连脱敏后的 "******" 也不回传：把掩码当值回填进密码框，
    # 用户会以为 key 被改短了；更糟的是这个假值会被"测试连接"当真值发给模型服务端，
    # 换来一个 401，表现为"地址能访问、却没有模型列表"（TS-023）。
    # 这里只给两个纯标记：hasValue（存过没有）与 valueLen（存了多长），
    # 前端据此画等量圆点（占位是纯显示，不作为值回传）；
    # 保存时留空 → 后端保留原值（见 _yaml_update_from_payload / _restore_sensitive）。
    # 例外：reveal=True（只有模型段传）时下发**解密后的真值**，理由见上方那段注释 ——
    # 那里框里的内容直接决定"发出去/存下来的是哪把钥匙"，占位说不清楚。
    raw = "" if v is None else str(v)
    row = {"key": k, "label": _PARAM_LABELS.get(k, k), "value": v,
           "type": typ, "editable": True,
           "optional": optional,
           "secret": secret}
    if k in _ENUM_PARAM_OPTIONS:
        # 下拉的参数：类型改成 enum，并把合法取值一并下发（前端不问第二遍）
        row["type"] = "enum"
        row["options"] = _ENUM_PARAM_OPTIONS[k]
    if secret:
        row["value"] = (str(plain_value)
                        if reveal and plain_value is not None
                        else "")
        # v 来自 _redact：有值时是 "******"，没值时保持空 —— 两种都能判出"是否已配置"
        row["hasValue"] = bool(raw.strip())
        # 位数必须取自**脱敏前**的明文：掩码恒为 6 个星号，用它的长度画点会把
        # 所有 key 都画成一样长，比不画更误导人（"我明明存了 32 位，怎么只剩 6 个点"）。
        # 下发的只有长度，一个字符都不出网。
        if row["hasValue"] and plain_value is not None:
            row["valueLen"] = len(str(plain_value))
    return row


def _blank_secret_masks(obj):
    """把脱敏产物 "******" 换成空串（只用于下发给前端的可编辑文本）

    _redact 的掩码一旦出现在可编辑文本里，就有"被当成真值保存"的风险；
    空串则是明确语义：留空 = 不修改，后端保存时保留原值。
    """
    if isinstance(obj, dict):
        return {k: ("" if v == MASK else _blank_secret_masks(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_blank_secret_masks(v) for v in obj]
    return obj


def _entry_badge(section: str, e: ModelEntry) -> str:
    """库条目卡片上的徽标文案：这条配置"能干什么"

    只有文档解析有这一枚：它的卡片上**没有模型ID**（见 _MODEL_SECTION_UI_FLAGS），
    于是那个位置改摆「处理能力」——一条配置与另一条的区别本来就是"地址 + 能力"，
    不写出来，同一台服务上的四条曲线（OCR / 版面 / 表格 / 公式）在列表里长得一样。
    """
    if section == "doc_parse":
        return doc_parse_capability_label((e.params or {}).get("capability"))
    return ""


def _entry_view(section: str, e: ModelEntry, active_id: str) -> dict:
    """模型库条目 → 前端视图（endpoint + configParams，与卡片表单同构）

    刻意复用表单那套行结构（含类型 / 凭据标记）：前端的「更改」才能原样回填表单，
    不必再写一遍字段映射 —— 少一份"两处字段名不一致"的机会。
    """
    flat = {"display_name": e.display_name, "base_url": e.base_url,
            "api_key": e.api_key, "model": e.model, **e.params}
    red = _redact(flat)          # 条目里的 api_key 同样不能明文下发
    return {
        "id": e.id,
        "displayName": e.display_name or e.model or e.base_url,
        "endpoint": e.base_url,
        "testedAt": e.tested_at,
        # 文档解析的卡片恒为 active（见 _MODEL_SECTION_UI_FLAGS 的 capabilityBadges）：
        # 库里每一条都是一项在用的能力，没有"切到哪一条生效"这回事 —— 前端据此永远
        # 点亮绿牌、也不摆「设为 active」
        "active": bool(e.id) and (section == "doc_parse" or e.id == active_id),
        # 卡片徽标（有的段才有，见 _entry_badge）：取代"模型ID"那个位置
        "badge": _entry_badge(section, e),
        # [0] 是这条配置调用的那个 —— 表单里的「模型ID」输入框填的就是它
        # （configParams 里那行 model 与它同源，见 normalize_entry_models）。
        # 界面只摆这一个（见 config-ui.js 的模型库卡片），列表仍整体返回：
        # 老配置里可能存过多个ID，别在接口层丢掉它们
        "modelIds": list(e.model_ids),
        "configParams": [_param_row(k, red.get(k), plain_value=flat.get(k),
                                    reveal=True)
                         for k in _MODEL_SECTION_PARAMS.get(section, ())
                         if k in flat],
    }


def _library_view(section: str, entries: list, active_id: str) -> dict:
    """一个模型段的库视图：条目（按加入顺序）+ 当前 active 的 id"""
    return {"activeId": active_id,
            "entries": [_entry_view(section, e, active_id) for e in entries]}


def _normalize_config(c: ServiceContainer) -> dict:
    """AppConfig → 前端配置统一载荷（含降级状态，指导补齐配置）"""
    cfg = c.config
    # plain 只在本函数内用于取凭据长度（_param_row 的 plain_value），绝不下发：
    # 下发的是 _redact 之后的 dump
    plain = cfg.model_dump(mode="json")
    dump = _redact(plain)
    weights = cfg.retrieval.default_route_weights or {}

    models = []
    for key, label, params in _MODEL_SECTIONS:
        sect = dump.get(key) or {}
        plain_sect = plain.get(key) or {}
        # 模型库视图取自配置对象（明文），不是脱敏后的 dump：条目里的 api_key
        # 由 _entry_view 单独按同一套凭据规则处理
        sect_cfg = getattr(cfg, key, None)
        lib_entries = list(getattr(sect_cfg, "models", None) or [])
        active_id = str(getattr(sect_cfg, "active_id", "") or "")
        # 右侧表单里的「模型ID」输入框填的是 active 条目 model_ids 里的第一个
        # （界面上模型ID 就这一个，见 config-ui.js 的 model 行）；与 endpoint /
        # configParams 同源：表单显示的就是"当前生效的配置"
        active_entry = next((e for e in lib_entries if e.id == active_id), None)
        # 文档解析的表单不摆"当前生效的那一套"：这一段没有单一生效的配置（每张卡片
        # 都一直生效，见 _entry_view），编辑一律从点徽标进来 —— 初始表单是一张空白表，
        # 填完测通即新增一条（与「增加模型」同一个状态，见 config-ui.js 的
        # clearFormForNew）。若像别的段那样回填，用户会以为改的是库里那一条，
        # 一保存却是新增，同名同能力会被拒收（见 model_library_upsert）
        blank_form = key == "doc_parse"
        models.append({
            "key": key, "label": label, "tab": "model", "refresh": None,
            "editable": True, "showTestButton": True,
            # 界面口径（缺某些行 / 占位提示）：不落盘，见 _MODEL_SECTION_UI_FLAGS
            **(_MODEL_SECTION_UI_FLAGS.get(key) or {}),
            "endpoint": "" if blank_form else sect.get("base_url", ""),
            "modelIds": list(active_entry.model_ids) if active_entry else [],
            "configParams": [_param_row(k, "" if blank_form else sect.get(k),
                                        plain_value=(None if blank_form
                                                     else plain_sect.get(k)),
                                        reveal=True)
                             for k in params if k in sect],
            # 这一段里所有「已测通」的配置。active 的那一条就是上面这些顶层
            # 字段的来源（见 models._sync_model_library）
            "library": _library_view(key, lib_entries, active_id),
        })

    # 重排模型：三张卡片里唯一"库不在顶层段"的一个（挂在 retrieval 上，见 _RerankStore）。
    # 它没有独立的服务地址可填（地址那一半并进了「模型路径/API」）—— noEndpoint 让
    # 前端既不画「API 地址」行，也不在校验里要求它（见 config-ui.js 的
    # injectEndpoint / buildPayload）。这里刻意不下发 endpoint 键
    ret_cfg = cfg.retrieval
    ret_active = next((e for e in ret_cfg.rerank_models
                       if e.id == ret_cfg.rerank_active_id), None)
    models.append({
        "key": "rerank", "label": "重排模型 (Rerank)", "tab": "model",
        "refresh": None, "editable": True, "showTestButton": True,
        "noEndpoint": True,
        # 勾了「启用重排」才真参与召回：没勾时左列绿牌换成灰牌「未启用」
        "notEnabled": "" if ret_cfg.rerank_enabled else
                      "「检索策略」里的「启用重排」没勾上：问答不会走重排",
        "modelIds": list(ret_active.model_ids) if ret_active else [],
        "configParams": [_param_row(k, getattr(ret_cfg, "rerank_" + k, None))
                         for k in _RERANK_PARAMS],
        "library": _library_view("rerank", list(ret_cfg.rerank_models),
                                 ret_cfg.rerank_active_id),
    })

    services = []
    for key, label, test_kind in _SERVICE_SECTIONS:
        sect = dump.get(key) or {}
        plain_sect = plain.get(key) or {}
        # 标题方言按配置里的 adapter 取：配置页展示的正是"将保存什么"
        label = _section_label(key, label, sect.get("adapter"))
        fixed = _SERVICE_FIXED_PARAMS.get(key) or {}
        flat = [_param_row(k, v, key, plain_sect.get(k))
                for k, v in _service_params(sect, key)
                if k != "adapter" and not isinstance(v, dict) and k not in fixed]
        services.append({"key": key, "label": label, "tab": "service",
                         "refresh": None, "editable": True,
                         "testKind": test_kind, "showTestButton": True,
                         "hint": _SERVICE_HINTS.get(key),
                         "saveScope": "service", "saveGate": "test",
                         "configParams": flat, "paramsJson": {}})

    # 特殊配置域是整块 JSON 编辑框，dump 里的掩码会原样出现在文本中；换成空串，
    # 既不让用户看到假值，也避免"带着星号保存"把真凭据覆盖掉（TS-023）
    special = [{"key": k, "label": k, "tab": "special", "refresh": None,
                "editable": True, "paramsJson": _blank_secret_masks(dump.get(k) or {})}
               for k in _SPECIAL_KEYS if isinstance(dump.get(k), dict)]

    perm = [{"role": p.role, "collections": list(p.collections),
             "is_admin": p.is_admin} for p in cfg.permissions]

    degraded = [{"component": k, "reason": v}
                for k, v in (getattr(c, "degraded", None) or {}).items()]

    return {
        "defaultsFlags": {
            "defaultWeights": bool(weights),
            "hasPermissions": bool(perm),
            "llmConfigured": bool((dump.get("llm") or {}).get("base_url")),
        },
        "modelProviders": models,
        "serviceGroups": services,
        "specialGroups": special,
        "permissionMappings": perm,
        "retrieval": {
            "enabledPaths": sorted(c.enabled_paths()),
            "defaultWeights": {k: round(float(v), 4) for k, v in weights.items()},
            "rerankEnabled": bool(cfg.retrieval.rerank_enabled),
            "rerankThreshold": cfg.retrieval.rerank_threshold,
            # 重排模型本身（模型名/模型ID/设备）不在这里下发：它是 modelProviders
            # 里的第三段（库 + active 条目），见上面那段注释
        },
        "system": {"version": cfg.version, "roleCount": len(cfg.permissions),
                   "degraded": degraded},
    }


def _coerce_param(row: dict):
    """前端行值 → YAML 标量（按 type 还原；非法数值返回 None 表示跳过）"""
    v = row.get("value")
    # 掩码与 UI 圆点都是"未修改"的显示占位（见 rag/config/secrets.py），
    # 绝不能当值写进 YAML
    if v is None or v == MASK or is_display_dots(v):
        return None
    typ = row.get("type") or "str"
    s = str(v).strip()
    if typ == "bool":
        return s.lower() in ("1", "true", "yes", "on")
    if typ == "int":
        try:
            return int(s)
        except ValueError:
            return None
    if typ == "float":
        try:
            return float(s)
        except ValueError:
            return None
    if typ == "list":
        return [x.strip() for x in s.split(",") if x.strip()]
    return str(v)


def _yaml_update_from_payload(payload: dict, enabled_paths: list[str]) -> dict:
    """前端保存载荷 → YAML 局部更新（仅放行白名单结构）"""
    update: dict = {}

    def _f(v, d=None):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    def _b(v):
        return str(v).lower() in ("1", "true", "yes", "on") if not isinstance(v, bool) else v

    # 特殊配置域 JSON 合并（先处理；retrieval 的专用字段随后覆盖，
    # 避免页面初始化时的旧 JSON 整体冲掉专用控件的新值）
    for group in (payload.get("specialGroups") or []):
        key = group.get("key")
        if key in _SPECIAL_KEYS and isinstance(group.get("paramsJson"), dict):
            update[key] = group["paramsJson"]

    # 检索策略（专用字段优先于 specialGroups JSON 中的同名键）
    ret_in = payload.get("retrieval") or {}
    ret: dict = {}
    if "defaultWeights" in ret_in:
        allowed = set(enabled_paths) | {"ephemeral"}
        ret["default_route_weights"] = {
            str(k): _f(v, 0.0) for k, v in (ret_in["defaultWeights"] or {}).items()
            if str(k) in allowed and _f(v) is not None}
    if "rerankEnabled" in ret_in:
        ret["rerank_enabled"] = _b(ret_in["rerankEnabled"])
    # 重排模型的几个镜像字段不再走"整段保存"：库才是源、它们是 active 条目的派生物
    # （见 models._sync_rerank_library），从这里写进去会被下一次加载覆盖掉 —— 那正是
    # "界面说保存成功、其实没生效"的静默失败，所以这里直接报错让用户刷新页面
    mirrors = [k for k in ("rerankModel", "rerankModelDir", "rerankDisplayName",
                           "rerankDevice") if k in ret_in]
    if mirrors:
        raise HTTPException(
            400, f"重排模型不再整段保存（收到 {', '.join(mirrors)}）："
                 "请用模型库的「存入模型库」/「设为 active」/「删除」；"
                 "若是刚升级过版本，刷新页面后重试")
    if "rerankThreshold" in ret_in:
        ret["rerank_threshold"] = _f(ret_in["rerankThreshold"], 0.3)
    if ret:
        update["retrieval"] = _deep_merge(update.get("retrieval") or {}, ret)

    # 权限映射
    perms = payload.get("permissionMappings")
    if perms is not None:
        cleaned = []
        for p in perms:
            role = str(p.get("role", "")).strip()
            if not role:
                continue
            cols = [str(c) for c in (p.get("collections") or ["*"])][:16]
            cleaned.append({"role": role, "collections": cols or ["*"],
                            "is_admin": _b(p.get("is_admin"))})
        update["permissions"] = cleaned

    # 模型域改走模型库接口（/model-library/upsert 等），这里不再直接写顶层字段：
    # 顶层是 active 条目的镜像（见 models._sync_model_library），从这条路径写进去
    # 会在下一次加载时被 active 条目覆盖 —— 一次静默失败：用户以为保存了，实际
    # 什么都没生效。浏览器缓存了旧 JS 的页面会走到这里，明确报错让它刷新，
    # 而不是让它"看起来保存成功"。
    if payload.get("modelProviders"):
        raise HTTPException(
            400, "模型配置已改为「模型库」方式保存：请刷新页面（Ctrl+F5）后重试")

    # 服务依赖域：扁平参数按类型写回（adapter 名不可改）
    service_keys = {k for k, _, _ in _SERVICE_SECTIONS}
    for group in (payload.get("serviceGroups") or []):
        # 旧页面缓存里可能还带着 mysql_meta：归一到 meta，否则这一段会被
        # 当成"不在保存范围内"静默丢弃，用户改了主机却什么都没写回
        key = LEGACY_SECTION_ALIASES.get(group.get("key"), group.get("key"))
        if key not in service_keys:
            continue
        sect = {}
        for row in group.get("configParams") or []:
            k = row.get("key")
            if not k or k == "adapter":
                continue
            v = _coerce_param(row)
            if v is not None:
                sect[str(k)] = v
        # 恒定参数：UI 已移除该开关，保存时一律写回固定值
        for fk, fv in (_SERVICE_FIXED_PARAMS.get(key) or {}).items():
            sect[fk] = fv
        if sect:
            update[key] = sect

    return update


def _deep_merge(dst: dict, src: dict) -> dict:
    out = dict(dst)
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _conn_label(endpoint: str) -> str:
    if not endpoint:
        return "unknown"
    host = endpoint.split("://", 1)[-1].split("/", 1)[0]
    ip = host.split(":", 1)[0]
    port = host.split(":", 1)[1] if ":" in host else ""
    return f"ip={ip} port={port}"


# ───────────────────────── 页面路由 ─────────────────────────

def _base_ctx(request: Request, user: UserContext, page: str) -> dict:
    return {"request": request, "page": page, "current_user": _user_view(user)}


@pages_router.get("/", response_class=HTMLResponse)
async def index():
    return RedirectResponse("/chat", status_code=302)


@pages_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.jinja2", {"request": request})


@pages_router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, session: str | None = None,
                    collection: str | None = None,
                    user: UserContext = Depends(_page_user)):
    c = _container(request)
    ctx = _base_ctx(request, user, "chat")
    retrieval_paths = [
        {"name": n, "description": _PATH_DESC.get(n, n), "enabled": True}
        for n in sorted(c.enabled_paths())
    ]
    ctx.update({
        "collections": [{"name": n, "displayName": n}
                        for n in _collection_names(c, user)],
        "selectedCollection": collection or "",
        "retrievalPaths": retrieval_paths,
        "initialSessionId": session or "",
        "initialMessages": [],
        "initialEphemeralDocs": [],
    })
    if session:
        try:
            st = await c.memory_service.load_session(session)
        except Exception:
            st = None
        if st and (st.user_id == user.user_id or user.is_admin):
            ctx["initialMessages"] = [{
                "id": m.message_id, "role": m.role.value, "content": m.content,
                "createdAt": m.created_at.isoformat(),
                "sources": [s.model_dump(mode="json") for s in m.sources],
                "feedback": m.feedback.value if m.feedback else None,
            } for m in st.short_term[-50:]]
            try:
                docs = await c.ephemeral_service.list_docs(session)
                ctx["initialEphemeralDocs"] = [
                    {"docId": d.get("doc_id"), "filename": d.get("filename"),
                     "fileType": d.get("file_type"), "size": d.get("file_size")}
                    for d in docs]
            except Exception:
                pass
    return templates.TemplateResponse("chat.jinja2", ctx)


@pages_router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, collection: str | None = None,
                         tab: str | None = None, docId: str | None = None,
                         archivedOnly: bool = False,
                         user: UserContext = Depends(_page_user)):
    c = _container(request)
    ctx = _base_ctx(request, user, "knowledge")
    q = None
    try:
        q = c.ingest_coordinator.queue
    except Exception:
        pass
    queued = q.qsize() if q is not None else 0
    depth = q.maxsize if (q is not None and getattr(q, "maxsize", 0)) else None
    ctx.update({
        "collections": [{"name": n, "displayName": n}
                        for n in _collection_names(c, user)],
        "defaultCollection": collection or "",
        "activeTab": ("trash" if archivedOnly else
                      tab if tab in ("docs", "tasks", "trash") else "docs"),
        "focusDocId": docId or "",
        "ephemeralTtlHours": c.config.ephemeral.ttl_hours,
        "roleOptions": _role_views(c),
        "queue": {"queuedTasks": queued, "queueDepthLimit": depth,
                  "queueFull": bool(depth and queued >= depth)},
    })
    return templates.TemplateResponse("knowledge.jinja2", ctx)


@pages_router.get("/knowledge/docs/{doc_id}", response_class=HTMLResponse)
async def doc_detail_page(request: Request, doc_id: str,
                          collection: str | None = None,
                          user: UserContext = Depends(_page_user)):
    c = _container(request)
    doc = None
    candidates = [user.tenant_id, "default",
                  doc_id.split("_")[0] if "_" in doc_id else ""]
    for tid in dict.fromkeys(t for t in candidates if t):
        try:
            doc = await c.meta.get_document(doc_id, tid)
        except Exception:
            doc = None
        if doc:
            break
    if doc is None:
        return RedirectResponse("/knowledge?tab=docs", status_code=302)
    ctx = _base_ctx(request, user, "doc-detail")
    ctx.update({"doc": _doc_view(doc), "backCollection": collection or ""})
    return templates.TemplateResponse("doc-detail.jinja2", ctx)


@pages_router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, user: UserContext = Depends(_page_user)):
    if not user.is_admin:
        return RedirectResponse("/chat", status_code=302)
    c = _container(request)
    ctx = _base_ctx(request, user, "config")
    ctx.update({
        "config_json": json.dumps(_normalize_config(c), ensure_ascii=False),
        "perm_config": {
            "roleOptions": _role_views(c),
            "collectionOptions": sorted(
                {col for p in c.config.permissions for col in p.collections}
                - {"*"} | {"default"}),
        },
        "runtime_meta": {"version": c.config.version,
                         "roleCount": len(c.config.permissions)},
    })
    return templates.TemplateResponse("config.jinja2", ctx)


# ═════════════════ 运行监控（实时状态快照） ═════════════════
#
# 口径统一（TS-014 / TS-015）：本页每一条服务结论都必须与「配置页测试连接」
# 和「容器启动自检」出自同一份预算、同一句原因文案 ——
#   · 适配器自带 health_probe（MySQL / ES）→ 用它自己的预算与原因；
#   · 没有 health_probe 的 → health_check() 外包一层统一兜底预算；
#   · Redis → ping() + redis_failure_reason()（与容器自检同一份翻译）；
#   · degraded 原因取容器的 SECTION_DEGRADED_KEYS 口径（含 vector/vector_store、
#     graph/knowledge_graph 这两套历史键），否则会出现「监控说已恢复、
#     配置页还挂着降级」的自相矛盾。
# 三条链路若各写各的超时，同一个服务会在三个界面给出三种结论，
# 「监控说挂了、配置页说通了」就再也无法解释。
#
# 本页还有一件别处办不到的事：把"运行期掉线"写回容器（container.mark_section_down）。
# 容器只在启动自检与配置保存时探依赖，"启动时健康、之后掉线"的段不会留下任何
# 痕迹 —— 不登记，后台自愈（_recovery_candidate）就认为它没失联，那一行只能红着
# 等重启，检索路也照旧谎报可用。每次刷新都带真实探测结论的，只有本页。

_MONITOR_PROBE_BUDGET_SEC = 6.0

# 监控轮询的单行预算：只决定「这一行此刻怎么显示」，不改适配器自己的预算
# ────────────────────────────────────────────────────────────────────────
# 适配器的 HEALTH_BUDGET_SEC（MySQL 12s / ES 10s / 向量模型 10s / 向量库 8s …）是与
# **配置页「测试连接」**共用的口径：那里是用户点一次、等一个明确结论，宽一点是对的。
# 但监控页是"每 10 秒看一次全局"，同一个宽预算在这里意味着——只要有一个依赖挂起，
# 整轮快照就得等它十几秒，而"监控页恰好在故障时最慢"是本末倒置：用户正是因为
# 怀疑出事才打开它，却拿不到任何反馈。
#
# 所以另给一个只有本页用的**显示预算**：超了就按「未在预算内返回」显示。对监控而言，
# "4 秒还没回"本身就是有效结论（此刻用户自己的查询也一样会卡住）。
# 关键的两点都不改：
#   · 探测任务**不被取消**（asyncio.shield），它在后台按适配器自己的预算跑完，照常
#     mark_section_down → 后台自愈接手。于是"慢但健康"的服务不会被误登记成掉线
#     （登记用的仍是适配器自己的判定，与配置页同口径，TS-015），而真正挂死的也
#     依然留下痕迹、依然有人来救；
#   · 只影响本页这一行的显示，不影响任何其它链路。
_MONITOR_ROW_BUDGET_SEC = 4.0

# 快照缓存 TTL：TTL 内的轮询直接复用上一次采样结果
# 默认轮询 10 秒，故多数轮询仍会重采；它挡掉的是"同一瞬间的重复请求"——
# 打开页面 + 前端紧接着拉一次、多个标签页、连点刷新。这些请求若各采一遍，
# 就是对着同一批依赖重复探测，而探测本身就是被监控对象的负载。
_MONITOR_SNAPSHOT_TTL_SEC = 15.0

# 首屏可以回放的最近采样最大年龄：超过就不下发，让前端显示「采集中」并自己去取。
# 回放的价值是"刚看过这一页，再进来不该闪一下"；一小时前的快照回放出来，
# 第一眼就是过期结论，那比空白更糟（页面上的采样时刻要读到第二眼才看得到）。
_MONITOR_PAGE_REPLAY_MAX_SEC = 60.0

# 被显示预算切断、但仍在后台按完整预算跑完的探测任务：持强引用防 GC
# （不持引用会被回收，任务半途消失，还会打一串 "Task was destroyed"）
_MONITOR_PENDING: set = set()

# 单行在飞的探测（key → Task）：一轮快照被预算切断后，那一行的探测还在后台跑，
# 下一轮轮询不该对同一个依赖再叠一轮 —— 被叠加的往往正是已经卡住的那个，
# 而"别给坏服务加压"正是这次改造的目的之一。命中就复用同一个任务。
_MONITOR_INFLIGHT: dict = {}

# 进程启动时刻：本模块随 create_app 一同导入，近似等于服务启动时间
_MONITOR_STARTED_AT = time.time()

# 本地替身实现名：命中即代表「这个能力没连外部服务，只是本地占位」
_MONITOR_LOCAL_IMPLS = frozenset({"mock", "memory", "dev", "local_fs", "none"})

# 上面这批替身里「完全没有网络端点」的几个：这类行的地址只是**配置里留着**的那
# 一个（--noconnection 下 llm.adapter=mock 但 base_url 仍是真实地址、
# meta.adapter=memory 但 host/port 仍是真实库）。
# 地址照报，但把 endpointInUse 标成 false，由前端写成「配置端点（当前未使用）」——
# 抹成 "—" 会让排障第一步要核对的地址凭空消失（用户只能回配置页翻）。
# local_fs 的本地目录、dev 的认证模式本身就是有意义的描述，不在此列。
_MONITOR_NETWORK_FREE_LOCALS = frozenset({"mock", "memory", "none"})

# 监控页分组（元组顺序即页面顺序）
# infra 里放的都是「进程内能兜底」的基础组件：缓存 → 进程内存、对象存储 →
# 本地文件系统、认证 → 进程内实现，故说明统一写成「退化为进程内本地实现」。
_MONITOR_GROUPS = (
    ("core", "核心依赖", "缺失时自动降级为本地实现，能力受限但不阻断启动"),
    ("retrieval", "检索与存储组件", "可选依赖，不可用时对应检索路自动关闭"),
    ("infra", "基础设施", "基础组件，不可用时退化为进程内本地实现"),
)

def _dsn_summary(dsn: str) -> str:
    """DSN 摘要：抹掉用户名与口令，只留方言与主机库名（监控页不外泄凭据）"""
    if not dsn:
        return "—"
    return re.sub(r"://[^@/]*@", "://***@", str(dsn))


def _monitor_endpoint(key: str, cfg) -> str:
    """端点摘要：只给定位信息，绝不带 password / api_key / secret_key"""
    try:
        if key == "meta":
            return f"{cfg.host}:{cfg.port}/{cfg.database}"
        if key == "redis":
            return f"{cfg.host}:{cfg.port}/{cfg.db}"
        if key == "storage":
            return (cfg.local_root if cfg.adapter == "local_fs"
                    else f"{cfg.endpoint}/{cfg.bucket}")
        if key == "vector_store":
            return (f"{cfg.host}:{cfg.port}/{cfg.database}"
                    if cfg.adapter == "pgvector" else f"{cfg.host}:{cfg.port}")
        if key == "fulltext":
            return "，".join(cfg.hosts or []) or "—"
        if key == "knowledge_graph":
            return cfg.uri or "—"
        if key in ("llm", "embedding"):
            return cfg.base_url or "—"
        if key == "synonym":
            return cfg.url or cfg.file or "—"
        if key == "auth":
            # 只有走 OIDC 才有"端点"可言；dev 模式下把实现名当端点显示，
            # 会在 ⓘ 里多一句"端点：dev"—— 实现的方言名不是网络位置，别混着说。
            return cfg.oidc_issuer if cfg.adapter == "oidc" else ""
        if key == "business_data":
            return _dsn_summary(cfg.dsn)
    except Exception:
        return "—"
    return "—"


# 模型名前面那个名词：与「基础服务」第一列的槽位名对得上，才能一眼看出是"谁的模型"。
# LLM 行讲的是主模型（改写/摘要另说），向量模型行讲的是 embedding 模型。
_MODEL_NOUN = {"llm": "主模型 ", "embedding": "向量模型 "}


def _monitor_model(key: str, cfg, runtime: str) -> tuple[str, str]:
    """LLM / 向量模型行的「此刻在跑哪个模型」→ (短名, tooltip 明细)

    方言名解决"用哪个实现"，这里补"该实现加载了哪个模型"：配
    openai_compatible 时真正决定行为的是 model 字段，只报实现名等于把最该
    确认的那一项留在配置页里。

    本地替身（mock / memory…）不报模型名：--noconnection 或降级之后
    llm.adapter=mock，但 cfg.model 仍是配置里那个真实模型，照着念会把
    内置演示实现说成"qwen38-27b"，与同一行的 mock badge 直接打架。
    但「配置了真实模型、此刻跑的却是本地替身」这件事必须说出来 —— 否则
    用户只看到一行 mock，不知道配置里的模型去哪了（见下 local 分支）。
    """
    if key not in ("llm", "embedding"):
        return "", ""
    model = str(getattr(cfg, "model", "") or "").strip()
    local = runtime in _MONITOR_LOCAL_IMPLS
    if local:
        # 短名留空（此刻在跑的不是它，不能当运行结果显示），但**必须**在 ⓘ 里
        # 交代配置里的模型去哪了，否则第二列只剩一个 mock，用户分不清是配置丢了
        # 还是被降级了。model 为空也要说："未配置"本身就是一种需要看见的配置状态
        # （本项目的 embedding 就长期没配 model，静默省掉 ⓘ 等于把那行留成哑巴）。
        # 唯一无话可说的是配置里写的就是 mock：那没有"配置 vs 实际"的落差。
        if model == runtime:
            return "", ""
        who = (f"{_MODEL_NOUN[key]}{model}" if model
               else f"未配置{_MODEL_NOUN[key].strip()}")
        return "", (f"{who}，当前实际运行 {runtime}"
                    "（本地替身，未连接外部模型服务）")
    if not model:
        return "", ""
    if key == "embedding":
        dim = getattr(cfg, "dim", 0)
        title = f"向量模型 {model}"
        if dim:
            title += f" · 输出维度 {dim}（须与向量库 collection 一致）"
        return model, title
    # LLM 按 task 路由到三个模型：只报主模型容易被读成"改写/摘要没生效"
    rewrite = str(getattr(cfg, "rewrite_model", "") or "").strip()
    summary = str(getattr(cfg, "summary_model", "") or "").strip()
    return model, (f"主模型 {model} · 改写 {rewrite or '同主模型'}"
                   f" · 摘要 {summary or '同主模型'}")


async def _monitor_probe(adapter) -> dict:
    """带预算探测单个适配器：health_probe 优先（自带预算与原因文案）

    probe 字段区分三种来源：
      live    —— 真做了网络/引擎探测；
      static  —— 适配器既无 health_probe 也无 health_check（JWT/Dev 认证、
                 文件同义词表），进程内实现无可探测，如实标注而不伪报「在线」。
    """
    probe = getattr(adapter, "health_probe", None)
    check = getattr(adapter, "health_check", None)
    if probe is None and check is None:
        return {"online": True, "latencyMs": None, "probe": "static",
                "message": "进程内实现，无独立健康探针（不做网络探测）"}
    t0 = time.perf_counter()
    try:
        if probe is not None:
            ok, message = await probe()
        else:
            ok = bool(await asyncio.wait_for(
                check(), timeout=_MONITOR_PROBE_BUDGET_SEC))
            message = ""
    except asyncio.TimeoutError:
        ok, message = False, f"探测超时（{_MONITOR_PROBE_BUDGET_SEC:g}s 预算内未返回）"
    except Exception as e:
        ok, message = False, f"探测异常：{str(e)[:180]}"
    return {"online": bool(ok), "probe": "live",
            "latencyMs": round((time.perf_counter() - t0) * 1000, 1),
            "message": "" if ok else (message or "健康检查未通过（适配器未给出原因）")}


async def _monitor_probe_redis(c: ServiceContainer) -> dict:
    """Redis 探测：ping + 容器自检同一份失败翻译（区分认证/库越界/不通/超时）"""
    from rag.adapters.redis_cache import (REDIS_HEALTH_BUDGET_SEC,
                                          redis_failure_reason)
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(c.redis.ping(), timeout=REDIS_HEALTH_BUDGET_SEC)
        return {"online": True, "probe": "live",
                "latencyMs": round((time.perf_counter() - t0) * 1000, 1),
                "message": ""}
    except Exception as e:
        return {"online": False, "probe": "live",
                "latencyMs": round((time.perf_counter() - t0) * 1000, 1),
                "message": redis_failure_reason(e, c.config.redis)}


def _monitor_service_specs(c: ServiceContainer) -> tuple:
    """(监控键, 中文名, 分组, 配置段, 适配器实例) —— 顺序即页面顺序

    中文名一律**只写到槽位**（不带方言也不带括号别名）：实现名由同一行的 badge
    按**实际运行**的实现给出，标题再拼一份就是多余的第二口径。槽位名才是这一行
    稳定的身份 —— 底下的实现换成别的产品、或降级成本地替身，标题都还是
    「元数据库」，而 badge 会如实改名。
    """
    cfg = c.config
    return (
        ("llm", "LLM 大模型", "core", cfg.llm, c.llm),
        ("embedding", "向量模型", "core", cfg.embedding, c.embedding),
        ("meta", "元数据库", "core", cfg.meta, c.meta),
        ("fulltext", "全文检索", "retrieval", cfg.fulltext, c.fulltext),
        ("vector_store", "向量库", "retrieval", cfg.vector_store, c.vector),
        ("knowledge_graph", "知识图谱", "retrieval",
         cfg.knowledge_graph, c.graph),
        ("business_data", "业务数据库", "retrieval",
         cfg.business_data, c.business),
        ("synonym", "同义词表", "retrieval", cfg.synonym, c.synonym),
        ("storage", "对象存储", "infra", cfg.storage, c.storage),
        ("redis", "缓存", "infra", cfg.redis, c.redis),
        ("auth", "认证服务", "infra", cfg.auth, c.auth),
    )


async def _monitor_snapshot(c: ServiceContainer) -> dict:
    """一次性采集：支撑服务探测 + 检索路/入库就绪度 + 工作流编排"""
    cfg = c.config

    async def _probe_row(spec: tuple) -> dict:
        """探一次这一行，并把「运行期掉线」写回容器

        与"这一行怎么显示"分开：显示可以被本页的显示预算切断（见
        _MONITOR_ROW_BUDGET_SEC），而探测结论与它带来的自愈不能。
        """
        key, label, _category, sec, adapter = spec
        enabled = bool(getattr(sec, "enabled", True))
        if adapter is None:
            if not enabled:
                message = "已禁用（配置未启用）"
            elif cfg.noconnection:
                message = "演示模式未连接（--noconnection 下不接外部依赖）"
            else:
                message = "未初始化（启动自检未通过，已自动关闭）"
            return {"online": False, "latencyMs": None, "probe": "skipped",
                    "message": message}
        if key == "redis":
            result = await _monitor_probe_redis(c)
        else:
            result = await _monitor_probe(adapter)
        # 运行期掉线（实例还在、只是探不通）→ 把探测结论写回容器
        # （实例已被摘掉的走上面 "未初始化" 分支：那是部署期结论，不在此列）
        # 不登记的话，这一行只会红着不动：degraded 里没有它，_recovery_candidate()
        # 两个条件都不成立 → 后台自愈永远跳过它，向量路也照旧算可用（见
        # container._vector_model_ready）。登记之后这一行才会自愈、检索路才会跟着关。
        if (enabled and not cfg.noconnection and result["probe"] == "live"
                and not result["online"]):
            c.mark_section_down(key, label, str(result["message"] or ""))
        return result

    def _spawn_probe(spec: tuple):
        """起一个探测任务并持强引用：被显示预算切断后它仍要在后台跑完"""
        key = spec[0]
        task = _MONITOR_INFLIGHT.get(key)
        if task is not None and not task.done():
            return task          # 上一轮那次还在跑，结果就是上一轮要的答案
        task = asyncio.create_task(_probe_row(spec))
        _MONITOR_INFLIGHT[key] = task
        _MONITOR_PENDING.add(task)

        def _done(t):
            _MONITOR_PENDING.discard(t)
            if _MONITOR_INFLIGHT.get(key) is t:
                _MONITOR_INFLIGHT.pop(key, None)
            if not t.cancelled() and t.exception() is not None:
                # 后台任务没人 await，异常必须在这里取走：否则 GC 时只会打一句
                # "Task exception was never retrieved"，日志里看不出是哪一行出的
                log.warning("monitor_probe_background_failed",
                            error=str(t.exception())[:200])

        task.add_done_callback(_done)
        return task

    async def _row(spec: tuple) -> dict:
        key, label, category, sec, adapter = spec
        enabled = bool(getattr(sec, "enabled", True))
        retry = None
        try:
            # shield：超预算时放弃的是"等"，不是探测本身（见 _MONITOR_ROW_BUDGET_SEC）
            result = await asyncio.wait_for(
                asyncio.shield(_spawn_probe(spec)),
                timeout=_MONITOR_ROW_BUDGET_SEC)
        except asyncio.TimeoutError:
            result = {
                "online": False, "latencyMs": None, "probe": "live",
                "timedOut": True,
                "message": (f"未在 {_MONITOR_ROW_BUDGET_SEC:g}s 内返回，本次按超时计"
                            "（探测仍在后台按该服务的完整预算继续，结论落地后会自动"
                            "登记，无需手工重试）"),
            }
        except Exception as e:                 # 探测自身异常，不牵连同轮其它行
            result = {"online": False, "latencyMs": None, "probe": "live",
                      "message": f"探测异常：{str(e)[:180]}"}
        # 后台自愈的重试进度：失联的段由它接手，把「试了几次、最近一次什么时候」
        # 报出来。否则用户看到状态长时间不变，只会以为程序根本没在管，
        # 又得靠重启/重存配置去猜。
        if (not result["online"] and enabled and not cfg.noconnection
                and key in RECOVERABLE_SECTIONS):
            stat = (c.recovery_stats or {}).get(key)
            if stat and stat.get("attempts"):
                last = datetime.fromtimestamp(stat["lastAt"]).astimezone()
                retry = {
                    "attempts": int(stat["attempts"]),
                    "at": last.isoformat(timespec="seconds"),
                    "atText": last.strftime("%H:%M:%S"),
                    "intervalSec": int(RECOVER_INTERVAL_SEC),
                }
        # 配置名 ≠ 运行名：降级后实例已经换成 mock/memory，
        # 展示必须读运行名，否则会把本地 mock 当成真实外部服务汇报。
        configured = str(getattr(sec, "adapter", "") or "")
        runtime = AdapterRegistry.name_of(key, adapter) or configured
        # 缓存（Redis）是唯一不进注册表的槽位：只有一个实现，配置段里也就没有
        # adapter 字段可选（见 RedisConfig），于是上面两句双双取空 → 第二列成了
        # 全表唯一一格 '—'，与其它行"实例不在，就报配置里那个注册名"的规则不一致。
        # 固定报 redis：它确实是外部依赖（不在 _MONITOR_LOCAL_IMPLS 里，徽章照常
        # 按"真实实现"上色），而这个短名正是配置页与日志里对它的称呼。
        if key == "redis" and not runtime:
            runtime = "redis"
        # 标题不拼方言：badge 已经按运行名给出实现，标题只保留槽位名（见
        # _monitor_service_specs）。两条信息各自只有一个出处，就不会打架。
        # 模型名同样只认运行名：降级成 mock 时它必须消失，不能把配置里的
        # 真实模型名挂在演示实现旁边（同 badge 的道理）
        model, model_title = _monitor_model(key, sec, runtime)
        endpoint = _monitor_endpoint(key, sec)
        # 端点一律报「配置里写了什么」，只把"此刻是不是它"另标一位。
        # 降级成替身（milvus→memory）、未启用、演示模式跳过、未初始化 —— 都只是
        # "没用它"，不是"没配"：地址抹掉就等于把排障要核对的第一项藏起来。
        # 误读风险由文字承担（前端把它写成「配置端点（当前未使用）」），
        # 不再靠隐藏信息来避免 —— 那是拿"看不到"换"不会误读"。
        endpoint_in_use = bool(
            enabled and adapter is not None
            and runtime not in _MONITOR_NETWORK_FREE_LOCALS)
        reason = next((c.degraded[k]
                       for k in SECTION_DEGRADED_KEYS.get(key, (key,))
                       if k in c.degraded), "")
        return {
            "key": key, "label": label, "category": category,
            "adapter": runtime,
            "configuredAdapter": configured,
            # 显示模型名还是注册名，由前端按"有没有模型名"决定，后端不再另发
            # 开关字段：同一件事有两个出处，迟早会打架
            "model": model,
            "modelTitle": model_title,
            "endpoint": endpoint,
            # 端点仍是"配置值"还是"此刻在用的地址"：由前端决定措辞
            "endpointInUse": endpoint_in_use,
            "enabled": enabled,
            "probe": result["probe"],
            "online": result["online"],
            "latencyMs": result["latencyMs"],
            "message": result["message"],
            # 本页显示预算切断（≠ 对端失联）：前端要把它与"连不上"分开说，
            # 否则用户会去重试/重启一个其实只是慢的依赖
            "timedOut": bool(result.get("timedOut")),
            "localImpl": runtime in _MONITOR_LOCAL_IMPLS,
            "degraded": bool(reason),
            "degradedReason": reason,
            "retry": retry,
        }

    services = list(await asyncio.gather(*(_row(s)
                                          for s in _monitor_service_specs(c))))
    # 「未启用」与「启用了但连不上」必须分开算：把 4 条主动关掉的检索路算进
    # offline 会让总览第一眼就显示成大面积异常，用户对真正的故障反而麻木。
    active = [s for s in services if s["enabled"]]
    core = [s for s in active if s["category"] == "core"]
    problems = [s for s in active if not s["online"]]

    # 检索路：配置启用 ∩ 适配器在线，差集就是「配了但连不上」的那几条
    enabled_paths = sorted(cfg.enabled_paths())
    live_paths = sorted(c.enabled_paths())
    lost_paths = [p for p in enabled_paths if p not in live_paths]

    try:
        wf = c.workflows.describe() if c.workflows is not None else {}
    except Exception:
        wf = {}
    ingest_wf = wf.get("ingest") or {}

    return {
        "status": "ok" if not problems and not c.degraded else "degraded",
        "checkedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app": {
            "name": cfg.app_name,
            "version": cfg.version,
            "noconnection": bool(cfg.noconnection),
            "configPath": str(getattr(cfg, "_config_path", "") or ""),
            "startedAt": datetime.fromtimestamp(_MONITOR_STARTED_AT)
                                  .astimezone().isoformat(timespec="seconds"),
            "uptimeSeconds": int(time.time() - _MONITOR_STARTED_AT),
        },
        # 后台自愈开关与节奏：文案由服务端给，免得前端把 30 秒写死后与后端漂移
        "recovery": {
            "enabled": not bool(cfg.noconnection),
            "intervalSec": int(RECOVER_INTERVAL_SEC),
        },
        "summary": {
            "total": len(services),
            "enabledTotal": len(active),
            "disabled": len(services) - len(active),
            "online": sum(1 for s in active if s["online"]),
            "offline": len(problems),
            "realOnline": sum(1 for s in active
                              if s["online"] and not s["localImpl"]),
            "localImpl": sum(1 for s in active if s["localImpl"]),
            "static": sum(1 for s in active if s["probe"] == "static"),
            "degraded": sum(1 for s in services if s["degraded"]),
            "coreOnline": sum(1 for s in core if s["online"]),
            "coreTotal": len(core),
            "livePaths": len(live_paths),
            "enabledPaths": len(enabled_paths),
            "lostPaths": len(lost_paths),
        },
        "groups": [{"key": k, "label": lb, "hint": hint}
                   for k, lb, hint in _MONITOR_GROUPS],
        "services": services,
        "readiness": {
            "retrieval": {
                "enabled": [{"name": p, "description": _PATH_DESC.get(p, p)}
                            for p in enabled_paths],
                "live": [{"name": p, "description": _PATH_DESC.get(p, p)}
                         for p in live_paths],
                "lost": [{"name": p, "description": _PATH_DESC.get(p, p),
                          "reason": _lost_path_reason(c, p)}
                         for p in lost_paths],
            },
            "ingest": dict(_queue_view(c), concurrency=cfg.ingest.concurrency,
                           maxRetries=cfg.ingest.max_retries,
                           verifySampleSize=cfg.ingest.verify_sample_size),
            "workflows": {
                "ingestNames": sorted(ingest_wf.keys()),
                "querySteps": list(wf.get("query") or []),
            },
            "consistencyCheck": {
                "enabled": cfg.consistency_check.enabled,
                "intervalHours": cfg.consistency_check.interval_hours,
                "sampleDocs": cfg.consistency_check.sample_docs,
                "alertThreshold": cfg.consistency_check.alert_threshold,
            },
            "rerank": {
                "enabled": bool(cfg.retrieval.rerank_enabled),
                # 这一处给**拼好的权重目录**而不是裸露的模型ID：监控页要回答的是
                # "业务实际在跑哪份权重"，配置页才需要把「模型路径」与「模型ID」分开
                "model": resolve_rerank_model_path(
                    getattr(cfg.retrieval, "rerank_model_dir", ""),
                    cfg.retrieval.rerank_model),
                "device": cfg.retrieval.rerank_device,
            },
            "observability": {
                "logLevel": cfg.observability.log_level,
                "metricsEnabled": cfg.observability.metrics_enabled,
                "pushgateway": cfg.observability.pushgateway_url,
                "tracingEnabled": cfg.observability.tracing_enabled,
            },
        },
        "degraded": dict(c.degraded),
    }


def _monitor_replay(c: ServiceContainer):
    """最近一次采样：够新就随首屏下发，否则 None（页面不等它）"""
    snap = c.monitor_snapshot
    if snap is None:
        return None
    age = time.time() - c.monitor_snapshot_at
    if age > _MONITOR_PAGE_REPLAY_MAX_SEC:
        return None
    # 与 /overview 同口径带上"离现在多久"：回放的有可能是几十秒前的采样，
    # 不标出来的话，首屏那句"最后更新 HH:MM:SS"会被读成此刻的状态。
    return dict(snap, sampledAgoSec=round(age, 1))


async def _monitor_snapshot_cached(c: ServiceContainer, *,
                                   force: bool = False) -> tuple:
    """取快照：TTL 内复用最近一次采样，过期则单飞重采一次

    返回 (快照, 该快照的年龄秒数)。年龄由服务端算——快照自带的 checkedAt 说
    "什么时候采的"，年龄说"离现在多久"，后者不该由浏览器拿本地时钟去减
    （两端时钟差多少，显示的"多久以前"就错多少）。

    force=True（用户点了「立即刷新」/ 改了刷新间隔）：跳过 TTL 现采一次，但**仍然
    单飞**——与正在跑的那一轮合并。否则连点几次就是几轮全量探测打在同一批依赖上。
    """
    if c.monitor_snapshot is not None and not force:
        age = time.time() - c.monitor_snapshot_at
        if age < _MONITOR_SNAPSHOT_TTL_SEC:
            return c.monitor_snapshot, age
    async with c.monitor_lock():
        # 等锁期间别人可能已经采完（"打开页面 + 前端紧接着取一次"正是这个形状），
        # 二次判 TTL：否则排完队又白采一轮
        if c.monitor_snapshot is not None and not force:
            age = time.time() - c.monitor_snapshot_at
            if age < _MONITOR_SNAPSHOT_TTL_SEC:
                return c.monitor_snapshot, age
        snap = await _monitor_snapshot(c)
        c.monitor_snapshot = snap
        c.monitor_snapshot_at = time.time()
        return snap, 0.0


@pages_router.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request, user: UserContext = Depends(_page_user)):
    """运行监控页（仅管理员）：支撑服务健康与业务就绪度，前端定时刷新

    **本路由不采样**：采样里最慢的那一项按它自己的预算走（可能十几秒），放在渲染
    之前等于"打开页面耗时 = 最慢依赖的探测预算"——而这个页面恰恰是在依赖出问题时
    才被打开的，把等待堆在最需要它快的时刻是本末倒置。

    页面因此立即返回，数据由前端 /overview 拉（首次请求会命中同一份采样，见
    _monitor_snapshot_cached 的单飞）；只有最近刚采过（_MONITOR_PAGE_REPLAY_MAX_SEC
    内）才把结果嵌进 HTML，顺带省掉首屏那一次闪烁，否则下发空值，前端先显示
    「采集中」——占位态不带任何结论，不会把"数据未到"渲染成"服务不可用"。
    """
    if not user.is_admin:
        return RedirectResponse("/chat", status_code=302)
    c = _container(request)
    ctx = _base_ctx(request, user, "monitor")
    ctx.update({
        "runtime_meta": {"version": c.config.version,
                         "appName": c.config.app_name},
        # 回放最近一次采样（可能为空）；为空时前端自己去取，TTL 内仍会命中同一份
        "initial_snapshot": _monitor_replay(c),
    })
    return templates.TemplateResponse("monitor.jinja2", ctx)


@admin_ui_router.get("/monitor/overview")
async def monitor_overview(request: Request, live: int = 0,
                           user: UserContext = Depends(require_admin)):
    """运行监控快照（管理员）：供监控页轮询，口径同配置页测试连接

    live=1 强制现采（用户主动刷新时用）；默认 TTL 内复用上一次采样结果。
    """
    snap, age = await _monitor_snapshot_cached(_container(request),
                                              force=bool(live))
    # 浅拷贝：sampledAgoSec 属于这一次响应，不能写进被缓存的那份快照里
    payload = dict(snap, sampledAgoSec=round(age, 1))
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


# ───────────────────── UI 补充 API ─────────────────────

@ui_router.get("/collections")
async def list_collections(request: Request,
                           user: UserContext = Depends(get_current_user)):
    c = _container(request)
    return {"collections": [{"name": n, "displayName": n}
                            for n in _collection_names(c, user)]}


@ui_router.get("/retrieval-paths")
async def retrieval_paths(request: Request,
                          user: UserContext = Depends(get_current_user)):
    c = _container(request)
    enabled = set(c.enabled_paths())
    weights = c.config.retrieval.default_route_weights or {}
    return {"paths": [
        {"name": n, "description": _PATH_DESC.get(n, n),
         "enabled": n in enabled, "defaultWeight": weights.get(n)}
        for n in sorted(set(_PATH_DESC) | enabled)]}


@ui_router.put("/sessions/{session_id}")
async def rename_session(session_id: str, request: Request,
                         user: UserContext = Depends(get_current_user)):
    body = await request.json()
    title = (body.get("title") or "").strip()[:120]
    if not title:
        raise HTTPException(400, "标题不能为空")
    c = _container(request)
    st = await c.memory_service.load_session(session_id)
    if st is None or (st.user_id != user.user_id and not user.is_admin):
        raise HTTPException(404, "会话不存在")
    st.title = title
    await c.memory_service.save_session(st)
    return {"ok": True, "session_id": session_id, "title": title}


@ui_router.post("/sessions/{session_id}/title")
async def rename_session_alias(session_id: str, request: Request,
                               user: UserContext = Depends(get_current_user)):
    return await rename_session(session_id, request, user)


@ui_router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, request: Request,
                     user: UserContext = Depends(get_current_user)):
    c = _container(request)
    task = await c.meta.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status not in (IngestStatus.FAILED, IngestStatus.PARTIAL):
        return {"ok": False, "error": "仅失败或部分完成的任务支持重试",
                "status": task.status.value}
    fresh = await c.ingest_coordinator.requeue_task(task_id)
    if fresh is None:
        return {"ok": False, "error": "重试失败"}
    return {"ok": True, "task_id": task_id, "status": fresh.status.value}


@ui_router.post("/ingest/tasks/{task_id}/retry")
async def retry_task_alias(task_id: str, request: Request,
                           user: UserContext = Depends(get_current_user)):
    return await retry_task(task_id, request, user)


@ui_router.get("/documents/{doc_id}/chunks")
async def list_doc_chunks(doc_id: str, request: Request, page: int = 1,
                          size: int = 12,
                          user: UserContext = Depends(get_current_user)):
    c = _container(request)
    try:
        ids = await c.meta.list_chunk_ids(doc_id)
    except Exception:
        ids = []
    total = len(ids)
    page = max(1, page)
    size = min(max(1, size), 50)
    window = ids[(page - 1) * size: page * size]
    metas = []
    texts: dict = {}
    if window:
        try:
            metas = await c.meta.get_chunks_by_ids(window)
        except Exception:
            metas = []
        try:
            texts = await c.meta.get_chunk_texts(window)
        except Exception:
            texts = {}
    m = {x.chunk_id: x for x in metas}
    items = []
    for cid in window:
        cm = m.get(cid)
        items.append({
            "chunk_id": cid,
            "chunk_type": cm.chunk_type if cm else "text",
            "section_path": (cm.section_path if cm else "") or "",
            "page": cm.page_num if cm else None,
            "tokens": cm.token_count if cm else 0,
            "quality_score": getattr(cm, "quality_score", None) if cm else None,
            "figure_label": getattr(cm, "figure_label", None) if cm else None,
            "figure_caption": getattr(cm, "figure_caption", None) if cm else None,
            "text": texts.get(cid, ""),
        })
    return {"items": items, "total": total}


@ui_router.get("/documents/{doc_id}/content")
async def doc_content(doc_id: str, request: Request,
                      user: UserContext = Depends(get_current_user)):
    c = _container(request)
    doc = await c.meta.get_document(doc_id, user.tenant_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if not doc.storage_url:
        raise HTTPException(404, "文档未落盘")
    if c.storage is None:
        raise HTTPException(503, "存储适配器不可用")
    try:
        data = await c.storage.get(doc.storage_url)
    except Exception as e:
        raise HTTPException(404, f"无法读取文档: {e}")
    ft = (doc.file_type or "").lower()
    if ft in {"txt", "md", "csv", "html", "json"}:
        content = data.decode("utf-8", errors="replace")
        if len(content) > 50000:
            return JSONResponse({"ok": False, "reason": "too_large",
                                 "preview": content[:50000]})
        return Response(content, media_type="text/plain; charset=utf-8")
    if ft in {"png", "jpg", "jpeg"}:
        mime = "image/png" if ft == "png" else "image/jpeg"
        return Response(data, media_type=mime)
    raise HTTPException(415, "该文件类型不支持在线预览")


@ui_router.post("/documents/{doc_id}/restore")
async def restore_document(doc_id: str, request: Request,
                           user: UserContext = Depends(require_admin)):
    c = _container(request)
    doc = await c.meta.get_document(doc_id, user.tenant_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    await c.meta.update_doc_status(doc_id, IngestStatus.DONE)
    return {"ok": True, "status": "done"}


@ui_router.delete("/documents/{doc_id}/permanent")
async def permanent_delete_document(doc_id: str, request: Request,
                                    user: UserContext = Depends(require_admin)):
    c = _container(request)
    doc = await c.meta.get_document(doc_id, user.tenant_id)
    if doc is None:
        return {"ok": True}
    # 五库物理清理（meta 级联删 chunks）
    if c.fulltext is not None:
        await c.fulltext.delete_by_doc(doc.collection, doc.doc_id)
    if c.vector is not None:
        await c.vector.delete_by_doc(doc.collection, doc.doc_id)
    if c.graph is not None:
        await c.graph.delete_by_doc(doc.tenant_id, doc.doc_id)
    if doc.storage_url and c.storage is not None:
        try:
            await c.storage.delete(doc.storage_url)
        except Exception as e:
            log.warning("storage_delete_failed", doc_id=doc.doc_id, error=str(e))
    try:
        await c.meta.delete_document(doc.doc_id, user.tenant_id)
    except Exception as e:
        log.warning("permanent_delete_meta_failed", doc_id=doc.doc_id, error=str(e))
    return {"ok": True}


# ───────────────────── 管理 API ─────────────────────

# 说明：GET /api/admin/config 已由 rag/api/routes/admin.py 提供（只读脱敏视图）。
# 配置页统一载荷通过页面模板注入（__CONFIG_INIT__），保存走下方 POST。

@admin_ui_router.post("/config")
async def save_config(request: Request,
                      user: UserContext = Depends(require_admin)):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")
    c = _container(request)
    update = _yaml_update_from_payload(payload, sorted(c.enabled_paths()))
    cfg_path = Path(c.config._config_path or "customer/customer_config.yaml")
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path
    if not cfg_path.exists():
        # 兜底：默认客户配置路径
        alt = Path("customer/customer_config.yaml")
        if alt.exists():
            cfg_path = alt
        else:
            raise HTTPException(500, "配置文件不存在")
    # 读盘 → 解密 → 合并 → 加密写盘，统一走 loader.merge_config_file（TS-023）。
    # 曾经在这里直接 yaml.safe_dump，既会把凭据明文写回，又会用界面回传的空值
    # 覆盖掉未修改的真凭据（不可逆），所以这段逻辑只允许有一个实现。
    try:
        from rag.config.loader import merge_config_file
        merge_config_file(cfg_path, update)
    except Exception as e:
        raise HTTPException(500, f"写入配置文件失败: {e}")

    # 热应用：能只重建本服务就只重建本服务；只有涉及多段/无法单段处理时
    # 才整容器重建（失败一律保留旧容器继续运行，重启后生效）
    applied, degraded, message, scope = False, {}, None, None
    if update:
        try:
            from rag.api.runtime import apply_config_update
            result = await apply_config_update(request.app, update, cfg_path)
            applied = True
            scope = result.get("scope")
            degraded = result.get("degraded") or {}
        except Exception as e:
            message = f"配置已写入文件，但热应用失败（重启后生效）: {e}"
    return {"ok": True, "updatedKeys": sorted(update.keys()),
            "applied": applied, "scope": scope, "degraded": degraded,
            "restartRequired": not applied, "message": message}


# ── 模型库：多套「已测通」的模型并存，选一个作为 active ──
#
# 为什么不做成"保存表单 = 覆盖当前配置"：用户手上常有若干个可用的服务地址
# （线上 DeepSeek、内网 vLLM、本机 Ollama…），覆盖式保存意味着换回来时要重新
# 填一遍地址与 key。库里的条目是"一套连得上的配置"的快照，active 只决定业务
# 用哪一套 —— 切换不重填、不丢 key。


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    return body


def _config_file(c: ServiceContainer) -> Path:
    """配置文件绝对路径（与 save_config 同一套兜底）"""
    path = Path(getattr(c.config, "_config_path", "")
                or "customer/customer_config.yaml")
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        alt = Path.cwd() / "customer/customer_config.yaml"
        if alt.exists():
            return alt
    return path


class _RerankStore:
    """把 retrieval 上的重排模型库伪装成一个"模型段"

    重排模型是本机 Cross-Encoder 权重：库挂在 retrieval 段上（见
    models._sync_rerank_library），库里没有地址与凭据，身份只有模型ID，条目级参数
    只有加载设备。除了这几处，条目结构与另外两段完全一致（同一个 ModelEntry），
    所以三个模型库接口靠这个壳子就能原样复用，一行流程都不用改。
    """

    def __init__(self, ret):
        self._ret = ret

    @property
    def models(self):
        return self._ret.rerank_models

    @property
    def active_id(self):
        return self._ret.rerank_active_id

    @property
    def display_name(self):
        return self._ret.rerank_display_name

    @property
    def model(self):
        return self._ret.rerank_model

    @property
    def model_dir(self):
        return self._ret.rerank_model_dir

    @property
    def device(self):
        return self._ret.rerank_device

    @property
    def base_url(self):
        return ""       # 本机权重没有服务地址：卡片不画这一行，也不参与校验

    @property
    def api_key(self):
        return ""       # 没有凭据可存


def _model_section(request: Request, section: str):
    """校验段名并取出内存里的（明文）模型段配置"""
    if section == "rerank":
        return _RerankStore(_container(request).config.retrieval)
    if section not in _MODEL_SECTION_KEYS:
        raise HTTPException(400, f"未知的模型段：{section or '(空)'}")
    return getattr(_container(request).config, section)


async def _write_model_section(request: Request, section: str,
                               entries: list[ModelEntry],
                               active_id: str) -> dict:
    """把模型库写盘并热应用（三个模型库接口共用）

    提交的是**整段**：entries 列表（loaders._deep_merge 对列表是整段替换）、
    active_id，以及 active 条目的顶层镜像字段。镜像必须一起提交，否则配置文件里
    会留下"顶层写着上一个模型、列表里 active 指着新模型"的自相矛盾状态 ——
    运行期不受影响（加载时按 active 重算顶层），但读文件的人会被误导。
    """
    # 库里可能一条都不剩：文档解析能把最后一张卡片上的最后一个能力删掉（见
    # model_library_delete）。此时没有 active 条目可镜像，顶层字段必须一起清空 ——
    # 留着旧地址的话，下次加载时 models._sync_model_library 会按"顶层还写着地址"
    # 把它复活成一条 legacy 条目，用户明明删了、刷新页面它又回来了
    active = next((e for e in entries if e.id == active_id), None)
    if section == "rerank":
        # 重排库的存储位置与另外两段不同：库挂在 retrieval 上，且没有地址/凭据，
        # 只写几个顶层镜像字段（模型名 / 模型路径 / 模型ID / 加载设备）
        update = {"retrieval": {
            "rerank_models": [e.model_dump(mode="json") for e in entries],
            "rerank_active_id": active_id,
            "rerank_display_name": active.display_name,
            "rerank_model_dir": str(active.params.get("model_dir") or "").strip(),
            "rerank_model": active.model,
            "rerank_device": active.params.get("device") or "cpu",
        }}
    else:
        sect: dict = {"models": [e.model_dump(mode="json") for e in entries],
                      "active_id": active_id if active else ""}
        for k in MODEL_ENTRY_FIELDS:
            sect[k] = getattr(active, k) if active else ""
        if active:
            sect.update(active.params)
        update = {section: sect}

    cfg_path = _config_file(_container(request))
    try:
        from rag.config.loader import merge_config_file
        # preserve_sensitive=False：这里每个凭据都取自内存里的明文真值，没有
        # "界面留空"这回事；保旧值只会把上一条的 key 复活到顶层 —— 一个免密
        # 的本地服务会因此收到一个不相干的旧钥匙
        merge_config_file(cfg_path, update, preserve_sensitive=False)
    except Exception as e:
        raise HTTPException(500, f"写入配置文件失败: {e}")

    applied, degraded, message = False, {}, None
    try:
        from rag.api.runtime import apply_config_update
        result = await apply_config_update(request.app, update, cfg_path)
        applied = True
        degraded = result.get("degraded") or {}
    except Exception as e:
        # 与配置页其它保存一致：写盘成功但热应用失败 → 如实告知，重启后生效
        message = f"已写入配置文件，但热应用失败（重启后生效）: {e}"
    return {"applied": applied, "scope": section, "degraded": degraded,
            "restartRequired": not applied, "message": message}


def _reuse_entries(sect) -> list[ModelEntry]:
    """复制一份库里的条目（改副本，避免写盘失败时内存已被改坏）"""
    return [ModelEntry.model_validate(e.model_dump()) for e in sect.models]


def _norm_endpoint(url: object) -> str:
    """地址比较用的规范化：只判"是不是同一台服务"，末尾的斜杠不算差别"""
    return str(url or "").strip().rstrip("/")


def _check_doc_parse_card(entries: list[ModelEntry], entry_id: str, name: str,
                          endpoint: str, cap: str) -> None:
    """文档解析入库前的校验（卡片 = 模型名，见 _MODEL_SECTIONS 的那段注释）

    1. **不同的模型名必须对应不同的 API 地址**：一张卡片只有一栏地址，两张卡片指着
       同一台服务就是重复配置（想给那台服务添能力，该加在同一张卡片上）；
    2. **同名 = 同一张卡片**：地址必须和卡片上已有的一致，否则这张卡片就有两栏地址；
    3. **同名同能力 = 同一条配置**：拒收 —— 要改的是那一条，不是再加一枚一样的徽标。
    """
    others = [e for e in entries if e.id != entry_id]
    same_name = [e for e in others if (e.display_name or "").strip() == name]
    if same_name:
        owner = str(same_name[0].base_url or "").strip()
        if _norm_endpoint(owner) != _norm_endpoint(endpoint):
            raise HTTPException(
                400, f"模型名「{name}」已在库里，指向 {owner or '（空地址）'}："
                     "同一张卡片只有一个 API 地址，换地址请换个模型名")
        label = doc_parse_capability_label(cap)
        if any(doc_parse_capability((e.params or {}).get("capability")) == cap
               for e in same_name):
            raise HTTPException(
                400, f"「{name}」已有「{label}」能力：同一项能力只存一条，"
                     "要改就点它卡片上那枚徽标")
        return
    clash = next((e for e in others
                  if _norm_endpoint(e.base_url) == _norm_endpoint(endpoint)), None)
    if clash is not None:
        raise HTTPException(
            400, f"API 地址已被模型「{clash.display_name or clash.id}」占用："
                 "不同的模型名必须对应不同的 API 地址；要给这台服务添能力，"
                 f"模型名请填「{clash.display_name or clash.id}」"
                 "（新能力会作为徽标加到它那张卡片上）")


@admin_ui_router.post("/model-library/upsert")
async def model_library_upsert(request: Request,
                               user: UserContext = Depends(require_admin)):
    """把表单里那套「已测通」的配置存入模型库（带 id = 更新，不带 = 新增）

    只接受测通过的配置：没测过就能入库，库里就会出现一批"看着可用、其实没连过"
    的条目 —— 那恰恰是这个列表要解决的问题。
    """
    body = await _json_body(request)
    section = str(body.get("section") or "")
    if not body.get("verified"):
        raise HTTPException(400, "该配置还没有通过「测试模型」，通过后才能存入模型库")

    sect = _model_section(request, section)
    rows = [r for r in (body.get("configParams") or []) if isinstance(r, dict)]
    by_key = {str(r.get("key")): r for r in rows}

    name = str((by_key.get("display_name") or {}).get("value") or "").strip()
    endpoint = str(body.get("endpoint") or "").strip()
    # **第一个模型ID是这条配置实际调用的那个**（表单里的「模型ID」输入框填的就是
    # 它）。界面只发这一个 —— 「获取模型ID」拉回来的候选属于选择手段，不入配置；
    # 但接口不去砍列表：老配置里可能存过多个ID，第 [0] 个仍是调用者。
    # 兜底 model 行：缓存了旧 JS 的页面只发那一行，没有 modelIds
    model_ids = clean_model_ids(body.get("modelIds") or []) \
        or clean_model_ids([(by_key.get("model") or {}).get("value")])
    # 表单里有没有「模型ID」这一行，由字段表决定，不硬编段名：没有那一行的段
    # （文档解析：PaddleX 按能力拆 endpoint，见 _MODEL_SECTIONS 的字段表）就既不
    # 要求它，也不把它写进条目 —— 条目身份由后端生成的 id 承担
    needs_model_id = "model" in _MODEL_SECTION_PARAMS.get(section, ())
    if not name:
        raise HTTPException(400, "模型名必填：它就是列表里显示、也是你用来认它的名字")
    if section == "rerank":
        # 重排没有独立的「API 地址」，那一栏就是「模型路径/API」：留空这条配置谁也
        # 重排不了 —— 运行期只能退到 LLM 重排，而用户以为自己配好了
        if not str((by_key.get("model_dir") or {}).get("value") or "").strip():
            raise HTTPException(
                400, "模型路径/API 必填：填本机权重目录（如 models），"
                     "或远程重排服务地址（http://host:port/v1）")
    elif not endpoint:
        raise HTTPException(400, "API 地址必填：留空表示本机 Mock 模式，无需入库")
    if needs_model_id and not model_ids:
        raise HTTPException(400, "模型ID必填：集合点的地址不足以确定调用哪个模型")

    entries = _reuse_entries(sect)
    entry_id = str(body.get("id") or "")
    if section == "doc_parse":
        # 文档解析的一张卡片 = 一个模型名：同名的几条是**同一张卡片上的几枚能力
        # 徽标**，判据不是"名字 + 模型ID"那套（见 _check_doc_parse_card）
        _check_doc_parse_card(
            entries, entry_id, name, endpoint,
            doc_parse_capability((by_key.get("capability") or {}).get("value")))
    else:
        # 一张卡片由「模型名 + 在用的那个模型ID」共同确定，不是只看名字：同一台机器
        # 上 vLLM 按模型名拆着跑、线上服务商一个 key 下挂好几个模型，都会出现"名字
        # 一样、模型不同"的两条配置 —— 那是两张卡片，不该拒收
        dedup_key = model_ids[0] if model_ids else ""
        dup = next((e for e in entries
                    if e.display_name == name and e.model == dedup_key
                    and e.id != entry_id), None)
        if dup is not None:
            shown = f"{name} / {dedup_key}" if dedup_key else name
            raise HTTPException(
                400, f"已有同名同模型「{shown}」：改那一条，或换个模型名 / 模型ID")

    target = next((e for e in entries if e.id and e.id == entry_id), None)
    is_new = target is None
    if is_new:
        target = ModelEntry(id=new_entry_id())
        entries.append(target)

    # 凭据：界面**下发**的就是解密后的真 key（见 _param_row 的 reveal），
    # 框里通常就是那把钥匙，原样存下来即可。这里仍走 _effective_api_key 统一算，
    # 而不是"留空就原样留着"：新建条目时表单里的留空意味着"用上面那把已经测通的
    # 钥匙"，原样留着就等于把配置存成空 key —— 卡片看着是好的，其实只借了段顶层的
    # 钥匙，下次「更改」时 API Key 框是空的（而且这条一旦被设为 active，顶层镜像
    # 也跟着变空，业务直接失去鉴权）。
    # 改一条时它取那一条自己的 key（与「测试模型」用的是同一把，见 _form_api_key）：
    # 老条目存成空 key 的就保持为空，等用户补填，不从别的条目借。
    # 把框删干净 = 用户主动清空这条的 key；圆点串（旧页面）当"没改"
    api_row = by_key.get("api_key") or {}
    target.api_key = _effective_api_key(
        sect, api_row.get("value"), "" if is_new else target.id,
        cleared=bool(api_row.get("cleared"))) if section != "rerank" else ""
    target.display_name = name
    target.base_url = endpoint
    target.model_ids = model_ids
    target.model = model_ids[0] if model_ids else ""   # 镜像：业务调用的就是第一个
    target.tested_at = time.strftime("%Y-%m-%d %H:%M:%S")
    # 条目参数必须**完整**：缺项会让"切到这条"变成"这一项沿用上一条的值"，
    # 用户看到的就是"切了模型但温度没跟着变"。界面没给的项用当前段值兜底
    params = {k: getattr(sect, k) for k in model_param_fields(section)
              if hasattr(sect, k)}
    for row in rows:
        k = str(row.get("key") or "")
        if k in model_param_fields(section):
            v = _coerce_param(row)
            if v is not None:
                # 白名单型参数（文档解析的处理能力）：归一后才落盘 —— 落一个
                # 打不出去的 endpoint 到配置里，要到真正解析时才炸，还看不出是
                # 谁写坏的（界面下拉只会给合法值，这里防的是手改 YAML / 旧页面）
                if k == "capability":
                    v = doc_parse_capability(v)
                params[k] = v
    if section == "rerank":
        # 模型路径首尾的空白会让拼出来的目录差一个字符（`models ` → `models /xxx`），
        # 界面看不出来、加载时才报路径不存在，在这里一次收口
        params["model_dir"] = str(params.get("model_dir") or "").strip()
    target.params = params

    if section == "doc_parse":
        # 文档解析没有"切到哪一条"（卡片恒 active，见 _entry_view）：active_id 只是
        # 顶层镜像字段的落脚点，谁写在那里都行 —— 库里原来那条还在就继续用它，免得
        # 每存一项能力就把顶层字段挪一次
        active_id = (sect.active_id
                     if any(e.id == sect.active_id for e in entries)
                     else entries[0].id)
        # 响应里的 active 给**本次存下的那一条**：前端据此把表单停在用户刚编辑的
        # 那枚徽标上（见 config-ui.js）。若回 active_id 那条，表单会被弹到库里第一
        # 张卡片上，用户接着再点一次保存就成了新增一条，被同名同能力拒收
        active = target
    else:
        # 库里本来一条都没有 → 它只能当 active（没有第二条可选）；否则按界面的选择
        activate = bool(body.get("activate")) or not sect.active_id
        active_id = target.id if activate else sect.active_id
        active = next(e for e in entries if e.id == active_id)

    result = await _write_model_section(request, section, entries, active_id)
    return {"ok": True, "section": section, "noop": False,
            "library": _library_view(section, entries, active_id),
            "active": _entry_view(section, active, active_id), **result}


@admin_ui_router.post("/model-library/activate")
async def model_library_activate(request: Request,
                                 user: UserContext = Depends(require_admin)):
    """切换业务实际使用的模型：只改 active_id 与顶层镜像，条目本身不动"""
    body = await _json_body(request)
    section = str(body.get("section") or "")
    entry_id = str(body.get("id") or "")
    sect = _model_section(request, section)
    entries = _reuse_entries(sect)
    active = next((e for e in entries if e.id == entry_id), None)
    if active is None:
        raise HTTPException(404, "该模型不在模型库里（可能已被删除），请刷新页面")

    view = {"ok": True, "section": section,
            "library": _library_view(section, entries, entry_id),
            "active": _entry_view(section, active, entry_id)}
    if entry_id == sect.active_id:
        # 已经是 active：不写盘、不重连 —— 空热应用会白白重建一次连接
        return {**view, "noop": True, "applied": True, "scope": None,
                "degraded": {}, "restartRequired": False, "message": None}

    result = await _write_model_section(request, section, entries, entry_id)
    return {**view, "noop": False, **result}


@admin_ui_router.post("/model-library/delete")
async def model_library_delete(request: Request,
                               user: UserContext = Depends(require_admin)):
    """从库里删掉一条配置

    当前 active 的那条不能删：删掉之后业务就没有模型可用了，必须先切到别的。
    这条规则同时保证了"运行期永远能按 active 读到顶层字段"这个前提。
    文档解析是例外：它没有"当前生效的那一条"（卡片恒 active，见 _entry_view），
    删一条 = 摘掉某张卡片上的一枚能力徽标，最后一项能力删掉整张卡片也就没了
    （卡片就是这些条目本身）—— 所以这里不设 active 这道坎，但要保证 active_id
    仍落在剩下的一条上，顶层镜像才不会指向一个不存在的条目。
    """
    body = await _json_body(request)
    section = str(body.get("section") or "")
    entry_id = str(body.get("id") or "")
    sect = _model_section(request, section)
    if entry_id and entry_id == sect.active_id and section != "doc_parse":
        raise HTTPException(400, "它是当前生效的模型，不能删除；"
                                 "请先把别的模型设为 active")
    entries = _reuse_entries(sect)
    left = [e for e in entries if e.id != entry_id]
    if len(left) == len(entries):
        raise HTTPException(404, "该模型不在模型库里（可能已被删除），请刷新页面")

    active_id = sect.active_id
    if section == "doc_parse":
        # 删掉的正是顶层镜像跟着的那条时改跟剩下的第一条；一条不剩就留空
        # （_write_model_section 会把顶层字段一并清空，见那里的注释）
        if not any(e.id == active_id for e in left):
            active_id = left[0].id if left else ""
        # 响应的 active 给 None（而不是剩下第一条）：表单该显示什么由前端决定 ——
        # 用户正在编辑的那条若被删了就清空表单，否则原样留着，别被弹到别的徽标上
        active_view = None
    else:
        active_view = _entry_view(section, next(e for e in left
                                                if e.id == active_id), active_id)

    result = await _write_model_section(request, section, left, active_id)
    return {"ok": True, "section": section, "noop": False,
            "library": _library_view(section, left, active_id),
            "active": active_view, **result}


def _project_root(request: Request) -> Path:
    """配置文件所在工程根：界面上相对路径（模型路径填 `models`）的统一基准

    相对路径不能按进程 CWD 解 —— uvicorn 的启动目录不一定是工程根，那样同一个
    `models` 会随启动方式指到不同地方，界面上看起来"时好时坏"。
    """
    c = _container(request)
    cfg_path = Path(getattr(c.config, "_config_path", "") or
                    "customer/customer_config.yaml")
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path
    return cfg_path.parent.parent


def _rerank_models_dir(request: Request, model_dir: object) -> Path:
    """重排模型的「模型路径」→ 实际要去列的目录

    绝对路径直接用（`/mydata/models`）；相对路径按工程根解（`models`）—— 与运行期
    加载时拼完整路径的基准一致（见 models.resolve_rerank_model_path）。
    """
    raw = str(model_dir or "").strip()
    p = Path(raw)
    return p if p.is_absolute() else _project_root(request) / raw


def _weight_dir_names(root: Path) -> list[str]:
    """权重目录下的可选模型名：只要目录名（去掉隐藏目录与下划线前缀的辅助目录）

    只回目录名、不回 `models/xxx` 这种带路径的写法 —— 路径已由「模型路径」那一行
    表达，徽标与模型ID 再拖一串路径，用户就得自己从里面截出目录名。
    """
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith((".", "_")))


def _row_value(rows, key: str) -> str:
    """表单行 → 字符串值（读的始终是这一行**当前**填的值，不是已保存的旧值）"""
    for r in rows or []:
        if isinstance(r, dict) and str(r.get("key")) == key:
            return str(r.get("value") or "").strip()
    return ""


def _form_scalar_overrides(rows: list, fields) -> tuple[dict, str | None]:
    """配置页表单行 → 适配器字段覆盖值（不写 YAML、不动运行中容器）

    返回 (overrides, 错误原因)；出错时 overrides 为空。

    两类"看起来有值"的输入必须跳过，否则探测会拿假值去建连：
    - 掩码 "******"：页面上的显示占位，不是用户填的值（TS-023）；
    - 凭据字段的空串：页面本就**不回传**已保存的凭据（见 _param_row），留空表示
      "不修改"而非"清空成空口令"。若当新值用，探测会拿空口令去连，报出来的
      是一次假的"认证失败/401"，比"根本没测"更容易把人带偏。
    非凭据字段的空串仍是有效值（例如清空 MySQL 密码 = 改成匿名连接），照常下发。
    """
    overrides: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        k = str(row.get("key") or "")
        if k not in fields:
            continue
        raw = row.get("value")
        if row.get("secret") and (is_untouched(raw) or is_display_dots(raw)):
            continue          # 凭据留空/掩码/圆点 → 沿用已保存值
        v = _coerce_param(row)
        if v is None:
            txt = "" if raw is None else str(raw).strip()
            if txt and txt != MASK:
                return {}, f"参数 {k} 取值非法：{txt}"
            continue          # 空值/掩码 → 沿用已保存值
        overrides[k] = v
    if not overrides:
        return {}, "表单未提供可用的连接参数"
    return overrides, None


# 背景（TS-014）：历史上"测试连接"通过而保存后自检报不可达，根因是两条链路的
# 超时预算不一致（探测无预算、自检 6s），叠加服务端 skip_name_resolve=OFF 导致
# 每个新连接要等满一次反向 DNS（实测 10s）。现在：
#   ① 预算统一到 meta_mysql.HEALTH_BUDGET_SEC，两条链路共用同一个常量；
#   ② 探测一律**真实建连**，不复用任何缓存的引擎/空闲连接 —— 页面上的
#      "测试连接"必须反映此刻服务端的真实状态（服务端刚修好或刚宕机，
#      点一下就看得出来），而不是把一条早就建好的连接报成"正常"。


async def _probe_mysql_with_form(c: ServiceContainer,
                                 rows: list) -> tuple[bool, str]:
    """用配置页表单**当前值**做一次直连探测（不写 YAML、不动运行中容器）

    背景：原「测试连接」只上报 kind，后端只能拿"已保存配置构建的容器适配器"，
    于是表单里改了 host 却没保存时，测的仍是旧地址，用户看到的是"配置了却失败"。
    这里改用表单值现建一个适配器，并回传真实失败原因。

    返回 (是否可用, 可读原因)。

    这里每次都用表单当前值**新建适配器并真实建连**：复用连接会让"测试连接"
    报出早已离开服务端的旧连接状态（服务端刚修改配置、刚宕机都看不出来）。
    """
    from rag.adapters.meta_mysql import MySQLMetaStore
    base = getattr(c.config, "meta", None)
    if base is None:
        return False, "当前配置缺少 meta 段，无法测试"
    overrides, err = _form_scalar_overrides(rows, base.model_fields)
    if err:
        return False, err
    probe_cfg = base.model_copy(update=overrides)
    store = MySQLMetaStore(probe_cfg)
    # 必须用带预算的 health_probe（而非裸 health_detail）：探测与容器自检
    # 共用同一份预算，两条链路才会给出一致结论；
    # 超时也回传"建连超时 + 服务端名称解析"的原因，而不是笼统的"失败"（TS-014）
    probe = getattr(store, "health_probe", None)
    try:
        ok, message = (await probe() if probe is not None
                       else await store.health_detail())
    except Exception as e:
        ok, message = False, f"MySQL 探测失败：{str(e)[:180]}"
    finally:
        # 探测用的引擎是一次性的：真实建连之后必须释放，否则每点一次
        # "测试连接"都会在后台留下一条永不回收的空闲连接
        try:
            await store.close()
        except Exception:
            pass
    return ok, message


async def _probe_fulltext_with_form(c: ServiceContainer,
                                    rows: list) -> tuple[bool, str]:
    """用配置页表单**当前值**做一次真实 ES 探测（不写 YAML、不动运行中容器）

    与 MySQL 同源（TS-014 / TS-016）：旧「测试连接」只上报 kind，后端只能拿
    "已保存配置构建的容器适配器"来测，于是 hosts 改了还没保存时测的仍是旧地址 ——
    用户填了 192.168.100.239，收到的却是 localhost 的超时报错，
    照那句"地址或端口不可达"去查防火墙，方向从一开始就被带偏了。

    与 MySQL 一样**每次真实建连、不复用缓存客户端**，并回传真实失败原因。
    """
    from rag.adapters.fulltext import ElasticsearchFTS
    base = getattr(c.config, "fulltext", None)
    if base is None:
        return False, "当前配置缺少 fulltext 段，无法测试"
    overrides, err = _form_scalar_overrides(rows, base.model_fields)
    if err:
        return False, err
    if "hosts" in overrides:
        hosts = overrides["hosts"]
        if not hosts:
            overrides.pop("hosts")      # 留空 = 沿用已保存的地址列表
        else:
            bad = [h for h in hosts if not h.startswith(("http://", "https://"))]
            if bad:
                # 缺协议时客户端会把它当主机名去解析，报出来的是"名称解析失败"，
                # 与"地址填错"看起来像两码事 —— 在入口直接点明该补什么
                return False, (f"地址列表每项都要带协议，{'、'.join(bad)} 应为 "
                               f"http://{bad[0]}")
    adapter = ElasticsearchFTS(base.model_copy(update=overrides))
    # 与容器自检共用同一份预算与口径；原因经 es_failure_reason 语义化
    # （含"客户端/服务端大版本不一致"这类专门分支，TS-015 / TS-016）
    probe = getattr(adapter, "health_probe", None)
    try:
        ok, message = (await probe() if probe is not None
                       else await adapter.health_detail())
    except Exception as e:
        ok, message = False, f"ES 探测失败：{str(e)[:180]}"
    finally:
        # 探测客户端是一次性的：真实建连后必须释放（TS-016）
        try:
            await adapter.aclose()
        except Exception:
            pass
    return ok, message


async def _probe_vector_with_form(c: ServiceContainer,
                                  rows: list) -> tuple[bool, str]:
    """用配置页表单**当前值**做一次真实向量库探测（不写 YAML、不动运行中容器）

    与 MySQL / ES 同源（TS-014 / 016）：旧「测试连接」只能拿"已保存配置构建的
    容器适配器"来测，于是 host 改了还没保存时测的仍是旧地址 —— 用户填了
    192.168.100.239，收到的却是 localhost 的超时报错，照那句"地址或端口不可达"
    去查防火墙，方向从一开始就被带偏了。

    同样每次真实建连、不复用单例（AdapterRegistry.create 会命中旧配置的缓存），
    并把真实失败原因回传（milvus_failure_reason 会把 pymilvus 那句笼统的
    "illegal connection params" 拆成地址/认证/权限三类）。
    """
    from rag.adapters.registry import AdapterRegistry
    base = getattr(c.config, "vector_store", None)
    if base is None:
        return False, "当前配置缺少 vector_store 段，无法测试"
    overrides, err = _form_scalar_overrides(rows, base.model_fields)
    if err:
        return False, err
    cfg = base.model_copy(update=overrides)
    name = str(cfg.adapter or "milvus")
    klass = AdapterRegistry.get_class("vector_store", name)
    if klass is None:
        return False, (f"向量库实现 {name!r} 未注册，可用："
                       + "、".join(AdapterRegistry.list_implementations(
                           "vector_store")))
    try:
        # 优先用 klass.probe()：milvus 用它建一条**一次性 dedicated 连接**，
        # "测一个新地址"不会牵动运行期那条连接，用完由下面 finally 关掉
        probe_obj = klass.probe(cfg) if hasattr(klass, "probe") else klass(cfg)
    except Exception as e:
        return False, f"向量库探测初始化失败：{str(e)[:180]}"
    probe = getattr(probe_obj, "health_probe", None)
    try:
        ok, message = (await probe() if probe is not None
                       else await probe_obj.health_detail())
    except Exception as e:
        ok, message = False, f"向量库探测失败：{str(e)[:180]}"
    finally:
        # 探测实例是一次性的：真实建连之后必须释放，否则每点一次"测试连接"
        # 都会在 pymilvus 里留下一条永不回收的 gRPC 通道
        closer = getattr(probe_obj, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass
    return ok, message


# S3/MinIO 在 bucket 级探测上能遇到的几种主流故障 → 可读原因。
# 不翻译的话页面只会收到一句"连接失败"：Adaptor.health_check() 把 S3Error
# 整个吞成 False，用户无从判断是地址、凭据还是权限问题（TS-015 同类问题）。
_S3_ERROR_HINTS = {
    "InvalidAccessKeyId": "Access Key 不存在",
    "SignatureDoesNotMatch": "Secret Key 不正确，或与 Access Key 不配对",
    "AccessDenied": "凭据被拒绝：无权访问该 bucket / 无权创建 bucket",
    "InvalidBucketName": "bucket 名不合法（3~63 位小写字母、数字、- 或 .）",
    "InvalidURI": "端点或 bucket 名不合法",
    "NoSuchBucket": "bucket 不存在",
}


def _s3_failure_reason(e: Exception) -> str:
    code = str(getattr(e, "code", "") or "")
    txt = str(e).strip().replace("\n", " ")[:180]
    if code:
        head = _S3_ERROR_HINTS.get(code, code)
        return f"{head}（{code}: {txt}）"
    # 连接层异常（urllib3 的 MaxRetryError / SSLError 等）没有 S3 code
    return f"{type(e).__name__}: {txt}"


async def _probe_storage_with_form(c: ServiceContainer,
                                   rows: list) -> tuple[bool, str]:
    """用配置页表单**当前值**做一次真实 MinIO 探测（不写 YAML、不动运行中容器）

    与 MySQL / ES 同源（TS-014 / TS-016：探测一律用用户此刻提交的值，不复用
    已保存配置构建的实例）。MinIO 这边还多解一个死锁：storage 是核心适配器，
    构造失败即降级为 local_fs，而「测试连接」原先只能拿容器里那个已降级的
    实例去测 → 表单怎么改都恒失败；偏偏「保存」又被 gate 挡在"测试通过"之后，
    于是新配置永远写不进 YAML，只能手改配置文件重启。

    探测动作与构造期保持一致（MinIOStorage.__init__ 就是"查 bucket，没有就建"），
    否则会出现反向的不一致：测试说通、保存后却降级（那又会被 gate 判成误挡）。
    """
    from minio import Minio
    base = getattr(c.config, "storage", None)
    if base is None:
        return False, "当前配置缺少 storage 段，无法测试"
    overrides, err = _form_scalar_overrides(rows, base.model_fields)
    if err:
        return False, err
    cfg = base.model_copy(update=overrides)
    endpoint = str(cfg.endpoint or "").strip()
    if not endpoint:
        return False, "端点为必填：形如 ip:port"
    if "://" in endpoint:
        # Minio() 自己拼协议头（secure 决定 http/https），带协议会抛
        # "path in endpoint is not allowed" —— 多半是从 ES 的 hosts 照抄来的
        return False, ("端点只填 ip:port，不要带 http:// 或 https://"
                       "（协议由「HTTPS」开关决定）")
    bucket = str(cfg.bucket or "").strip()
    if not bucket:
        return False, "Bucket 为必填，留空无法定位存储桶"
    if not cfg.access_key and not cfg.secret_key:
        return False, ("未填写 Access Key / Secret Key；MinIO 的这两个值就是"
                       "控制台登录用的用户名与密码")
    if cfg.access_key and not cfg.secret_key:
        return False, "已填 Access Key 但 Secret Key 为空，两者必须成对填写"
    try:
        import urllib3
        from rag.adapters.storage import (PROBE_BUDGET_SEC,
                                          PROBE_CONNECT_TIMEOUT_SEC,
                                          PROBE_READ_TIMEOUT_SEC)
        client = Minio(
            endpoint, access_key=cfg.access_key, secret_key=cfg.secret_key,
            secure=bool(cfg.secure),
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=PROBE_CONNECT_TIMEOUT_SEC,
                                        read=PROBE_READ_TIMEOUT_SEC),
                retries=urllib3.Retry(total=0),   # 探测不重试（TS-002 同源）
            ))
    except Exception as e:
        return False, f"端点不合法（应形如 ip:port，不含协议与路径）：{e}"

    def _run() -> tuple[bool, str]:
        # minio-py 是同步 SDK：放线程里跑，避免阻塞事件循环
        try:
            if client.bucket_exists(bucket):
                return True, f"连接正常，bucket {bucket} 可访问"
            client.make_bucket(bucket)
            return True, (f"连接正常；bucket {bucket} 原不存在，已自动创建"
                          "（保存后同样会自动创建）")
        except Exception as e:
            return False, _s3_failure_reason(e)

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run),
                                      timeout=PROBE_BUDGET_SEC)
    except (asyncio.TimeoutError, TimeoutError):
        return False, (f"探测超时（超过 {PROBE_BUDGET_SEC:g}s 无响应）："
                       f"{endpoint} 未在预算内回应 HEAD 请求；"
                       "确认地址/端口是否正确、是否有防火墙静默丢包")


async def _probe_redis_with_form(c: ServiceContainer,
                                 rows: list) -> tuple[bool, str]:
    """用配置页表单**当前值**真实建连探测 Redis（不写 YAML、不动运行中容器）

    与 MySQL / ES / 向量库 / MinIO 同源（TS-014 / 016 / 017），但 Redis 此前是唯一
    "拿已保存配置的实例来测"的服务，于是有两个后果：
      1) host 改了还没保存时，测的是旧地址 —— 返回的却是"连接正常"，真正的失败
         要等保存之后才出现，用户拿着"测试通过"去查别处（同 TS-016 第 3 组）；
      2) Redis 曾不可达（degraded 有记录）时，health_test 的 deg_reason 分支排在
         前面复述旧原因 ⇒ 改对了地址也永远测不过，而保存又被"测试通过"gate 住
         ⇒ 只能手改 customer_config.yaml 重启（与 TS-017 向量库的死锁同构）。
    """
    from rag.adapters.redis_cache import probe as redis_probe
    base = getattr(c.config, "redis", None)
    if base is None:
        return False, "当前配置缺少 redis 段，无法测试"
    overrides, err = _form_scalar_overrides(rows, base.model_fields)
    if err:
        return False, err
    # 非凭据字段的空字符串是**有意填的值**（清空 = 改成匿名连接），helper 会原样下发；
    # 只有凭据字段的留空/掩码会被跳过（页面不回传真凭据，见 _form_scalar_overrides）。
    # 这里走一次真正校验（不是 model_copy：它不做类型校验，会把 "abc" 直接塞给
    # 客户端，最后表现成一次莫名其妙的连接超时）
    try:
        cfg = type(base).model_validate({**base.model_dump(), **overrides})
    except Exception as e:
        return False, f"参数校验未通过：{str(e)[:180]}"
    return await redis_probe(cfg)


# OpenAI 兼容路径：模型段的 base_url 一律已含 /v1（与 adapters/embedding.py 同口径），
# 这里只追加模型服务自己的两个端点
_CHAT_PATH = "/chat/completions"
_EMBEDDINGS_PATH = "/embeddings"
# 真实模型调用的预算：冷启动的 vLLM / Ollama 首次请求要加载权重，比探 /models 慢得多。
# 判据是"有没有正确回应"，不该因为等得久就判失败
_MODEL_CALL_TIMEOUT_SEC = 30.0
# 取模型列表的预算：只是一次 /models 查询，服务端再慢也就一个列表
_MODEL_LIST_TIMEOUT_SEC = 10.0


def _section_entry(sect, entry_id: object):
    """段里按 id 找库条目（没给 id / 找不到 → None）"""
    if not entry_id:
        return None
    for e in getattr(sect, "models", None) or []:
        if e.id and e.id == str(entry_id):
            return e
    return None


def _effective_api_key(sect, api_key: object, entry_id: object = "",
                       cleared: bool = False) -> str:
    """表单里的 API Key → 真正该发出去（以及该存下去）的那把钥匙

    正常路径：模型段的 api_key 是唯一"框里就是真值"的凭据（见 _param_row 的 reveal），
    框里的文本**就是**那把钥匙 —— 原样返回。用户在框里删掉
    几个字符，就是真把钥匙改坏了，请求会如实失败（不再有"看着像改过、其实悄悄
    回落到了自己的钥匙"这种假象）。

    下面的回落只作兜底：旧页面（不下发真值）、以及非 reveal 的凭据仍在用
    "留空 = 不改动"的语义（见 TS-023），规则是**用框里圆点代表的那把**。
      - 正在「更改」某条（entry_id）：那一条自己的 key。它没存过 key（老条目存的
        就是空、或本机服务免鉴权）就是空钥匙 —— 如实发出去、如实存下来，绝不拿
        别的条目（active 那条）顶上：否则框里明明是空的、模型列表却拉回来了，
        用户以为这条配置自带钥匙，其实只是借了别人的（借来的还不会被存下来，
        于是成了一张永远要借钥匙的卡片）
      - 没进编辑态（新建 / 表单显示的就是当前生效的配置）：段顶层 —— 它就是 active
        条目的镜像
    掩码 "******" 与留空同义（is_untouched 一并覆盖）；一串圆点也同义
    （is_display_dots）—— 圆点串绝不可能是真钥匙，宁可当没改，也不能把它发出去。

    框里被删干净则是用户**主动清空**（cleared=True）—— 那种情况下这条配置就是
    不要钥匙，如实返回空，绝不回落到哪一把上。

    「测试模型」「获取模型ID」「存入模型库」三条路径**共用本函数**：分开算就会出现
    "拿 A 的钥匙测通了、入库却把条目存成空 key"这种自相矛盾的状态。
    """
    if cleared:
        return ""          # 用户主动清空：不要钥匙（区别于"没动过"）
    if is_display_dots(api_key):
        # 圆点画的是"框里代表的那把"，不是真钥匙：与留空同义 → 走回落
        api_key = ""
    if not is_untouched(api_key):
        return str(api_key or "").strip()
    entry = _section_entry(sect, entry_id)
    if entry is not None:
        return str(getattr(entry, "api_key", "") or "")
    return str(getattr(sect, "api_key", "") or "")


def _form_api_key(request: Request, kind: str, api_key: object,
                  entry_id: object = "", cleared: bool = False) -> str:
    """表单里的 API Key → 真正该发出去的钥匙（规则详见 _effective_api_key）

    框里通常就是真钥匙，原样返回；只有"留空 / 一串圆点"（旧页面、或非 reveal 的
    凭据）才回落成"框里代表的那把" —— 留空若直接当空钥匙发出去，会换回一个 401，
    表现为"地址能访问、却没有模型列表"（TS-023）。
    """
    sect = getattr(_container(request).config, kind, None)
    if sect is None:
        return "" if cleared else str(api_key or "").strip()
    return _effective_api_key(sect, api_key, entry_id, cleared)


def _resp_body_snippet(resp) -> str:
    """响应体片段（换行压平）：报错时带上服务端原话，用户/我们才能判因"""
    try:
        return (resp.text or "").replace("\n", " ").strip()[:180]
    except Exception:
        return ""


def _models_failure_reason(resp, endpoint: str) -> str:
    """取模型列表失败 → 「下一步该动哪里」"""
    code = resp.status_code
    body = _resp_body_snippet(resp) or "（无响应体）"
    if code in (401, 403):
        return (f"HTTP {code}：鉴权失败 —— 上面那行 API Key 不对或没带上。"
                f"原始信息：{body}")
    if code == 404:
        return (f"HTTP 404：{endpoint.rstrip('/')}/models 不存在 —— 检查 API 地址是"
                f"否少写/多写 /v1。原始信息：{body}")
    return f"HTTP {code}：服务端拒绝。原始信息：{body}"


def _model_call_failure_reason(resp, endpoint: str, path: str,
                               model_id: str) -> str:
    """真实调用失败 → 把"模型ID不存在"与"路径/鉴权不对"分开说

    这两类失败在 HTTP 上都常是 404/400，但处置动作完全不同：前者去改模型ID，
    后者去改 API 地址。只报状态码会把用户引向错误的方向。
    """
    code = resp.status_code
    body = _resp_body_snippet(resp) or "（无响应体）"
    low = body.lower()
    if code in (401, 403):
        return (f"HTTP {code}：鉴权失败 —— API Key 不对或没带上。原始信息：{body}")
    if (model_id.lower() in low
            or ("model" in low and any(w in low for w in
                                       ("not exist", "does not exist", "unknown",
                                        "invalid", "no such")))):
        return (f"HTTP {code}：服务端不认识模型ID「{model_id}」—— 可点「获取模型ID」"
                f"按服务端返回的列表选一个（或核对拼写）。原始信息：{body}")
    if code == 404:
        return (f"HTTP 404：{endpoint.rstrip('/')}{path} 不存在 —— 检查 API 地址是否"
                f"少写/多写 /v1。原始信息：{body}")
    return f"HTTP {code}：服务端拒绝。原始信息：{body}"


@admin_ui_router.post("/model-library/list-models")
async def list_model_ids(request: Request,
                         user: UserContext = Depends(require_admin)):
    """「获取模型ID」：按表单里的 API 地址 + API Key 拉一次 OpenAI 兼容 /models

    与「测试模型」刻意分开：这里只负责"把服务端自报的模型列出来给用户挑"，
    不做可用性判定 —— 地址通不通、模型ID能不能用，是下一步「测试模型」的事。
    选项列表本身也不可信（vLLM 报的是 --served-model-name，有的服务压根不实现
    /models），所以它只是**省去手抄**，不是白名单：模型ID 始终允许手填。

    拉列表用的钥匙 = 表单里那把（留空 = 正在「更改」的那条自己的，见
    _effective_api_key）：所以"框里空着却拉到了列表"只会发生在**这条配置自己
    存着 key**（框里画着圆点）的时候，否则如实报 401。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = str(body.get("kind") or "llm")
    endpoint = str(body.get("endpoint") or "").strip()
    label = str(body.get("label") or kind)
    if kind == "doc_parse":
        # 文档解析没有"可选模型"这一概念（一台 PaddleX 服务按能力拆 endpoint，
        # 见 models.DOC_PARSE_CAPABILITIES），界面上也没有「可用模型」那一行。
        # 这里明确回绝，免得旧页面 / 直接调接口的调用方走进下面的 /models 分支，
        # 换回一个 404 被误读成"这台 PaddleX 服务有问题"
        return {"ok": False, "models": [],
                "message": "文档解析不用获取模型ID：填好 API 地址后，"
                           "在「处理能力」里选 OCR / 版面识别 / 表格识别 / 公式识别"}
    if kind == "rerank":
        # 本机 Cross-Encoder：没有服务端可问，候选就是「模型路径/API」那个目录下的
        # 子目录（与「测试模型」同一套扫描，见 health_test）。回目录名即可 ——
        # 路径那一半用户已经填在「模型路径/API」里了，模型ID 不该再带一遍
        rows = body.get("params") if isinstance(body.get("params"), list) else []
        model_dir = _row_value(rows, "model_dir")
        if not model_dir:
            return {"ok": False, "models": [],
                    "message": "重排模型：先填「模型路径/API」（如 models 或 "
                               "/mydata/models），再来获取模型"}
        if is_http_url(model_dir):
            # 填的是远程服务地址：那边没有"目录"可列（rerank 服务通常也不实现
            # /models），所以这里不是失败，只是没事可做 —— 模型ID 按服务要求手填
            return {"ok": True, "models": [],
                    "message": f"「模型路径/API」填的是远程服务地址（{model_dir}）："
                               "远程重排不用列本地权重，模型ID 按服务要求手填即可"}
        root = _rerank_models_dir(request, model_dir)
        models = _weight_dir_names(root)
        return {"ok": True, "models": models,
                "message": (f"在 {root} 下找到 {len(models)} 个模型："
                            "点徽标即填入「模型ID」" if models else
                            f"{root} 下没有模型目录（该目录不存在、或没放权重）")}
    if not endpoint:
        return {"ok": False, "models": [],
                "message": f"{label}：先填 API 地址，再来获取模型ID"}
    # entryId：正在「更改」的那条。Key 留空时用它自己的钥匙（见 _effective_api_key）——
    # 空 key 的条目就得如实 401，不能借 active 那条的钥匙把列表拉回来
    api_key = _form_api_key(request, kind, body.get("apiKey"),
                            body.get("entryId"),
                            cleared=bool(body.get("apiKeyCleared")))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=_MODEL_LIST_TIMEOUT_SEC) as hc:
            r = await hc.get(endpoint.rstrip("/") + "/models", headers=headers)
    except Exception as e:
        return {"ok": False, "models": [],
                "message": f"无法访问 {endpoint}：{str(e)[:150]}"}
    if r.status_code != 200:
        return {"ok": False, "models": [],
                "message": _models_failure_reason(r, endpoint)}
    models: list = []
    try:
        for m in ((r.json() or {}).get("data") or []):
            if isinstance(m, dict) and m.get("id"):
                models.append(str(m["id"]))
    except Exception:
        models = []
    models = models[:100]
    if not models:
        return {"ok": True, "models": [],
                "message": "服务端没返回模型列表：可手填模型ID"}
    return {"ok": True, "models": models,
            "message": f"取到 {len(models)} 个模型：点徽标即填入「模型ID」"}


async def _probe_rerank_api(url: str, model: str) -> tuple[bool, str]:
    """远程重排服务探活：真发一次两文档的 rerank，能解析出结果才算通

    与 LLM/向量的「测试模型」同一个口径 —— 真调用，而不是只看地址通不通：地址
    少写一段 /v1、服务压根没起、那个地址返回的其实不是 rerank 契约（有些网关对
    什么都回 200），都会在这里当场暴露，而不是等用户问答时才发现重排没生效。
    端点补全用 models.rerank_api_endpoint：测的就是运行期要打的地址。
    """
    import httpx
    target = rerank_api_endpoint(url)
    payload: dict = {"query": "ping", "documents": ["ping", "pong"], "top_n": 2}
    if model:
        payload["model"] = model
    try:
        async with httpx.AsyncClient(timeout=_MODEL_CALL_TIMEOUT_SEC) as hc:
            r = await hc.post(target, json=payload)
    except Exception as e:
        return False, f"无法访问 {target}：{str(e)[:150]}"
    body = _resp_body_snippet(r) or "（无响应体）"
    if r.status_code != 200:
        if r.status_code in (401, 403):
            return False, (f"HTTP {r.status_code}：鉴权失败 —— 这台重排服务要凭据，"
                           f"而「模型路径/API」一栏只有地址。原始信息：{body}")
        return False, f"HTTP {r.status_code}：{target} 拒绝。原始信息：{body}"
    try:
        data = r.json() or {}
    except Exception:
        return False, f"HTTP 200 但响应不是 JSON：不是 rerank 接口。原始信息：{body}"
    items = data.get("results") or data.get("data") or []
    if not isinstance(items, list) or not items:
        return False, (f"HTTP 200 但响应里没有 results：{target} 不是 rerank 接口"
                       f"（检查地址是否少了/多了 /v1）。原始信息：{body}")
    return True, f"{target} 正常回应（{len(items)} 条打分）"


@admin_ui_router.post("/health/test")
async def health_test(request: Request,
                      user: UserContext = Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = str(body.get("kind") or "")
    endpoint = str(body.get("endpoint") or "").strip()
    rows = body.get("params") if isinstance(body.get("params"), list) else []
    c = _container(request)
    started = time.perf_counter()
    online, message, probed_with_form = False, "", False
    models: list = []

    if kind == "rerank":
        # 两义分流：「模型路径/API」填 http(s) 地址 = 那台远程重排服务，填目录 =
        # 本机 Cross-Encoder 权重。两边都真测 —— 远程发一次最小 rerank 请求（地址
        # 少写一段 /v1、服务没起、返回体不是 rerank 契约，都在这里当场暴露），
        # 本机判"拼出来的目录在不在"：权重文件齐不齐、显存够不够，要到第一次
        # 问答真正加载时才知道（TS-009 记的正是这条测试的口径局限）
        model = str(body.get("model") or "").strip()
        model_dir = _row_value(rows, "model_dir")
        if is_http_url(model_dir):
            online, message = await _probe_rerank_api(model_dir, model)
        else:
            models = _weight_dir_names(_rerank_models_dir(request, model_dir)) \
                if model_dir else []
            if not model:
                message = ("模型ID为空：先填「模型路径/API」再点「获取模型」选一个，"
                           "或直接手填 HuggingFace ID / 完整路径")
            elif not model_dir:
                # 没填「模型路径/API」= 模型ID 本身就是完整引用：绝对路径可当场判定，
                # 其余按 HuggingFace ID 在线获取（本地无从判定，不该拦着）
                p = Path(model)
                if p.is_absolute():
                    online = p.is_dir()
                    message = (f"本地权重目录存在：{p}" if online
                               else f"本地路径不存在：{p}")
                else:
                    online = True
                    message = (f"未填「模型路径/API」：将按 {model} 在线获取"
                               "（HuggingFace ID）")
            else:
                p = Path(resolve_rerank_model_path(model_dir, model))
                if not p.is_absolute():
                    p = _project_root(request) / str(p)
                online = p.is_dir()
                if online:
                    message = f"本地权重目录存在：{p}"
                else:
                    message = (f"本地权重目录不存在：{p}"
                               "（核对「模型路径/API」与「模型ID」）")
                    if models:
                        message += "；该目录下有：" + "、".join(models)
    # 文档解析（PaddleX）的「测试模型」：只探服务本身 —— GET /health 拿到 200 就算
    # 通过。处理能力不参与判定（它只决定以后真正解析时打哪个 endpoint，见
    # models.doc_parse_endpoint_path），所以换个能力不必重测（前端 TEST_IRRELEVANT_KEYS）。
    # 探的是**表单里的地址**（改了还没保存也能测，与其它模型段同一口径）。
    elif kind == "doc_parse":
        if not endpoint:
            message = ("API 地址为空：先填 PaddleX 服务地址（如 http://host:8080），"
                       "再点「测试模型」")
        else:
            target = endpoint.rstrip("/") + "/health"
            import httpx
            try:
                async with httpx.AsyncClient(
                        timeout=_MODEL_LIST_TIMEOUT_SEC) as hc:
                    r = await hc.get(target)
            except Exception as e:
                message = f"无法访问 {target}：{str(e)[:150]}"
            else:
                if r.status_code == 200:
                    online = True
                    cap = _row_value(rows, "capability")
                    message = (f"{target} 正常回应（HTTP 200）；这条配置将用于"
                               f"「{doc_parse_capability_label(cap)}」"
                               f"（{doc_parse_endpoint_path(cap)}）")
                else:
                    message = (f"HTTP {r.status_code}：{target} 未就绪。"
                               f"原始信息：{_resp_body_snippet(r) or '（无响应体）'}")
    # LLM/VLM/Embedding「测试模型」：用表单里的地址 + Key + 模型ID **真发一次请求**
    # （对话模型 → /chat/completions，向量模型 → /embeddings），有正确回应才算通过。
    # 旧口径只 GET 一次 /models，等于只证明"地址通"：模型ID 填错、Key 没权限、
    # 模型其实没加载，全都会显示"测试通过"，要到问答/入库时才炸在业务里。
    elif kind in ("llm", "vlm", "embedding") and endpoint:
        # VLM 是"能带图的对话模型"，接口与 LLM 完全同一条：这里发纯文字 ping 就够
        # 了（多模态服务照样收纯文字消息）—— 要验的是"这个地址 + 这把 Key + 这个
        # 模型ID 能不能对话"，不是"它会不会看图"
        is_chat = kind in ("llm", "vlm")
        model_id = str(body.get("model") or "").strip()
        # 与「获取模型ID」「入库」同一把钥匙（见 _effective_api_key）：
        # 框里的圆点没动 = 沿用（回落）；把圆点删干净 = 主动清空，如实拿空钥匙去测
        api_key = _form_api_key(request, kind, body.get("apiKey"),
                                body.get("entryId"),
                                cleared=bool(body.get("apiKeyCleared")))
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        path = _CHAT_PATH if is_chat else _EMBEDDINGS_PATH
        if not model_id:
            message = ("模型ID为空：先点上面的「获取模型ID」选一个，"
                       "或直接手填再测")
        else:
            import httpx
            if is_chat:
                payload = {"model": model_id,
                           "messages": [{"role": "user", "content": "ping"}],
                           "max_tokens": 1, "temperature": 0,
                           "stream": False}
            else:
                payload = {"model": model_id, "input": ["ping"]}

            async def _call(pl: dict):
                async with httpx.AsyncClient(
                        timeout=_MODEL_CALL_TIMEOUT_SEC) as hc:
                    return await hc.post(endpoint.rstrip("/") + path,
                                         headers=headers, json=pl)

            try:
                r = await _call(payload)
                if (r.status_code == 400 and is_chat
                        and any(s in _resp_body_snippet(r).lower() for s in
                                ("max_tokens", "temperature",
                                 "unsupported", "unknown parameter"))):
                    # 个别新模型（o1 系等）不收 max_tokens/temperature，且响应体点名
                    # 参数不受支持 —— 去掉这两个键再来一次，别让参数的差异伪装成
                    # "连不上"（判据仍是"有没有正确回应"）
                    payload.pop("max_tokens", None)
                    payload.pop("temperature", None)
                    r = await _call(payload)
                if r.status_code != 200:
                    message = _model_call_failure_reason(r, endpoint, path,
                                                         model_id)
                elif is_chat:
                    try:
                        choices = (r.json() or {}).get("choices") or []
                    except Exception:
                        choices = []
                    online = bool(choices)
                    message = (f"{model_id} 正常回应（HTTP 200）" if online else
                               "HTTP 200 但响应里没有 choices：这不是 OpenAI 兼容的"
                               "对话接口（检查 API 地址是否指向 /v1）")
                else:
                    try:
                        vec = ((r.json() or {}).get("data")
                               or [{}])[0].get("embedding") or []
                    except Exception:
                        vec = []
                    online = bool(vec)
                    message = (f"{model_id} 正常返回向量（{len(vec)} 维）"
                               if online else
                               "HTTP 200 但响应里没有向量：这不是 OpenAI 兼容的"
                               "向量接口（检查 API 地址是否指向 /v1）")
            except Exception as e:
                message = f"调用失败: {str(e)[:150]}"
    else:
        # 服务依赖：kind → 容器属性
        attr_map = {"meta": "meta", "mysql_meta": "meta",
                    "vector_store": "vector",
                    "knowledge_graph": "graph", "business_data": "business",
                    "vector": "vector", "graph": "graph",
                    "business": "business", "fulltext": "fulltext",
                    "storage": "storage", "synonym": "synonym",
                    "llm": "llm", "embedding": "embedding"}
        deg_reason = (getattr(c, "degraded", None) or {}).get(
            _DEGRADED_KEY.get(kind, kind))
        if kind in ("meta", "mysql_meta") and rows:
            # 表单值直连探测：改完即可测，无需先保存并等待容器热重建
            online, message = await _probe_mysql_with_form(c, rows)
            probed_with_form = True
        elif kind == "fulltext" and rows:
            # 同理（TS-016）：hosts 改了没保存时，旧行为测的是旧地址，
            # 报出来的却是"新地址不可达"，把排查方向带偏
            online, message = await _probe_fulltext_with_form(c, rows)
            probed_with_form = True
        elif kind == "vector" and rows:
            # 同理：host 改了没保存时，旧行为测的是旧地址。且必须在 deg_reason
            # 之前 —— 向量路一旦被关闭（adapter 置 None），旧分支只会复述构造期
            # 的原因，用户改对了地址也永远测不过 → 保存被 gate 挡住
            online, message = await _probe_vector_with_form(c, rows)
            probed_with_form = True
        elif kind == "storage" and rows:
            # 必须在 deg_reason 之前：storage 降级后这里若只看 degraded 记录，
            # 就会复述构造期的旧原因 → 测不过 → 保存被 gate 挡住 → 死锁
            online, message = await _probe_storage_with_form(c, rows)
            probed_with_form = True
        elif kind == "redis" and rows:
            # 同理（TS-019）：Redis 一旦降级，旧分支只会复述构造期的原因，
            # 用户改对了地址也永远测不过 → 保存被 gate 挡住 → 只能手改 YAML 重启
            online, message = await _probe_redis_with_form(c, rows)
            probed_with_form = True
        elif deg_reason:
            # 已降级为本地实现（MySQL→内存等）：其 health_check 恒为 True，
            # 必须按 degraded 记录判定，否则"降级"会被报成"连接正常"
            online, message = False, str(deg_reason)
        elif kind == "redis":
            # 兜底：表单没带参数时才用"已保存配置"的实例（兼容旧前端 / 直接调接口）。
            # 失败原因同样要翻译，否则又是一句无信息的"连接失败"（TS-019）
            from rag.adapters.redis_cache import redis_failure_reason
            try:
                online = bool(await c.redis.ping()) if c.redis else False
                message = ("Redis 连接正常（按已保存配置）" if online
                           else "Redis 未连接")
            except Exception as e:
                message = redis_failure_reason(e,
                                              getattr(c.config, "redis", None))
        else:
            adapter = getattr(c, attr_map.get(kind, kind), None)
            try:
                if adapter is not None and hasattr(adapter, "health_probe"):
                    # 自带超时预算的探测（MySQL / ES）：与容器自检共用同一口径，
                    # 否则会出现"页面测通了、保存却说不可用"（TS-014 / TS-015）
                    online, message = await adapter.health_probe()
                elif adapter is not None and hasattr(adapter, "health_detail"):
                    # 适配器可直接给出可读原因（如 MySQL 的 errno 语义）
                    online, message = await adapter.health_detail()
                elif adapter is not None and hasattr(adapter, "health_check"):
                    online = bool(await adapter.health_check())
                elif adapter is not None:
                    online = True
                else:
                    message = "适配器未实例化（可能已降级或未配置）"
                online = bool(online)
            except Exception as e:
                online = False
                message = str(e)[:200]
            if not message:
                message = f"{kind or 'service'} 连接正常" if online else "连接失败"
        # 该服务不支持表单直连：明确标注测的是"已保存配置"，避免误判
        if rows and not probed_with_form:
            message = f"{message}（按已保存配置测试，保存后才生效）"
    latency = round((time.perf_counter() - started) * 1000, 1)
    return {"ok": online, "online": online, "message": message,
            "latencyMs": latency, "connection": _conn_label(endpoint),
            "models": models, "probedWithForm": probed_with_form}


@admin_ui_router.get("/documents/{doc_id}/parse-preview")
async def doc_parse_preview(request: Request, doc_id: str, page: int = 1,
                            per_page: int = 500,
                            user: UserContext = Depends(get_current_user)):
    c = _container(request)
    doc = await c.meta.get_document(doc_id, user.tenant_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    try:
        ids = await c.meta.list_chunk_ids(doc_id)
    except Exception:
        ids = []
    page = max(1, page)
    per_page = min(max(1, per_page), 1000)
    window = ids[(page - 1) * per_page: page * per_page]
    metas, texts = [], {}
    try:
        metas = await c.meta.get_chunks_by_ids(window)
        texts = await c.meta.get_chunk_texts([x.chunk_id for x in metas])
    except Exception:
        pass
    elements, outline, seen = [], [], set()
    for i, cm in enumerate(metas, 1):
        text = texts.get(cm.chunk_id, "") or ""
        if cm.section_path:
            parts = [p for p in str(cm.section_path).split("/") if p.strip()]
            title = parts[-1] if parts else ""
            key = (min(len(parts) or 1, 4), title)
            if title and key not in seen:
                seen.add(key)
                outline.append({"level": key[0], "title": title})
        el = {"type": cm.chunk_type if cm.chunk_type in
              ("heading", "text", "table", "list", "image_ocr", "caption",
               "image_caption", "code") else "text",
              "text": text, "page": cm.page_num,
              "section_path": cm.section_path or "",
              "n": i, "chunk_id": cm.chunk_id, "word_count": len(text)}
        if cm.chunk_type == "table":
            lines = [l for l in text.splitlines() if "|" in l]
            if lines:
                headers = [h.strip() for h in lines[0].strip("|").split("|")]
                rows = [[r.strip() for r in l.strip("|").split("|")]
                        for l in lines[1:] if not set(l) <= set("|-: ")]
                el["raw_data"] = {"headers": headers, "rows": rows}
        elements.append(el)
    return {"doc_id": doc_id, "elements": elements, "outline": outline,
            "page": page, "has_more": page * per_page < len(ids)}


# ─────────────── Piece 局部刷新（服务端渲染片段）───────────────

_PIECE_PAGES = ("knowledge", "chat", "doc-detail")
_PAGE_DIR = {"doc-detail": "doc_detail"}

_ACTIVE_TASK_STATUS = ("queued", "parsing", "embedding", "uploading",
                       "indexing", "retrying")


def _doc_view(doc) -> dict:
    status = doc.status.value if hasattr(doc.status, "value") else str(doc.status)
    return {
        "doc_id": doc.doc_id, "filename": doc.filename,
        "collection": doc.collection, "file_type": doc.file_type,
        "status": status, "file_size": doc.file_size,
        "page_count": doc.page_count, "chunk_count": doc.chunk_count,
        "version": doc.version, "error": doc.error,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if getattr(doc, "updated_at", None) else None,
        "allowed_roles": list(doc.allowed_roles or []),
        "created_by": doc.created_by, "storage_url": doc.storage_url,
        "quality_score": getattr(doc, "quality_score", None),
    }


async def _find_doc(c: ServiceContainer, doc_id: str, user: UserContext):
    candidates = [user.tenant_id, "default",
                  doc_id.split("_")[0] if "_" in doc_id else ""]
    for tid in dict.fromkeys(t for t in candidates if t):
        try:
            doc = await c.meta.get_document(doc_id, tid)
        except Exception:
            doc = None
        if doc:
            return doc
    return None


def _task_view(t) -> dict:
    status = t.status.value if hasattr(t.status, "value") else str(t.status)
    return {
        "task_id": t.task_id, "doc_id": t.doc_id, "batch_id": t.batch_id,
        "filename": getattr(t, "filename", "") or "",
        "collection": t.collection, "status": status,
        "stage": getattr(t, "stage", "") or "",
        "error": getattr(t, "error", "") or "",
        "progress": getattr(t, "progress", None),
        "created_at": t.created_at.isoformat() if getattr(t, "created_at", None) else None,
        "updated_at": t.updated_at.isoformat() if getattr(t, "updated_at", None) else None,
    }


async def _piece_data(page: str, piece: str, request: Request,
                      user: UserContext) -> dict:
    c = _container(request)
    q = request.query_params

    if page == "knowledge" and piece == "stats_cards":
        try:
            docs = await c.meta.list_documents(user.tenant_id, None, None, 1000, 0)
        except Exception:
            docs = []
        try:
            tasks = await c.meta.list_tasks(user.tenant_id, limit=200, offset=0)
        except Exception:
            tasks = []
        sv = lambda d: d.status.value if hasattr(d.status, "value") else str(d.status)
        tv = lambda t: t.status.value if hasattr(t.status, "value") else str(t.status)
        active = [t for t in tasks if tv(t) in _ACTIVE_TASK_STATUS]
        queue = _queue_view(c)
        return {"stats": {
            "totalDocs": len([d for d in docs if sv(d) not in ("deleted", "superseded")]),
            "doneDocs": len([d for d in docs if sv(d) == "done"]),
            "failedDocs": len([d for d in docs if sv(d) in ("failed", "partial")]),
            "trashDocs": len([d for d in docs if sv(d) == "deleted"]),
            "activeTasks": len(active)}, "queue": queue}

    if page == "knowledge" and piece in ("docs_tab", "trash_tab"):
        archived = piece == "trash_tab" or q.get("archivedOnly") in ("1", "true")
        status = q.get("status") or ""
        st = None
        if archived:
            st = IngestStatus.DELETED
        elif status:
            try:
                st = IngestStatus(status)
            except ValueError:
                st = None
        try:
            docs = await c.meta.list_documents(
                user.tenant_id, q.get("collection") or None, st, 1000, 0)
        except Exception:
            docs = []
        sv = lambda d: d.status.value if hasattr(d.status, "value") else str(d.status)
        if st is None:
            docs = [d for d in docs if sv(d) not in ("deleted", "superseded")]
        kw = (q.get("q") or "").strip().lower()
        if kw:
            docs = [d for d in docs if kw in (d.filename or "").lower()]
        docs.sort(key=lambda d: d.created_at, reverse=True)
        page_no = max(1, int(q.get("page", 1) or 1))
        size = min(max(1, int(q.get("size", 10) or 10)), 50)
        total = len(docs)
        window = docs[(page_no - 1) * size: page_no * size]
        return {"docs": [_doc_view(d) for d in window], "total": total,
                "page": page_no, "size": size,
                "hasMore": page_no * size < total,
                "archivedOnly": archived,
                "collections": _collection_names(c, user)}

    if page == "knowledge" and piece == "tasks_tab":
        status = q.get("status") or ""
        st = None
        if status and status != "active":
            try:
                st = IngestStatus(status)
            except ValueError:
                st = None
        try:
            tasks = await c.meta.list_tasks(
                user.tenant_id, q.get("collection") or None, st, 500, 0)
        except Exception:
            tasks = []
        tv = lambda t: t.status.value if hasattr(t.status, "value") else str(t.status)
        if status == "active":
            tasks = [t for t in tasks if tv(t) in _ACTIVE_TASK_STATUS]
        tasks.sort(key=lambda t: getattr(t, "created_at", None) or 0, reverse=True)
        page_no = max(1, int(q.get("page", 1) or 1))
        size = min(max(1, int(q.get("size", 10) or 10)), 50)
        total = len(tasks)
        window = tasks[(page_no - 1) * size: page_no * size]
        return {"tasks": [_task_view(t) for t in window], "total": total,
                "page": page_no, "size": size,
                "hasMore": page_no * size < total,
                "collections": _collection_names(c, user)}

    if page == "chat" and piece == "sessions":
        try:
            sessions = await c.memory_service.list_sessions(user.user_id)
        except Exception:
            sessions = []
        return {"sessions": [{
            "session_id": s.session_id, "title": s.title or "新会话",
            "created_at": str(s.created_at), "updated_at": str(s.updated_at),
            "message_count": len(s.short_term),
            "ephemeral_count": len(s.ephemeral_doc_ids),
        } for s in sessions], "activeSessionId": q.get("active") or ""}

    if page == "chat" and piece == "ephemeral_docs":
        session_id = q.get("session") or ""
        docs = []
        if session_id:
            try:
                docs = await c.ephemeral_service.list_docs(session_id)
            except Exception:
                docs = []
        return {"docs": [{
            "docId": d.get("doc_id"), "filename": d.get("filename"),
            "fileType": d.get("file_type"), "size": d.get("file_size"),
        } for d in docs], "session": session_id}

    if page == "chat" and piece == "messages":
        session_id = q.get("session") or ""
        messages = []
        if session_id:
            try:
                st = await c.memory_service.load_session(session_id)
            except Exception:
                st = None
            if st and (st.user_id == user.user_id or user.is_admin):
                messages = [{
                    "id": m.message_id, "role": m.role.value, "content": m.content,
                    "createdAt": m.created_at.isoformat(),
                    "sources": [s.model_dump(mode="json") for s in m.sources],
                    "feedback": m.feedback.value if m.feedback else None,
                } for m in st.short_term[-50:]]
        return {"messages": messages, "session": session_id}

    if page == "doc-detail" and piece in ("doc_header", "doc_attrs"):
        doc = await _find_doc(c, q.get("docId") or "", user)
        return {"doc": _doc_view(doc) if doc else None}

    if page == "doc-detail" and piece == "chunks_container":
        doc_id = q.get("docId") or ""
        page_no = max(1, int(q.get("page", 1) or 1))
        size = min(max(1, int(q.get("size", 12) or 12)), 50)
        try:
            ids = await c.meta.list_chunk_ids(doc_id)
        except Exception:
            ids = []
        total = len(ids)
        window = ids[(page_no - 1) * size: page_no * size]
        metas, texts = [], {}
        if window:
            try:
                metas = await c.meta.get_chunks_by_ids(window)
            except Exception:
                metas = []
            try:
                texts = await c.meta.get_chunk_texts(window)
            except Exception:
                texts = {}
        m = {x.chunk_id: x for x in metas}
        items = []
        for idx, cid in enumerate(window, (page_no - 1) * size + 1):
            cm = m.get(cid)
            items.append({
                "n": idx, "chunk_id": cid,
                "chunk_type": cm.chunk_type if cm else "text",
                "section_path": (cm.section_path if cm else "") or "",
                "page": cm.page_num if cm else None,
                "tokens": cm.token_count if cm else 0,
                "quality_score": getattr(cm, "quality_score", None) if cm else None,
                "text": texts.get(cid, ""),
            })
        return {"items": items, "total": total, "page": page_no, "size": size,
                "hasMore": page_no * size < total}

    if page == "doc-detail" and piece == "preview_pane":
        doc_id = q.get("docId") or ""
        doc = await _find_doc(c, doc_id, user)
        pv = await _parse_preview_data(c, doc_id) if doc else None
        return {"doc": _doc_view(doc) if doc else None,
                "elements": (pv or {}).get("elements", []),
                "outline": (pv or {}).get("outline", [])}

    raise HTTPException(404, f"未知片段: {page}/{piece}")


def _queue_view(c: ServiceContainer) -> dict:
    q = None
    try:
        q = c.ingest_coordinator.queue
    except Exception:
        pass
    queued = q.qsize() if q is not None else 0
    depth = q.maxsize if (q is not None and getattr(q, "maxsize", 0)) else None
    return {"queuedTasks": queued, "queueDepthLimit": depth,
            "queueFull": bool(depth and queued >= depth)}


async def _parse_preview_data(c: ServiceContainer, doc_id: str) -> dict:
    try:
        ids = await c.meta.list_chunk_ids(doc_id)
    except Exception:
        ids = []
    metas, texts = [], {}
    try:
        metas = await c.meta.get_chunks_by_ids(ids[:500])
        texts = await c.meta.get_chunk_texts([x.chunk_id for x in metas])
    except Exception:
        pass
    elements, outline, seen = [], [], set()
    for i, cm in enumerate(metas, 1):
        text = texts.get(cm.chunk_id, "") or ""
        if cm.section_path:
            parts = [p for p in str(cm.section_path).split("/") if p.strip()]
            title = parts[-1] if parts else ""
            key = (min(len(parts) or 1, 4), title)
            if title and key not in seen:
                seen.add(key)
                outline.append({"level": key[0], "title": title})
        el = {"type": cm.chunk_type if cm.chunk_type in
              ("heading", "text", "table", "list", "image_ocr", "caption",
               "image_caption", "code") else "text",
              "text": text, "page": cm.page_num,
              "section_path": cm.section_path or "",
              "n": i, "chunk_id": cm.chunk_id, "word_count": len(text)}
        if cm.chunk_type == "table":
            lines = [l for l in text.splitlines() if "|" in l]
            if lines:
                headers = [h.strip() for h in lines[0].strip("|").split("|")]
                rows = [[r.strip() for r in l.strip("|").split("|")]
                        for l in lines[1:] if not set(l) <= set("|-: ")]
                el["raw_data"] = {"headers": headers, "rows": rows}
        elements.append(el)
    return {"elements": elements, "outline": outline}


@ui_router.get("/ui/piece/{page}/{piece}")
async def ui_piece(page: str, piece: str, request: Request,
                   user: UserContext = Depends(get_current_user)):
    if page not in _PIECE_PAGES:
        raise HTTPException(404, f"未知页面: {page}")
    data = await _piece_data(page, piece, request, user)
    tpl_name = f"partials/{_PAGE_DIR.get(page, page)}/{piece}.jinja2"
    try:
        tpl = templates.get_template(tpl_name)
    except Exception:
        raise HTTPException(404, f"未知片段: {page}/{piece}") from None
    html = tpl.render({
        "request": request, "data": data, "page": page,
        "current_user": _user_view(user),
    })
    return JSONResponse({"html": html})


# ───────────────────── 注册 ─────────────────────

def register_ui(app) -> None:
    """在 create_app 中调用：挂载静态资源 + 页面路由 + UI 补充 API"""
    from rag.api.routes import documents as _docs  # noqa: F401 (保证路由模块加载)
    static_dir = WEB_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)),
                  name="static")

    @app.exception_handler(_LoginRequired)
    async def _login_redirect(request: Request, exc: _LoginRequired):
        resp = RedirectResponse("/login", status_code=302)
        # 顺带清掉失效 Cookie，避免登录页循环
        if request.cookies.get("rag_token"):
            resp.delete_cookie("rag_token", path="/")
        return resp

    app.include_router(pages_router)
    app.include_router(ui_router)
    app.include_router(admin_ui_router)
    log.info("ui_registered", static=str(static_dir))
