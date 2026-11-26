# -*- coding: utf-8 -*-
"""片段覆盖对账（P0）：同一骨架行被 LLM 拆成多条子句 criterion。

真实病例（2026-11-18 测试7-1 一体化运维平台维保，PRJ-20261118-RGX5）：
骨架第 0 行「投标人实力 0-3 分（ISO27001/ISO20000/ISO9000 每证 1 分）」被抽成
三条 1 分子项；`_covers` 要求 40 字符切片命中，三条引文都太短 → 判未覆盖 →
缺口补齐又生成 3 分父项 → Σ=103。修复只在片段集合恰好闭合行满分时登记覆盖；
其余情形只披露、不改数（fail-closed）。
"""
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.facts import BidFacts
from app.engine.objective import score_count
from app.rules.check_infer import extract_tender_submission_date
from app.rules.model import ExtractedCriterion
from app.rules.reconcile import (
    _fragment_probes,
    _union_coverage,
    reconcile_explicit_count_allocations,
    reconcile_with_skeleton,
)
from app.rules.scoring_skeleton import ScoreSkeleton, SkeletonRow, parse_skeleton

YITIHUA_FX = Path(__file__).parent / "fixtures" / "scoring_skeleton" / "yitihua_weiyun_2026"

ISO_NAMES = ["信息安全管理体系认证", "信息技术服务管理体系认证", "质量管理体系认证"]
ISO_QUOTES = [
    "具有ISO27001 信息安全管理体系认证证书得1 分；",
    "具有ISO20000 信息技术服务管理体系认证得1 分；",
    "具有ISO9000 质量管理体系认证得1 分。",
]
ISO_ALLOCATION = (
    "投标人具有有效期内的ISO9001 质量管理体系认证、ISO27001信息安全管理体系认证、"
    "ISO20000 信息技术服务管理体系认证证书的，每项得1 分。最高得3 分。"
)
CMMI_ALLOCATION = "具有有效期内CMMI 认证二级或CSMM 认证二级及以上得2 分。"
SHARED_PROOF = (
    "以上资质均提供有效证书复印件并加盖公章，其中管理体系认证还需提供全国认证认可"
    "信息公共服务平台http://cx.cnca.cn/查询结果截图并加盖公章，未按要求提供不得分。"
)


def _certificate_allocations() -> tuple[SkeletonRow, list[ExtractedCriterion]]:
    row = SkeletonRow(
        group="投标人认证及资质情况", group_total=None, sub_idx=None,
        subjectivity="objective", max_score=Decimal(5), page_no=95,
        text=f"{ISO_ALLOCATION}\n{CMMI_ALLOCATION}\n【证明材料】{SHARED_PROOF}",
    )
    children = [
        ExtractedCriterion(
            code="c44", name=f"{row.group}—管理体系认证", category="commercial",
            score_type="count", max_score=Decimal(5), quote=ISO_ALLOCATION, page_hint=95,
            description=f"{ISO_ALLOCATION}\n【证明材料】{SHARED_PROOF}",
            params={"field": "certificates", "per_item": "1", "max_items": "3"},
        ),
        ExtractedCriterion(
            code="c45", name=f"{row.group}—CMMI/CSMM 认证", category="commercial",
            score_type="match", max_score=Decimal(2), quote=CMMI_ALLOCATION, page_hint=95,
            description=f"{CMMI_ALLOCATION}\n【证明材料】{SHARED_PROOF}",
            check_type="human_only", params={"field": "certificates", "engine": "count_tier"},
        ),
    ]
    return row, children


@pytest.mark.parametrize("iso_maximum", [Decimal(3), Decimal(5)])
def test_count_allocations_keep_explicit_child_maxima(iso_maximum: Decimal) -> None:
    row, children = _certificate_allocations()
    children[0].max_score = iso_maximum
    performance = SkeletonRow(
        group="业绩", group_total=None, sub_idx=None, subjectivity="objective",
        max_score=Decimal(4), text="每提供一个类似项目案例得2分，最高得4分。", page_no=95,
    )
    out, report = reconcile_with_skeleton(
        [ExtractedCriterion(
            code="c43", name=performance.group, category="commercial", score_type="count",
            max_score=performance.max_score, quote=performance.text, page_hint=95,
        ), *children],
        document_id=1, pages=[
            (5, "五、投标文件的递交\n递交截止时间：2025 年11 月19 日09 时30 分00 秒"),
            (95, f"1、商务部分（9分）\n{performance.text}\n{row.text}"),
        ],
        skeleton=ScoreSkeleton(rows=[performance, row]),
    )

    assert [(item.code, item.max_score) for item in out] == [
        ("c43", Decimal(4)), ("c44", Decimal(3)), ("c45", Decimal(2)),
    ]
    assert report["final_sum"] == 9.0
    assert report["generated"] == []
    assert report["explicit_count_allocations"][0]["sum"] == 5.0
    assert report["multi_covered_rows"] == []
    assert out[1].params["field"] == "certificates"
    assert "查询结果截图" in (out[1].required_evidence or "")
    cmmi = out[2]
    assert cmmi.score_type == "count"
    assert cmmi.check_type == "count_at_least"
    assert cmmi.params == {
        "field": "cmmi", "per_item": "2", "max_items": "1", "_objective_form": "existence",
        "cert_min_level": 2, "cert_require_current": True, "cert_reference_date": "2025-11-19",
    }
    assert "盖公章" in (cmmi.required_evidence or "")
    assert "截图" not in (cmmi.description or "") + (cmmi.required_evidence or "")


