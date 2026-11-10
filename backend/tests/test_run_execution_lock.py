"""Real child processes prove run ownership survives files but not dead workers."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from app.services.run_execution_lock import RunExecutionBusy, exclusive_run


BACKEND_ROOT = Path(__file__).resolve().parents[1]
CHILD = """
import sys
from app.services.run_execution_lock import RunExecutionBusy, exclusive_run
try:
    with exclusive_run(int(sys.argv[1])):
        print('acquired', flush=True)
        if sys.argv[2] == 'hold':
            import os
            from pathlib import Path
            Path(sys.argv[3]).write_text(str(os.getpid()), encoding='ascii')
            sys.stdin.readline()
        elif sys.argv[2] == 'exception':
            raise RuntimeError('intentional test exception')
        elif sys.argv[2] == 'crash':
            import os
            os._exit(27)
    print('released', flush=True)
except RunExecutionBusy:
    print('busy', flush=True)
    sys.exit(23)
"""


@pytest.fixture
def lock_env(monkeypatch, tmp_path):
    # Keep runtime files from parent and children under the user's work root.
    # The POSIX branch also lets the same OS-lock tests run on Linux CI.
    base = Path(r"E:\codex work") / "rpf-run-lock-tests" if os.name == "nt" else tmp_path
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="case-", dir=base) as directory:
        data_root = Path(directory)
        monkeypatch.setenv("BID_EVAL_DATA_DIR", str(data_root))
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = str(BACKEND_ROOT)
        child_env["PYTHONPYCACHEPREFIX"] = str(data_root / "pycache")
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUNBUFFERED"] = "1"
        for name in ("BID_EVAL_LLM_PROVIDER", "BID_EVAL_LLM_API_KEY", "BID_EVAL_VISION_API_KEY",
                     "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
            child_env[name] = ""
        yield data_root, child_env


def _child(env: dict, run_id=11, mode="once") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", CHILD, str(run_id), mode],
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)


def _holding_child(env: dict, run_id=11):
    ready = Path(env["BID_EVAL_DATA_DIR"]) / f"ready-{uuid.uuid4().hex}"
    process = subprocess.Popen([sys.executable, "-c", CHILD, str(run_id), "hold", str(ready)],
                               env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready.exists():
            try:
                worker_pid = int(ready.read_text(encoding="ascii"))
            except ValueError:
                pass  # A reader may observe the file between open and write.
            else:
                return process, worker_pid
        if process.poll() is not None:
            output, error = process.communicate(timeout=15)
            pytest.fail(f"Lock holder exited before readiness: {output} {error}")
        time.sleep(0.01)
    process.kill()
    output, error = process.communicate(timeout=15)
    pytest.fail(f"Lock holder never became ready: {output} {error}")


def _terminate_holder(process, worker_pid):
    try:
        os.kill(worker_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=15)


def test_independent_process_cannot_acquire_an_owned_run_but_can_acquire_another(lock_env):
    root, env = lock_env
    with exclusive_run(11):
        contender = _child(env)
        assert contender.returncode == 23
        assert contender.stdout.strip() == "busy"
        other = _child(env, run_id=12)
        assert other.returncode == 0, other.stderr
        assert other.stdout.splitlines() == ["acquired", "released"]
    assert (root / "run_locks" / "11.lock").exists()
    released = _child(env)
    assert released.returncode == 0, released.stderr


def test_same_process_separate_context_cannot_reenter_owned_run(lock_env):
    with exclusive_run(11):
        with pytest.raises(RunExecutionBusy):
            with exclusive_run(11):
                pytest.fail("second lease must not enter")


def test_existing_lock_file_is_never_truncated_replaced_or_removed(lock_env):
    root, env = lock_env
    directory = root / "run_locks"
    directory.mkdir()
    path = directory / "11.lock"
    path.write_bytes(b"persistent previous-owner data")
    identity = path.stat().st_ino
    for _ in range(2):
        with exclusive_run(11):
            assert path.stat().st_ino == identity
        assert path.read_bytes() == b"persistent previous-owner data"
        successor = _child(env)
        assert successor.returncode == 0, successor.stderr
    assert path.read_bytes() == b"persistent previous-owner data"
    assert path.stat().st_ino == identity


def test_normal_child_exit_releases_lock_without_deleting_stable_file(lock_env):
    root, env = lock_env
    holder, worker_pid = _holding_child(env)
    try:
        assert holder.stdout.readline().strip() == "acquired"
        with pytest.raises(RunExecutionBusy):
            with exclusive_run(11):
                pytest.fail("child still owns the run")
        output, error = holder.communicate(input="release\n", timeout=15)
        assert holder.returncode == 0, error
        assert output.strip() == "released"
        lock_path = root / "run_locks" / "11.lock"
        assert lock_path.exists()
        with exclusive_run(11):
            assert lock_path.exists()
    finally:
        if holder.poll() is None:
            # Windows venv python.exe may be a launcher with a different PID.
            _terminate_holder(holder, worker_pid)


def test_exception_in_parent_releases_lock_and_preserves_exception(lock_env):
    _, env = lock_env
    with pytest.raises(RuntimeError, match="original evaluation failure"):
        with exclusive_run(11):
            raise RuntimeError("original evaluation failure")
    released = _child(env)
    assert released.returncode == 0, released.stderr


@pytest.mark.parametrize("mode, code", [("exception", 1), ("crash", 27)])
def test_exception_and_abrupt_child_exit_release_os_lock(lock_env, mode, code):
    root, env = lock_env
    holder = _child(env, mode=mode)
    assert holder.returncode == code
    assert holder.stdout.strip() == "acquired"
    assert (root / "run_locks" / "11.lock").exists()
    with exclusive_run(11):
        contender = _child(env)
        assert contender.returncode == 23


def test_killed_process_releases_os_lock_for_existing_file(lock_env):
    root, env = lock_env
    holder, worker_pid = _holding_child(env)
    try:
        assert holder.stdout.readline().strip() == "acquired"
        blocked = _child(env)
        assert blocked.returncode == 23
        os.kill(worker_pid, signal.SIGTERM)
        holder.communicate(timeout=15)
        assert holder.returncode != 0
        assert (root / "run_locks" / "11.lock").exists()
        successor = _child(env)
        assert successor.returncode == 0, successor.stderr
    finally:
        if holder.poll() is None:
            _terminate_holder(holder, worker_pid)


@pytest.mark.parametrize("run_id", [0, -1, True, "11", 1.5])
def test_invalid_run_id_never_creates_a_lock_path(lock_env, run_id):
    root, _ = lock_env
    with pytest.raises(ValueError, match="positive integer"):
        with exclusive_run(run_id):
            pytest.fail("invalid run must never enter")
    assert not (root / "run_locks").exists()


def test_filesystem_failure_is_not_misreported_as_run_busy(lock_env):
    root, _ = lock_env
    (root / "run_locks").write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError) as error:
        with exclusive_run(11):
            pytest.fail("invalid storage must never enter")
    assert not isinstance(error.value, RunExecutionBusy)
