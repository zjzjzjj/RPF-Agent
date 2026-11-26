# -*- coding: utf-8 -*-
"""Technical failures remain pending and never enter final ranking."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.db.models import (
    Bidder, Criterion, EvaluationRun, Project, ReviewCollectionBatch,
    ReviewCollectionSet, RulePackage, ScoreRecord, utcnow,
)
from app.db.session import session_scope
from app.llm.base import LLMError
from app.services.automatic_scoring_service import CellOutcome, _persist_cell
from app.services.automation_service import execution_progress, progress
from app.services.ranking_service import compute_ranking


def _run_context(app_env):
    with session_scope() as session:
        session.add(Project(id="technical-finality", name="技术终态测试"))
        session.flush()
        package = RulePackage(project_id="technical-finality", version=1, status="frozen")
        session.add(package)
        session.flush()
        bidder = Bidder(project_id="technical-finality", name="甲")
        first = Criterion(package_id=package.id, code="C1", name="案例一",
                          category="commercial", score_type="count", max_score=Decimal("10"))
        second = Criterion(package_id=package.id, code="C2", name="案例二",
                           category="commercial", score_type="count", max_score=Decimal("10"))
        session.add_all([bidder, first, second])
        session.flush()
        criteria = [
            {"id": first.id, "code": "C1", "name": first.name, "category": "commercial",
             "score_type": "count", "max_score": "10", "description": "未提供的不得分。"},
            {"id": second.id, "code": "C2", "name": second.name, "category": "commercial",
             "score_type": "count", "max_score": "10", "description": "满足条件得10分。"},
        ]
        manifest = {
            "criteria": criteria,
            "bidders": [{"id": bidder.id, "name": bidder.name}],
            "provider": "offline", "model": "offline",
        }
        run = EvaluationRun(
            project_id="technical-finality", package_id=package.id, package_version=1,
            status="running", stages_json=["objective"],
            config_json={"automation_version": 1, "bidder_ids": [bidder.id],
                         "automation_manifest": manifest},
        )
        session.add(run)
        session.flush()
        return {
            "run_id": run.id, "bidder_id": bidder.id,
            "criteria": criteria, "manifest": manifest,
        }


def test_model_failure_keeps_verified_subtotal_out_of_final_score_and_rank(app_env):
    ctx = _run_context(app_env)
    error = LLMError("INTERNAL-SECRET 输出字段缺失")
    error.review_attempts_exhausted = True
    error.verified_subtotal = Decimal("2.00")
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], None, error)

    with session_scope() as session:
        row = session.query(ScoreRecord).one()
        assert row.final_score is None
        assert row.machine_score is None and row.ai_suggested_score is None
        fallback = row.ai_raw_json["auto_review"]["fallback"]
        assert fallback["kind"] == "material_review_pending"
        assert fallback["reason_code"] == "model_output_incomplete"
        assert fallback["value"] == "2.00"
        assert fallback["value_role"] == "verified_subtotal"
        assert "provisional_zero" not in str(row.ai_raw_json)

    state = progress(ctx["run_id"])
    assert state["material_pending"] == 1 and state["failed"] == 0
    assert state["is_final"] is False and state["complete"] is False
    ranked = compute_ranking("technical-finality", ctx["run_id"])
    assert ranked["rows"][0]["rank"] is None
    assert ranked["rows"][0]["total"] == "0.00"
    assert ranked["rows"][0]["missing_final"] == ["案例一"]


def test_reading_failure_is_pending_and_resumable(app_env):
    ctx = _run_context(app_env)
    error = LLMError("INTERNAL-SECRET 原页无法读取")
    error.reading_incomplete = True
    error.unreadable_pages = [{"document_id": 7, "page_no": 9}]
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], None, error)

    with session_scope() as session:
        row = session.query(ScoreRecord).one()
        assert row.final_score is None
        fallback = row.ai_raw_json["auto_review"]["fallback"]
        assert fallback["kind"] == "material_review_pending"
        assert fallback["reason_code"] == "reading_incomplete"
        assert fallback["value"] is None
        assert fallback["unreadable_pages"] == [{"document_id": 7, "page_no": 9}]

    state = progress(ctx["run_id"])
    assert state["material_pending"] == 1 and not state["is_final"]
    assert compute_ranking("technical-finality", ctx["run_id"])["rows"][0]["rank"] is None


def test_complete_confirmed_absence_with_explicit_sanction_still_settles_zero(app_env):
    ctx = _run_context(app_env)
    zero = CellOutcome(
        "fail", "完整核验确认未提供，按细则不得分", Decimal("0"), [], [],
        basis="absence", raw={
            "confirmed_absent": True, "coverage_complete": True,
            "zero_reason": "confirmed_absent",
        },
    )
    scored = CellOutcome(
        "pass", "完整证据核验后确定性计分", Decimal("5"), [], [],
        basis="deterministic", raw={"calculation": "满足条件，计5分"},
    )
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], zero)
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][1], scored)
    with session_scope() as session:
        session.get(EvaluationRun, ctx["run_id"]).status = "succeeded"

    state = progress(ctx["run_id"])
    assert state["is_final"] is True and state["material_pending"] == 0
    with session_scope() as session:
        rows = {row.criterion_id: row for row in session.query(ScoreRecord).all()}
        assert rows[ctx["criteria"][0]["id"]].final_score == Decimal("0")
        assert rows[ctx["criteria"][1]["id"]].final_score == Decimal("5")
    ranked = compute_ranking("technical-finality", ctx["run_id"])
    assert ranked["rows"][0]["rank"] == 1
    assert ranked["rows"][0]["total"] == "5.00"


def test_execution_progress_moves_only_later_covered_failures_to_history(app_env):
    ctx = _run_context(app_env)
    now = utcnow()
    failed_at = now - timedelta(minutes=5)
    covered_at = now + timedelta(minutes=1)
    collections = {}
    for index, criterion in enumerate(ctx["criteria"]):
        collection = ReviewCollectionSet(
            run_id=ctx["run_id"], group_key=f"v1-text:{index}",
            criterion_id=criterion["id"], bidder_id=ctx["bidder_id"],
            input_signature=f"signature-{index}", generation=1,
            plan_json={"kind": "v1-text", "batch_keys": ["b0"],
                       "units": [[{"unit_id": f"u{index}", "document_id": 7,
                                   "page_no": 20 + index}]]},
            state="failed",
        )
        with session_scope() as session:
            session.add(collection)
            session.flush()
            batch = ReviewCollectionBatch(
                collection_id=collection.id, batch_key="b0",
                unit_ids_json=[f"u{index}"], state="failed", generation=1,
                error_json={"code": "LLMError", "reason_code": "execution_incomplete",
                            "message": "材料采集或自动评审未形成可核验终态，待人工复核"},
                created_at=failed_at, updated_at=failed_at, started_at=failed_at,
            )
            session.add(batch)
            collections[criterion["id"]] = collection.id

    with session_scope() as session:
        covered = ScoreRecord(
            run_id=ctx["run_id"], bidder_id=ctx["bidder_id"],
            criterion_id=ctx["criteria"][0]["id"], review_status="auto_confirmed",
            machine_status="pass", final_score=Decimal("5"), confirm_time=covered_at,
            updated_at=covered_at,
            ai_raw_json={"auto_review": {"state": "completed", "basis": "deterministic",
                                         "reasoning": "后续补评已覆盖"}},
        )
        pending = ScoreRecord(
            run_id=ctx["run_id"], bidder_id=ctx["bidder_id"],
            criterion_id=ctx["criteria"][1]["id"], review_status="auto_confirmed",
            machine_status="concern", final_score=None, confirm_time=covered_at,
            updated_at=covered_at,
            ai_raw_json={"auto_review": {
                "state": "completed", "basis": "fallback", "reasoning": "待人工复核",
                "fallback": {"kind": "material_review_pending", "value": None},
            }},
        )
        session.add_all([covered, pending])

    state = execution_progress(ctx["run_id"])
    assert state["batches_failed"] == 1
    assert state["batches_failed_historical"] == 1
    assert [item["resolved"] for item in state["historical_failures"]] == [True]
    assert state["historical_failures"][0]["covered_by_score_id"] is not None
    assert [item["resolved"] for item in state["failures"]] == [False]
    assert "INTERNAL" not in str(state)
