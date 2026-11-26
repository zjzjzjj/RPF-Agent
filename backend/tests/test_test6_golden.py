from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "test6_manual_scores.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_test6_golden_card_is_consistent() -> None:
    data = _load()
    assert len(data["bidder_order"]) == 5
    for values in data["manual_summary"].values():
        assert len(values) == 5
    for values in data["expected_objective"].values():
        assert len(values) == 5
    for values in data["manual_subjective"].values():
        assert len(values) == 5

    adjustment = data["independent_rule_adjustment"]
    for index in range(5):
        combined = Decimal(adjustment["manual_combined_c44_c45"][index])
        expected = Decimal(adjustment["expected_c44_plus_c45"][index])
        manual_total = Decimal(data["manual_summary"]["total"][index])
        assert Decimal(adjustment["adjusted_total"][index]) == manual_total - combined + expected


def test_test6_c44_c45_vectors_are_frozen() -> None:
    data = _load()
    assert data["expected_objective"]["c44"] == ["0", "3", "3", "3", "3"]
    assert data["expected_objective"]["c45"] == ["2", "2", "2", "0", "2"]


def test_test6_objective_targets_are_within_frozen_maxima() -> None:
    data = _load()
    maxima = {"c43": 4, "c44": 3, "c45": 2, "c48": 4, "c49": 6, "c50": 2, "c51": 3}
    for code, maximum in maxima.items():
        values = [Decimal(value) for value in data["expected_objective"][code]]
        assert all(0 <= value <= maximum for value in values), (code, values)


def test_test6_acceptance_thresholds_are_explicit() -> None:
    acceptance = _load()["acceptance"]
    assert acceptance["objective_exact"] is True
    assert Decimal(acceptance["subjective_max_abs_error"]) <= Decimal("0.50")
    assert Decimal(acceptance["subjective_mae"]) <= Decimal("0.30")
    assert Decimal(acceptance["adjusted_total_max_abs_error"]) <= Decimal("1.00")
    assert acceptance["ranking_exact"] is True
