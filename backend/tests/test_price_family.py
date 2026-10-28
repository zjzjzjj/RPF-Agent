# -*- coding: utf-8 -*-
"""P0-A/P0-D：价格公式确定性正则目录 + 校验器细分。

句式 fixture 取自 21 份浙江中烟招标文件语料（docs/评分细则语料台账-招标文件集21份.md
第四节），数值为原文实录——仅作解析测试数据，不进引擎作为跨项目默认值（红线）。
"""
from __future__ import annotations

from decimal import Decimal

from app.rules.compiler import (
    _parse_benchmark_gate,
    _parse_outlier_policy,
    _parse_trim_tiers,
    parse_price_formula_text,
)
from app.rules.validators import run_validators


# --- 六变体基准价句式 ---------------------------------------------------------

# 次低价（2024 烟丝检验仪器）
SECOND_LOWEST_TEXT = """1）进入商务报价最佳报价值需满足以下条件：（1）有效投标报价，或经修正后的有效投标报价；
（2）技术部分评审得分达到技术部分总分的 70%及以上；
最佳报价值：满足进入商务报价最佳报价值条件的全部有效投标总价中的次低价为最佳报价值。
1）等于最佳报价值的评标价得满分 50分；
2）评标价偏离（高于或低于）最佳报价值的，按评标价偏离最佳报价值的正负百分率，计算报价得分：每低 1个百分点扣 0.3分；每高1 个百分点扣0.5分。扣完为止。
注：有效投标报价高于最佳报价的两倍或低于最佳报价的一半，其商务报价得分为 0分，并剔除该报价后重新计算得出最佳报价。
"""


def test_second_lowest_benchmark() -> None:
    parsed = parse_price_formula_text(SECOND_LOWEST_TEXT)
    assert parsed["benchmark_method"] == "second_lowest"
    assert parsed["deduct_rate_down"] == Decimal("0.3")
    assert parsed["deduct_rate_up"] == Decimal("0.5")


def test_smoke_entry_gate_and_text_outlier() -> None:
    parsed = parse_price_formula_text(SECOND_LOWEST_TEXT)
    gate = parsed["benchmark_gate"]
    assert gate["gate_on"] == "tech"
    assert gate["min_ratio"] == "70"
    assert gate["ratio_kind"] == "percent"
    assert gate["on_fail"] == "exclude"
    outlier = parsed["outlier_policy"]
    assert outlier["max_mult"] == "2"
    assert outlier["min_mult"] == "0.5"
    assert outlier["on_hit"] == "zero_and_recompute"


# 二次算术平均·复合（2025 实验室设备第一批：≥6 家首均去 1 高 1 低 + 低于首均再平均）
SECOND_AVG_TEXT = """若有效投标人≥6个，则全部有效投标人最终报价扣除一个最高价和一个最低价后，剩余有效投标人评标价的算术平均值为第一次算术平均值。对低于第一次算术平均值的有效投标价再进行算术平均值计算，得出“最佳报价值”；
若有效投标人＜6个，则全部有效投标人最终报价的算术平均值为第一次算术平均值。对低于第一次算术平均值的有效投标价再进行算术平均值计算，得出“最佳报价值”。
投标人报价低于最佳报价值时，每下浮一个百分点扣 0.4分；
投标人报价高于最佳报价值时，每上浮一个百分点扣 0.6分。
注：有效投标价格＞最佳报价*2或＜最佳报价/2，其商务报价得分为 0分，剔除该报价后重新计算得出最佳报价值。
"""


def test_compound_trimmed_then_average_below() -> None:
    parsed = parse_price_formula_text(SECOND_AVG_TEXT)
    assert parsed["benchmark_method"] == "average_trimmed_then_below"
    assert parsed["trim_count"] == 1
    assert parsed["trim_threshold"] == 6


def test_numeric_outlier_two_sided() -> None:
    parsed = parse_price_formula_text(SECOND_AVG_TEXT)
    outlier = parsed["outlier_policy"]
    assert outlier["max_mult"] == "2"
    assert outlier["min_mult"] == "0.5"


