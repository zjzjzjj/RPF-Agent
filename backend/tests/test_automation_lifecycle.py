"""Managed automatic runs cannot claim completion by hiding missing cells."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, Bidder, Criterion, CriterionCheck, Document, DocumentPage, EvaluationRun, EvidenceCitation, Job, Project, RulePackage, ScoreRecord
from app.db.session import session_scope
from app.errors import Conflict, ValidationFailed
from app.services import automation_service as auto


@pytest.fixture()
def managed(app_env, monkeypatch):
    monkeypatch.setenv("BID_EVAL_LLM_MODEL", "offline-model")
    monkeypatch.delenv("BID_EVAL_VISION_MODEL", raising=False)
    monkeypatch.delenv("BID_EVAL_VISION_BASE_URL", raising=False)
    with session_scope() as s:
        s.add(Project(id="auto-test", name="自动评审恢复测试"))
        s.flush()
        pkg = RulePackage(project_id="auto-test", version=1, status="frozen", content_hash="frozen")
        s.add(pkg)
        s.flush()
        b = Bidder(project_id="auto-test", name="甲", bid_price=Decimal("100"), price_status="confirmed")
        c1 = Criterion(package_id=pkg.id, code="Q1", name="电子投标文件递交时限", category="compliance", score_type="veto", status="verified")
        c2 = Criterion(package_id=pkg.id, code="S1", name="商务分", category="commercial", score_type="count", max_score=Decimal("10"), status="verified")
        s.add_all([b, c1, c2])
        s.flush()
        run = EvaluationRun(project_id="auto-test", package_id=pkg.id, package_version=1,
                            package_content_hash="frozen", status="failed", config_json={"bidder_ids": [b.id]},
                            stages_json=["objective", "price", "subjective"])
        s.add(run)
        s.flush()
        result = {"run_id": run.id, "bidder_id": b.id, "veto_id": c1.id, "score_id": c2.id, "package_id": pkg.id}
    result["criteria"] = [
        {"id": result["veto_id"], "code": "Q1", "name": "电子投标文件递交时限", "category": "compliance", "score_type": "veto", "max_score": None},
        {"id": result["score_id"], "code": "S1", "name": "商务分", "category": "commercial", "score_type": "count", "max_score": "10"},
    ]
    result["manifest"] = auto.initialize_run(result["run_id"], result["criteria"])
    return result


def _cell(m, cid, *, score=None, verdict="pass", meta=None, warnings=None):
    with session_scope() as s:
        row = ScoreRecord(run_id=m["run_id"], bidder_id=m["bidder_id"], criterion_id=cid,
                          machine_status=verdict, review_status="auto_confirmed", final_score=score,
                          ai_reasoning="基于冻结事实完成确定性核对", warnings_json=warnings,
                          ai_raw_json={"auto_review": meta or {
                              "state": "completed", "basis": "deterministic", "facts": {"checked": True}}})
        s.add(row)
        s.flush()
        return row.id


def test_expected_matrix_counts_missing_records(managed):
    _cell(managed, managed["veto_id"], verdict="fail")
    p = auto.progress(managed["run_id"])
    assert (p["expected"], p["completed"], p["remaining"]) == (2, 1, 1)
    with pytest.raises(ValidationFailed):
        auto.finish_run(managed["run_id"])
    assert auto.progress(managed["run_id"])["resumable"] is True


def test_complete_run_and_failing_qualification_keep_bidder_ranked(managed):
    _cell(managed, managed["veto_id"], verdict="fail")
    _cell(managed, managed["score_id"], score=Decimal("7"))
    auto.finish_run(managed["run_id"])
    from app.services.ranking_service import compute_ranking
    out = compute_ranking("auto-test", managed["run_id"])
    assert out["is_final"] is True
    assert out["rows"][0]["rank"] == 1
    assert out["rows"][0]["total"] == "7.00"
    with session_scope() as s:
        assert s.get(Bidder, managed["bidder_id"]).status != "rejected"


@pytest.mark.parametrize("changed_input", ["quote", "added_document", "original_bytes", "parsed_page"])
def test_finish_revalidates_frozen_inputs_and_preserves_completed_cells(managed, app_env, changed_input):
    from app.services.ranking_service import compute_ranking
    from app.services.report_service import build_formal_report

    path = app_env / "finish-frozen-source.txt"
    path.write_text("冻结文件原文", encoding="utf-8")
    with session_scope() as s:
        doc = Document(project_id="auto-test", bidder_id=managed["bidder_id"], kind="bid",
                       original_filename=path.name, stored_path=str(path), sha256="metadata-hash",
                       page_count=1, parse_status="parsed")
        s.add(doc)
        s.flush()
        page = DocumentPage(document_id=doc.id, page_no=1, text="冻结文件原文", source="text", has_text_layer=True)
        s.add(page)
        s.flush()
        page_id = page.id
        run = s.get(EvaluationRun, managed["run_id"])
        run.config_json = {key: value for key, value in run.config_json.items() if key != "automation_manifest"}
    auto.initialize_run(managed["run_id"], managed["criteria"])
    row_ids = [_cell(managed, managed["veto_id"]), _cell(managed, managed["score_id"], score=Decimal("7"))]
    with session_scope() as s:
        s.get(EvaluationRun, managed["run_id"]).status = "running"
        if changed_input == "quote":
            s.get(Bidder, managed["bidder_id"]).bid_price = Decimal("101")
        elif changed_input == "added_document":
            extra_path = app_env / "later-upload.txt"
            extra_path.write_text("运行中新上传的材料", encoding="utf-8")
            s.add(Document(project_id="auto-test", bidder_id=managed["bidder_id"], kind="bid",
                           original_filename=extra_path.name, stored_path=str(extra_path), sha256="new-file"))
        elif changed_input == "parsed_page":
            s.get(DocumentPage, page_id).text = "重新解析后变化的页面"
    if changed_input == "original_bytes":
        path.write_text("同路径文件已替换，元数据哈希未改", encoding="utf-8")

    with pytest.raises(ValidationFailed, match="新建评分运行"):
        auto.finish_run(managed["run_id"])
    progress = auto.progress(managed["run_id"])
    assert progress["completed"] == 2
    assert progress["remaining"] == 0
    assert progress["is_final"] is False
    with session_scope() as s:
        run = s.get(EvaluationRun, managed["run_id"])
        assert run.status == "failed"
        assert "运行输入或评审配置已变化" in run.config_json["error"]
        assert {row.id for row in s.execute(select(ScoreRecord)).scalars()} == set(row_ids)
        assert s.get(ScoreRecord, row_ids[1]).final_score == Decimal("7")
    assert compute_ranking("auto-test", managed["run_id"])["rows"][0]["rank"] is None
    with pytest.raises(ValidationFailed):
        build_formal_report("auto-test", managed["run_id"])
    with pytest.raises(Conflict, match="新建评分运行"):
        auto.resume_run(managed["run_id"], "tester")


@pytest.mark.parametrize("score,warnings", [("11", None), ("7", [{"fallback_score": {"value": "7"}}])])
def test_invalid_or_fallback_score_never_completes(managed, score, warnings):
    _cell(managed, managed["veto_id"])
    _cell(managed, managed["score_id"], score=Decimal(score), warnings=warnings)
    assert auto.progress(managed["run_id"])["remaining"] == 1


def test_external_boundary_is_terminal_but_not_technical_failure(managed):
    _cell(managed, managed["veto_id"], verdict="unverifiable", meta={
        "state": "completed", "basis": "external", "external_requirement": True,
        "coverage_complete": True})
    _cell(managed, managed["score_id"], score=Decimal("5"))
    assert auto.finish_run(managed["run_id"])["is_final"] is True


@pytest.mark.parametrize("coverage", [None, False, "true"])
def test_external_boundary_requires_complete_readable_search_coverage(managed, coverage):
    _cell(managed, managed["veto_id"], verdict="unverifiable", meta={
        "state": "completed", "basis": "external", "external_requirement": True,
        "coverage_complete": coverage})
    assert auto.progress(managed["run_id"])["completed"] == 0


def test_resume_is_idempotent_and_preserves_completed_cells(managed):
    row_id = _cell(managed, managed["veto_id"])
    first = auto.resume_run(managed["run_id"], "tester")
    assert auto.resume_run(managed["run_id"], "tester") == first
    with session_scope() as s:
        assert len(list(s.execute(select(Job)).scalars())) == 1
        assert s.get(ScoreRecord, row_id) is not None
    assert auto.completed_keys(managed["run_id"]) == {(managed["bidder_id"], managed["veto_id"])}


def test_simultaneous_resume_requests_create_one_job(managed):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    def request(_):
        barrier.wait(timeout=5)
        return auto.resume_run(managed["run_id"], "tester")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert results[0] == results[1]
    with session_scope() as s:
        assert len(list(s.execute(select(Job)).scalars())) == 1


def test_ordinary_document_cannot_be_closed_as_external_boundary(managed):
    with session_scope() as s:
        run = s.get(EvaluationRun, managed["run_id"])
        cfg = dict(run.config_json)
        manifest = dict(cfg["automation_manifest"])
        criteria = [dict(c) for c in manifest["criteria"]]
        criteria[0]["name"] = "营业执照复印件"
        manifest["criteria"] = criteria
        cfg["automation_manifest"] = manifest
        run.config_json = cfg
    _cell(managed, managed["veto_id"], verdict="unverifiable", meta={
        "state": "completed", "basis": "external", "external_requirement": True,
        "coverage_complete": True})
    assert auto.progress(managed["run_id"])["completed"] == 0


def test_resume_rejects_changed_quote(managed):
    with session_scope() as s:
        s.get(Bidder, managed["bidder_id"]).bid_price = Decimal("101")
    with pytest.raises(Conflict, match="已变化"):
        auto.resume_run(managed["run_id"], "tester")


def test_failed_run_rank_is_provisional_and_formal_report_blocked(managed):
    _cell(managed, managed["veto_id"])
    from app.services.ranking_service import compute_ranking
    from app.services.report_service import build_formal_report, build_report
    ranked = compute_ranking("auto-test", managed["run_id"])
    assert ranked["provisional"] is True
    assert ranked["rows"][0]["rank"] is None
    with session_scope() as s:
        s.get(EvaluationRun, managed["run_id"]).status = "succeeded"
    assert build_report("auto-test", managed["run_id"])["status"] == "draft"
    with pytest.raises(ValidationFailed):
        build_formal_report("auto-test", managed["run_id"])


def test_failed_run_with_complete_cells_still_has_no_published_rank(managed):
    _cell(managed, managed["veto_id"], verdict="fail")
    _cell(managed, managed["score_id"], score=Decimal("7"))
    from app.services.report_service import build_formal_report, build_report
    from app.services.score_summary_service import build_score_summary

    report = build_report("auto-test", managed["run_id"])
    assert report["status"] == "draft"
    assert report["ranking"]["is_final"] is False
    assert report["ranking"]["rows"][0]["rank"] is None
    summary = build_score_summary("auto-test", managed["run_id"])
    assert summary["run"]["ready"] is False
    assert summary["bidders"][0]["rank"] is None
    with pytest.raises(ValidationFailed):
        build_formal_report("auto-test", managed["run_id"])


def test_missing_vision_can_bind_once_without_replacing_completed_text_cell(managed, monkeypatch, app_env):
    document_path = app_env / "reviewed-source.txt"
    document_path.write_text("已完成评审的有效文本引文", encoding="utf-8")
    with session_scope() as s:
        document = Document(project_id="auto-test", bidder_id=managed["bidder_id"], kind="bid",
                            original_filename=document_path.name, stored_path=str(document_path), sha256="frozen-source")
        s.add(document)
        s.flush()
        document_id = document.id
        run = s.get(EvaluationRun, managed["run_id"])
        run.config_json = {key: value for key, value in run.config_json.items() if key != "automation_manifest"}
    auto.initialize_run(managed["run_id"], managed["criteria"])
    completed_id = _cell(managed, managed["score_id"], score=Decimal("7"),
                         meta={"state": "completed", "basis": "evidence"})
    with session_scope() as s:
        s.add(EvidenceCitation(score_record_id=completed_id, criterion_id=managed["score_id"],
                               document_id=document_id, page_no=1, quote="已完成评审的有效文本引文", verified=True))
    monkeypatch.setenv("BID_EVAL_VISION_MODEL", "vision-a")
    monkeypatch.setenv("BID_EVAL_VISION_BASE_URL", "https://vision.example/v1")

    _, job_id = auto.resume_run(managed["run_id"], "configuration-repair")
    with session_scope() as s:
        run = s.get(EvaluationRun, managed["run_id"])
        frozen = run.config_json["automation_manifest"]["scoring_config"]
        assert frozen["vision_model"] == "vision-a"
        assert frozen["vision_base_url"] == "https://vision.example/v1"
        row = s.get(ScoreRecord, completed_id)
        assert row.final_score == Decimal("7")
        logs = s.execute(select(AuditLog).where(AuditLog.action == "run.bind_unused_model")).scalars().all()
        assert len(logs) == 1
        assert logs[0].actor == "configuration-repair"
        assert logs[0].detail_json["vision_model"] == "vision-a"
        s.get(Job, job_id).status = "failed"
        run.status = "failed"
    monkeypatch.setenv("BID_EVAL_VISION_MODEL", "vision-b")
    with pytest.raises(Conflict):
        auto.resume_run(managed["run_id"], "tester")
    assert auto.completed_keys(managed["run_id"]) == {(managed["bidder_id"], managed["score_id"])}


def test_completed_visual_result_prevents_missing_model_binding(managed, monkeypatch):
    _cell(managed, managed["veto_id"], meta={
        "state": "completed", "basis": "visual", "visual_evidence": [
            {"document_id": 1, "page_no": 1, "observation": "原页可见完整签章"},
        ],
    })
    monkeypatch.setenv("BID_EVAL_VISION_MODEL", "vision-a")
    with pytest.raises(Conflict):
        auto.resume_run(managed["run_id"], "tester")
    with session_scope() as s:
        assert s.execute(select(AuditLog).where(AuditLog.action == "run.bind_unused_model")).first() is None


def test_credentials_can_be_repaired_without_changing_model_identity(managed, monkeypatch):
    monkeypatch.setenv("BID_EVAL_LLM_API_KEY", "replacement-key")
    monkeypatch.setenv("BID_EVAL_VISION_API_KEY", "replacement-vision-key")
    assert auto.resume_run(managed["run_id"], "tester")[0] == managed["run_id"]


def test_missing_text_provider_can_bind_with_unused_capability(managed, monkeypatch):
    _cell(managed, managed["veto_id"])
    monkeypatch.setenv("BID_EVAL_LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("BID_EVAL_LLM_BASE_URL", "https://llm.example/v1")
    auto.resume_run(managed["run_id"], "configuration-repair")
    with session_scope() as s:
        run = s.get(EvaluationRun, managed["run_id"])
        manifest = run.config_json["automation_manifest"]
        assert manifest["provider"] == "openai_compat"
        assert manifest["scoring_config"]["llm_base_url"] == "https://llm.example/v1"
        assert s.execute(select(AuditLog).where(AuditLog.action == "run.bind_unused_model")).scalar_one().actor == "configuration-repair"


def test_managed_cells_reject_manual_changes_in_every_run_state(managed):
    from app.api.scores import EvidenceBody, ReviewBody, add_evidence, relocate_evidence, review_score

    row_id = _cell(managed, managed["score_id"], score=Decimal("7"))
    with session_scope() as s:
        citation = EvidenceCitation(score_record_id=row_id, criterion_id=managed["score_id"], quote="原始引文")
        s.add(citation)
        s.flush()
        citation_id = citation.id
    for status in ("running", "failed", "succeeded"):
        with session_scope() as s:
            s.get(EvaluationRun, managed["run_id"]).status = status
        for action in ("confirm", "override", "reject_ai", "flag_conflict", "confirm_reject"):
            with pytest.raises(Conflict, match="只读"):
                review_score(row_id, ReviewBody(action=action, score="1", reason="尝试改写"), "human")
        with pytest.raises(Conflict, match="只读"):
            add_evidence(EvidenceBody(document_id=1, quote="人工追加", score_record_id=row_id), "human")
        with pytest.raises(Conflict, match="只读"):
            relocate_evidence(citation_id)
    with session_scope() as s:
        assert s.get(ScoreRecord, row_id).final_score == Decimal("7")
        assert s.get(ScoreRecord, row_id).review_status == "auto_confirmed"
        assert s.get(EvidenceCitation, citation_id).quote == "原始引文"


def test_managed_reveal_is_read_only_and_technical_failure_is_not_human_queue(managed):
    from app.api.runs import run_stats
    from app.api.scores import RevealBody, reveal_score, review_queue

    row_id = _cell(managed, managed["score_id"], verdict="execution_failed", meta={"state": "failed"})
    with session_scope() as s:
        s.get(ScoreRecord, row_id).review_status = "pending"
    reveal_score(row_id, RevealBody(), "reader")
    with session_scope() as s:
        assert s.get(ScoreRecord, row_id).ai_revealed_at is None
    stats = run_stats(managed["run_id"])
    assert stats["need_human"] == 0
    assert stats["automation"]["failed"] == 1
    assert stats["automation"]["remaining"] == 2
    queue = review_queue("auto-test", managed["run_id"])
    assert queue["items"] == []
    assert queue["automation"]["failed"] == 1


def test_formal_export_is_blocked_until_automatic_completion(managed):
    from app.api.reports import export_report

    for fmt in ("formal_html", "docx"):
        with pytest.raises(ValidationFailed):
            export_report("auto-test", fmt, managed["run_id"])
    draft = export_report("auto-test", "html", managed["run_id"])
    assert draft["url"].endswith(f"?run_id={managed['run_id']}")


def test_automatic_review_api_exposes_page_observations_without_image_payloads(managed):
    import json
    from app.api.scores import get_score
    from app.services.score_summary_service import build_score_summary

    image_payload = "data:image/png;base64," + "A" * 600
    row_id = _cell(managed, managed["score_id"], score=Decimal("7"), meta={
        "state": "completed", "basis": "visual", "attempts": 3,
        "reasoning": "已按原始扫描页核验", "confirmed_absent": False, "coverage_complete": True,
        "visual_evidence": [{"document_id": 1, "page_no": 2, "observation": "可见有效盖章",
                             "image_sha256": "hash", "image_url": image_payload}],
        "calculation": {"formula": "8-1=7", "image_url": image_payload, "base64": "A" * 600},
    })
    with session_scope() as s:
        row = s.get(ScoreRecord, row_id)
        row.ai_raw_json = {**row.ai_raw_json, "attempt_history": [
            {"round": 0, "state": "failed", "error": "需补读原页", "image_url": image_payload},
            {"round": 2, "state": "completed"},
        ]}
    detail = get_score(row_id)
    summary = build_score_summary("auto-test", managed["run_id"])
    item = next(item for bucket in summary["bidders"][0]["buckets"] for item in bucket["items"]
                if item["score_record_id"] == row_id)
    for metadata in (detail.auto_review, item["auto_review"]):
        assert metadata["visual_evidence"][0]["page_no"] == 2
        assert metadata["visual_evidence"][0]["observation"] == "可见有效盖章"
        assert metadata["attempt_history"][0]["error"] == "需补读原页"
        assert metadata["calculation"] == {"formula": "8-1=7"}
        assert "base64" not in json.dumps(metadata)
        assert "image_url" not in json.dumps(metadata)


def test_not_applicable_and_unknown_qualification_status_cannot_hide_unfinished_cells(managed):
    row_id = _cell(managed, managed["veto_id"])
    with session_scope() as s:
        s.get(ScoreRecord, row_id).review_status = "not_applicable"
    assert auto.progress(managed["run_id"])["completed"] == 0
    with session_scope() as s:
        row = s.get(ScoreRecord, row_id)
        row.review_status = "auto_confirmed"
        row.machine_status = "recommend_reject"
    assert auto.progress(managed["run_id"])["completed"] == 0
    score_id = _cell(managed, managed["score_id"])
    with session_scope() as s:
        s.get(ScoreRecord, score_id).review_status = "not_applicable"
    assert auto.progress(managed["run_id"])["remaining"] == 2


def test_empty_matrix_includes_all_frozen_bidders_and_criteria(managed):
    from app.api.runs import run_matrix

    matrix = run_matrix(managed["run_id"])
    assert [b["bidder_id"] for b in matrix["bidders"]] == [managed["bidder_id"]]
    assert {c["criterion_id"] for c in matrix["criteria"]} == {managed["veto_id"], managed["score_id"]}
    assert matrix["cells"] == {}
    assert matrix["automation"]["remaining"] == 2


def test_ranking_uses_frozen_category_and_in_total_policy(managed):
    from app.services.ranking_service import compute_ranking

    _cell(managed, managed["veto_id"])
    _cell(managed, managed["score_id"], score=Decimal("7"))
    with session_scope() as s:
        live = s.get(Criterion, managed["score_id"])
        live.category = "price"
        live.code = "CHANGED"
        s.add(CriterionCheck(criterion_id=live.id, check_type="count_at_least", params_json={"in_total": False}))
    row = compute_ranking("auto-test", managed["run_id"])["rows"][0]
    assert row["total"] == "7.00"
    assert row["commercial_total"] == "7.00"
    assert row["price_score"] is None
    assert row["breakdown"][1]["code"] == "S1"
    with session_scope() as s:
        run = s.get(EvaluationRun, managed["run_id"])
        cfg = dict(run.config_json)
        manifest = dict(cfg["automation_manifest"])
        frozen_criteria = [dict(c) for c in manifest["criteria"]]
        frozen_criteria[1]["params"] = {"in_total": False}
        manifest["criteria"] = frozen_criteria
        cfg["automation_manifest"] = manifest
        run.config_json = cfg
        check = s.execute(select(CriterionCheck).where(CriterionCheck.criterion_id == managed["score_id"])).scalar_one()
        check.params_json = {"in_total": True}
    assert compute_ranking("auto-test", managed["run_id"])["rows"][0]["total"] == "0.00"


def test_late_resume_retry_returns_receipt_after_success(managed):
    _cell(managed, managed["veto_id"])
    receipt = auto.resume_run(managed["run_id"], "tester")
    _cell(managed, managed["score_id"], score=Decimal("7"))
    auto.finish_run(managed["run_id"])
    with session_scope() as s:
        s.get(Job, receipt[1]).status = "succeeded"
    assert auto.resume_run(managed["run_id"], "tester") == receipt
    with session_scope() as s:
        assert len(s.execute(select(Job)).scalars().all()) == 1


def test_initially_successful_run_cannot_be_resumed(managed):
    _cell(managed, managed["veto_id"])
    _cell(managed, managed["score_id"], score=Decimal("7"))
    auto.finish_run(managed["run_id"])
    with pytest.raises(Conflict):
        auto.resume_run(managed["run_id"], "tester")


def test_resume_checks_actual_file_bytes(managed, app_env):
    path = app_env / "uploaded.pdf"
    path.write_bytes(b"original")
    with session_scope() as s:
        s.add(Document(project_id="auto-test", bidder_id=managed["bidder_id"], kind="bid",
                       original_filename="uploaded.pdf", stored_path=str(path), sha256="metadata"))
        run = s.get(EvaluationRun, managed["run_id"])
        cfg = dict(run.config_json)
        cfg.pop("automation_manifest")
        run.config_json = cfg
    auto.initialize_run(managed["run_id"], managed["criteria"])
    path.write_bytes(b"changed")
    with pytest.raises(Conflict):
        auto.resume_run(managed["run_id"], "tester")


@pytest.mark.parametrize("verdict,label", [("fail", "不符合"), ("unverifiable", "现有材料无法证实")])
def test_managed_reports_describe_actual_automatic_methods(managed, verdict, label):
    from io import BytesIO

    from docx import Document as DocxDocument

    from app.services.report_service import (
        build_formal_report, build_report, render_docx, render_formal_html, render_html,
    )

    _cell(managed, managed["veto_id"], verdict=verdict, meta={
        "state": "completed", "basis": "external", "external_requirement": True,
        "coverage_complete": True,
    } if verdict == "unverifiable" else None)
    _cell(managed, managed["score_id"], score=Decimal("7"))
    # Exercise the subjective appendix without inventing committee records.
    with session_scope() as s:
        s.get(Criterion, managed["score_id"]).score_type = "subjective"
        run = s.get(EvaluationRun, managed["run_id"])
        cfg = dict(run.config_json)
        manifest = dict(cfg["automation_manifest"])
        criteria = [dict(c) for c in manifest["criteria"]]
        criteria[1]["score_type"] = "subjective"
        manifest["criteria"] = criteria
        cfg["automation_manifest"] = manifest
        run.config_json = cfg
    auto.finish_run(managed["run_id"])

    report = build_formal_report("auto-test", managed["run_id"])
    ordinary = build_report("auto-test", managed["run_id"])
    assert report["summary_rows"][0]["rank"] == 1
    assert report["summary_rows"][0]["price_no_tax"] is None
    assert report["price_rows"][0]["price_no_tax"] is None
    assert report["tax_rate"] is None
    assert report["compliance_table"]["rows"][0]["cells"][0]["verdict"] == label
    assert report["review_notes"][0]["items"][0]["verdict"] == label
    assert report["judge_labels"] == []
    assert report["ai_detail"][0]["items"][0]["judges"] == []
    assert report["ai_detail"][0]["items"][0]["method"] == "AI 按细则评分"
    doc = DocxDocument(BytesIO(render_docx(report)))
    doc_text = "\n".join([p.text for p in doc.paragraphs] + [
        cell.text for table in doc.tables for row in table.rows for cell in row.cells
    ])
    for output in (render_formal_html(report), render_html(ordinary), doc_text):
        assert label in output
        assert "冻结细则核验资格与符合性事实" in output
        assert "定向检索、补充 OCR、视觉核验与 AI 复评" in output
        assert "不符合的投标人仍参与评分和排名" in output
        assert "去最高最低" not in output
        assert "去除一个最高分" not in output
        assert "最终得分(去尾)" not in output
        assert "AI 评委矩阵" not in output
        assert "证据簇分层评审" not in output
        assert "须经评标委员会确认" not in output
        assert "有否决建议" not in output
    assert "评委分：" not in doc_text
    assert "未提供可核验税率，不推算不含税报价" in doc_text
    assert "1.06" not in doc_text


def test_managed_draft_reports_missing_matrix_cells_as_unfinished(managed):
    from app.services.report_service import build_report, render_html

    report = build_report("auto-test", managed["run_id"])
    assert report["status"] == "draft"
    assert report["progress"]["open"] == 2
    assert report["progress"]["criteria_open"] == 2
    assert len(report["open_questions"]) == 2
    assert all(q["score_id"] is None for q in report["open_questions"])
    output = render_html(report)
    assert "尚有 2 项未完成" in output
    assert "全部条款已完成自动评审" not in output
    assert "全部应评条款已形成有效终态" not in output
    assert "可批量裁决" not in output
    assert "待人工处置" not in output


def test_managed_report_preserves_confirmed_absence_reasoning(managed):
    from app.services.report_service import build_report, render_html

    _cell(managed, managed["veto_id"])
    row_id = _cell(managed, managed["score_id"], score=Decimal("0"), meta={
        "state": "completed", "basis": "absence", "coverage_complete": True,
        "confirmed_absent": True, "reasoning": "全部关联文件检索后确认未提交所需证书，按规则扣分",
    })
    with session_scope() as s:
        s.get(ScoreRecord, row_id).ai_reasoning = None
    auto.finish_run(managed["run_id"])
    output = render_html(build_report("auto-test", managed["run_id"]))
    assert "确认所需材料缺失，按细则形成结论" in output
    assert "全部关联文件检索后确认未提交所需证书" in output
    assert "确定性计算或已留存的页图证据完成评审" not in output


def test_legacy_formal_report_retains_assisted_review_wording(managed):
    from app.services.report_service import build_formal_report, render_formal_html

    _cell(managed, managed["veto_id"])
    _cell(managed, managed["score_id"], score=Decimal("7"))
    with session_scope() as s:
        run = s.get(EvaluationRun, managed["run_id"])
        run.config_json = {"bidder_ids": [managed["bidder_id"]]}
        run.status = "succeeded"
    report = build_formal_report("auto-test", managed["run_id"])
    assert report["compliance_table"]["rows"][0]["cells"][0]["verdict"] == "通过"
    output = render_formal_html(report)
    assert "AI 辅助评审" in output
    assert "须经评标委员会确认后生效" in output
    assert "全自动评标系统" not in output
