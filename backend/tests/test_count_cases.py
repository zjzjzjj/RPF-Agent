# -*- coding: utf-8 -*-
"""业绩/人员案例核验回归（2026-10-21）。

真实故障：T1.2（团队成员经验 6 分）四家全 0（真实 6/0/0/6）、c12（负责人经验
4 分）龙即 0（真实约 4）。根因链：
① `_llm_verify_cases` 只把全文选段的 15k 字符送模型，几百页标书里案例页进不了
   上下文；② `_sanitize_case` 把"要求文本影子"判法套在短清单上，误杀合法引文；
③ 只要 cases 非空就清空 unresolved，0 分成为正式结论（无法与"未定分"区分）；
④ `person_scope` 用字符串精确比较，"leader（项目负责人）"整项归零。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.facts import BidFacts

REQ_TEXT = "【证明材料】须提供合同复印件、发票复印件和业主证明（或验收材料），不提供或提供不全的不得分"


# ---------------------------------------------------------------------------
# _sanitize_case
# ---------------------------------------------------------------------------
def test_sanitize_flags_requirement_shadow() -> None:
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "某项目", "contract": True, "invoice": True, "acceptance": True,
        "quote": "须提供合同复印件、发票复印件和业主证明",  # 照抄要求文本
    }
    out = _sanitize_case(case, REQ_TEXT)
    assert out["sanitized"] == "requirement_shadow"
    assert not any(out[k] for k in ("contract", "invoice", "acceptance"))


def test_sanitize_keeps_legit_quote_with_entities() -> None:
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "浙江中烟门户运维", "contract": True, "invoice": True, "acceptance": False,
        "quote": "2023年6月签订，合同金额2,208,000元，服务期三年",
    }
    out = _sanitize_case(case, REQ_TEXT)
    assert out["sanitized"] == "ok"
    assert out["contract"] is True and out["invoice"] is True


def test_sanitize_short_quote_with_number_is_ok() -> None:
    from app.services.scoring_service import _sanitize_case

    out = _sanitize_case({"name": "x", "quote": "2024年6月", "contract": True}, REQ_TEXT)
    assert out["sanitized"] == "ok"


# ---------------------------------------------------------------------------
# 候选页取样
# ---------------------------------------------------------------------------
def test_case_candidate_pages_finds_deep_pages() -> None:
    from app.services.scoring_service import _case_candidate_pages

    pages = [(i, f"无关内容第{i}页" * 20, "text") for i in range(1, 200)]
    pages[157] = (158, "合同名称：某运维项目 甲方：浙江中烟 合同金额：2,208,000元", "text")
    batches = _case_candidate_pages(pages)
    sent = {no for batch in batches for no, _t in batch}
    assert 158 in sent, "案例页必须进入送审上下文（旧实现只看 15k 字符选段）"
    assert 157 in sent and 159 in sent, "±1 页扩展窗"


def test_case_candidate_pages_batches_by_budget() -> None:
    from app.services.scoring_service import _CASE_BATCH_CHARS, _case_candidate_pages

    big = "合同 甲方 乙方 " + "内容" * 4000  # 约 8k 字符/页
    pages = [(i, big, "text") for i in range(1, 12)]
    batches = _case_candidate_pages(pages)
    assert len(batches) >= 2
    for batch in batches:
        assert sum(len(t) for _no, t in batch) <= _CASE_BATCH_CHARS * 1.2


# ---------------------------------------------------------------------------
# role 归一
# ---------------------------------------------------------------------------
def test_normalize_role_variants() -> None:
    from app.engine.objective import _normalize_role

    assert _normalize_role({"role": "Leader"}) == "leader"
    assert _normalize_role({"role": "项目负责人"}) == "leader"
    assert _normalize_role({"role": "leader（项目负责人）"}) == "leader"
    assert _normalize_role({"role": "member"}) == "member"
    assert _normalize_role({"role": "团队成员"}) == "member"
    assert _normalize_role({"role": ""}) is None


# ---------------------------------------------------------------------------
# unresolved 三态
# ---------------------------------------------------------------------------
def _count_crit(**kw) -> dict:
    base = {
        "name": "项目团队成员（除项目负责人外）有类似项目经验",
        "max_score": Decimal("6"),
        "required_artifacts": "合同复印件、发票复印件、验收材料",
        "params": {"field": "performance", "per_item": "1.5", "max_items": "4",
                   "person_scope": "member"},
    }
    base.update(kw)
    return base


def test_score_count_all_cases_sanitized_is_unresolved() -> None:
    """有案例但引文全被降级 → 不写正式 0 分，标未定分。"""
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "member", "contract": False, "invoice": False,
         "acceptance": False, "quote": "", "sanitized": "requirement_shadow"},
        {"name": "B", "role": "member", "contract": False, "invoice": False,
         "acceptance": False, "quote": "", "sanitized": "quote_too_short"},
    ])
    result = score_count(crit, crit["params"], facts)
    assert result.unresolved == "cases_unverified"
    assert result.conflict is True


def test_score_count_legit_case_missing_material_scores_zero() -> None:
    """合法案例但材料缺失 → 0 分是真实评审结论（先数业绩 0 = 确无发票/验收）。"""
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "member", "contract": True, "invoice": False,
         "acceptance": False, "quote": "2023年6月签订合同，金额80万元", "sanitized": "ok"},
    ])
    result = score_count(crit, crit["params"], facts)
    assert result.unresolved is None
    assert result.score == Decimal("0")
    assert result.conflict is True


def test_score_count_role_variants_not_zeroed() -> None:
    """role 写成"团队成员"也必须按 member 计入（旧实现精确比较 "member"）。"""
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "团队成员", "contract": True, "invoice": True,
         "acceptance": True, "quote": "2023年合同，金额800000元", "sanitized": "ok"},
    ])
    result = score_count(crit, crit["params"], facts)
    assert result.score == Decimal("1.5")


def test_score_count_no_cases_stays_unresolved() -> None:
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    crit = _count_crit()
    result = score_count(crit, crit["params"], facts)
    assert result.unresolved == "field_missing"


# ---------------------------------------------------------------------------
# 材料清单解析（required_evidence 整句 → 清单）
# ---------------------------------------------------------------------------
def test_parse_required_artifacts_splits_clause() -> None:
    from app.engine.objective import parse_required_artifacts

    items = parse_required_artifacts(REQ_TEXT)
    assert "合同复印件" in items
    assert "发票复印件" in items
    assert any("验收" in x for x in items)
    assert not any("不得分" in x for x in items)


def test_parse_required_artifacts_without_marker() -> None:
    """required_evidence 没有【证明材料】标记时也要能解析出材料清单。"""
    from app.engine.objective import parse_required_artifacts

    items = parse_required_artifacts("须提供合同复印件、发票复印件以及认证平台查询结果截图")
    assert "合同复印件" in items and "发票复印件" in items
    assert any("截图" in x for x in items)
