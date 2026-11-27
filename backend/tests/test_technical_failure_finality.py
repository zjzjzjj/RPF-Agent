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


# ==== P0-A（2026-11-28）全自动口径：技术失败格=暂定分+保留续跑 ====
# run205 实证：同规则包两次运行，c44/c49 被收口定 0 翻转 18 分——管线失败
# 不得冒充评审结论。以下四例锁「崩溃小计不外泄 + 暂定分计入总分直接排名 +
# 保留续跑 + 回滚开关」。


def _full_auto_env(monkeypatch):
    monkeypatch.setenv("BID_EVAL_FULL_AUTO", "1")
    monkeypatch.setenv("BID_EVAL_SCORE_CLOSURE", "1")


def test_full_auto_technical_failure_keeps_crash_subtotal_audit_only(app_env, monkeypatch):
    _full_auto_env(monkeypatch)
    ctx = _run_context(app_env)
    error = LLMError("INTERNAL-SECRET 输出字段缺失")
    error.review_attempts_exhausted = True
    error.verified_subtotal = Decimal("2.00")
    error.verified_subtotal_source = "model_crash"
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], None, error)
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][1], CellOutcome(
        "pass", "完整证据核验后确定性计分", Decimal("5"), [], [],
        basis="deterministic", raw={"calculation": "满足条件，计5分"},
    ))
    with session_scope() as session:
        session.get(EvaluationRun, ctx["run_id"]).status = "succeeded"

    with session_scope() as session:
        row = session.query(ScoreRecord).filter_by(criterion_id=ctx["criteria"][0]["id"]).one()
        # 崩溃模型小计 2.00 绝不进分：暂定 0（非评审结论，续核可翻案）
        assert row.final_score == Decimal("0")
        assert row.machine_status == "concern"
        meta = row.ai_raw_json["auto_review"]
        assert meta["basis"] == "fallback"
        assert meta["provisional"] is True
        assert meta["provisional_value"] == "0"
        assert meta["resumable"] is True
        fallback = meta["fallback"]
        assert fallback["value"] == "2.00"  # 仅审计留痕
        assert fallback["value_source"] == "model_crash"
        assert "provisional_zero" not in str(row.ai_raw_json)

    state = progress(ctx["run_id"])
    assert state["material_pending"] == 1 and state["failed"] == 0
    # is_final（可发布出排名）与 resumable（可翻案）解耦，二者同时成立
    assert state["is_final"] is True and state["resumable"] is True
    ranked = compute_ranking("technical-finality", ctx["run_id"])
    row0 = ranked["rows"][0]
    assert row0["rank"] == 1  # 零未定分：暂定分直接排名
    assert Decimal(row0["total"]) == Decimal("5")  # 5（定分）+ 0（暂定）；崩溃小计 2.00 不在总分
    assert row0["fallback"] == ["案例一"]
    assert ranked["has_fallback"] is True


def test_full_auto_technical_failure_uses_deterministic_provisional(app_env, monkeypatch):
    _full_auto_env(monkeypatch)
    ctx = _run_context(app_env)
    error = LLMError("执行故障")
    error.provisional_value = Decimal("3.00")
    error.provisional_source = "deterministic"
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], None, error)
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][1], CellOutcome(
        "pass", "完整证据核验后确定性计分", Decimal("5"), [], [],
        basis="deterministic", raw={"calculation": "满足条件，计5分"},
    ))
    with session_scope() as session:
        session.get(EvaluationRun, ctx["run_id"]).status = "succeeded"

    with session_scope() as session:
        row = session.query(ScoreRecord).filter_by(criterion_id=ctx["criteria"][0]["id"]).one()
        assert row.final_score == Decimal("3.00")
        meta = row.ai_raw_json["auto_review"]
        assert meta["provisional"] is True
        assert meta["provisional_value"] == "3.00"
        assert meta["provisional_source"] == "deterministic"

    state = progress(ctx["run_id"])
    assert state["material_pending"] == 1 and state["resumable"] is True
    ranked = compute_ranking("technical-finality", ctx["run_id"])
    assert Decimal(ranked["rows"][0]["total"]) == Decimal("8")
    assert ranked["rows"][0]["rank"] == 1
    assert ranked["rows"][0]["breakdown"][0]["provisional"] is True


