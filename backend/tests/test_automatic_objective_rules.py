"""Managed objective scores follow the frozen eligible set and exact amount bands."""
from decimal import Decimal

import pytest

from app.engine.facts import BidFacts
from app.engine.objective import score_attribute_tier, score_count, score_count_tier


def _cert_rule(**changes):
    return {
        "name": "认证证书", "description": "ISO9001、ISO27001、ISO20000每项得1分，最高3分。",
        "max_score": "3", "verified_facts": {},
        "cert_backing": {"ISO9001": True, "ISO14001": True, "ISO27001": True, "ISO20000": True},
        **changes,
    }


@pytest.mark.parametrize("rule_field", [
    "name", "description", "source_quote", "required_evidence", "required_artifacts", "required_artifacts_raw",
])
def test_managed_count_filters_unrelated_iso_against_every_frozen_rule_field(rule_field):
    rule = _cert_rule(name="认证证书", description="")
    rule[rule_field] = "ISO9001认证证书"
    got = score_count(rule, {"field": "certificates", "per_item": "1"},
                      BidFacts(text="", certificates=["ISO14001"]))
    assert got.score == 0
    assert got.unresolved is None


@pytest.mark.parametrize("rule_text", [
    "ISO9001、27001、20000每项1分", "ISO/IEC 9001、ISO/IEC 27001、ISO/IEC 20000每项1分",
    "GB/T19001、GB/T22080、GB/T24405每项1分",
    "质量管理体系、信息安全管理体系、信息技术服务管理体系每项1分",
])
def test_managed_iso_aliases_and_compact_lists_keep_only_named_families(rule_text):
    got = score_count(_cert_rule(description=rule_text), {"field": "certificates", "per_item": "1"},
                      BidFacts(text="", certificates=["ISO9001", "ISO27001", "ISO14001"]))
    assert got.score == Decimal("2")


def test_managed_cmmi_only_does_not_count_unrelated_iso():
    got = score_count(_cert_rule(name="CMMI认证证书", description="CMMI认证每项1分"),
                      {"field": "certificates", "per_item": "1"},
                      BidFacts(text="", certificates=["ISO14001"]))
    assert got.score == 0


def test_managed_source_quote_keeps_distinct_frozen_iso_and_cmmi_rates():
    rule = _cert_rule(
        name="认证证书", description="",
        source_quote="ISO9001认证每项1分；CMMI认证得2分。",
        cert_backing={"ISO9001": True, "CMMI": True},
    )
    got = score_count(rule, {"field": "certificates", "per_item": "1"},
                      BidFacts(text="", certificates=["ISO9001", "CMMI"]))
    assert got.score == Decimal("3")


def test_legacy_certificate_count_remains_unchanged():
    rule = _cert_rule()
    rule.pop("verified_facts")
    got = score_count(rule, {"field": "certificates", "per_item": "1"},
                      BidFacts(text="", certificates=["ISO14001"]))
    assert got.score == Decimal("1")


def test_managed_certificate_tier_filters_unrelated_iso_before_counting():
    got = score_count_tier(_cert_rule(), {"field": "certificates", "tiers": [{"min_count": 1, "score": "3"}]},
                           BidFacts(text="", certificates=["ISO14001"]))
    assert got.score == 0
    assert got.unresolved is None


def test_managed_certificate_attribute_count_filters_unrelated_iso():
    got = score_attribute_tier(_cert_rule(), {"attr": "count", "tiers": [{"score": "1"}]},
                               BidFacts(text="", certificates=["ISO14001"]))
    assert got.score == 0


AMOUNT_TIERS = [
    {"min_amount_yuan": "500000", "max_amount_yuan": "1000000", "per_item": "0.5"},
    {"min_amount_yuan": "1000000", "per_item": "1"},
]


