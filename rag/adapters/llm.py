"""
LLM 适配器（rag/adapters/llm.py）

openai_compatible：任何 OpenAI Chat Completions 格式服务
（vLLM / Ollama / 国产模型兼容端点 / OpenAI 本体）。
task 参数路由到不同模型（rewrite_model / summary_model / 主模型）。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx

from rag.config.models import LLMConfig
from rag.observability.logging import get_logger

from .base import LLMAdapter
from .registry import AdapterRegistry

log = get_logger("rag.llm")

# 单条回复的默认 token 上限：只用于**没传 max_tokens 且配置也没给**的兜底。
# 取 2048 而不是 16/200 这类小值，是因为现在的服务端里有大量**推理模型**：
# 它们会先输出一段 reasoning（有些把 reasoning 单独放字段、有些直接写进
# content），小上限会被思考过程吃光 —— 表现为 HTTP 200 但内容为空。
# 实测：Qwen3.8 在 max_tokens=16 时 completion_tokens=16 全是 reasoning_tokens，
# content 为 null，连"这张图是什么颜色"都答不出来。
_DEFAULT_REPLY_TOKENS = 2048

# 显式传了 max_tokens 时，若该端点被观测为**推理模型**，额外补的"思考额度"。
#
# 为什么需要：调用点传的 512/300/10 这些数字，原意是"答案不该超过这么多字"，
# 但在推理模型上思考与答案**共用同一个额度** —— 思考一长，答案根本没机会写，
# 表现为 HTTP 200 + 空 content（实测：思考 1383 字符就把 512 吃光）。
# 这跟用户"我明明把 max_tokens 配成 65536"的预期完全相反：那个配置值在
# `max_tokens or config.max_tokens` 的分支里被调用点的显式值挡掉了。
# 观测到推理行为后补一份额度，显式值才回到它原本的语义（"答案的预算"）。
_REASONING_ALLOWANCE = 2048

# ── "别思考"的参数：各家不同，且**同一家也可能因网关而异** ──────────────
#
# 实测（本项目的 DeepSeek 网关，同一个 prompt，看 reasoning_content 长度）：
#   baseline                      → reasoning 586 字符 / 170 reasoning_tokens
#   {"thinking":{"type":"disabled"}} → **0**            ← DeepSeek 官方文档的开关
#   {"reasoning_effort":"none"}      → **0**            ← OpenAI 系写法，该网关也认
#   {"chat_template_kwargs":{"enable_thinking":false}} → 448（无效）
#   {"enable_thinking":false}        → 277（无效）
#   {"reasoning_effort":"minimal"}   → 255（只是减弱，仍在思考）
# 官方文档（https://api-docs.deepseek.com/guides/thinking_mode）写的是：
# OpenAI 格式 `{"thinking":{"type":"enabled|disabled"}}`（SDK 里走 extra_body）、
# `reasoning_effort` 控力度；Anthropic 格式是 `{"reasoning":{"effort":"none"}}`。
# 自托管（vLLM/SGLang 上的 Qwen3 等）通常认 `chat_template_kwargs.enable_thinking`。
#
# 所以不写死一招，而是**按顺序试、记住这个端点上真正生效的那一招**（判据：
# 回了 200 且 reasoning_content 为空）；网关不认（400）就换下一招。
_NO_THINK_STRATEGIES: tuple[tuple[str, dict], ...] = (
    ("deepseek_thinking", {"thinking": {"type": "disabled"}}),
    ("openai_reasoning_effort", {"reasoning_effort": "none"}),
    ("vllm_chat_template", {"chat_template_kwargs": {"enable_thinking": False}}),
)

# 哪些任务**不需要思考**（短且结构化）。这是内置策略，**不对外暴露成配置项**：
#   · 摘要 / 关键词 / 实体抽取、意图分类、忠实度评估、看图写描述 —— 这类任务要的是
#     "快速给出结构化结果"，思考只会拖时间、吃额度（实测关掉后快 4 倍，且从
#     "正文为空"变成稳定拿到 JSON）；
#   · 开放式作答（task="generate"，如最终答案生成）保留思考 —— 那才是思考值钱的
#     地方，这个取舍由框架定，不该让每个客户去猜"我该不该关思考"。
# 想临时覆盖的只有**代码内部**：`generate(thinking=True/False)`（探测、自测用）。
_NO_THINK_TASKS = frozenset({"summary", "extract", "intent", "rewrite",
                             "evaluate", "vision", "caption"})


def no_think_for(task: str) -> bool:
    """该任务是否应当关闭思考（内置策略，见 `_NO_THINK_TASKS`）"""
    return task in _NO_THINK_TASKS


def _extract_reply(data: dict) -> str:
    """从 OpenAI 兼容响应里取**回复正文**，兼容推理模型与两家字段名

    认三种形态（实测都遇到过）：
      · 普通对话模型：message.content 有文本；
      · 推理模型（DeepSeek-R1 / Qwen3.8 等）：思考过程在 message.reasoning_content
        或 message.reasoning，content 可能为空/为 null；
      · content 是分块数组（少数网关）：拼 text 部分。

    ⚠ **思考文本绝不当成回复返回**（原来的兜底就是这么干的，实测踩到）：
    `content` 为空通常意味着"推理模型的思考把 max_tokens 吃光了，答案还没开始写"。
    这时把 `reasoning_content` 当答案返回，下游会拿到一段"我在想该怎么回答…"的
    独白去当摘要/图片描述/JSON —— 表现为：
      · 摘要/关键词解析失败（`JSONDecodeError`）；
      · 图片描述写进去的是模型的思考过程（错误数据，最难发现）；
      · 日志里报成"模型返回了合法 JSON 但 summary 为空"，与真实原因不符。
    返回空串才是诚实的：调用方据此**加大预算重试**或如实降级。

    取不到内容一律返回空串 —— 调用方据此判断"没拿到内容"，而不是把 null 当答案。
    """
    if not data:
        return ""
    try:
        msg = (data.get("choices") or [{}])[0].get("message") or {}
    except (AttributeError, IndexError, TypeError):
        return ""
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(
            str(p.get("text") or "") for p in content if isinstance(p, dict))
    return (content or "").strip()


def _reasoning_len(data: dict) -> int:
    """本次响应里思考文本的长度（>0 表示"思考占了额度"，用于区分空回复的成因）"""
    return len(_reasoning_text(data))


def _reasoning_text(data: dict) -> str:
    """本次响应里的思考正文（没有则空串）"""
    try:
        msg = (data.get("choices") or [{}])[0].get("message") or {}
    except (AttributeError, IndexError, TypeError):
        return ""
    for key in ("reasoning_content", "reasoning"):
        alt = msg.get(key)
        if isinstance(alt, str) and alt.strip():
            return alt.strip()
    return ""


@AdapterRegistry.register("llm", "openai_compatible")
class OpenAICompatibleLLM(LLMAdapter):

    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.api_key}"} if config.api_key else {},
            timeout=config.timeout,
        )
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    def _model_for_task(self, task: str) -> str:
        cfg = self.config
        if task in ("rewrite", "intent", "extract") and cfg.rewrite_model:
            return cfg.rewrite_model
        if task in ("summary", "compress") and cfg.summary_model:
            return cfg.summary_model
        return cfg.model

    # OpenAI 兼容接口对 temperature 的取值范围有硬约束，但**区间不是统一的**：
    # OpenAI 约定 [0,2]（DeepSeek 官方文档也是 `<= 2`），Anthropic 是 [0,1]，
    # 而向量模型压根不校验这个参数。配置里被填成越界值时，服务端会直接 400 拒掉
    # **整个请求** —— 整条功能（图片描述/摘要/生成）静默失效，而错误信息指向的
    # 参数与被影响的功能看起来毫无关系（实测：temperature=7.0 让图片理解整条挂掉）。
    # 这里收口到 [0,2] 并留一条 warning：宁可略微偏离用户的温度设定，也不要让一个
    # 越界数字把功能整个打掉；同时把"你到底配了什么"如实说出来。
    _TEMPERATURE_RANGE = (0.0, 2.0)

    # "服务端不认 response_format" 只告警一次，并且**记住之后不再发送**：这是
    # 端点级结论，不是每次调用的故障（与 doc_parse 的 _unsupported 缓存同一思路）。
    # 只抑制日志不抑制参数的话，每次调用都要"先 400 再重试"，白多一倍往返。
    _warned_no_json_mode = False
    _no_json_mode = False
    # 是否已**观测到**该端点会输出思考（由真实响应的 reasoning_content 判定，
    # 不靠模型名猜）。置位后，显式指定的 max_tokens 会额外获得思考额度。
    _reasoning_seen = False
    # "关闭思考"的探测结果：`"base_url|model" → 生效的策略名`
    # （值 "none" = 这个端点上哪招都不管用）。与 response_format 的缓存同一思路：
    # 端点级结论只探测一次，之后不重复试错。
    _no_think_ok: dict[str, str] = {}
    _warned_no_think_unsupported = False

    def _safe_temperature(self, value: float | None) -> float | None:
        """返回可安全上送的温度；**None = 这个段不该带温度参数**（请求里省略）

        视觉模型段（VLMConfig）没有 temperature 字段 —— 它的合法区间取决于该视觉
        模型自己的声明，我们无法替用户判断，所以整段不发这个参数，交给服务端默认值。
        """
        configured = value if value is not None else getattr(
            self.config, "temperature", None)
        if configured is None:
            return None
        lo, hi = self._TEMPERATURE_RANGE
        try:
            t = float(configured)
        except (TypeError, ValueError):
            return None
        if lo <= t <= hi:
            return t
        clamped = min(max(t, lo), hi)
        log.warning("temperature_clamped", configured=t, used=clamped,
                    model=self.config.model, section=self.config.adapter)
        return clamped

    async def generate(
        self,
        messages: list[dict],
        task: str = "generate",
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        thinking: bool | None = None,
    ) -> str:
        """非流式生成 → 只要**回复正文**（思考文本不会混进来，见 `_extract_reply`）

        `thinking` 是**内部参数**（不作为配置项暴露）：`None` 时按内置策略
        `no_think_for(task)` 判定（结构化短任务关思考），显式 True/False 用于探测与自测。
        """
        out = await self.generate_ex(
            messages, task=task, temperature=temperature,
            max_tokens=max_tokens, response_format=response_format,
            thinking=thinking)
        return out["content"]

    async def generate_ex(
        self,
        messages: list[dict],
        task: str = "generate",
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        thinking: bool | None = None,
    ) -> dict:
        """非流式生成 → 完整视图 `{content, reasoning, finish_reason, model, max_tokens}`

        给"探测/健康检查/诊断"用：这些场景需要区分"模型压根没回话"与
        "模型回了话、但正文为空（思考吃满了额度）"，也需要在判断"模型到底看没看见
        这张图"时把思考文本一起纳入（见 container.probe_vision）。
        """
        temp = self._safe_temperature(temperature)
        budget = self._effective_max_tokens(max_tokens)
        payload = {
            "model": self._model_for_task(task),
            "messages": messages,
            "max_tokens": budget,
            "stream": False,
        }
        # 只在有值时带上：没有该字段的段（vlm）就不发，由服务端用默认温度
        if temp is not None:
            payload["temperature"] = temp
        if response_format and not OpenAICompatibleLLM._no_json_mode:
            payload["response_format"] = response_format
        # 是否关闭思考：调用方显式指定优先（内部用），否则按**内置策略**判断
        # （结构化短任务关思考，见 no_think_for）。刻意不做成配置项：
        # "该不该关"是框架的取舍，交给客户只会多一个能配错的旋钮。
        want_no_think = (not thinking if thinking is not None
                         else no_think_for(task))
        data, used = await self._post_with_policy(payload, want_no_think)
        self._note_reasoning(data)
        reply = _extract_reply(data)
        reason_len = _reasoning_len(data)
        finish = (data.get("choices") or [{}])[0].get("finish_reason")
        if not reply:
            # 拿到 200 却没有内容：十有八九是上限太小、被推理模型的思考过程吃光了。
            # 不静默返回空串——那会让上游把"没答案"当成"模型没内容"，从而写入
            # 空的图片描述、空的摘要，问题要到很久以后才被发现。
            log.warning("llm_empty_reply", model=payload["model"],
                        max_tokens=budget, requested=max_tokens,
                        reasoning_chars=reason_len, finish=finish,
                        thinking_policy=("off" if want_no_think else "on/默认"),
                        no_think_strategy=used,
                        hint=("思考过程吃满了 max_tokens、答案没写出来："
                              "调大本次调用的 max_tokens 或换非推理模型"
                              if reason_len else "服务端确实没返回内容"))
        return {"content": reply, "reasoning": _reasoning_text(data),
                "finish_reason": finish, "model": payload["model"],
                "max_tokens": budget, "no_think": used}

    async def _post_with_policy(self, payload: dict,
                                no_think: bool) -> tuple[dict, str | None]:
        """发一次 chat/completions；`no_think=True` 时按探测结果附加"别思考"参数

        返回 `(响应体, 生效的策略名或 None)`。规则：
          · 已知该端点上哪一招有效 → 直接用它（类级缓存，一次探测长期受益）；
          · 未知 → 依次试候选：网关 400（不认这个参数）或仍然返回了思考 → 换下一招；
          · 全部候选都无效 → 用最后一招的结果返回（至少不丢功能），并记一次结论。
        """
        key = f"{self.config.base_url}|{payload.get('model')}"
        if not no_think:
            return await self._post_json(payload), None
        known = OpenAICompatibleLLM._no_think_ok.get(key)
        if known == "none":
            # 已经探明这个端点关不掉思考 → 不再逐招试错（每次三倍请求纯属浪费），
            # 直接按普通请求发；可靠性交给预算阶梯与"思考当不了答案"那两条兜底
            return await self._post_json(payload), None
        strategies = ([s for s in _NO_THINK_STRATEGIES if s[0] == known]
                      or list(_NO_THINK_STRATEGIES))
        last: dict = {}
        for i, (name, extra) in enumerate(strategies):
            body = {**payload, **extra}
            try:
                data = await self._post_json(body)
            except httpx.HTTPStatusError as e:
                # 400 = 网关不认这组参数（实测有些网关直接报 unknown field）
                if getattr(e.response, "status_code", None) == 400:
                    if not OpenAICompatibleLLM._warned_no_think_unsupported:
                        OpenAICompatibleLLM._warned_no_think_unsupported = True
                        log.info("llm_no_think_param_rejected", param=name,
                                 body=e.response.text[:160],
                                 effect="换用下一种关闭思考的写法")
                    continue
                raise
            last = data
            if _reasoning_len(data) == 0:
                # ⚠ 这里刻意**不打日志**（原来是 log.info，用户明确要求去掉）。
                # 原因有两层：
                #   ① 它是成功路径上的实现细节 —— 探明"这一招有效"之后每篇文档都走
                #      同一条路，用户看到这行既不能动作、也不代表任何异常；
                #   ② 本进程的 structlog **没有级别过滤**（configure_logging 从未被
                #      调用，见 rag/observability/logging.py 的注释），降成 debug 也
                #      照样会打出来 —— 想安静只能不打。
                # 需要知道"这个端点上用的是哪一招"时：
                #   · 代码里读 generate_ex() 的返回值（`no_think` 字段）；
                #   · 文档见 doc/data_path.md §8.24（含两个端点的实测结论）。
                # 真正需要"看得见"的是**关不掉**：那是 warning（见下方
                # llm_no_think_unavailable），保留。
                OpenAICompatibleLLM._no_think_ok[key] = name
                return data, name
        if last and key not in OpenAICompatibleLLM._no_think_ok:
            # 一招都没关掉：记一次结论，别每块都刷日志（预算阶梯仍会兜底）
            OpenAICompatibleLLM._no_think_ok[key] = "none"
            log.warning("llm_no_think_unavailable", model=payload.get("model"),
                        tried=[n for n, _ in strategies],
                        effect="该模型/网关无法关闭思考，继续依赖预算阶梯")
        return last or await self._post_json(payload), None

    async def _post_json(self, payload: dict) -> dict:
        """POST /chat/completions（含 response_format 的 400 降级），返回响应体"""
        async with self._semaphore:
            resp = await self._client.post("/chat/completions", json=payload)
            # 服务端不认 response_format（400）→ 去掉它重试一次，并记住结论。
            # 这个参数是"让模型必须回 JSON"的增强，不是功能前提：
            # 为了它把整条链路打掉，与"宁可少一点格式保证"相比明显更糟。
            if resp.status_code == 400 and "response_format" in payload:
                OpenAICompatibleLLM._no_json_mode = True
                if not OpenAICompatibleLLM._warned_no_json_mode:
                    OpenAICompatibleLLM._warned_no_json_mode = True
                    log.warning(
                        "llm_response_format_unsupported",
                        model=payload.get("model"), status=400,
                        body=resp.text[:200],
                        effect="该服务端不支持 response_format，之后不再发送；"
                               "JSON 正确性改由 prompt + 解析容错保证")
                payload = {k: v for k, v in payload.items()
                           if k != "response_format"}
                resp = await self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            return resp.json()

    def _effective_max_tokens(self, explicit: int | None) -> int:
        """本次实际发送的 max_tokens

        规则：显式值仍是"答案预算"，但**端点被观测为推理模型后额外补一份思考额度**；
        无论怎么补都不超过配置里的上限（用户把上限配小是有意为之，要尊重）。
        """
        ceiling = int(self.config.max_tokens or _DEFAULT_REPLY_TOKENS)
        if not explicit:
            return max(1, ceiling)          # 没传 → 用配置值（这就是用户看到的那个数）
        want = int(explicit)
        if OpenAICompatibleLLM._reasoning_seen:
            want += _REASONING_ALLOWANCE
        return max(1, min(want, max(ceiling, int(explicit))))

    def _note_reasoning(self, data: dict) -> None:
        """按**真实响应**判断这是不是推理端点（不靠模型名猜），只记一次结论

        结论只用于内部：`_effective_max_tokens` 之后会给显式预算多留一份思考额度
        （见 §8.22）。**不打印日志** —— 与 `llm_no_think_strategy_ok` 同一类：
        成功路径上的实现细节，而本进程的 structlog 没有级别过滤（见
        `rag/observability/logging.py`），降级成 debug 也照样会打出来。
        """
        if OpenAICompatibleLLM._reasoning_seen or _reasoning_len(data) <= 0:
            return
        OpenAICompatibleLLM._reasoning_seen = True

    async def stream_generate(
        self,
        messages: list[dict],
        task: str = "generate",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        temp = self._safe_temperature(temperature)
        payload = {
            "model": self._model_for_task(task),
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_tokens
            or _DEFAULT_REPLY_TOKENS,
            "stream": True,
        }
        if temp is not None:
            payload["temperature"] = temp
        async with self._semaphore:
            async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        import json
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        if delta.get("content"):
                            yield delta["content"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    async def health_check(self) -> bool:
        try:
            resp = await self._client.get("/models", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False


# vLLM 与 openai_compatible 协议一致，仅注册别名
AdapterRegistry._classes["llm"]["vllm"] = OpenAICompatibleLLM
