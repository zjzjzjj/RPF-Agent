# -*- coding: utf-8 -*-
"""数据分级闸门：涉密数据只准本地端点，显式开关可放开。

2026-10-21：切换到云端模型（硅基流动 GLM-5.3-Flash）后，真实招标/投标原文会
离开本机。这不是默认行为——必须由部署方显式设置
`BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL=1`，且网关会记一次性告警。
"""
from __future__ import annotations

import logging

import pytest

from app.errors import DataClassDenied
from app.llm.gateway import Gateway


class _Provider:
    name = "openai_compat"
    model = "zai-org/GLM-5.3-Flash"
    host = "api.siliconflow.cn"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system, user, *, temperature=0.0, max_tokens=None):  # noqa: ANN001
        from app.llm.base import LLMResult

        self.calls += 1
        return LLMResult(text="{}", provider=self.name, model=self.model)


class _LocalProvider(_Provider):
    host = "127.0.0.1"


def _reset_warned() -> None:
    from app.llm import gateway as gw

    gw._REMOTE_CONFIDENTIAL_WARNED.clear()


def test_confidential_to_remote_denied_by_default(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL", raising=False)
    _reset_warned()
    with pytest.raises(DataClassDenied):
        Gateway(provider=_Provider()).chat("sys", "user", data_class="confidential")


def test_confidential_to_remote_allowed_with_explicit_flag(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL", "1")
    _reset_warned()
    provider = _Provider()
    result = Gateway(provider=provider).chat("sys", "user", data_class="confidential")
    assert result.text == "{}"
    assert provider.calls == 1


def test_warning_logged_only_once_per_host(monkeypatch, caplog) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL", "1")
    _reset_warned()
    gateway = Gateway(provider=_Provider())
    with caplog.at_level(logging.WARNING, logger="bid_eval.llm"):
        gateway.chat("sys", "user1", data_class="confidential")
        gateway.chat("sys", "user2", data_class="confidential")
    hits = [r for r in caplog.records if "涉密数据将发往非本地端点" in r.getMessage()]
    assert len(hits) == 1, "同一端点只告警一次，避免每次调用刷屏"


def test_synthetic_unaffected_by_flag(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL", raising=False)
    _reset_warned()
    provider = _Provider()
    assert Gateway(provider=provider).chat("sys", "user", data_class="synthetic").text == "{}"


def test_local_endpoint_always_allowed(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL", raising=False)
    _reset_warned()
    provider = _LocalProvider()
    assert Gateway(provider=provider).chat("sys", "user", data_class="confidential").text == "{}"


def test_private_ip_treated_as_local(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL", raising=False)
    _reset_warned()

    class _Private(_Provider):
        host = "192.168.1.10"

    assert Gateway(provider=_Private()).chat("sys", "user", data_class="confidential").text == "{}"


def test_health_exposes_flag(client) -> None:  # noqa: ANN001
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["allow_remote_confidential"] is False
