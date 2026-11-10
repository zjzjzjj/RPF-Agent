"""Per-worker activity channel; only the coordinator writes job progress."""
from contextlib import contextmanager
from contextvars import ContextVar

_reporter = ContextVar("automatic_review_activity", default=None)


@contextmanager
def reporting(callback):
    token = _reporter.set(callback)
    try:
        yield
    finally:
        _reporter.reset(token)


def report(message: str):
    callback = _reporter.get()
    if callback is not None:
        callback(message)
