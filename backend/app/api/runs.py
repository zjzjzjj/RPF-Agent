"""评分运行与评分矩阵 API。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.deps import get_actor
from app.errors import NotFound
from app.services import audit_service, scoring_service
from app.services.ranking_service import compute_ranking
from app.services.automation_service import progress as automation_progress

router = APIRouter(tags=["runs"])


class RunCreate(BaseModel):
    package_id: int | None = None  # 缺省用当前冻结包
    bidder_ids: list[int]
    stages: list[Literal["objective", "price", "subjective"]] | None = None
    provider: str | None = None
    model: str | None = None  # 前端可选模型（模型目录内的 id）
    executor_version: Annotated[int, Field(strict=True, ge=1, le=2)] | None = None
    failure_policy: Literal["stop", "continue"] = "continue"


class FailurePolicyUpdate(BaseModel):
    failure_policy: Literal["stop", "continue"]


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
    automation: dict | None = None
    executor_version: int = 1
    execution_progress: dict | None = None
    qualification_mode: str | None = None
    count_deterministic_mode: str | None = None


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
    auto_review_basis: str | None = None


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
        executor_version=body.executor_version,
        failure_policy=body.failure_policy,
    )
    audit_service.log(project_id, actor, "run.create", "run", str(run_id),
                      {"package_id": package_id, "bidders": body.bidder_ids, "model": body.model,
                       "executor_version": body.executor_version, "failure_policy": body.failure_policy})
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
        from app.services.automation_service import progress
        return [_run_out(r, progress(r.id, session=s)) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int) -> RunOut:
    from sqlalchemy import select

    from app.db.models import EvaluationRun
    from app.db.session import session_scope

    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("运行不存在")
        from app.services.automation_service import progress
        return _run_out(run, progress(run_id, session=s))


def _run_out(run, automation: dict | None) -> RunOut:
    from app.services.execution_version import run_executor_version

    cfg = run.config_json or {}
    return RunOut.model_validate(run).model_copy(update={
        "automation": automation, "executor_version": run_executor_version(run),
        "execution_progress": (automation or {}).get("execution_progress"),
        "qualification_mode": cfg.get("qualification_mode"),
        "count_deterministic_mode": cfg.get("count_deterministic_mode"),
    })


@router.get("/runs/{run_id}/execution-progress")
def get_execution_progress(run_id: int) -> dict | None:
    from app.services.automation_service import execution_progress
    from app.db.models import EvaluationRun
    from app.db.session import session_scope

    with session_scope() as s:
        if s.get(EvaluationRun, run_id) is None:
            raise NotFound("运行不存在")
        return execution_progress(run_id, session=s)


@router.get("/runs/{run_id}/results.csv")
def export_results(run_id: int) -> Response:
    from app.services.result_export_service import export_results_csv

    return Response(export_results_csv(run_id), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="run{run_id}-results.csv"'})


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: int, actor: str = Depends(get_actor)) -> dict:
    from app.services.automation_service import resume_run as resume

    continued_run_id, job_id = resume(run_id, actor)
    return {"run_id": continued_run_id, "job_id": job_id}


@router.patch("/runs/{run_id}/failure-policy")
def update_failure_policy(run_id: int, body: FailurePolicyUpdate, actor: str = Depends(get_actor)) -> dict:
    from app.services.automation_service import set_failure_policy

    return set_failure_policy(run_id, body.failure_policy, actor)


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
        automation = automation_progress(run_id, session=s)
    return {
        "score_records": int(total),
        "need_human": 0 if automation is not None else int(need_human),
        "conflicts": int(conflicts),
        "cited": int(cited),
        "verified": int(verified),
        "verification_rate": round(verified / cited, 4) if cited else None,
        "automation": automation,
    }


@router.get("/runs/{run_id}/matrix")
def run_matrix(run_id: int) -> dict:
    from sqlalchemy import select

    from app.db.models import Bidder, Criterion, EvaluationRun, ScoreRecord
    from app.db.session import session_scope

    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("评分运行不存在")
        automation = automation_progress(run_id, session=s)
        manifest = (run.config_json or {}).get("automation_manifest") or {}
        rows = s.execute(
            select(ScoreRecord, Criterion, Bidder)
            .join(Criterion, Criterion.id == ScoreRecord.criterion_id)
            .join(Bidder, Bidder.id == ScoreRecord.bidder_id)
            .where(ScoreRecord.run_id == run_id)
            .order_by(Criterion.sort_order, Bidder.id)
        ).all()

    bidders: dict[int, dict] = {}
    criteria_index: dict[int, dict] = {}
    if automation is not None:
        bidders = {int(b["id"]): {"bidder_id": int(b["id"]), "name": b["name"], "status": "pending"}
                   for b in manifest.get("bidders", [])}
        criteria_index = {int(c["id"]): {
            "criterion_id": int(c["id"]), "code": c.get("code"), "name": c.get("name", c.get("code")),
            "category": c.get("category"), "score_type": c.get("score_type"),
            "max_score": str(c["max_score"]) if c.get("max_score") is not None else None,
        } for c in manifest.get("criteria", [])}
    cells: dict[int, dict[int, MatrixCell]] = {}
    for score, crit, bidder in rows:
        if automation is not None:
            if bidder.id not in bidders or crit.id not in criteria_index:
                continue
            meta = criteria_index[crit.id]
        else:
            bidders[bidder.id] = {"bidder_id": bidder.id, "name": bidder.name, "status": bidder.status}
            meta = criteria_index[crit.id] = {
                "criterion_id": crit.id, "code": crit.code, "name": crit.name,
                "category": crit.category, "score_type": crit.score_type,
                "max_score": str(crit.max_score) if crit.max_score is not None else None,
            }
        cells.setdefault(bidder.id, {})[crit.id] = MatrixCell(
            score_record_id=score.id,
            criterion_code=meta["code"] or crit.code,
            criterion_name=meta["name"] or crit.name,
            category=meta["category"] or crit.category,
            score_type=meta["score_type"] or crit.score_type,
            max_score=meta["max_score"],
            final_score=str(score.final_score) if score.final_score is not None else None,
            machine_score=str(score.machine_score) if score.machine_score is not None else None,
            review_status=score.review_status,
            evidence_status=score.evidence_status,
            veto_flag=score.veto_flag,
            machine_status=score.machine_status,
            auto_review_basis=(score.ai_raw_json or {}).get("auto_review", {}).get("basis"),
        )
    return {
        "run_id": run_id,
        "automation": automation,
        "out_of_scope": (run.config_json or {}).get("excluded_criteria") or [],
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


@router.get("/runs/{run_id}/score-summary")
def run_score_summary(
    run_id: int,
    project_id: str = Query(..., min_length=1),
    max_reason_chars: int = Query(1200, ge=200, le=8000),
) -> dict:
    """评分结果单页的聚合数据：排名 + 四分桶 + 逐项加减分明细与理由。

    总分/名次/各桶分值与 `/projects/{pid}/ranking` 同源（都走 compute_ranking）。
    """
    from app.services.score_summary_service import build_score_summary

    return build_score_summary(project_id, run_id, max_reason_chars=max_reason_chars)
