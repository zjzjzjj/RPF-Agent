"""评分运行与评分矩阵 API。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.deps import get_actor
from app.errors import NotFound
from app.services import audit_service, scoring_service
from app.services.ranking_service import compute_ranking

router = APIRouter(tags=["runs"])


class RunCreate(BaseModel):
    package_id: int | None = None  # 缺省用当前冻结包
    bidder_ids: list[int]
    stages: list[Literal["objective", "price", "subjective"]] | None = None
    provider: str | None = None
    model: str | None = None  # 前端可选模型（模型目录内的 id）


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str
    package_id: int
    package_version: int
    package_content_hash: str | None
    status: str
    stages_json: list | None
    llm_provider: str | None
    llm_model: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class MatrixCell(BaseModel):
    score_record_id: int
    criterion_code: str
    criterion_name: str
    category: str
    score_type: str
    max_score: str | None
    final_score: str | None
    machine_score: str | None
    review_status: str
    evidence_status: str
    veto_flag: bool
    machine_status: str | None


@router.post("/projects/{project_id}/runs")
def create_run(project_id: str, body: RunCreate, actor: str = Depends(get_actor)) -> dict:
    from app.services import rule_service

    package_id = body.package_id
    if package_id is None:
        # 默认用最新**冻结**版本：current_package 会取到修订草稿（草稿不可评分）
        pkg = rule_service.latest_frozen_package(project_id)
        if pkg is None:
            from app.errors import ValidationFailed

            raise ValidationFailed("没有已冻结的规则包，不能发起评分")
        package_id = pkg.id
    run_id, job_id = scoring_service.create_run(
        project_id,
        package_id,
        body.bidder_ids,
        [s for s in (body.stages or ["objective", "price", "subjective"])],
        actor,
        body.provider,
        body.model,
    )
    audit_service.log(project_id, actor, "run.create", "run", str(run_id),
                      {"package_id": package_id, "bidders": body.bidder_ids, "model": body.model})
    return {"run_id": run_id, "job_id": job_id}


@router.get("/projects/{project_id}/runs", response_model=list[RunOut])
def list_runs(project_id: str) -> list[RunOut]:
    from sqlalchemy import select

    from app.db.models import EvaluationRun
    from app.db.session import session_scope

    with session_scope() as s:
        rows = s.execute(
            select(EvaluationRun).where(EvaluationRun.project_id == project_id)
            .order_by(EvaluationRun.id.desc())
        ).scalars().all()
        return [RunOut.model_validate(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int) -> RunOut:
    from sqlalchemy import select

    from app.db.models import EvaluationRun
    from app.db.session import session_scope

    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("运行不存在")
        s.expunge(run)
        return RunOut.model_validate(run)


@router.get("/runs/{run_id}/stats")
def run_stats(run_id: int) -> dict:
    from sqlalchemy import func, select

    from app.db.models import ScoreRecord
    from app.db.session import session_scope

    with session_scope() as s:
        total = s.execute(
            select(func.count(ScoreRecord.id)).where(ScoreRecord.run_id == run_id)
        ).scalar_one()
        need_human = s.execute(
            select(func.count(ScoreRecord.id)).where(
                ScoreRecord.run_id == run_id,
                ScoreRecord.review_status.in_(("pending", "ai_suggested", "conflict_flagged")),
            )
        ).scalar_one()
        conflicts = s.execute(
            select(func.count(ScoreRecord.id)).where(
                ScoreRecord.run_id == run_id, ScoreRecord.review_status == "conflict_flagged"
            )
        ).scalar_one()
        cited = s.execute(
            select(func.count(ScoreRecord.id)).where(
                ScoreRecord.run_id == run_id, ScoreRecord.evidence_status != "none"
            )
        ).scalar_one()
        verified = s.execute(
            select(func.count(ScoreRecord.id)).where(
                ScoreRecord.run_id == run_id, ScoreRecord.evidence_status == "verified"
            )
        ).scalar_one()
    return {
        "score_records": int(total),
        "need_human": int(need_human),
        "conflicts": int(conflicts),
        "cited": int(cited),
        "verified": int(verified),
        "verification_rate": round(verified / cited, 4) if cited else None,
    }


@router.get("/runs/{run_id}/matrix")
def run_matrix(run_id: int) -> dict:
    from sqlalchemy import select

    from app.db.models import Bidder, Criterion, ScoreRecord
    from app.db.session import session_scope

    with session_scope() as s:
        rows = s.execute(
            select(ScoreRecord, Criterion, Bidder)
            .join(Criterion, Criterion.id == ScoreRecord.criterion_id)
            .join(Bidder, Bidder.id == ScoreRecord.bidder_id)
            .where(ScoreRecord.run_id == run_id)
            .order_by(Criterion.sort_order, Bidder.id)
        ).all()

    bidders: dict[int, dict] = {}
    criteria_index: dict[int, dict] = {}
    cells: dict[int, dict[int, MatrixCell]] = {}
    for score, crit, bidder in rows:
        bidders[bidder.id] = {"bidder_id": bidder.id, "name": bidder.name, "status": bidder.status}
        criteria_index[crit.id] = {
            "criterion_id": crit.id, "code": crit.code, "name": crit.name,
            "category": crit.category, "score_type": crit.score_type,
            "max_score": str(crit.max_score) if crit.max_score else None,
        }
        cells.setdefault(bidder.id, {})[crit.id] = MatrixCell(
            score_record_id=score.id,
            criterion_code=crit.code,
            criterion_name=crit.name,
            category=crit.category,
            score_type=crit.score_type,
            max_score=str(crit.max_score) if crit.max_score else None,
            final_score=str(score.final_score) if score.final_score is not None else None,
            machine_score=str(score.machine_score) if score.machine_score is not None else None,
            review_status=score.review_status,
            evidence_status=score.evidence_status,
            veto_flag=score.veto_flag,
            machine_status=score.machine_status,
        )
    return {
        "run_id": run_id,
        "bidders": list(bidders.values()),
        "criteria": list(criteria_index.values()),
        "cells": {str(b): {str(c): cell.model_dump() for c, cell in cols.items()}
                  for b, cols in cells.items()},
    }


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, actor: str = Depends(get_actor)) -> dict:
    from sqlalchemy import select

    from app.db.models import EvaluationRun, Job
    from app.db.session import session_scope
    from app.jobs import queue

    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("运行不存在")
        # 必须按 payload 里的 run_id 过滤：一个项目可先后发起多次评分运行，
        # 只按 kind+状态取最新任务会取消到别的运行（口径同 scoring_service.reconcile_stale_runs）。
        job = s.execute(
            select(Job).where(
                Job.kind == "evaluate",
                Job.status.in_(("queued", "running")),
                Job.payload_json["run_id"].as_integer() == run_id,
            ).order_by(Job.id.desc()).limit(1)
        ).scalar_one_or_none()
    ok = queue.request_cancel(job.id) if job else False
    return {"ok": ok}


@router.get("/projects/{project_id}/ranking")
def get_ranking(project_id: str, run_id: int | None = None) -> dict:
    return compute_ranking(project_id, run_id)
