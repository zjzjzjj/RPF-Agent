# -*- coding: utf-8 -*-
"""抽取对账 + 缺口补齐测试（评标细则骨架）。

核心场景（多媒体维保项目实证 91≠100）：LLM 只抽到部分计分行时，
reconcile 以骨架为权威补齐缺口行/校正分值，Σmax_score 闭合到 100。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.rules.model import ExtractedCriterion
from app.rules.reconcile import reconcile_with_skeleton
from app.rules.scoring_skeleton import parse_skeleton

FX = Path(__file__).parent / "fixtures" / "scoring_skeleton" / "multimedia_2025"


def _skeleton_and_pages():
    tables = {int(k): v for k, v in json.loads(
        (FX / "tables_p135_137.json").read_text(encoding="utf-8")).items()}
    pages = {int(k): v for k, v in json.loads(
        (FX / "pages_p135_137.json").read_text(encoding="utf-8")).items()}
    sk = parse_skeleton(tables, pages)
    assert sk is not None
    return sk, sorted(pages.items())


def _partial_criteria() -> list[ExtractedCriterion]:
    """模拟实测 91 分形态：只抽到部分行（含价格项）。"""
    sk, _ = _skeleton_and_pages()
    # 取骨架前 8 行里抽 5 行 + 价格项，漏掉约 9 分
    picked = [sk.rows[0], sk.rows[2], sk.rows[6], sk.rows[12], sk.rows[13]]
    out = []
    for r in picked:
        out.append(ExtractedCriterion(
            name=r.group, category="technical", score_type="subjective",
            max_score=r.max_score, description=r.text, page_hint=r.page_no,
        ))
    out.append(ExtractedCriterion(
        name="报价得分", category="price", score_type="formula",
        max_score=Decimal("50"), description="偏离扣分",
        formula_type="deviation_deduct", benchmark_method="average",
    ))
    return out


def test_reconcile_fills_gaps_to_100() -> None:
    sk, pages = _skeleton_and_pages()
    out, report = reconcile_with_skeleton(
        _partial_criteria(), document_id=1, pages=pages, skeleton=sk)
    total = sum((c.max_score or Decimal(0) for c in out
                 if c.score_type in ("formula", "count", "subjective")), Decimal(0))
    assert total == Decimal("100")
    assert not report["skipped"]
    assert report["final_sum"] == 100.0
    assert len(report["generated"]) >= 10  # 缺口行补齐


def test_reconcile_generated_rows_cover_all_skeleton_texts() -> None:
    """骨架 17 行的细则文本逐行可被某条 criterion 的描述/引文命中。"""
    sk, pages = _skeleton_and_pages()
    out, _ = reconcile_with_skeleton(
        _partial_criteria(), document_id=1, pages=pages, skeleton=sk)
    from app.parsing.normalize import normalize_no_ws
    corpus = [normalize_no_ws(f"{c.name}\n{c.description or ''}\n{c.quote or ''}")
              for c in out]
    for row in sk.rows:
        core = normalize_no_ws(row.text)[:14]
        assert any(core in t for t in corpus), f"骨架行未覆盖: {row.group} {row.text[:20]}"


def test_reconcile_generated_quotes_are_verbatim_in_pages() -> None:
    """生成条目的 quote 必须能在页文本逐字定位（verify_citation 口径）。"""
    sk, pages = _skeleton_and_pages()
    out, report = reconcile_with_skeleton(
        _partial_criteria(), document_id=1, pages=pages, skeleton=sk)
    from app.parsing.normalize import normalize_no_ws
    joined = normalize_no_ws("\n".join(t for _, t in pages))
    gen_names = set(report["generated"])
    generated = [c for c in out if c.name in gen_names]
    assert generated
    for c in generated:
        assert c.quote, f"生成条目缺 quote: {c.name}"
        assert normalize_no_ws(c.quote) in joined


def test_reconcile_parent_granularity_no_duplicate() -> None:
    """父项粒度抽取（一条 max=组分值 覆盖整组）→ 不重复生成子行。"""
    sk, pages = _skeleton_and_pages()
    # "投标方案评析"组：3 行合计 10，父项一条 max=10
    parent = ExtractedCriterion(
        name="投标方案评析", category="technical", score_type="subjective",
        max_score=Decimal("10"),
        description="对招标人运维需求、运维服务的重点、难点进行科学、合理、准确地分析描述 "
                    "有全面完整的服务质量管理体系，服务规范 提供应急预案及处理方案切实可行",
    )
    out, report = reconcile_with_skeleton(
        [parent], document_id=1, pages=pages, skeleton=sk)
    gen_names = report["generated"]
    assert not any("投标方案评析" in n for n in gen_names)  # 整组不补
    total = sum((c.max_score or Decimal(0) for c in out
                 if c.score_type in ("formula", "count", "subjective")), Decimal(0))
    assert total == Decimal("100")


def test_reconcile_fixes_score_mismatch() -> None:
    """1:1 覆盖但分值不符 → 以骨架为准改写。"""
    sk, pages = _skeleton_and_pages()
    row = sk.rows[2]  # 业绩 0-6
    bad = ExtractedCriterion(
        name="投标人业绩实力", category="commercial", score_type="count",
        max_score=Decimal("4"), description=row.text,  # 分值错抽成 4
    )
    out, report = reconcile_with_skeleton(
        [bad], document_id=1, pages=pages, skeleton=sk)
    fixed = [c for c in out if c.name == "投标人业绩实力"]
    assert fixed[0].max_score == Decimal("6")
    assert report["score_fixed"]


def test_reconcile_noop_when_skeleton_none() -> None:
    """骨架不可用 → 原样返回（回退纯 LLM，不劣于现状）。"""
    import app.config as config
    import app.rules.reconcile as rec_mod

    orig = rec_mod.extract_score_skeleton
    rec_mod.extract_score_skeleton = lambda *a, **k: None
    try:
        crits = _partial_criteria()
        out, report = reconcile_with_skeleton(crits, document_id=1, pages=[])
        assert out == crits
        assert report.get("skipped")
    finally:
        rec_mod.extract_score_skeleton = orig

    # 开关关闭同样 no-op
    import app.config as cfg
    old = cfg.scoring_skeleton_enabled
    cfg.scoring_skeleton_enabled = lambda: False
    try:
        crits = _partial_criteria()
        out, report = reconcile_with_skeleton(crits, document_id=1, pages=[])
        assert out == crits
        assert report.get("skipped")
    finally:
        cfg.scoring_skeleton_enabled = old


def test_score_type_mapping_count_and_subjective() -> None:
    """确定性生成的 score_type：每件计分语言→count，主观行→subjective。"""
    sk, pages = _skeleton_and_pages()
    out, report = reconcile_with_skeleton(
        _partial_criteria(), document_id=1, pages=pages, skeleton=sk)
    gen = {c.name: c for c in out}
    # 行"（5）服务团队成员具有特种作业操作证…每人得 1 分，最高不超 2 分" → count
    certs = [c for c in out if "特种作业" in (c.description or "")]
    if certs:
        c = certs[0]
        assert c.score_type == "count"
        assert c.params.get("field") in ("certificates", "performance")
        assert c.params.get("per_item") == "1"


def test_compile_rules_wiring_end_to_end(app_env, monkeypatch) -> None:
    """compile_rules 主链接线：mock 残缺抽取 → 落库包 Σmax_score=100。

    验证 reconcile 调用真的插在落库前（不是孤立函数），且生成条目 quote
    逐字可核验（status 走 _triage 正常分诊，不进 invalid）。
    """
    from decimal import Decimal

    from sqlalchemy import select

    from app.db.models import Criterion, Document, DocumentPage, Project
    from app.db.session import session_scope
    from app.rules import compiler as compiler_mod
    from app.rules.model import RuleExtraction

    sk, pages = _skeleton_and_pages()

    with session_scope() as s:
        s.add(Project(id="PRJ-TEST-0001", name="对账接线测试"))
        s.flush()
        doc = Document(project_id="PRJ-TEST-0001", kind="tender",
                       original_filename="t.pdf", stored_path="t.pdf",
                       sha256="0" * 64, parse_status="parsed")
        s.add(doc)
        s.flush()
        doc_id = doc.id
        for pno, text in pages:
            s.add(DocumentPage(document_id=doc_id, page_no=pno, text=text))

    # mock 抽取：只给 8/17 行 + 价格项（模拟实测 91 分形态）
    partial = _partial_criteria()
    fake_extraction = RuleExtraction(criteria=partial)
    monkeypatch.setattr(
        compiler_mod, "_extract_chunk",
        lambda *a, **k: (fake_extraction, type("R", (), {"model": "mock"})()))
    # 骨架走真实 fixture（PDF 打开失败 → 用 fixture 骨架注入）
    monkeypatch.setattr(
        "app.rules.reconcile.extract_score_skeleton", lambda *a, **k: sk)

    package_id = compiler_mod.compile_rules(
        "PRJ-TEST-0001", doc_id, actor="tester")

    with session_scope() as s:
        crits = s.execute(
            select(Criterion).where(Criterion.package_id == package_id)
        ).scalars().all()
        total = sum((c.max_score or Decimal(0) for c in crits
                     if c.score_type in ("formula", "count", "subjective")), Decimal(0))
        assert total == Decimal("100"), f"落库合计 {total} != 100"
        # 骨架 17 行的细则文本全部被覆盖
        from app.parsing.normalize import normalize_no_ws
        corpus = normalize_no_ws("\n".join(
            f"{c.name}\n{c.description or ''}\n{c.source_quote or ''}" for c in crits))
        for row in sk.rows:
            core = normalize_no_ws(row.text)[:14]
            assert core in corpus, f"落库后骨架行未覆盖: {row.group}"
        # 生成条目不得进 invalid（quote 逐字可核验）
        bad = [c for c in crits if c.status == "invalid"]
        assert not bad, f"invalid 条目: {[c.name for c in bad]}"


def test_match_typed_scoring_row_converted_to_counting() -> None:
    """骨架计分行被 LLM 分成 match（判断项不计分）→ 按骨架映射改写为计分类型。

    实测根因（Σ 96≠100）："承诺…得2分"被 prompt 的 match（满足条件得分）
    误导成分到 match，4 分永远兑现不了。"""
    sk, pages = _skeleton_and_pages()
    row = next(r for r in sk.rows if "驻场" in r.text and "承诺" in r.text)
    match_item = ExtractedCriterion(
        name="承诺额外增加驻场人员", category="technical", score_type="match",
        max_score=Decimal("2"), description=row.text)
    out, report = reconcile_with_skeleton([match_item], document_id=1, pages=pages, skeleton=sk)
    assert match_item.score_type in ("count", "subjective")
    assert report["score_type_fixed"]
    # 改写后分值计入总分
    total = sum((c.max_score or Decimal(0) for c in out
                 if c.score_type in ("formula", "count", "subjective")), Decimal(0))
    assert total == Decimal("100")


def test_judgment_match_without_score_stays() -> None:
    """判断项 match（无分值、不覆盖骨架行）保持 match 不动。"""
    sk, pages = _skeleton_and_pages()
    judgment = ExtractedCriterion(
        name="报价修正原则", category="compliance", score_type="match",
        max_score=None, description="投标文件报价出现前后不一致的按开标一览表为准。")
    out, report = reconcile_with_skeleton([judgment], document_id=1, pages=pages, skeleton=sk)
    assert judgment.score_type == "match"  # 未被改写
    assert not report["score_type_fixed"]


def test_category_corrected_from_skeleton_section() -> None:
    """覆盖条目的类别错标（培训计划被标 commercial）→ 以骨架章节位置校正。"""
    sk, pages = _skeleton_and_pages()
    row = next(r for r in sk.rows if "培训计划" in r.text)  # 技术部分/服务保障情况
    bad = ExtractedCriterion(
        name="培训计划", category="commercial", score_type="subjective",
        max_score=Decimal("2"), description=row.text)
    out, report = reconcile_with_skeleton([bad], document_id=1, pages=pages, skeleton=sk)
    assert bad.category == "technical"
    assert report["category_fixed"]


def test_reconcile_prunes_parent_child_double_count():
    """父子双计裁剪：父项（max=组分值）+ 子项（4+4+2）同时抽出 → 丢父保子。

    run 54 pkg22 Σ=109≠100 的根因形态：LLM 把整组与子项都当独立计分项输出。
    """
    sk, pages = _skeleton_and_pages()
    # "投标方案评析"组 3 行合计 10（4+4+2）
    group_rows = [r for r in sk.rows if r.group == "投标方案评析"]
    assert len(group_rows) == 3
    parent = ExtractedCriterion(
        name="投标方案评析", category="technical", score_type="subjective",
        max_score=Decimal("10"),
        description="对招标人运维需求、运维服务的重点、难点进行科学、合理、准确地分析描述 "
                    "有全面完整的服务质量管理体系，服务规范 提供应急预案及处理方案切实可行",
    )
    children = [
        ExtractedCriterion(
            name=f"投标方案评析（{i}）", category="technical", score_type="subjective",
            max_score=r.max_score, description=r.text, page_hint=r.page_no,
        )
        for i, r in enumerate(group_rows, 1)
    ]
    out, report = reconcile_with_skeleton(
        [parent] + children, document_id=1, pages=pages, skeleton=sk)
    names = [c.name for c in out]
    assert "投标方案评析" not in names  # 父项被裁掉
    assert sum(1 for n in names if "投标方案评析" in n) == 3
    assert report["overcoverage_pruned"]
    total = sum((c.max_score or Decimal(0)) for c in out
                if c.score_type in ("formula", "count", "subjective"))
    assert total == Decimal("100")


def test_reconcile_generates_price_from_business_quote_ledger():
    """台账组名「商务报价部分」（复合名，行首非「报价/价格」）也要能生成价格项。"""
    from decimal import Decimal as D

    from app.rules.model import ExtractedCriterion
    from app.rules.scoring_skeleton import LedgerEntry, ScoreSkeleton

    skeleton = ScoreSkeleton(
        rows=[],
        ledger=[LedgerEntry(label="商务报价部分", max_score=D("55"),
                            text="3、商务报价部分（55 分）价格分=55×（评标基准价/投标人报价）",
                            page_no=94)],
    )
    pages = {94: "3、商务报价部分（55 分）价格分=55×（评标基准价/投标人报价）"}
    out, report = reconcile_with_skeleton(
        [], document_id=1, pages=list(pages.items()), skeleton=skeleton)
    assert report["price_generated"] is True
    price = [c for c in out if c.category == "price" or c.score_type == "formula"]
    assert price and price[0].max_score == D("55")


# --- P0-C：主/客观 hint（纯 LLM 回退路径兜底） ---------------------------------

def test_subjectivity_hint_from_quantifiable_language() -> None:
    """细则含"每…得 N 分"量化措辞 → _subjectivity_hint=objective（不改 score_type）。"""
    from app.rules.compiler import enrich_extracted
    from app.rules.model import ExtractedCriterion

    c = ExtractedCriterion(
        category="commercial", name="增值服务", score_type="match",
        description="每认可一项增值服务得2分，最高4分",
    )
    enrich_extracted([c], "")
    assert (c.params or {}).get("_subjectivity_hint") == "objective"
    assert c.score_type == "match"  # 只产 hint 不改类型（歧义不猜测）


def test_subjectivity_hint_explicit_model_value_wins() -> None:
    """模型照抄评分表"主/客观分"列的显式值优先于文本特征。"""
    from app.rules.compiler import enrich_extracted
    from app.rules.model import ExtractedCriterion

    c = ExtractedCriterion(
        category="technical", name="功能模块化", score_type="subjective",
        description="综合评审模块化设计的先进性", subjectivity_hint="subjective",
    )
    enrich_extracted([c], "")
    assert (c.params or {}).get("_subjectivity_hint") is None


# --- P1-C.6 样品分总分口径（代际反转：三部分单列 / 四部分入总分） ---------------

def test_sample_in_total_three_parts_false() -> None:
    from app.rules.compiler import enrich_extracted
    from app.rules.model import ExtractedCriterion

    c = ExtractedCriterion(
        category="technical", name="样品评审", score_type="subjective",
        max_score=10, description="样品外观与检测报告评审",
    )
    tender = "综合得分为以上三部分得分的总和（商务、技术、报价）。样品分单列不计入综合得分。"
    enrich_extracted([c], tender)
    assert (c.params or {}).get("in_total") is False


def test_sample_in_total_four_parts_true() -> None:
    from app.rules.compiler import enrich_extracted
    from app.rules.model import ExtractedCriterion

    c = ExtractedCriterion(
        category="technical", name="样品评审", score_type="subjective",
        max_score=10, description="样品外观评审",
    )
    enrich_extracted([c], "综合得分为以上四部分得分的总和。")
    assert (c.params or {}).get("in_total") is True


def test_non_sample_criterion_no_total_scope_flag() -> None:
    from app.rules.compiler import enrich_extracted
    from app.rules.model import ExtractedCriterion

    c = ExtractedCriterion(
        category="technical", name="技术方案", score_type="subjective",
        max_score=10, description="方案评审",
    )
    enrich_extracted([c], "综合得分为以上三部分得分的总和。")
    assert "in_total" not in (c.params or {})
