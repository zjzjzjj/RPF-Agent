"""Physical HTTP limits, persistent budgets and retry boundaries stay honest."""
from __future__ import annotations

import multiprocessing
import os
import queue
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from app.errors import LLMBudgetExceeded, LLMNotConfigured
from app.jobs.queue import JobCancelled
from app.llm import request_control as control
from app.llm.base import LLMResult
from app.llm.gateway import Gateway


@pytest.fixture
def request_env(tmp_path, monkeypatch):
    from app.db import session as db

    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BID_EVAL_DB_PATH", str(tmp_path / "request-test.db"))
    monkeypatch.setenv("BID_EVAL_LLM_CONCURRENCY", "2")
    monkeypatch.setenv("BID_EVAL_LLM_DAILY_BUDGET", "0")
    monkeypatch.setenv("BID_EVAL_LLM_DISK_CACHE", "0")
    db.reset_engine()
    with db.get_engine().begin() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS llm_request_daily_budget "
                                "(day_utc VARCHAR(10) PRIMARY KEY,calls INTEGER NOT NULL DEFAULT 0)"))
    control.reset_request_metrics()
    yield tmp_path
    db.reset_engine()


def _hold_process_slot(directory, entered, release_path):
    os.environ["BID_EVAL_DATA_DIR"] = directory
    os.environ["BID_EVAL_LLM_CONCURRENCY"] = "2"
    os.environ["BID_EVAL_LLM_DAILY_BUDGET"] = "0"
    from app.llm.request_control import http_attempt

    with http_attempt(provider="offline-test", model="offline-test"):
        entered.put(os.getpid())
        entered.close()
        entered.join_thread()
        deadline = time.monotonic() + 20
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.02)


def _budget_calls():
    from app.db.session import get_engine

    with get_engine().connect() as connection:
        return connection.execute(text("SELECT COALESCE(SUM(calls),0) FROM llm_request_daily_budget")).scalar_one()


def _provider(monkeypatch, create):
    from app.llm.openai_compat import OpenAICompatProvider

    monkeypatch.setenv("BID_EVAL_LLM_TRANSIENT_RETRIES", "1")
    monkeypatch.setenv("BID_EVAL_LLM_TRANSIENT_ATTEMPTS", "4")
    provider = OpenAICompatProvider(api_key="offline-key", base_url="http://localhost/v1", model="offline-model")
    provider._client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=create)))
    return provider


def _response(content='{"answer":"ok"}'):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content=content), finish_reason="stop")],
        usage=types.SimpleNamespace(prompt_tokens=7, completion_tokens=11))


def test_cross_gateway_instances_share_one_request_limit(request_env):
    active = 0
    peak = 0
    lock = threading.Lock()

    class Provider:
        name, model, host = "offline", "offline-model", "localhost"

        def complete(self, *args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return LLMResult(text="{}", provider=self.name, model=self.model)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(Gateway(Provider()).chat, "system", f"user-{i}") for i in range(8)]
        for future in futures:
            future.result(timeout=5)
    assert peak == 2
    assert control.request_metrics()["attempts"] == 8
    assert control.request_metrics()["active"] == 0


def test_slot_limit_is_cross_process_and_crash_releases_it(request_env):
    context = multiprocessing.get_context("spawn")
    entered = context.Queue()
    release_path = request_env / "release-test-processes"
    processes = [context.Process(target=_hold_process_slot, args=(str(request_env), entered, str(release_path)))
                 for _ in range(3)]
    try:
        for process in processes:
            process.start()
        holders = {entered.get(timeout=15), entered.get(timeout=15)}
        with pytest.raises(queue.Empty):
            entered.get(timeout=0.2)
        victim = next(p for p in processes if p.pid in holders)
        victim.terminate()
        victim.join(timeout=5)
        assert entered.get(timeout=10) not in holders
    finally:
        release_path.touch()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def test_daily_budget_is_atomic_across_threads(request_env, monkeypatch):
    monkeypatch.setenv("BID_EVAL_LLM_DAILY_BUDGET", "3")
    results = []

    def call():
        try:
            with control.http_attempt(provider="offline", model="offline"):
                return True
        except LLMBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: call(), range(8)))
    assert sum(results) == 3
    assert _budget_calls() == 3
    assert control.request_metrics()["attempts"] == 3


def test_ocr_shares_slots_without_consuming_llm_budget(request_env, monkeypatch):
    monkeypatch.setenv("BID_EVAL_LLM_DAILY_BUDGET", "1")
    with control.http_attempt(provider="ocr", model="ocr", budget=False):
        pass
    assert _budget_calls() == 0
    assert control.request_metrics()["attempts"] == 1


