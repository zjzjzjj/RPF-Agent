"""Automatic execution preserves real completed work and never manufactures scores."""
from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import (
    Bidder, Criterion, Document, DocumentPage, EvaluationRun, EvidenceCitation,
    Job, Project, RulePackage, ScoreRecord,
)
from app.db.session import session_scope
from app.engine.auto_review import AutoReviewDecision
from app.llm.base import LLMError
from app.services import automation_service as automation
from app.services import automatic_scoring_service as scoring


PRICE_LIMIT_RULE = {
    "score_type": "veto", "check_type": "math_relation",
    "params": {"max_amount_yuan": "5049600", "yearly_max_amount_yuan": ["1519800", "1683200", "1846600"]},
    "_frozen_bidder": {"bid_price": "4017400", "price_status": "confirmed"},
}


def test_confirmed_total_never_substitutes_for_missing_annual_quotes():
    result = AutoReviewDecision(verdict="pass", reasoning="总价未超过合计上限", coverage_complete=True)
    with pytest.raises(LLMError, match="不能以总价代替年度报价"):
        scoring._verified_price_limit(PRICE_LIMIT_RULE, result)
    result.verdict = "fail"
    result.confirmed_absent = True
    verdict, detail, values = scoring._verified_price_limit(PRICE_LIMIT_RULE, result)
    assert verdict == "fail" and "确认缺少" in detail and values == {"bid_price": "4017400"}
    result.coverage_complete = False
    with pytest.raises(LLMError, match="尚未核验"):
        scoring._verified_price_limit(PRICE_LIMIT_RULE, result)


@pytest.mark.parametrize("first_year,expected", [("1519800", "pass"), ("1519800.01", "fail")])
def test_annual_price_threshold_is_recalculated_even_when_model_disagrees(first_year, expected):
    result = AutoReviewDecision(verdict="pass" if expected == "fail" else "fail", reasoning="模型初步结论",
                                facts={"yearly_bid_price_1": first_year, "yearly_bid_price_2": "1500000",
                                       "yearly_bid_price_3": "997600"})
    verdict, detail, values = scoring._verified_price_limit(PRICE_LIMIT_RULE, result)
    assert verdict == expected and "1519800" in detail and values["yearly_bid_price_1"] == first_year


def test_conflicting_extracted_total_requires_reextraction():
    result = AutoReviewDecision(verdict="pass", reasoning="原文报价与冻结值有差异", facts={"bid_price": "4017401"})
    with pytest.raises(LLMError, match="报价冲突"):
        scoring._verified_price_limit(PRICE_LIMIT_RULE, result)


def test_horizontal_executor_includes_all_frozen_bidders_and_preserves_ownership(managed_run, monkeypatch):
    with session_scope() as s:
        original = s.get(EvaluationRun, managed_run["id"])
        own_doc = s.get(Document, managed_run["document"])
        own_doc.data_class = "synthetic"
        peer = Bidder(project_id=original.project_id, name="乙", bid_price=Decimal("120"), price_status="confirmed")
        s.add(peer)
        s.flush()
        peer_doc = Document(project_id=original.project_id, bidder_id=peer.id, kind="bid", parse_status="parsed",
                            original_filename="peer.txt", stored_path=own_doc.stored_path, sha256=own_doc.sha256,
                            page_count=1, data_class="confidential")
        s.add(peer_doc)
        s.flush()
        s.add(DocumentPage(document_id=peer_doc.id, page_no=1, text=managed_run["text"], source="text"))
        run = EvaluationRun(project_id=original.project_id, package_id=original.package_id, package_version=1,
                            package_content_hash="frozen", status="queued", stages_json=["objective"],
                            config_json={"automation_version": 1, "bidder_ids": [managed_run["bidder"], peer.id]})
        s.add(run)
        s.flush()
        run_id, peer_id, peer_document = run.id, peer.id, peer_doc.id
    rule = {**managed_run["criteria"][0], "name": "串通投标情形", "check_type": "human_only"}
    automation.initialize_run(run_id, [rule])
    calls = []

    def assess(criterion, pages, data_class, gateway_factory):
        calls.append(criterion["_frozen_bidder"]["id"])
        assert {b["id"] for b in criterion["_frozen_bidders"]} == {managed_run["bidder"], peer_id}
        assert {p["document_id"] for p in pages} == {managed_run["document"], peer_document}
        assert {p["bidder_id"] for p in pages} == {managed_run["bidder"], peer_id}
        assert data_class == "confidential"
        counterpart = next(p for p in pages if p["bidder_id"] != criterion["_frozen_bidder"]["id"])
        return scoring.CellOutcome("unverifiable", "已比较所有投标文件，外部协商与平台记录未提供", pages=pages,
                                   evidence=[{"document_id": counterpart["document_id"], "page_no": 1,
                                              "quote": counterpart["text"], "kind": "text"}],
                                   basis="external", raw={"coverage_complete": True})

    monkeypatch.setattr(scoring, "_assess_cell", assess)
    result = scoring.execute_automatic_run(run_id)
    assert set(calls) == {managed_run["bidder"], peer_id} and result["automation"]["complete"]
    with session_scope() as s:
        rows = list(s.execute(select(ScoreRecord).where(ScoreRecord.run_id == run_id)).scalars())
        assert len(rows) == 2
        for row in rows:
            citation = s.execute(select(EvidenceCitation).where(EvidenceCitation.score_record_id == row.id)).scalar_one()
            assert citation.document_id == (peer_document if row.bidder_id == managed_run["bidder"] else managed_run["document"])
            assert citation.verified


