"""OpenAI 兼容 provider：接 DeepSeek / Qwen / GLM 等（base_url 可配）。

只依赖 openai SDK 的 chat.completions 接口——这是国内各家云服务的最小公共面。
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse

from app import config
from app.errors import LLMNotConfigured
from app.llm.base import LLMError, LLMResult

# 推理模型（DeepSeek-V4 / GLM 混合思考等）常把 <think>…</think> 内联在 content
# 里，思考文本含花括号会污染下游 extract_json 的首个"{"定位——在源头剥掉。
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class OpenAICompatProvider:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ):
        key = api_key or config.llm_api_key()
        url = base_url or config.llm_base_url()
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
        self.host = (urlparse(url).hostname or "").lower()
        self._client = OpenAI(api_key=key, base_url=url, timeout=timeout or config.llm_timeout_s())

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResult:
        started = time.time()
        extra_body: dict = {}
        if config.llm_disable_thinking():
            extra_body["chat_template_kwargs"] = {"enable_thinking": False}
        if config.llm_seed() is not None:
            extra_body["seed"] = config.llm_seed()
        attempts = 2 if config.llm_transient_retries() else 1
        result: LLMResult | None = None
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    extra_body=extra_body or None,
                )
                choice = resp.choices[0] if resp.choices else None
                text = (choice.message.content if choice and choice.message else "") or ""
                text = _THINK_RE.sub("", text).strip()
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
                last_error = exc
                msg = str(exc).lower()
                transient = any(
                    token in msg
                    for token in ("timeout", "timed out", "rate limit", "429", "500", "502", "503", "504", "connection")
                )
                # 瞬态失败（网络/限流/服务波动）重试——分层评审调用量大，
                # 单次偶发超时不重试会让整项评分随机降级（2026-10-16 实证：
                # 一次 timed out 让龙即一个评分项被兜底 0 分）
                if transient and attempt < attempts:
                    delay = 5 * attempt
                    logging.getLogger("bid_eval.llm").warning(
                        "瞬态失败（第 %d 次），%ds 后重试：%s", attempt, delay, msg[:120]
                    )
                    time.sleep(delay)
                    continue
                raise LLMError(f"模型调用失败：{exc}", transient=transient) from exc
        if result is None:
            raise LLMError(
                f"模型调用失败：{last_error}", transient=True
            ) from last_error
        return result
