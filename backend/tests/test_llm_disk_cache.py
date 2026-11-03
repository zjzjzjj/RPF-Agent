# -*- coding: utf-8 -*-
"""LLM 磁盘缓存（阶段 1，2026-10-30）。

机制：chat_json 校验通过的结果落 data/llm_cache/<key16>.json；内存缓存未命中时
先查磁盘（命中回填内存、cached=True、不耗预算）；坏文件/缺字段静默当 miss；
解析/契约失败重试前驱逐该 key 的内存+磁盘缓存（修"重试命中自缓存坏文本"缺陷）；
BID_EVAL_LLM_DISK_CACHE=0 时行为与改前一致（纯内存缓存）。

pytest 进程内缓存默认关（见 config.llm_disk_cache_enabled 的 2026-10-30 实证：
存量 chat_json 用例共用缓存键且不隔离 data_dir）。本文件用例显式置 1 开启，
并在临时 BID_EVAL_DATA_DIR 下运行，不碰真实 data/llm_cache。
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from app import config
from app.llm import gateway as gw
from app.llm.base import LLMError, LLMResult
from app.llm.gateway import Gateway


class _Out(BaseModel):
    a: str


class _FakeProvider:
    name = "fake"
    model = "fake-model"
    host = "localhost"
    base_url = "http://fake.local/v1"

    def __init__(self, text: str = '{"a": "ok"}') -> None:
        self.text = text
        self.calls = 0

    def complete(self, system, user, *, temperature=0.0, max_tokens=None):  # noqa: ANN001
        self.calls += 1
        return LLMResult(text=self.text, provider=self.name, model=self.model)


class _SequenceProvider(_FakeProvider):
    def __init__(self, texts: list[str]) -> None:
        super().__init__(texts[0] if texts else "")
        self.texts = texts
        self.users: list[str] = []

    def complete(self, system, user, *, temperature=0.0, max_tokens=None):  # noqa: ANN001
        self.users.append(user)
        index = min(self.calls, len(self.texts) - 1)
        self.text = self.texts[index]
        return super().complete(
            system, user, temperature=temperature, max_tokens=max_tokens
        )


@pytest.fixture()
def disk_env(tmp_path, monkeypatch):  # noqa: ANN001
    """隔离环境：临时 data 目录 + 显式开启磁盘缓存 + 清缓存统计/预算计数。"""
    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BID_EVAL_LLM_DISK_CACHE", "1")  # pytest 内默认关，本用例组显式开
    monkeypatch.delenv("BID_EVAL_LLM_DAILY_BUDGET", raising=False)
    gw._CACHE_STATS.update({"mem": 0, "disk": 0, "miss": 0})
    gw._BUDGET_STATE.update({"day": "", "calls": 0})
    yield tmp_path
    gw._CACHE_STATS.update({"mem": 0, "disk": 0, "miss": 0})
    gw._BUDGET_STATE.update({"day": "", "calls": 0})


def test_disk_cache_flag_semantics(monkeypatch) -> None:  # noqa: ANN001
    """显式 0/1 优先；未设置时生产开、pytest 进程内关（2026-10-30 实证保护存量用例）。"""
    monkeypatch.delenv("BID_EVAL_LLM_DISK_CACHE", raising=False)
    assert config.llm_disk_cache_enabled() is False, "pytest 内未显式设置应默认关"
    monkeypatch.setenv("BID_EVAL_LLM_DISK_CACHE", "1")
    assert config.llm_disk_cache_enabled() is True
    monkeypatch.setenv("BID_EVAL_LLM_DISK_CACHE", "0")
    assert config.llm_disk_cache_enabled() is False


def test_disk_cache_hit_across_gateway_instances(disk_env) -> None:  # noqa: ANN001
    """首次 miss 调 provider 并落盘；新 Gateway 实例（内存空）命中磁盘。"""
    fake = _FakeProvider('{"a": "hello"}')
    out1, res1 = Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert out1.a == "hello" and fake.calls == 1 and res1.cached is False
    assert gw._CACHE_STATS["miss"] == 1
    files = list((disk_env / "llm_cache").glob("*.json"))
    assert len(files) == 1, "校验通过的结果应落盘"

    out2, res2 = Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert fake.calls == 1, "第二次（新实例）应命中磁盘，provider 零调用"
    assert res2.cached is True and out2.a == "hello"
    assert gw._CACHE_STATS["disk"] == 1 and gw._CACHE_STATS["mem"] == 0


def test_contract_retry_changes_only_retry_user_and_repopulates_original_key(disk_env) -> None:  # noqa: ANN001
    """契约失败后的请求可修复，但原始请求仍复用规范缓存键。"""
    fake = _SequenceProvider(['{"wrong": "x"}', '{"a": "fixed"}'])
    g = Gateway(provider=fake)

    out, res = g.chat_json("sys", "user", _Out, prompt_version="v1")
    assert out.a == "fixed" and res.cached is False
    assert fake.calls == 2
    assert fake.users[0] == "user"
    assert fake.users[1] != "user"
    assert "顶层必须包含这些字段：a" in fake.users[1]

    out2, res2 = g.chat_json("sys", "user", _Out, prompt_version="v1")
    assert out2.a == "fixed" and res2.cached is True
    assert fake.calls == 2, "修复成功后原始请求键应直接命中缓存"

    out3, res3 = Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert out3.a == "fixed" and res3.cached is True
    assert fake.calls == 2, "新 Gateway 也应命中原始磁盘键"


def test_contract_retry_failed_attempt_keys_not_cached(disk_env) -> None:  # noqa: ANN001
    """每次契约重试使用独立键，三次失败不留下坏缓存。"""
    fake = _SequenceProvider(['{"wrong": "x"}'] * 3)
    with pytest.raises(LLMError, match="不符合契约"):
        Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert fake.calls == 3
    assert len(fake.users) == 3
    assert len(set(fake.users)) == 3
    assert not list((disk_env / "llm_cache").glob("*.json"))


def test_memory_cache_hit_within_instance(disk_env) -> None:  # noqa: ANN001
    """同一 Gateway 实例二次调用走内存缓存（mem 统计 +1），不碰磁盘/预算。"""
    fake = _FakeProvider('{"a": "ok"}')
    g = Gateway(provider=fake)
    g.chat_json("sys", "user", _Out, prompt_version="v1")
    out, res = g.chat_json("sys", "user", _Out, prompt_version="v1")
    assert fake.calls == 1 and res.cached is True and out.a == "ok"
    assert gw._CACHE_STATS == {"mem": 1, "disk": 0, "miss": 1}


def test_corrupt_or_incomplete_cache_file_treated_as_miss(disk_env) -> None:  # noqa: ANN001
    """坏 JSON / 缺必需字段的缓存文件 = miss（静默回退 provider），不抛错。"""
    fake = _FakeProvider('{"a": "v1"}')
    Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    path = next((disk_env / "llm_cache").glob("*.json"))

    # ① 文件不是合法 JSON
    path.write_text("{{{不是合法 JSON", encoding="utf-8")
    out, res = Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert fake.calls == 2 and res.cached is False and out.a == "v1"

    # ② 合法 JSON 但缺必需字段（text/provider/model）
    path.write_text(json.dumps({"provider": "fake"}), encoding="utf-8")
    out, res = Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert fake.calls == 3 and res.cached is False and out.a == "v1"


def test_unparseable_text_retries_not_poisoned(disk_env) -> None:  # noqa: ANN001
    """不可解析文本：3 次都真正调 provider，且不落盘（重试不被自缓存毒化）。"""
    fake = _FakeProvider("完全不是 JSON 的文本")
    with pytest.raises(LLMError):
        Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert fake.calls == 3
    assert not list((disk_env / "llm_cache").glob("*.json")), "校验失败的结果不得上盘"


def test_validation_failure_retries_not_poisoned(disk_env) -> None:  # noqa: ANN001
    """契约失败（可解析但违反模型）：同样 3 次真调用、不落盘。"""
    fake = _FakeProvider('{"a": 123}')  # _Out.a 要求 str → ValidationError
    with pytest.raises(LLMError, match="不符合契约"):
        Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert fake.calls == 3
    assert not list((disk_env / "llm_cache").glob("*.json")), "契约失败的结果不得上盘"


def test_invalid_cached_text_evicted_and_recalled(disk_env) -> None:  # noqa: ANN001
    """盘上文本能解析成 JSON 但违反契约：驱逐坏缓存 → 重试真正调 provider。"""
    fake = _FakeProvider('{"a": "good"}')
    Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    path = next((disk_env / "llm_cache").glob("*.json"))
    path.write_text(
        json.dumps(
            {
                "provider": "fake",
                "model": "fake-model",
                "text": '{"a": 123}',
                "tokens_in": None,
                "tokens_out": None,
                "latency_ms": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out, res = Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert fake.calls == 2, "坏缓存应被驱逐并真正调 provider"
    assert out.a == "good" and res.cached is False


def test_cache_key_covers_thinking_seed_base_url() -> None:
    """thinking / disable_thinking / seed / base_url 任一维度变化 → 键必须变化。"""
    base = Gateway._cache_key("s", "u", "p", "m", "v")
    assert Gateway._cache_key("s", "u", "p", "m", "v") == base, "同参数键必须稳定"
    assert Gateway._cache_key("s", "u", "p", "m", "v", thinking="disabled") != base
    assert Gateway._cache_key("s", "u", "p", "m", "v", disable_thinking=True) != base
    assert Gateway._cache_key("s", "u", "p", "m", "v", seed=7) != base
    assert Gateway._cache_key("s", "u", "p", "m", "v", base_url="http://other/v1") != base


def test_disk_hit_does_not_consume_budget(disk_env, monkeypatch) -> None:  # noqa: ANN001
    """磁盘命中不耗预算：预算=1 已被首次真实调用用尽，第二次仍应成功。"""
    monkeypatch.setenv("BID_EVAL_LLM_DAILY_BUDGET", "1")
    fake = _FakeProvider('{"a": "ok"}')
    Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert gw._BUDGET_STATE["calls"] == 1

    out, res = Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert res.cached is True and out.a == "ok"
    assert gw._BUDGET_STATE["calls"] == 1, "磁盘命中不得再耗预算（否则会被上限拦下）"
    assert fake.calls == 1


def test_disk_cache_disabled_env0(disk_env, monkeypatch) -> None:  # noqa: ANN001
    """BID_EVAL_LLM_DISK_CACHE=0：行为与改前一致——不落盘、第二次也调 provider。"""
    monkeypatch.setenv("BID_EVAL_LLM_DISK_CACHE", "0")
    fake = _FakeProvider('{"a": "ok"}')
    Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    out, res = Gateway(provider=fake).chat_json("sys", "user", _Out, prompt_version="v1")
    assert fake.calls == 2 and res.cached is False and out.a == "ok"
    assert not (disk_env / "llm_cache").exists(), "开关关闭时不应创建缓存目录"
