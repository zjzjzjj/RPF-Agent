"""Batch concurrency preserves context, ordered evidence and coordinator writes."""
from __future__ import annotations

import threading
import time
from contextvars import ContextVar

import pytest

from app.llm.batch_scheduler import BatchScheduler, reset_batch_schedulers, run_batches


@pytest.fixture(autouse=True)
def scheduler_env(monkeypatch):
    monkeypatch.setenv("BID_EVAL_LLM_CONCURRENCY", "2")
    yield
    reset_batch_schedulers()


def test_results_keep_input_order_and_callbacks_run_on_coordinator():
    marker = ContextVar("test_batch_marker", default="missing")
    token = marker.set("bound-run")
    first_may_finish = threading.Event()
    thread = threading.get_ident()
    checkpoints = []

    def worker(index):
        assert marker.get() == "bound-run"
        if index == 0:
            assert first_may_finish.wait(5)
        return index * 10

    def completed(index, result):
        assert threading.get_ident() == thread
        checkpoints.append((index, result))
        if index == 1:
            first_may_finish.set()

    try:
        assert run_batches([0, 1], worker, on_completed=completed) == [0, 10]
    finally:
        marker.reset(token)
    assert checkpoints == [(1, 10), (0, 0)]


def test_ready_groups_rotate_and_queue_is_bounded():
    scheduler = BatchScheduler(1, max_queued=3)
    release = threading.Event()
    started = threading.Event()
    calls = []

    def blocker():
        started.set()
        assert release.wait(5)

    try:
        initial = scheduler.submit("blocking", blocker)
        assert started.wait(2)
        a1 = scheduler.submit("a", lambda: calls.append("a1"))
        a2 = scheduler.submit("a", lambda: calls.append("a2"))
        b1 = scheduler.submit("b", lambda: calls.append("b1"))
        assert scheduler.submit("c", lambda: None) is None
        release.set()
        for future in (initial, a1, a2, b1):
            future.result(timeout=5)
        assert calls == ["a1", "b1", "a2"]
    finally:
        release.set()
        scheduler.shutdown()


def test_failure_keeps_inflight_success_and_stops_queued_work():
    second_started = threading.Event()
    failed = threading.Event()
    calls, completed = [], []

    def worker(index):
        calls.append(index)
        if index == 0:
            assert second_started.wait(5)
            failed.set()
            raise RuntimeError("batch failed")
        second_started.set()
        assert failed.wait(5)
        time.sleep(0.05)
        return "verified"

    with pytest.raises(RuntimeError, match="batch failed"):
        run_batches([0, 1, 2], worker, on_completed=lambda i, r: completed.append((i, r)))
    assert calls == [0, 1]
    assert completed == [(1, "verified")]


def test_batch_cancel_check_prevents_any_dispatch():
    class Canceled(Exception):
        pass

    def cancel():
        raise Canceled()

    with pytest.raises(Canceled):
        run_batches([1, 2], lambda _: pytest.fail("canceled"), cancel_check=cancel)


def test_nested_batch_submission_fails_instead_of_deadlocking():
    with pytest.raises(RuntimeError, match="nested batch scheduling"):
        run_batches([0, 1], lambda item: run_batches([item], lambda value: value))
