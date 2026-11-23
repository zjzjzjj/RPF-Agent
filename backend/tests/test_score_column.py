# -*- coding: utf-8 -*-
"""二轴制数据层：文件"主/客观分"列值持久化（2026-11-23 显示口径二轴制）。

口径轴严格取评标文件"主/客观分"列值（score_column），来源（score_column_source）
分 file（骨架列解析，权威）/ llm_copy（LLM 照抄）/ inferred（措辞推断，不作显示
来源）/ None（无列不猜）。本文件锁住三条产线与复制链。
"""
from __future__ import annotations

from decimal import Decimal

from app.rules.compiler import enrich_extracted
from app.rules.model import ExtractedCriterion
from app.rules.reconcile import _generate_for_row, _score_type_for_row
from app.rules.scoring_skeleton import SkeletonRow


def _row(subjectivity, text="样品外观综合评价 0-10分", **kw):  # noqa: ANN001, ANN202
    base = dict(group="样品评审", group_total=Decimal("10"), sub_name=None, sub_idx=None,
                subjectivity=subjectivity, max_score=Decimal("10"),
                text=text, page_no=123, required_evidence=None)
    base.update(kw)
    return SkeletonRow(**base)


def test_generate_for_row_writes_file_column() -> None:
    c = _generate_for_row(_row("subjective"), {}, "technical")
    assert c.score_column == "subjective"
    assert c.score_column_source == "file"
    c2 = _generate_for_row(_row("objective"), {}, "technical")
    assert c2.score_column == "objective"
    assert c2.score_column_source == "file"


def test_generate_for_row_no_column_is_none() -> None:
    c = _generate_for_row(_row(None), {}, "technical")
    assert c.score_column is None and c.score_column_source is None


def test_enrich_llm_copy_vs_inferred() -> None:
    # LLM 照抄列值 → llm_copy
    c = ExtractedCriterion(category="technical", name="项目负责人", score_type="subjective",
                           subjectivity_hint="objective")
    enrich_extracted([c], "")
    assert (c.score_column, c.score_column_source) == ("objective", "llm_copy")

    # 量化措辞推断 → inferred（不作显示来源）
    c2 = ExtractedCriterion(category="technical", name="驻场承诺", score_type="subjective",
                            description="每提供1个成功案例得 2 分")
    enrich_extracted([c2], "")
    assert (c2.score_column, c2.score_column_source) == ("objective", "inferred")

    # 无 hint 无措辞 → 不动
    c3 = ExtractedCriterion(category="technical", name="需求理解", score_type="subjective",
                            description="综合评价理解深度")
    enrich_extracted([c3], "")
    assert c3.score_column is None


def test_enrich_never_overwrites_existing_column() -> None:
    c = ExtractedCriterion(category="technical", name="项目负责人", score_type="subjective",
                           subjectivity_hint="objective",
                           score_column="subjective", score_column_source="file")
    enrich_extracted([c], "")
    assert (c.score_column, c.score_column_source) == ("subjective", "file")


def test_score_type_for_row_subjective_column() -> None:
    st, params = _score_type_for_row(_row("subjective"))
    assert st == "subjective"
    assert params.get("_subjectivity_hint") == "subjective"


def test_copy_chain_fields_registered() -> None:
    # new_version 复制链的字段清单必须含两列（漏一处即丢口径）
    from app.services.rule_service import _CRIT_FIELDS
    assert "score_column" in _CRIT_FIELDS
    assert "score_column_source" in _CRIT_FIELDS


# ---- 二轴投影（特例口径表逐行锁定） -----------------------------------------

def test_scoring_basis_of_table() -> None:
    from app.services.score_summary_service import scoring_basis_of

    assert scoring_basis_of("subjective", "file") == "主观"
    assert scoring_basis_of("objective", "file") == "客观"
    assert scoring_basis_of("objective", "llm_copy") == "客观"
    assert scoring_basis_of("objective", "inferred") == "—", "措辞推断不作显示来源"
    assert scoring_basis_of("subjective", None) == "—"
    assert scoring_basis_of(None, None) == "—"


def test_scoring_method_of_table() -> None:
    from app.services.score_summary_service import scoring_method_of as m

    assert m("subjective") == "AI评审"
    assert m("subjective", {"_objective_form": "all_or_nothing"}) == "AI评审"
    assert m("subjective", {"_objective_form": "unparsed_objective"}) == "AI评审"
    assert m("count") == "混合（AI核验事实+规则计数）"
    assert m("count", {}, ai_raw={"presence": {"state": "present"}}) == "规则评审"
    assert m("formula") == "规则评审"
    assert m("subjective", {"_objective_route": "value_tier"}) == "混合（AI核验事实+规则计数）"
    assert m("veto", verdict_source="llm") == "AI评审"
    assert m("veto") == "规则评审"
    assert m("match", verdict_source="llm") == "AI评审"
