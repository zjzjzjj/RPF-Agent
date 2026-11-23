"""资格轻量通道：确定性预检 + 每家一次快审，不 OCR、不逐条款 LLM、不阻断。

二值化（2026-11-23 决策 0017）：每条款必给 pass/fail（通过/存疑）——
pass=未发现确定性否决点（不等于确认符合），fail=存疑/建议否决（需人工确认）。
确定性否决点只有两类：事实矛盾型预检不符（数值/日期/计数/数学关系）与
LLM 逐字反面引文本地核验命中；材料缺失、扫描件不可读、签章类事项、
快审失败、引文核验不过一律 pass（缺证据≠不符合，误否决不可接受）。
v1/v2 旧运行的四态/三态显示逐字保留（见 _valid_cell 与派生的 legacy 分支）。
"""
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
    """事实矛盾型预检不符（注册资本低于门槛）=确定性否决点，不被 LLM pass 覆盖。"""
    pages = [_page("注册资本人民币10万元。")]
    criteria = [{"code": "C1", "name": "注册资本门槛", "score_type": "veto",
                 "check_type": "value_in_range",
                 "params": {"field": "registered_capital", "min": 1000000}}]
    gateway = FakeGateway(items=[{"code": "C1", "status": "pass", "reason": "看起来没问题"}])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["C1"].status == "fail"
    assert results["C1"].deterministic == "fail"
    assert results["C1"].reject_source == "deterministic"


def test_quick_review_failure_passes_without_reject_point():
    """快审调用失败=未能核查≠不符合：落 pass（未见否决点），留痕待人工核对。"""
    pages = [_page("普通材料文本。")]
    criteria = [{"code": "Q1", "name": "授权委托书", "score_type": "veto",
                 "check_type": "human_only", "params": {}}]
    gateway = FakeGateway(error=RuntimeError("provider down"))
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["Q1"].status == "pass"
    assert results["Q1"].quick_state == "failed"
    assert results["Q1"].needs_human_check is True


def test_visual_clause_passes_with_human_check_note():
    """签章类视觉/外部事项 AI 不查验：落 pass 并标待人工核对（决策 0017 口径③）。"""
    pages = [_page("投标文件须加盖投标人公章。")]
    criteria = [{"code": "S1", "name": "投标文件签章", "score_type": "veto",
                 "check_type": "human_only", "params": {}}]
    gateway = FakeGateway(items=[{"code": "S1", "status": "pass", "reason": "应当有章"}])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["S1"].status == "pass"
    assert results["S1"].needs_human_check is True


def test_llm_reject_with_verified_quote_becomes_fail():
    pages = [_page("资质证书显示等级为二级，低于招标要求的一级资质。")]
    criteria = [{"code": "R1", "name": "资质等级", "score_type": "veto",
                 "check_type": "human_only", "params": {}}]
    gateway = FakeGateway(items=[{
        "code": "R1", "status": "reject", "reason": "资质等级低于要求",
        "evidence": [{"quote": "资质证书显示等级为二级", "document_id": 1, "page_no": 1}],
    }])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["R1"].status == "fail"
    assert results["R1"].reject_source == "llm"
    assert results["R1"].evidence_items == [
        {"quote": "资质证书显示等级为二级", "document_id": 1, "page_no": 1}]
    payload = light.cell_payload(results["R1"])
    assert payload["schema_version"] == 3
    assert payload["reject_source"] == "llm"
    assert payload["reject_quotes"] == results["R1"].evidence_items


def test_llm_reject_relocates_wrong_page_hint():
    pages = [_page("资质证书显示等级为二级，低于招标要求的一级资质。")]
    criteria = [{"code": "R1", "name": "资质等级", "score_type": "veto",
                 "check_type": "human_only", "params": {}}]
    gateway = FakeGateway(items=[{
        "code": "R1", "status": "reject", "reason": "等级不符",
        "evidence": [{"quote": "资质证书显示等级为二级", "document_id": 1, "page_no": 99}],
    }])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    # 页码不可信：以逐字命中处的真实页码为准
    assert results["R1"].status == "fail"
    assert results["R1"].evidence_items[0]["page_no"] == 1


def test_llm_reject_with_unverifiable_quote_passes_with_lead():
    """引文核验不过（可能编造）→ 不作为否决点：落 pass，线索保留在理由里。"""
    pages = [_page("资质证书显示等级为一级，满足招标要求。")]
    criteria = [{"code": "R1", "name": "资质等级", "score_type": "veto",
                 "check_type": "human_only", "params": {}}]
    gateway = FakeGateway(items=[{
        "code": "R1", "status": "reject", "reason": "编造的反面事实",
        "evidence": [{"quote": "资质证书显示等级为特级以下不予认可", "document_id": 1, "page_no": 1}],
    }])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["R1"].status == "pass"
    assert results["R1"].reject_source == ""
    assert results["R1"].evidence_items == []
    assert results["R1"].needs_human_check is True


