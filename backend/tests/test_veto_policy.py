"""AI 不否决策略单测：auto 模式否决条款 fail → 疑问（need_human），人工模式不变。"""
from __future__ import annotations

from app.engine.models import PrimitiveResult


def test_review_status_auto_doubt_kept(monkeypatch):
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    from app.services.scoring_service import _review_status_for_verdict

    assert _review_status_for_verdict({"verdict": "need_human"}) == "conflict_flagged"
    assert _review_status_for_verdict({"verdict": "pass"}) == "auto_confirmed"
    assert _review_status_for_verdict({"verdict": "recommend_reject"}) == "auto_confirmed"


def test_review_status_manual_unchanged(monkeypatch):
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "0")
    from app.services.scoring_service import _review_status_for_verdict

    assert _review_status_for_verdict({"verdict": "pass"}) == "pending"
    assert _review_status_for_verdict({"verdict": "recommend_reject"}) == "conflict_flagged"
    assert _review_status_for_verdict({"verdict": "need_human"}) == "pending"


def test_eval_veto_auto_fail_downgrades_to_doubt(monkeypatch):
    """auto 模式下 LLM 裁决 fail → 疑问（need_human），不再产出否决建议。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")

    import app.engine.auto_judge as auto_judge
    from app.services import scoring_service as ss

    class FakeGateway:
        pass

    def fake_refine(gateway, c, facts, text, machine_result, data_class="synthetic"):
        assert machine_result.status == "unknown"
        return PrimitiveResult("fail", "模型认为证据不足（宁严勿纵）", need_human=False)

    monkeypatch.setattr(auto_judge, "refine_with_llm", fake_refine)

    class Facts:
        text = "x"

    out = ss._eval_veto(
        {"check_type": "human_only", "params": {}, "name": "测试条款"},
        Facts(),
        text="x",
        gateway=FakeGateway(),
    )
    assert out["verdict"] == "need_human"
    assert out["message"].startswith("AI 存疑（不自动否决，转评标委员会认定）")
    assert "宁严勿纵" in out["message"]


def test_eval_veto_auto_pass_stays_pass(monkeypatch):
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")

    import app.engine.auto_judge as auto_judge
    from app.services import scoring_service as ss

    class FakeGateway:
        pass

    monkeypatch.setattr(
        auto_judge, "refine_with_llm",
        lambda *a, **k: PrimitiveResult("pass", "已找到承诺书", need_human=False),
    )

    class Facts:
        text = "x"

    out = ss._eval_veto(
        {"check_type": "human_only", "params": {}, "name": "测试条款"},
        Facts(),
        text="x",
        gateway=FakeGateway(),
    )
    assert out["verdict"] == "pass"