def test_waiting_cancellation_never_reserves_budget(request_env, monkeypatch):
    monkeypatch.setenv("BID_EVAL_LLM_CONCURRENCY", "1")
    monkeypatch.setenv("BID_EVAL_LLM_DAILY_BUDGET", "2")
    cancelled, waiting = threading.Event(), threading.Event()

    def cancel_check():
        waiting.set()
        if cancelled.is_set():
            raise JobCancelled()

    def blocked_call():
        with control.request_scope(cancel_check=cancel_check):
            with control.http_attempt(provider="offline", model="offline"):
                pytest.fail("a canceled queued request cannot be dispatched")

    with control.http_attempt(provider="offline", model="offline"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(blocked_call)
            assert waiting.wait(2)
            cancelled.set()
            with pytest.raises(JobCancelled):
                blocked.result(timeout=3)
        assert _budget_calls() == 1


def test_retry_releases_slot_and_counts_every_http_attempt(request_env, monkeypatch):
    from app.llm import openai_compat

    monkeypatch.setenv("BID_EVAL_LLM_DAILY_BUDGET", "10")
    calls, sleeps = [], []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) < 4:
            raise RuntimeError("503 busy")
        return _response()

    def backoff(seconds):
        assert control.request_metrics()["active"] == 0
        sleeps.append(seconds)

    monkeypatch.setattr(openai_compat, "cooperative_sleep", backoff)
    result = _provider(monkeypatch, create).complete("system", "user")
    assert result.text == '{"answer":"ok"}'
    assert len(calls) == _budget_calls() == 4
    assert len(sleeps) == 3
    assert control.request_metrics()["failed"] == 3


def test_budget_error_is_not_wrapped_or_retried(request_env, monkeypatch):
    from app.llm import openai_compat

    monkeypatch.setenv("BID_EVAL_LLM_DAILY_BUDGET", "1")
    monkeypatch.setattr(openai_compat, "cooperative_sleep", lambda _: None)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("503 busy")

    with pytest.raises(LLMBudgetExceeded):
        _provider(monkeypatch, create).complete("system", "user")
    assert len(calls) == _budget_calls() == 1


def test_control_and_cancel_errors_propagate_unchanged(request_env, monkeypatch):
    from app.llm import openai_compat

    error = LLMNotConfigured("missing request config")

    def unavailable():
        raise error

    monkeypatch.setattr(control, "_reserve_budget", unavailable)
    provider = _provider(monkeypatch, lambda **kwargs: pytest.fail("no SDK call"))
    with pytest.raises(LLMNotConfigured) as caught:
        provider.complete("system", "user")
    assert caught.value is error
    with control.request_scope(cancel_check=lambda: (_ for _ in ()).throw(JobCancelled())):
        with pytest.raises(JobCancelled):
            provider.complete("system", "user")


def test_json_repair_counts_new_generation_once_and_cache_hit_is_free(request_env, monkeypatch):
    monkeypatch.setenv("BID_EVAL_LLM_DAILY_BUDGET", "2")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return _response("bad json" if len(calls) == 1 else '{"answer":"ok"}')

    class Answer(BaseModel):
        answer: str

    gateway = Gateway(_provider(monkeypatch, create))
    result, _ = gateway.chat_json("system", "user", Answer)
    cached, metadata = gateway.chat_json("system", "user", Answer)
    assert result == cached and metadata.cached
    assert len(calls) == _budget_calls() == 2


def test_retry_after_is_respected_with_a_bounded_delay():
    from app.llm.openai_compat import _retry_delay

    error = RuntimeError("429")
    error.response = types.SimpleNamespace(headers={"Retry-After": "7"})
    assert _retry_delay(error, 1) == 7
    error.response.headers["Retry-After"] = "6000"
    assert _retry_delay(error, 1) == 60


def test_nested_metric_observers_both_receive_cache_hits_without_material():
    outer, inner = [], []
    with control.request_scope(metadata={"run_id": 85, "prompt": "must-not-be-logged"}, on_event=outer.append):
        with control.request_scope(metadata={"batch_index": 3}, on_event=inner.append):
            control.emit_cache_hit("memory")
        control.emit_cache_hit("disk")
    assert [event["cache_kind"] for event in outer] == ["memory", "disk"]
    assert len(inner) == 1 and inner[0]["batch_index"] == 3
    assert all("prompt" not in event for event in outer + inner)
    assert all(event["run_id"] == 85 for event in outer)


def test_failed_metric_observer_does_not_hide_nested_observer():
    events = []

    def broken(event):
        raise RuntimeError("observer unavailable")

    with control.request_scope(on_event=broken):
        with control.request_scope(on_event=events.append):
            control.emit_cache_hit("memory")
    assert events[0]["event"] == "cache_hit"