def test_llm_reject_without_evidence_passes_with_lead():
    """无引文的 reject 绝不放行成否决点（宁漏勿错杀）。"""
    pages = [_page("普通材料文本，未见资质描述。")]
    criteria = [{"code": "R1", "name": "资质等级", "score_type": "veto",
                 "check_type": "human_only", "params": {}}]
    gateway = FakeGateway(items=[{"code": "R1", "status": "reject", "reason": "不满足"}])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["R1"].status == "pass"
    assert results["R1"].reject_source == ""


def test_llm_unknown_status_passes_without_reject_point():
    pages = [_page("普通材料文本。")]
    criteria = [{"code": "Q1", "name": "授权委托书", "score_type": "veto",
                 "check_type": "human_only", "params": {}}]
    gateway = FakeGateway(items=[{"code": "Q1", "status": "unknown", "reason": "无法判断"}])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["Q1"].status == "pass"
    # 快审存疑线索保留 → 待人工核对注记（线索在理由里）
    assert results["Q1"].needs_human_check is True


def test_excerpt_quick_pass_stays_pass(monkeypatch):
    """节选不再降级：快审 pass 即 pass（pass=未见否决点，节选只影响覆盖注记）。"""
    monkeypatch.setenv("BID_EVAL_QUALIFICATION_QUICK_CHARS", "4000")  # 配置有 4000 下限
    pages = [_page("材料内容" * 1500)]  # 6000 字符 > 4000 预算 → 节选
    criteria = [{"code": "Q1", "name": "服务承诺", "score_type": "veto",
                 "check_type": "human_only", "params": {}}]
    gateway = FakeGateway(items=[{"code": "Q1", "status": "pass", "reason": "看起来齐全"}])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["Q1"].input_complete is False
    assert results["Q1"].status == "pass"
    # 节选 pass 覆盖有限 → 归入待人工核对（误放须可见，决策 0017 注记口径）
    assert results["Q1"].needs_human_check is True


def test_absence_and_missing_material_never_reject():
    """材料/短语未见型不符（document_present/contains_phrase）=缺证据≠不符合：pass。"""
    pages = [_page("普通材料文本，没有任何证书复印件。")]
    criteria = [
        {"code": "D1", "name": "资质证书", "score_type": "veto",
         "check_type": "document_present", "params": {"document": "资质证书"}},
        {"code": "P1", "name": "承诺短语", "score_type": "veto",
         "check_type": "contains_phrase", "params": {"phrase": "完全响应招标文件"}},
    ]
    gateway = FakeGateway(items=[])
    results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
    assert results["D1"].status == "pass"
    assert results["D1"].reject_source == ""
    assert results["P1"].status == "pass"
    assert results["P1"].reject_source == ""


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
        payload = {"id": run.id, "bidder": bidder.id, "criteria": rules, "text": content,
                   "doc_id": doc.id}
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


def test_executor_persists_llm_reject_citations_and_summary(monkeypatch, app_env):
    """LLM 否决（带逐字反面引文）端到端：引文落库、evidence_status 回写、汇总建议=存疑（建议否决）。"""
    run = _light_run(monkeypatch, app_env)
    doc_id = run["doc_id"]

    def fake_review(criteria, pages, data_class, gateway):
        return {"Q1": light.LightResult(
            status="fail", reason="建议否决（仅建议，需人工确认）：快审发现明确反面事实——未提供运行维护方案",
            deterministic="unknown", quick="reject", quick_state="completed",
            reject_source="llm",
            evidence_items=[{"quote": "投标人提供运行维护方案，服务响应及时", "document_id": doc_id, "page_no": 1}])}

    monkeypatch.setattr(light, "review_bidder_qualification", fake_review)
    result = scoring.execute_automatic_run(run["id"])
    assert result["automation"]["complete"]
    with session_scope() as s:
        row = s.execute(select(ScoreRecord).where(ScoreRecord.run_id == run["id"])).scalar_one()
        assert row.machine_status == "fail"
        cit = s.execute(select(EvidenceCitation).where(
            EvidenceCitation.score_record_id == row.id)).scalar_one()
        assert cit.created_by == "llm"
        assert cit.citation_role == "veto"
        assert cit.quote == "投标人提供运行维护方案，服务响应及时"
        assert cit.document_id == doc_id and cit.page_no == 1
        assert cit.verified
        assert row.evidence_status == "verified"
        light_payload = row.ai_raw_json["auto_review"]["lightweight"]
        assert light_payload["schema_version"] == 3
        assert light_payload["reject_source"] == "llm"

    from app.services.score_summary_service import build_score_summary
    summary = build_score_summary("qual-light", run["id"])
    bidder_out = summary["bidders"][0]
    assert bidder_out["qualification_suggestion"] == {
        "code": "reject", "label": "存疑（建议否决，需人工确认）", "reject_count": 1}
    verdict_bucket = next(b for b in bidder_out["buckets"] if b["kind"] == "verdict")
    assert verdict_bucket["verdicts"]["fail"] == 1


