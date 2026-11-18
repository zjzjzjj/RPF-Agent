# -*- coding: utf-8 -*-
"""normalize_package 硬防线回归（2026-11-18 利群商标条盒实证）。

②b match 带分值归正：明示形态可判 → 归正计分项 + 留痕；判不出 → 不动
   （MATCH_WITH_SCORE 校验阻断交人工——不猜）。
②c 权重表假项退役：quote 空 + 权重表上下文 + 无计分语言 → retire + 留痕。
层指纹：ReviewRecord.reason 前缀与 stats 键名同其他层（skeleton 的
weight_table_skipped warning / compiler 的 weight_pruned 清单）互不相同，
摘除任一层对应断言必红。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import (
    Criterion,
    CriterionCheck,
    Document,
    DocumentPage,
    ReviewRecord,
    RulePackage,
)
from app.db.session import session_scope
from app.services import rule_service

WEIGHT_PAGE = "标段 产品名称 所占样品分\n1 利群(休闲细支)商标条盒 10分"


@pytest.fixture()
def seeded(client):  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "不变式自愈"}).json()["id"]
    with session_scope() as s:
        doc = Document(project_id=pid, kind="tender", original_filename="t.pdf",
                       stored_path="t.pdf", sha256="1" * 64, parse_status="parsed")
        s.add(doc)
        s.flush()
        s.add(DocumentPage(document_id=doc.id, page_no=125, text=WEIGHT_PAGE))
        pkg = RulePackage(project_id=pid, version=1, status="draft",
                          origin="document", source_document_id=doc.id,
                          created_by="tester")
        s.add(pkg)
        s.flush()
        # ① match 带分值（二值存在形态，可判 → 应归正 count）
        s.add(Criterion(package_id=pkg.id, code="T1", category="technical",
                        name="检测人员",
                        description="能提供检测人员配置方案的得1分，未提供的得0分。",
                        max_score=Decimal("1"), score_type="match", is_veto=False,
                        source_quote="能提供检测人员配置方案的得1 分，未提供的得0 分。",
                        origin="document", status="needs_input", sort_order=1))
        # ② match 带分值（形态不可判 → 不动，交 MATCH_WITH_SCORE 人工）
        s.add(Criterion(package_id=pkg.id, code="T2", category="technical",
                        name="神秘条款", description="综合评议后酌情给分。",
                        max_score=Decimal("3"), score_type="match", is_veto=False,
                        source_quote="综合评议后酌情给分。",
                        origin="document", status="needs_input", sort_order=2))
        # ③ 权重表假项（quote 空 + 权重上下文 → 退役）
        s.add(Criterion(package_id=pkg.id, code="T3", category="technical",
                        name="利群(休闲细支)商标条盒", description="",
                        max_score=Decimal("10"), score_type="subjective", is_veto=False,
                        source_quote=None,
                        origin="document", status="needs_input", sort_order=3))
        # ④ 真评分项（有引文 → 反例不动）
        s.add(Criterion(package_id=pkg.id, code="T4", category="commercial",
                        name="体系认证", description="一个得1分，合计3分。",
                        max_score=Decimal("3"), score_type="count", is_veto=False,
                        source_quote="具有管理体系认证证书且在有效期内，一个得1 分，合计3 分。",
                        origin="document", status="needs_input", sort_order=4))
        s.flush()
        return pid, pkg.id


def _crit_by_code(pkg_id: int, code: str) -> Criterion:
    with session_scope() as s:
        return s.execute(
            select(Criterion).where(
                Criterion.package_id == pkg_id, Criterion.code == code)
        ).scalar_one()


def test_normalize_fixes_match_with_score_and_retires_weight_row(seeded) -> None:  # noqa: ANN001
    """②b 归正可判形态 + ②c 退役权重假项 + 各自留痕（层指纹）。"""
    _pid, pkg_id = seeded
    stats = rule_service.normalize_package(pkg_id, actor="tester")
    assert stats["match_scored_fixed"] == 1
    assert stats["weight_rows_retired"] == 1

    c1 = _crit_by_code(pkg_id, "T1")
    assert c1.score_type == "count"
    with session_scope() as s:
        check = s.execute(
            select(CriterionCheck).where(CriterionCheck.criterion_id == c1.id)
        ).scalar_one()
        assert (check.params_json or {}).get("per_item") == "1"
        assert (check.params_json or {}).get("_objective_form") == "existence"
        recs = s.execute(
            select(ReviewRecord).where(ReviewRecord.subject_id == str(c1.id))
        ).scalars().all()
        assert any(r.reason.startswith("自动修复：判断项不得带分值") for r in recs)

    c3 = _crit_by_code(pkg_id, "T3")
    assert c3.status == "retired"
    with session_scope() as s:
        recs = s.execute(
            select(ReviewRecord).where(ReviewRecord.subject_id == str(c3.id))
        ).scalars().all()
        assert any(r.reason.startswith("自动修复：权重表") for r in recs)


def test_normalize_leaves_unparseable_match_and_quoted_item(seeded) -> None:  # noqa: ANN001
    """形态不可判的 match 不动（不猜）；有引文的真评分项不受波及。"""
    _pid, pkg_id = seeded
    rule_service.normalize_package(pkg_id, actor="tester")
    c2 = _crit_by_code(pkg_id, "T2")
    assert c2.score_type == "match", "形态不可判必须保持原样交人工"
    c4 = _crit_by_code(pkg_id, "T4")
    assert c4.status != "retired" and c4.score_type == "count"


def test_normalize_invariants_idempotent(seeded) -> None:  # noqa: ANN001
    """幂等：再跑一遍零新增（防重复退役/重复留痕）。"""
    _pid, pkg_id = seeded
    rule_service.normalize_package(pkg_id, actor="tester")
    stats = rule_service.normalize_package(pkg_id, actor="tester")
    assert stats["match_scored_fixed"] == 0
    assert stats["weight_rows_retired"] == 0
