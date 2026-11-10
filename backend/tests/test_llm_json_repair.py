# -*- coding: utf-8 -*-
"""模型输出 JSON 的容错解析（2026-10-21 线上故障）。

真实报错：`规则抽取失败（含修复重试）（分块 1/2）：模型输出 JSON 无法解析：
Invalid \\escape: line 28 column 52`。8B 模型在 quotes/description 里输出
`\\（`、Windows 路径、`\\u-2026` 这类非法转义，`json.loads` 整段拒绝，编译中止。
"""
from __future__ import annotations

import json

import pytest

from app.llm.base import LLMError
from app.llm.gateway import (
    _repair_invalid_escapes,
    _strip_trailing_commas,
    extract_json,
)


def test_repairs_fullwidth_paren_escape() -> None:
    raw = r'{"a": "\（中文条款", "b": 1}'
    out = extract_json(raw)
    assert out["a"] == "\\（中文条款"
    assert out["b"] == 1


def test_repairs_windows_path_escape() -> None:
    # 注意：`\t` `\f` `\n` `\r` `\b` 本身是合法 JSON 转义，修复器不会改动它们
    #（否则会把真正的换行/制表符改坏）。这里用不含这些字母的路径。
    raw = r'{"path": "C:\Users\x\data\cfg"}'
    out = extract_json(raw)
    assert out["path"] == "C:\\Users\\x\\data\\cfg"


def test_valid_escape_letters_win_over_path_interpretation() -> None:
    """`\t` 是合法转义 → 按制表符解析（JSON 语义），这是刻意保留的行为。"""
    raw = r'{"path": "C:\temp"}'
    assert extract_json(raw)["path"] == "C:\temp"


def test_repairs_bad_unicode_escape() -> None:
    raw = r'{"a": "\u-2026 年度", "b": 2}'
    out = extract_json(raw)
    assert out["a"].startswith("\\u-2026")


def test_keeps_valid_escapes_intact() -> None:
    raw = r'{"nl": "line1\nline2", "bs": "C:\\path", "q": "a\"b", "u": "\u4e2d"}'
    out = extract_json(raw)
    assert out["nl"] == "line1\nline2"
    assert out["bs"] == "C:\\path"
    assert out["q"] == 'a"b'
    assert out["u"] == "中"


def test_allows_raw_newline_inside_string() -> None:
    raw = '{"a": "line1\nline2", "b": 3}'  # 字符串内是真实换行
    out = extract_json(raw)
    assert out["a"] == "line1\nline2"


def test_strips_trailing_comma() -> None:
    raw = '{"a": [1, 2,], "b": {"c": 1,},}'
    out = extract_json(raw)
    assert out == {"a": [1, 2], "b": {"c": 1}}


def test_trailing_comma_inside_string_not_touched() -> None:
    raw = '{"a": "x,}", "b": 1}'
    assert extract_json(raw) == {"a": "x,}", "b": 1}
    text, fixed = _strip_trailing_commas(raw)
    assert fixed == 0 and text == raw


def test_repair_does_not_touch_outside_strings() -> None:
    text, fixed = _repair_invalid_escapes('{"a": 1}\n说明\\文字')
    assert fixed == 0
    assert text == '{"a": 1}\n说明\\文字'


def test_truncated_still_raises() -> None:
    with pytest.raises(LLMError, match="不完整"):
        extract_json('{"a": 1, "b": "未闭合')


def test_fenced_and_prefixed_output() -> None:
    raw = '说明如下：\n```json\n{"a": 1}\n```\n以上。'
    assert extract_json(raw) == {"a": 1}


def test_double_repair_is_idempotent() -> None:
    raw = r'{"a": "\（x", "b": "C:\d"}'
    once, n1 = _repair_invalid_escapes(raw)
    twice, n2 = _repair_invalid_escapes(once)
    assert n1 == 2 and n2 == 0
    assert json.loads(once) == json.loads(twice)


def test_mixed_valid_and_invalid_escapes_survive_roundtrip() -> None:
    raw = r'{"desc": "运维\（含税）\n金额 80 万元，路径 C:\opt", "n": 3}'
    out = extract_json(raw)
    assert out["desc"].startswith("运维\\（含税）\n")
    assert "C:\\opt" in out["desc"]
    assert out["n"] == 3


# ---------------------------------------------------------------------------
# 经过 Gateway.chat_json 的端到端路径（所有模型输出解析的唯一入口）
# ---------------------------------------------------------------------------
class _Out(__import__("pydantic").BaseModel):
    a: str


class _FakeProvider:
    name = "fake"
    model = "fake-model"
    host = "localhost"

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def complete(self, system, user, *, temperature=0.0, max_tokens=None):  # noqa: ANN001
        from app.llm.base import LLMResult

        self.calls += 1
        return LLMResult(text=self.text, provider=self.name, model=self.model)


def test_chat_json_repairs_and_calls_model_once() -> None:
    from app.llm.gateway import Gateway

    fake = _FakeProvider(r'{"a": "见\（附件）与 C:\opt"}')
    out, _res = Gateway(provider=fake).chat_json("sys", "user", _Out)
    assert out.a == "见\\（附件）与 C:\\opt"
    assert fake.calls == 1


def test_chat_json_error_does_not_leak_model_text() -> None:
    """错误信息不得回显模型原文（confidential 合规：会进日志/job.error/前端）。"""
    from app.llm.gateway import Gateway

    secret = "浙江中烟机密条款：报价不得低于成本"
    fake = _FakeProvider(f"没有 JSON，只有{secret}")
    with pytest.raises(LLMError) as ei:
        Gateway(provider=fake).chat_json("sys", "user", _Out)
    assert secret not in str(ei.value)


def test_chat_json_validation_error_drops_input_value() -> None:
    from app.llm.gateway import Gateway

    fake = _FakeProvider('{"a": 123}')  # 契约要求 str，pydantic 会校验失败
    with pytest.raises(LLMError) as ei:
        Gateway(provider=fake).chat_json("sys", "user", _Out)
    msg = str(ei.value)
    assert "不符合契约" in msg
    assert "123" not in msg, "errors() 的 input 字段（模型输出的原值）不得外泄"