def test_summary_projects_pass_suggestion(monkeypatch, app_env):
    run = _light_run(monkeypatch, app_env)

    def fake_review(criteria, pages, data_class, gateway):
        return {"Q1": light.LightResult(status="pass", reason="确定性预检通过",
                                        deterministic="pass", quick="skipped", quick_state="skipped")}

    monkeypatch.setattr(light, "review_bidder_qualification", fake_review)
    scoring.execute_automatic_run(run["id"])
    from app.services.score_summary_service import build_score_summary
    summary = build_score_summary("qual-light", run["id"])
    bidder_out = summary["bidders"][0]
    assert bidder_out["qualification_suggestion"] == {"code": "pass", "label": "通过"}
    verdict_bucket = next(b for b in bidder_out["buckets"] if b["kind"] == "verdict")
    assert verdict_bucket["verdicts"]["pass"] == 1
    assert verdict_bucket["verdicts"]["concern"] == 0


def test_summary_pass_suggestion_carries_manual_check_count(monkeypatch, app_env):
    """无建议否决项 → "通过"（附待人工核对注记）；needs_human_check 只作注记，绝不否决。"""
    run = _light_run(monkeypatch, app_env)

    def fake_review(criteria, pages, data_class, gateway):
        return {"Q1": light.LightResult(status="pass", reason="通过（未见确定性否决点）：签章类需人工查验",
                                        deterministic="unknown", quick="skipped", quick_state="skipped",
                                        needs_human_check=True)}

    monkeypatch.setattr(light, "review_bidder_qualification", fake_review)
    scoring.execute_automatic_run(run["id"])
    from app.services.score_summary_service import build_score_summary
    summary = build_score_summary("qual-light", run["id"])
    bidder_out = summary["bidders"][0]
    assert bidder_out["qualification_suggestion"] == {
        "code": "pass", "label": "通过", "manual_check_count": 1}


@pytest.mark.parametrize("status", ["pass", "fail", "concern", "not_deeply_reviewed"])
def test_valid_cell_accepts_lightweight_v1_statuses(status):
    """v1（旧运行）四状态照旧放行——旧数据不改写。"""
    from app.db.models import ScoreRecord
    from app.services.automation_service import _valid_cell

    row = ScoreRecord(run_id=1, bidder_id=1, criterion_id=1, review_status="auto_confirmed",
                      machine_status=status, ai_reasoning="轻量预检理由",
                      ai_raw_json={"auto_review": {"state": "completed", "basis": "lightweight",
                                                   "reasoning": "轻量预检理由",
                                                   "lightweight": {"schema_version": 1,
                                                                   "quick_state": "completed"}}})
    assert _valid_cell(row, {"score_type": "veto"}, [])


@pytest.mark.parametrize("status", ["pass", "fail", "concern"])
def test_valid_cell_accepts_lightweight_v2_tristate(status):
    from app.db.models import ScoreRecord
    from app.services.automation_service import _valid_cell

    row = ScoreRecord(run_id=1, bidder_id=1, criterion_id=1, review_status="auto_confirmed",
                      machine_status=status, ai_reasoning="轻量预检理由",
                      ai_raw_json={"auto_review": {"state": "completed", "basis": "lightweight",
                                                   "reasoning": "轻量预检理由",
                                                   "lightweight": {"schema_version": 2, "status": status,
                                                                   "quick_state": "completed"}}})
    assert _valid_cell(row, {"score_type": "veto"}, [])