# ×0.95 三档去极值（多媒体维保 p137 实录，边界 5/10）
MM_TEXT = """1、若有效投标人＞10 个时，全部有效投标人投标总价中去掉一个最高价、一个次高价、一个最低价和一个次低价后的算术平均值*0.95 为评标基准价；
2、若5 个＜有效投标人≤10 个时，则全部有效投标人投标总价扣除一个最高价和一个最低价后的算术平均值*0.95 为最佳报价值；
3、若有效投标人≤5 个，则全部有效投标人最终报价的算术平均值*0.95 为最佳报价值。
每高于1 个百分点扣0.8 分，每低于一个百分点扣0.4 分。
注：1）投标有效家数超过5 家，经专家评审的（商务分+技术分）达不到（商务分+技术分）满分的70%的，不进入最佳报价计算。
2）有效投标报价高于最佳报价的两倍或低于最佳报价的一半，其商务报价得分为0 分，并剔除该报价后重新计算得出最佳报价。
"""


def test_k_factor_extracted() -> None:
    parsed = parse_price_formula_text(MM_TEXT)
    assert parsed["k"] == Decimal("0.95")
    assert parsed["trim_tiers"] == [[5, 0], [10, 1], [None, 2]]
    assert parsed["trim_threshold"] == 5


def test_biz_tech_gate() -> None:
    gate = _parse_benchmark_gate(MM_TEXT)
    assert gate is not None
    assert gate["gate_on"] == "biz_tech"
    assert gate["min_ratio"] == "70"
    assert gate["on_fail"] == "exclude"


# 老代分档 15/30（利群休闲细支）——旧硬编码 5/10 解析器对它直接失明
TIER_15_30_TEXT = """若参与评标基准价计算的有效投标人等于或不足15家时，则去掉1个最高投标报价和1个最低投标报价的算术平均值为评标基准价；
若参与评标基准价计算的有效投标人超过15家且不足30家，则去掉2个最高和2个最低投标报价的算术平均值为评标基准价；
若参与评标基准价计算的有效投标人达到或超过30家，则去掉4个最高和4个最低投标报价的算术平均值为评标基准价。
每低1个百分点扣0.4分；每高1个百分点扣0.6分。扣完为止。
"""


def test_generic_tier_bounds_15_30() -> None:
    parsed = parse_price_formula_text(TIER_15_30_TEXT)
    assert parsed["trim_tiers"] == [[15, 1], [30, 2], [None, 4]]
    assert parsed["benchmark_method"] == "average_trimmed"
    assert parsed["trim_threshold"] == 15


# 赋半分门槛（2026 一体化运维平台维保）
HALF_MAX_TEXT = """经专家评审的（商务分+技术分）达不到（商务分+技术分）满分的70%的，该供应商报价分赋值为满分报价分的50%（即从报价分满分30分开始计算得分），并剔除该报价后重新计算得出最佳报价值。
每低于一个百分点扣0.4分，每高于1个百分点扣0.8分。
"""


def test_half_max_gate() -> None:
    gate = _parse_benchmark_gate(HALF_MAX_TEXT)
    assert gate is not None
    assert gate["gate_on"] == "biz_tech"
    assert gate["on_fail"] == "half_max"
    assert gate["half_max_ratio"] == "50"


# 样品 0 分/商技绝对分数线剔基准（商标条盒新代）
SAMPLE_GATE_TEXT = """样品部分得分为0分或（和）“商务/资信部分+技术部分”合计分值＜32分的投标人，其投标报价不纳入评标基准价的计算。
未参与评标基准价计算的投标报价可以进入（商务）报价评审。
评标基准价为各有效投标人报价的算术平均价。
每低1个百分点扣0.2分；每高1个百分点扣1.0分。扣完为止。
"""


def test_sample_composite_gate_absolute_score() -> None:
    parsed = parse_price_formula_text(SAMPLE_GATE_TEXT)
    assert parsed["benchmark_method"] == "average"
    gate = parsed["benchmark_gate"]
    assert gate["gate_on"] == "sample_or_composite"
    assert gate["min_ratio"] == "32"
    assert gate["ratio_kind"] == "absolute"
    assert gate["on_fail"] == "exclude"
    assert gate["still_reviewable"] is True


