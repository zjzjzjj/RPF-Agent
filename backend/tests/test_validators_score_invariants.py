# -*- coding: utf-8 -*-
"""评分不变式校验器测试（2026-11-18 利群商标条盒实证）。

- MATCH_WITH_SCORE（error）：match 带分值自相矛盾——match 不计分语义下该分值
  永远兑现不了（Σ 恒缺此分，7 条实证缺 15 分）；数据侧 normalize 自愈，
  形态判不出的由本检查阻断交人工。
- OBJECTIVE_AS_SUBJECTIVE（warn）：客观列落 subjective 代判（全有全无/形态
  不可判）的诚实披露通道。
- prompt 契约：match 措辞纠偏 + 权重表禁抽 + PROMPT_VERSION 随动。
"""
from __future__ import annotations

from app.rules.validators import run_validators


def _crit(**kw):  # noqa: ANN002
    base = {
        "code": "c1", "name": "测试项", "score_type": "count",
        "max_score": "2", "is_veto": False, "origin": "document",
        "source_quote": "原文引用", "params": {}, "levels": [],
        "category": "technical",
    }
    base.update(kw)
    return base


def _codes(crits):  # noqa: ANN002
    return {f.code for f in run_validators({"package": {"total_max": 100}, "criteria": crits})}


def test_match_with_score_is_error() -> None:
    codes = _codes([_crit(score_type="match", max_score="4")])
    assert "MATCH_WITH_SCORE" in codes


def test_match_without_score_not_flagged() -> None:
    codes = _codes([_crit(score_type="match", max_score=None)])
    assert "MATCH_WITH_SCORE" not in codes


def test_scoring_types_with_score_not_flagged() -> None:
    for st in ("count", "subjective", "formula"):
        codes = _codes([_crit(score_type=st, max_score="4")])
        assert "MATCH_WITH_SCORE" not in codes, st


def test_match_with_score_hint_offers_three_exits() -> None:
    findings = run_validators(
        {"package": {"total_max": 100}, "criteria": [_crit(score_type="match", max_score="4")]})
    f = next(x for x in findings if x.code == "MATCH_WITH_SCORE")
    assert f.severity == "error"
    hint = f.hint or ""
    for word in ("count", "清空分值", "停用"):
        assert word in hint, f"出口提示缺少：{word}"


def test_objective_as_subjective_warn_all_or_nothing() -> None:
    codes = _codes([_crit(
        score_type="subjective", max_score="10",
        params={"_subjectivity_hint": "objective", "_objective_form": "all_or_nothing"})])
    assert "OBJECTIVE_AS_SUBJECTIVE" in codes


def test_objective_as_subjective_warn_unparsed() -> None:
    codes = _codes([_crit(
        score_type="subjective", max_score="2",
        params={"_subjectivity_hint": "objective", "_objective_form": "unparsed_objective"})])
    assert "OBJECTIVE_AS_SUBJECTIVE" in codes


def test_objective_count_no_objective_as_subjective() -> None:
    codes = _codes([_crit(
        score_type="count",
        params={"_subjectivity_hint": "objective", "_objective_form": "existence"})])
    assert "OBJECTIVE_AS_SUBJECTIVE" not in codes


def test_quote_required_hint_mentions_weight_table() -> None:
    findings = run_validators(
        {"package": {"total_max": 100}, "criteria": [_crit(source_quote="")]})
    f = next(x for x in findings if x.code == "QUOTE_REQUIRED")
    assert "权重表" in (f.hint or "")
    assert "停用" in (f.hint or "")


def test_prompt_match_wording_fixed_and_version_bumped() -> None:
    from app import config
    from app.rules import prompts

    p = prompts.RULE_SYSTEM_PROMPT
    assert "match（满足条件得分）" not in p, "误导措辞必须移除（把计分行推向 match 的病根）"
    assert "match" in p and "不计分" in p
    assert "权重表" in p and "禁止抽成 criterion" in p
    assert config.llm_prompt_version() == "2026-11-19.1"


# --- ⑬ 分部覆盖（P2-11 类别缺项防线，测试12 样品整类缺失实证） ---------------

_PART_DOC = ("1、商务部分（8 分）\n2、技术部分（32分）\n"
             "3、报价部分（50分）\n4、样品部分（10分）")


def _base_crits():  # noqa: ANN202
    return [
        _crit(code="c1", name="供货业绩", category="commercial", max_score="8"),
        _crit(code="c2", name="技术方案", category="technical", max_score="32"),
        _crit(code="c3", name="报价得分", category="price",
              score_type="formula", max_score="50"),
    ]


def test_part_coverage_sample_missing_is_error() -> None:
    findings = run_validators(
        {"package": {"total_max": 100}, "criteria": _base_crits()},
        document_text=_PART_DOC)
    f = next((x for x in findings if x.code == "PART_COVERAGE"), None)
    assert f is not None, "样品部分声明了分值却零覆盖 → 必须报错"
    assert f.severity == "error"
    assert "样品" in f.message


def test_part_coverage_sample_present_passes() -> None:
    crits = _base_crits() + [
        _crit(code="c4", name="样品评审", category="technical", max_score="10")]
    findings = run_validators(
        {"package": {"total_max": 100}, "criteria": crits},
        document_text=_PART_DOC)
    assert "PART_COVERAGE" not in {f.code for f in findings}


def test_part_coverage_standard_part_missing_is_error() -> None:
    crits = [c for c in _base_crits() if c["category"] != "commercial"] + [
        _crit(code="c4", name="样品评审", category="technical", max_score="10")]
    findings = run_validators(
        {"package": {"total_max": 100}, "criteria": crits},
        document_text=_PART_DOC)
    f = next((x for x in findings if x.code == "PART_COVERAGE"), None)
    assert f is not None and "商务" in f.message


def test_part_coverage_skipped_without_document_text() -> None:
    findings = run_validators(
        {"package": {"total_max": 100}, "criteria": _base_crits()})
    assert "PART_COVERAGE" not in {f.code for f in findings}
