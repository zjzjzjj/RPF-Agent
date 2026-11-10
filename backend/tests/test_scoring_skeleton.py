# -*- coding: utf-8 -*-
"""评标细则表格骨架解析器测试。

真实 fixture：浙江多媒体维保项目 p135-137 的 find_tables 产物（17 计分行 Σ=50
+ 表外报价段 50 = 100）；合成 PDF smoke 覆盖 find_tables 适配层。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.rules.scoring_skeleton import (
    ScoreSkeleton,
    locate_scoring_section,
    parse_skeleton,
    extract_score_skeleton,
)

FX = Path(__file__).parent / "fixtures" / "scoring_skeleton" / "multimedia_2025"


def _load_fixture() -> tuple[dict[int, list], dict[int, str]]:
    tables = json.loads((FX / "tables_p135_137.json").read_text(encoding="utf-8"))
    pages = json.loads((FX / "pages_p135_137.json").read_text(encoding="utf-8"))
    return {int(k): v for k, v in tables.items()}, {int(k): v for k, v in pages.items()}


def _expected() -> dict:
    return json.loads((FX / "expected.json").read_text(encoding="utf-8"))


def test_skeleton_extracts_17_rows_sum_50() -> None:
    tables, pages = _load_fixture()
    sk = parse_skeleton(tables, pages)
    assert sk is not None
    exp = _expected()
    assert len(sk.rows) == len(exp["rows"]) == 17
    assert sk.row_sum() == Decimal(str(exp["row_sum"])) == Decimal("50")
    for got, want in zip(sk.rows, exp["rows"]):
        assert got.group == want["group"], f"{got.group} != {want['group']}"
        assert got.sub_idx == want["sub_idx"]
        assert got.subjectivity == want["subjectivity"]
        assert got.max_score == Decimal(str(want["max_score"]))


def test_skeleton_ledger_includes_price_50_and_closes_100() -> None:
    tables, pages = _load_fixture()
    sk = parse_skeleton(tables, pages)
    assert sk is not None
    labels = {e.label: e.max_score for e in sk.ledger}
    assert labels.get("报价部分") == Decimal("50")  # 表外正文段，不在表里
    assert sk.target_total() == Decimal("100")
    assert sk.groups_consistent() is True
    assert sk.closed() is True


def test_skeleton_cross_page_continuation_joined() -> None:
    """两处跨页续行：0-4 服务质量 p135→p136、0-3 多级运维 p136→p137。"""
    tables, pages = _load_fixture()
    sk = parse_skeleton(tables, pages)
    assert sk is not None
    svc = next(r for r in sk.rows if r.group == "投标方案评析" and r.max_score == Decimal("4")
               and "服务质量" in r.text)
    assert "服务内容基本符合" in svc.text          # p136 首行续文
    assert svc.text.index("服务质量") < svc.text.index("服务内容基本符合")
    ops = next(r for r in sk.rows if r.group == "服务保障情况" and r.max_score == Decimal("3")
               and "多级运维" in r.text)
    assert "操作性考虑" in ops.text               # p137 首行续文


def test_skeleton_material_rows_go_to_required_evidence() -> None:
    tables, pages = _load_fixture()
    sk = parse_skeleton(tables, pages)
    assert sk is not None
    # 【证明材料】行不产计分行；至少一处挂到了计分行的 required_evidence
    assert not any("证明材料" in r.group for r in sk.rows)
    assert any(r.required_evidence and "证明材料" in r.required_evidence
               for r in sk.rows)


def test_skeleton_subitem_rows_inherit_group_subjectivity() -> None:
    """子行主/客观列空白时继承组级（实测行 2 资质证书行即此形态）。"""
    tables, pages = _load_fixture()
    sk = parse_skeleton(tables, pages)
    assert sk is not None
    row2 = sk.rows[1]
    assert row2.group == "投标人资质实力"
    assert row2.subjectivity == "objective"  # 继承组级，不得为 None


def test_locate_scoring_section_anchors_table_pages() -> None:
    tables, pages = _load_fixture()
    span = locate_scoring_section(sorted(pages.items()), tables)
    assert span is not None
    assert span[0] <= 135 <= span[1]
    assert span[1] >= 137  # 含表外报价段页


def test_parse_skeleton_none_without_tables() -> None:
    assert parse_skeleton({}, {1: "没有任何表格的散文评标办法"}) is None
    assert parse_skeleton({1: []}, {1: "x"}) is None


def test_synthetic_pdf_roundtrip() -> None:
    """合成表格 PDF smoke：画线表格可被 find_tables 识别并解析。"""
    pymupdf = pytest.importorskip("pymupdf")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "synth.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=500, height=400)
        # 5 列 x 3 行的极简评标细则表
        cols_x = [50, 130, 230, 300, 360]
        rows_y = [80, 130, 180, 230]
        for y in rows_y:
            page.draw_line((50, y), (470, y))
        for x in cols_x + [470]:
            page.draw_line((x, 80), (x, 230))
        cells = [
            ["序号", "评审因素", "主/客观分", "分值", "评标细则"],
            ["1", "方案（4 分）", "主观分", "0-4", "方案完整详细"],
            ["", "", "客观分", "0-2", "承诺驻场加分"],
        ]
        for ri, y in enumerate(rows_y[:-1]):  # 4 条横线 → 3 个文本行
            for ci, x in enumerate(cols_x):
                page.insert_text((x + 3, y + 22), cells[ri][ci], fontsize=9,
                                 fontname="china-s")
        page.insert_text((50, 280), "1、报价部分（10 分）偏离扣分", fontsize=9,
                         fontname="china-s")
        doc.save(path)
        doc.close()

        sk = extract_score_skeleton(str(path))
        assert sk is not None
        assert sk.row_sum() == Decimal("6")
        assert {r.max_score for r in sk.rows} == {Decimal("4"), Decimal("2")}
        assert sk.rows[1].subjectivity == "objective"


# ---------------------------------------------------------------------------
# 二维码平台运维项目（评审细则表头 + 商务报价部分复合组名，2026-10-27 实证）
# ---------------------------------------------------------------------------
QR_FX = Path(__file__).parent / "fixtures" / "scoring_skeleton" / "qr_maintenance_2025"


def _load_qr_fixture() -> tuple[dict[int, list], dict[int, str]]:
    tables = json.loads((QR_FX / "tables_p92_95.json").read_text(encoding="utf-8"))
    pages = json.loads((QR_FX / "pages_p92_95.json").read_text(encoding="utf-8"))
    return {int(k): v for k, v in tables.items()}, {int(k): v for k, v in pages.items()}


def test_qr_skeleton_10_rows_sum_45() -> None:
    """「评审细则」表头被识别，10 行 Σ=45（商务 9+技术 36）——旧正则整段 None。"""
    tables, pages = _load_qr_fixture()
    sk = parse_skeleton(tables, pages)
    assert sk is not None
    assert len(sk.rows) == 10
    assert sk.row_sum() == Decimal("45")
    groups = [r.group for r in sk.rows]
    assert "投标人业绩" in groups and "运维方案" in groups


def test_qr_ledger_captures_business_quote_and_closes_100() -> None:
    """台账含「商务部分 9」「技术部分 36」「商务报价部分 55」= target 100。

    复合组名「商务报价部分」是旧正则漏配的形态（(商务|技术|价格|报价)部分
    匹配不了「商务」后紧跟「报价」）。
    """
    tables, pages = _load_qr_fixture()
    sk = parse_skeleton(tables, pages)
    assert sk is not None
    labels = {e.label: e.max_score for e in sk.ledger}
    assert labels.get("商务部分") == Decimal("9")
    assert labels.get("技术部分") == Decimal("36")
    assert labels.get("商务报价部分") == Decimal("55")
    assert sk.ledger_sum() == Decimal("100")
    assert sk.target_total() == Decimal("100")
    assert sk.closed() is True


def test_qr_locate_scoring_section_extends_backward() -> None:
    """章节窗口向前扩：p92（标题+商务9分声明）必须在 span 内（表在 p93）。"""
    tables, pages = _load_qr_fixture()
    span = locate_scoring_section(sorted(pages.items()), tables)
    assert span is not None
    start, end = span
    assert start <= 92, f"span 起点 {start} 未含 p92（商务 9 分声明页）"
    assert end >= 94


def test_qr_expected_fixture_matches() -> None:
    """fixture 快照：行/台账形态与 expected.json 一致（防意外漂移）。"""
    tables, pages = _load_qr_fixture()
    sk = parse_skeleton(tables, pages)
    exp = json.loads((QR_FX / "expected.json").read_text(encoding="utf-8"))
    assert sk is not None
    assert len(sk.rows) == len(exp["rows"]) == 10
    assert str(sk.target_total()) == exp["target_total"] == "100"
    assert sk.closed() == exp["closed"] is True
    ledgers = {(e.label, str(e.max_score)) for e in sk.ledger}
    for want in exp["ledger"]:
        assert (want["label"], want["max_score"]) in ledgers


# --- P0-B：列头变体 / 双分值行 / 占位符 / 标段分设 ----------------------------

def _hdr(cells: list[str]) -> list[str | None]:
    return cells


def test_header_variant_main_slash_guest() -> None:
    """「主/客分」（一体化维保商务表）与「主/客/观分」（数据库）列变体。"""
    for subj_col in ("主/客分", "主/客/观分", "主/客 观分"):
        tables = {1: [[
            _hdr(["序号", "评审因素", subj_col, "分值", "评标细则"]),
            ["1", "业绩", "客观分", "0-3", "近三年同类业绩每个1分"],
        ]]}
        sk = parse_skeleton(tables, {1: ""})
        assert sk is not None, subj_col
        assert sk.rows[0].subjectivity == "objective"


def test_header_variant_da_fen_fang_shi() -> None:
    """老代「打分方式」列（2024 烟丝资信表）映射为 subj 列。"""
    tables = {1: [[
        _hdr(["序号", "评分项目", "打分方式", "分值", "评分要点及说明"]),
        ["1", "投标产品业绩", "客观分", "0-4", "合同额100万以上每个1分"],
    ]]}
    sk = parse_skeleton(tables, {1: ""})
    assert sk is not None
    assert sk.rows[0].subjectivity == "objective"
    assert sk.rows[0].max_score == Decimal("4")


def test_merged_header_score_and_detail_same_cell() -> None:
    """合并表头「分值/评分标准」同格 → 同时占 score+detail，不再整表丢失。"""
    tables = {1: [[
        _hdr(["序号", "评审因素", "分值/评分标准", "评标细则"]),
        ["1", "售后服务", "0-6", "12小时到现场2分，本地化服务2分"],
    ]]}
    sk = parse_skeleton(tables, {1: ""})
    assert sk is not None
    assert sk.rows[0].max_score == Decimal("6")


def test_dual_score_row_split() -> None:
    """同项双分值行（重招标段三「客观0-1 主观0-2」）→ 拆两行不丢分值。"""
    tables = {1: [[
        _hdr(["序号", "评审因素", "主/客观分", "分值", "评标细则"]),
        ["1", "售后服务案及人员培训情况", "", "客观0-1\n主观0-2",
         "具有售后服务体系得0-1分；培训方案优劣得0-2分"],
    ]]}
    sk = parse_skeleton(tables, {1: ""})
    assert sk is not None
    assert len(sk.rows) == 2
    kinds = {(r.subjectivity, r.max_score) for r in sk.rows}
    assert kinds == {("objective", Decimal("1")), ("subjective", Decimal("2"))}


def test_placeholder_row_skipped_with_warning() -> None:
    """占位符行（前*名/__%）跳过并告警，不得静默启用为规则。"""
    tables = {1: [[
        _hdr(["序号", "评审因素", "主/客观分", "分值", "评标细则"]),
        ["1", "推荐中标候选人", "", "0", "推荐综合得分排序前*名的投标人"],
        ["2", "业绩", "客观分", "2", "每个1分最高2分"],
    ]]}
    sk = parse_skeleton(tables, {1: ""})
    assert sk is not None
    assert len(sk.rows) == 1
    assert sk.rows[0].group == "业绩"
    assert any("占位符" in w for w in sk.warnings)


def test_dual_family_rows_alt_not_merged() -> None:
    """同款列头二次整行出现 → 标段分设：主行集取先者，其余进 rows_alt 并告警。"""
    hdr = _hdr(["序号", "评审因素", "主/客观分", "分值", "评标细则"])
    tables = {1: [
        [hdr, ["1", "资信", "客观分", "4", "业绩每个1分"], ["2", "技术", "客观分", "46", "方案评审"]],
        [hdr, ["1", "资信", "客观分", "7", "业绩每个1分"], ["2", "技术", "客观分", "33", "方案评审"]],
    ]}
    sk = parse_skeleton(tables, {1: ""})
    assert sk is not None
    assert len(sk.rows) == 2
    assert sk.row_sum() == Decimal("50")
    assert len(sk.rows_alt) == 2
    assert sk.rows_alt[0].max_score == Decimal("7")
    assert any("ambiguous_dual_scoring" in w for w in sk.warnings)


def test_multi_lot_ledger_warning() -> None:
    """同名表外分值声明多个分值（按标段分设）→ multi_lot_ledger 告警。"""
    pages = {
        1: "五、评标细则\n1、报价部分（50 分）每低1个百分点扣0.2分",
        2: "1、报价部分（60 分）每低1个百分点扣0.2分",
    }
    tables = {1: [[
        _hdr(["序号", "评审因素", "主/客观分", "分值", "评标细则"]),
        ["1", "业绩", "客观分", "2", "每个1分"],
    ]]}
    sk = parse_skeleton(tables, pages)
    assert sk is not None
    assert any("multi_lot_ledger" in w for w in sk.warnings)


def test_level_table_header_rejected() -> None:
    """「档次|分值区间|评分标准」档位表不是评分表（无序号/评审因素）——
    其差档"0-N分"行不得被当成计分行（样例项目 e2e 冻结失败根因）。"""
    tables = {4: [[
        _hdr(["档次", "分值区间", "评分标准"]),
        ["优", "17-20分", "技术方案完整、先进"],
        ["差", "0-5分", "技术方案不完整或存在明显缺陷"],
    ]]}
    assert parse_skeleton(tables, {4: ""}) is None