def test_trim_tiers_none_for_single_tier() -> None:
    """单档截尾不产 tiers（走旧键），与 test_normalize_package 同口径。"""
    assert _parse_trim_tiers("若有效投标人＞5个，则全部有效投标人最终报价扣除一个最高价和一个最低价后的算术平均值为最佳报价值。") is None


def test_outlier_policy_requires_recompute_clause() -> None:
    """没有"剔除…重新计算"语境不产 outlier_policy（不猜阈值）。"""
    assert _parse_outlier_policy("报价高于最佳报价的两倍其商务报价得分为0分。") is None


# --- P0-D 校验器 --------------------------------------------------------------


def _price_crit(ext: dict) -> dict:
    return {
        "code": "P1", "category": "price", "name": "报价分", "score_type": "formula",
        "is_veto": False, "max_score": Decimal("50"),
        "formula": {
            "formula_type": "deviation_deduct", "benchmark_method": "average_trimmed",
            "k": Decimal("1"), "params_json": ext,
        },
    }


def _codes(criteria: list[dict]) -> set[str]:
    findings = run_validators({"package": {"total_max": 100}, "criteria": criteria})
    return {f.code for f in findings}


def test_validator_trim_tiers_malformed() -> None:
    assert "PRICE_TRIM_TIERS_MALFORMED" in _codes(
        [_price_crit({"deduct_rate_down": "0.2", "trim_tiers": [[10, 2], [5, 1]]})]
    )
    assert "PRICE_TRIM_TIERS_MALFORMED" not in _codes(
        [_price_crit({"deduct_rate_down": "0.2", "trim_tiers": [[5, 0], [10, 1], [None, 2]]})]
    )


def test_validator_gate_incomplete() -> None:
    assert "PRICE_GATE_INCOMPLETE" in _codes(
        [_price_crit({"deduct_rate_down": "0.2",
                      "benchmark_gate": {"min_ratio": "0.7", "on_fail": "half_max"}})]
    )
    assert "PRICE_GATE_INCOMPLETE" not in _codes(
        [_price_crit({"deduct_rate_down": "0.2",
                      "benchmark_gate": {"min_ratio": "70", "ratio_kind": "percent",
                                         "gate_on": "tech", "on_fail": "exclude"}})]
    )


def test_validator_outlier_incomplete_warns() -> None:
    assert "PRICE_OUTLIER_POLICY_INCOMPLETE" in _codes(
        [_price_crit({"deduct_rate_down": "0.2", "outlier_policy": {"on_hit": "zero_and_recompute"}})]
    )


def test_validator_label_mismatch_warns() -> None:
    assert "PRICE_METHOD_LABEL_MISMATCH" in _codes(
        [_price_crit({"deduct_rate_down": "0.2",
                      "_label_mismatch": {"model": "average_trimmed", "deterministic": "average_below"}})]
    )


def test_engine_approx_covers_new_keys() -> None:
    assert "PRICE_ENGINE_APPROX" in _codes(
        [_price_crit({"deduct_rate_down": "0.2",
                      "benchmark_gate": {"min_ratio": "70", "ratio_kind": "percent",
                                         "gate_on": "tech", "on_fail": "exclude"}})]
    )


def test_validator_ambiguous_subjectivity_warns() -> None:
    crit = {
        "code": "C1", "category": "commercial", "name": "增值服务", "score_type": "match",
        "is_veto": False, "max_score": Decimal("4"),
        "params": {"_subjectivity_hint": "objective"},
    }
    findings = run_validators({"package": {"total_max": 100}, "criteria": [crit]})
    assert "AMBIGUOUS_SUBJECTIVITY" in {f.code for f in findings}


# --- P1 引擎执行链 -------------------------------------------------------------

from decimal import Decimal as D  # noqa: E402

from app.engine.models import PriceBid  # noqa: E402
from app.engine.price import compute_benchmark, compute_price_scores, _tiers_cut  # noqa: E402


def _dev_formula(**ext) -> dict:
    f = {
        "formula_type": "deviation_deduct", "benchmark_method": "average",
        "k": D(1), "max_score": D(50), "rounding": 2,
        "params_json": {"deduct_rate_down": "0.2", "deduct_rate_up": "1.0", **ext},
    }
    return f


