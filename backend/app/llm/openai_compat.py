"""OpenAI 兼容 provider：接 DeepSeek / Qwen / GLM 等（base_url 可配）。

只依赖 openai SDK 的 chat.completions 接口——这是国内各家云服务的最小公共面。
"""
from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import urlparse

from app import config
from app.errors import AppError, LLMNotConfigured
from app.llm.base import LLMError, LLMResult
from app.llm.request_control import check_cancelled, cooperative_sleep, http_attempt
from app.services.execution_version import current_execution_manifest, execution_config_value

# 推理模型（DeepSeek-V4 / GLM 混合思考等）常把 <think>…</think> 内联在 content
# 里，思考文本含花括号会污染下游 extract_json 的首个"{"定位——在源头剥掉。
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# 上下文窗口超限报错（vLLM/OpenAI 兼容端点的两种常见措辞）
_CTX_LIMIT_RE = re.compile(r"maximum context length is (\d+)", re.IGNORECASE)
_CTX_INPUT_RE = re.compile(r"request has (\d+) input tokens", re.IGNORECASE)
_CTX_MIN_OUTPUT = 512  # 钳制后至少留这么多输出空间，否则交给上层显式失败


def _clamp_max_tokens_for_context(message: str, current: int | None) -> int | None:
    """从「输入 + max_tokens 超过模型上下文」的报错里算出可用输出上限。

    本地小上下文模型（如 Qwen3-8B --max-model-len 24576）上，固定 max_tokens=8192
    遇到 20K tokens 的招标文件会必然超限（2026-10-20 实证：compile 400）。
    云端大上下文模型不会触发该分支，行为不变。
    """
    if current is None:
        return None  # 未显式限制输出，无可钳制
    limit = _CTX_LIMIT_RE.search(message)
    used = _CTX_INPUT_RE.search(message)
    if not limit or not used:
        return None
    available = int(limit.group(1)) - int(used.group(1)) - 64
    if available < _CTX_MIN_OUTPUT or available >= current:
        return None
    return available