@pytest.fixture()
def managed_run(app_env, monkeypatch):
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    monkeypatch.setenv("BID_EVAL_LLM_MODEL", "offline-test")
    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "0")
    monkeypatch.setenv("BID_EVAL_LLM_CONCURRENCY", "1")
    content = "投标人提供完整运行维护方案，响应流程明确且附有交付及保障措施。"
    source = app_env / "bid.txt"
    source.write_text(content, encoding="utf-8")
    with session_scope() as s:
        s.add(Project(id="auto-executor", name="自动评审测试"))
        s.flush()
        package = RulePackage(project_id="auto-executor", version=1, status="frozen", content_hash="frozen")
        s.add(package)
        s.flush()
        bidder = Bidder(project_id="auto-executor", name="甲", bid_price=Decimal("100"), price_status="confirmed")
        first = Criterion(package_id=package.id, code="Q1", name="方案存在", category="compliance",
                          score_type="veto", status="verified")
        second = Criterion(package_id=package.id, code="S1", name="运行维护方案", category="technical",
                           score_type="subjective", max_score=Decimal("10"), status="verified")
        s.add_all([bidder, first, second])
        s.flush()
        doc = Document(project_id="auto-executor", bidder_id=bidder.id, kind="bid", parse_status="parsed",
                       original_filename="bid.txt", stored_path=str(source),
                       sha256=hashlib.sha256(source.read_bytes()).hexdigest(), page_count=1)
        s.add(doc)
        s.flush()
        s.add(DocumentPage(document_id=doc.id, page_no=1, text=content, source="text"))
        criteria = [
            {"id": first.id, "code": "Q1", "name": first.name, "category": "compliance", "score_type": "veto",
             "check_type": "contains_phrase", "params": {"phrase": "运行维护方案"}, "max_score": None},
            {"id": second.id, "code": "S1", "name": second.name, "category": "technical", "score_type": "subjective",
             "max_score": "10", "description": "根据运行维护方案完整性评分，最高10分。"},
        ]
        run = EvaluationRun(project_id="auto-executor", package_id=package.id, package_version=1,
                            package_content_hash="frozen", status="queued",
                            stages_json=["objective", "subjective", "price"],
                            config_json={"automation_version": 1, "bidder_ids": [bidder.id]})
        s.add(run)
        s.flush()
        result = {"id": run.id, "bidder": bidder.id, "criteria": criteria, "document": doc.id, "text": content}
    automation.initialize_run(result["id"], criteria)
    return result


def test_failed_cell_preserves_success_and_resume_only_runs_unfinished(managed_run, monkeypatch):
    calls = []

    def fail_subjective(criterion, pages, data_class, gateway_factory):
        calls.append(criterion["code"])
        if criterion["code"] == "S1":
            raise LLMError("temporary offline model outage")
        return scoring.CellOutcome("pass", "AI已核实方案存在", pages=pages, basis="evidence",
                                   evidence=[{"document_id": managed_run["document"], "page_no": 1,
                                              "kind": "text", "quote": managed_run["text"]}])

    monkeypatch.setattr(scoring, "_assess_cell", fail_subjective)
    with pytest.raises(LLMError, match="保留"):
        scoring.execute_automatic_run(managed_run["id"])
    assert automation.progress(managed_run["id"])["completed"] == 1
    with session_scope() as s:
        rows = list(s.execute(select(ScoreRecord).where(ScoreRecord.run_id == managed_run["id"])).scalars())
        assert len(rows) == 2
        failed = next(row for row in rows if row.machine_status == "execution_failed")
        assert failed.final_score is None
        successful_id = next(row.id for row in rows if row.review_status == "auto_confirmed")

    def recovered(criterion, pages, data_class, gateway_factory):
        assert criterion["code"] == "S1", "successful cell must not be rerun"
        return scoring.CellOutcome("pass", "依据完整运行维护方案评为8分", Decimal("8"),
                                   [{"document_id": managed_run["document"], "page_no": 1,
                                     "quote": managed_run["text"], "kind": "text"}], pages,
                                   basis="evidence")

    monkeypatch.setattr(scoring, "_assess_cell", recovered)
    first = automation.resume_run(managed_run["id"], "tester")
    assert automation.resume_run(managed_run["id"], "tester") == first
    result = scoring.execute_automatic_run(managed_run["id"])
    assert result["need_human"] == 0
    assert result["automation"]["remaining"] == 0
    with session_scope() as s:
        assert s.get(ScoreRecord, successful_id).review_status == "auto_confirmed"
        assert s.get(Bidder, managed_run["bidder"]).status == "scored"
        citations = list(s.execute(select(EvidenceCitation)).scalars())
        assert len(citations) == 2 and all(c.verified for c in citations)
        rows = list(s.execute(select(ScoreRecord)).scalars())
        assert all(r.review_status == "auto_confirmed" for r in rows)


def test_exhausted_cell_failure_stops_dispatching_more_items(managed_run, monkeypatch):
    calls = []

    def fail(criterion, *args):
        calls.append(criterion["code"])
        error = LLMError("bounded recovery exhausted")
        error.validation_history = [{"attempt": index, "state": "failed", "error": "引文来源无效"}
                                    for index in range(3)]
        raise error

    monkeypatch.setattr(scoring, "_assess_cell", fail)
    with pytest.raises(LLMError, match="保留"):
        scoring.execute_automatic_run(managed_run["id"])
    assert calls == ["Q1"]
    assert automation.progress(managed_run["id"])["failed"] == 1
    assert "exhausted" in automation.progress(managed_run["id"])["last_error"]
    with session_scope() as s:
        row = s.execute(select(ScoreRecord).where(ScoreRecord.run_id == managed_run["id"])).scalar_one()
        assert len(row.ai_raw_json["validation_history"]) == 3
        assert row.final_score is None and row.machine_status == "execution_failed"


def test_continue_policy_records_first_error_and_runs_later_items(managed_run, monkeypatch):
    automation.set_failure_policy(managed_run["id"], "continue", "tester")
    calls = []

    def assess(criterion, pages, *args):
        calls.append(criterion["code"])
        if criterion["code"] == "Q1":
            raise LLMError("provider refused this item")
        return scoring.CellOutcome("pass", "方案证据支持8分", Decimal("8"),
                                   [{"document_id": managed_run["document"], "page_no": 1,
                                     "quote": managed_run["text"], "kind": "text"}], pages)

    monkeypatch.setattr(scoring, "_assess_cell", assess)
    result = scoring.execute_automatic_run(managed_run["id"])
    assert calls == ["Q1", "S1"]
    assert result["automation"]["completed"] == 1
    assert result["automation"]["skipped"] == 1
    assert result["automation"]["processed"] == 2
    assert result["automation"]["pending"] == 0
    assert not result["automation"]["complete"] and not result["automation"]["is_final"]
    assert result["automation"]["result_available"]
    with session_scope() as s:
        assert s.get(EvaluationRun, managed_run["id"]).status == "partial"
        failed = s.execute(select(ScoreRecord).where(ScoreRecord.machine_status == "execution_failed")).scalar_one()
        assert failed.final_score is None
    # Delayed execution does not retry skipped items or duplicate citations.
    scoring.execute_automatic_run(managed_run["id"])
    assert calls == ["Q1", "S1"]
    from app.services.result_export_service import export_results_csv
    from app.services.report_service import build_report, render_html
    csv = export_results_csv(managed_run["id"])
    assert "故障跳过" in csv and "provider refused this item" in csv and "8.00" in csv
    report = build_report("auto-executor", managed_run["id"])
    assert report["status"] == "partial" and "含故障跳过项" in render_html(report)
    assert report["ranking"]["rows"][0]["total"] == "8.00"


