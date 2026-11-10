"""轻量任务队列：jobs 表 + 进程内 asyncio worker。

为什么不用 Celery/BackgroundTasks：
- 评分任务本质是"长任务 + 进度 + 可取消 + 可重试 + 崩溃可恢复"；
- 单机 MVP 不需要外部 broker；`JobRunner.submit()` 是将来换任务框架的唯一接缝。

语义：
- 提交：写一行 status=queued。
- 领取：worker 轮询，原子地 queued→running（同时拾取心跳超时的僵尸任务）。
- 进度：handler 通过 JobContext 上报；每步检查 cancel_requested。
- 结果：handler 返回 dict 存 result_json；异常存 error 并置 failed。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import traceback
from contextlib import suppress
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import OperationalError

from app import config
from app.db.models import Job, utcnow
from app.db.session import session_scope
from app.services.run_execution_lock import RunExecutionBusy

log = logging.getLogger("bid_eval.jobs")

_worker_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


class JobCancelled(Exception):
    """handler 在取消检查点抛出，由 runner 捕获并置 cancelled。"""


class JobContext:
    """传给 handler 的上下文：进度上报与取消检查。

    handler 运行在工作线程里，进度更新各自开短事务，不与业务事务纠缠。
    """

    def __init__(self, job_id: int):
        self.job_id = job_id
        self._done = 0
        self._total = 0
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None

    def start(self, total: int, message: str = "") -> None:
        self._total = max(1, total)
        self._done = 0
        self._update(message)

    def advance(self, n: int = 1, message: str | None = None) -> None:
        self._done += n
        self._update(message)
        self.check_cancelled()

    def report(self, message: str) -> None:
        """Report real work within a step without inventing completed items."""
        self._update(message)
        self.check_cancelled()

    def check_cancelled(self) -> None:
        # Progress and cancellation are control-plane writes; a transient
        # SQLite lock must not kill an evaluation run (run 85: an OperationalError
        # escaped from a progress update and failed the whole job).
        for attempt in range(3):
            try:
                with session_scope() as s:
                    job = s.get(Job, self.job_id)
                    if job is not None and job.cancel_requested:
                        raise JobCancelled()
                return
            except JobCancelled:
                raise
            except OperationalError:
                if attempt == 2:
                    log.warning("任务 %s 取消状态检查失败（数据库锁），本轮跳过", self.job_id,
                                exc_info=True)
                    return
                time.sleep(0.2 * (attempt + 1))

    def _update(self, message: str | None) -> None:
        for attempt in range(3):
            try:
                with session_scope() as s:
                    job = s.get(Job, self.job_id)
                    if job is None:
                        return
                    job.done_steps = self._done
                    job.total_steps = self._total
                    if message is not None:
                        job.message = message[:500]
                    job.heartbeat_at = utcnow()
                return
            except OperationalError:
                if attempt == 2:
                    log.warning("任务 %s 进度更新失败（数据库锁），继续执行", self.job_id,
                                exc_info=True)
                    return
                time.sleep(0.2 * (attempt + 1))

    # -- 心跳 ---------------------------------------------------------------
    # 进度更新只在 handler 调 advance() 时刷新 heartbeat_at，而 handler 可能长时间
    # 卡在一次 LLM 调用上（BID_EVAL_LLM_TIMEOUT_S 已接近/超过 JOB_STALE_S）。
    # 此时任务会被判僵尸、被另一个 worker 重复领取执行（同一任务跑两遍）。
    # 用独立守护线程持续续期，与业务步骤解耦。
    def start_heartbeat(self) -> None:
        if self._hb_thread is not None:
            return
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name=f"job-heartbeat-{self.job_id}", daemon=True
        )
        self._hb_thread.start()

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2.0)
            self._hb_thread = None

    def _heartbeat_loop(self) -> None:
        interval = max(1, config.JOB_HEARTBEAT_S)
        while not self._hb_stop.wait(interval):
            try:
                with session_scope() as s:
                    job = s.get(Job, self.job_id)
                    if job is None or job.status != "running":
                        return
                    job.heartbeat_at = utcnow()
            except Exception:  # noqa: BLE001 —— 心跳失败不得影响任务本身
                # 必须 warning 可见：心跳连续写失败会让任务被判僵尸、从头重跑乃至
                # 重试 3 次后放弃（job 44 实证：2h40m 换一个 failed）。静默吞掉 = 事故无从排查。
                log.warning("任务 %s 心跳更新失败", self.job_id, exc_info=True)


# ---------------------------------------------------------------------------
# 提交与查询（同步，供 API/服务层调用）
# ---------------------------------------------------------------------------
def submit(kind: str, payload: dict[str, Any], project_id: str | None = None) -> int:
    """入队一个任务，返回 job id。"""
    with session_scope() as s:
        job = Job(kind=kind, payload_json=payload, project_id=project_id, status="queued")
        s.add(job)
        s.flush()
        return job.id


def request_cancel(job_id: int) -> bool:
    """请求取消任务。

    queued 任务**立即置 cancelled**：它还没开始，等 worker 领取再取消既不必要、
    也永远等不到（前面可能压着几十分钟的长任务）——此前只置标志的后果是
    用户点取消拿到 200，任务却永远停在排队（2026-10-20 实证）。
    running 任务只置标志，由 handler 在下一个 advance/checkpoint 处自行中断。
    """
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None or job.status in ("succeeded", "failed", "cancelled"):
            return False
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.message = "已取消（未开始）"
            job.finished_at = utcnow()
        return True


def get_job(job_id: int) -> Job | None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return None
        s.expunge(job)
        return job


def queue_position(job_id: int) -> int:
    """该任务前面还有几个未完成任务（running 的算 1 个）。0 = 正在跑或已终态。

    供 UI 显示「排队中，前面还有 N 个任务」——单 worker 下这是用户判断
    「任务没反应」到底是排队还是卡死的唯一线索。
    """
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None or job.status != "queued":
            return 0
        ahead = s.execute(
            select(func.count(Job.id)).where(
                Job.status.in_(("queued", "running")),
                Job.id < job_id,
            )
        ).scalar_one()
        return int(ahead or 0)


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------
def _claim_next_job() -> int | None:
    """原子领取下一个任务；顺带拾取心跳超时的僵尸 running 任务。

    领取必须用**条件 UPDATE**（WHERE 带上可领取状态并检查 rowcount），
    SELECT→无条件 UPDATE 的读改写在多 worker 下会把同一任务发给两个人。
    """
    stale_before = utcnow() - timedelta(seconds=config.JOB_STALE_S)
    # 先把"僵尸且重试次数已用尽"的任务判死：否则它会每轮都被重新领取、
    # 无限重复执行（attempts 无上限时的真实风险）。
    with session_scope() as s:
        s.execute(
            update(Job)
            .where(
                Job.status == "running",
                Job.heartbeat_at < stale_before,
                Job.attempts >= config.JOB_MAX_ATTEMPTS,
            )
            .values(
                status="failed",
                message="执行失败：任务心跳超时",
                error=f"任务心跳超时且重试次数已达上限（{config.JOB_MAX_ATTEMPTS} 次），已放弃",
                finished_at=utcnow(),
            )
        )
    reclaimable = (
        (Job.status == "running")
        & (Job.heartbeat_at < stale_before)
        & (Job.attempts < config.JOB_MAX_ATTEMPTS)
    )
    for _ in range(10):  # 竞争失败重试若干次后让位
        with session_scope() as s:
            candidate = s.execute(
                select(Job.id)
                .where(or_(Job.status == "queued", reclaimable))
                .order_by(Job.id)
                .limit(1)
            ).scalar_one_or_none()
        if candidate is None:
            return None
        with session_scope() as s:
            result = s.execute(
                update(Job)
                .where(
                    Job.id == candidate,
                    or_(Job.status == "queued", reclaimable),
                )
                .values(
                    status="running",
                    started_at=utcnow(),
                    heartbeat_at=utcnow(),
                    attempts=Job.attempts + 1,
                )
            )
        if result.rowcount == 1:
            return candidate
    return None


def _run_job(job_id: int) -> None:
    from app.jobs.handlers import get_handler

    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        kind = job.kind
        payload = dict(job.payload_json or {})
        # 领取前已请求取消的（含竞态：排队期间取消标志残留）直接终结，不跑 handler
        if job.cancel_requested:
            job.status = "cancelled"
            job.message = "已取消"
            job.finished_at = utcnow()
            return

    ctx = JobContext(job_id)
    ctx.start_heartbeat()
    try:
        handler = get_handler(kind)
        result = handler(ctx, payload) or {}
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None or job.cancel_requested:
                if job is not None:
                    job.status = "cancelled"
                    job.message = "已取消"
                    job.finished_at = utcnow()
                return
            job.status = "succeeded"
            job.result_json = result
            job.message = "完成"
            job.finished_at = utcnow()
    except RunExecutionBusy:
        # Another process still owns this run. Leave the shared job receipt
        # untouched: its original worker continues heartbeat/progress and will
        # persist the result. If that worker dies, normal stale recovery retries.
        log.info("任务 %s 已有运行中的评标进程，本次未重复执行", job_id)
    except JobCancelled:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is not None:
                job.status = "cancelled"
                job.message = "已取消"
                job.finished_at = utcnow()
    except Exception as exc:  # noqa: BLE001 —— 任务边界，必须兜住一切并落库
        log.exception("任务 %s（%s）失败", job_id, kind)
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is not None:
                job.status = "failed"
                job.message = "执行失败"
                job.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-4000:]}"
                job.finished_at = utcnow()
    finally:
        ctx.stop_heartbeat()


async def _worker_loop() -> None:
    """worker 主循环。

    循环体必须兜住一切异常：这里崩了任务系统就永久停摆且无人知晓。
    瞬时错误（如 SQLite busy）记日志后稍候重试即可。
    """
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            job_id = await asyncio.to_thread(_claim_next_job)
        except Exception:  # noqa: BLE001 —— 领取失败不能杀死 worker
            log.exception("任务领取失败，1 秒后重试")
            job_id = None
        if job_id is None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_stop_event.wait(), timeout=1.0)
            continue
        try:
            await asyncio.to_thread(_run_job, job_id)
        except Exception:  # noqa: BLE001 —— 同上；_run_job 内部已兜业务异常，这里是最后的保险
            log.exception("任务 %s 执行框架异常", job_id)


async def start_worker() -> None:
    global _worker_task, _stop_event
    if _worker_task is not None:
        return
    _stop_event = asyncio.Event()
    # 多 worker（2026-10-27 提速）：评分与规则编译并行，消除单 worker 排队阻塞
    # （job 155 被 28min compile_rules 队头挡了 18min）。_claim_next_job 是条件
    # UPDATE 原子领取，多 worker 下不会重复领取。默认 1（串行）兼容旧行为。
    n = config.job_workers()
    _worker_task = asyncio.gather(
        *(_worker_loop() for _ in range(n)), return_exceptions=True
    ) if n > 1 else asyncio.create_task(_worker_loop(), name="job-worker")


async def stop_worker() -> None:
    global _worker_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _worker_task is not None:
        _worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _worker_task
        _worker_task = None