@pytest.mark.parametrize("reason", ["missing", "wrong_sum", "duplicate", "full_row", "ambiguous", "alternative"])
def test_count_allocations_require_disjoint_complete_source(reason: str) -> None:
    row, children = _certificate_allocations()
    rows = [row]
    if reason == "missing":
        children.pop()
    elif reason == "wrong_sum":
        row.max_score = Decimal(6)
    elif reason == "duplicate":
        children.append(children[0].model_copy())
    elif reason == "full_row":
        children[0].quote = f"{ISO_ALLOCATION}{CMMI_ALLOCATION}"
    elif reason == "alternative":
        children[1].quote = f"或者{CMMI_ALLOCATION}"
        row.text = row.text.replace(f"。\n{CMMI_ALLOCATION}", f"，{children[1].quote}")
    else:
        rows.append(replace(row, group="另一评分行"))
    before = [item.model_dump() for item in children]

    assert reconcile_explicit_count_allocations(children, rows) == []
    assert [item.model_dump() for item in children] == before


@pytest.mark.parametrize(("text", "expected"), [
    ("递交截止时间：2025 年11 月19 日09 时30 分00 秒", "2025-11-19"),
    ("投标截止时间：2025-11-19，投标截止日期为2025年11月19日", "2025-11-19"),
    ("投标截止时间：2025-11-19，递交截止日期为2025年11月20日", None),
    ("开标时间：2025-11-19。投标截止时间详见前附表", None),
    ("投标截止时间：2025年2月30日", None),
])
def test_extract_tender_submission_date(text: str, expected: str | None) -> None:
    assert extract_tender_submission_date([(5, text)]) == expected


@pytest.mark.parametrize(("evidence", "score", "unresolved"), [
    ({"name": "CSMM", "backing": True, "cert_level": "二级", "valid_until": "2025年11月19日"}, 2, None),
    ({"name": "CMMI", "backing": True, "cert_level": "Maturity Level 5", "valid_until": "2026-01-01"}, 2, None),
    ({"name": "CMMI", "backing": True, "cert_level": "一级", "valid_until": "2027-01-01"}, 0, None),
    ({"name": "CSMM", "backing": True, "cert_level": "三级", "valid_until": "2025-11-18"}, 0, None),
    ({"name": "CSMM", "backing": True, "cert_level": "三级", "valid_until": "2027-01-01",
      "valid_from": "2025-11-20"}, 0, None),
    ({"name": "CSMM", "backing": True, "cert_level": "三级"}, 0, "certificate_unverified"),
    ({"name": "CSMM", "backing": True, "valid_until": "2027-01-01"}, 0, "certificate_unverified"),
    ({"name": "CSMM", "cert_level": "三级", "valid_until": "2027-01-01"}, 0, "certificate_unverified"),
    ({"name": "ISO9001", "backing": True, "cert_level": "三级", "valid_until": "2027-01-01"},
     0, "certificate_unverified"),
    (None, 0, "certificate_unverified"),
])
def test_maturity_certificate_scoring(evidence: dict | None, score: int, unresolved: str | None) -> None:
    row, children = _certificate_allocations()
    reconcile_explicit_count_allocations(children, [row], submission_date="2025-11-19")
    criterion = {"max_score": children[1].max_score, "certificate_details": [evidence] if evidence else []}
    facts = BidFacts(text="", cert_claims=["CMMI 五级"])

    result = score_count(criterion, children[1].params, facts)

    assert result.score == Decimal(score)
    assert result.unresolved == unresolved