def test_tiers_cut_lookup() -> None:
    tiers = [[5, 0], [10, 1], [None, 2]]
    assert _tiers_cut(3, tiers) == 0
    assert _tiers_cut(8, tiers) == 1
    assert _tiers_cut(12, tiers) == 2
    # 老代 15/30 边界
    old = [[15, 1], [30, 2], [None, 4]]
    assert _tiers_cut(10, old) == 1
    assert _tiers_cut(20, old) == 2
    assert _tiers_cut(40, old) == 4


def test_benchmark_trim_tiers_beats_single_tier() -> None:
    """n=12 分档应去 2 高 2 低，单档近似只去 1 高 1 低 → 基准价不同（非等差报价）。"""
    prices = [D(80), D(90), D(95), D(98), D(100), D(102),
              D(104), D(106), D(110), D(120), D(150), D(300)]
    tiers = [[5, 0], [10, 1], [None, 2]]
    base_t, note_t = compute_benchmark(prices, "average_trimmed", trim_tiers=tiers)
    base_s, _ = compute_benchmark(prices, "average_trimmed", trim_count=1, trim_threshold=5)
    assert "分档规则" in note_t
    assert base_t != base_s  # 12 家：tiers 去 2 高 2 低，单档只去 1 高 1 低


def test_benchmark_compound_trimmed_then_below() -> None:
    prices = [D(100), D(102), D(104), D(106), D(108), D(200)]
    # 首步去 1 高 1 低（6>6? trim_threshold=6 → n>6 才截，6 家不截）→ 首均=120
    base, note = compute_benchmark(
        prices, "average_trimmed_then_below", trim_count=1, trim_threshold=6
    )
    assert "第一次算术平均" in note and "二次平均" in note
    # 首均 120；≤120 的报价 {100,102,104,106,108} 均值=104
    assert base == D("104.00")


def test_outlier_zero_and_recompute() -> None:
    f = _dev_formula(outlier_policy={
        "max_mult": "2", "min_mult": "0.5", "on_hit": "zero_and_recompute",
        "source_text": "超过基准2倍或低于一半记0并剔除重算",
    })
    bids = [
        PriceBid(1, "甲", D(100)),
        PriceBid(2, "乙", D(102)),
        PriceBid(3, "丙", D(104)),
        PriceBid(4, "丁", D(106)),
        PriceBid(5, "戊", D(500)),  # 5×均值 → 离群
    ]
    comp = compute_price_scores(bids, f)
    by_id = {r.bidder_id: r for r in comp.results}
    assert by_id[5].price_score == 0
    assert "离群保护" in comp.formula_explanation
    assert "剔除后重算" in comp.formula_explanation
    # 重算基准 = 100/102/104/106 均值 103 → 甲偏离为负得更多
    assert comp.benchmark == D("103.00")
    assert by_id[1].price_score > 0


def test_outlier_params_incomplete_not_executed() -> None:
    f = _dev_formula(outlier_policy={"on_hit": "zero_and_recompute", "source_text": "x"})
    bids = [PriceBid(1, "甲", D(100)), PriceBid(2, "乙", D(101))]
    comp = compute_price_scores(bids, f)
    assert "离群保护参数不全" in comp.formula_explanation


def test_gate_exclude_keeps_score_but_drops_from_benchmark() -> None:
    f = _dev_formula(benchmark_gate={
        "min_ratio": "70", "ratio_kind": "percent", "gate_on": "tech",
        "on_fail": "exclude", "source_text": "技术分≥70%进基准",
    })
    bids = [
        PriceBid(1, "甲", D(100), gate_value=D(40), gate_basis=D(50)),   # 80% 达标
        PriceBid(2, "乙", D(90), gate_value=D(30), gate_basis=D(50)),    # 60% 未达
        PriceBid(3, "丙", D(110), gate_value=D(46), gate_basis=D(50)),   # 92% 达标
    ]
    comp = compute_price_scores(bids, f)
    # 基准价只由甲、丙构成：(100+110)/2=105（乙 90 被剔出）
    assert comp.benchmark == D("105.00")
    by_id = {r.bidder_id: r for r in comp.results}
    assert by_id[2].price_score > 0  # 仍计分供评审（未达门槛≠不得分）
    assert "未达基准价门槛" in (by_id[2].explanation or "")