def test_continue_resume_skips_previously_failed_cells(managed_run, monkeypatch):
    first = managed_run["criteria"][0]
    scoring._persist_cell(managed_run["id"], managed_run["bidder"], first, None, LLMError("previous failure"))
    with session_scope() as s:
        s.get(EvaluationRun, managed_run["id"]).status = "failed"
    automation.set_failure_policy(managed_run["id"], "continue", "tester")
    receipt = automation.resume_run(managed_run["id"], "tester")
    calls = []

    def fail(criterion, *args):
        calls.append(criterion["code"])
        raise LLMError("new failure")

    monkeypatch.setattr(scoring, "_assess_cell", fail)
    from app.jobs.queue import JobContext
    scoring.execute_automatic_run(managed_run["id"], ctx=JobContext(receipt[1]))
    assert calls == ["S1"]
    assert automation.progress(managed_run["id"])["skipped"] == 2
    assert automation.resume_run(managed_run["id"], "tester") == receipt
    with session_scope() as s:
        job = s.get(Job, receipt[1])
        assert job.done_steps == job.total_steps == 3
        assert "跳过 2 项" in job.message


def test_continue_does_not_finalize_a_missing_matrix_record(managed_run):
    automation.set_failure_policy(managed_run["id"], "continue", "tester")
    scoring._persist_cell(managed_run["id"], managed_run["bidder"], managed_run["criteria"][0],
                          None, LLMError("unavailable"))
    from app.errors import ValidationFailed
    with pytest.raises(ValidationFailed):
        automation.finish_run(managed_run["id"])
    state = automation.progress(managed_run["id"])
    assert state["pending"] == 1 and not state["result_available"]
    from app.services.result_export_service import export_results_csv
    csv = export_results_csv(managed_run["id"])
    assert "故障跳过" in csv and "尚未执行" in csv and "运行维护方案" in csv


def test_continue_does_not_repeat_failed_assessment_or_full_ocr(managed_run, monkeypatch):
    from app.engine import auto_review
    from app.services.execution_version import bind_run_execution

    with session_scope() as s:
        manifest = s.get(EvaluationRun, managed_run["id"]).config_json["automation_manifest"]
    manifest["scoring_config"]["auto_review_rounds"] = 2
    pages, classification = scoring._load_pages(managed_run["bidder"])
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise LLMError("invalid evidence")

    monkeypatch.setattr(auto_review, "review_criterion", fail)
    monkeypatch.setattr(scoring, "_supplement_pages", lambda *a, **kw: pytest.fail("no full reread after error"))
    with bind_run_execution(manifest, failure_policy="continue"), pytest.raises(LLMError, match="invalid evidence"):
        scoring._assess_cell(managed_run["criteria"][1], pages, classification, lambda: object())
    assert len(calls) == 1


def test_continue_preserves_cancellation_and_does_not_emit_partial(managed_run, monkeypatch):
    from app.jobs.queue import JobCancelled

    automation.set_failure_policy(managed_run["id"], "continue", "tester")
    monkeypatch.setattr(scoring, "_assess_cell", lambda *a, **kw: (_ for _ in ()).throw(JobCancelled()))
    with pytest.raises(JobCancelled):
        scoring.execute_automatic_run(managed_run["id"])
    assert automation.progress(managed_run["id"])["failed"] == 0


def test_document_ocr_is_shared_across_clauses_and_activity_preserves_real_counts(managed_run, monkeypatch):
    from app.jobs.queue import JobContext
    from app.services.review_activity import report

    pages, classification = scoring._load_pages(managed_run["bidder"])
    monkeypatch.setattr(scoring, "_load_pages", lambda bid: ([{**p, "unreadable": True} for p in pages], classification))
    ocr_calls = []

    def supplement(criterion, original, **kwargs):
        ocr_calls.append(1)
        return [{**p, "unreadable": False, "supplemented": True} for p in original]

    def assess(criterion, original, *args):
        assert all(not p["unreadable"] for p in original)
        report("原文审阅 第 1/1 批")
        return scoring.CellOutcome("pass", "原页证据已核实", Decimal("8") if criterion["score_type"] == "subjective" else None,
                                   [{"document_id": managed_run["document"], "page_no": 1, "kind": "text", "quote": managed_run["text"]}],
                                   original, basis="evidence")

    monkeypatch.setattr(scoring, "_supplement_pages", supplement)
    monkeypatch.setattr(scoring, "_assess_cell", assess)
    with session_scope() as s:
        job = Job(kind="evaluate", status="running", payload_json={"run_id": managed_run["id"]})
        s.add(job); s.flush(); job_id = job.id
    ctx = JobContext(job_id)
    messages = []
    original_report = ctx.report

    def capture(message):
        messages.append(message)
        original_report(message)

    monkeypatch.setattr(ctx, "report", capture)
    scoring.execute_automatic_run(managed_run["id"], ctx=ctx)
    assert ocr_calls == [1]
    assert any("原文审阅" in message for message in messages)
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.done_steps == job.total_steps == 3


def test_invalid_completed_marker_can_be_repaired(managed_run):
    criterion = managed_run["criteria"][1]
    with session_scope() as s:
        row = ScoreRecord(run_id=managed_run["id"], bidder_id=managed_run["bidder"], criterion_id=criterion["id"],
                          machine_status="pass", review_status="auto_confirmed", final_score=Decimal("7"),
                          ai_raw_json={"auto_review": {"state": "completed", "basis": "evidence"}})
        s.add(row)
    pages, _ = scoring._load_pages(managed_run["bidder"])
    outcome = scoring.CellOutcome("pass", "证据补齐后形成8分结论", Decimal("8"),
                                  [{"document_id": managed_run["document"], "page_no": 1,
                                    "quote": managed_run["text"], "kind": "text"}], pages, basis="evidence")
    scoring._persist_cell(managed_run["id"], managed_run["bidder"], criterion, outcome)
    scoring._persist_cell(managed_run["id"], managed_run["bidder"], criterion, outcome)
    with session_scope() as s:
        record = s.execute(select(ScoreRecord)).scalar_one()
        assert record.final_score == Decimal("8")
        assert len(list(s.execute(select(EvidenceCitation)).scalars())) == 1


