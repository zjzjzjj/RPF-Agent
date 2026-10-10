"""OpenAI 兼容 provider：接 DeepSeek / Qwen / GLM 等（base_url 可配）。

只依赖 openai SDK 的 chat.completions 接口——这是国内各家云服务的最小公共面。
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

from app import config
from app.llm.base import LLMError, LLMResult


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
        if not key:
            raise LLMError("未配置 API Key（BID_EVAL_LLM_API_KEY）")
        if not url:
            raise LLMError("未配置 base_url（BID_EVAL_LLM_BASE_URL）")

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
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            transient = any(
                token in msg
                for token in ("timeout", "timed out", "rate limit", "429", "500", "502", "503", "504", "connection")
            )
            raise LLMError(f"模型调用失败：{exc}", transient=transient) from exc

        choice = resp.choices[0] if resp.choices else None
        text = (choice.message.content if choice and choice.message else "") or ""
        usage = getattr(resp, "usage", None)
        return LLMResult(
            text=text,
            provider=self.name,
            model=self.model,
            tokens_in=getattr(usage, "prompt_tokens", None),
            tokens_out=getattr(usage, "completion_tokens", None),
            latency_ms=int((time.time() - started) * 1000),
        )
