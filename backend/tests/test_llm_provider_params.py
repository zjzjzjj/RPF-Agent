# -*- coding: utf-8 -*-
"""云端模型参数：思考开关、输出预算、推理模型空输出。

2026-10-21 实测（硅基流动 zai-org/GLM-5.3-Flash）：
- 它是推理模型：先 reasoning_content 再 content，思考计入 max_tokens；
  max_tokens 太小时 **content 为空**（finish_reason=length）。
- `thinking: {"type": "disabled"}` 被接受；`enable_thinking` / `chat_template_kwargs`
  在云端无效（后者是 vLLM 专用）。
"""
from __future__ import annotations

import types

import pytest

from app.llm.base import LLMError


class _FakeCompletions:
    def __init__(self, content: str, finish_reason: str = "stop"):
        self.content = content
        self.finish_reason = finish_reason
        self.calls: list[dict] = []

    def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        message = types.SimpleNamespace(content=self.content)
        choice = types.SimpleNamespace(message=message, finish_reason=self.finish_reason)
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=20)
        return types.SimpleNamespace(choices=[choice], usage=usage)


def _provider(monkeypatch, content='"{}"', finish_reason: str = "stop"):  # noqa: ANN001
    from app.llm.openai_compat import OpenAICompatProvider

    monkeypatch.setenv("BID_EVAL_LLM_PROVIDER", "siliconflow")
    monkeypatch.setenv("BID_EVAL_LLM_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("BID_EVAL_LLM_API_KEY", "test-key")
    monkeypatch.setenv("BID_EVAL_LLM_MODEL", "zai-org/GLM-5.3-Flash")
    monkeypatch.setenv("BID_EVAL_LLM_DISABLE_THINKING", "0")
    monkeypatch.delenv("BID_EVAL_LLM_SEED", raising=False)
    monkeypatch.setenv("BID_EVAL_LLM_TRANSIENT_RETRIES", "0")
    p = OpenAICompatProvider()
    fake = _FakeCompletions(content, finish_reason)
    p._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=fake)
    )
    return p, fake


def test_thinking_disabled_sent(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_LLM_THINKING", "disabled")
    p, fake = _provider(monkeypatch)
    p.complete("sys", "user")
    assert fake.calls[0]["extra_body"]["thinking"] == {"type": "disabled"}


def test_thinking_enabled_sent(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_LLM_THINKING", "enabled")
    p, fake = _provider(monkeypatch)
    p.complete("sys", "user")
    assert fake.calls[0]["extra_body"]["thinking"] == {"type": "enabled"}


def test_thinking_auto_omits_param(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_LLM_THINKING", "auto")
    p, fake = _provider(monkeypatch)
    p.complete("sys", "user")
    assert "thinking" not in (fake.calls[0]["extra_body"] or {})


def test_vllm_chat_template_kwargs_still_supported(monkeypatch) -> None:  # noqa: ANN001
    """本地 vLLM 的旧开关保留，云端新开关互不干扰。"""
    monkeypatch.setenv("BID_EVAL_LLM_THINKING", "auto")
    p, fake = _provider(monkeypatch)
    monkeypatch.setenv("BID_EVAL_LLM_DISABLE_THINKING", "1")  # 环境在调用时读取
    p.complete("sys", "user")
    body = fake.calls[0]["extra_body"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "thinking" not in body


def test_default_max_tokens_applied_when_not_given(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_LLM_DEFAULT_MAX_TOKENS", "2048")
    p, fake = _provider(monkeypatch)
    p.complete("sys", "user")
    assert fake.calls[0]["max_tokens"] == 2048


def test_explicit_max_tokens_not_overridden(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_LLM_DEFAULT_MAX_TOKENS", "2048")
    p, fake = _provider(monkeypatch)
    p.complete("sys", "user", max_tokens=20000)
    assert fake.calls[0]["max_tokens"] == 20000


def test_empty_content_with_length_raises_actionable_error(monkeypatch) -> None:  # noqa: ANN001
    """推理模型把预算耗在思考上：必须报"推理耗尽 max_tokens"，不能静默返回空。"""
    p, _fake = _provider(monkeypatch, content="", finish_reason="length")
    with pytest.raises(LLMError) as ei:
        p.complete("sys", "user")
    assert "推理" in str(ei.value) and "max_tokens" in str(ei.value)


def test_empty_content_with_stop_is_not_an_error(monkeypatch) -> None:  # noqa: ANN001
    """finish_reason=stop 的空回答不按截断处理（交给上层判空）。"""
    p, _fake = _provider(monkeypatch, content="", finish_reason="stop")
    out = p.complete("sys", "user")
    assert out.text == ""