def test_bad_quote_rolls_back_score_and_completion_marker(managed_run):
    pages, _ = scoring._load_pages(managed_run["bidder"])
    outcome = scoring.CellOutcome("pass", "模型生成了不存在的证据", Decimal("8"),
                                  [{"document_id": managed_run["document"], "page_no": 1,
                                    "quote": "这段很长的引文并不真实存在于本次投标原文中", "kind": "text"}], pages,
                                  basis="evidence")
    with pytest.raises(LLMError, match="引文"):
        scoring._persist_cell(managed_run["id"], managed_run["bidder"], managed_run["criteria"][1], outcome)
    with session_scope() as s:
        assert not list(s.execute(select(ScoreRecord)).scalars())
        assert not list(s.execute(select(EvidenceCitation)).scalars())


def test_verified_objective_value_uses_formula_not_model_score():
    criterion = {"score_type": "count", "name": "响应时间", "max_score": "4", "params": {
        "engine": "value_tier", "field": "response_hours",
        "tiers": [{"max_value": "4", "score": "4"}, {"max_value": "8", "score": "2"}],
    }}
    decision = AutoReviewDecision(verdict="pass", reasoning="已核验承诺函4小时响应", coverage_complete=True,
                                  facts={"response_hours": "4"})
    score, detail, _ = scoring._verified_count(criterion, decision, [{"text": "响应时间：8小时。"}])
    assert score == Decimal("4")
    assert "4" in detail


def test_unreadable_or_partial_count_cannot_be_finalized():
    decision = AutoReviewDecision(verdict="pass", reasoning="只找到了部分材料", coverage_complete=False,
                                  facts={"certificates": ["ISO9001"]})
    with pytest.raises(LLMError, match="全部"):
        scoring._verified_count({"params": {}, "max_score": "3"}, decision, [])


def test_confirmed_absence_has_no_fallback_score(managed_run):
    criterion = managed_run["criteria"][1]
    outcome = scoring.CellOutcome("fail", "已完整查阅文件，未提供评分细则要求的方案，按细则不得分。",
                                  Decimal("0"), basis="absence",
                                  raw={"confirmed_absent": True, "coverage_complete": True, "zero_reason": "confirmed_absent"})
    scoring._persist_cell(managed_run["id"], managed_run["bidder"], criterion, outcome)
    assert (managed_run["bidder"], criterion["id"]) in automation.completed_keys(managed_run["id"])
    with session_scope() as s:
        row = s.execute(select(ScoreRecord)).scalar_one()
        assert row.final_score == 0
        assert row.warnings_json == []


def test_create_run_never_drops_bidder_with_missing_price(managed_run):
    from app.services.scoring_service import create_run

    with session_scope() as s:
        bidder = s.get(Bidder, managed_run["bidder"])
        bidder.bid_price = None
        bidder.price_status = "extracted"
        run = s.get(EvaluationRun, managed_run["id"])
        package_id = run.package_id
    run_id, _ = create_run("auto-executor", package_id, [managed_run["bidder"]],
                           ["price"], "tester")
    with session_scope() as s:
        created = s.get(EvaluationRun, run_id)
        assert set(created.stages_json) == {"objective", "subjective", "price"}
        cfg = created.config_json
        assert cfg["bidder_ids"] == [managed_run["bidder"]]
        assert cfg["automation_manifest"]["bidders"][0]["bid_price"] is None


def test_per_cell_failure_does_not_write_default_zero(managed_run):
    scoring._persist_cell(managed_run["id"], managed_run["bidder"], managed_run["criteria"][1], None,
                          LLMError("timeout"))
    with session_scope() as s:
        row = s.execute(select(ScoreRecord)).scalar_one()
        assert row.machine_status == "execution_failed"
        assert row.final_score is None and row.machine_score is None
    assert automation.progress(managed_run["id"])["failed"] == 1


def test_closure_fallback_settles_failed_cell(managed_run, monkeypatch):
    monkeypatch.setenv("BID_EVAL_SCORE_CLOSURE", "1")
    criterion = managed_run["criteria"][1]
    scoring._persist_cell(managed_run["id"], managed_run["bidder"], criterion, None,
                          LLMError("provider unavailable"))
    with session_scope() as s:
        row = s.execute(select(ScoreRecord)).scalar_one()
        assert row.machine_status == "concern"
        assert row.final_score == Decimal("0.00")
        assert row.review_status == "auto_confirmed"
        assert row.ai_raw_json["auto_review"]["basis"] == "fallback"
        assert row.warnings_json[0]["fallback_score"]["kind"]
    state = automation.progress(managed_run["id"])
    assert state["completed"] == 1
    assert state["fallback"] == 1
    assert state["failed"] == 0


def test_direct_subjective_accepts_score_without_evidence(monkeypatch):
    from app.engine import auto_review as ar

    monkeypatch.setenv("BID_EVAL_SUBJECTIVE_DIRECT", "1")

    def fake_review(*args, **kwargs):
        return ar.AutoReviewDecision(verdict="pass", reasoning="方案完整可行", score=Decimal("3"))

    monkeypatch.setattr(ar, "review_criterion", fake_review)
    pages = [{"document_id": 1, "page_no": 1, "text": "运维方案内容", "source": "text"}]
    outcome = scoring._assess_cell(
        {"name": "运维方案", "score_type": "subjective", "max_score": "5",
         "description": "对运维方案进行综合评审"},
        pages, "synthetic", lambda: object(),
    )
    assert outcome.score == Decimal("3.00")
    assert outcome.basis == "model"
    assert outcome.verdict == "pass"


def test_keyword_qualification_requires_ai_final_review(monkeypatch):
    from app.engine import auto_review

    pages = [{"document_id": 1, "page_no": 1, "text": "这是包含营业执照一词的目录模板，并不提供证明材料。", "source": "text"}]
    calls = []

    def review(*args, **kwargs):
        calls.append(kwargs["round_no"])
        return AutoReviewDecision(verdict="fail", reasoning="目录模板不构成营业执照证明",
                                  evidence=[{"document_id": 1, "page_no": 1, "kind": "text", "quote": pages[0]["text"]}])

    monkeypatch.setattr(auto_review, "review_criterion", review)
    outcome = scoring._assess_cell({"name": "营业执照", "score_type": "veto", "check_type": "contains_phrase",
                                   "params": {"phrase": "营业执照"}}, pages, "synthetic", lambda: object())
    assert outcome.verdict == "fail" and outcome.basis == "evidence"
    assert calls == [0]


def test_cited_law_does_not_expand_document_presence_check_into_signature_review():
    assert not scoring._is_visual_requirement({"name": "投标文件应包含投标函", "description": "须包含投标函章节",
                                               "source_quote": "投标文件未经投标单位盖章和单位负责人签字，应当否决其投标。"})
    assert scoring._is_visual_requirement({"name": "投标函签章", "description": "核验单位盖章和负责人签字"})


