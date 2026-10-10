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


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int) -> JobOut:
    job = queue.get_job(job_id)
    if job is None:
        raise NotFound("任务不存在")
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    ok = queue.request_cancel(job_id)
    if not ok:
        raise NotFound("任务不存在或已结束")
    return {"ok": True}


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: int) -> EventSourceResponse:
    async def _gen():
        while True:
            job = await asyncio.to_thread(queue.get_job, job_id)
            if job is None:
                yield {"event": "error", "data": json.dumps({"message": "任务不存在"})}
                return
            yield {
                "event": "progress",
                "data": json.dumps(
                    {
                        "status": job.status,
                        "done_steps": job.done_steps,
                        "total_steps": job.total_steps,
                        "message": job.message,
                    },
                    ensure_ascii=False,
                ),
            }
            if job.status in _TERMINAL:
                return
            await asyncio.sleep(1.0)

    return EventSourceResponse(_gen())
