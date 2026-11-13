"""R0 客观路由的子句级闭合（P0-4c）。

多子项清单（编号子项各自计分）不得被某一个档位"借用"分值闭合，整条细则误路由成单档位。
用例数值均为合成数值，结构取自评标细则的常见形态，不取任何项目答案。
"""
from app.rules.check_infer import infer_objective_route


def _crit(text: str) -> dict:
    return {"name": "售后服务", "score_type": "subjective", "max_score": "6",
            "description": text, "source_quote": "", "required_evidence": ""}


MULTI_SUBITEM = (
    "根据对投标人提供的售后服务方案： 1、 接到用户故障通知后在线沟通无法解决，投标人在8小时内到达用户现场的得3 分，"
    "接到用户故障通知后48小时内到达用户现场的得2 分，其他不得分。须提供承诺函加盖投标人公章。 "
    "2、 投标人在招标人所在地设置有本地化服务的得3 分。须提供服务情况或承诺函加盖投标人公章。 "
    "3、提供投标产品使用说明书及使用简章的各得1 分。"
)


def test_multi_subitem_checklist_is_not_collapsed_into_a_single_hours_tier():
    # 响应时限档的分值 {3, 2} 与子项 2、3 的分值重合，但子项 2、3 的计分句不在任何档位子句内 → 不闭合
    assert infer_objective_route(_crit(MULTI_SUBITEM)) is None


def test_closed_hours_table_still_routes_to_value_tier():
    route = infer_objective_route(_crit("服务承诺：4小时内到达得4分，5小时内到达得3分，超过5小时不得分。"))
    assert route is not None and route[0] == "value_tier"


def test_objective_marker_does_not_bypass_an_uncovered_score_clause():
    # 「客观分」标注不能让未被档位覆盖的「得2分」计分句消失
    text = "客观分：4小时内到达得4分，5小时内到达得3分；另提供培训方案的得2分。"
    assert infer_objective_route(_crit(text)) is None


def test_per_item_cap_phrase_does_not_block_a_closed_route():
    # 「最高得N分」是封顶句，不是档位分值：不应使每件计数的闭合失败
    route = infer_objective_route(_crit("每提供1名负责人得1分，最高得3分。"))
    assert route is not None and route[0] == "count"
    assert route[1]["per_item"] == "1" and route[1]["max_items"] == "3"


# --- P0-4d：质保期线性步进的基线必须由本条细则原文明示 -----------------------------------

def test_warranty_step_with_base_only_in_tender_requirements_gets_no_plan():
    # "满足招标要求的质保期以外"的基线数值在招标技术要求中、不在本条细则原文内 → 不生成计划（歧义不猜）
    from app.rules.check_infer import infer_engine_params

    text = "满足招标要求的质保期以外的，每额外增加12 个月质保期加1 分，最高得2 分。"
    assert infer_engine_params(text) == {}
    assert infer_objective_route(_crit(text)) is None


def test_warranty_step_with_base_stated_in_same_rule_text_is_linear():
    from app.rules.check_infer import infer_engine_params

    text = "质保期不少于1年。满足招标要求的质保期以外的，每额外增加12 个月质保期加1 分，最高得2 分。"
    params = infer_engine_params(text)
    assert params["engine"] == "value_linear"
    assert params["field"] == "warranty_months"
    assert params["base"] == "12" and params["step"] == "12" and params["per"] == "1" and params["cap"] == "2"