LANGUAGE_REQUIREMENT_2640 = (
    "（4）投标文件的语言。投标文件以及投标人与招标人就有关投标事宜的所有来往函电，"
    "均应以中文书写。除签名、盖章、专用名称等特殊情形外，"
    "以中文以外的文字表述的投标文件视同未提供。"
)


def test_frozen_language_rule_2640_does_not_require_signature_images():
    assert not scoring._is_visual_requirement({
        "name": "投标文件的语言", "description": LANGUAGE_REQUIREMENT_2640,
        "required_evidence": LANGUAGE_REQUIREMENT_2640,
    })


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_frozen_language_rule_accepts_ai_text_verdict_without_forced_vision(monkeypatch, verdict):
    text = "投标人承诺投标文件及有关投标事宜的来往函电均以中文书写。"
    pages = [{"document_id": 77, "page_no": 1, "text": text, "source": "text",
              "has_text_layer": True, "unreadable": False, "requires_vision": False}]
    monkeypatch.setattr(scoring, "_supplement_pages", lambda *a, **kw: pytest.fail("语言例外不强制补读签章图"))
    calls = []

    def review(*args, **kwargs):
        calls.append((kwargs["round_no"], kwargs["use_vision"]))
        return AutoReviewDecision(verdict=verdict, reasoning="已根据全文语言事实评审",
            evidence=[{"document_id": 77, "page_no": 1, "kind": "text", "quote": text}])

    outcome = scoring._assess_cell({"id": 2640, "name": "投标文件的语言", "score_type": "veto",
        "description": LANGUAGE_REQUIREMENT_2640}, pages, "synthetic", lambda: object(), reviewer=review)
    assert outcome.verdict == verdict and outcome.basis == "evidence"
    assert calls == [(0, False)] and outcome.attempts == 1


@pytest.mark.parametrize("description", [
    "除签名、盖章、专用名称等特殊情形外，均须使用中文书写。",
    "除了签字和盖章以外，其他文字须以中文书写。",
    "除签章、专业术语等情况外，投标文件均使用汉语。",
    "除签名 / 盖章 / 商标外，均须以中文编制。",
])
def test_language_exception_enumeration_does_not_require_visual_review(description):
    assert not scoring._is_visual_requirement({"name": "语言", "description": description})


@pytest.mark.parametrize("criterion", [
    {"name": "语言与签章要求", "description": LANGUAGE_REQUIREMENT_2640},
    {"name": "语言", "description": LANGUAGE_REQUIREMENT_2640 + "投标函须加盖单位印章。"},
    {"name": "语言", "description": "除签名、盖章外，均用中文书写，投标函仍须盖章。"},
    {"name": "语言", "description": LANGUAGE_REQUIREMENT_2640,
     "required_evidence": "提供负责人签字并加盖单位印章的投标函。"},
    {"name": "签章", "description": "除签名、盖章应真实有效外，正文须使用中文。"},
    {"name": "中文文件", "description": "除报价表无需签字外，其余文件须以中文书写。"},
    {"name": "签章完整性", "description": "除营业执照外，投标函须签字盖章。"},
    {"name": "拟投入设备", "description": "提供仓库备件和维保车辆照片。"},
    {"name": "语言及仓库证明", "description": LANGUAGE_REQUIREMENT_2640 + "提供仓库照片。"},
])
def test_language_exception_preserves_actual_visual_obligations(criterion):
    assert scoring._is_visual_requirement(criterion)


def test_unexpected_model_transport_failure_automatically_retries(monkeypatch):
    from app.engine import auto_review

    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "2")
    monkeypatch.setattr(scoring, "_supplement_pages", lambda c, p, **kwargs: p)
    pages = [{"document_id": 1, "page_no": 1, "text": "投标文件所附证件清晰有效，符合本项目明确的资格条件。", "source": "text"}]
    rounds = []

    def review(*args, **kwargs):
        rounds.append(kwargs["round_no"])
        if len(rounds) < 3:
            raise TimeoutError("model timeout")
        return AutoReviewDecision(verdict="pass", reasoning="已核实资格证明",
                                  evidence=[{"document_id": 1, "page_no": 1, "quote": pages[0]["text"]}])

    monkeypatch.setattr(auto_review, "review_criterion", review)
    outcome = scoring._assess_cell({"name": "资格条件", "score_type": "veto"}, pages, "synthetic", lambda: object())
    assert outcome.attempts == 3 and rounds == [0, 1, 2]
    assert len(outcome.raw["attempt_history"]) == 3


def test_price_reextraction_ignores_unconfirmed_heuristic_and_saves_sources(managed_run, monkeypatch):
    with session_scope() as s:
        run = s.get(EvaluationRun, managed_run["id"])
        manifest = dict(run.config_json["automation_manifest"])
    manifest["bidders"] = [{"id": managed_run["bidder"], "bid_price": "50000", "price_status": "extracted"}]
    pages, data_class = scoring._load_pages(managed_run["bidder"])
    calls = []

    def assessed(*args):
        calls.append(1)
        return scoring.CellOutcome("pass", "已核对开标总价，保证金不是总价", evidence=[{
            "document_id": managed_run["document"], "page_no": 1, "kind": "text", "quote": managed_run["text"],
        }], pages=pages, basis="evidence", raw={"facts": {"bid_price": "1200000"}})

    monkeypatch.setattr(scoring, "_assess_cell", assessed)
    mapping = {managed_run["bidder"]: (pages, data_class)}
    assert scoring._resolve_prices(managed_run["id"], manifest, mapping, lambda: None) == {
        managed_run["bidder"]: Decimal("1200000")}
    scoring._resolve_prices(managed_run["id"], manifest, mapping, lambda: None)
    assert calls == [1], "a verified quote must not be re-extracted on resume"
    with session_scope() as s:
        cfg = s.get(EvaluationRun, managed_run["id"]).config_json
        assert cfg["resolved_price_evidence"][str(managed_run["bidder"])]["evidence"]


def test_scanned_heading_is_not_complete_coverage(managed_run):
    with session_scope() as s:
        doc = s.get(Document, managed_run["document"])
        doc.parse_status = "degraded"
        doc.warnings_json = ["第 1 页疑似扫描件，已超出 OCR 页数上限，跳过"]
        page = s.execute(select(DocumentPage)).scalar_one()
        page.text = "附件 证书 第1页"
    pages, _ = scoring._load_pages(managed_run["bidder"])
    assert pages[0]["unreadable"] and pages[0]["requires_vision"]