def test_gate_half_max_short_circuit() -> None:
    f = _dev_formula(benchmark_gate={
        "min_ratio": "70", "ratio_kind": "percent", "gate_on": "biz_tech",
        "on_fail": "half_max", "half_max_ratio": "50", "source_text": "赋满分50%",
    })
    bids = [
        PriceBid(1, "甲", D(100), gate_value=D(40), gate_basis=D(50)),
        PriceBid(2, "乙", D(90), gate_value=D(20), gate_basis=D(50)),  # 40% 未达
    ]
    comp = compute_price_scores(bids, f)
    by_id = {r.bidder_id: r for r in comp.results}
    assert by_id[2].price_score == D("25.00")  # 50×50%
    assert "赋满分报价分的" in comp.formula_explanation


def test_gate_unknown_not_executed() -> None:
    f = _dev_formula(benchmark_gate={
        "min_ratio": "70", "ratio_kind": "percent", "gate_on": "tech",
        "on_fail": "exclude", "source_text": "技术分≥70%进基准",
    })
    bids = [PriceBid(1, "甲", D(100)), PriceBid(2, "乙", D(90))]
    comp = compute_price_scores(bids, f)
    # 分不可得 → 两家都不被门槛排除，基准=95 全量
    assert comp.benchmark == D("95.00")
    assert "门槛所需分不可得" in comp.formula_explanation


def test_gate_sample_composite_absolute() -> None:
    f = _dev_formula(benchmark_gate={
        "min_ratio": "32", "ratio_kind": "absolute", "gate_on": "sample_or_composite",
        "on_fail": "exclude", "source_text": "商技<32 剔基准",
    })
    bids = [
        PriceBid(1, "甲", D(100), gate_value=D(40)),  # 40≥32 达标
        PriceBid(2, "乙", D(90), gate_value=D(30)),   # 30<32 未达
        PriceBid(3, "丙", D(110), sample_pass=False),  # 样品 0 分
    ]
    comp = compute_price_scores(bids, f)
    assert comp.benchmark == D("100.00")  # 仅甲进基准


def test_gate_min_valid_bidders_precondition_excluded() -> None:
    """规则原文「投标有效家数超过5家…才不进入最佳报价」→ 前置条件不足不执行门槛。"""
    from app.rules.compiler import _parse_benchmark_gate

    seg = "注：1）投标有效家数超过5家，经专家评审的（商务分+技术分）达不到（商务分+技术分）满分的70%的，不进入最佳报价计算"
    gate = _parse_benchmark_gate(seg)
    assert gate is not None
    assert gate.get("min_valid_bidders") == 6  # 超过5家 → ≥6 才执行

    # 引擎侧：有效家数 5（< 6）→ 门槛不执行 → 全员进基准价
    f = _dev_formula(benchmark_gate=gate)
    bids = [
        PriceBid(1, "甲", D(100), gate_value=D(10), gate_basis=D(50)),   # 20% 但门槛不生效
        PriceBid(2, "乙", D(120), gate_value=D(8), gate_basis=D(50)),
    ]
    comp = compute_price_scores(bids, f)
    assert comp.benchmark == D("110.00")  # (100+120)/2 两家全进
    assert "不执行70%门槛" in comp.formula_explanation


def test_gate_all_excluded_falls_back_to_ungated() -> None:
    """门槛把全部投标人都排掉 → 不执行（规则自相矛盾/输入失真），禁全 0。"""
    f = _dev_formula(benchmark_gate={
        "min_ratio": "70", "ratio_kind": "percent", "gate_on": "tech",
        "on_fail": "exclude", "source_text": "技术≥70%进基准",
    })
    bids = [
        PriceBid(1, "甲", D(100), gate_value=D(10), gate_basis=D(50)),  # 20%
        PriceBid(2, "乙", D(120), gate_value=D(12), gate_basis=D(50)),  # 24%
    ]
    comp = compute_price_scores(bids, f)
    assert comp.benchmark is not None  # 护栏：不为空
    by_id = {r.bidder_id: r for r in comp.results}
    assert by_id[1].price_score > 0
    assert "规则自相矛盾" in comp.formula_explanation


# --- 2026-10-28：价格公式确定性识别（早退拆除 / 公式式扣分率 / 次低 n=2 / 未识别阻断） ---


