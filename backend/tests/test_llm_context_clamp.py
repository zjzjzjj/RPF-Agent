"""LLM 客户端：上下文超限时的 max_tokens 自动钳制。

2026-10-20 实证：本地 Qwen3-8B（--max-model-len 24576）上，招标文件 20147 输入
tokens + 固定 max_tokens=8192 必然超限，vLLM 返回 400，compile 任务失败。
修复：从报错里解析出精确的上下文/输入 token 数，钳制后重试一次。
"""
from __future__ import annotations

import pytest

from app.llm.openai_compat import _clamp_max_tokens_for_context

# 真实的 vLLM 400 报文（来自服务器日志）
VLLM_400 = (
    "Error code: 400 - {'error': {'message': \"'max_tokens' or 'max_completion_tokens' "
    "is too large: 8192. This model's maximum context length is 24576 tokens and your "
    "request has 20147 input tokens (8192 > 24576 - 20147). (parameter=max_tokens, value=8192)\", "
    "'type': 'BadRequestError', 'param': None, 'code': 400}}"
)


def test_clamps_to_remaining_context() -> None:
    """24576 - 20147 - 64 = 4365（小于当前 8192 → 生效）。"""
    assert _clamp_max_tokens_for_context(VLLM_400, 8192) == 24576 - 20147 - 64


def test_no_clamp_when_already_small_enough() -> None:
    """当前 max_tokens 本来就放得下时不改动。"""
    assert _clamp_max_tokens_for_context(VLLM_400, 1000) is None


def test_no_clamp_when_no_room_left() -> None:
    """剩余空间连最小输出都不够 → 不改（交给上层显式失败，避免死循环）。"""
    no_room = VLLM_400.replace("20147 input tokens", "24500 input tokens")
    assert _clamp_max_tokens_for_context(no_room, 8192) is None


def test_no_clamp_without_explicit_max_tokens() -> None:
    assert _clamp_max_tokens_for_context(VLLM_400, None) is None


def test_ignores_unrelated_errors() -> None:
    assert _clamp_max_tokens_for_context("request timed out", 8192) is None
    assert _clamp_max_tokens_for_context("", 8192) is None


def test_deepseek_variant_message() -> None:
    """不同实现措辞不同：仍按同一正则解析。"""
    msg = (
        "This model's maximum context length is 65536 tokens. However, you requested "
        "70000 tokens (60000 in the messages, 10000 in the completion). "
        "Please reduce the length of the messages or completion."
    )
    # 该措辞不含 "request has N input tokens"，解析不到 → 不改（安全兜底）
    assert _clamp_max_tokens_for_context(msg, 10000) is None


@pytest.mark.parametrize(
    ("limit", "used", "expected"),
    [
        (24576, 20147, 4365),   # 剩余不足 8192 → 钳制
        (32768, 10000, None),   # 剩余 22704 > 8192 → 无需改动
        (8192, 7000, 1128),     # 剩余仍大于最小输出 → 钳制
        (8192, 8000, None),     # 剩余 128 < 512 → 不钳制
    ],
)
def test_boundaries(limit: int, used: int, expected: int | None) -> None:
    msg = (
        f"maximum context length is {limit} tokens and your request has "
        f"{used} input tokens (8192 > {limit} - {used})"
    )
    got = _clamp_max_tokens_for_context(msg, 8192)
    assert got == expected
