# -*- coding: utf-8 -*-
"""P1-E 新计分原语：属性分档 / 偏离表逐条扣分 / 值档位 / 线性步进 + 参数推断。

规则文本阈值全部来自 fixture 原文（语料句式），投标人侧事实从 BidFacts.text
确定性抽取——单测离线跑，不依赖 LLM。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.facts import BidFacts
from app.engine.objective import (
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