def test_valid_cell_rejects_v2_llm_reject_without_quotes():
    from app.db.models import ScoreRecord
    from app.services.automation_service import _valid_cell

    row = ScoreRecord(run_id=1, bidder_id=1, criterion_id=1, review_status="auto_confirmed",
                      machine_status="fail", ai_reasoning="x",
                      ai_raw_json={"auto_review": {"state": "completed", "basis": "lightweight",
                                                   "reasoning": "x",
                                                   "lightweight": {"schema_version": 2, "status": "fail",
                                                                   "reject_source": "llm",
                                                                   "reject_quotes": [],
                                                                   "quick_state": "completed"}}})
    assert not _valid_cell(row, {"score_type": "veto"}, [])


@pytest.mark.parametrize("status", ["pass", "fail"])
def test_valid_cell_accepts_lightweight_v3_binary(status):
    """v3（二值化）：pass/fail 闭合；llm 否决必带引文规则同 v2。"""
    from app.db.models import ScoreRecord
    from app.services.automation_service import _valid_cell

    row = ScoreRecord(run_id=1, bidder_id=1, criterion_id=1, review_status="auto_confirmed",
                      machine_status=status, ai_reasoning="轻量预检理由",
                      ai_raw_json={"auto_review": {"state": "completed", "basis": "lightweight",
                                                   "reasoning": "轻量预检理由",
                                                   "lightweight": {"schema_version": 3, "status": status,
                                                                   "quick_state": "completed"}}})
    assert _valid_cell(row, {"score_type": "veto"}, [])


def test_valid_cell_rejects_v3_third_state():
    """v3 只收 pass/fail：三态残留（concern）不闭合——逼出引擎口径漂移。"""
    from app.db.models import ScoreRecord
    from app.services.automation_service import _valid_cell

    row = ScoreRecord(run_id=1, bidder_id=1, criterion_id=1, review_status="auto_confirmed",
                      machine_status="concern", ai_reasoning="x",
                      ai_raw_json={"auto_review": {"state": "completed", "basis": "lightweight",
                                                   "reasoning": "x",
                                                   "lightweight": {"schema_version": 3, "status": "concern",
                                                                   "quick_state": "completed"}}})
    assert not _valid_cell(row, {"score_type": "veto"}, [])


def test_valid_cell_rejects_v3_llm_reject_without_quotes():
    from app.db.models import ScoreRecord
    from app.services.automation_service import _valid_cell

    row = ScoreRecord(run_id=1, bidder_id=1, criterion_id=1, review_status="auto_confirmed",
                      machine_status="fail", ai_reasoning="x",
                      ai_raw_json={"auto_review": {"state": "completed", "basis": "lightweight",
                                                   "reasoning": "x",
                                                   "lightweight": {"schema_version": 3, "status": "fail",
                                                                   "reject_source": "llm",
                                                                   "reject_quotes": [],
                                                                   "quick_state": "completed"}}})
    assert not _valid_cell(row, {"score_type": "veto"}, [])


def test_valid_cell_rejects_lightweight_on_scoring_item():
    from app.db.models import ScoreRecord
    from app.services.automation_service import _valid_cell

    row = ScoreRecord(run_id=1, bidder_id=1, criterion_id=1, review_status="auto_confirmed",
                      machine_status="pass", final_score=Decimal("2"), ai_reasoning="x",
                      ai_raw_json={"auto_review": {"state": "completed", "basis": "lightweight",
                                                   "lightweight": {"schema_version": 1,
                                                                   "quick_state": "completed"}}})
    assert not _valid_cell(row, {"score_type": "count", "max_score": "5"}, [])


# ---------------------------------------------------------------------------
# 汇总投影（每家资格建议，纯派生）
# ---------------------------------------------------------------------------
def test_derive_qualification_suggestion_cases():
    """v1/v2 旧运行的三态投影逐字不变（无 lightweight_schema_version 字段）。"""
    from app.services.score_summary_service import derive_qualification_suggestion

    def items(*statuses, lightweight=True, score_type="veto"):
        return [{"lightweight": lightweight, "machine_status": s, "score_type": score_type}
                for s in statuses]

    assert derive_qualification_suggestion(items("fail", "pass")) == {"code": "reject", "label": "建议否决"}
    assert derive_qualification_suggestion(items("concern", "pass")) == {"code": "concern", "label": "存疑"}
    assert derive_qualification_suggestion(items("pass", "pass")) == {"code": "pass", "label": "通过"}
    # legacy 未深评 / 故障一律归存疑
    assert derive_qualification_suggestion(items("not_deeply_reviewed")) == {"code": "concern", "label": "存疑"}
    assert derive_qualification_suggestion(items("execution_failed")) == {"code": "concern", "label": "存疑"}
    # 混跑：深评 fail 留在轻量运行里也算建议否决；故障兜底/未评残留绝不放行"通过"
    mixed = items("pass") + [
        {"lightweight": False, "machine_status": "fail", "score_type": "match"}]
    assert derive_qualification_suggestion(mixed) == {"code": "reject", "label": "建议否决"}
    fallback = items("pass") + [
        {"lightweight": False, "machine_status": "concern", "score_type": "veto"}]
    assert derive_qualification_suggestion(fallback) == {"code": "concern", "label": "存疑"}
    not_run = items("pass") + [
        {"lightweight": False, "machine_status": None, "score_type": "veto"}]
    assert derive_qualification_suggestion(not_run) == {"code": "concern", "label": "存疑"}
    # 计分条目不进资格建议范围
    scoring = items("pass") + [
        {"lightweight": False, "machine_status": "fail", "score_type": "count"}]
    assert derive_qualification_suggestion(scoring) == {"code": "pass", "label": "通过"}
    # full 模式（无轻量条目）不产建议
    assert derive_qualification_suggestion(
        [{"lightweight": False, "machine_status": "fail", "score_type": "veto"}]) is None
    assert derive_qualification_suggestion([]) is None


