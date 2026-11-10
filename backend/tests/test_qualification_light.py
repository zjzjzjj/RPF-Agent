"""资格轻量通道：确定性预检 + 每家一次快审，不 OCR、不逐条款 LLM、不阻断。"""
from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import (
    Bidder, Criterion, Document, DocumentPage, EvaluationRun, EvidenceCitation,
    Project, ReviewCollectionSet, RulePackage, ScoreRecord,
)
from app.db.session import session_scope
from app.engine import qualification_light as light
from app.services import automation_service as automation
from app.services import automatic_scoring_service as scoring


class FakeGateway:
    def __init__(self, items=None, error=None):
        self.calls = 0
        self.items = items or []
        self.error = error

    def chat_json(self, system, user, out_model, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return out_model.model_validate({"items": self.items}), None


def _page(text: str) -> dict:
    return {"document_id": 1, "page_no": 1, "text": text, "source": "text"}


def test_deterministic_pass_plus_one_quick_review_per_bidder():
    pages = [_page("投标人提供运行维护方案，服务响应及时。")]
    criteria = [
        {"code": "Q1", "name": "方案存在", "score_type": "veto",
         "check_type": "contains_phrase", "params": {"phrase": "运行维护方案"}},
        {"code": "Q2", "name": "服务承诺", "score_type": "veto",
         "check_type": "human_only", "params": {}},
    ]
    gateway = FakeGateway(items=[{"code": "Q2", "status": "pass", "reason": "文中可见响应承诺"}])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert gateway.calls == 1
    assert results["Q1"].status == "pass"
    assert results["Q2"].status == "pass"
    assert results["Q2"].quick_state == "completed"


def test_hard_deterministic_fail_is_not_overridden():
    pages = [_page("注册资本人民币10万元。")]
    criteria = [{"code": "C1", "name": "注册资本门槛", "score_type": "veto",
                 "check_type": "value_in_range",
                 "params": {"field": "registered_capital", "min": 1000000}}]
    gateway = FakeGateway(items=[{"code": "C1", "status": "pass", "reason": "看起来没问题"}])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["C1"].status == "fail"
    assert results["C1"].deterministic == "fail"


def test_quick_review_failure_never_becomes_pass():
    pages = [_page("普通材料文本。")]
    criteria = [{"code": "Q1", "name": "授权委托书", "score_type": "veto",
                 "check_type": "human_only", "params": {}}]
    gateway = FakeGateway(error=RuntimeError("provider down"))
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["Q1"].status == "not_deeply_reviewed"
    assert results["Q1"].quick_state == "failed"


def test_visual_clause_is_not_deeply_reviewed_without_vision():
    pages = [_page("投标文件须加盖投标人公章。")]
    criteria = [{"code": "S1", "name": "投标文件签章", "score_type": "veto",
                 "check_type": "human_only", "params": {}}]
    gateway = FakeGateway(items=[{"code": "S1", "status": "pass", "reason": "应当有章"}])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["S1"].status == "not_deeply_reviewed"


def _light_run(monkeypatch, app_env):
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    monkeypatch.setenv("BID_EVAL_LLM_MODEL", "offline-test")
    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "0")
    monkeypatch.setenv("BID_EVAL_LLM_CONCURRENCY", "1")
    content = "投标人提供运行维护方案，服务响应及时。"
    source = app_env / "bid-light.txt"
    source.write_text(content, encoding="utf-8")
    with session_scope() as s:
        s.add(Project(id="qual-light", name="资格轻量测试"))
        s.flush()
        package = RulePackage(project_id="qual-light", version=1, status="frozen", content_hash="frozen")
        s.add(package)
        s.flush()
        bidder = Bidder(project_id="qual-light", name="甲", bid_price=Decimal("100"), price_status="confirmed")
        criterion = Criterion(package_id=package.id, code="Q1", name="方案存在", category="compliance",
                              score_type="veto", status="verified")
        s.add_all([bidder, criterion])
        s.flush()
        doc = Document(project_id="qual-light", bidder_id=bidder.id, kind="bid", parse_status="parsed",
                       original_filename="bid.txt", stored_path=str(source),
                       sha256=hashlib.sha256(source.read_bytes()).hexdigest(), page_count=1)
        s.add(doc)
        s.flush()
        s.add(DocumentPage(document_id=doc.id, page_no=1, text=content, source="text"))
        rules = [{"id": criterion.id, "code": "Q1", "name": criterion.name, "category": "compliance",
                  "score_type": "veto", "check_type": "contains_phrase",
                  "params": {"phrase": "运行维护方案"}, "max_score": None}]
        run = EvaluationRun(project_id="qual-light", package_id=package.id, package_version=1,
                            package_content_hash="frozen", status="queued",
                            stages_json=["objective", "subjective", "price"],
                            config_json={"automation_version": 1, "bidder_ids": [bidder.id],
                                         "qualification_mode": "light",
                                         "qualification_light_test": True})
        s.add(run)
        s.flush()
        payload = {"id": run.id, "bidder": bidder.id, "criteria": rules, "text": content}
    automation.initialize_run(payload["id"], rules)
    return payload


