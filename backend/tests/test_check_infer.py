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


# --- R0 客观分路由（2026-11-03 run78）：主观格的明示档表 → 客观引擎 ----------
# 双向防线：用友式（明示档表必须路由到确定性引擎）+ 德勤式（歧义不许路由）。

def test_infer_objective_route_service_promise_value_tier():
    """用友式：服务承诺「4小时内到达得4分,5小时得3分…」+「客观分」标注 → value_tier。"""
    from app.rules.check_infer import infer_objective_route

    c = {
        "name": "服务承诺",
        "description": "承诺远程服务团队在4 小时内到达现场得4 分,承诺5 小时内到达现场得3 分,"
                       "承诺6 小时内到达现场得2 分,承诺7 小时内到达现场得1 分，其他不得分。",
        "source_quote": "5  服务承诺  客观分 0-4 分  分,承诺7 小时内到达现场得1 分",
    }
    route = infer_objective_route(c)
    assert route is not None
    kind, params = route
    assert kind == "value_tier"
    assert params["field"] == "response_hours"
    assert [t["max_value"] for t in params["tiers"]] == ["4", "5", "6", "7"]
    assert [t["score"] for t in params["tiers"]] == ["4", "3", "2", "1"]


def test_infer_objective_route_service_promise_no_marker_closed_table():
    """闭合档表（所有「得N分」都被解析）同样可路由：无「客观分」字样也算客观形态。"""
    from app.rules.check_infer import infer_objective_route

    c = {
        "name": "服务承诺",
        "description": "承诺远程服务团队在4 小时内到达现场得4 分,承诺5 小时内到达现场得3 分,"
                       "承诺6 小时内到达现场得2 分,承诺7 小时内到达现场得1 分，其他不得分。",
        "source_quote": "",
    }
    assert infer_objective_route(c) is not None  # 闭合 → 路由


def test_infer_objective_route_cert_level_attribute_tier():
    """用友式：ITSS「四级得1分，三级及以上得2分」→ attribute_tier(cert_level)，
    「三级及以上」按语言学语义展开为 优于/等于三级 的显式列表。"""
    from app.rules.check_infer import infer_objective_route

    c = {
        "name": "投标人综合实力-ITSS信息技术服务标准符合性证书",
        "description": "投标人具有ITSS 信息技术服务标准符合性证书（业务领域：运行维护），"
                       "四级得1 分，三级及以上得2 分。",
        "source_quote": "投标人具有ITSS 信息技术服务标准符合性证书，四级得1 分，三级及以上得2 分。",
    }
    route = infer_objective_route(c)
    assert route is not None
    kind, params = route
    assert kind == "attribute_tier"
    assert params["attr"] == "cert_level"
    by_score = {t["score"]: set(t["match_any"]) for t in params["tiers"]}
    assert by_score["2"] == {"一级", "二级", "三级"}
    assert by_score["1"] == {"四级"}


def test_infer_objective_route_person_experience_tiers():
    """用友式：项目负责人「2个经验得1分；4个得2分」→ count(person_scope=leader)，
    计数源按档位短语量词后名词（经验）判定，防证书词抢 field。"""
    from app.rules.check_infer import infer_objective_route

    c = {
        "name": "项目负责人",
        "description": "项目负责人具有项目经理PMP 证书或工程师职称证书，且具有2 个及以上"
                       "信息系统项目实施经验，得1 分；项目负责人具有项目经理PMP 证书或"
                       "工程师职称证书，且具有4 个及以上信息系统项目实施经验，得2 分。",
        "source_quote": "客观分 0-2 分",
    }
    route = infer_objective_route(c)
    assert route is not None
    kind, params = route
    assert kind == "count"
    assert params.get("person_scope") == "leader"
    assert params["tiers"] == [{"min_count": 2, "score": "1"}, {"min_count": 4, "score": "2"}]


def test_infer_objective_route_cert_count_tier():
    """用友式：云平台证书「提供两项得1分，提供四项得2分」→ count_tier(certificates)。"""
    from app.rules.check_infer import infer_objective_route

    c = {
        "name": "投标人综合实力-云平台开发或运维经验及认证/培训证书",
        "description": "投标人具有相应云平台开发或运维经验，可提供运维团队具有相应云平台产品"
                       "认证或培训证书，提供两项得1 分，提供四项得2 分，未提供不得分。",
        "source_quote": "客观分 0-2 分",
    }
    route = infer_objective_route(c)
    assert route is not None
    kind, params = route
    assert kind == "count_tier"
    assert params["field"] == "certificates"
    assert params["tiers"] == [{"min_count": 2, "score": "1"}, {"min_count": 4, "score": "2"}]


