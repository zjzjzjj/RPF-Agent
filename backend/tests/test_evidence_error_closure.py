# -*- coding: utf-8 -*-
"""Technical evidence failures never become final numeric scores."""
from decimal import Decimal

from app.db.models import ScoreRecord
from app.llm.base import LLMError
from app.engine.auto_review import _synthesis_evidence_error
from app.services.automatic_scoring_service import _write_closure_fallback
from app.services.automation_service import public_auto_review


class _Session:
    def __init__(self):
        self.added = []

    def add(self, item):
        self.added.append(item)


def test_synthesis_evidence_error_keeps_verified_facts_without_provisional_zero():
    error = LLMError("综合复评引用了未经原批次核验的证据")
    error.review_attempts_exhausted = True
    error.synthesis_evidence_rejected = True
    error.verified_subtotal = Decimal("2.00")
    error.verified_facts = [{"facts": {"verified_cases": [{"name": "已核验案例"}]}}]
    error.rejected_evidence = [{
        "reference": "b7e0", "document_id": 70, "page_no": 206,
        "kind": "text", "summary": "模型新增证据摘要", "candidates": ["b6e0"],
    }]
    row = ScoreRecord(run_id=1, bidder_id=2, criterion_id=3, rule_version=1)
    session = _Session()

    _write_closure_fallback(session, row, {"score_type": "count"}, error)

    assert row.final_score is None
    assert row.review_status == "auto_confirmed"
    meta = row.ai_raw_json["auto_review"]
    fallback = meta["fallback"]
    assert fallback["kind"] == "material_review_pending"
    assert fallback["technical_error"] is True
    assert fallback["value"] == "2.00"
    assert "provisional_zero" not in str(row.ai_raw_json)
    assert "error" not in meta
    assert row.ai_raw_json["verified_facts"] == error.verified_facts
    assert row.ai_raw_json["rejected_evidence"] == error.rejected_evidence


def test_source_ledger_match_failure_is_audited_as_technical_evidence_error():
    item = {"evidence_id": "R7", "document_id": 70, "page_no": 206,
            "kind": "text", "quote": "模型新增的引文"}
    error = _synthesis_evidence_error(
        "分组综合证据无法唯一匹配完整来源台账", item, ["b6e0", "b6e1"]
    )
    row = ScoreRecord(run_id=1, bidder_id=2, criterion_id=3, rule_version=1)
    session = _Session()

    _write_closure_fallback(session, row, {"score_type": "count"}, error)

    assert row.final_score is None
    fallback = row.ai_raw_json["auto_review"]["fallback"]
    assert fallback["kind"] == "material_review_pending"
    assert fallback["technical_error"] is True
    assert fallback["rejected_evidence"] == [{
        "reference": "R7", "document_id": 70, "page_no": 206, "kind": "text",
        "summary": "模型新增的引文", "candidates": ["b6e0", "b6e1"],
    }]
    assert "provisional_zero" not in str(row.ai_raw_json)


def test_public_technical_evidence_review_hides_internal_exception_text():
    error = _synthesis_evidence_error(
        "内部源ID R7 绑定到 d70p206 失败", {"evidence_id": "R7"}, ["b6e0"]
    )
    error.attempt_history = [{"round": 1, "state": "failed", "error": "内部源ID R7 绑定失败"}]
    row = ScoreRecord(run_id=1, bidder_id=2, criterion_id=3, rule_version=1)
    _write_closure_fallback(_Session(), row, {"score_type": "count"}, error)
    row.ai_raw_json["auto_review"]["error"] = "内部源ID R7 绑定失败"

    public = public_auto_review(row)
    serialized = str(public)
    assert "内部源ID" not in serialized
    assert "绑定失败" not in serialized
    assert public["review_note"] == "证据绑定已自动核验，已保留原批次核验记录"
    assert "error" not in public["attempt_history"][0]