def test_maturity_certificate_does_not_assume_reference_date() -> None:
    row, children = _certificate_allocations()
    reconcile_explicit_count_allocations(children, [row])
    criterion = {"max_score": children[1].max_score, "certificate_details": [
        {"name": "CMMI", "backing": True, "cert_level": "三级", "valid_until": "2027-01-01"},
    ]}

    result = score_count(criterion, children[1].params, BidFacts(text=""))

    assert result.score == 0
    assert result.unresolved == "certificate_unverified"
    assert "投标截止日" in result.detail


def _yitihua_skeleton_and_pages():
    tables = {int(k): v for k, v in json.loads(
        (YITIHUA_FX / "tables_p88_90.json").read_text(encoding="utf-8")).items()}
    pages = {int(k): v for k, v in json.loads(
        (YITIHUA_FX / "pages_p88_90.json").read_text(encoding="utf-8")).items()}
    sk = parse_skeleton(tables, pages)
    assert sk is not None
    return sk, sorted(pages.items())


def _yitihua_full_criteria(sk, fragment_count: int = 3) -> list[ExtractedCriterion]:
    """模拟实测抽取：第 0 行拆成 ISO 子项；其余行整行覆盖；报价 60。"""
    out: list[ExtractedCriterion] = []
    for i, row in enumerate(sk.rows):
        if i == 0:
            for j in range(fragment_count):
                out.append(ExtractedCriterion(
                    name=ISO_NAMES[j], category="commercial", score_type="count",
                    max_score=Decimal("1"), quote=ISO_QUOTES[j],
                    description=ISO_QUOTES[j], page_hint=88))
            continue
        out.append(ExtractedCriterion(
            name=f"{row.group}-{i}",
            category=("commercial" if i < 3 else "technical"),
            score_type=("count" if row.subjectivity == "objective" else "subjective"),
            max_score=row.max_score, description=row.text,
            quote=row.text[:40], page_hint=row.page_no))
    out.append(ExtractedCriterion(
        name="报价分", category="price", score_type="formula",
        max_score=Decimal("60"), description="报价分计算",
        quote="2、取算术平均值*0.95 为最佳报价值。", page_hint=90,
        formula_type="deviation_deduct", benchmark_method="average",
        deduct_rate_down=Decimal("0.4"), deduct_rate_up=Decimal("0.8"),
        upper_bound=Decimal("60"), lower_bound=Decimal("0")))
    return out


def _scored_total(out: list[ExtractedCriterion]) -> Decimal:
    return sum((c.max_score or Decimal(0) for c in out
                if c.score_type in ("formula", "count", "subjective")), Decimal(0))


def test_yitihua_fragment_row_not_double_counted() -> None:
    """核心回归：3 条 ISO 子项（1+1+1）足额覆盖 0-3 行 → 不再生成父项，Σ=100。"""
    sk, pages = _yitihua_skeleton_and_pages()
    out, report = reconcile_with_skeleton(
        _yitihua_full_criteria(sk), document_id=1, pages=pages, skeleton=sk)

    assert report["final_sum"] == 100.0
    assert report["generated"] == []
    assert len(report["fragment_covered"]) == 1
    covered = report["fragment_covered"][0]
    assert covered["sum"] == 3.0
    assert covered["coverage"] >= 0.9
    assert covered["row_index"] == 0
    assert set(covered["fragments"]) == set(ISO_NAMES)
    # 三条子项保留且各 1 分；不存在生成的 max=3 父项
    kept = [c for c in out if c.name in ISO_NAMES]
    assert len(kept) == 3 and all(c.max_score == Decimal("1") for c in kept)
    assert not any(c.max_score == Decimal("3") and "投标人实力" in c.name for c in out)
    assert _scored_total(out) == Decimal("100")
    json.dumps(report, ensure_ascii=False)  # Job.result_json 必须可序列化


def test_yitihua_evidence_only_quote_not_fragment() -> None:
    """引文只命中【证明材料】段 → 不算片段，整行生成（fail-closed）。"""
    sk, pages = _yitihua_skeleton_and_pages()
    crits = _yitihua_full_criteria(sk, fragment_count=0)
    crits.append(ExtractedCriterion(
        name="证明材料得分", category="commercial", score_type="count",
        max_score=Decimal("3"), quote="未提供或不符合以上条件的不得分",
        description="未提供或不符合以上条件的不得分", page_hint=88))
    out, report = reconcile_with_skeleton(crits, document_id=1, pages=pages, skeleton=sk)

    assert report["fragment_covered"] == []
    assert report["generated"]
    assert any("投标人实力" in n for n in report["generated"])
    assert _scored_total(out) == Decimal("103")