def test_full_auto_provisional_cell_remains_resumable(app_env, monkeypatch):
    """暂定格保留续跑（P0-A）：_is_reading_gap_row 认它，resume 可删行重评。

    完整 resume_run 链（输入指纹校验/删行/入队）由 test_automatic_scoring 的
    managed_run 用例覆盖；这里锁全自动暂定格的续跑资格判定。"""
    _full_auto_env(monkeypatch)
    ctx = _run_context(app_env)
    error = LLMError("执行故障")
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], None, error)
    with session_scope() as session:
        session.get(EvaluationRun, ctx["run_id"]).status = "succeeded"
        row = session.query(ScoreRecord).one()

        from app.services.automation_service import _is_reading_gap_row

        assert _is_reading_gap_row(row) is True
    state = progress(ctx["run_id"])
    assert state["material_pending"] == 1 and state["resumable"] is True


def test_full_auto_technical_provisional_off_keeps_legacy_settle(app_env, monkeypatch):
    _full_auto_env(monkeypatch)
    monkeypatch.setenv("BID_EVAL_TECHNICAL_PROVISIONAL", "0")
    ctx = _run_context(app_env)
    error = LLMError("执行故障")
    error.verified_subtotal = Decimal("2.00")
    error.verified_subtotal_source = "model_crash"
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], None, error)

    with session_scope() as session:
        row = session.query(ScoreRecord).one()
        assert row.final_score == Decimal("2.00")  # 回滚：旧收口语义（小计直接定格）
        assert not (row.ai_raw_json["auto_review"] or {}).get("provisional")
    state = progress(ctx["run_id"])
    assert state["material_pending"] == 0 and state["resumable"] is False


def test_full_auto_settled_fallback_survives_closure_pass(app_env, monkeypatch):
    """run205 根因回归（2026-11-28 实证）：全自动定格过的真实评分（正已核实
    小计）不得被 closure 收口覆盖成通用 0——auto_finalized 必须落进
    auto_review 元数据，_valid_cell 警告闸据其放行，closure 只封真正
    未完成的格。c44/c48/c49 两次运行翻转 18 分即此覆盖链。"""
    _full_auto_env(monkeypatch)
    ctx = _run_context(app_env)
    outcome = CellOutcome(
        "concern", "仍需核验查询截图", None, [], [], "fallback", 1,
        {"fallback": {"kind": "material_review_pending", "value": "3",
                      "value_role": "verified_subtotal", "basis": "machine_fallback",
                      "required_materials": ["查询截图"], "candidate_pages": []},
         "coverage_complete": False},
        [{"message": "仍需核验查询截图",
          "fallback_score": {"kind": "material_review_pending", "value": "3"}}],
    )
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], outcome)

    # closure 再收口一次：已完成格必须原样保留（is_cell_completed 短路）
    sealed = LLMError("自动评审已按当前材料形成最终结果")
    sealed.code = "closure_sealed"
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], None, sealed)

    with session_scope() as session:
        row = session.query(ScoreRecord).one()
        assert row.final_score == Decimal("3")  # 真实定格分保留
        meta = row.ai_raw_json["auto_review"]
        assert meta.get("error_code") != "closure_sealed"
        assert meta.get("auto_finalized") is True


def test_full_auto_settled_zero_fallback_is_provisional_and_survives_closure(app_env, monkeypatch):
    """零值技术兜底格：暂定 0 + 可续核，closure 不得二次覆盖其审计内容。"""
    _full_auto_env(monkeypatch)
    ctx = _run_context(app_env)
    outcome = CellOutcome(
        "concern", "仍需核验查询截图", None, [], [], "fallback", 1,
        {"fallback": {"kind": "material_review_pending", "value": None,
                      "value_role": "verified_subtotal", "basis": "machine_fallback",
                      "required_materials": ["查询截图"], "candidate_pages": []},
         "coverage_complete": False},
        [{"message": "仍需核验查询截图",
          "fallback_score": {"kind": "material_review_pending", "value": None}}],
    )
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], outcome)
    sealed = LLMError("自动评审已按当前材料形成最终结果")
    sealed.code = "closure_sealed"
    _persist_cell(ctx["run_id"], ctx["bidder_id"], ctx["criteria"][0], None, sealed)
    with session_scope() as session:
        session.get(EvaluationRun, ctx["run_id"]).status = "succeeded"

    with session_scope() as session:
        row = session.query(ScoreRecord).one()
        meta = row.ai_raw_json["auto_review"]
        assert row.final_score == Decimal("0")
        assert meta.get("provisional") is True
        assert meta.get("error_code") != "closure_sealed"
    state = progress(ctx["run_id"])
    assert state["material_pending"] == 1 and state["resumable"] is True


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
