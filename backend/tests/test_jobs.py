"""任务队列的健壮性：心跳续期、僵尸重领上限、取消运行的任务作用域。

对应 2026-10-19 修的三处问题：
1. `cancel_run` 曾按 kind+状态取最新任务，多运行项目下会取消到别的运行；
2. 心跳只在 handler 调 advance() 时刷新，单次 LLM 调用可超过 JOB_STALE_S，
   任务会被判僵尸并被第二个 worker 重复领取执行；
3. 僵尸重领没有次数上限，异常任务会被无限重复执行。
"""
from __future__ import annotations

import time
from datetime import timedelta

from app import config
from app.db.models import EvaluationRun, Job, Project, RulePackage, utcnow
from app.db.session import session_scope


def _seed_two_runs() -> tuple[int, int, int, int]:
    """造一个项目 + 两个运行 + 各自一个 running 的 evaluate 任务。"""
    with session_scope() as s:
        s.add(Project(id="PRJ-TEST-CANCEL", name="取消作用域测试", status="evaluating"))
    with session_scope() as s:
        pkg = RulePackage(project_id="PRJ-TEST-CANCEL", version=1, status="frozen")
        s.add(pkg)
        s.flush()
        r1 = EvaluationRun(project_id="PRJ-TEST-CANCEL", package_id=pkg.id,
                           package_version=1, status="running")
        r2 = EvaluationRun(project_id="PRJ-TEST-CANCEL", package_id=pkg.id,
                           package_version=1, status="running")
        s.add_all([r1, r2])
        s.flush()
        j1 = Job(kind="evaluate", project_id="PRJ-TEST-CANCEL", status="running",
                 payload_json={"run_id": r1.id})
        j2 = Job(kind="evaluate", project_id="PRJ-TEST-CANCEL", status="running",
                 payload_json={"run_id": r2.id})
        s.add_all([j1, j2])
        s.flush()
        return r1.id, r2.id, j1.id, j2.id


def test_cancel_run_only_cancels_its_own_job(app_env, client):
    """取消运行 A 不得动到运行 B 的任务（此前会取消到最新的那个）。"""
    run_a, run_b, job_a, job_b = _seed_two_runs()

    resp = client.post(f"/api/runs/{run_a}/cancel")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    with session_scope() as s:
        assert s.get(Job, job_a).cancel_requested is True
        assert s.get(Job, job_b).cancel_requested is False


def test_cancel_run_without_live_job_is_noop(app_env, client):
    """运行已无存活任务时返回 ok=False，不误伤其它任务。"""
    run_a, _run_b, job_a, job_b = _seed_two_runs()
    with session_scope() as s:
        s.get(Job, job_a).status = "succeeded"

    resp = client.post(f"/api/runs/{run_a}/cancel")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False

    with session_scope() as s:
        assert s.get(Job, job_b).cancel_requested is False


def test_job_context_heartbeat_keeps_job_alive(app_env, monkeypatch):
    """handler 长时间不调 advance() 时，心跳线程仍持续续期。"""
    from app.jobs.queue import JobContext

    monkeypatch.setattr(config, "JOB_HEARTBEAT_S", 1)
    stale = utcnow() - timedelta(hours=1)
    with session_scope() as s:
        job = Job(kind="evaluate", status="running", heartbeat_at=stale, payload_json={})
        s.add(job)
        s.flush()
        job_id = job.id

    ctx = JobContext(job_id)
    ctx.start_heartbeat()
    try:
        time.sleep(2.5)
        with session_scope() as s:
            hb = s.get(Job, job_id).heartbeat_at
        assert hb > stale, "心跳线程未续期"
        assert hb > utcnow() - timedelta(seconds=3), "心跳未保持新鲜"
    finally:
        ctx.stop_heartbeat()

    # 停止后不再写库
    with session_scope() as s:
        after_stop = s.get(Job, job_id).heartbeat_at
    time.sleep(1.5)
    with session_scope() as s:
        assert s.get(Job, job_id).heartbeat_at == after_stop


def test_stale_job_gives_up_after_max_attempts(app_env):
    """僵尸任务重领次数用尽 → 判 failed，不再被领取执行。"""
    from app.jobs import queue

    with session_scope() as s:
        job = Job(
            kind="evaluate",
            status="running",
            heartbeat_at=utcnow() - timedelta(seconds=config.JOB_STALE_S + 60),
            attempts=config.JOB_MAX_ATTEMPTS,
            payload_json={},
        )
        s.add(job)
        s.flush()
        job_id = job.id

    assert queue._claim_next_job() is None

    with session_scope() as s:
        row = s.get(Job, job_id)
        assert row.status == "failed"
        assert "上限" in (row.error or "")


def test_stale_job_reclaimed_below_max_attempts(app_env):
    """未超上限的僵尸任务仍可被重新领取（保留崩溃恢复能力）。"""
    from app.jobs import queue

    with session_scope() as s:
        job = Job(
            kind="evaluate",
            status="running",
            heartbeat_at=utcnow() - timedelta(seconds=config.JOB_STALE_S + 60),
            attempts=1,
            payload_json={},
        )
        s.add(job)
        s.flush()
        job_id = job.id

    assert queue._claim_next_job() == job_id
    with session_scope() as s:
        row = s.get(Job, job_id)
        assert row.status == "running"
        assert row.attempts == 2
