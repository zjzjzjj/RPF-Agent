# -*- coding: utf-8 -*-
"""P1-E 新计分原语：属性分档 / 偏离表逐条扣分 / 值档位 / 线性步进 + 参数推断。

规则文本阈值全部来自 fixture 原文（语料句式），投标人侧事实从 BidFacts.text
确定性抽取——单测离线跑，不依赖 LLM。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.facts import BidFacts
from app.engine.objective import (
    _extract_response_hours,
    score_attribute_tier,
    score_deviation_deduct_table,
    score_value_linear,
    score_value_tier,
)
from app.rules.check_infer import infer_engine_params


def _facts(text: str, **kw) -> BidFacts:
    return BidFacts(text=text, **kw)


# --- infer_engine_params（规则文本 → params，只认明示） ------------------------

def test_infer_deviation_deduct_table() -> None:
    text = "按《技术偏离表》响应情况评审：每缺失一条扣 2 分，每负偏离一条扣 1 分，正偏离不加分，最低 0 分。"
    p = infer_engine_params(text)
    assert p["engine"] == "deviation_deduct_table"
    assert p["per_missing"] == "2"
    assert p["per_negative"] == "1"


def test_infer_value_tier_response_hours() -> None:
    text = "承诺 2 小时内到现场得 3 分，4 小时内得 2 分，6 小时内得 1 分，超出不得分。"
    p = infer_engine_params(text)
    assert p["engine"] == "value_tier"
    assert p["field"] == "response_hours"
    assert p["tiers"] == [
        {"max_value": "2", "score": "3"},
        {"max_value": "4", "score": "2"},
        {"max_value": "6", "score": "1"},
    ]


def test_infer_value_linear_warranty() -> None:
    text = "满足 1 年质保期以外，每额外增加 12 个月质保期加 1 分，最高 2 分。"
    p = infer_engine_params(text)
    assert p["engine"] == "value_linear"
    assert p["field"] == "warranty_months"
    assert p["base"] == "12"
    assert p["step"] == "12"
    assert p["per"] == "1"
    assert p["cap"] == "2"


def test_infer_attribute_tier_years() -> None:
    text = "项目负责人具有 5 年及以上工作经验得 3 分，4 年及以上得 2 分，3 年及以上得 1 分。"
    p = infer_engine_params(text)
    assert p["engine"] == "attribute_tier"
    assert p["attr"] == "years"
    assert {"min": "5", "score": "3"} in p["tiers"]
    assert {"min": "3", "score": "1"} in p["tiers"]


def test_infer_none_for_ordinary_count() -> None:
    """普通业绩计数不误触发新原语（≥2 档句式才认）。"""
    assert infer_engine_params(
        "自 2021 年 1 月 1 日以来同类业绩每个得 1 分，最高 4 分，合同金额 100 万元以上。"
    ) == {}


# --- score_deviation_deduct_table ---------------------------------------------

DEV_TEXT = """三、技术偏离表
序号 | 招标要求 | 投标响应 | 偏离情况
1 | 过滤精度≤5μm | ≤5μm | 无偏离
2 | 流量≥100L/min | 80L/min | 负偏离
3 | 材质 316L | 未提供 | 负偏离
4 | 接口 DN50 | DN50 | 无偏离
"""


def test_deviation_deduct_counts_and_caps() -> None:
    c = {"max_score": Decimal("20")}
    params = {"engine": "deviation_deduct_table", "per_missing": "2", "per_negative": "1"}
    r = score_deviation_deduct_table(c, params, _facts(DEV_TEXT))
    # 4 行；missing=1（行3）→ 行3 同时命中"未提供"与"负偏离"按 missing 归类（neg 优先不重复）
    # missing 判定排除了命中 neg 的行 → missing=0？行3 含"负偏离"→归 neg。
    # 期望：negative={行2,行3}=2 → deduct=2×1=2 → 18
    assert r.score == Decimal("18")
    assert r.conflict is True
    assert r.unresolved is None


def test_deviation_no_table_unresolved() -> None:
    c = {"max_score": Decimal("20")}
    params = {"engine": "deviation_deduct_table", "per_missing": "2"}
    r = score_deviation_deduct_table(c, params, _facts("本投标文件无偏离表内容。"))
    assert r.unresolved == "no_facts"
    assert r.conflict is True


def test_deviation_missing_params_field_missing() -> None:
    r = score_deviation_deduct_table(
        {"max_score": Decimal("20")}, {"engine": "deviation_deduct_table"},
        _facts(DEV_TEXT),
    )
    # per 缺省=0 → 不扣分（参数未给视为不适用扣分），不是 field_missing
    assert r.unresolved is None
    assert r.score == Decimal("20")


# --- score_value_tier / score_value_linear ------------------------------------

HOURS_TEXT = "我方承诺：故障响应时间 2 小时内到现场，4 小时内恢复运行。"


def test_value_tier_hit_and_miss() -> None:
    params = {
        "engine": "value_tier", "field": "response_hours", "direction": "lower_better",
        "tiers": [
            {"max_value": "2", "score": "3"},
            {"max_value": "4", "score": "2"},
            {"max_value": "6", "score": "1"},
        ],
    }
    r = score_value_tier({"max_score": Decimal("3")}, params, _facts(HOURS_TEXT))
    assert r.score == Decimal("3")
    assert r.conflict is True

    # 8 小时 → 不落任何档 → 0 分 + conflict
    r2 = score_value_tier(
        {"max_score": Decimal("3")}, params,
        _facts("我方响应时间为 8 小时内到达。"),
    )
    assert r2.score == 0
    assert r2.conflict is True


def test_value_tier_no_value_unresolved() -> None:
    params = {
        "engine": "value_tier", "field": "response_hours",
        "tiers": [{"max_value": "2", "score": "3"}],
    }
    r = score_value_tier({"max_score": Decimal("3")}, params, _facts("无承诺文本"))
    assert r.unresolved == "no_facts"


def test_value_linear_warranty_capped() -> None:
    params = {
        "engine": "value_linear", "field": "warranty_months",
        "base": "12", "step": "12", "per": "1", "cap": "2",
    }
    # 3 年=36 月 → (36-12)/12=2 → 2 分（封顶 2）
    r = score_value_linear(
        {"max_score": Decimal("2")}, params,
        _facts("本项目提供 3 年免费保修服务。"),
    )
    assert r.score == Decimal("2")
    assert r.conflict is True
    # 1 年 → 0 步 → 0 分
    r2 = score_value_linear(
        {"max_score": Decimal("2")}, params,
        _facts("质保期：1 年。"),
    )
    assert r2.score == Decimal("0")
    # 5 年=60 月 → 4 步 → 4 分被 cap 2 封顶
    r3 = score_value_linear(
        {"max_score": Decimal("5")}, params,
        _facts("免费保修 5 年。"),
    )
    assert r3.score == Decimal("2")


# --- 承诺值抽取窗口化（2026-11-02 run77 c54 实证） ----------------------------

RUN77_FULL = (
    "商务技术偏离表\n"
    "应急服务：根据实际情况，在现场运维人员无法处理用户提出的问题时，"
    "派遣高级技术人员8 小时到达现场，24 小时之内系统恢复正常；\n"
    "…（中间 300 页正文）…\n"
    "2.11.4.2.服务承诺\n"
    "7.我公司承诺提供应急服务：根据实际情况，在系统发生驻场运维人员无法解决的"
    "重大故障时，派遣远程服务团队在4 小时到达现场，24 小时之内系统恢复正常。"
)
RUN77_WINDOW = (
    "2.11.4.2.服务承诺\n"
    "7.我公司承诺提供应急服务：根据实际情况，在系统发生驻场运维人员无法解决的"
    "重大故障时，派遣远程服务团队在4 小时到达现场，24 小时之内系统恢复正常。"
)

_TIERS = {
    "engine": "value_tier", "field": "response_hours", "direction": "lower_better",
    "tiers": [
        {"max_value": "4", "score": "4"},
        {"max_value": "5", "score": "3"},
        {"max_value": "6", "score": "2"},
        {"max_value": "7", "score": "1"},
    ],
}


def test_value_tier_window_beats_deviation_first_match() -> None:
    """run77 c54：偏离表"8小时"在前、承诺函"4小时"在后。窗口（承诺函页）
    必须选出 4 → 满分；旧首匹配拿走 8 → 0 分误判。"""
    r = score_value_tier(
        {"max_score": Decimal("4"), "value_window_text": RUN77_WINDOW},
        _TIERS, _facts(RUN77_FULL),
    )
    assert r.score == Decimal("4"), r.detail


def test_value_tier_no_window_context_priority() -> None:
    """无窗口时弃首匹配改语境排序：承诺同句的"4小时"胜过偏离表的"8小时"。"""
    r = score_value_tier({"max_score": Decimal("4")}, _TIERS, _facts(RUN77_FULL))
    assert r.score == Decimal("4"), r.detail


def test_value_tier_window_miss_falls_back_full_text() -> None:
    """窗口里没有值（索引指针解析错位）→ 回退全文，仍按语境取值。"""
    r = score_value_tier(
        {"max_score": Decimal("4"), "value_window_text": "窗口里只有培训计划没有承诺"},
        _TIERS, _facts(RUN77_FULL),
    )
    assert r.score == Decimal("4"), r.detail


def test_response_hours_divergent_values_visible() -> None:
    """多值分歧并列进片段（可复算），取语境最优。"""
    value, snippet = _extract_response_hours(RUN77_FULL)
    assert value == Decimal("4")
    assert "多值分歧" in snippet


def test_response_hours_labeled_preferred() -> None:
    """带标签值（响应时间：2 小时）语境分最高。"""
    value, _ = _extract_response_hours(
        "服务承诺书：我方承诺重大故障远程服务团队在4 小时到达现场。响应时间：2 小时。"
    )
    assert value == Decimal("2")


# --- score_attribute_tier -----------------------------------------------------

def test_attribute_tier_cert_name_differentiated() -> None:
    """逐证不同分（G7）：ITSS 1 分 / ISO27001 1 分 / 其余不计；按名称匹配。"""
    params = {
        "engine": "attribute_tier", "attr": "cert_name",
        "tiers": [
            {"match": "ITSS", "score": "1"},
            {"match": "ISO27001", "score": "1"},
            {"match": "CMMI3", "score": "2"},
        ],
    }
    facts = _facts("", certificates=["ITSS三级证书", "ISO27001:2018", "ISO9001", "CMMI3"])
    r = score_attribute_tier({"max_score": Decimal("4")}, params, facts)
    assert r.score == Decimal("4")  # 1+1+2（ISO9001 不命中）
    assert r.conflict is True


def test_attribute_tier_cert_capped() -> None:
    params = {
        "engine": "attribute_tier", "attr": "cert_name",
        "tiers": [{"match": "ITSS", "score": "3"}],
    }
    facts = _facts("", certificates=["ITSS三级", "ITSS二级"])
    r = score_attribute_tier({"max_score": Decimal("4")}, params, facts)
    assert r.score == Decimal("4")  # 3+3 封顶 4


def test_attribute_tier_person_years_no_facts() -> None:
    """人员年限口径缺 person_attributes → no_facts 不猜测（不是 0 分结论）。"""
    params = {
        "engine": "attribute_tier", "attr": "years",
        "tiers": [{"min": "5", "score": "3"}, {"min": "4", "score": "2"}],
    }
    r = score_attribute_tier({"max_score": Decimal("3")}, params, _facts("任意文本"))
    assert r.unresolved == "no_facts"


def test_attribute_tier_person_years_scoring() -> None:
    params = {
        "engine": "attribute_tier", "attr": "years",
        "tiers": [{"min": "5", "score": "3"}, {"min": "4", "score": "2"}],
    }
    facts = _facts("", person_attributes=[
        {"name": "张三", "years": "6"},
        {"name": "李四", "years": "4"},
    ])
    r = score_attribute_tier({"max_score": Decimal("6")}, params, facts)
    assert r.score == Decimal("5")  # 3 + 2
    assert r.unresolved is None


# --- R0 客观分路由执行（2026-11-03 run78）：双向防线 -------------------------
# 用友式（材料/承诺在不许清零）+ 德勤式（抽不到事实不许满额）。

def test_value_tier_window_promise_scores_full() -> None:
    """用友式：承诺函窗口「4小时内到达」→ 4 分（偏离表干扰值不再首匹配抢分）。"""
    from decimal import Decimal as D

    crit = {"max_score": D("4"),
            "value_window_text": "服务承诺函：我公司承诺远程服务团队在4 小时内到达现场。"}
    params = {"engine": "value_tier", "field": "response_hours",
              "direction": "lower_better",
              "tiers": [{"max_value": "4", "score": "4"}, {"max_value": "5", "score": "3"},
                        {"max_value": "6", "score": "2"}, {"max_value": "7", "score": "1"}]}
    facts = _facts("偏离表：响应时间 8 小时（通用条款）")  # 全文只有干扰值
    r = score_value_tier(crit, params, facts)
    assert r.score == D("4")  # 窗口优先于全文


def test_value_tier_no_promise_value_zero_no_facts() -> None:
    """德勤式：抽不到承诺值 → 0 + no_facts（不猜、不给印象分）。"""
    from decimal import Decimal as D

    crit = {"max_score": D("4")}
    params = {"engine": "value_tier", "field": "response_hours",
              "tiers": [{"max_value": "4", "score": "4"}]}
    facts = _facts("我公司提供周到的现场服务。")  # 无任何小时承诺
    r = score_value_tier(crit, params, facts)
    assert r.score == D("0")
    assert r.unresolved == "no_facts"


def test_attribute_tier_cert_level_scoring() -> None:
    """用友式：证书名带等级「（三级）」→ 命中「三级及以上」档 2 分。"""
    from decimal import Decimal as D

    from app.engine.facts import BidFacts

    crit = {"max_score": D("2")}
    params = {"engine": "attribute_tier", "attr": "cert_level",
              "tiers": [{"match_any": ["一级", "二级", "三级"], "score": "2"},
                        {"match_any": ["四级"], "score": "1"}]}
    facts = BidFacts(text="")
    facts.certificates = ["ITSS信息技术服务标准符合性证书（三级）"]
    r = score_attribute_tier(crit, params, facts)
    assert r.score == D("2")

    facts4 = BidFacts(text="")
    facts4.certificates = ["ITSS信息技术服务标准符合性证书（四级）"]
    r4 = score_attribute_tier(crit, params, facts4)
    assert r4.score == D("1")


def test_attribute_tier_cert_level_no_certs_zero_no_facts() -> None:
    """德勤式：无证书清单 → 0 + no_facts。"""
    from decimal import Decimal as D

    from app.engine.facts import BidFacts

    crit = {"max_score": D("2")}
    params = {"engine": "attribute_tier", "attr": "cert_level",
              "tiers": [{"match_any": ["三级"], "score": "2"}]}
    facts = BidFacts(text="")
    r = score_attribute_tier(crit, params, facts)
    assert r.score == D("0")
    assert r.unresolved == "no_facts"


def test_run_check_unknown_type_falls_through() -> None:
    """存量降级路径锁死：围串标功能下线后，冻结包里残留的 check_type
    （如 collusion_check）不得崩、不得误判——run_check fallthrough 一律
    unknown + need_human（转人工/LLM 裁决）。"""
    from app.engine.objective import run_check

    facts = BidFacts(text="")
    for stale_type in ("collusion_check", "some_future_check"):
        r = run_check(stale_type, {}, facts)
        assert r.status == "unknown", r
        assert r.need_human is True
        assert "未知检查类型" in r.message