# 公式式扣分率（语料 3/4 份）：投标报价得分（报价高于基准值）=50-（投标报价-基准值）/基准值*100*0.4
FORMULA_RATE_TEXT = """投标报价得分（报价高于基准值）=50-（投标报价-基准值）/基准值*100*0.4。
投标报价得分（报价低于基准值）=50-（基准值-投标报价）/基准值*100*0.2。
若有效投标人＞5个，则全部有效投标人最终报价扣除一个最高价和一个最低价后的算术平均值为最佳报价值。
"""


def test_formula_style_deduct_rate() -> None:
    parsed = parse_price_formula_text(FORMULA_RATE_TEXT)
    assert parsed["formula_type"] == "deviation_deduct"
    assert parsed["deduct_rate_up"] == Decimal("0.4")
    assert parsed["deduct_rate_down"] == Decimal("0.2")
    assert parsed["benchmark_method"] == "average_trimmed"


def test_benchmark_method_only_returns_benchmark_ratio() -> None:
    """只有基准法（无扣分率）→ benchmark_ratio，不再早退返回空。"""
    parsed = parse_price_formula_text("取算术平均值*0.95为最佳报价值。")
    assert parsed["formula_type"] == "benchmark_ratio"
    assert parsed["benchmark_method"] == "average"
    assert parsed["k"] == Decimal("0.95")
    assert "deduct_rate_up" not in parsed


def test_unrecognized_returns_empty() -> None:
    """完全不识别（无基准法也无扣分率）→ 空 dict，交调用方报未识别。"""
    assert parse_price_formula_text("本项目价格分由评标委员会讨论确定。") == {}


def test_second_lowest_two_bids_takes_lowest() -> None:
    """n=2 取最低价（宁波空调箱 p90：只有2家时取最低投标报价）。"""
    from app.engine.price import compute_benchmark

    two = [D("100"), D("120")]
    base, _note = compute_benchmark(two, "second_lowest", D("1"))
    assert base == D("100.00")  # 旧实现取 ordered[1]=120，与原文相反

    three = [D("100"), D("110"), D("120")]
    base3, _ = compute_benchmark(three, "second_lowest", D("1"))
    assert base3 == D("110.00")  # n=3 仍是次低


def test_engine_unknown_formula_type_returns_uncomputable() -> None:
    """未知 formula_type 不抛异常，返回"不可计算"说明。"""
    from app.engine.price import compute_price_scores

    f = {"max_score": D("50"), "formula_type": "unknown_type",
         "benchmark_method": "average", "k": D("1")}
    bids = [PriceBid(1, "甲", D(100))]
    comp = compute_price_scores(bids, f)
    by_id = {r.bidder_id: r for r in comp.results}
    assert "不可计算" in by_id[1].explanation
    assert by_id[1].price_score == D("0")


def test_validator_unrecognized_formula_finding() -> None:
    """公式未识别 → PRICE_FORMULA_UNRECOGNIZED（error）。"""
    crit = {
        "code": "P1", "category": "price", "name": "报价分", "score_type": "formula",
        "is_veto": False, "max_score": Decimal("50"),
        "formula": {"formula_type": "", "benchmark_method": "", "params_json": {}},
    }
    codes = _codes([crit])
    assert "PRICE_FORMULA_UNRECOGNIZED" in codes


def test_enrich_no_silent_default_formula_type() -> None:
    """enrich 路径：未识别的 window 不硬编码 formula_type/k。"""
    from app.rules.compiler import parse_price_formula_text

    parsed = parse_price_formula_text("价格分由评标委员会讨论确定。")
    assert parsed == {}  # 无 formula_type 可默认，交阻断


def test_engine_empty_formula_type_returns_uncomputable() -> None:
    """空 formula_type（未识别）→ 全员不可计算，不静默回退 benchmark_ratio。"""
    from app.engine.price import compute_price_scores

    f = {"max_score": D("50"), "formula_type": "",
         "benchmark_method": "average", "k": D("1")}
    bids = [PriceBid(1, "甲", D(100))]
    comp = compute_price_scores(bids, f)
    assert comp.benchmark is None
    assert "未识别" in comp.formula_explanation
    for r in comp.results:
        assert r.price_score == D("0")
        assert "不可计算" in r.explanation
