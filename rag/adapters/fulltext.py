"""
全文检索适配器（rag/adapters/fulltext.py）

elasticsearch：主实现。
索引结构（三字段共享存储，各自不同查询语义）：
  - content:        text + ik_max_word，BM25 主字段（权重 1.0）
  - content.kw_exact: keyword 子字段，不分词，精确匹配
  - summary:        text，高权重（1.5）
  - keywords:       keyword，存提取的专有名词/型号

服务端前置条件：ES 本体 + **analysis-ik 中文分词插件**。索引由应用自动创建，
但 mapping 里写死了 ik_max_word，缺插件时建索引必然 400 —— 因此
health_detail() 会同时校验"集群连通"与"分词器可用"（见 TS-015）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from rag.config.models import FullTextConfig
from rag.models import ChunkMeta, RetrievedChunk
from rag.observability.logging import get_logger

from .base import FullTextSearchAdapter
from .registry import AdapterRegistry

log = get_logger("rag.adapters.fulltext")

# ── 超时预算（代码常量，刻意不作为用户配置项）─────────────────────
# 与 MySQL 同源（TS-014）：把"连不上要等多久"交给用户填，只会让人以为把它
# 调大就能解决连接问题。收敛成常量后，「测试连接」与运行期自检共用同一份
# 预算，不会再出现"页面测通了、保存却说不可用"。
# 业务请求（search/bulk/delete_by_query…）的超时：与旧配置项的默认值一致，
# 保持业务面行为不变
DATA_TIMEOUT_SEC = 10.0
# 探测单次请求上限：info / _analyze 都是轻量请求，正常毫秒级返回
PROBE_REQUEST_TIMEOUT_SEC = 4.0
# 探测总预算：需串行发两次请求，故取 > 2× 单次上限。刻意取宽 ——
# 误判一次就要关掉整条全文检索路（写入与检索同时降级）
HEALTH_BUDGET_SEC = 10.0
# 单次探测超过该阈值即告警（不改判定，只把"合法地慢"说清楚）
SLOW_PROBE_SEC = 3.0

# mapping 依赖的中文分词器：ES / OpenSearch 的 analysis-ik 插件注册它。
# 自检必须用**同一个名字**验证，否则会"能连上、却建不了索引"
IK_ANALYZER = "ik_max_word"


def _err_text(exc: Exception) -> str:
    """异常里最适合给人看的原始信息（截断、去换行）"""
    raw = getattr(exc, "error", None) or exc
    return repr(raw).replace("\n", " ")[:200]


def _err_full_text(exc: Exception) -> str:
    """异常的全部可读文本（类名 + str + 响应体），用于判别**具体**错误类型

    `_err_text` 只取 error 字段：够展示，不够判别 —— ES 的
    media_type_header_exception 真正原因（"Accept version must be..."）
    在响应体里，只比对 error 字段会漏判（TS-016）。
    """
    parts = [type(exc).__name__, str(exc)]
    for attr in ("error", "info", "body"):
        v = getattr(exc, attr, None)
        if v:
            parts.append(str(v))
    return " ".join(parts)[:1500]


def _client_version() -> str:
    """当前 elasticsearch-py 版本（8.x 是 tuple、9.x 是 str，两种都要能读）"""
    try:
        import elasticsearch
    except Exception:
        return "未知"
    v = getattr(elasticsearch, "__version__", "")
    if isinstance(v, (tuple, list)):
        return ".".join(str(x) for x in v)
    return str(v) or "未知"


def _looks_like_missing_analyzer(exc: Exception) -> bool:
    """该异常是否真的是"分词器找不到"

    判据用**分词器名出现在报错正文里**：ES 报缺分词器时必定带上被请求的
    analyzer 名（`failed to find analyzer [ik_max_word]`）；而大版本不一致的
    media_type 400、探测途中的连接中断都不会出现它。
    """
    return IK_ANALYZER in _err_full_text(exc).lower()


def es_failure_reason(exc: Exception) -> str:
    """ES 客户端异常 → 可操作的中文原因（配置页「测试连接」直接展示）

    旧实现 `except Exception: return False` 把"DNS 失败 / 拒绝连接 / 401 /
    403 / 证书错误"一律压成"连接失败"四个字，而这五类的处置方向完全不同
    （同 TS-011 对 MySQL errno 的语义化）。
    按**异常类名**判别而不是 import 具体类：客户端 8.x → 9.x 的类定义位置
    有过迁移，类名比导入路径稳定。
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    raw = _err_text(exc)
    if name == "ConnectionTimeout":
        return (f"连接超时（{PROBE_REQUEST_TIMEOUT_SEC:.0f}s 内无响应）：地址或端口"
                f"不可达、被防火墙静默丢包（DROP），https 还可能是证书握手卡住；"
                f"原始信息：{raw}")
    if name in ("ConnectionError", "ConnectError", "NewConnectionError",
                "ConnectTimeoutError"):
        return (f"无法连接（{name}）：连接被拒绝或主机不可达 —— 请核对地址、端口与"
                f"服务是否已启动；原始信息：{raw}")
    if name == "AuthenticationException" or status == 401:
        return (f"认证失败（HTTP 401）：用户名/密码与服务端不一致；若集群未开启"
                f"安全认证，请把用户名与密码都留空；原始信息：{raw}")
    if name == "AuthorizationException" or status == 403:
        return (f"权限不足（HTTP 403）：该账号无权读取集群信息或调用分词接口，"
                f"请让集群管理员放行；原始信息：{raw}")
    if name in ("TlsError", "SSLError", "SSLCertVerificationError"):
        return (f"TLS 握手失败（{name}）：服务端证书不被信任或协议不匹配 —— "
                f"自签证书可关闭「校验证书」或导入 CA；原始信息：{raw}")
    # 客户端/服务端**大版本不一致**：ES 会对每个请求回 400，与网络、插件无关。
    # 8.x 服务端只认 compatible-with=8/7，9.x 客户端却默认发 9（TS-016）。
    if status == 400 and (
            "media_type_header" in _err_full_text(exc).lower()
            or "accept version must be" in _err_full_text(exc).lower()):
        ver = _client_version()
        major = ver.split(".")[0] if ver != "未知" else "?"
        return (f"客户端与服务端大版本不一致（当前客户端 {ver}）：ES 服务端只接受 "
                f"Accept: ...compatible-with=8/7，而 {ver} 客户端默认发 "
                f"compatible-with={major}，于是**每个**请求都被 400 拒掉 —— "
                f"与网络、防火墙、IK 插件都无关（地址本身是通的）。请安装与服务端"
                f"同大版本的客户端：pip install \"elasticsearch[async]>=8.13,<9\"，"
                f"然后重启应用。原始信息：{raw}")
    prefix = f"HTTP {status}" if status else name
    return f"ES 连接失败（{prefix}）：{raw}"