class OpenAICompatProvider:
    controls_http_attempts = True
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ):
        key = api_key or config.llm_api_key()
        url = base_url or execution_config_value("llm_base_url")
        # 配置缺失必须抛 LLMNotConfigured（AppError）而非 LLMError：
        # 网关惰性解析后，构造发生在首次调用处——抛 LLMError 会被逐项降级处理器
        # 吞掉（auto 模式静默把模糊项判 fail），配置错误必须让整个运行显式失败。
        if not key:
            raise LLMNotConfigured(
                "已配置 provider 但缺少 API Key：请设置 BID_EVAL_LLM_API_KEY（或在仓库根目录 .env 中配置）"
            )
        if not url:
            raise LLMNotConfigured(
                "已配置 provider 但缺少 base_url：请设置 BID_EVAL_LLM_BASE_URL（如 https://api.deepseek.com/v1）"
            )

        from openai import OpenAI

        self.name = "openai_compat"
        self.model = model or config.llm_model()
        # 完整 base_url 进 LLM 缓存键（2026-10-30 阶段 1）：同 host 不同路径前缀
        # 可能是不同端点/不同模型服务，只用 host 会把两者混成同一缓存键。
        self.base_url = url
        self.host = (urlparse(url).hostname or "").lower()
        self._client = OpenAI(
            api_key=key,
            base_url=url,
            timeout=timeout or config.llm_timeout_s(),
            # Every physical request must acquire a shared slot and budget.
            # Hidden SDK retries would evade both counters.
            max_retries=0,
        )

    @staticmethod
    def _extra_body() -> dict:
        """服务商扩展参数：思考开关（云端通用）+ vLLM 专用开关 + seed。

        - `thinking: {"type": "disabled"}`：硅基流动等云端对推理模型（GLM-5.3-Flash）
          的通用写法，实测有效（2026-10-21）；`enable_thinking`/
          `chat_template_kwargs` 在云端**无效**。
        - `chat_template_kwargs` 只对本地 vLLM 有用，保留以兼容既有部署。
        """
        extra: dict = {}
        thinking = execution_config_value("llm_thinking")
        if thinking in ("disabled", "enabled"):
            extra["thinking"] = {"type": thinking}
        if execution_config_value("llm_disable_thinking"):
            extra["chat_template_kwargs"] = {"enable_thinking": False}
        if execution_config_value("llm_seed") is not None:
            extra["seed"] = execution_config_value("llm_seed")
        return extra

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        images: list[str] | None = None,
    ) -> LLMResult:
        started = time.time()
        extra_body = self._extra_body()
        # 推理模型的思考也算在 max_tokens 里：不给预算会被思考耗尽 → content 为空
        if max_tokens is None:
            max_tokens = execution_config_value("llm_default_max_tokens")
        attempts = min(4, max(1, config.llm_transient_attempts())) if config.llm_transient_retries() else 1
        from app.services.execution_version import continue_after_errors

        if continue_after_errors():
            attempts = 1
        result: LLMResult | None = None
        last_error: Exception | None = None
        # 上下文钳制重试用**独立预算**，不占用瞬态重试次数
        clamp_retries = 1
        attempt = 1
        content = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": image}}
                for image in images
            ]
        while attempt <= attempts:
            check_cancelled()
            entered_attempt = False
            try:
                with http_attempt(provider=self.name, model=self.model,
                                  images=len(images or [])) as request:
                    entered_attempt = True
                    resp = self._client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": content},
                        ],
                        temperature=temperature,
                        max_tokens=max_tokens,
                        extra_body=extra_body or None,
                    )
                    usage = getattr(resp, "usage", None)
                    request.tokens_in = getattr(usage, "prompt_tokens", None)
                    request.tokens_out = getattr(usage, "completion_tokens", None)
                choice = resp.choices[0] if resp.choices else None
                text = (choice.message.content if choice and choice.message else "") or ""
                text = _THINK_RE.sub("", text).strip()
                if not text and getattr(choice, "finish_reason", None) == "length":
                    # 推理模型把输出预算耗在思考上：content 为空但 token 已用尽。
                    # 直接说清楚，避免上层报"找不到 JSON（共 0 字符）"这种误导信息。
                    raise LLMError(
                        "模型输出被截断：推理过程耗尽了 max_tokens（content 为空）；"
                        "请调大 BID_EVAL_LLM_DEFAULT_MAX_TOKENS / BID_EVAL_LLM_MAX_TOKENS，"
                        "或设置 BID_EVAL_LLM_THINKING=disabled"
                    )
                usage = getattr(resp, "usage", None)
                result = LLMResult(
                    text=text,
                    provider=self.name,
                    model=self.model,
                    tokens_in=getattr(usage, "prompt_tokens", None),
                    tokens_out=getattr(usage, "completion_tokens", None),
                    latency_ms=int((time.time() - started) * 1000),
                )
                break
            except Exception as exc:  # noqa: BLE001
                from app.jobs.queue import JobCancelled

                if not entered_attempt or isinstance(exc, (AppError, JobCancelled)):
                    raise
                last_error = exc
                raw = str(exc)
                msg = raw.lower()
                status = getattr(exc, "status_code", None)
                if status is None:
                    status = getattr(getattr(exc, "response", None), "status_code", None)

                # ① 上下文窗口不足：把 max_tokens 钳到剩余空间后立即重试
                clamped = _clamp_max_tokens_for_context(raw, max_tokens)
                if clamped is not None and current_execution_manifest() is not None:
                    from app.llm.request_sizing import ReviewContextExceeded

                    raise ReviewContextExceeded("模型上下文不足，须拆分完整材料或事实组；禁止降低冻结输出预算") from exc
                if clamped is not None and clamp_retries > 0:
                    clamp_retries -= 1
                    logging.getLogger("bid_eval.llm").warning(
                        "模型上下文不足，max_tokens %s → %d 后重试", max_tokens, clamped
                    )
                    max_tokens = clamped
                    continue  # 不递增 attempt，不消耗瞬态预算

                if isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 429):
                    # Re-reading documents cannot repair a provider rejection.
                    # Preserve the failure and stop at this boundary; never
                    # rewrite material to evade a content-policy response.
                    if status == 451:
                        message = "模型服务商拒绝该批材料请求（HTTP 451）；须由服务商核查，原件和已完成结果已保留"
                    else:
                        message = f"模型服务商拒绝请求（HTTP {status}），请核查账号、权限或请求配置"
                    error = LLMError(message)
                    error.review_attempts_exhausted = True
                    raise error from exc

                # ② 瞬态失败（网络/限流/服务波动）重试——分层评审调用量大，
                # 单次偶发超时不重试会让整项评分随机降级（2026-10-16 实证：
                # 一次 timed out 让龙即一个评分项被兜底 0 分）
                transient = status in (408, 409, 429, 500, 502, 503, 504) or any(
                    token in msg
                    for token in ("timeout", "timed out", "rate limit", "429", "500", "502", "503", "504", "connection")
                )
                if transient and attempt < attempts:
                    delay = _retry_delay(exc, attempt)
                    logging.getLogger("bid_eval.llm").warning(
                        "瞬态失败（第 %d 次），%.2fs 后重试：%s", attempt, delay, type(exc).__name__
                    )
                    cooperative_sleep(delay)
                    attempt += 1
                    continue
                raise LLMError(f"模型调用失败：{exc}", transient=transient) from exc
        if result is None:
            raise LLMError(
                f"模型调用失败：{last_error}", transient=True
            ) from last_error
        return result


def _retry_delay(error: Exception, attempt: int) -> float:
    """Honor a bounded Retry-After; never retain an HTTP slot during backoff."""
    headers = getattr(getattr(error, "response", None), "headers", {}) or {}
    try:
        retry_after = float(headers.get("retry-after") or headers.get("Retry-After") or 0)
    except (TypeError, ValueError):
        retry_after = 0
    if retry_after > 0:
        return min(retry_after, 60.0)
    return 5 * attempt + random.uniform(0, 0.5)
