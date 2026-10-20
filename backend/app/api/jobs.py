"""任务 API：查询、SSE 进度、取消。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sse_starlette.sse import EventSourceResponse

from app.errors import NotFound
from app.jobs import queue

router = APIRouter(tags=["jobs"])

_TERMINAL = ("succeeded", "failed", "cancelled")


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str | None
    kind: str
    status: str
    done_steps: int
    total_steps: int
    message: str | None
    result_json: dict | None
    error: str | None
    attempts: int
    queue_position: int = 0


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int) -> JobOut:
    job = queue.get_job(job_id)
    if job is None:
        raise NotFound("任务不存在")
    out = JobOut.model_validate(job)
    out.queue_position = queue.queue_position(job_id)
    return out


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    ok = queue.request_cancel(job_id)
    if not ok:
        raise NotFound("任务不存在或已结束")
    return {"ok": True}


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: int) -> EventSourceResponse:
    """任务进度 SSE 流：每秒推一次，终态（附 error/result_json）后关闭。

    前端 EventSource 无法自定义请求头，故本端点刻意不依赖 X-Actor（只读）。
    ping=15 由 sse_starlette 发注释行心跳，防中间层按空闲断连。
    """

    async def _gen():
        while True:
            job = await asyncio.to_thread(queue.get_job, job_id)
            if job is None:
                yield {"event": "error", "data": json.dumps({"message": "任务不存在"})}
                return
            payload = {
                "status": job.status,
                "kind": job.kind,
                "done_steps": job.done_steps,
                "total_steps": job.total_steps,
                "message": job.message,
                "queue_position": queue.queue_position(job_id) if job.status == "queued" else 0,
            }
            if job.status in _TERMINAL:
                # 终态把 error/result_json 一并送出，前端不必再补一次 GET /jobs/{id}
                payload["error"] = job.error
                payload["result_json"] = job.result_json
            yield {
                "event": "progress",
                "data": json.dumps(payload, ensure_ascii=False),
            }
            if job.status in _TERMINAL:
                return
            await asyncio.sleep(1.0)

    return EventSourceResponse(_gen(), ping=15)
