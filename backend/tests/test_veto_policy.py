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


# ===========================================================================
# 阶段 2 否决提速（2026-10-30）：human_only 组批 / math 冲突跳 LLM /
# 注册资本抽取补强 / 组批开关回退
# ===========================================================================
def _ho(code: str, name: str = "人工判定项") -> dict:
    return {"code": code, "name": name, "check_type": "human_only", "params": {}}


class _CodeEchoGateway:
    """从 user 块提取全部 code，按 code 生成确定性裁决（验证组批映射与落库顺序）。

    calls 记录调用次数（验证组批杠杆：N 格 → ⌈N/批大小⌉ 次调用）；
    per_call_delay_s 模拟先发块耗时更长（强制块间乱序完成，验证顺序不变式）。
    """

    def __init__(self, verdict_by_code: dict[str, str], per_call_delay_s: float = 0.0):
        import threading

        self._v = verdict_by_code
        self._delay = per_call_delay_s
        self._lock = threading.Lock()
        self.calls = 0

    def chat_json(self, system, user, model, data_class="synthetic"):  # noqa: ANN001
        import re as _re
        import time

        with self._lock:
            self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        codes = _re.findall(r'"code": "([A-Za-z0-9_]+)"', user)
        from app.engine.auto_judge import AutoJudgements, BatchJudgementItem

        return AutoJudgements(judgements=[
            BatchJudgementItem(
                code=c,
                verdict=self._v.get(c, "pass"),
                reasoning=f"裁决 {c}",
                evidence=[{"quote": f"原文摘录 {c}"}],
            )
            for c in codes
        ]), {}


def test_veto_batch_three_human_only_cells(monkeypatch):
    """3 条 human_only 组批：FakeGateway 一次返回 3 条 judgements → 逐格
    verdict/message 与逐格预期一致；一次调用覆盖 3 格（组批杠杆）。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    from app.engine.facts import extract_bid_facts
    from app.services import scoring_service as ss

    gw = _CodeEchoGateway({"c1": "pass", "c2": "fail", "c3": "pass"})
    facts = extract_bid_facts("投标文件正文。")
    cells = [(10, _ho("c1", "密封盖章")), (10, _ho("c2", "签字")), (10, _ho("c3", "承诺书"))]
    out = ss._resolve_veto_results(
        cells,
        facts_by_bidder={10: facts},
        text_by_bidder={10: facts.text},
        scan_hints={10: False},
        gateway=gw,
        data_class="synthetic",
    )
    assert gw.calls == 1  # 3 格同一投标人同批 → 1 次 LLM 调用
    assert out[0]["verdict"] == "pass"
    assert out[0]["message"] == "自动裁决（通过）：裁决 c1"
    assert out[0]["evidence"] == "原文摘录 c1"
    assert out[1]["verdict"] == "need_human"
    assert out[1]["message"].startswith("AI 存疑（不自动否决，转评标委员会认定）")
    assert "自动裁决（不通过）：裁决 c2" in out[1]["message"]
    assert out[2]["verdict"] == "pass"
    assert out[2]["message"] == "自动裁决（通过）：裁决 c3"


def test_veto_batch_missing_code_degrades_that_cell(monkeypatch):
    """批量输出缺某个 code → 该条按契约降 fail（need_human 疑问），其余照常。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    from app.engine.auto_judge import AutoJudgements, BatchJudgementItem
    from app.engine.facts import extract_bid_facts
    from app.services import scoring_service as ss

    class PartialGateway:
        def chat_json(self, system, user, model, data_class="synthetic"):  # noqa: ANN001
            # 只返回 c1/c3，遗漏 c2
            return AutoJudgements(judgements=[
                BatchJudgementItem(code="c1", verdict="pass", reasoning="有对应内容"),
                BatchJudgementItem(code="c3", verdict="pass", reasoning="有对应内容"),
            ]), {}

    facts = extract_bid_facts("正文")
    cells = [(10, _ho("c1")), (10, _ho("c2")), (10, _ho("c3"))]
    out = ss._resolve_veto_results(
        cells,
        facts_by_bidder={10: facts},
        text_by_bidder={10: facts.text},
        scan_hints={10: False},
        gateway=PartialGateway(),
        data_class="synthetic",
    )
    assert out[0]["verdict"] == "pass"
    assert out[1]["verdict"] == "need_human"
    assert "缺少该条" in out[1]["message"]
    assert out[2]["verdict"] == "pass"