def test_executor_uses_light_path_without_ocr_or_collection(monkeypatch, app_env):
    run = _light_run(monkeypatch, app_env)
    monkeypatch.setattr(scoring, "_supplement_pages", lambda *a, **kw: pytest.fail("light path must not OCR"))
    calls = []

    def fake_review(criteria, pages, data_class, gateway):
        calls.append(len(criteria))
        return {"Q1": light.LightResult(status="pass", reason="确定性预检通过",
                                        deterministic="pass", quick="skipped", quick_state="skipped")}

    monkeypatch.setattr(light, "review_bidder_qualification", fake_review)
    result = scoring.execute_automatic_run(run["id"])
    assert result["automation"]["complete"]
    assert calls == [1]
    with session_scope() as s:
        row = s.execute(select(ScoreRecord).where(ScoreRecord.run_id == run["id"])).scalar_one()
        assert row.machine_status == "pass"
        assert row.final_score is None and row.evidence_status == "none"
        assert row.ai_raw_json["auto_review"]["basis"] == "lightweight"
        assert s.execute(select(EvidenceCitation).where(EvidenceCitation.score_record_id == row.id)).first() is None
        collections = list(s.execute(select(ReviewCollectionSet).where(
            ReviewCollectionSet.run_id == run["id"])).scalars())
        assert all((c.plan_json or {}).get("kind") == "execution-metrics" for c in collections)


@pytest.mark.parametrize("status", ["pass", "fail", "concern", "not_deeply_reviewed"])
def test_valid_cell_accepts_lightweight_statuses(status):
    from app.db.models import ScoreRecord
    from app.services.automation_service import _valid_cell

    row = ScoreRecord(run_id=1, bidder_id=1, criterion_id=1, review_status="auto_confirmed",
                      machine_status=status, ai_reasoning="轻量预检理由",
                      ai_raw_json={"auto_review": {"state": "completed", "basis": "lightweight",
                                                   "reasoning": "轻量预检理由",
                                                   "lightweight": {"schema_version": 1,
                                                                   "quick_state": "completed"}}})
    assert _valid_cell(row, {"score_type": "veto"}, [])


def test_valid_cell_rejects_lightweight_on_scoring_item():
    from app.db.models import ScoreRecord
    from app.services.automation_service import _valid_cell

    row = ScoreRecord(run_id=1, bidder_id=1, criterion_id=1, review_status="auto_confirmed",
                      machine_status="pass", final_score=Decimal("2"), ai_reasoning="x",
                      ai_raw_json={"auto_review": {"state": "completed", "basis": "lightweight",
                                                   "lightweight": {"schema_version": 1,
                                                                   "quick_state": "completed"}}})
    assert not _valid_cell(row, {"score_type": "count", "max_score": "5"}, [])
