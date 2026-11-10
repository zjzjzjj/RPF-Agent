"""Bound actual HTTP attempts across gateways and processes on this host.

Slot files are stable OS locks in the shared data directory. They are never
removed and no transaction is held while waiting, calling a model or backing off.
Only actual dispatched attempts consume the optional persistent daily budget.
"""
from __future__ import annotations

import errno
import logging
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from app import config
from app.errors import LLMBudgetExceeded

log = logging.getLogger("bid_eval.llm.attempt")
_CANCEL: ContextVar[Callable[[], None] | None] = ContextVar("llm_attempt_cancel", default=None)
_EVENT: ContextVar[Callable[[dict], None] | None] = ContextVar("llm_attempt_event", default=None)
_METADATA: ContextVar[dict | None] = ContextVar("llm_attempt_metadata", default=None)
_METRIC_LOCK = threading.Lock()
_METRICS = {"attempts": 0, "completed": 0, "failed": 0, "active": 0, "peak_active": 0,
            "queue_ms": 0, "service_ms": 0, "pdf_tasks": 0, "pdf_failed": 0,
            "pdf_worker_ms": 0, "pdf_queue_wait_ms": 0, "pdf_prepare_ms": 0,
            "ocr_ms": 0, "ocr_local_ms": 0, "ocr_network_ms": 0, "render_ms": 0}
_RECENT: deque[dict] = deque(maxlen=256)
# Legacy read-only diagnostic alias; never used to enforce the budget.
_BUDGET_STATE: dict[str, object] = {"day": "", "calls": 0}
_SAFE_METADATA = {"run_id", "job_id", "bidder_id", "criterion_id", "batch_index", "phase"}


@contextmanager
def request_scope(*, cancel_check=None, metadata: dict | None = None, on_event=None):
    """Bind cancellation and metadata; batch workers propagate this context."""
    tokens = []
    if cancel_check is not None:
        tokens.append((_CANCEL, _CANCEL.set(cancel_check)))
    if on_event is not None:
        parent = _EVENT.get()
        def observer(event):
            for callback in (parent, on_event):
                if callback is not None:
                    try:
                        callback(event)
                    except Exception as exc:
                        log.warning("request metric observer failed: %s", type(exc).__name__)
        observer._run_activity_observer = bool(getattr(parent, "_run_activity_observer", False)
                                               or getattr(on_event, "_run_activity_observer", False))
        tokens.append((_EVENT, _EVENT.set(observer)))
    if metadata is not None:
        safe = {**(_METADATA.get() or {}), **{k: v for k, v in metadata.items() if k in _SAFE_METADATA}}
        tokens.append((_METADATA, _METADATA.set(safe)))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def check_cancelled() -> None:
    callback = _CANCEL.get()
    if callback is not None:
        callback()


def run_activity_metrics_active() -> bool:
    return bool(getattr(_EVENT.get(), "_run_activity_observer", False))


def cooperative_sleep(seconds: float) -> None:
    """Cancelable backoff outside the HTTP slot, also easy to replace in tests."""
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        check_cancelled()
        step = min(remaining, 0.1)
        time.sleep(step)
        remaining -= step
    check_cancelled()


def _lock_slot(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def _slot() -> Iterator[None]:
    limit = min(8, max(1, config.llm_max_concurrency()))
    directory = config.data_dir() / "llm_request_slots"
    directory.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    busy = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}
    acquired = None
    start = threading.get_ident() % limit
    try:
        while acquired is None:
            check_cancelled()
            for offset in range(limit):
                descriptor = os.open(directory / f"slot-{(start + offset) % limit}.lock", flags, 0o600)
                os.set_inheritable(descriptor, False)
                try:
                    _lock_slot(descriptor)
                except OSError as exc:
                    os.close(descriptor)
                    if exc.errno not in busy:
                        raise
                else:
                    acquired = descriptor
                    break
            if acquired is None:
                cooperative_sleep(0.05)
        check_cancelled()
        yield
    finally:
        if acquired is not None:
            os.close(acquired)


def _reserve_budget() -> None:
    limit = config.llm_daily_call_budget()
    if limit <= 0:
        return
    from app.db.session import get_engine

    day = datetime.now(timezone.utc).date().isoformat()
    # The schema is installed by the explicit execution-v2 migration (or
    # init_db in development/tests), never as a side effect of a paid request.
    with get_engine().begin() as connection:
        connection.execute(text(
            "INSERT INTO llm_request_daily_budget (day_utc,calls) VALUES (:day,0) "
            "ON CONFLICT(day_utc) DO NOTHING"), {"day": day})
        updated = connection.execute(text(
            "UPDATE llm_request_daily_budget SET calls=calls+1 "
            "WHERE day_utc=:day AND calls<:limit"), {"day": day, "limit": limit})
        if updated.rowcount != 1:
            raise LLMBudgetExceeded(f"今日模型HTTP调用已达上限 {limit} 次")
        calls = connection.execute(text(
            "SELECT calls FROM llm_request_daily_budget WHERE day_utc=:day"), {"day": day}).scalar_one()
    with _METRIC_LOCK:
        _BUDGET_STATE.update(day=day, calls=calls)


