"""Nonblocking OS locks prevent duplicate execution across worker processes.

Lock files are stable runtime data, never ownership markers. They are never
unlinked: deleting a locked file would let another worker lock a different inode
for the same run. Closing the non-inheritable descriptor releases the OS lock,
including when its process crashes or is killed.
"""
from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager

from app import config


class RunExecutionBusy(RuntimeError):
    """Another worker already owns this run; its job/state must be left alone."""


def _acquire(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        # Windows byte-range locks may extend beyond EOF, so the stable lock
        # file needs no initialization, truncation or writes before acquisition.
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def exclusive_run(run_id: int) -> Iterator[None]:
    """Own one run until this context exits, or raise RunExecutionBusy at once.

    All worker processes sharing the evaluation database must use the same data
    directory. Unrelated filesystem/configuration errors remain ordinary errors,
    rather than being misreported as an already running evaluation.
    """
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise ValueError("run_id must be a positive integer")
    directory = config.data_dir() / "run_locks"
    directory.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory / f"{run_id}.lock", flags, 0o600)
    try:
        os.set_inheritable(descriptor, False)
        try:
            _acquire(descriptor)
        except OSError as exc:
            busy_codes = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}
            if os.name == "nt":
                busy_codes.add(errno.EDEADLK)
            if exc.errno in busy_codes:
                raise RunExecutionBusy(f"评标运行 #{run_id} 已由其他执行进程持有") from exc
            raise
        yield
    finally:
        # Releasing by close avoids both stale-file deletion races and a second
        # unlock error hiding the exception raised by the evaluation itself.
        os.close(descriptor)
