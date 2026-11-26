from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.auto_review import AutoReviewDecision, _validate_subjective_score
from app.llm.base import LLMError


ROOT = Path(__file__).resolve().parents[2]
INPUTS = Path(__file__).parent / "fixtures" / "test6_subjective_inputs.json"
MANUAL = Path(__file__).parent / "fixtures" / "test6_manual_scores.json"
DEFAULT_CALIBRATION = ROOT / "data" / "calibration" / "default.json"
RUN176 = Path(__file__).parent / "fixtures" / "test6_subjective_run176.json"


def _load_inputs() -> list[dict]:
    return json.loads(INPUTS.read_text(encoding="utf-8"))["cases"]


def _rule_for(case: dict) -> dict:
    sub_scores = case["sub_scores"]
    description = "；".join(
        f'{item["name"]}（0-{item["max"]}分）' for item in sub_scores
    )
    maximum = sum(Decimal(str(item["max"])) for item in sub_scores)
    return {
        "name": case["criterion"],
        "score_type": "subjective",
        "max_score": str(maximum),
        "description": description,
        "_subjective_direct": True,
    }


def _decision(items, evidence=None):
    return AutoReviewDecision(
        verdict="pass",
        reasoning="逐子项核验",
        score=None,
        evidence=evidence or [],
        sub_scores=items,
    )


RULE = {
    "name": "运维方案",
    "score_type": "subjective",
    "max_score": "6",
    "description": "服务目标（0-2分）；服务计划（0-2分）；巡检方案（0-2分）",
    "_subjective_direct": True,
}


def test_direct_subjective_requires_each_frozen_subitem_and_sums() -> None:
    decision = _decision([
        {"name": "服务目标", "score": "1.5"},
        {"name": "服务计划", "score": "1.0"},
        {"name": "巡检方案", "score": "2.0"},
    ])
    _validate_subjective_score(decision, RULE)
    assert decision.score == Decimal("4.5")
    assert decision.sub_score_calculation.startswith("1.5 + 1.0 + 2.0 = 4.5")


@pytest.mark.parametrize("items", [
    [{"name": "服务目标", "score": "1"}, {"name": "服务计划", "score": "1"}],
    [{"name": "服务目标", "score": "1"}, {"name": "服务计划", "score": "1"},
     {"name": "新增项", "score": "1"}],
])
def test_direct_subjective_rejects_missing_unknown_or_out_of_range_subitems(items) -> None:
    with pytest.raises(LLMError):
        _validate_subjective_score(_decision(items), RULE)


def test_direct_subjective_clamps_out_of_range_subitem_instead_of_failing() -> None:
    decision = _decision([
        {"name": "服务目标", "score": "3"}, {"name": "服务计划", "score": "1"},
        {"name": "巡检方案", "score": "1"},
    ])
    _validate_subjective_score(decision, RULE)
    assert decision.score == Decimal("4")


def test_direct_subjective_selects_evidence_for_every_subitem() -> None:
    from app.engine.auto_review import direct_subjective_pages

    pages = [
        {"document_id": 1, "page_no": 1, "text": "服务目标：明确量化目标和保障逻辑"},
        {"document_id": 1, "page_no": 2, "text": "服务计划：阶段划分、责任分工和进度管控"},
        {"document_id": 1, "page_no": 3, "text": "巡检方案：监控、巡检、事件分析和问题识别"},
        {"document_id": 1, "page_no": 4, "text": "无关的公司简介"},
    ]
    selected = direct_subjective_pages(RULE, pages)
    assert {(page["document_id"], page["page_no"]) for page in selected} >= {
        (1, 1), (1, 2), (1, 3),
    }


def test_test6_subitem_calibration_meets_manual_tolerance(monkeypatch) -> None:
    from app.engine import auto_review

    profile = json.loads(DEFAULT_CALIBRATION.read_text(encoding="utf-8"))
    monkeypatch.setattr(auto_review.config, "calibration_profile", lambda: profile)
    manual = json.loads(MANUAL.read_text(encoding="utf-8"))
    cases = _load_inputs()
    for code in ("c46", "c47"):
        expected = [Decimal(value) for value in manual["manual_subjective"][code]]
        actual = []
        for case, expected_value in zip(
            (item for item in cases if item["criterion"] == code), expected
        ):
            evidence = [
                {"evidence_id": f"R{index}"}
                for index in range(int(case["evidence_count"]))
            ]
            decision = _decision(case["sub_scores"], evidence)
            _validate_subjective_score(decision, _rule_for(case))
            actual.append(decision.score)
            assert decision.score is not None
        errors = [abs(got - want) for got, want in zip(actual, expected)]
        assert max(errors) <= Decimal("0.50"), (code, actual, expected)
        assert sum(errors, Decimal(0)) / Decimal(len(errors)) <= Decimal("0.30"), (code, actual, expected)


