# -*- coding: utf-8 -*-
"""清单式多子项计分（P0-4③）：编号子项各自独立核验，单子项失败不连坐整格。

结构取自评标细则的常见清单写法（"1、…得X分 2、…得Y分 3、…各得Z分"）；数值为合成值，
不取任何项目的原文数值或答案（红线）。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.subjective import aggregate_subitems, apply_quote_gate, parse_subitems

D = Decimal

# 合成清单：子项 1 时限两档（上限取最高档 3 分），子项 2 固定 2 分，子项 3 "A及B的各得1分"（2 项 → 2 分）
# 三项上限之和 = 7，与满分一致 → 结构化；满分写 6 则口径不闭合 → 不结构化
CHECKLIST = (
    "根据对投标人提供的服务方案： 1、 接到用户故障通知后在线沟通无法解决，投标人在8小时内到达现场的得3分，"
    "接到用户故障通知后48小时内到达现场的得1分，其他不得分。须提供承诺函加盖投标人公章。 "
    "2、 投标人在招标人所在地设置有服务网点的得2分。须提供服务情况或承诺函。 "
    "3、提供投标产品使用说明书及使用简章的各得1分。"
)
CRITERION = {"name": "服务方案", "score_type": "subjective", "max_score": "7", "description": CHECKLIST}


def test_checklist_items_are_parsed_with_caps_summing_to_maximum():
    items = parse_subitems(CHECKLIST, D("7"))
    assert [cap for _name, cap in items] == [D("3"), D("2"), D("2")]
    assert len({name for name, _cap in items}) == 3


def test_checklist_not_structured_when_caps_do_not_sum_to_maximum():
    # 上限之和 7 ≠ 满分 6：分值口径不闭合 → 不结构化（歧义不猜，退回整项判读）
    assert parse_subitems(CHECKLIST, D("6")) == []
    # 未给满分时也不结构化
    assert parse_subitems(CHECKLIST) == []


def test_missing_subitem_scores_zero_only_for_itself():
    items = parse_subitems(CHECKLIST, D("7"))
    names = [name for name, _cap in items]
    sub_scores = [
        {"name": names[0], "score": "3", "quote": "8小时内到达现场"},
        # 子项 2 缺失（无证据）→ 只该子项计 0，不影响子项 1、3
        {"name": names[2], "score": "2", "quote": "使用说明书及使用简章"},
    ]
    total, normalized, warnings = aggregate_subitems(CRITERION, sub_scores, None, D("7"))
    assert total == D("5")
    missing = [item for item in normalized if item.get("evidence_state") == "missing"]
    assert len(missing) == 1 and missing[0]["name"] == names[1]
    assert any("未提供证据的子项按 0 计" in w for w in warnings)


def test_unverifiable_quote_zeroes_only_the_failing_subitem():
    items = parse_subitems(CHECKLIST, D("7"))
    names = [name for name, _cap in items]
    sub_scores = [
        {"name": names[0], "score": "3", "quote": "8小时内到达现场"},
        {"name": names[1], "score": "2", "quote": "引文不存在于投标文件"},
        {"name": names[2], "score": "2", "quote": "使用说明书及使用简章"},
    ]
    verified = {"8小时内到达现场", "使用说明书及使用简章"}
    total, warnings, executed = apply_quote_gate(sub_scores, lambda q: q in verified)
    assert executed is True
    assert total == D("5")  # 子项 1 与 3 保留，子项 2 因引文无法定位按 0
    assert sub_scores[1].get("gate_killed") is True
    assert "gate_killed" not in sub_scores[0] and "gate_killed" not in sub_scores[2]
    assert any("未在投标文件中定位到" in w for w in warnings)


def test_plain_fixed_score_subitems_still_use_the_existing_format():
    # 既有"名称（0-X 分）"形态不受清单解析影响
    legacy = "协同办公（0-2 分）、行政管理（0-3 分）"
    assert parse_subitems(legacy, D("5")) == [("协同办公", D("2")), ("行政管理", D("3"))]
