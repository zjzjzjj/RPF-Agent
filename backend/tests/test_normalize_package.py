# -*- coding: utf-8 -*-
"""草稿规则包确定性自愈（normalize_package）与价格/子项修复的回归测试。

真实项目（PRJ-20261020-1H47）卡在 confirm/freeze 的三类错误：
- 6 条 LEVEL_BOTTOM_MISMATCH（模型只抽到一条"优 6-7"档位）
- 1 条 PRICE_PARAMS_INCOMPLETE（k 为空，且模板公式被文档不完整公式覆盖）
- 1 条 TOTAL_SCORE 106≠100（【证明材料】块被当成独立评分项）
这里用最小数据复现并钉住修复。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import (
    Criterion,
    CriterionCheck,
    CriterionLevel,
    DocNode,
    Document,
    PriceFormula,
    RulePackage,
)
from app.db.session import session_scope
from app.services import rule_service

PRICE_NODE_TEXT = """3、报价部分（50 分）
投标价格的合理性：分析投标价格是否合理，投标价格范围是否完整，有否重大错漏项，报价口径是否一致。
若有效投标人＞5个，则全部有效投标人最终报价扣除一个最高价和一个最低价后的算术平均值为最佳报价值；
若有效投标人≤5个，则全部有效投标人最终报价的算术平均值为最佳报价值。
每低1个百分点扣0.2分；每高1个百分点扣0.4分。扣完为止。
"""

TECH_NODE_TEXT = """2、技术部分（40 分）
根据投标人对本项目重点功能模块运维服务需求的理解，根据分析情况完整、与需求联系紧密等内容进行综合评分：
1  0-7  主观分
系统主要功能：协同办公（0-2 分）、行政管理（0-2 分）、党建工团（0-1 分）、移动企务应用（0-1 分）、管控中心（0-1 分）。
根据投标人对本项目系统运行环境，包括服务器操作系统、数据库、中间件的需求理解进行综合评分（0-3 分）。
"""


@pytest.fixture()
def seeded(client):  # noqa: ANN001
    """一个含三类缺陷的草稿包 + 一个法条条目（应保持不变）。"""
    pid = client.post("/api/projects", json={"name": "自愈测试"}).json()["id"]
    with session_scope() as s:
        s.add(Document(
            project_id=pid, kind="tender", original_filename="t.pdf",
            stored_path="t.pdf", sha256="0" * 64, parse_status="parsed",
        ))
        s.flush()
        doc_id = s.execute(
            select(Document.id).where(Document.project_id == pid)
        ).scalar_one()
        s.add(DocNode(
            document_id=doc_id, node_id="n-price", kind="item", page=99, depth=4,
            text=PRICE_NODE_TEXT,
        ))
        s.add(DocNode(
            document_id=doc_id, node_id="n-tech", kind="item", page=97, depth=4,
            text=TECH_NODE_TEXT,
        ))
        pkg = RulePackage(
            project_id=pid, version=1, status="draft", origin="merged",
            source_document_id=doc_id, created_by="tester",
        )
        s.add(pkg)
        s.flush()

        price = Criterion(
            package_id=pkg.id, code="T3.1", category="price", name="报价合理性",
            description="投标价格的合理性，按不含税投标总价进行评审",
            max_score=Decimal("50"), score_type="formula", is_veto=False,
            source_node_id="n-price", source_page=99,
            source_quote="投标价格的合理性：分析投标价格是否合理",
            origin="document", status="needs_input", sort_order=1,
        )
        s.add(price)
        s.flush()
        s.add(PriceFormula(
            package_id=pkg.id, criterion_id=price.id,
            formula_type="benchmark_ratio", benchmark_method="average",
            k=None, upper_bound=None, rounding=2, exclude_invalid=True,
        ))

        subj = Criterion(
            package_id=pkg.id, code="T4.1", category="technical", name="需求理解",
            description="根据投标人对本项目重点功能模块运维服务需求的理解进行综合评分",
            max_score=Decimal("7"), score_type="subjective", is_veto=False,
            source_node_id="n-tech", source_page=97,
            source_quote="根据投标人对本项目重点功能模块运维服务需求的理解，根据分析情况完整、与需求联系紧密等内容进行综合评分",
            origin="document", status="needs_input", sort_order=2,
        )
        s.add(subj)
        s.flush()
        # 模型发明的单条档位：最低下界 6 ≠ 0
        s.add(CriterionLevel(
            criterion_id=subj.id, level_label="优", score_min=Decimal("6"),
            score_max=Decimal("7"), description="优",
        ))

        count = Criterion(
            package_id=pkg.id, code="c29", category="technical", name="项目团队成员具有类似项目经验",
            description="项目团队成员有类似项目经验的，每提供一个案例得1.5 分，最高得6 分",
            max_score=Decimal("6"), score_type="count", is_veto=False,
            source_node_id="n-tech", source_page=97,
            source_quote="项目团队成员有类似项目经验的，每提供一个案例得1.5 分，最高得6 分",
            origin="document", status="needs_input", sort_order=3,
        )
        s.add(count)
        s.flush()
        s.add(CriterionCheck(
            criterion_id=count.id, check_type="count_at_least", field="performance",
            params_json={"field": "performance", "min_count": "1", "per_item": "1.5"},
            on_fail="need_human",
        ))

        # 【证明材料】块被单独抽成一条 veto 型条目（TOTAL_SCORE 106 的来源）
        mat = Criterion(
            package_id=pkg.id, code="c30", category="technical", name="项目团队成员证明材料",
            description="【证明材料】须提供项目团队成员的业绩证明文件、社保缴纳证明，否则该项不得分",
            max_score=Decimal("6"), score_type="veto", is_veto=False,
            source_node_id=None, source_page=97,
            source_quote="【证明材料】须提供项目团队成员的业绩证明文件、社保缴纳证明",
            origin="document", status="needs_input", sort_order=4,
        )
        s.add(mat)

        # 表头/结论行
        s.add(Criterion(
            package_id=pkg.id, code="T1.1", category="qualification",
            name="资格审查不通过", max_score=None, score_type="veto",
            is_veto=True, veto_consequence="disqualify",
            source_node_id=None, source_quote="资格审查不通过",
            origin="document", status="needs_input", sort_order=5,
        ))
        # 法条条目：normalize 不应改动
        s.add(Criterion(
            package_id=pkg.id, code="LAW-1", category="compliance",
            name="法定否决条款", max_score=None, score_type="match",
            is_veto=False, source_node_id="法条://某法/第一条",
            source_quote="法条原文", origin="law", status="verified", sort_order=6,
        ))
        package_id = pkg.id
    return pid, package_id


def test_normalize_repairs_level_formula_and_materials(seeded) -> None:  # noqa: ANN001
    _pid, package_id = seeded
    stats = rule_service.normalize_package(package_id, "tester")
    assert stats["levels_dropped"] == 1
    assert stats["price_repaired"] >= 1
    assert stats["materials_folded"] == 1
    assert stats["summary_retired"] == 1

    with session_scope() as s:
        by_code = {
            c.code: c
            for c in s.execute(
                select(Criterion).where(
                    Criterion.package_id == package_id
                )
            ).scalars().all()
        }
        assert by_code["c30"].status == "retired"
        assert "【证明材料】" in (by_code["c29"].required_evidence or "")
        assert by_code["T1.1"].status == "retired"
        assert by_code["LAW-1"].status == "verified"

        levels = s.execute(
            select(CriterionLevel).where(
                CriterionLevel.criterion_id == by_code["T4.1"].id
            )
        ).scalars().all()
        assert levels == []

        formula = s.execute(
            select(PriceFormula).where(
                PriceFormula.package_id == package_id
            )
        ).scalar_one()
        assert formula.formula_type == "deviation_deduct"
        assert formula.benchmark_method == "average_trimmed"
        assert Decimal(formula.params_json["deduct_rate_down"]) == Decimal("0.2")
        assert Decimal(formula.params_json["deduct_rate_up"]) == Decimal("0.4")
        assert formula.upper_bound == Decimal("50")

        count_check = s.execute(
            select(CriterionCheck).where(
                CriterionCheck.criterion_id == by_code["c29"].id
            )
        ).scalar_one()
        assert count_check.params_json.get("max_items") == "4"
        assert count_check.params_json.get("person_scope") == "member"

        # 描述补全：原文子项被追加进 T4.1
        assert "协同办公（0-2 分）" in (by_code["T4.1"].description or "")

    # 幂等：再跑一次不得重复追加/报错
    with session_scope() as s:
        c29 = s.execute(
            select(Criterion).where(
                Criterion.package_id == package_id, Criterion.code == "c29"
            )
        ).scalar_one()
        before = c29.required_evidence
    stats2 = rule_service.normalize_package(package_id, "tester")
    assert stats2["materials_folded"] == 0
    with session_scope() as s:
        c29 = s.execute(
            select(Criterion).where(
                Criterion.package_id == package_id, Criterion.code == "c29"
            )
        ).scalar_one()
        assert c29.required_evidence == before


def test_validators_ignore_veto_typed_scoring_item() -> None:
    """score_type=veto 但 is_veto=0 的 6 分条目不得计入非否决总分。"""
    from app.rules.validators import run_validators

    criteria = [
        {"code": "P1", "category": "price", "name": "价格", "max_score": "50",
         "score_type": "formula", "is_veto": False, "source_quote": "价格",
         "formula": {"formula_type": "benchmark_ratio", "benchmark_method": "average",
                     "k": "1"}},
        {"code": "S1", "category": "technical", "name": "技术", "max_score": "50",
         "score_type": "subjective", "is_veto": False, "source_quote": "技术",
         "levels": [{"label": "优", "min": "0", "max": "50", "criteria": "优"}]},
        {"code": "M1", "category": "technical", "name": "材料块", "max_score": "6",
         "score_type": "veto", "is_veto": False, "source_quote": "材料"},
    ]
    findings = run_validators({"package": {"total_max": 100}, "criteria": criteria})
    assert not any(f.code == "TOTAL_SCORE" for f in findings)


def test_price_text_parse_asymmetric_rates() -> None:
    from app.rules.compiler import parse_price_formula_text

    parsed = parse_price_formula_text(PRICE_NODE_TEXT)
    assert parsed["formula_type"] == "deviation_deduct"
    assert parsed["deduct_rate_down"] == Decimal("0.2")
    assert parsed["deduct_rate_up"] == Decimal("0.4")
    assert parsed["benchmark_method"] == "average_trimmed"
    assert parsed["trim_count"] == 1
    assert parsed["trim_threshold"] == 5


def test_price_upper_capped_at_max_and_zero_bid() -> None:
    from app.engine.models import PriceBid
    from app.engine.price import compute_price_scores

    formula = {
        "formula_type": "benchmark_ratio", "benchmark_method": "average",
        "k": "0.95", "max_score": "40", "upper_bound": "60", "rounding": 2,
    }
    bids = [
        PriceBid(1, "低", Decimal("100")),
        PriceBid(2, "高", Decimal("200")),
        PriceBid(3, "零", Decimal("0")),
    ]
    comp = compute_price_scores(bids, formula)
    by_id = {r.bidder_id: r for r in comp.results}
    assert by_id[1].price_score <= Decimal("40")
    assert by_id[3].price_score == Decimal("0")


def test_deviation_without_rates_raises() -> None:
    from app.engine.models import PriceBid
    from app.engine.price import compute_price_scores

    formula = {
        "formula_type": "deviation_deduct", "benchmark_method": "average",
        "k": "1", "max_score": "50", "params_json": {},
    }
    with pytest.raises(ValueError):
        compute_price_scores([PriceBid(1, "甲", Decimal("100"))], formula)


def test_benchmark_trim_one_only_above_threshold() -> None:
    from app.engine.price import compute_benchmark

    prices = [Decimal(str(x)) for x in (100, 101, 102, 103, 104, 130)]
    # n=6 > 5 → 各截 1 家；基准价 = (101+102+103+104)/4 = 102.5
    base, expl = compute_benchmark(
        prices, "average_trimmed", Decimal("1"), Decimal("0"),
        trim_count=1, trim_threshold=5,
    )
    assert base == Decimal("102.50")
    assert "去掉最高最低各 1 家" in expl

    # n=5 → 不截
    base5, expl5 = compute_benchmark(
        prices[:5], "average_trimmed", Decimal("1"), Decimal("0"),
        trim_count=1, trim_threshold=5,
    )
    assert base5 == Decimal("102.00")
    assert "不剔除" in expl5


def test_aggregate_subitems_overrides_model_score() -> None:
    from app.engine.subjective import aggregate_subitems

    criterion = {
        "description": "系统主要功能：协同办公（0-2 分）、行政管理（0-2 分）、党建工团（0-1 分）",
        "max_score": "5",
    }
    subs = [
        {"name": "协同办公", "score": 2, "max": 2},
        {"name": "行政管理", "score": 1, "max": 2},
        # 党建工团缺失 → 0
    ]
    score, normalized, warnings = aggregate_subitems(
        criterion, subs, Decimal("5"), Decimal("5")
    )
    assert score == Decimal("3")
    assert any("党建工团" in w for w in warnings)
    assert len(normalized) == 3


def test_aggregate_subitems_falls_back_when_no_name_matches() -> None:
    from app.engine.subjective import aggregate_subitems

    criterion = {"description": "协同办公（0-2 分）、行政管理（0-2 分）"}
    subs = [{"name": "完全不同的子项", "score": 4, "max": 4}]
    score, _normalized, _warnings = aggregate_subitems(
        criterion, subs, Decimal("4"), Decimal("4")
    )
    assert score == Decimal("4")  # 不因名称不一致整项归零


def test_quote_span_tolerates_table_columns_inside_sentence() -> None:
    """PDF 表格列文字会插进句子中间，整句匹配失败时退回首段匹配。"""
    from app.rules.compiler import _quote_span

    node = (
        "根据投标人对本项目重点功能模块运维服务需求的理解，提供详细完整、可行、"
        "合理的运维服务方3 运维方案 0-8 主观分案。投标人制定运维方案应包含以下内容："
        "运维服务目标（0-1 分）、运维服务计划（0-1 分）。"
    )
    quote = "根据投标人对本项目重点功能模块运维服务需求的理解，提供详细完整、可行、合理的运维服务方案"
    span = _quote_span(node, quote)
    assert span is not None
    assert span[2] is False  # 只匹配到前缀
    assert node[span[0]:].startswith("根据投标人")



def test_accept_criterion_can_verify_needs_input(seeded) -> None:  # noqa: ANN001
    """逐条「确认」必须能把 needs_input 置 verified（旧实现要求已 verified，必 422）。"""
    _pid, package_id = seeded
    with session_scope() as s:
        crit = s.execute(
            select(Criterion).where(
                Criterion.package_id == package_id, Criterion.code == "T4.1"
            )
        ).scalar_one()
        crit_id = crit.id
        assert crit.status == "needs_input"
    out = rule_service.accept_criterion(crit_id, "tester", "与招标文件原文核对一致")
    assert out.status == "verified"
    with session_scope() as s:
        again = s.get(Criterion, crit_id)
        assert again.status == "verified"
        assert again.edit_json and again.edit_json.get("accepted_from_status") == "needs_input"


def test_signoff_all_refreshes_validations(client) -> None:  # noqa: ANN001
    """「全部签批」后必须重跑校验，页面不能继续显示旧的 TEMPLATE_FALLBACK_SIGNOFF。"""
    pid = client.post("/api/projects", json={"name": "签批刷新"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/rules/instantiate",
                    json={"template_id": "comprehensive-goods"})
    assert r.status_code == 200, r.text
    package_id = r.json()["package_id"]
    before = client.get(f"/api/rules/packages/{package_id}/validations").json()
    assert any(v["code"] == "TEMPLATE_FALLBACK_SIGNOFF" for v in before)

    r = client.post(f"/api/rules/packages/{package_id}/signoff-all",
                    json={"reason": "模板兜底统一签批"})
    assert r.status_code == 200, r.text
    after = client.get(f"/api/rules/packages/{package_id}/validations").json()
    assert not any(v["code"] == "TEMPLATE_FALLBACK_SIGNOFF" for v in after)