def test_blank_separator_is_known_readable(managed_run):
    with session_scope() as s:
        page = s.execute(select(DocumentPage)).scalar_one()
        page.text = ""
        page.source = "skipped"
        page.has_text_layer = False
    pages, _ = scoring._load_pages(managed_run["bidder"])
    assert pages[0]["known_blank"] and not pages[0]["unreadable"]


def test_unknown_case_type_cannot_receive_provisional_points():
    criterion = {"score_type": "count", "name": "项目负责人运维案例", "max_score": "4", "params": {
        "person_scope": "leader", "field": "performance", "per_item": "1",
    }}
    decision = AutoReviewDecision(verdict="pass", reasoning="合同存在但尚未确认类型", coverage_complete=True,
                                  facts={"verified_cases": [{"name": "平台项目", "role": "leader", "contract": True,
                                                              "invoice": True, "acceptance": True,
                                                              "social_security": True, "name_reflected": True,
                                                              "work_proof": True, "experience_proof": True}]})
    with pytest.raises(LLMError, match="项目类型"):
        scoring._verified_count(criterion, decision, [])


def test_missing_verified_scalar_cannot_fall_back_to_text_heuristic():
    criterion = {"score_type": "count", "name": "响应时间", "max_score": "4", "params": {
        "engine": "value_tier", "field": "response_hours", "tiers": [{"max_value": "4", "score": "4"}],
    }}
    decision = AutoReviewDecision(verdict="pass", reasoning="尚未核实承诺值", coverage_complete=True)
    with pytest.raises(LLMError, match="no_facts"):
        scoring._verified_count(criterion, decision, [{"text": "响应时间为4小时，模板供填写参考。"}])


def test_incomplete_count_rule_cannot_generate_default_zero():
    decision = AutoReviewDecision(verdict="pass", reasoning="证书已核实", coverage_complete=True,
                                  facts={"certificates": ["ISO9001"], "cert_backing": {"ISO9001": True}})
    with pytest.raises(LLMError, match="每项分值"):
        scoring._verified_count({"score_type": "count", "max_score": "3", "params": {"field": "certificates"}},
                                decision, [])


def test_performance_count_tier_applies_required_material_gate():
    criterion = {"score_type": "count", "name": "企业业绩", "max_score": "4", "required_evidence": "合同、发票、验收证明",
                 "params": {"engine": "count_tier", "field": "performance", "tiers": [{"min_count": 1, "score": "4"}]}}
    decision = AutoReviewDecision(verdict="pass", reasoning="合同已提供，确认未提供发票", coverage_complete=True,
                                  facts={"performance_amounts": ["1000000"], "verified_cases": [{
                                      "name": "平台维护", "amount_yuan": "1000000", "contract": True,
                                      "invoice": False, "acceptance": True,
                                  }]})
    score, detail, _ = scoring._verified_count(criterion, decision, [])
    assert score == 0 and "缺证明材料" in detail


def test_cmmi_with_confirmed_missing_backing_does_not_score():
    decision = AutoReviewDecision(verdict="fail", reasoning="确认没有认证证明材料", coverage_complete=True,
                                  facts={"certificates": ["CMMI"], "cert_backing": {"CMMI": False}})
    score, _, _ = scoring._verified_count({"score_type": "count", "max_score": "1", "params": {
        "field": "cmmi", "per_item": "1",
    }}, decision, [])
    assert score == 0


def test_certificate_query_screenshot_must_be_explicitly_reviewed():
    decision = AutoReviewDecision(verdict="pass", reasoning="证书存在，查询截图未核验", coverage_complete=True,
                                  facts={"certificates": ["ISO9001"], "cert_backing": {"ISO9001": True}})
    with pytest.raises(LLMError, match="截图"):
        scoring._verified_count({"score_type": "count", "max_score": "1", "required_evidence": "提供认证查询截图",
                                "params": {"field": "certificates", "per_item": "1"}}, decision, [])


def test_duplicate_person_cannot_inflate_attribute_score():
    decision = AutoReviewDecision(verdict="pass", reasoning="同一人员在简历及汇总表重复出现", coverage_complete=True,
                                  facts={"person_attributes": [{"name": "张三", "years": "10"},
                                                               {"name": "张 三", "years": "10"}]})
    with pytest.raises(LLMError, match="人员清单存在重复"):
        scoring._verified_count({"score_type": "count", "max_score": "2", "params": {
            "engine": "attribute_tier", "attr": "years", "tiers": [{"min": "5", "score": "1"}],
        }}, decision, [])


def test_unfinished_review_restarts_with_fresh_semantic_cache_context(monkeypatch):
    from app.engine import auto_review

    contexts = []
    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "0")

    def failed(*args, **kwargs):
        contexts.append(kwargs["previous"]["execution_attempt"])
        raise LLMError("引用未找到")

    monkeypatch.setattr(auto_review, "review_criterion", failed)
    for _ in range(2):
        with pytest.raises(LLMError) as caught:
            scoring._assess_cell({"name": "资格", "score_type": "veto"}, [], "synthetic", lambda: object())
        assert len(caught.value.attempt_history) == 1
    assert contexts[0] != contexts[1], "resume must not replay the same semantically invalid cached response"


def test_dense_mixed_page_is_sent_to_ocr_before_absence_can_be_reviewed(monkeypatch):
    from app.parsing import pdf_pymupdf

    pages = [{"document_id": 1, "page_no": 1, "stored_path": "mixed.pdf", "sha256": "source",
              "text": "正文文字层。" * 2000, "source": "text", "has_text_layer": True,
              "unreadable": True, "requires_vision": True}]
    calls = []

    def extract(path, page_numbers, **kwargs):
        calls.append((path, page_numbers))
        return {1: "原页扫描图中的实际证书内容及认证信息。" * 30}

    monkeypatch.setattr(pdf_pymupdf, "ocr_pages_text", extract)
    supplemented = scoring._supplement_pages({"name": "认证证书"}, pages, visual=False)
    assert calls == [("mixed.pdf", [1])]
    assert supplemented[0]["supplemented"] and not supplemented[0]["unreadable"]
    assert pages[0]["unreadable"], "reviewing one criterion must not mutate another cell's source pages"


