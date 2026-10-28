# -*- coding: utf-8 -*-
"""确定性检查/计分参数推断（check_infer）——规则文本驱动，禁止项目拟合。"""
from __future__ import annotations

from decimal import Decimal

from app.rules.check_infer import infer_check, infer_count_params


def test_infer_count_params_fills_gaps_only():
    """只填缺失项：模型显式值优先。"""
    c = {
        "name": "投标人业绩实力",
        "description": "每提供 1 个类似项目业绩得 2 分，最高得 6 分",
        "source_quote": "近三年承担过类似项目，每个 2 分，满分 6 分",
        "required_evidence": "合同复印件、发票复印件",
        "max_score": Decimal("6"),
    }
    params = infer_count_params(c, {"per_item": "2"})  # max_items/field 缺
    assert params["per_item"] == "2"  # 不覆盖
    assert params["field"] == "performance"
    assert params["max_items"] == "3"  # max_score6 / per_item2


def test_infer_count_params_per_item_and_field_from_text():
    """「每提供一项…得 1 分」→ per_item=1；证书词 → certificates；人员词 → person_scope。"""
    c = {
        "name": "服务团队技术力量—特种作业操作证",
        "description": "团队成员具有特种作业操作证的每人得 1 分，最高不超 2 分",
        "source_quote": "", "required_evidence": "证书复印件",
    }
    params = infer_count_params(c, {})
    assert params["per_item"] == "1"
    assert params["field"] == "certificates"
    assert params["person_scope"] == "member"


def test_infer_count_params_generic_items_fallback():
    """通用按件计数（无业绩/证件对象）→ field=items，不得留空落「未知计数字段」。"""
    c = {
        "name": "服务保障情况—其它增值服务",
        "description": "每提供一项增值服务得 1 分，最高 3 分",
        "source_quote": "", "required_evidence": "",
    }
    params = infer_count_params(c, {})
    assert params["field"] == "items"
    assert params["per_item"] == "1"


def test_infer_check_certificate_presence():
    c = {"name": "投标人资质实力—电子与智能化工程专业承包及音视频集成工程能力证书",
         "description": "", "source_quote": ""}
    gen = infer_check(c)
    assert gen is not None
    check_type, params = gen
    assert check_type == "document_present"
    assert "证书" in params["document"]


def test_infer_check_commitment_phrase():
    c = {"name": "服务团队技术力量—承诺额外增加驻场人员", "description": "", "source_quote": ""}
    gen = infer_check(c)
    assert gen is not None
    check_type, params = gen
    assert check_type == "contains_phrase"
    assert params["phrase"] == "承诺额外增加驻场人员"  # 组名前缀已剥


def test_infer_check_undecidable_returns_none():
    """密封/对比类判不了 → None（调用方落 human_only），不许猜。"""
    assert infer_check({"name": "应急响应优于招标人要求响应时间一半",
                        "description": "", "source_quote": ""}) is None
    assert infer_check({"name": "签字盖章", "description": "", "source_quote": ""}) is None


def test_infer_check_level_requirement_not_document_present():
    """「资质等级/有效期」类**等级与有效性要求**不得生成 document_present——
    文本提及即 pass 是假通过（审查实锤）；交 human_only → LLM 语义裁决。"""
    for name in ("资质等级达到一级以上", "营业执照有效期满足要求", "资质被吊销或处于暂停期"):
        assert infer_check({"name": name, "description": "", "source_quote": ""}) is None, name


def test_infer_count_params_iso_mode_not_triggered_by_amounts():
    """含「120000 元」的证件项不得被误判 ISO 型（裸数字不进 ISO 判定）。"""
    from app.engine.objective import score_count
    from app.engine.facts import BidFacts

    facts = BidFacts(text="")
    facts.certificates = ["特种作业操作证", "职称证书"]
    crit = {
        "name": "服务团队证书", "max_score": Decimal("2"),
        "description": "每提供 1 个证书得 1 分，注册资本 120000 万元",
        "required_artifacts": "证书复印件",
    }
    result = score_count(crit, {"field": "certificates", "per_item": "1", "max_items": "2"}, facts)
    # 通用证件按清单总数计（2 个），而不是 ISO 口径的 0
    assert result.score == Decimal("2"), result.detail


def test_infer_count_tier_iso_system():
    """P3 分档计数：ISO体系「两项得1分、三项得2分」→ count_tier（run59 T5.2 根因）。"""
    from app.rules.check_infer import infer_count_params

    c = {
        "name": "投标人资质实力", "max_score": 4,
        "description": "投标人具有ISO9001质量管理、环境管理及职业健康安全管理体系认证、"
                        "信息技术服务认证两项得1分，三项得得2分",
        "source_quote": "", "required_evidence": "",
    }
    p = infer_count_params(c, {"field": "certificates", "min_count": "2"})
    assert p.get("engine") == "count_tier"
    assert p.get("tiers") == [{"min_count": 2, "score": "1"}, {"min_count": 3, "score": "2"}]


def test_score_count_tier_hits_highest():
    """分档计数引擎：计数命中满足的最高档，非 per_item 线性。"""
    from app.engine.facts import BidFacts
    from app.engine.objective import score_count_tier

    params = {"engine": "count_tier", "field": "certificates",
              "tiers": [{"min_count": 2, "score": "1"}, {"min_count": 3, "score": "2"}]}
    facts = BidFacts(text="")
    facts.certificates = ["ISO9001", "ISO14001", "ISO45001"]
    r = score_count_tier({"max_score": 4}, params, facts)
    assert r.score == 2  # 3 项命中「≥3」档

    facts2 = BidFacts(text="")
    facts2.certificates = ["ISO9001", "ISO14001"]
    r2 = score_count_tier({"max_score": 4}, params, facts2)
    assert r2.score == 1  # 2 项命中「≥2」档

    facts3 = BidFacts(text="")
    facts3.certificates = ["ISO9001"]
    r3 = score_count_tier({"max_score": 4}, params, facts3)
    assert r3.score == 0  # 1 项不满足任何档
