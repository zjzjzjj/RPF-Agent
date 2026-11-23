# -*- coding: utf-8 -*-
"""评分类别确定性校正回归（2026-10-21）。

真实故障：招标文件「1、商务部分（10 分）」下的业绩/认证两条被 LLM 标成
category=technical，而排名/报告/前端全部按 category 分桶 → 商务桶恒 0
（真实商务分 10/10/7/1，系统一律 0）。类别不需要猜：source_node_id 指向的
节点标题就是「1、商务部分（10 分）」。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import (
    Criterion,
    DocNode,
    Document,
    ReviewRecord,
    RulePackage,
)
from app.db.session import session_scope
from app.rules import category_map
from app.services import rule_service


def test_category_from_titles_keywords() -> None:
    assert category_map.category_from_titles(["1、商务部分（10 分）"]) == "commercial"
    assert category_map.category_from_titles(["2、技术部分（40 分）"]) == "technical"
    assert category_map.category_from_titles(["3、报价部分（50 分）"]) == "price"
    assert category_map.category_from_titles(["资格审查文件"]) == "qualification"
    assert category_map.category_from_titles(["符合性审查"]) == "compliance"
    # 正文长句不参与（标题长度上限）
    assert category_map.category_from_titles(["本项目的商务部分包括业绩和认证两类评审因素，具体见下表"]) is None


def test_category_from_node_chain_uses_ancestor() -> None:
    titles = {
        "t:root": "招标文件",
        "t:root:C12": "评标办法",
        "t:root:C12:SEC6:I5": "五、评标细则",
        "t:root:C12:SEC6:I5:N1": "1、商务部分（10 分）",
    }
    assert category_map.category_from_node_chain(titles, "t:root:C12:SEC6:I5:N1") == "commercial"
    # 节点自身无标题 → 回退祖先
    assert category_map.category_from_node_chain(titles, "t:root:C12:SEC6:I5:N1:X9") == "commercial"


def test_category_from_node_chain_ignores_law() -> None:
    assert category_map.category_from_node_chain({"法条://a": "商务部分"}, "法条://a") is None


def test_category_from_quote_position() -> None:
    pages = [
        (97, "五、评标细则\n1、商务部分（10 分）\n序号 评审项目 分值 评审细则\n投标人自2022年1月1日起至今…"),
    ]
    cat = category_map.category_from_quote_position(
        pages, "投标人自2022年1月1日起至今", 97
    )
    assert cat == "commercial"


# ---------------------------------------------------------------------------
# normalize_package ⓪ 步：存量包自愈
# ---------------------------------------------------------------------------
@pytest.fixture()
def cat_pkg(client):  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "类别校正"}).json()["id"]
    with session_scope() as s:
        s.add(Document(project_id=pid, kind="tender", original_filename="t.pdf",
                       stored_path="t.pdf", sha256="1" * 64, parse_status="parsed"))
        s.flush()
        doc_id = s.execute(
            select(Document.id).where(Document.project_id == pid)
        ).scalar_one()
        s.add(DocNode(document_id=doc_id, node_id="t:root:C12:SEC6:I5",
                      kind="chapter", page=97, text="五、评标细则", title="五、评标细则", depth=3))
        s.add(DocNode(document_id=doc_id, node_id="t:root:C12:SEC6:I5:N1",
                      kind="item", page=97, text="1、商务部分（10 分）", title="1、商务部分（10 分）", depth=4))
        s.add(DocNode(document_id=doc_id, node_id="t:root:C12:SEC6:I5:N2",
                      kind="item", page=97, text="2、技术部分（40 分）", title="2、技术部分（40 分）", depth=4))
        pkg = RulePackage(project_id=pid, version=1, status="draft", origin="merged",
                          source_document_id=doc_id, created_by="tester")
        s.add(pkg)
        s.flush()
        pkg_id = pkg.id

        def add(code, name, cat, node, **kw):  # noqa: ANN001
            c = Criterion(package_id=pkg_id, code=code, category=cat, name=name,
                          description=name, score_type=kw.pop("score_type", "count"),
                          max_score=Decimal("6"), is_veto=False,
                          source_node_id=node, source_page=97, source_quote=name,
                          origin=kw.pop("origin", "document"), status="verified",
                          sort_order=kw.pop("sort_order", 1))
            s.add(c)
            s.flush()
            return c.id

        ids = {
            "commercial_wrong": add("T5.1", "投标人业绩", "technical",
                                    "t:root:C12:SEC6:I5:N1", sort_order=1),
            "technical_ok": add("T4.1", "需求理解", "technical",
                                "t:root:C12:SEC6:I5:N2", score_type="subjective", sort_order=2),
            "law": add("LAW-1", "法定否决条款", "compliance", "法条://某法/第一条",
                       score_type="veto", origin="law", sort_order=3),
        }
    return pid, pkg_id, ids


def test_normalize_corrects_category_with_trace(cat_pkg) -> None:  # noqa: ANN001
    _pid, pkg_id, ids = cat_pkg
    stats = rule_service.normalize_package(pkg_id, "auto")
    assert stats["category_corrected"] == 1

    with session_scope() as s:
        c = s.get(Criterion, ids["commercial_wrong"])
        assert c.category == "commercial"
        assert (c.edit_json or {}).get("category_from_node", {}).get("new") == "commercial"
        # 技术项与法条项不变
        assert s.get(Criterion, ids["technical_ok"]).category == "technical"
        assert s.get(Criterion, ids["law"]).category == "compliance"
        recs = s.execute(
            select(ReviewRecord).where(
                ReviewRecord.subject_type == "criterion",
                ReviewRecord.subject_id == str(ids["commercial_wrong"]),
                ReviewRecord.action == "edit",
            )
        ).scalars().all()
        assert recs and "章节标题" in recs[0].reason

    # 幂等：第二次不再改动
    stats2 = rule_service.normalize_package(pkg_id, "auto")
    assert stats2["category_corrected"] == 0


def test_normalize_respects_manual_edits(cat_pkg) -> None:  # noqa: ANN001
    _pid, pkg_id, ids = cat_pkg
    with session_scope() as s:
        s.add(ReviewRecord(
            subject_type="criterion", subject_id=str(ids["commercial_wrong"]),
            action="edit", actor="tester", reason="人工确认按技术分计",
        ))
    stats = rule_service.normalize_package(pkg_id, "auto")
    assert stats["category_corrected"] == 0
    with session_scope() as s:
        assert s.get(Criterion, ids["commercial_wrong"]).category == "technical"


def test_merge_pairs_after_category_correction(client) -> None:  # noqa: ANN001
    """类别错标的文档条目校正后，必须能与模板 commercial 条目配对（不再被弃用）。"""
    from app.db.models import CriterionCheck

    pid = client.post("/api/projects", json={"name": "合并配对"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/rules/instantiate",
                    json={"template_id": "comprehensive-default"})
    target_id = r.json()["package_id"]

    from app.db.models import Document as Doc
    with session_scope() as s:
        s.add(Doc(project_id=pid, kind="tender", original_filename="t.pdf",
                  stored_path="t.pdf", sha256="2" * 64, parse_status="parsed"))
        s.flush()
        doc_id = s.execute(select(Doc.id).where(Doc.project_id == pid)).scalar_one()
        s.add(DocNode(document_id=doc_id, node_id="t:root:C12:SEC6:I5:N1",
                      kind="item", page=97, text="1、商务部分（10 分）",
                      title="1、商务部分（10 分）", depth=4))
        merge = RulePackage(project_id=pid, version=2, status="draft",
                            merge_target_id=target_id, source_document_id=doc_id,
                            compiled_by="test", created_by="test")
        s.add(merge)
        s.flush()
        c = Criterion(package_id=merge.id, code="2.1", category="technical",
                      name="同类项目业绩", score_type="count", max_score=Decimal("6"),
                      is_veto=False, description="每提供一个得2分，最高6分",
                      source_node_id="t:root:C12:SEC6:I5:N1", source_page=97,
                      source_quote="具有信息系统运维案例", origin="document",
                      status="verified", sort_order=1)
        s.add(c)
        s.flush()
        s.add(CriterionCheck(criterion_id=c.id, check_type="count_at_least",
                             params_json={"field": "performance", "per_item": "2"},
                             on_fail="need_human"))
        merge_id = merge.id

    prop = client.get(f"/api/rules/packages/{merge_id}/match-proposal").json()
    paired = [
        p for p in prop["pairs"]
        if p["template"]["category"] == "commercial" and p["document"]["code"] == "2.1"
    ]
    assert paired, "类别校正后模板 commercial 条目应与文档业绩条目配对"
    assert paired[0]["document"]["category"] == "commercial"


# ---------------------------------------------------------------------------
# 不变式：score_type == "formula" ⇒ category = "price"（2026-11-11 烟丝仪器项目）
# 真实故障：报价得分挂「商务部分（0-50 分）」节点下被标 commercial，价格链路
# （enrich 正则补全 / PriceFormula 落库 / normalize 重解析）全按 category=="price"
# 过门被拦死 → PRICE_FORMULA_UNRECOGNIZED + 缺 benchmark_method。
# ---------------------------------------------------------------------------


def test_formula_forces_price_in_extracted(app_env) -> None:  # noqa: ANN001
    """编译期（enrich/落库之前）：formula 条目强制归 price，且幂等。"""
    from app.rules.model import ExtractedCriterion

    c = ExtractedCriterion(
        category="commercial",
        name="商务报价得分",
        score_type="formula",
        max_score=Decimal("50"),
        description="次低价为最佳报价值，每低1个百分点扣0.3分；每高1个百分点扣0.5分。",
        quote="次低价为最佳报价值",
    )
    changed = category_map.apply_category_to_extracted([c], 0)
    assert c.category == "price"
    assert changed == 1
    assert category_map.apply_category_to_extracted([c], 0) == 0  # 幂等


def test_plan_fixes_formula_forces_price_and_blocks_downgrade() -> None:
    """DB 权威修复（compile/normalize 后置）：公式项强制 price，
    且不被「商务部分」节点映射改回 commercial。"""
    base = dict(
        package_id=0,
        code="c30",
        name="商务报价得分",
        description="x",
        score_type="formula",
        max_score=Decimal("50"),
        is_veto=False,
        source_node_id="t:root:SEC6:I5:N3",
        source_page=101,
        source_quote="次低价为最佳报价值",
        origin="document",
        status="verified",
        sort_order=1,
    )
    titles = {"t:root:SEC6:I5:N3": "商务部分（0-50 分）"}

    wrong = Criterion(category="commercial", **base)
    fixes = category_map.plan_category_fixes([wrong], 1, titles, [])
    assert [(f[1], f[2], f[3]) for f in fixes] == [
        ("commercial", "price", "formula 类型强制归价格")
    ]

    ok = Criterion(category="price", **{**base, "code": "c31"})
    assert category_map.plan_category_fixes([ok], 1, titles, []) == []


# ---------------------------------------------------------------------------
# 多分部节点不作类别权威（2026-11-18 测试7-1 实证）
# ---------------------------------------------------------------------------
def test_node_part_categories_variants() -> None:
    assert category_map.node_part_categories(
        "1、商务部分（10 分）……2、技术部分（30 分）……") == {"commercial", "technical"}
    assert category_map.node_part_categories("1、商务部分（10 分）") == {"commercial"}
    assert category_map.node_part_categories("3、报价部分（60分）") == {"price"}
    assert category_map.node_part_categories("没有分部分值声明") == set()


def test_plan_fixes_skips_multi_part_node() -> None:
    """节点正文含多分部标记 → 标题不可作权威，保持 LLM 原类别。"""
    node_id = "t:root:C12:SEC6:I5:N1"
    crit = Criterion(
        package_id=0, code="c23", category="technical", name="项目重点、难点分析",
        description="技术评审", score_type="subjective", max_score=Decimal("6"),
        is_veto=False, source_node_id=node_id, source_page=89,
        source_quote="对本次招标项目运维服务工作的重点、难点进行分析",
        origin="document", status="verified", sort_order=1,
    )
    titles = {node_id: "1、商务部分（10 分）"}

    # 对照：不传 multi_part_nodes → 旧行为会按标题强改成 commercial
    fixes = category_map.plan_category_fixes([crit], 1, titles, [])
    assert [(f[1], f[2]) for f in fixes] == [("technical", "commercial")]

    # 修复后：多分部节点撤权，B 通道无页文本 → 不改
    assert category_map.plan_category_fixes(
        [crit], 1, titles, [], multi_part_nodes={node_id}) == []


def test_normalize_multi_part_node_keeps_llm_category(client) -> None:  # noqa: ANN001
    """落库路径：多分部节点下的技术条目在 normalize 后仍为 technical。"""
    from sqlalchemy import select

    from app.db.models import Criterion, DocNode, Document, RulePackage
    from app.db.session import session_scope
    from app.services import rule_service

    pid = client.post("/api/projects", json={"name": "多分部类别"}).json()["id"]
    node_id = "t:root:C12:SEC6:I5:N1"
    with session_scope() as s:
        s.add(Document(project_id=pid, kind="tender", original_filename="t.pdf",
                       stored_path="t.pdf", sha256="9" * 64, parse_status="parsed"))
        s.flush()
        doc_id = s.execute(
            select(Document.id).where(Document.project_id == pid)
        ).scalar_one()
        s.add(DocNode(
            document_id=doc_id, node_id=node_id, kind="item", page=88, depth=4,
            title="1、商务部分（10 分）",
            text="1、商务部分（10 分）……2、技术部分（30 分）……3、报价部分（60 分）"))
        pkg = RulePackage(project_id=pid, version=1, status="draft", origin="document",
                          source_document_id=doc_id, created_by="tester")
        s.add(pkg)
        s.flush()
        pkg_id = pkg.id
        c = Criterion(
            package_id=pkg_id, code="c23", category="technical", name="项目重点、难点分析",
            description="技术评审", score_type="subjective", max_score=Decimal("6"),
            is_veto=False, source_node_id=node_id, source_page=89,
            source_quote="对本次招标项目运维服务工作的重点、难点进行分析",
            origin="document", status="verified", sort_order=1)
        s.add(c)
        s.flush()
        crit_id = c.id

    stats = rule_service.normalize_package(pkg_id, "auto")
    assert stats["category_corrected"] == 0
    with session_scope() as s:
        assert s.get(Criterion, crit_id).category == "technical"


# --- 2026-11-23 样品分部归技术桶（测试12 利群 p119「4、样品部分（10分）」） -------

def test_category_from_titles_sample_part() -> None:
    assert category_map.category_from_titles(["4、样品部分（10 分）"]) == "technical"
    assert category_map.category_from_titles(["4、样品评审（10 分）"]) == "technical"
    # 裸"样品"不进词表：用户需求章节「九、投标样品要求」不是评分分部标题
    assert category_map.category_from_titles(["九、投标样品要求"]) is None


def test_category_from_quote_position_sample_section() -> None:
    pages = [
        (119, "3、报价部分（50分）\n3.1 计算报价的偏离情况\n4、样品部分（10分）\n"
              "序号 评审项目 分值 评审细则\n1 样品评审 0-10分"),
    ]
    cat = category_map.category_from_quote_position(pages, "1 样品评审 0-10分", 119)
    assert cat == "technical", "回扫最近标题必须命中样品部分，而非落回报价部分"


def test_node_part_categories_recognizes_sample() -> None:
    cats = category_map.node_part_categories(
        "2、技术部分（32分）……3、报价部分（50分）……4、样品部分（10分）")
    assert "technical" in cats and "price" in cats