def test_yitihua_fragment_page_hint_too_far_ignored() -> None:
    """页码差 >1 的片段不登记（防跨项目/跨章节误配），保持 fail-closed。"""
    sk, pages = _yitihua_skeleton_and_pages()
    crits = _yitihua_full_criteria(sk)
    for c in crits[:3]:
        c.page_hint = 10
    out, report = reconcile_with_skeleton(crits, document_id=1, pages=pages, skeleton=sk)

    assert report["fragment_covered"] == []
    assert report["generated"]
    assert _scored_total(out) == Decimal("103")


def test_yitihua_fragment_sum_mismatch_reported() -> None:
    """片段 1+1=2 ≠ 行满分 3 → sum_mismatch，条目不删、整行生成（总分超出阻断）。"""
    sk, pages = _yitihua_skeleton_and_pages()
    out, report = reconcile_with_skeleton(
        _yitihua_full_criteria(sk, fragment_count=2),
        document_id=1, pages=pages, skeleton=sk)

    assert report["fragment_covered"] == []
    assert report["fragment_unmatched"]
    assert report["fragment_unmatched"][0]["reason"] == "sum_mismatch"
    assert report["fragment_unmatched"][0]["fragment_sum"] == 2.0
    assert report["generated"]
    assert _scored_total(out) == Decimal("102")
    assert sum(1 for c in out if c.name in ISO_NAMES) == 2


def test_yitihua_fragment_low_coverage_reported() -> None:
    """单条 max=3 只引用首分句（覆盖 34%）→ low_coverage，不登记。"""
    sk, pages = _yitihua_skeleton_and_pages()
    crits = _yitihua_full_criteria(sk, fragment_count=0)
    crits.append(ExtractedCriterion(
        name="投标人实力", category="commercial", score_type="count",
        max_score=Decimal("3"), quote=ISO_QUOTES[0],
        description=ISO_QUOTES[0], page_hint=88))
    out, report = reconcile_with_skeleton(crits, document_id=1, pages=pages, skeleton=sk)

    assert report["fragment_covered"] == []
    assert report["fragment_unmatched"]
    assert report["fragment_unmatched"][0]["reason"] == "low_coverage"
    assert report["generated"]
    assert _scored_total(out) == Decimal("103")


def test_yitihua_duplicate_fragment_probe_reported() -> None:
    """重复主引文（同句 1+1 + 另一句 1 凑 3）→ duplicate_probe，不登记。"""
    sk, pages = _yitihua_skeleton_and_pages()
    crits = _yitihua_full_criteria(sk, fragment_count=0)
    for name, quote in ((ISO_NAMES[0], ISO_QUOTES[0]),
                        (ISO_NAMES[0], ISO_QUOTES[0]),
                        (ISO_NAMES[1], ISO_QUOTES[1])):
        crits.append(ExtractedCriterion(
            name=name, category="commercial", score_type="count",
            max_score=Decimal("1"), quote=quote, description=quote, page_hint=88))
    out, report = reconcile_with_skeleton(crits, document_id=1, pages=pages, skeleton=sk)

    assert report["fragment_covered"] == []
    assert report["fragment_unmatched"]
    assert report["fragment_unmatched"][0]["reason"] == "duplicate_probe"
    assert report["generated"]
    assert _scored_total(out) == Decimal("103")


def test_yitihua_fragment_ambiguous_across_rows() -> None:
    """同一分句命中两行 → 歧义，不登记、两行都生成。"""
    # 行首 40 字符切片不等于该分句 → 不构成整行覆盖，才进入片段识别
    prefix = "本项目评分要求如下，投标人应逐项响应并附证明材料："
    body = prefix + "提供有效证书得1分。"
    row = SkeletonRow(group="重复项", group_total=None, sub_idx=None,
                      subjectivity="objective", max_score=Decimal("1"),
                      text=body, page_no=1)
    sk = ScoreSkeleton(rows=[row, SkeletonRow(
        group="重复项", group_total=None, sub_idx=None,
        subjectivity="objective", max_score=Decimal("1"),
        text=body, page_no=1)])
    crit = ExtractedCriterion(
        name="证书", category="commercial", score_type="count",
        max_score=Decimal("1"), quote="提供有效证书得1分。",
        description="提供有效证书得1分。", page_hint=1)
    out, report = reconcile_with_skeleton(
        [crit], document_id=1, pages=[(1, body)], skeleton=sk)

    assert report["fragment_ambiguous"]
    assert report["fragment_covered"] == []
    assert len(report["generated"]) == 2
    # 歧义条目不删，两行各自生成 → 总分超出，由 TOTAL_SCORE 阻断（fail-closed）
    assert _scored_total(out) == Decimal("3")