def _amount_score(amounts, *, managed=True, tiers=None, **param_changes):
    cases = [{"name": f"项目{i}", "amount_yuan": amount, "sanitized": "ok"}
             for i, amount in enumerate(amounts)]
    rule = {"name": "投标人业绩", "max_score": "10", "verified_cases": cases}
    if managed:
        rule["verified_facts"] = {}
    params = {"field": "performance", "per_item": "9", "tiers": tiers or AMOUNT_TIERS, **param_changes}
    facts = BidFacts(text="", performance_amounts=[Decimal(str(value)) for value in amounts if value is not None])
    return score_count(rule, params, facts)


def test_managed_amount_bands_are_calculated_per_case_without_linear_fallback():
    got = _amount_score(["600000", "1200000"])
    assert got.score == Decimal("1.5")
    assert "逐案例金额分档" in got.detail
    assert got.unresolved is None


def test_managed_amount_boundary_uses_inclusive_lower_and_exclusive_upper():
    assert _amount_score(["999999.99"]).score == Decimal("0.5")
    assert _amount_score(["1000000"]).score == Decimal("1")
    assert _amount_score(["499999.99"]).score == 0


def test_managed_amount_boundary_respects_explicit_inclusion_flags():
    tiers = [
        {**AMOUNT_TIERS[0], "max_inclusive": True},
        {**AMOUNT_TIERS[1], "min_inclusive": False},
    ]
    assert _amount_score(["1000000"], tiers=tiers).score == Decimal("0.5")


def test_managed_amount_bands_apply_frozen_amount_threshold():
    assert _amount_score(["600000", "1200000"], min_amount_yuan="1000000").score == Decimal("1")


def test_managed_amount_cap_keeps_highest_scoring_eligible_cases():
    assert _amount_score(["600000", "1200000"], max_items=1).score == Decimal("1")


@pytest.mark.parametrize("amount", [None, "-1", "NaN", "Infinity"])
def test_managed_missing_or_invalid_case_amount_cannot_become_a_final_score(amount):
    with pytest.raises((ValueError, ArithmeticError)):
        _amount_score([amount])


def test_managed_overlapping_amount_bands_cannot_choose_a_convenient_rate():
    tiers = [AMOUNT_TIERS[0], {"min_amount_yuan": "900000", "per_item": "1"}]
    with pytest.raises(ValueError, match="重叠"):
        _amount_score(["950000"], tiers=tiers)


def test_managed_amount_bands_require_individual_cases():
    got = score_count({"name": "投标人业绩", "max_score": "10", "verified_facts": {}},
                      {"field": "performance", "tiers": AMOUNT_TIERS, "per_item": "9"},
                      BidFacts(text="", performance_amounts=[Decimal("1200000")]))
    assert got.unresolved == "no_facts"


def test_legacy_amount_band_interpretation_remains_unchanged():
    assert _amount_score(["600000", "1200000"], managed=False, per_item="1").score == Decimal("2")


@pytest.mark.parametrize("engine", ["count", "count_tier"])
def test_automatic_executor_accepts_complete_amount_bands_and_recomputes_them(engine):
    from app.engine.auto_review import AutoReviewDecision
    from app.services.automatic_scoring_service import _verified_count

    rule = {
        "name": "投标人业绩", "score_type": "count", "max_score": "10",
        "description": "每项按金额档计分，50万元至100万元0.5分，100万元及以上1分。",
        "params": {"engine": engine, "field": "performance", "per_item": "9", "tiers": AMOUNT_TIERS},
    }
    decision = AutoReviewDecision(
        verdict="pass", reasoning="已核验全部案例的实际金额", coverage_complete=True,
        facts={"performance_amounts": ["600000", "1200000"], "verified_cases": [
            {"name": "甲项目", "amount_yuan": "600000"},
            {"name": "乙项目", "amount_yuan": "1200000"},
        ]},
    )
    score, explanation, _ = _verified_count(rule, decision, [{"text": "已核验的完整投标材料"}])
    assert score == Decimal("1.5")
    assert "逐案例金额分档" in explanation