def test_refine_with_llm_batch_illegal_verdict_degrades_that_cell():
    """条目级 verdict 非法 → 该条降 fail，不连坐其余条目。"""
    from app.engine.auto_judge import AutoJudgements, BatchJudgementItem, refine_with_llm_batch

    class G:
        def chat_json(self, system, user, model, data_class="synthetic"):  # noqa: ANN001
            return AutoJudgements(judgements=[
                BatchJudgementItem(code="c1", verdict="pass", reasoning="正常"),
                BatchJudgementItem(code="c2", verdict="pending", reasoning="违约输出"),
            ]), {}

    items = [
        {"criterion": _ho("c1"), "facts_text": "{}", "price_line": ""},
        {"criterion": _ho("c2"), "facts_text": "{}", "price_line": ""},
    ]
    out = refine_with_llm_batch(G(), items, "短文本")
    assert out["c1"].verdict == "pass"
    assert out["c2"].verdict == "fail"
    assert "verdict 非法" in out["c2"].reasoning


def test_veto_batch_quota_error_raises_through(monkeypatch):
    """402 类 LLMError → fail-fast 穿透批量路径（不降级成假否决/假疑问）。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    import pytest

    from app.engine.facts import extract_bid_facts
    from app.llm.base import LLMError
    from app.services import scoring_service as ss

    class QuotaGateway:
        def chat_json(self, *a, **k):  # noqa: ANN001
            raise LLMError("402 Payment Required: insufficient balance")

    facts = extract_bid_facts("正文")
    cells = [(10, _ho("c1")), (10, _ho("c2"))]
    with pytest.raises(LLMError):
        ss._resolve_veto_results(
            cells,
            facts_by_bidder={10: facts},
            text_by_bidder={10: facts.text},
            scan_hints={10: False},
            gateway=QuotaGateway(),
            data_class="synthetic",
        )


def test_veto_batch_order_stable_across_blocks(monkeypatch):
    """块间乱序完成 → 落库（返回列表）顺序仍与 veto_cells 原序一致（不变式 I3）。

    2 投标人 × 3 条、批大小 2 → 4 块；先发块人为延迟，强制后发块先完成。
    """
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    monkeypatch.setenv("BID_EVAL_VETO_BATCH_SIZE", "2")
    from app.engine.facts import extract_bid_facts
    from app.services import scoring_service as ss

    verdicts = {"c1": "pass", "c2": "fail", "c3": "pass",
                "c4": "fail", "c5": "pass", "c6": "fail"}
    gw = _CodeEchoGateway(verdicts, per_call_delay_s=0.05)
    facts = extract_bid_facts("正文")
    cells = [
        (10, _ho("c1")), (10, _ho("c2")), (10, _ho("c3")),
        (20, _ho("c4")), (20, _ho("c5")), (20, _ho("c6")),
    ]
    out = ss._resolve_veto_results(
        cells,
        facts_by_bidder={10: facts, 20: facts},
        text_by_bidder={10: facts.text, 20: facts.text},
        scan_hints={10: False, 20: False},
        gateway=gw,
        data_class="synthetic",
    )
    assert gw.calls == 4  # 2 投标人 × ⌈3/2⌉ 块
    expected_verdict = ["pass", "need_human", "pass", "need_human", "pass", "need_human"]
    assert [r["verdict"] for r in out] == expected_verdict
    for i, (_bid, c) in enumerate(cells):
        assert f"裁决 {c['code']}" in out[i]["message"]


def test_veto_batch_disabled_matches_single_path(monkeypatch):
    """组批开关关闭 → 与旧单格路径逐字节一致（同一网关逐格 _eval_veto 对照）。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    monkeypatch.setenv("BID_EVAL_VETO_HUMAN_ONLY_BATCH", "0")
    from decimal import Decimal

    from app.engine.auto_judge import AutoJudgement
    from app.engine.facts import extract_bid_facts
    from app.services import scoring_service as ss

    class G:
        def __init__(self):
            self.calls = 0

        def chat_json(self, system, user, model, data_class="synthetic"):  # noqa: ANN001
            self.calls += 1
            return AutoJudgement(verdict="pass", reasoning="文本通道已见对应内容"), {}

    facts = extract_bid_facts("投标函 授权委托书\n不转包、不分包")
    facts.performance_amounts = [Decimal("1000000")] * 3
    gw = G()
    cells = [
        (7, _ho("c1", "密封盖章")),                                   # human_only → single LLM
        (7, {"code": "c2", "name": "不转包", "check_type": "contains_phrase",
             "params": {"phrase": "不转包、不分包"}}),                 # 命中 → pass 终态
        (7, {"code": "c3", "name": "保证金", "check_type": "document_present",
             "params": {"document": "投标保证金缴纳凭证"}}),           # unknown → single LLM
        (7, {"code": "c4", "name": "业绩", "check_type": "count_at_least",
             "params": {"field": "performance", "min_count": "3"}}),   # 满足 → pass 终态
    ]
    got = ss._resolve_veto_results(
        cells,
        facts_by_bidder={7: facts},
        text_by_bidder={7: facts.text},
        scan_hints={7: False},
        gateway=gw,
        data_class="synthetic",
    )
    expected = [
        ss._eval_veto(c, facts, text=facts.text, gateway=gw, data_class="synthetic")
        for _bid, c in cells
    ]
    assert got == expected


