"""
配置模型（rag/config/models.py）

对应 customer_config.yaml 的完整结构。
原则：配置即产品 —— 所有客户化差异通过此文件表达，不改代码。
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from pydantic import (AliasChoices, BaseModel, Field, field_validator,
                      model_validator)


# ═══════════════════════════════════════════════════════════
# 历史命名别名（改名时的兼容层）
# ═══════════════════════════════════════════════════════════
#
# 元数据库段曾叫 mysql_meta，适配器注册名也叫 mysql_meta —— 槽位与实现同名，
# 监控页的 badge（显示注册名）因此只能写出 "mysql_meta"：读者既看不出
# 后端是哪个数据库，也看不出它是不是本地替身。
# 现统一为：配置段 meta、适配器槽位 meta、注册名 mysql。
#
# 兼容口径：存量 YAML 与前端缓存里的旧键一律仍可读（loader 改段名 +
# 字段别名 + adapter 值归一），下一次保存即以新键回写，旧键自然消失。
LEGACY_SECTION_ALIASES: dict[str, str] = {"mysql_meta": "meta"}
LEGACY_ADAPTER_ALIASES: dict[str, str] = {"mysql_meta": "mysql"}


# ═══════════════════════════════════════════════════════════
# 模型库：一个模型段里的多条「已测通」配置
# ═══════════════════════════════════════════════════════════
#
# 这一段原来只有一份扁平字段（地址 / 模型ID / 凭据 / 参数），用户改一次就是
# "覆盖"掉上一套 —— 想留着上次那套地址和 key，只能自己抄到记事本里。模型库
# 把「一套可用配置」固化成一个条目，多套并存，由 active_id 指定业务（问答、
# 向量化）实际使用的那一套。
#
# 顶层那几个字段（base_url / api_key / model / display_name 与运行参数）
# **始终是 active 条目的镜像**，不是第二份真相：适配器构造、监控页、降级判定
# 全都照旧读顶层字段，因此本机制对运行期零侵入；两者也不会漂移 —— 每次加载都
# 按 active 条目重写顶层（见 _sync_model_library）。
#
# 「一个地址 + 一个 key」下往往摆着好几个模型（同一台 vLLM 上 qwen / bge 混部，
# 或线上服务商给了一串模型名），因此条目里存的是**一组模型ID** model_ids，
# model 只是其中"业务实际调用"的那一个的镜像（见 normalize_entry_models）。
MODEL_ENTRY_FIELDS = ("display_name", "base_url", "api_key", "model")

# 各段独有的运行参数：条目里跟着条目走，顶层跟着 active 条目走。
# 不含 adapter / rewrite_model / summary_model —— 它们是**段级**的：
# 换地址、换模型都不改变"用哪个适配器、改写用不用小模型"。
_MODEL_PARAM_FIELDS: dict[str, tuple[str, ...]] = {
    "llm": ("temperature", "max_tokens", "timeout", "max_concurrency"),
    # VLM 视觉模型（多模态对话模型）：接的是同一个 OpenAI 兼容 /chat/completions，
    # 地址 + Key + 模型ID 三件套与 llm 完全一样，条目级运行参数也就同一套 —— 差别
    # 只在业务上谁去吃图。配置页里它与 llm 共用一份表单（见 config-ui.js 的
    # SHARED_FORM_LIBS），段名各是各的：两个库、两个 active，互不覆盖
    "vlm": ("temperature", "max_tokens", "timeout", "max_concurrency"),
    "embedding": ("dim", "batch_size", "query_prefix", "normalize", "timeout"),
    # 重排模型：条目级参数是「模型路径/API + 加载设备」。model_dir 存本机权重
    # 所在目录（用户填 models / /mydata/models 这类），也是**远程重排服务地址**
    # 的落脚点 —— 界面上就一栏，填目录走本机 Cross-Encoder、填 http(s) 走它的
    # /rerank（见 is_http_url / rerank_api_endpoint）。模型ID 只存目录名或服务要
    # 的模型名 —— 两处分开放，界面上才能"选目录 → 选模型"，而卡片徽标与模型ID
    # 框只显示目录名，不拖着一串路径（见 resolve_rerank_model_path）。
    # 它不是顶层的一个"段"（存储挂在 retrieval 上，见 _sync_rerank_library），
    # 但沿用同一套条目结构与参数表 —— 三个模型库接口才能原样复用
    "rerank": ("model_dir", "device"),
    # 视觉模型（vlm）刻意**不在条目参数里放 temperature**：
    # 温度取值范围由服务端与模型声明共同决定（OpenAI 约定 [0,2]，Anthropic 是 [0,1]，
    # 向量模型干脆不校验），一个越界值会让**整个请求**被 400 拒掉 —— 表现为"图片描述
    # 全空"，而错误信息指向的参数与被影响的功能看起来毫无关系（实测：配 7.0 时
    # Qwen3.8 报 temperature must be in [0, 2]，把图片理解整条打掉）。
    # 所以这一段不给用户填、请求里也不带它，由服务端用默认值 —— 少一个能配错的旋钮。
    "vlm": (),
    # 文档解析（Doc-Parse）：条目级参数只有「处理能力」一项 —— 它就是 PaddleX
    # 服务上的一个 endpoint 路径（见 DOC_PARSE_CAPABILITIES）。这一段**没有模型ID**：
    # 一台 PaddleX 服务按能力拆成不同 endpoint，没有"调用哪个模型"这种选择，
    # 条目身份由后端生成的 id 承担（见 new_entry_id）—— 界面上也就没有那一行。
    # 同名的几条 = 同一张卡片（同一个模型名）上的几枚能力徽标，见 DocParseConfig
    "doc_parse": ("capability",),
}


# ═══════════════════════════════════════════════════════════
# 文档解析（Doc-Parse）：PaddleX 服务的一项「处理能力」
# ═══════════════════════════════════════════════════════════
#
# PaddleX 的 serving 把不同任务拆成不同 endpoint，同一个服务地址换一段路径就是
# 另一种能力。所以"这一条配置是什么"= 地址 + 能力，没有一个 model 字段可填：
#   ocr                 → /ocr                  （文字识别）
#   layout-parsing      → /layout-parsing       （版面识别）
#   table-recognition   → /table-recognition    （表格识别）
#   formula-recognition → /formula-recognition  （公式识别）
# 键刻意就用 endpoint 名（而不是另外发明 ocr/layout/table/formula）：配置里读到
# 一个键就知道要打哪个地址，不必再回来查这张表。
DOC_PARSE_CAPABILITIES: dict[str, tuple[str, str]] = {
    "ocr": ("OCR", "/ocr"),
    "layout-parsing": ("版面识别", "/layout-parsing"),
    "table-recognition": ("表格识别", "/table-recognition"),
    "formula-recognition": ("公式识别", "/formula-recognition"),
}
DOC_PARSE_DEFAULT_CAPABILITY = "ocr"


def doc_parse_capability(value: Any) -> str:
    """处理能力 → 白名单内的键（空值 / 非法值一律回落 ocr）

    手改 YAML 写成别的字符串、或旧客户端发来一个不认识的值时，归一到一个**确实
    存在**的 endpoint 上：静默回落比"配了一个打不出去的路径"更好排查。
    """
    key = str(value or "").strip()
    return key if key in DOC_PARSE_CAPABILITIES else DOC_PARSE_DEFAULT_CAPABILITY


def doc_parse_endpoint_path(value: Any) -> str:
    """处理能力 → 运行期真正要打的 endpoint 路径（如 /ocr）"""
    return DOC_PARSE_CAPABILITIES[doc_parse_capability(value)][1]


def doc_parse_capability_label(value: Any) -> str:
    """处理能力 → 界面上显示的中文名（如「版面识别」），给卡片徽标用"""
    return DOC_PARSE_CAPABILITIES[doc_parse_capability(value)][0]

# 存量配置（YAML 里还没有 models 段）自动升级出来的那条的 id。刻意用固定串而非
# 随机值：界面的「更改」要按 id 找回同一条，随机 id 每加载一次就换一个，用户
# 第二次点「更改」会变成新增。
LEGACY_ENTRY_ID = "current"


def new_entry_id() -> str:
    """条目 id：短、随机、不可猜。只是身份，不含任何配置内容"""
    return secrets.token_hex(4)


def model_param_fields(section: str) -> tuple[str, ...]:
    """该模型段的「条目级」运行参数名，顺序即界面上的填写顺序"""
    return _MODEL_PARAM_FIELDS.get(section, ())


class ModelEntry(BaseModel):
    """模型库里的一条配置：一个服务地址 + 一组模型ID + 凭据 + 运行参数

    只存"连得上这个服务"所需的东西。adapter、改写/摘要模型留在段级 ——
    它们不属于某一条，换了地址也还是同一个适配器。
    """
    id: str = ""
    display_name: str = ""          # 界面上列出来的模型名（用户认的就是它）
    base_url: str = ""
    api_key: str = ""
    # 这一套「地址 + key」下可用的模型ID，第一个是业务实际调用的那个。
    # 界面上按 chips 展示；点另一个即置首（= 换业务在用的模型）
    model_ids: list[str] = Field(default_factory=list)
    model: str = ""                 # = model_ids[0]，见 normalize_entry_models
    tested_at: str = ""             # 最近一次连接测试通过的时间（界面如实显示新旧）
    params: dict = Field(default_factory=dict)   # 见 _MODEL_PARAM_FIELDS[段名]


def clean_model_ids(ids: Any) -> list[str]:
    """模型ID列表：去空白、丢空串、去重，顺序照用户给的（第一个 = 在用）"""
    out: list[str] = []
    for v in ids or []:
        s = str(v or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def is_http_url(value: Any) -> bool:
    """值是不是一个 http(s) 地址

    重排的「模型路径/API」一栏两义共存（本机权重目录 / 远程重排服务地址），分流
    全靠它：界面上按目录列候选、运行期按地址发请求（见 routes.health_test 的
    rerank 分支与 query_retrieve 的远程分支）。
    """
    return str(value or "").strip().lower().startswith(("http://", "https://"))


def rerank_api_endpoint(url: Any) -> str:
    """「模型路径/API」填的地址 → 真正要 POST 的 rerank 端点

    用户填的多半是服务根地址（`http://host:8080`、`http://host:8080/v1`），少数
    会把端点一起填上。补全只认一种**已经完整**的写法（末段就是 rerank），其余一律
    补 `/rerank` —— 猜「/v1/rerank 还是 /rerank」只能靠约定，而这个函数是
    「测试模型」与运行期**共用**的：两边永远打同一个地址，不会出现"测通了但问答
    不走它"（见 routes._probe_rerank_api 与 query_retrieve._remote_rerank_scores）。
    """
    u = str(url or "").strip().rstrip("/")
    if not u:
        return ""
    return u if u.rsplit("/", 1)[-1].lower() == "rerank" else u + "/rerank"


def split_rerank_model_ref(value: Any) -> tuple[str, str]:
    """旧口径的「模型路径 + 模型ID」合一体 → (模型路径, 模型ID)

    只拆两种**不含歧义**的本地路径：
    - 绝对路径（`/mydata/models/bge-reranker-base`、`D:/m/models/xxx`）：绝对路径
      只可能是本地目录，HuggingFace ID 从不以分隔符开头；
    - 工程预置目录前缀（`models/bge-reranker-base`）。

    HuggingFace ID（`BAAI/bge-reranker-v2-m3`）与 http(s) 地址**都必须原样留着**：
    前者没有本地目录，拆成 "BAAI" + "bge-reranker-v2-m3" 就再也下不回来了 —— 这
    正是旧口径里"模型ID 里面既可以写 ID 也可以写路径"留下的坑；后者拆开就不再是
    一个能打的地址（两处都靠"不以分隔符开头"这一条天然落到下面原样返回）。
    """
    s = str(value or "").strip()
    if not s:
        return "", ""
    if os.path.isabs(s) or s.startswith(("./", ".\\", "../", "..\\")):
        d, _, m = s.replace("\\", "/").rpartition("/")
        return (d, m) if d and m else ("", s)
    head, sep, tail = s.partition("/")
    if sep and head.strip().lower() == "models":
        return "models", tail
    return "", s


def resolve_rerank_model_path(model_dir: Any, model: Any) -> str:
    """(模型路径/API, 模型ID) → 交给重排后端的实际目标

    界面上分两行填（先选目录、再选目录下的模型），加载要的是一个完整路径：
    填了目录就拼成 `<目录>/<模型ID>`。「模型路径/API」填的是 http(s) 地址时原样
    返回这个地址：那是台远程重排服务，模型ID 不进路径、只进请求体（见
    query_retrieve 的远程分支）。目录留空则原样返回模型ID —— 这时它要么是
    HuggingFace ID（在线获取，本来就没有本地目录），要么本身就是个完整路径。
    """
    d = str(model_dir or "").strip().rstrip("/\\")
    m = str(model or "").strip()
    if is_http_url(d):
        return d
    if not d or not m or os.path.isabs(m):
        return m
    return f"{d}/{m}"


def normalize_entry_models(e: ModelEntry) -> None:
    """把「一个条目一组模型ID」收敛成一条不变式（原地修改）

    两条规则：
    1. model_ids 去重去空；**第一个就是业务在用的那个**，e.model 是它的镜像。
    2. e.model 不在列表里 → 以它为准并置首。手改 YAML 只写了 model（存量配置
       本来就只写 model）、或列表被写坏时，用户明确写下的那个ID才是他想用的。

    e.model 之所以必须与 model_ids[0] 同源：适配器、监控、降级判定只读
    e.model / 顶层字段，而卡片上显示的是 model_ids —— 两处一旦不一致，
    用户看到的"在用模型"和业务实际调用的就不是一回事了。
    """
    ids = clean_model_ids(e.model_ids)
    m = str(e.model or "").strip()
    if m and (not ids or ids[0] != m):
        ids = [m] + [i for i in ids if i != m]
    e.model_ids = ids
    e.model = ids[0] if ids else ""


def _sync_model_library(sect: Any, section: str) -> None:
    """维护「模型库 ↔ 顶层字段」的不变式（原地修改 sect）

    三条规则，缺一条都会让"界面显示的 active"与"业务实际在用"对不上：
    1. 库里没有条目、但顶层已配置 → 按顶层补一条。存量配置自动升级到模型库，
       用户不必手改 YAML，也不会出现"页面上列表是空的，可业务在跑"的割裂感。
    2. active_id 指向不存在的条目 → 先按身份（地址 + 模型ID）找回同一条，
       再不然取第一条。手改 YAML 把 active_id 写丢时，这样选回的最接近原意。
    3. 顶层身份与运行参数 ← active 条目。顶层是派生物 —— 运行期读的就是它，
       所以"切 active"对适配器、监控、降级判定都是透明的。
    """
    fields = _MODEL_PARAM_FIELDS.get(section, ())
    entries: list[ModelEntry] = list(sect.models)

    if not entries:
        # 没配过就返回：库为空才是正确状态，不是"配置丢了"。
        # 判据只看 base_url —— 空地址 = 未配置（启动降级为内置 Mock）；只看
        # 模型ID 不行，embedding 的 model 有默认值 "bge-m3"，那会把从没配过
        # 的向量段也升级成一条"Mock 模型"条目
        if not str(sect.base_url or "").strip():
            return
        entries = [ModelEntry(
            id=LEGACY_ENTRY_ID,
            display_name=str(sect.display_name or ""),
            base_url=str(sect.base_url or ""),
            api_key=str(sect.api_key or ""),
            model=str(sect.model or ""),
            params={k: getattr(sect, k) for k in fields if hasattr(sect, k)},
        )]

    # 存量条目只有 model（模型库刚上线那一版），让它补出 model_ids 并保持
    # 「model = model_ids[0]」；手改 YAML 把两者写岔了也在这里收口
    for e in entries:
        normalize_entry_models(e)

    active = next((e for e in entries if e.id and e.id == sect.active_id), None)
    if active is None:
        ident = lambda e: (e.base_url.strip(), e.model.strip())
        cur = (str(sect.base_url or "").strip(), str(sect.model or "").strip())
        active = next((e for e in entries
                       if ident(e) == cur and (cur[0] or cur[1])), None) \
            or entries[0]

    sect.models = entries
    sect.active_id = active.id
    # 身份字段无条件写回（含留空）：否则用户清空的 base_url 会被上一次加载的
    # 旧值"复活"。运行参数只在条目记过时才写 —— 条目诞生之后新增的参数字段
    # 不该被这条路径清回默认值。
    for k in MODEL_ENTRY_FIELDS:
        setattr(sect, k, getattr(active, k))
    for k in fields:
        if k in active.params:
            setattr(sect, k, active.params[k])


def _sync_rerank_library(ret: Any) -> None:
    """维护「重排模型库 ↔ retrieval.rerank_* 镜像」的不变式（原地修改 ret）

    与 _sync_model_library 同构（库是源、顶层字段是 active 条目的镜像），差异只有
    两处 —— 也是重排模型与另外两段本质不同的地方：
    1. 存储挂在 retrieval 段上，不是一个顶层段：重排是本机 Cross-Encoder 权重目录，
       没有服务地址、没有凭据可存；
    2. 身份只有"模型ID"一个，条目级参数是「模型路径/API」与加载设备（填本机权重
       目录时是路径、填远程服务地址时是 URL，两义共用同一栏）。
    """
    fields = _MODEL_PARAM_FIELDS["rerank"]
    entries: list[ModelEntry] = list(ret.rerank_models)

    if not entries:
        # 顶层写着模型ID → 那是业务当下真正会去加载的那个，补成一条。
        # 判据不能像 _sync_model_library 那样看"地址空不空"：重排的 rerank_model
        # 缺省值本身就是一条可用的配置（运行期拿它加载 Cross-Encoder），
        # 所以这里补出来的条目是实情，不是凭空造的 Mock 条目
        raw = str(ret.rerank_model or "").strip()
        if not raw:
            return
        # 旧口径把目录与模型ID 合写在 rerank_model 一处（如 models/bge-reranker-base），
        # 拆开存：顶层只留目录名，目录挪到 model_dir —— 与界面上的两行同口径
        model_dir = str(getattr(ret, "rerank_model_dir", "") or "").strip()
        if not model_dir:
            model_dir, raw = split_rerank_model_ref(raw)
        entries = [ModelEntry(
            id=LEGACY_ENTRY_ID,
            display_name=str(ret.rerank_display_name or ""),
            model=raw,
            params={"model_dir": model_dir,
                    "device": str(ret.rerank_device or "cpu")},
        )]

    for e in entries:
        normalize_entry_models(e)
        # 存量条目：模型ID 里可能还带着目录前缀（旧口径把目录与ID 合在一处），
        # 挪到「模型路径」参数上 —— 卡片徽标与模型ID 框就只剩目录名了
        if not str(e.params.get("model_dir") or "").strip():
            d, m = split_rerank_model_ref(e.model)
            if d:
                e.params["model_dir"] = d
                e.model = m
                # 同一套权重下并列的其它模型ID 也按同样口径去前缀，徽标才一致
                e.model_ids = clean_model_ids(
                    split_rerank_model_ref(i)[1] for i in e.model_ids)

    active = next((e for e in entries if e.id and e.id == ret.rerank_active_id),
                  None)
    if active is None:
        cur = str(ret.rerank_model or "").strip()
        active = next((e for e in entries
                       if e.model.strip() == cur and cur), None) or entries[0]

    ret.rerank_models = entries
    ret.rerank_active_id = active.id
    # 模型名与模型ID 无条件写回（含留空）：否则用户清空的那次会被旧值"复活"
    ret.rerank_display_name = active.display_name
    ret.rerank_model = active.model
    # 设备只在条目记过时才写 —— 条目诞生之后新增的参数字段不该被清回默认值
    for k in fields:
        if k in active.params:
            setattr(ret, "rerank_" + k, active.params[k])
    # 模型路径与模型ID 是一对（拼出真正要加载的权重目录），所以它也**无条件**写回：
    # 条目里缺这个参数时（手写配置、老客户端）留着上一次的旧路径，会把新模型ID
    # 拼到不相干的目录下 —— 那是个只在加载时才炸的错配
    ret.rerank_model_dir = str(active.params.get("model_dir") or "").strip()


# ═══════════════════════════════════════════════════════════
# 适配器连接配置
# ═══════════════════════════════════════════════════════════

class LLMConfig(BaseModel):
    adapter: str = "openai_compatible"      # openai_compatible / vllm / 自定义注册名
    # 显示名：用户给这套模型配置起的名字（如 "DeepSeek 线上" / "内网 vLLM"），
    # 配置页把它显示在卡片标题上 —— 卡片标题原来只有槽位名（LLM 大模型），
    # 换一套服务后标题照旧，只有翻参数行才看得出配的是谁。
    # 纯标识，不参与适配器构造：它与 base_url 指向的真实服务无关，改它不该重建连接。
    display_name: str = ""
    # 任意 OpenAI 兼容服务（vLLM / Ollama / DeepSeek / 通义 / OpenAI…）
    # base_url 留空 = 未配置 → 启动自动降级内置 Mock，可在 UI 配置页填写
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    rewrite_model: str | None = None        # 查询改写/意图分类用小模型（缺省用 model）
    summary_model: str | None = None        # 摘要压缩用模型
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout: float = 60.0
    max_concurrency: int = 8
    # 模型库与当前生效的那一条（见文件上方 _sync_model_library）：
    # 上面这些字段是 active 条目的镜像，不要在保存路径里单独改它们
    models: list[ModelEntry] = Field(default_factory=list)
    active_id: str = ""

    @model_validator(mode="after")
    def _sync_library(self):
        _sync_model_library(self, "llm")
        return self


class VLMConfig(BaseModel):
    """视觉模型（VLM）：图片理解用的多模态对话模型

    与 LLMConfig 同构 —— 它就是一个 OpenAI 兼容的对话服务（vLLM 上的 Qwen-VL、
    线上 qwen-vl-max…），只是消息里能带图。模型库、「测试模型」、写入 YAML 全都
    复用同一套机制（见 _sync_model_library）。入库时的图片描述走这一段
    （ingest_parse 的 VLMCaptionStep 读 services.vlm）。

    **没有 temperature**（与 LLMConfig 的关键差异）：温度范围由服务端/模型声明决定
    （OpenAI 约定 [0,2]，Anthropic 是 [0,1]），越界值会让整个带图请求被 400 拒掉，
    而我们无法替用户判断某个视觉模型的确切区间。实测教训：这里曾配 7.0（值看着像
    "稳妥的温度"），Qwen3.8 直接报 `temperature must be in [0, 2]`，图片理解整条
    静默失效。所以这一段既不暴露该参数、请求里也不发送它，交给服务端默认值。
    """
    adapter: str = "openai_compatible"
    # 显示名：同 LLMConfig.display_name（视觉模型与主模型常是两套服务，卡片标题
    # 需要各自的标识才分得清；配置页里两者共用一份表单，这行更是唯一的区分）
    display_name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_tokens: int = 2048
    timeout: float = 60.0
    max_concurrency: int = 8
    # 模型库与当前生效的那一条（语义同 LLMConfig，见 _sync_model_library）
    models: list[ModelEntry] = Field(default_factory=list)
    active_id: str = ""

    # 注：存量 YAML 里可能还留着 temperature（例如那个把图片理解打掉的 7.0）。
    # pydantic 默认忽略未知字段 —— 本段没有该属性，所以请求侧也读不到它，
    # 自然不会被发出去（见 llm._safe_temperature）。不需要额外兼容代码。

    @model_validator(mode="after")
    def _sync_library(self):
        _sync_model_library(self, "vlm")
        return self


class EmbeddingConfig(BaseModel):
    adapter: str = "http_embedding"
    # 显示名：同 LLMConfig.display_name（嵌入模型与 LLM 常指向两套不同服务，
    # 卡片标题需要各自的标识来区分）
    display_name: str = ""
    # 向量化服务地址；留空 = 未配置 → 启动自动降级内置 Mock
    base_url: str = ""
    api_key: str = ""
    model: str = "bge-m3"
    dim: int = 1024
    batch_size: int = 32
    query_prefix: str = ""                  # BGE 系列: "为这个句子生成表示以用于检索相关文章："
    normalize: bool = True                  # 显式 L2 归一化（Milvus IP 度量依赖）
    timeout: float = 30.0
    # 模型库与当前生效的那一条（语义同 LLMConfig，见 _sync_model_library）
    models: list[ModelEntry] = Field(default_factory=list)
    active_id: str = ""

    @model_validator(mode="after")
    def _sync_library(self):
        _sync_model_library(self, "embedding")
        return self


class DocParseConfig(BaseModel):
    """文档解析（Doc-Parse）：PaddleX 服务配置段

    与另外几段共用同一套模型库存储（一段 models + active_id，见 _sync_model_library），
    界面上只填三样：模型名、API 地址、处理能力（见 DOC_PARSE_CAPABILITIES）。三处与
    LLM/Embedding 段不同：

    1. **没有模型ID**：PaddleX 把不同任务拆成不同 endpoint，没有"调用哪个模型"
       这种选择，条目身份由后端生成的条目 id 承担（见 new_entry_id）—— 界面上的
       「模型ID」行、「获取模型ID/可用模型」行都不存在（见 routes._MODEL_SECTIONS
       里本段的字段表与 _MODEL_SECTION_UI_FLAGS）。
    2. api_key / model 两个字段保留，只为与 MODEL_ENTRY_FIELDS 的镜像口径对齐
       （_sync_model_library 会把 active 条目的这几个字段无条件写回段顶层）：
       PaddleX serving 默认不带鉴权，界面上不展示 api_key，model 恒为空。
    3. **卡片 = 模型名**：同名的多条就是同一张卡片上的几枚「处理能力」徽标（一台
       PaddleX 服务上的一项能力 = 库里的一条），不是"多套可切换的配置"。所以这一段
       没有"切 active"这回事，卡片恒为 active（见 routes._entry_view），同名条目的
       地址必须一致、同名同能力只存一条（见 routes._check_doc_parse_card）。

    「测试模型」测的是服务的 /health（PaddleX serving 的存活探针）—— 测通即可入库。
    """

    display_name: str = ""
    base_url: str = ""
    api_key: str = ""                   # 保留仅为镜像口径；界面上不展示
    model: str = ""                     # 本段没有模型ID，恒为空
    capability: str = DOC_PARSE_DEFAULT_CAPABILITY
    # 单次请求最多送几页 PDF。10 = PaddleX serving `max_num_input_imgs` 的默认值：
    # **超过它的页会被服务端静默丢掉**（HTTP 仍是 200），所以客户端按这个值分批送，
    # 并把"某个批次返回条数不足"当作截断信号做二分重试（见 doc_parse._pdf_batches）。
    # 客户把服务端的上限调大后，这里也可以调大以减少请求数。
    max_pages_per_request: int = 10
    # 版面引擎的实现名 → 决定 rag/adapters/layout.py 用哪个 LayoutAdapter。
    # 新增引擎时在 _ADAPTERS 注册并在这里填名字即可（**不需要改解析器/步骤**）。
    layout_engine: str = "paddlex"
    # 模型库与当前生效的那一条（语义同 LLMConfig，见 _sync_model_library）
    models: list[ModelEntry] = Field(default_factory=list)
    active_id: str = ""

    @field_validator("capability")
    @classmethod
    def _normalize_capability(cls, v: str) -> str:
        """非法能力值归一（手改 YAML 写成别的 → 回落 ocr，与界面下拉的取值一致）"""
        return doc_parse_capability(v)

    @model_validator(mode="after")
    def _sync_library(self):
        _sync_model_library(self, "doc_parse")
        return self


class VectorStoreConfig(BaseModel):
    adapter: str = "milvus"                 # milvus / qdrant / pgvector
    enabled: bool = True
    host: str = "localhost"
    port: int = 19530
    user: str = ""
    password: str = ""
    # 物理集合名/表名的前缀（三个 adapter 语义统一：前缀 + 逻辑集合名）
    # 只允许字母、数字、下划线且不以数字开头；改它 = 换一整套集合，旧数据不迁移
    collection_prefix: str = "rag_"
    # 仅 pgvector 使用（其余 adapter 自动隐藏该参数）：库必须先存在
    database: str = "rag"
    timeout: float = 10.0
    # 注：用户名与密码**成对留空**即匿名连接（Milvus/ES 未开启认证时的正常用法）；
    # 只填其中一个会被适配器在构造期拒绝，避免静默退化成匿名连接


class FullTextConfig(BaseModel):
    adapter: str = "elasticsearch"          # elasticsearch / opensearch
    enabled: bool = True
    hosts: list[str] = Field(default_factory=lambda: ["http://localhost:9200"])
    # ES 可能未开启安全认证 → 用户名与密码都可留空（都空 = 匿名访问）
    username: str = ""
    password: str = ""
    index_prefix: str = "rag_"
    # 仅对 https 地址生效
    verify_certs: bool = False
    # 注：请求超时 / 健康检查预算**不是用户配置项**，统一由
    # rag/adapters/fulltext.py 的模块常量管理（REQUEST_TIMEOUT_SEC /
    # HEALTH_BUDGET_SEC）—— "验证失败怎么判"是实现细节，用户调它解决不了
    # 连接问题，只会让「测试连接」与运行期自检各说各话（见 TS-015）


class MetaStoreConfig(BaseModel):
    """元数据库（配置段 meta）连接配置

    字段按 MySQL 形状定义，由适配器解释：注册名 mysql 直接使用这组字段；
    将来接入别的元数据库后端时，新增字段由对应适配器读取，段名不必再改。
    """
    adapter: str = "mysql"                  # mysql / memory
    enabled: bool = True
    host: str = "localhost"
    port: int = 3306
    user: str = "rag"
    password: str = ""
    database: str = "rag_meta"
    charset: str = "utf8mb4"
    auto_create_tables: bool = True
    # 注：连接池容量 / 建连超时 / 健康检查预算**不是用户配置项**，
    # 统一由 rag/adapters/meta_mysql.py 的模块常量管理（POOL_SIZE /
    # CONNECT_TIMEOUT_SEC / HEALTH_BUDGET_SEC）—— 用户不需要、也不应
    # 通过调它们来解决连接问题（见 TS-014）

    @field_validator("adapter")
    @classmethod
    def _normalize_adapter(cls, v: str) -> str:
        """历史注册名 mysql_meta → mysql（存量配置无需手工改）"""
        name = str(v or "").strip()
        return LEGACY_ADAPTER_ALIASES.get(name, name)


class RedisConfig(BaseModel):
    host: str = "localhost"
    port: int = 6379
    password: str = ""
    db: int = 0
    session_ttl_hours: int = 48
    prefix: str = "rag:"


class StorageConfig(BaseModel):
    adapter: str = "minio"                  # minio / local_fs
    enabled: bool = True
    endpoint: str = "localhost:9000"
    access_key: str = ""
    secret_key: str = ""
    bucket: str = "rag-docs"
    secure: bool = False
    local_root: str = "./data/files"        # local_fs 模式根目录
    preview_url_ttl: int = 3600


class KnowledgeGraphConfig(BaseModel):
    adapter: str = "neo4j"                  # neo4j / nebula
    enabled: bool = False
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = ""
    max_hops: int = 3


class BusinessDataConfig(BaseModel):
    adapter: str = "sqlalchemy"
    enabled: bool = False
    dsn: str = "mysql+pymysql://user:pass@localhost:3306/business"
    schema_file: str = "customer/business_schema.yaml"
    allowed_tables: list[str] = Field(default_factory=list)
    sensitive_fields: list[str] = Field(default_factory=list)
    max_rows: int = 100


class SynonymConfig(BaseModel):
    adapter: str = "file_based"             # file_based / http_service
    file: str = "customer/synonyms.yaml"
    url: str = ""
    api_key: str = ""
    auto_reload_minutes: int = 30


class AuthConfig(BaseModel):
    adapter: str = "jwt"                    # jwt / oidc / dev
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    token_expire_hours: int = 24
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # dev 模式：无认证，默认管理员上下文（仅限开发环境）
    dev_user_id: str = "dev-admin"
    dev_roles: list[str] = Field(default_factory=lambda: ["admin"])
    dev_tenant_id: str = "default"


# ═══════════════════════════════════════════════════════════
# 功能行为配置
# ═══════════════════════════════════════════════════════════

class RetrievalConfig(BaseModel):
    """检索路功能开关（系统层面硬上限）与召回参数"""
    enable_kw_exact: bool = True
    enable_vector: bool = True
    enable_bm25: bool = True
    enable_graph: bool = False
    enable_structured: bool = False
    enable_ephemeral: bool = True
    top_k_per_path: int = 20                # 每路召回条数（统一缺省）
    kw_exact_top_k: int = 8                 # 各路独立 top_k（0 = 用统一值）
    vector_top_k: int = 10
    bm25_top_k: int = 10
    ephemeral_top_k: int = 8
    graph_hops: int = 2                     # 图谱遍历跳数（≤ knowledge_graph.max_hops）
    graph_max_nodes: int = 50
    rrf_k: int = 60                         # RRF 常数
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    # 「模型路径/API」：本机权重所在目录（models / /mydata/models 这类）或**远程
    # 重排服务地址**（http(s)://…）。填目录时模型ID 是它下面的子目录名，加载时拼成
    # 完整路径；填地址时模型ID 只进请求体（见 resolve_rerank_model_path / is_http_url）。
    # 留空 = 模型ID 本身就是个可直接加载的引用 —— 上面那个缺省值就是这么用的
    rerank_model_dir: str = ""
    rerank_display_name: str = ""             # 模型名：只给界面区分用，不参与推理
    rerank_device: str = "cpu"                # Cross-Encoder 加载设备：cpu / cuda
    rerank_threshold: float = 0.3
    final_top_n: int = 6                    # 进入 Prompt 的 Chunk 数
    dedup_similarity: float = 0.92          # 语义去重阈值
    route_timeout_seconds: float = 5.0      # 每路独立超时
    self_eval_threshold: float = 0.5        # 触发二轮检索的分数线
    self_eval_max_iterations: int = 2
    ephemeral_score_boost: float = 1.2
    default_route_weights: dict[str, float] = {}   # 软路由默认权重（0-1）
    # 重排模型库与当前生效的那一条（见文件上方 _sync_rerank_library）：与 LLM /
    # 向量段同构（多套已测通的配置并存，active 决定业务用哪条），只是存储挂在
    # retrieval 上 —— 重排要么是本机权重目录、要么是一台无需凭据的远程重排服务
    # （地址与权重目录共用 rerank_model_dir 这一栏），没有独立的 base_url/api_key。
    # 上面那几个 rerank_* 字段是 active 条目的镜像，不要在保存路径里单独改它们
    rerank_models: list[ModelEntry] = Field(default_factory=list)
    rerank_active_id: str = ""

    @model_validator(mode="after")
    def _sync_rerank_lib(self):
        _sync_rerank_library(self)
        return self


class PipelineConfig(BaseModel):
    """Pipeline 步骤开关"""
    enable_vlm: bool = True                 # 图片理解
    enable_summary: bool = True             # Chunk 摘要生成
    enable_keywords: bool = True
    enable_entities: bool = True
    enable_faithfulness: bool = True        # 忠实度校验
    faithfulness_threshold: float = 0.6
    enable_query_rewrite: bool = True
    enable_sub_query: bool = True           # 复合问题分解
    total_timeout_seconds: int = 30         # Pipeline 整体超时
    chunk_parent_max_tokens: int = 2000
    chunk_child_max_tokens: int = 512
    min_chunk_tokens: int = 20
    security_max_query_length: int = 2000
    security_blocked_words: list[str] = Field(default_factory=list)


class MemoryConfig(BaseModel):
    short_term_max_turns: int = 10
    short_term_token_budget: int = 1500
    working_summary_max_tokens: int = 400
    session_archive_after_minutes: int = 120    # 无活动归档
    long_term_enabled: bool = True
    topic_switch_threshold: float = 0.45        # 话题跳转余弦相似度阈值


class EphemeralConfig(BaseModel):
    enabled: bool = True
    ttl_hours: int = 2
    max_file_size_mb: int = 50
    max_files_per_session: int = 5
    target_seconds: int = 30                 # 轻量处理时限


class NotificationConfig(BaseModel):
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    webhook_enabled: bool = False
    webhook_url: str = ""
    webhook_secret: str = ""


class ConsistencyCheckConfig(BaseModel):
    enabled: bool = True
    interval_hours: int = 1
    sample_docs: int = 100
    alert_threshold: int = 10


class IngestConfig(BaseModel):
    concurrency: int = 4                     # 入库并发
    retry_backoff_seconds: list[int] = Field(default_factory=lambda: [60, 300, 900])
    max_retries: int = 3
    verify_sample_size: int = 6              # 入库后验证抽样数
    batch_upsert_size: int = 500


class ObservabilityConfig(BaseModel):
    log_level: str = "INFO"
    log_json: bool = True
    metrics_enabled: bool = True
    pushgateway_url: str = ""                # 空 = 不推送
    tracing_enabled: bool = False
    otlp_endpoint: str = ""


# ═══════════════════════════════════════════════════════════
# 权限与提示词
# ═══════════════════════════════════════════════════════════

class RolePermission(BaseModel):
    """角色 → collection 映射（RBAC on Collections）"""
    role: str
    collections: list[str]                   # ["*"] 代表全部
    is_admin: bool = False


class PromptConfig(BaseModel):
    system_prompt: str = (
        "你是企业知识库问答助手。严格依据提供的参考文档回答问题，"
        "在关键陈述后用 [文档N] 标注来源；若参考文档不足以回答，"
        "明确说明知识库中没有相关信息，不要编造。"
    )
    enable_cot: bool = False                 # 思维链提示
    temperature: float = 0.3
    max_answer_tokens: int = 2048
    intent_few_shots: list[dict] = Field(default_factory=list)
    rerank_fallback_prompt: str = ""


# ═══════════════════════════════════════════════════════════
# 顶层配置
# ═══════════════════════════════════════════════════════════

class AppConfig(BaseModel):
    """customer_config.yaml 的完整结构"""
    app_name: str = "RAG 智能问答平台"
    version: str = "3.0.0"

    auth: AuthConfig = Field(default_factory=AuthConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    # 视觉模型（图片理解）：配置页里与 llm 共用一份表单，存储仍是独立的段
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    # 文档解析（Doc-Parse）：PaddleX 服务（OCR / 版面 / 表格 / 公式），一段一库
    doc_parse: DocParseConfig = Field(default_factory=DocParseConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    fulltext: FullTextConfig = Field(default_factory=FullTextConfig)
    # 元数据库：段名 meta（历史名 mysql_meta 仍可读，见 LEGACY_SECTION_ALIASES）
    meta: MetaStoreConfig = Field(
        default_factory=MetaStoreConfig,
        validation_alias=AliasChoices("meta", "mysql_meta"))
    redis: RedisConfig = Field(default_factory=RedisConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    knowledge_graph: KnowledgeGraphConfig = Field(default_factory=KnowledgeGraphConfig)
    business_data: BusinessDataConfig = Field(default_factory=BusinessDataConfig)
    synonym: SynonymConfig = Field(default_factory=SynonymConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)

    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    ephemeral: EphemeralConfig = Field(default_factory=EphemeralConfig)
    consistency_check: ConsistencyCheckConfig = Field(default_factory=ConsistencyCheckConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    permissions: list[RolePermission] = Field(default_factory=list)
    prompts: PromptConfig = Field(default_factory=PromptConfig)
    # 用户角色未配置任何映射时的默认可访问集合（["*"]=全部；[]=拒绝）
    unknown_role_collections: list[str] = Field(
        default_factory=lambda: ["*"])

    server_ingest_root: str | None = None   # 服务器路径入库允许根目录
    workflows_file: str = "customer/workflows.yaml"
    default_collection: str = "default"
    max_upload_mb: int = 500

    # 运行时标志（--noconnection 启动参数注入，非 YAML 字段）：
    # 演示模式 —— 不连接任何外部服务，全部依赖替换为本地实现
    noconnection: bool = False

    # 运行时注入（非 YAML 字段）
    _config_path: str | None = None

    def collections_for_roles(self, roles: list[str]) -> list[str]:
        """角色 → 可访问 collection 白名单（并集；* 代表全部）"""
        result: set[str] = set()
        wildcard = False
        for perm in self.permissions:
            if perm.role in roles:
                if "*" in perm.collections:
                    wildcard = True
                result.update(perm.collections)
        if wildcard:
            return ["*"]
        if result:
            return sorted(result)
        # 角色无任何映射时的默认策略（可配置；["*"]=放开，[]=拒绝）
        return list(self.unknown_role_collections)

    def is_admin_role(self, roles: list[str]) -> bool:
        return any(p.is_admin for p in self.permissions if p.role in roles)

    def enabled_paths(self) -> set[str]:
        """系统层面启用的检索路集合"""
        r = self.retrieval
        paths = {"ephemeral"} if r.enable_ephemeral else set()
        if r.enable_kw_exact and self.fulltext.enabled:
            paths.add("kw_exact")
        if r.enable_vector and self.vector_store.enabled:
            paths.add("vector")
        if r.enable_bm25 and self.fulltext.enabled:
            paths.add("bm25")
        if r.enable_graph and self.knowledge_graph.enabled:
            paths.add("graph")
        if r.enable_structured and self.business_data.enabled:
            paths.add("structured")
        return paths