def test_textless_image_keeps_visual_obligation_even_with_other_ocr_text(monkeypatch):
    from app.parsing import pdf_pymupdf

    pages = [{"document_id": 1, "page_no": number, "stored_path": "mixed.pdf", "sha256": "source",
              "text": "原有正文。", "source": "text", "has_text_layer": True,
              "unreadable": True, "requires_vision": True} for number in (1, 2)]

    def extract(path, page_numbers, *, on_textless, **kwargs):
        on_textless({"page_no": 1, "image_index": 2, "image_count": 2,
                     "image_sha256": "photograph", "engine_attempts": [{"engine": "rapidocr", "outcome": "no_text"}]})
        return {1: "同页第一幅图中的有效文字。" * 10, 2: "第二页完整扫描件的认证信息。" * 10}

    monkeypatch.setattr(pdf_pymupdf, "ocr_pages_text", extract)
    supplemented = scoring._supplement_pages({"name": "材料核验"}, pages, visual=False)
    assert "第一幅图" in supplemented[0]["text"]
    assert supplemented[0]["unreadable"] and supplemented[0]["requires_vision"]
    assert supplemented[0]["ocr_textless_issues"][0]["image_sha256"] == "photograph"
    assert not supplemented[1]["unreadable"] and not supplemented[1]["requires_vision"]
    assert "ocr_textless_issues" not in pages[0]


def test_textless_photo_routes_only_its_original_page_to_actual_visual_review(monkeypatch):
    from types import SimpleNamespace
    from app.engine import auto_review
    from app.evidence import renderer
    from app.parsing import pdf_pymupdf

    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "2")
    pages = [{"document_id": 77, "page_no": 418, "stored_path": "original.pdf", "sha256": "frozen",
              "text": "公司仓库备品备件照片。", "source": "text", "has_text_layer": True,
              "unreadable": True, "requires_vision": True,
              "ocr_textless_issues": [{"page_no": 418, "image_index": 1, "image_sha256": "photo"}]},
             {"document_id": 77, "page_no": 419, "stored_path": "original.pdf", "sha256": "frozen",
              "text": "可逐字读取的其他材料及保障说明。" * 100, "source": "text", "has_text_layer": True}]
    rendered = []

    def render(sha, path, page_no, **kwargs):
        rendered.append(page_no)
        return SimpleNamespace(read_bytes=lambda: b"original-page-image")

    def review(gateway, criterion, current, **kwargs):
        assert kwargs["use_vision"] and current[0]["image_url"]
        assert "image_url" not in current[1]
        return AutoReviewDecision(verdict="pass", reasoning="依据仓库备品照片及保障说明评分", score=Decimal("4"),
            evidence=[{"document_id": 77, "page_no": 418, "kind": "visual", "observation": "原页为仓库备品箱照片"}])

    monkeypatch.setattr(renderer, "render_page_png", render)
    monkeypatch.setattr(pdf_pymupdf, "ocr_pages_text", lambda *a, **kw: pytest.fail("已完成空文字识别的照片不重复OCR"))
    monkeypatch.setattr(auto_review, "review_criterion", review)
    result = scoring._assess_cell({"name": "保障方案", "score_type": "subjective", "max_score": "5"},
        pages, "synthetic", lambda: pytest.fail("图像事实必须视觉核验"))
    assert result.score == Decimal("4") and result.basis == "visual" and rendered == [418]
    assert result.raw["readability_issues"][0]["page_no"] == 418


def test_ocr_technical_failure_is_not_converted_into_textless_photo(monkeypatch):
    from app.parsing import pdf_pymupdf

    def failed(*args, **kwargs):
        raise RuntimeError("OCR timeout at original page 418")

    monkeypatch.setattr(pdf_pymupdf, "ocr_pages_text", failed)
    with pytest.raises(RuntimeError, match="timeout"):
        scoring._supplement_pages({}, [{"document_id": 77, "page_no": 418, "stored_path": "original.pdf",
            "sha256": "frozen", "source": "text", "text": "正文", "unreadable": True}], visual=False)


def test_exhausted_ocr_does_not_restart_cell_supplementation(monkeypatch):
    from app.parsing.pdf_runtime import OCRProcessingError

    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "2")
    calls = []

    def failed(*args, **kwargs):
        calls.append(kwargs["visual"])
        raise OCRProcessingError("全部读取引擎已失败")

    monkeypatch.setattr(scoring, "_supplement_pages", failed)
    with pytest.raises(OCRProcessingError, match="读取引擎"):
        scoring._assess_cell({"name": "运行方案", "score_type": "subjective", "max_score": "10"},
            [{"document_id": 77, "page_no": 418, "text": "正文", "unreadable": True}],
            "synthetic", lambda: pytest.fail("读取完成前不得调模型"))
    assert calls == [False]


@pytest.mark.parametrize("error_kind", ["ocr_exhausted", "budget"])
def test_preparation_never_retries_exhausted_ocr_or_budget(managed_run, monkeypatch, error_kind):
    from app.errors import LLMBudgetExceeded
    from app.parsing.pdf_runtime import OCRProcessingError
    from app.services import execution_version

    pages, classification = scoring._load_pages(managed_run["bidder"])
    monkeypatch.setattr(scoring, "_load_pages", lambda bid: ([{**p, "unreadable": True} for p in pages], classification))
    original_config = execution_version.execution_config_value
    monkeypatch.setattr(execution_version, "execution_config_value",
        lambda name: 2 if name == "auto_review_rounds" else original_config(name))
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        if error_kind == "budget":
            raise LLMBudgetExceeded("今日调用预算耗尽")
        raise OCRProcessingError("全部读取引擎已失败")

    monkeypatch.setattr(scoring, "_supplement_pages", failed)
    monkeypatch.setattr(scoring, "_assess_cell", lambda *a, **k: pytest.fail("读取失败不得生成评审分数"))
    with pytest.raises(LLMError, match="保留"):
        scoring.execute_automatic_run(managed_run["id"])
    assert calls == [1]
    state = automation.progress(managed_run["id"])
    assert state["completed"] == 0 and state["failed"] == 1
    with session_scope() as s:
        records = list(s.execute(select(ScoreRecord).where(ScoreRecord.run_id == managed_run["id"])).scalars())
        assert records and all(record.final_score is None for record in records)