def es_timeout_reason(cfg: FullTextConfig, budget: float, elapsed: float) -> str:
    """探测等满预算的原因 —— 必须与"连不上"区分开

    "等满超时"与"立刻被拒"是两种故障：前者多为网络静默丢包、地址落在不存在的
    网段、或 TLS 握手卡住；后者是服务没在监听。旧文案合并成一句"连接失败"，
    用户据此会去查错方向（同 TS-014）。
    """
    hosts = "、".join(cfg.hosts or []) or "(未填地址)"
    return (f"探测超时（{elapsed:.1f}s 已超过预算 {budget:g}s）：{hosts} 无响应。"
            f"常见于地址/端口填错、防火墙静默丢包（DROP）或 https 证书握手卡住；"
            f"可在服务端用 curl 直连该地址自测")


@AdapterRegistry.register("fulltext", "elasticsearch")
class ElasticsearchFTS(FullTextSearchAdapter):

    def __init__(self, config: FullTextConfig):
        # 异步客户端统一从包顶导入：`elasticsearch.asyncio` 这个子模块路径在
        # 8.x / 9.x 里**并不存在**（实测 9.4.1 直接 ModuleNotFoundError），
        # 而它会让适配器构造失败 → 容器把全文检索整段判为不可用，
        # 页面却只显示"适配器未实例化"（真正原因只留在日志里）
        try:
            from elasticsearch import AsyncElasticsearch
        except ImportError:                    # 兼容极老版本的自定义构建
            from elasticsearch.asyncio import AsyncElasticsearch  # type: ignore
        self.config = config
        # ES 可能未开启安全认证：用户名与密码都留空时不做 basic_auth
        # （只填其一时仍带上，交给服务端返回明确的 401 而不是静默匿名访问）
        auth = ((config.username, config.password)
                if (config.username or config.password) else None)
        self._client = AsyncElasticsearch(
            hosts=config.hosts,
            basic_auth=auth,
            verify_certs=config.verify_certs,
            request_timeout=DATA_TIMEOUT_SEC,
        )
        self._prefix = config.index_prefix

    def _index(self, name: str) -> str:
        # 已是完整物理名或复合索引串（逗号连接的多索引）时原样透传
        if name.startswith(self._prefix) or "," in name:
            return name
        return f"{self._prefix}{name}"

    def index_names(self, names: list[str]) -> str:
        """把多个逻辑集合名拼接为可直接用于 search 的多索引串"""
        return ",".join(self._index(n) for n in names)

    async def ensure_index(self, index: str) -> None:
        full = self._index(index)
        exists = await self._client.indices.exists(index=full)
        if exists:
            return
        mapping: dict[str, Any] = {
            "settings": {
                "analysis": {
                    "analyzer": {
                        "ik_default": {"type": "custom", "tokenizer": "ik_max_word"},
                    }
                },
                "number_of_shards": 1, "number_of_replicas": 0,
            },
            "mappings": {
                "properties": {
                    "chunk_id":   {"type": "keyword"},
                    "doc_id":     {"type": "keyword"},
                    "tenant_id":  {"type": "keyword"},
                    "collection": {"type": "keyword"},
                    "filename":   {"type": "keyword"},
                    "file_type":  {"type": "keyword"},
                    "chunk_type": {"type": "keyword"},
                    "content": {
                        "type": "text",
                        "analyzer": "ik_max_word",
                        "fields": {
                            "kw_exact": {"type": "keyword",
                                         "ignore_above": 512},
                        },
                    },
                    "summary":  {"type": "text", "analyzer": "ik_max_word"},
                    "keywords": {"type": "keyword"},
                    "section_path": {"type": "keyword"},
                    "page_num": {"type": "integer"},
                    "figure_label": {"type": "keyword"},
                    "allowed_roles": {"type": "keyword"},
                    "quality_score": {"type": "float"},
                    "created_at": {"type": "date"},
                }
            },
        }
        await self._client.indices.create(index=full, body=mapping)

    async def upsert_chunks(self, index: str, chunks: list[ChunkMeta],
                            texts: list[str], summaries: list[str | None],
                            keywords: list[list[str]],
                            doc: dict | None = None) -> None:
        full = self._index(index)
        await self.ensure_index(index)
        doc = doc or {}
        actions: list[dict] = []
        for c, text, summary, kws in zip(chunks, texts, summaries, keywords):
            actions.append({"index": {"_index": full, "_id": c.chunk_id}})
            body = {
                "chunk_id": c.chunk_id, "doc_id": c.doc_id,
                "tenant_id": c.tenant_id, "collection": c.collection,
                "chunk_type": c.chunk_type,
                "content": text, "summary": summary or "",
                "keywords": kws, "section_path": c.section_path or "",
                "page_num": c.page_num,
                "figure_label": c.figure_label or "",
                "figure_caption": c.figure_caption or "",
                "allowed_roles": c.allowed_roles or [],
                "quality_score": c.quality_score,
            }
            # 文档级字段：mapping 里声明了，但 ChunkMeta 不携带（见 base 契约）。
            # `filename` 缺失会让 BM25 单独召回的引用没有文件名；`created_at` 缺失
            # 会让"按时间字段筛选"的 Kibana 视图（数据视图的时间字段通常就选它）
            # 一条都查不到 —— 看起来就像"ES 里没有内容"。
            for k in ("filename", "file_type", "created_at"):
                v = doc.get(k)
                if v:
                    body[k] = v
            actions.append(body)
        if actions:
            resp = await self._client.bulk(operations=actions, refresh="wait_for")
            if resp.get("errors"):
                # 部分失败必须**当成失败**：原实现只打一条 warning，于是文档仍然
                # 是 done、质量报告里一个字都不提，表现为"悄悄少了几块"。
                # 抛出去交给 WriteStep 记成 es_write_failed（文档转 partial）。
                bad = []
                for item in (resp.get("items") or []):
                    for op, res in (item or {}).items():
                        if isinstance(res, dict) and res.get("error"):
                            bad.append(f"{res.get('_id')}: "
                                       f"{str(res['error'].get('reason') or res['error'])[:120]}")
                msg = (f"ES 批量写入部分失败：{len(bad)}/{len(chunks)} 条，"
                       f"示例 {bad[:2]}" if bad else
                       f"ES 批量写入返回 errors=true（{len(chunks)} 条，"
                       f"未解析出具体原因）")
                raise RuntimeError(msg)
            log.debug("es_bulk_done", index=full, count=len(chunks))

    async def get_doc_enrichment(self, index: str,
                                 doc_id: str) -> dict[str, dict]:
        """取该文档现有块的摘要/关键词（补写时保留，见 base 契约）"""
        full = self._index(index)
        try:
            resp = await self._client.search(
                index=full, size=10000, query={"term": {"doc_id": doc_id}},
                _source=["chunk_id", "summary", "keywords"])
        except Exception as e:                    # 索引不存在等：当作没有
            log.warning("es_enrichment_read_failed", index=full,
                        doc_id=doc_id, error=_err_text(e))
            return {}
        out: dict[str, dict] = {}
        for h in resp["hits"]["hits"]:
            s = h.get("_source") or {}
            cid = s.get("chunk_id") or h.get("_id")
            out[cid] = {"summary": s.get("summary") or None,
                        "keywords": list(s.get("keywords") or [])}
        return out

    async def delete_by_ids(self, index: str, chunk_ids: list[str]) -> int:
        """按 chunk_id 批量删除（重跑清理旧块用）"""
        ids = [c for c in (chunk_ids or []) if c]
        if not ids:
            return 0
        full = self._index(index)
        resp = await self._client.delete_by_query(
            index=full, query={"terms": {"chunk_id": ids}},
            refresh=True, conflicts="proceed",
        )
        return int(resp.get("deleted", 0))

    def _base_filter(self, filter: dict | None) -> list[dict]:
        must: list[dict] = []
        if not filter:
            return must
        if filter.get("tenant_id"):
            must.append({"term": {"tenant_id": filter["tenant_id"]}})
        if filter.get("collection"):
            must.append({"term": {"collection": filter["collection"]}})
        if filter.get("doc_id"):
            must.append({"term": {"doc_id": filter["doc_id"]}})
        if filter.get("chunk_ids"):
            must.append({"terms": {"chunk_id": filter["chunk_ids"][:50000]}})
        if filter.get("allowed_roles"):
            # 数据层权限硬过滤：文档角色为空=公开；否则须包含用户角色之一
            must.append({"bool": {
                "should": [
                    {"terms": {"allowed_roles": filter["allowed_roles"]}},
                    {"bool": {"must_not": {"exists": {"field": "allowed_roles"}}}},
                ],
                "minimum_should_match": 1,
            }})
        return must

    async def search(self, index: str, query: str, top_k: int = 20,
                     filter: dict | None = None,
                     synonym_boost_terms: list[str] | None = None) -> list[RetrievedChunk]:
        full = self._index(index)
        must = self._base_filter(filter)
        should: list[dict] = [
            {"match": {"summary": {"query": query, "boost": 1.5}}},
            {"match": {"content": {"query": query, "boost": 1.0}}},
        ]
        if synonym_boost_terms:
            # 同义词扩展词降权合并（0.4）
            should.append({"match": {"content": {
                "query": " ".join(synonym_boost_terms), "boost": 0.4}}})
        body = {
            "size": top_k,
            "query": {"bool": {"must": must, "should": should,
                               "minimum_should_match": 1 if should else 0}},
            "_source": ["chunk_id", "doc_id", "content", "summary",
                        "section_path", "page_num", "chunk_type",
                        "collection", "filename", "figure_label",
                        "figure_caption", "quality_score"],
        }
        resp = await self._client.search(index=full, body=body)
        out: list[RetrievedChunk] = []
        for i, hit in enumerate(resp["hits"]["hits"]):
            s = hit["_source"]
            out.append(RetrievedChunk(
                chunk_id=s.get("chunk_id", hit["_id"]),
                doc_id=s.get("doc_id", ""), text=s.get("content", ""),
                score=float(hit["_score"] or 0), rank=i + 1,
                title=s.get("filename"), section_path=s.get("section_path"),
                page_num=s.get("page_num"), chunk_type=s.get("chunk_type", "text"),
                collection=s.get("collection"),
                figure_label=s.get("figure_label") or None,
                figure_caption=s.get("figure_caption") or None,
                quality_score=float(s.get("quality_score") or 1.0),
            ))
        return out

    async def search_exact(self, index: str, terms: list[str], top_k: int = 20,
                           filter: dict | None = None) -> list[RetrievedChunk]:
        """kw_exact：content.kw_exact term + keywords term 精确匹配"""
        full = self._index(index)
        must = self._base_filter(filter)
        should: list[dict] = []
        for t in terms:
            should.append({"term": {"content.kw_exact": {"value": t, "boost": 2.0}}})
            should.append({"term": {"keywords": {"value": t, "boost": 1.5}}})
        body = {
            "size": top_k,
            "query": {"bool": {"must": must, "should": should,
                               "minimum_should_match": 1}},
            "_source": ["chunk_id", "doc_id", "content", "section_path",
                        "page_num", "chunk_type", "collection", "filename",
                        "figure_label", "figure_caption", "quality_score"],
        }
        resp = await self._client.search(index=full, body=body)
        out: list[RetrievedChunk] = []
        for i, hit in enumerate(resp["hits"]["hits"]):
            s = hit["_source"]
            out.append(RetrievedChunk(
                chunk_id=s.get("chunk_id", hit["_id"]),
                doc_id=s.get("doc_id", ""), text=s.get("content", ""),
                score=float(hit["_score"] or 0), rank=i + 1,
                title=s.get("filename"), section_path=s.get("section_path"),
                page_num=s.get("page_num"), chunk_type=s.get("chunk_type", "text"),
                collection=s.get("collection"),
                figure_label=s.get("figure_label") or None,
                figure_caption=s.get("figure_caption") or None,
                quality_score=float(s.get("quality_score") or 1.0),
            ))
        return out

    async def delete_by_doc(self, index: str, doc_id: str) -> int:
        full = self._index(index)
        resp = await self._client.delete_by_query(
            index=full, query={"term": {"doc_id": doc_id}},
            refresh=True, conflicts="proceed",
        )
        return int(resp.get("deleted", 0))

    async def get_doc_chunk_ids(self, index: str, doc_id: str) -> set[str]:
        full = self._index(index)
        resp = await self._client.search(
            index=full, size=10000, query={"term": {"doc_id": doc_id}},
            _source=["chunk_id"],
        )
        return {h["_source"].get("chunk_id", h["_id"])
                for h in resp["hits"]["hits"]}

    # ── 生命周期 ─────────────────────────────────────────────

    async def aclose(self) -> None:
        """释放 ES 客户端连接池（容器热重载 / 关机 / 一次性探测都会调用）

        适配器原先没有关闭入口，而 `_close_quietly()` 与 `shutdown()` 都是按
        `aclose` / `close` 查找、找不到就静默跳过 —— 于是每保存一次配置
        （fulltext 段热重建）就留下一个永不回收的连接池（TS-016）。
        """
        try:
            await self._client.close()
        except Exception as e:                  # 释放失败不该影响主流程
            log.warning("es_close_failed", hosts=self.config.hosts,
                        error=_err_text(e))

    # ── 健康检查 /「测试连接」（两条链路共用同一口径）──────────

    def _probe_client(self):
        """探测专用的客户端视图：单次请求更紧、且不重试

        重试是业务请求路径的优化手段，不是自检的手段（TS-002 / TS-006）：
        探测要快速给出结论，卡住时由外层预算兜底并报"探测超时"。
        业务请求（search/bulk）继续用默认重试与 DATA_TIMEOUT_SEC。
        """
        options = getattr(self._client, "options", None)
        if options is None:                    # 兼容无 options 的客户端版本
            return self._client
        return options(request_timeout=PROBE_REQUEST_TIMEOUT_SEC,
                       max_retries=0)

    async def _check_ik(self) -> tuple[bool, str]:
        """用与 mapping 相同的分词器名调 _analyze，验证中文分词真的可用

        "能连上集群" ≠ "ES 可用"：缺 analysis-ik 插件时 mapping 里的
        ik_max_word 会让 indices.create 返回 400，建索引与写入全部失败，
        而旧的 GET / 探测对此一无所知（TS-015）。
        """
        try:
            await self._probe_client().indices.analyze(
                analyzer=IK_ANALYZER, text="中文分词自检")
            return True, ""
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status in (401, 403) or type(e).__name__ in (
                    "AuthenticationException", "AuthorizationException"):
                return False, es_failure_reason(e)
            if not _looks_like_missing_analyzer(e):
                # 只有"分词器找不到"才能归因到 IK 插件。其余异常一律交回通用翻译，
                # 否则会把"客户端/服务端大版本不一致的 400"（TS-016）或"探测途中
                # 连接中断"误报成"缺插件"，让用户去装一个本来没问题的插件 ——
                # 与 TS-015 自己要治的"误归因"是同一类毛病。
                return False, es_failure_reason(e)
            return False, (
                f"未检测到中文分词器 {IK_ANALYZER}：ES 缺少 elasticsearch-"
                f"analysis-ik 插件（或插件版本与 ES 版本不一致、未被加载）。"
                f"请在 ES 的 plugins 目录安装与 ES 版本一致的 IK 插件后重启 ES，"
                f"再点「测试连接」。原始信息：{_err_text(e)}")

    async def health_detail(self) -> tuple[bool, str]:
        """返回 (是否可用, 可读原因)：**集群连通 + 中文分词器**两项都要过

        失败原因不再被吞掉：把 ES 客户端的异常翻译成"下一步该动哪里"，
        否则配置页只有一句无信息的"连接失败"（TS-011 / TS-015）。
        """
        try:
            info = await self._probe_client().info()
        except Exception as e:
            log.warning("es_health_failed", hosts=self.config.hosts,
                        error=_err_text(e))
            return False, es_failure_reason(e)
        version = str((info or {}).get("version", {}).get("number") or "")
        ok, reason = await self._check_ik()
        if not ok:
            log.warning("es_ik_unavailable", hosts=self.config.hosts,
                        error=reason[:200])
            return False, reason
        label = f"ES {version}" if version else "ES"
        return True, f"{label} 连接正常，中文分词器 {IK_ANALYZER} 可用"

    async def health_check(self) -> bool:
        ok, _ = await self.health_detail()
        return ok

    async def health_probe(self) -> tuple[bool, str]:
        """带预算的健康检查：配置页「测试连接」与运行期自检共用同一口径

        与 MySQL 同源（TS-014）：两条链路各用各的超时，就会出现"页面测通了、
        保存却说不可用"，而且两条结论都"有据可查"，极难排查。
        """
        started = time.perf_counter()
        try:
            ok, reason = await asyncio.wait_for(
                self.health_detail(), timeout=HEALTH_BUDGET_SEC)
        except (asyncio.TimeoutError, TimeoutError):
            elapsed = time.perf_counter() - started
            log.warning("es_health_timeout", budget=HEALTH_BUDGET_SEC,
                        elapsed=round(elapsed, 2), hosts=self.config.hosts)
            return False, es_timeout_reason(self.config, HEALTH_BUDGET_SEC,
                                            elapsed)
        elapsed = time.perf_counter() - started
        if ok and elapsed >= SLOW_PROBE_SEC:
            # "慢但成功"不会走任何失败分支，必须自己发声（同 mysql_slow_connect）
            log.warning("es_slow_probe", elapsed=round(elapsed, 2),
                        hosts=self.config.hosts,
                        hint="ES 响应明显偏慢：检查集群负载/网络，"
                             "或确认地址是否指向了反向代理")
        return ok, reason


# OpenSearch 协议兼容，注册别名
AdapterRegistry._classes["fulltext"]["opensearch"] = ElasticsearchFTS