def test_derive_qualification_suggestion_v3_binary():
    """v3 二值化投影：只有确定性否决点（fail）→存疑（建议否决）；否则通过（附待核对注记）。"""
    from app.services.score_summary_service import derive_qualification_suggestion

    def items(*pairs, score_type="veto"):
        # pairs: (machine_status, needs_human_check)
        return [{"lightweight": True, "lightweight_schema_version": 3,
                 "machine_status": st, "needs_human_check": nhc, "score_type": score_type}
                for st, nhc in pairs]

    assert derive_qualification_suggestion(items(("fail", False), ("pass", False))) == {
        "code": "reject", "label": "存疑（建议否决，需人工确认）", "reject_count": 1}
    assert derive_qualification_suggestion(items(("pass", False), ("pass", False))) == {
        "code": "pass", "label": "通过"}
    # 无否决点 + 待核对项 → 通过（附注记）：缺证据/未查验绝不否决
    assert derive_qualification_suggestion(items(("pass", False), ("pass", True), ("pass", True))) == {
        "code": "pass", "label": "通过", "manual_check_count": 2}
    # fail 优先于注记
    assert derive_qualification_suggestion(items(("fail", True), ("pass", True))) == {
        "code": "reject", "label": "存疑（建议否决，需人工确认）", "reject_count": 1}
    # 故障兜底/未评残留格计入待核对注记（2026-11-16 教训：残留在"通过"里必须可见）
    residual = items(("pass", False)) + [
        {"lightweight": True, "lightweight_schema_version": 3,
         "machine_status": "execution_failed", "score_type": "veto"},
        {"lightweight": False, "machine_status": None, "score_type": "veto"}]
    assert derive_qualification_suggestion(residual) == {
        "code": "pass", "label": "通过", "manual_check_count": 2}


def test_report_verdict_lightweight_labels():
    from app.services.report_service import _report_verdict

    # v1/v2 旧显示逐字不变
    assert _report_verdict("pass", True, True) == "通过"
    assert _report_verdict("fail", True, True) == "建议否决"
    assert _report_verdict("concern", True, True) == "存疑"
    assert _report_verdict("not_deeply_reviewed", True, True) == "未深评"  # legacy 行
    # v3 二值化：fail 显示"存疑"（不写"不通过/建议否决"）
    assert _report_verdict("pass", True, True, light_schema=3) == "通过"
    assert _report_verdict("fail", True, True, light_schema=3) == "存疑"
    # deep 口径不变
    assert _report_verdict("fail", True, False) == "不符合"
    assert _report_verdict("pass", True, False) == "符合"


def test_lightweight_binary_anchor_no_third_state():
    """二值化核心锚：引擎任何路径只产生 pass/fail，不产生第三态。"""
    pages = [_page("普通材料文本。")]
    criteria = [{"code": "A", "name": "外部事项", "score_type": "veto",
                 "check_type": "human_only", "params": {}},
                {"code": "B", "name": "承诺条款", "score_type": "match",
                 "check_type": "human_only", "params": {}}]
    for gateway in (FakeGateway(error=RuntimeError("down")),
                    FakeGateway(items=[]),
                    FakeGateway(items=[{"code": "B", "status": "unknown", "reason": "无法判断"}]),
                    FakeGateway(items=[{"code": "B", "status": "reject", "reason": "无引文"}])):
        results = light.review_bidder_qualification(criteria, pages, "synthetic", gateway)
        assert all(r.status in ("pass", "fail") for r in results.values())
        assert all(r.status != "not_deeply_reviewed" for r in results.values())