def test_math_relation_fail_skips_llm(monkeypatch):
    """math_relation fail+need_human（结构化冲突已定论）→ 不调 LLM，直接
    conflict_flagged 转人工；开关关（BID_EVAL_MATH_SKIP_LLM=0）回退 LLM。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    from decimal import Decimal

    from app.engine.auto_judge import AutoJudgement
    from app.engine.facts import extract_bid_facts
    from app.services import scoring_service as ss

    class RecordingGateway:
        def __init__(self):
            self.calls = 0

        def chat_json(self, system, user, model, data_class="synthetic"):  # noqa: ANN001
            self.calls += 1
            return AutoJudgement(verdict="pass", reasoning="不应被调用"), {}

    facts = extract_bid_facts("")
    facts.bid_price = Decimal("100")
    facts.bid_price_upper = "贰佰元整"
    facts.bid_price_upper_value = Decimal("200")  # 大小写不一致 → fail + need_human
    c = {"code": "m1", "name": "报价算术关系", "check_type": "math_relation", "params": {}}

    gw = RecordingGateway()
    out = ss._eval_veto(c, facts, text=facts.text, gateway=gw)
    assert gw.calls == 0
    assert out["verdict"] == "conflict_flagged"

    # 预检去向：冲突格 → "none"（终态映射，不进 LLM 路）
    result, kind = ss._veto_precheck(c, facts)
    assert kind == "none"
    assert result.status == "fail" and result.need_human

    # 开关关 → 回退逐格 LLM 旧路径
    monkeypatch.setenv("BID_EVAL_MATH_SKIP_LLM", "0")
    gw2 = RecordingGateway()
    out2 = ss._eval_veto(c, facts, text=facts.text, gateway=gw2)
    assert gw2.calls == 1
    assert out2["verdict"] == "pass"


def test_math_relation_unknown_keeps_llm(monkeypatch):
    """未抽到报价的 unknown（无法比对）→ 保留 LLM 语义裁决。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    from app.engine.auto_judge import AutoJudgement
    from app.engine.facts import extract_bid_facts
    from app.services import scoring_service as ss

    class G:
        def __init__(self):
            self.calls = 0

        def chat_json(self, system, user, model, data_class="synthetic"):  # noqa: ANN001
            self.calls += 1
            return AutoJudgement(verdict="pass", reasoning="文本通道未见异常报价"), {}

    facts = extract_bid_facts("正文无报价")  # bid_price=None → unknown
    c = {"code": "m2", "name": "报价算术关系", "check_type": "math_relation", "params": {}}
    gw = G()
    out = ss._eval_veto(c, facts, text=facts.text, gateway=gw)
    assert gw.calls == 1
    assert out["verdict"] == "pass"


def test_registered_capital_variants():
    """注册资本抽取补强（阶段 2，2026-10-30）：连接字 为/计/系/共、单位「元」
    不换算、「万 元」空格变体、「…整」后缀；中文大写宁缺勿错。"""
    from decimal import Decimal

    from app.engine.facts import extract_bid_facts

    # 连接字「为」+ 万元（含千分位）
    assert extract_bid_facts("注册资本为人民币1,000万元。").registered_capital == Decimal("10000000")
    # 连接字「计」+ …万元整
    assert extract_bid_facts("注册资本计人民币500万元整。").registered_capital == Decimal("5000000")
    # 纯「元」单位：值即元，不乘 10000
    assert extract_bid_facts("注册资本：人民币1000000元").registered_capital == Decimal("1000000")
    # 「万 元」空格变体（OCR 拆字）+ 连接字「系」
    assert extract_bid_facts("注册资本系人民币200 万 元").registered_capital == Decimal("2000000")
    # 宁缺勿错：中文大写数字不解析
    assert extract_bid_facts("注册资本：人民币壹仟万元").registered_capital is None
    # 旧形态回归：冒号 + 人民币 + 万元（首个 .search 命中语义不变）
    assert extract_bid_facts(
        "注册资本：人民币2,000万元\n其他注册资本：人民币3,000万元"
    ).registered_capital == Decimal("20000000")
