"""A bounded, fair thread scheduler for independent model collection batches.

The cell coordinator never executes network work in its own pool. ContextVars
are copied per batch and checkpoint callbacks run only on the calling thread.
"""
from __future__ import annotations

import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from typing import Any, Callable

from app import config
from app.llm.request_control import request_scope


@dataclass
class _Task:
    future: Future
    invoke: Callable[[], Any]


class BatchScheduler:
    def __init__(self, max_workers: int, *, max_queued: int | None = None):
        self.max_workers = max(1, int(max_workers))
        self.max_queued = max(self.max_workers, int(max_queued or self.max_workers * 2))
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="review-batch")
        self._condition = threading.Condition(threading.RLock())
        self._groups: dict[object, deque[_Task]] = {}
        self._rotation: deque[object] = deque()
        self._queued = 0
        self._active = 0
        self._closed = False

    def submit(self, group: object, invoke: Callable[[], Any]) -> Future | None:
        """Nonblocking admission lets callers persist completions while full."""
        with self._condition:
            if self._closed:
                raise RuntimeError("batch scheduler is closed")
            if self._queued >= self.max_queued:
                return None
            future = Future()
            if group not in self._groups:
                self._groups[group] = deque()
                self._rotation.append(group)
            self._groups[group].append(_Task(future, invoke))
            self._queued += 1
            self._pump()
            return future

    def _pump(self):
        while self._active < self.max_workers and self._rotation:
            group = self._rotation.popleft()
            tasks = self._groups[group]
            task = tasks.popleft()
            self._queued -= 1
            if tasks:
                self._rotation.append(group)
            else:
                del self._groups[group]
            if not task.future.set_running_or_notify_cancel():
                continue
            self._active += 1
            submitted = self._pool.submit(task.invoke)
            submitted.add_done_callback(lambda done, task=task: self._finish(task, done))
        self._condition.notify_all()

    def _finish(self, task, done):
        try:
            task.future.set_result(done.result())
        except BaseException as exc:
            task.future.set_exception(exc)
        finally:
            with self._condition:
                self._active -= 1
                self._pump()

    def shutdown(self):
        with self._condition:
            self._closed = True
            for tasks in self._groups.values():
                for task in tasks:
                    task.future.cancel()
        self._pool.shutdown(wait=True)


_SCHEDULERS: dict[int, BatchScheduler] = {}
_LOCK = threading.Lock()
_IN_BATCH: ContextVar[bool] = ContextVar("inside_model_batch_worker", default=False)


def _scheduler() -> BatchScheduler:
    workers = min(8, max(1, config.llm_max_concurrency()))
    with _LOCK:
        if workers not in _SCHEDULERS:
            _SCHEDULERS[workers] = BatchScheduler(workers)
        return _SCHEDULERS[workers]


def run_batches(items, worker, *, group_id=None, on_completed=None, cancel_check=None):
    """Return input-ordered results; on_completed(zero_based_index,result).

    Calls are admitted in a bounded window. On failure queued work is canceled,
    in-flight successes are checkpointed, and the original failure is raised.
    """
    if _IN_BATCH.get():
        raise RuntimeError("nested batch scheduling is not allowed; submit only leaf collection calls")
    inputs = list(items)
    if not inputs:
        return []
    scheduler = _scheduler()
    # A private queue per invocation prevents two callers' colliding labels
    # from canceling or starving one another; the label is observational only.
    group = object()
    pending: dict[Future, int] = {}
    results = [None] * len(inputs)
    next_index = 0
    first_error = None
    window = scheduler.max_workers

    while pending or (next_index < len(inputs) and first_error is None):
        if first_error is None and cancel_check is not None:
            try:
                cancel_check()
            except BaseException as exc:
                first_error = exc
        while first_error is None and next_index < len(inputs) and len(pending) < window:
            index = next_index
            context = copy_context()

            def invoke(index=index):
                token = _IN_BATCH.set(True)
                try:
                    with request_scope(cancel_check=cancel_check, metadata={"batch_index": index}):
                        if cancel_check is not None:
                            cancel_check()
                        return worker(inputs[index])
                finally:
                    _IN_BATCH.reset(token)

            submitted = scheduler.submit(group, lambda context=context, invoke=invoke: context.run(invoke))
            if submitted is None:
                break
            pending[submitted] = index
            next_index += 1
        if first_error is not None:
            for future in pending:
                future.cancel()
        if not pending:
            if first_error is None:
                with scheduler._condition:
                    scheduler._condition.wait(0.05)
            continue
        ready, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
        for future in ready:
            index = pending.pop(future)
            if future.cancelled():
                continue
            try:
                result = future.result()
                results[index] = result
                if on_completed is not None:
                    on_completed(index, result)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
    if first_error is not None:
        raise first_error
    return results


def reset_batch_schedulers():
    """Tests/shutdown only; let active work drain before replacing schedulers."""
    with _LOCK:
        existing = list(_SCHEDULERS.values())
        _SCHEDULERS.clear()
    for scheduler in existing:
        scheduler.shutdown()