def test_mixed_image_that_ocr_cannot_read_reaches_visual_review(monkeypatch):
    from app.engine import auto_review

    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "2")
    pages = [{"document_id": 1, "page_no": 1, "text": "正文文字层。" * 2000,
              "source": "text", "has_text_layer": True, "unreadable": True, "requires_vision": True}]
    reviewed = []
    supplemented = []

    def supplement(criterion, original, *, visual, exhaustive=False):
        supplemented.append(visual)
        return [{**p, **({"image_url": "offline-image"} if visual else {})} for p in original]

    def review(gateway, criterion, original, **kwargs):
        reviewed.append(kwargs["use_vision"])
        if not kwargs["use_vision"]:
            raise LLMError("嵌入扫描图尚不可读，不能确认材料缺失")
        return AutoReviewDecision(verdict="pass", reasoning="已核验扫描图中的方案内容", score=Decimal("8"),
                                  evidence=[{"document_id": 1, "page_no": 1, "kind": "visual",
                                             "observation": "原页图像包含完整的方案图表"}])

    monkeypatch.setattr(scoring, "_supplement_pages", supplement)
    monkeypatch.setattr(auto_review, "review_criterion", review)
    outcome = scoring._assess_cell({"name": "运行方案", "score_type": "subjective", "max_score": "10"},
                                   pages, "synthetic", lambda: object())
    # Known reading failures go directly to supplementation, before a wasted
    # initial model call; unresolved OCR then reaches actual visual review.
    assert reviewed == [False, True]
    assert supplemented == [False, True]
    assert outcome.score == Decimal("8") and outcome.basis == "visual"


def test_unread_receipt_cannot_be_finalized_as_external_record_missing(monkeypatch):
    from app.engine import auto_review

    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "0")
    monkeypatch.setattr(auto_review, "review_criterion", lambda *args, **kwargs: AutoReviewDecision(
        verdict="unverifiable", reasoning="现有材料无法证实平台递交时间", coverage_complete=False,
    ))
    with pytest.raises(LLMError, match="读取故障"):
        scoring._assess_cell({"name": "平台递交时间", "score_type": "match", "params": {"external_fact": True}},
                             [{"document_id": 1, "page_no": 1, "text": "扫描回执未识别", "unreadable": True}],
                             "synthetic", lambda: object())


def test_one_valid_case_cannot_hide_another_case_verification_failure():
    criterion = {"score_type": "count", "name": "企业业绩", "max_score": "4",
                 "params": {"field": "performance", "per_item": "1"}}
    decision = AutoReviewDecision(verdict="pass", reasoning="一个案例已核验，另一个引文定位失败", coverage_complete=True,
                                  facts={"verified_cases": [{"name": "已核验项目", "amount_yuan": "1000000"},
                                                            {"name": "待核验项目", "sanitized": "quote_not_in_bid"}]})
    with pytest.raises(LLMError, match="须补评"):
        scoring._verified_count(criterion, decision, [])


def test_price_conflict_without_frozen_resolution_never_chooses_a_default(managed_run, monkeypatch):
    from app.engine import auto_review

    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "2")
    monkeypatch.setattr(scoring, "_supplement_pages", lambda criterion, pages, **kwargs: pages)
    with session_scope() as s:
        manifest = dict(s.get(EvaluationRun, managed_run["id"]).config_json["automation_manifest"])
    manifest["bidders"] = [{"id": managed_run["bidder"], "bid_price": "100", "price_status": "conflict"}]
    pages, data_class = scoring._load_pages(managed_run["bidder"])
    calls = []

    def unresolved(gateway, criterion, material, **kwargs):
        calls.append(kwargs["round_no"])
        assert "price_rules" in criterion
        return AutoReviewDecision(verdict="fail", reasoning="大小写总价冲突，冻结细则未给修正口径",
                                  facts={"bid_price": "100"}, missing_evidence=["缺少明确冲突修正规则"])

    monkeypatch.setattr(auto_review, "review_criterion", unresolved)
    with pytest.raises(LLMError, match="冲突修正"):
        scoring._resolve_prices(managed_run["id"], manifest, {managed_run["bidder"]: (pages, data_class)},
                                lambda: object())
    assert calls == [0, 1, 2]
    with session_scope() as s:
        cfg = s.get(EvaluationRun, managed_run["id"]).config_json
        assert not cfg.get("resolved_prices") and not cfg.get("resolved_price_evidence")


def test_resume_rejects_changed_page_readability_warnings(managed_run):
    from app.errors import Conflict

    with session_scope() as s:
        s.get(EvaluationRun, managed_run["id"]).status = "failed"
        s.get(Document, managed_run["document"]).warnings_json = ["第 1 页 OCR 识别失败"]
    with pytest.raises(Conflict, match="输入"):
        automation.resume_run(managed_run["id"], "tester")


def test_busy_duplicate_job_does_not_overwrite_active_run_or_job(managed_run, monkeypatch):
    from app.jobs import handlers, queue
    from app.services import scoring_service
    from app.services.run_execution_lock import RunExecutionBusy, exclusive_run

    with session_scope() as s:
        s.get(EvaluationRun, managed_run["id"]).status = "running"
        job = Job(kind="evaluate", project_id="auto-executor", status="running",
                  payload_json={"run_id": managed_run["id"]})
        s.add(job)
        s.flush()
        job_id = job.id
    monkeypatch.setattr(handlers, "get_handler", lambda kind: scoring_service.evaluate_job)
    with exclusive_run(managed_run["id"]):
        with pytest.raises(RunExecutionBusy):
            scoring.execute_automatic_run(managed_run["id"])
        queue._run_job(job_id)
    with session_scope() as s:
        assert s.get(EvaluationRun, managed_run["id"]).status == "running"
        job = s.get(Job, job_id)
        assert job.status == "running" and job.error is None and job.result_json is None


def test_delayed_job_retry_returns_completed_result_without_loading_or_reviewing(managed_run, monkeypatch):
    def reviewed(criterion, pages, data_class, gateway_factory):
        return scoring.CellOutcome(
            "pass", "已经完整核验并评分", Decimal("8") if criterion["score_type"] == "subjective" else None,
            [{"document_id": managed_run["document"], "page_no": 1, "kind": "text",
              "quote": managed_run["text"]}], pages, basis="evidence",
        )
    monkeypatch.setattr(scoring, "_assess_cell", reviewed)
    original = scoring.execute_automatic_run(managed_run["id"])
    with session_scope() as s:
        # Later project changes do not rewrite a successfully frozen run.
        s.get(Bidder, managed_run["bidder"]).bid_price = Decimal("200")
        run = s.get(EvaluationRun, managed_run["id"])
        finished_at = run.finished_at
        citations = len(list(s.execute(select(EvidenceCitation)).scalars()))
    monkeypatch.setattr(scoring, "_load_pages", lambda *args: pytest.fail("completed retry must not reload live files"))
    monkeypatch.setattr(scoring, "_assess_cell", lambda *args: pytest.fail("completed retry must not call AI"))
    assert scoring.execute_automatic_run(managed_run["id"]) == original
    with session_scope() as s:
        run = s.get(EvaluationRun, managed_run["id"])
        assert run.status == "succeeded" and run.finished_at == finished_at
        assert len(list(s.execute(select(EvidenceCitation)).scalars())) == citations