def test_infer_objective_route_per_item_linear():
    """「每1人得1分，最高不超过3分」→ count(per_item=1, max_items=3)。"""
    from app.rules.check_infer import infer_objective_route

    c = {
        "name": "驻厂运维人员（每人计分）",
        "description": "驻厂运维人员具有云平台认证或培训证书并有项目实施经验，"
                       "同时具备以上条件的驻场运维人员每1 人得1 分，最高不超过3 分。",
        "source_quote": "客观分 0-3 分",
    }
    route = infer_objective_route(c)
    assert route is not None
    kind, params = route
    assert kind == "count"
    assert params["per_item"] == "1"
    assert params["max_items"] == "3"


def test_infer_objective_route_guard_single_tier_none():
    """德勤式守卫：只有 1 个「得N分」档 → 不路由（回退 subjective，歧义不猜）。"""
    from app.rules.check_infer import infer_objective_route

    c = {
        "name": "服务承诺",
        "description": "承诺4 小时内到达现场得4 分。",
        "source_quote": "客观分 0-4 分",
    }
    assert infer_objective_route(c) is None


def test_infer_objective_route_guard_unclosed_none():
    """德勤式守卫：档表被解析但文本另有散文计分提及（不闭合）且无客观标注 → 不路由。"""
    from app.rules.check_infer import infer_objective_route

    c = {
        "name": "需求理解",
        "description": "对需求理解深刻得3 分，理解较深刻得2 分；整体方案优秀的再酌情得1 分。",
        "source_quote": "",
    }
    assert infer_objective_route(c) is None


def test_infer_objective_route_guard_prose_none():
    """德勤式守卫：主观散文（区间档/无明示档表）→ None，绝不客观化。"""
    from app.rules.check_infer import infer_objective_route

    c = {
        "name": "系统维护服务方案",
        "description": "方案完整、可行得 8-10 分，较完整得 4-7 分，一般得 0-3 分。",
        "source_quote": "主观分 0-10 分",
    }
    assert infer_objective_route(c) is None


# --- A3 档表断行容错（2026-11-04）：OCR 断行不丢档，不放宽守卫 ----------------

def test_route_linebreak_variants_equal():
    """断行变体与无断行解析结果逐字段相等（小时档/证书等级档/计数档）。"""
    from app.rules.check_infer import infer_objective_route

    pairs = [
        # 响应时间档："小时\n内"断行
        ({"name": "服务承诺",
          "description": "承诺4 小时内到达现场得4 分,承诺5 小时内到达现场得3 分,"
                         "承诺6 小时内到达现场得2 分,承诺7 小时内到达现场得1 分。",
          "source_quote": "客观分 0-4 分"},
         {"name": "服务承诺",
          "description": "承诺4 小时\n内到达现场得4 分,承诺5 小时\n内到达现场得3 分,"
                         "承诺6 小时\n内到达现场得2 分,承诺7 小时\n内到达现场得1 分。",
          "source_quote": "客观分 0-4 分"}),
        # 证书等级档："三级\n及以上"断行（旧代码降级成单档 → ≥2 档守卫不通过）
        ({"name": "ITSS证书",
          "description": "具有ITSS 证书，四级得1 分，三级及以上得2 分。",
          "source_quote": "客观分 0-2 分"},
         {"name": "ITSS证书",
          "description": "具有ITSS 证书，四级得1 分，三级及\n以上得2 分。",
          "source_quote": "客观分 0-2 分"}),
        # 计数档："两项\n得1分"断行 + "得\n得"双得断行
        ({"name": "云平台证书",
          "description": "提供两项得1 分，提供四项得2 分，未提供不得分。",
          "source_quote": "客观分 0-2 分"},
         {"name": "云平台证书",
          "description": "提供两项\n得1 分，提供四项得\n得2 分，未提供不得分。",
          "source_quote": "客观分 0-2 分"}),
    ]
    for plain, broken in pairs:
        r_plain = infer_objective_route(plain)
        r_broken = infer_objective_route(broken)
        assert r_plain is not None, plain["name"]
        assert r_broken is not None, f"断行丢档：{broken['name']}"
        assert r_plain[0] == r_broken[0]
        assert r_plain[1] == r_broken[1], f"{plain['name']}：{r_plain[1]} != {r_broken[1]}"


def test_route_linebreak_years_tier():
    """年限档断行：「5 年\n及以上工作经验得 3 分」不丢档。"""
    from app.rules.check_infer import infer_engine_params

    text_plain = "具有5 年及以上工作经验得3 分，具有3 年及以上工作经验得1 分。"
    text_broken = "具有5 年\n及以上工作经验得3 分，具有3 年\n及以上工作经验得1 分。"
    p_plain = infer_engine_params(text_plain)
    p_broken = infer_engine_params(text_broken)
    assert p_plain.get("engine") == "attribute_tier", p_plain
    assert p_plain == p_broken


def test_route_linebreak_negative_still_none():
    """断行容错不放宽守卫：单档断行仍不路由（≥2 档门槛不变）。"""
    from app.rules.check_infer import infer_objective_route

    c = {
        "name": "服务承诺",
        "description": "承诺4 小时\n内到达现场得4 分。",
        "source_quote": "",
    }
    assert infer_objective_route(c) is None