def test_calibration_uses_generic_features_without_bidder_answers() -> None:
    profile = json.loads(DEFAULT_CALIBRATION.read_text(encoding="utf-8"))
    payload = json.dumps(profile, ensure_ascii=False)
    manual = json.loads(MANUAL.read_text(encoding="utf-8"))
    forbidden = [item["name"] for item in manual["bidder_order"]]
    assert not any(name in payload for name in forbidden)
    assert "bidder_id" not in payload
    assert "fixed_scores" not in payload
    assert "expected_scores" not in payload
    models = profile["subjective_models"]
    assert models
    for model in models:
        assert "feature_order" in model
        assert "coefficients" in model
        assert "subitem_weights" not in model


def test_missing_quality_context_keeps_raw_subitem_sum(monkeypatch) -> None:
    from app.engine import auto_review

    profile = json.loads(DEFAULT_CALIBRATION.read_text(encoding="utf-8"))
    monkeypatch.setattr(auto_review.config, "calibration_profile", lambda: profile)
    case = next(item for item in _load_inputs() if item["criterion"] == "c46")
    items = [
        {"name": item["name"], "score": item["score"], "max": item["max"]}
        for item in case["sub_scores"]
    ]
    decision = _decision(items)
    _validate_subjective_score(decision, _rule_for(case))
    assert decision.score == sum(Decimal(item["score"]) for item in items)

    # Either side of the generic context missing is also a ratio-only fallback.
    decision = _decision(items, [{"evidence_id": "R0"}])
    _validate_subjective_score(decision, _rule_for(case))
    assert decision.score == sum(Decimal(item["score"]) for item in items)


def test_malformed_calibration_never_turns_into_zero(monkeypatch) -> None:
    from app.engine import auto_review

    profile = {
        "subjective_models": [{
            "maxima": ["1", "1", "2", "2", "2", "1"],
            "feature_order": ["raw_total"],
            "feature_mean": ["0"],
            "feature_scale": ["0"],
            "coefficients": ["not-a-number"],
            "intercept": "0",
        }],
    }
    monkeypatch.setattr(auto_review.config, "calibration_profile", lambda: profile)
    case = next(item for item in _load_inputs() if item["criterion"] == "c46")
    decision = _decision(case["sub_scores"])
    _validate_subjective_score(decision, _rule_for(case))
    assert decision.score == sum(Decimal(item["score"]) for item in case["sub_scores"])


def test_test6_second_run_variants_meet_manual_tolerance(monkeypatch) -> None:
    from app.engine import auto_review

    profile = json.loads(DEFAULT_CALIBRATION.read_text(encoding="utf-8"))
    monkeypatch.setattr(auto_review.config, "calibration_profile", lambda: profile)
    source = json.loads(INPUTS.read_text(encoding="utf-8"))["cases"]
    variants = json.loads(RUN176.read_text(encoding="utf-8"))["cases"]
    for code in ("c46", "c47"):
        template = next(item for item in source if item["criterion"] == code)
        rows = [item for item in variants if item["criterion"] == code]
        actual = []
        for row in rows:
            reasoning = "明确" * int(row["positive_terms"]) + "不足" * int(row["negative_terms"])
            items = [
                {"name": item["name"], "max": item["max"], "score": score,
                 "reasoning": reasoning if index == 0 else "逐子项档位核验"}
                for index, (item, score) in enumerate(
                    zip(template["sub_scores"], row["scores"])
                )
            ]
            evidence = [{"evidence_id": f"R{index}"}
                        for index in range(int(row["evidence_count"]))]
            decision = _decision(items, evidence)
            _validate_subjective_score(decision, _rule_for({"criterion": code, "sub_scores": items}))
            actual.append(decision.score)
        expected = [Decimal(row["expected"]) for row in rows]
        errors = [abs(got - want) for got, want in zip(actual, expected)]
        assert max(errors) <= Decimal("0.50"), (code, actual, expected)
        assert sum(errors, Decimal(0)) / Decimal(len(errors)) <= Decimal("0.30"), (code, actual, expected)
