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

    def fake_refine(gateway, c, facts, text, machine_result, data_class="synthetic", **_kw):
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


def test_veto_scan_hint_exempts_hard_fail():
    """扫描件豁免只对**存在性/检索类**检查生效（2026-10-27 收窄口径）。

    contains_phrase 未命中 → unknown（不硬 fail，无论 scan_hint）；
    document_present 类硬 fail 在扫描件页在场时不否决；
    count_at_least（结构化事实输入）的确定性 fail **保留**——不受扫描件可见性影响，
    否则 LLM 触发率虚高 60-76% 且引入模型方差。
    """
    from decimal import Decimal

    from app.engine.facts import extract_bid_facts
    from app.services.scoring_service import _eval_veto

    facts = extract_bid_facts("本文件只谈技术方案。")
    c = {"code": "c13", "name": "不得转包、不允许分包",
         "check_type": "contains_phrase", "params": {"phrase": "不转包、不分包"}}
    # contains_phrase 未命中 → unknown（不硬 fail）
    r0 = _eval_veto(c, facts, text="")
    assert r0["verdict"] != "recommend_reject"

    # 存在性类（document_present）+ scan_hint → 豁免
    facts1 = extract_bid_facts("只有技术方案正文。")
    c1 = {"code": "c1", "name": "投标保证金缴纳凭证",
          "check_type": "document_present", "params": {"document": "投标保证金缴纳凭证"}}
    r1 = _eval_veto(c1, facts1, text="", scan_hint=True)
    assert r1["verdict"] != "recommend_reject"

    # 计数类（count_at_least）确定性 fail 保留（结构化事实，不受扫描件影响）
    facts2 = extract_bid_facts("")
    facts2.performance_amounts = [Decimal("1000000")]  # 1 个 < 要求 3 → 硬 fail
    c2 = {"code": "c9", "name": "近三年业绩",
          "check_type": "count_at_least",
          "params": {"field": "performance", "min_count": "3"}}
    r2 = _eval_veto(c2, facts2, text="", scan_hint=True)
    assert r2["verdict"] == "recommend_reject"  # 收窄后计数 fail 不再豁免


def test_date_valid_primitive():
    """date_valid 三态：窗口外 fail、窗口内 pass、找不到日期 unknown。"""
    from app.engine.facts import extract_bid_facts
    from app.engine.objective import run_check

    facts = extract_bid_facts(
        "授权委托书\n本单位授权张三为代理人。落款日期：2026年6月30日\n（单位盖章）")
    # 落款晚于截止 → fail（金程授权书 2026-6-30 无效实证）
    r = run_check("date_valid", {"document": "授权委托书", "not_after": "2026-06-15"}, facts)
    assert r.status == "fail"
    # 窗口内 → pass
    r2 = run_check("date_valid", {"document": "授权委托书", "not_after": "2026-12-31"}, facts)
    assert r2.status == "pass"
    # 无日期 → unknown
    facts3 = extract_bid_facts("授权委托书（正文无落款日期）")
    r3 = run_check("date_valid", {"document": "授权委托书"}, facts3)
    assert r3.status == "unknown" and r3.need_human