def _emit(event: dict) -> None:
    event = {**(_METADATA.get() or {}), "event_id": uuid.uuid4().hex, **event}
    with _METRIC_LOCK:
        _RECENT.append(dict(event))
    callback = _EVENT.get()
    if callback is not None:
        try:
            callback(event)
        except Exception as exc:  # Metrics must not change a valid review.
            log.warning("request metric observer failed: %s", type(exc).__name__)


def emit_cache_hit(kind: str) -> None:
    """Record a logical response reuse without consuming an HTTP slot."""
    if kind not in {"memory", "disk"}:
        raise ValueError("unknown LLM cache kind")
    _emit({"event": "cache_hit", "cache_kind": kind})


def emit_pdf_task_metrics(operation: str, timing: dict, *, error_type: str | None = None) -> None:
    """Bridge worker-only durations into the caller's run/batch observers.

    OCR totals combine local recognition and remote HTTP service; request_ms
    also includes remote OCR, so these are overlapping views, not additive.
    PDF queue/IPC time stays separate from worker computation throughout.
    """
    if operation not in {"probe", "render", "ocr_images", "layout", "rapid_text", "rapid_lines"}:
        return

    def duration(name):
        # A malformed diagnostic must never turn successful PDF work into a
        # review failure; worker envelopes are not part of the evidence value.
        try:
            return max(0, int(timing.get(name, 0)))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return 0

    durations = {name: duration(name) for name in (
        "pdf_worker_ms", "pdf_queue_wait_ms", "pdf_prepare_ms", "render_ms", "ocr_local_ms")}
    with _METRIC_LOCK:
        _METRICS["pdf_tasks"] += 1
        _METRICS["pdf_failed"] += int(error_type is not None)
        for name, value in durations.items():
            _METRICS[name] += value
        _METRICS["ocr_ms"] += durations["ocr_local_ms"]
    _emit({"event": "pdf_task_finished", "operation": operation,
           "error_type": error_type, **durations})


@contextmanager
def _queued_slot(base):
    ticket = {"started": False}
    _emit({**base, "event": "queued"})
    try:
        with _slot():
            yield ticket
    finally:
        if not ticket["started"]:
            _emit({**base, "event": "queue_cancelled"})


@dataclass
class Attempt:
    tokens_in: int | None = None
    tokens_out: int | None = None


@contextmanager
def http_attempt(*, provider: str = "unknown", model: str = "unknown", images: int = 0,
                 budget: bool = True):
    """One SDK HTTP attempt; configuration/budget/cancel errors propagate intact."""
    queued_at = time.monotonic()
    base = {"provider": str(provider), "model": str(model), "images": int(images),
            "request_id": uuid.uuid4().hex}
    with _queued_slot(base) as ticket:
        check_cancelled()
        if budget:
            _reserve_budget()
        # There is intentionally no cancellation checkpoint after reservation:
        # the caller immediately dispatches this attempt, so it is counted once.
        started = time.monotonic()
        queue_ms = int((started - queued_at) * 1000)
        with _METRIC_LOCK:
            _METRICS["attempts"] += 1
            _METRICS["active"] += 1
            _METRICS["peak_active"] = max(_METRICS["peak_active"], _METRICS["active"])
            _METRICS["queue_ms"] += queue_ms
        ticket["started"] = True
        _emit({**base, "event": "started", "queue_ms": queue_ms})
        attempt = Attempt()
        error_type = None
        try:
            yield attempt
        except BaseException as exc:
            error_type = type(exc).__name__
            raise
        finally:
            service_ms = int((time.monotonic() - started) * 1000)
            with _METRIC_LOCK:
                _METRICS["active"] -= 1
                _METRICS["failed" if error_type else "completed"] += 1
                _METRICS["service_ms"] += service_ms
                if provider == "remote_paddle":
                    _METRICS["ocr_network_ms"] += service_ms
                    _METRICS["ocr_ms"] += service_ms
            event = {**base, "event": "finished", "service_ms": service_ms,
                     "queue_ms": queue_ms, "error_type": error_type,
                     "tokens_in": attempt.tokens_in, "tokens_out": attempt.tokens_out}
            _emit(event)
            log.info("http attempt provider=%s model=%s queue_ms=%s service_ms=%s error_type=%s",
                     provider, model, queue_ms, service_ms, error_type)


def request_metrics() -> dict:
    with _METRIC_LOCK:
        return {**_METRICS, "recent": list(_RECENT)}


def reset_request_metrics() -> None:
    """Test/diagnostic reset; it never resets persistent budgets or slot locks."""
    with _METRIC_LOCK:
        if _METRICS["active"]:
            raise RuntimeError("cannot reset metrics while requests are active")
        for key in _METRICS:
            _METRICS[key] = 0
        _RECENT.clear()
        _BUDGET_STATE.update(day="", calls=0)
