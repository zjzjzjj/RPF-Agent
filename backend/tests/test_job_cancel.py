"""任务取消语义：queued 任务取消立即生效；被取消任务对应的运行标记为 cancelled。

2026-10-20 实证的缺陷：request_cancel 对 queued 只置标志 → 排队任务永远停在
queued（前面压着长任务根本领不到），用户点取消拿到 200 却毫无效果。
"""
from __future__ import annotations

import threading

from sqlalchemy import select

from app.db.models import EvaluationRun, Job
from app.db.session import session_scope
from app.jobs import queue
from app.jobs.handlers import register

_release = threading.Event()


@register("test_slow_block")
def _slow_block(ctx, payload) -> dict:  # noqa: ANN001
    """占住单 worker，制造「后面有任务在排队」的现场。"""
    _release.wait(timeout=20)
    return {"ok": True}


def _wait_status(job_id: int, status: str, timeout: float = 10.0) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = queue.get_job(job_id)
        if job is not None and job.status == status:
            return
        time.sleep(0.05)
    raise TimeoutError(f"任务 {job_id} 未在 {timeout}s 内变为 {status}")


def test_request_cancel_queued_is_immediate(client, app_env) -> None:  # noqa: ANN001
    """worker 被长任务占住时，取消排队中的任务必须立即置 cancelled（而非只置标志）。"""
    _release.clear()
    try:
        first = queue.submit("test_slow_block", {}, project_id=None)
        _wait_status(first, "running")  # worker 已被占住

        second = queue.submit("test_slow_block", {}, project_id=None)
        assert queue.get_job(second).status == "queued"

        assert queue.request_cancel(second) is True
        job = queue.get_job(second)
        assert job.status == "cancelled", f"排队任务应立即取消，实际 {job.status}"
        assert job.finished_at is not None
        # 再取消已终态的任务 → False
        assert queue.request_cancel(second) is False
    finally:
        _release.set()
        _wait_status(first, "succeeded", timeout=25)


def test_run_job_precheck_cancelled(client, app_env) -> None:  # noqa: ANN001
    """领取时已带取消标志的任务直接终结，handler 不应被执行。"""
    with session_scope() as s:
        job = Job(kind="test_slow_block", payload_json={}, status="queued", cancel_requested=True)
        s.add(job)
        s.flush()
        job_id = job.id

    from app.jobs.queue import _run_job

    _run_job(job_id)  # 预检命中，直接 cancelled；handler 若被执行会阻塞到超时
    job = queue.get_job(job_id)
    assert job.status == "cancelled"


def _mk_run(client, pid: str) -> int:
    """造一个真实规则包 + 非终态运行（reconcile 的最小组件）。"""
    pkg = client.post(f"/api/projects/{pid}/rules/instantiate",
                      json={"template_id": "comprehensive-goods"}).json()
    with session_scope() as s:
        run = EvaluationRun(
            project_id=pid, package_id=pkg["package_id"], package_version=1,
            status="queued", stages_json=["objective"],
        )
        s.add(run)
        s.flush()
        return run.id


def test_reconcile_marks_cancelled_job_run(client, app_env) -> None:  # noqa: ANN001
    """任务被取消后，对应运行由 reconcile 置 cancelled（而非 failed 吓用户重新发起）。"""
    pid = client.post("/api/projects", json={"name": "取消语义测试"}).json()["id"]
    run_id = _mk_run(client, pid)
    with session_scope() as s:
        s.add(
            Job(
                kind="evaluate", payload_json={"run_id": run_id},
                project_id=pid, status="cancelled", finished_at=None,
            )
        )

    from app.services.scoring_service import reconcile_stale_runs

    assert reconcile_stale_runs(pid) == 1
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        assert run.status == "cancelled"
        assert run.config_json["error"] == "用户取消"


def test_reconcile_orphan_run_failed(client, app_env) -> None:  # noqa: ANN001
    """无任何对应任务的运行（进程被杀等）仍标记为 failed，语义不混。"""
    pid = client.post("/api/projects", json={"name": "孤儿运行测试"}).json()["id"]
    run_id = _mk_run(client, pid)
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        run.status = "running"

    from app.services.scoring_service import reconcile_stale_runs

    assert reconcile_stale_runs(pid) == 1
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        assert run.status == "failed"


def test_queue_position(client, app_env) -> None:  # noqa: ANN001
    """排队位置：running 的算 1 个，queued 按先后计数；终态为 0。"""
    _release.clear()
    try:
        first = queue.submit("test_slow_block", {}, project_id=None)
        _wait_status(first, "running")
        assert queue.queue_position(first) == 0  # running 中

        second = queue.submit("test_slow_block", {}, project_id=None)
        third = queue.submit("test_slow_block", {}, project_id=None)
        assert queue.queue_position(second) == 1  # 前面 1 个（running 的 first）
        assert queue.queue_position(third) == 2

        queue.request_cancel(second)
        assert queue.queue_position(second) == 0  # 已 cancelled
        assert queue.queue_position(third) == 1  # first 仍占着
    finally:
        _release.set()
        _wait_status(first, "succeeded", timeout=25)