def test_yitihua_multi_covered_row_reported_only() -> None:
    """同一行被两条整行 criterion 覆盖 → 只披露，不自动删（无法从骨架判权威）。"""
    row = SkeletonRow(group="单项", group_total=None, sub_idx=None,
                      subjectivity="objective", max_score=Decimal("3"),
                      text="具有三项证书，每项得1分。", page_no=1)
    sk = ScoreSkeleton(rows=[row])
    crits = [
        ExtractedCriterion(name=f"单项{i}", category="commercial", score_type="count",
                           max_score=Decimal("3"), description=row.text,
                           quote=row.text, page_hint=1)
        for i in range(2)
    ]
    out, report = reconcile_with_skeleton(
        crits, document_id=1, pages=[(1, row.text)], skeleton=sk)

    assert len(report["multi_covered_rows"]) == 1
    assert len(report["multi_covered_rows"][0]["criteria"]) == 2
    assert len(out) == 2
    assert _scored_total(out) == Decimal("6")


def test_union_coverage_merges_overlaps() -> None:
    assert _union_coverage("abcdef", ["abc", "cde"]) == 5 / 6
    assert _union_coverage("abcdef", ["abc", "bcd"]) == 4 / 6
    assert _union_coverage("abcdef", ["xyz"]) == 0.0
    assert _union_coverage("", ["abc"]) == 0.0


def test_fragment_probe_thresholds() -> None:
    def probes(quote: str | None = None, description: str | None = None,
               name: str = "评分项名称") -> list[str]:
        c = ExtractedCriterion(
            name=name, category="commercial", score_type="count",
            max_score=Decimal("1"), quote=quote, description=description)
        return _fragment_probes(c)

    assert probes(quote="短句") == []
    assert probes(quote="提供有效证书") == []              # 6 字 < 8
    assert probes(quote="提供有效证书得1分") != []          # 10 字带计分锚点
    assert probes(quote="提供有效证书及证明材料") == []      # 12 字无锚点
    assert probes(quote="提供有效证书及证明材料扫描件原件") != []  # 16 字不再要求锚点
    assert probes(description="每提供一项得2分") != []


def test_yitihua_compile_wiring_no_double_count(app_env, monkeypatch) -> None:
    """compile_rules 全链接线：落库 Σ=100、无生成父项、TOTAL_SCORE 消失。"""
    from sqlalchemy import select

    from app.db.models import Criterion, Document, DocumentPage, PackageValidation, Project
    from app.db.session import session_scope
    from app.rules import compiler as compiler_mod
    from app.rules.model import RuleExtraction

    sk, pages = _yitihua_skeleton_and_pages()
    with session_scope() as s:
        s.add(Project(id="PRJ-TEST-YITIHUA", name="片段接线测试"))
        s.flush()
        doc = Document(project_id="PRJ-TEST-YITIHUA", kind="tender",
                       original_filename="t.pdf", stored_path="t.pdf",
                       sha256="4" * 64, parse_status="parsed")
        s.add(doc)
        s.flush()
        doc_id = doc.id
        for pno, text in pages:
            s.add(DocumentPage(document_id=doc_id, page_no=pno, text=text))

    fake = RuleExtraction(criteria=_yitihua_full_criteria(sk))
    monkeypatch.setattr(
        compiler_mod, "_extract_chunk",
        lambda *a, **k: (fake, type("R", (), {"model": "mock"})()))
    monkeypatch.setattr(
        "app.rules.reconcile.extract_score_skeleton", lambda *a, **k: sk)

    meta: dict = {}
    package_id = compiler_mod.compile_rules(
        "PRJ-TEST-YITIHUA", doc_id, actor="tester", selection_meta_out=meta)

    rep = meta.get("skeleton_reconcile") or {}
    assert rep.get("final_sum") == 100.0
    assert rep.get("generated") == []
    assert rep.get("fragment_covered")

    with session_scope() as s:
        crits = s.execute(
            select(Criterion).where(Criterion.package_id == package_id)
        ).scalars().all()
        total = sum((c.max_score or Decimal(0) for c in crits
                     if c.score_type in ("formula", "count", "subjective")), Decimal(0))
        assert total == Decimal("100")
        assert not any("投标人实力具有" in c.name for c in crits)
        names = [c.name for c in crits]
        assert all(n in names for n in ISO_NAMES)
        codes = s.execute(
            select(PackageValidation.code).where(PackageValidation.package_id == package_id)
        ).scalars().all()
        assert "TOTAL_SCORE" not in codes
