"""Automatic evaluations must execute complete price rules or fail explicitly."""
from decimal import Decimal

import pytest

from app.engine.models import PriceBid
from app.engine.price import compute_price_scores


def _formula(**changes):
    return {
        "formula_type": "deviation_deduct", "benchmark_method": "average",
        "max_score": "40", "deduct_rate": "0.5", **changes,
    }


def _bids():
    return [PriceBid(1, "甲", Decimal("100")), PriceBid(2, "乙", Decimal("120"))]


@pytest.mark.parametrize("changes", [
    {"formula_type": None},
    {"formula_type": "unknown"},
    {"benchmark_method": "unknown"},
    {"max_score": "NaN"},
    {"k": "0"},
    {"upper_bound": "41"},
    {"lower_bound": "20", "upper_bound": "10"},
    {"deduct_rate": "-1"},
    {"rounding": "-1"},
    {"params_json": {"lot_weights": [{"weight": "100"}]}},
    {"params_json": {"outlier_policy": {"on_hit": "zero_and_recompute"}}},
    {"params_json": {"trim_tiers": [[5, 1]]}},
    {"params_json": {"benchmark_gate": {"min_ratio": "NaN"}}},
    {"params_json": {"special_rules": ["技术分不足70%不进入最佳报价计算"]}},
    {"params_json": {"special_rules": ["报价超倍，剔除该报价后重新计算"]}},
    {"formula_type": "above_deduct", "deduct_rate": "0"},
])
def test_strict_incomplete_rule_never_produces_scores(changes):
    with pytest.raises(ValueError):
        compute_price_scores(_bids(), _formula(**changes), strict=True)


def _gate(**changes):
    return {
        "gate_on": "tech", "ratio_kind": "percent", "min_ratio": "70",
        "on_fail": "exclude", **changes,
    }


def test_strict_waits_for_gate_scores():
    with pytest.raises(ValueError, match="评分尚未完成"):
        compute_price_scores(_bids(), _formula(params_json={"benchmark_gate": _gate()}), strict=True)


def test_strict_all_gate_excluded_is_failure_not_fallback_benchmark():
    bids = [PriceBid(1, "甲", Decimal("100"), gate_value=Decimal("60"), gate_basis=Decimal("100"))]
    with pytest.raises(ValueError, match="排除全部报价"):
        compute_price_scores(bids, _formula(params_json={"benchmark_gate": _gate()}), strict=True)


def test_strict_explicit_fixed_score_when_all_below_gate():
    bids = [PriceBid(1, "甲", Decimal("100"), gate_value=Decimal("60"), gate_basis=Decimal("100"))]
    result = compute_price_scores(
        bids, _formula(params_json={"benchmark_gate": _gate(on_fail="half_max", half_max_ratio="50")}), strict=True,
    )
    assert result.benchmark is None
    assert result.results[0].price_score == Decimal("20")
    assert result.results[0].excluded_from_benchmark is True


def test_strict_gate_exclusion_preserves_bidder_score_and_audit_flag():
    bids = [
        PriceBid(1, "甲", Decimal("100"), gate_value=Decimal("60"), gate_basis=Decimal("100")),
        PriceBid(2, "乙", Decimal("120"), gate_value=Decimal("80"), gate_basis=Decimal("100")),
    ]
    result = compute_price_scores(bids, _formula(params_json={"benchmark_gate": _gate()}), strict=True)
    assert result.benchmark == Decimal("120")
    assert {row.bidder_id for row in result.results} == {1, 2}
    assert next(row for row in result.results if row.bidder_id == 1).excluded_from_benchmark is True
    assert next(row for row in result.results if row.bidder_id == 2).excluded_from_benchmark is False


def test_strict_missing_quote_is_failure_not_zero_score():
    with pytest.raises(ValueError, match="报价缺失或无效"):
        compute_price_scores([PriceBid(1, "甲", Decimal(0))], _formula(), strict=True)


def test_strict_zero_decimal_precision_is_respected():
    result = compute_price_scores(_bids(), _formula(rounding=0), strict=True)
    assert all(row.price_score.as_tuple().exponent == 0 for row in result.results)


def test_strict_complete_formula_matches_deterministic_legacy_arithmetic():
    strict = compute_price_scores(_bids(), _formula(), strict=True)
    legacy = compute_price_scores(_bids(), _formula())
    assert strict.benchmark == legacy.benchmark
    assert [(r.bidder_id, r.price_score) for r in strict.results] == [(r.bidder_id, r.price_score) for r in legacy.results]


def test_strict_sample_gate_requires_sample_verdict_even_above_composite_threshold():
    bids = [PriceBid(1, "甲", Decimal("100"), gate_value=Decimal("80"))]
    gate = _gate(gate_on="sample_or_composite", ratio_kind="absolute", min_ratio="32")
    with pytest.raises(ValueError, match="评分尚未完成"):
        compute_price_scores(bids, _formula(params_json={"benchmark_gate": gate}), strict=True)


def test_strict_sample_failure_excludes_quote_but_still_scores_bidder():
    bids = [
        PriceBid(1, "甲", Decimal("100"), gate_value=Decimal("80"), sample_pass=False),
        PriceBid(2, "乙", Decimal("120"), gate_value=Decimal("80"), sample_pass=True),
    ]
    gate = _gate(gate_on="sample_or_composite", ratio_kind="absolute", min_ratio="32")
    result = compute_price_scores(bids, _formula(params_json={"benchmark_gate": gate}), strict=True)
    assert result.benchmark == Decimal("120")
    assert {row.bidder_id for row in result.results} == {1, 2}
    assert {row.bidder_id for row in result.results if row.excluded_from_benchmark} == {1}


@pytest.mark.parametrize("missing_rate", ["deduct_rate_up", "deduct_rate_down"])
def test_strict_deviation_formula_does_not_default_missing_direction_to_zero(missing_rate):
    present_rate = "deduct_rate_down" if missing_rate == "deduct_rate_up" else "deduct_rate_up"
    with pytest.raises(ValueError, match="扣分率缺失"):
        compute_price_scores(
            _bids(), _formula(deduct_rate=None, params_json={present_rate: "0.5"}), strict=True,
        )


def test_strict_deviation_formula_accepts_explicit_zero_direction():
    result = compute_price_scores(
        _bids(), _formula(deduct_rate=None, params_json={"deduct_rate_down": "0", "deduct_rate_up": "0.5"}),
        strict=True,
    )
    assert next(row for row in result.results if row.bidder_id == 1).price_score == Decimal("40")


@pytest.mark.parametrize("method,params", [
    ("average_trimmed", {"trim_tiers": [[5, 1], [None, 2]]}),
    ("average_trimmed", {"trim_count": 1, "trim_threshold": 0}),
    ("average_trimmed_then_below", {"trim_count": 1, "trim_threshold": 0}),
])
def test_strict_trim_does_not_reduce_frozen_cut_when_too_few_quotes(method, params):
    with pytest.raises(ValueError, match="不能缩减剔除家数"):
        compute_price_scores(_bids(), _formula(benchmark_method=method, params_json=params), strict=True)
