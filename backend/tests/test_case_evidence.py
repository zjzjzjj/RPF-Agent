# -*- coding: utf-8 -*-
"""业绩案例证据抽取回归（2026-10-21）：金额解析 / 章节定位 / 材料三态 / OCR 目标。

真实背景：4 份标书业绩全 0，根因是（1）候选页截断、（2）发票页未 OCR、（3）只靠
LLM 猜案例。这里锁住确定性抽取的关键行为（都不调 LLM、不开 PDF）。
"""
from __future__ import annotations

from decimal import Decimal

from app.evidence.case_evidence import (
    CaseBlock,
    CaseRow,
    PageRec,
    SectionGuess,
    build_case_digest,
    classify_materials,
    link_case_blocks,
    locate_section,
    material_ocr_targets,
    parse_amount_yuan,
)


# ---------------------------------------------------------------------------
# 金额
# ---------------------------------------------------------------------------
def test_parse_amount_variants() -> None:
    assert parse_amount_yuan("¥96,000, 000.00 元") == Decimal("96000000.00")
    assert parse_amount_yuan("160万") == Decimal("1600000")
    assert parse_amount_yuan("84.168万") == Decimal("841680.0")
    assert parse_amount_yuan("RMB9 3.6 万元") == Decimal("936000.0")
    assert parse_amount_yuan("1665000.00 元") == Decimal("1665000.00")
    assert parse_amount_yuan("800,000") == Decimal("800000")
    assert parse_amount_yuan("80万元") == Decimal("800000")


def test_parse_amount_header_unit_wan() -> None:
    # 表头"合同金额（万元）" + 单元格"160" → 160 万
    assert parse_amount_yuan("160", header_unit_yuan=10000) == Decimal("1600000")


def test_parse_amount_none_and_zero() -> None:
    assert parse_amount_yuan("") is None
    assert parse_amount_yuan(None) is None
    assert parse_amount_yuan("0") is None


# ---------------------------------------------------------------------------
# 章节定位
# ---------------------------------------------------------------------------
def _pages_with_index() -> list[PageRec]:
    pages = [
        PageRec(94, "详细评审索引表\n2.8 投标人类似项目业绩案例 第118页到第167页\n- 86 -", "text"),
        PageRec(126, "投标人类似项目业绩案例\n序号 项目名称 委托单位 合同主要内容 合同金额 合同签订时间", "text"),
    ]
    for pno in range(127, 176):
        pages.append(PageRec(pno, f"第{pno}页正文", "text"))
    return pages


def test_locate_section_index_offset() -> None:
    sec = locate_section(_pages_with_index())
    assert sec.found
    assert (sec.start, sec.end) == (126, 175)
    assert sec.method == "index_offset"
    assert sec.offset == 8


def test_locate_section_near_when_offset_slightly_off() -> None:
    pages = _pages_with_index()
    # 表头页实际在 130（超出索引映射 126 的 ±3 窗）→ 就近搜索应命中 130
    pages = [p for p in pages if p.page_no != 126]
    pages.append(PageRec(130, "序号 项目名称 合同金额 合同签订时间", "text"))
    sec = locate_section(pages)
    assert sec.found
    assert sec.start == 130
    assert sec.method == "index_offset_near"


def test_locate_section_fallback_heading_window() -> None:
    pages = [
        PageRec(10, "封面", "text"),
        PageRec(20, "业绩案例 序号 项目名称 合同金额 合同签订时间", "text"),
        PageRec(21, "案例内容", "text"),
        PageRec(22, "保密承诺书 正文" + "x" * 300, "text"),
        PageRec(23, "下一节", "text"),
    ]
    sec = locate_section(pages)
    assert sec.found
    assert sec.start == 20
    assert sec.method == "heading_window"
    assert sec.end <= 22  # 在章节边界前截断


def test_locate_section_none() -> None:
    sec = locate_section([PageRec(1, "毫无相关内容", "text")])
    assert not sec.found


# ---------------------------------------------------------------------------
# 案例 → 材料页绑定
# ---------------------------------------------------------------------------
def test_link_case_blocks_searches_after_table() -> None:
    pages = [
        PageRec(10, "序号 项目名称 合同金额 甲项目 乙项目", "text"),  # 表页：两名字都在
        PageRec(11, "甲项目 合同正文 甲方 乙方", "text"),
        PageRec(12, "甲项目 发票", "text"),
        PageRec(13, "乙项目 合同正文 甲方 乙方", "text"),
        PageRec(14, "乙项目 验收", "text"),
    ]
    rows = [
        CaseRow(1, "甲项目", Decimal("1000000"), None, 10),
        CaseRow(2, "乙项目", Decimal("2000000"), None, 10),
    ]
    sec = locate_section([PageRec(10, "序号 项目名称 合同金额", "text")])  # 仅表头
    sec = type(sec)(10, 14, "index_offset", 1.0, 0)  # 手工构造证据段
    blocks = link_case_blocks(rows, pages, sec)
    assert blocks[0].pages[0] == 11 and blocks[0].pages[-1] == 12
    assert blocks[1].pages[0] == 13 and blocks[1].pages[-1] == 14


# ---------------------------------------------------------------------------
# 材料三态
# ---------------------------------------------------------------------------
def _run_classify(pages, ratios, block_pages):
    block = CaseBlock(row=CaseRow(1, "某项目", Decimal("1000000"), None, 10), pages=block_pages)
    classify_materials([block], pages, ratios)
    return block


def test_classify_all_signals_true() -> None:
    pages = [
        PageRec(10, "甲方 乙方 项目合同 合同金额 1000000", "text"),
        PageRec(11, "电子发票 发票号码 12345 开票日期 2024年1月1日 价税合计", "text"),
        PageRec(12, "验收报告 验收结论 验收合格 验收日期", "text"),
    ]
    b = _run_classify(pages, {}, [10, 11, 12])
    assert b.status == {"contract": True, "invoice": True, "acceptance": True}
    assert b.pages_by_material["invoice"] == [11]


def test_classify_unreadable_is_null() -> None:
    pages = [PageRec(10, "某某合同 甲方 乙方", "text"), PageRec(11, "8.1 发票 - 120 -", "text")]
    b = _run_classify(pages, {11: 0.22}, [10, 11])
    assert b.status["contract"] is True
    assert b.status["invoice"] is None
    assert b.reasons["invoice"] == "unreadable"


def test_classify_absent_on_readable_page_is_false() -> None:
    pages = [
        PageRec(10, "甲方 乙方 项目合同", "text"),
        PageRec(11, "本页只有技术方案正文，没有材料" + "x" * 300, "text"),
    ]
    b = _run_classify(pages, {}, [10, 11])
    assert b.status["invoice"] is False
    assert b.reasons["invoice"] == "absent"


def test_classify_contract_boilerplate_not_invoice() -> None:
    # 合同付款条款提到"发票"字样，但没有发票强信号 → 不算发票
    pages = [PageRec(10, "甲方 乙方 项目合同 付款前乙方应开具发票", "text")]
    b = _run_classify(pages, {}, [10])
    assert b.status["invoice"] is False


# ---------------------------------------------------------------------------
# OCR 目标
# ---------------------------------------------------------------------------
def test_material_ocr_targets_block_capped() -> None:
    """块级目标：只取"材料不可读"案例块的前 N 页，避免整段扫描页全送 OCR。"""
    pages = [PageRec(i, "短文本 - 1 -", "text") for i in range(10, 20)]
    ratios = {i: 0.2 for i in range(10, 20)}
    block = CaseBlock(
        row=CaseRow(1, "某项目", None, None, 10),
        pages=list(range(10, 15)),
        status={"contract": True, "invoice": None, "acceptance": True},
        reasons={"contract": "signal", "invoice": "unreadable", "acceptance": "signal"},
    )
    sec = SectionGuess(10, 20, "index_offset", 1.0, 0)
    targets = material_ocr_targets(pages, ratios, sec, blocks=[block], per_block=2)
    assert targets == [10, 11]


def test_material_ocr_targets_select_unreadable_material_pages() -> None:
    pages = [
        PageRec(10, "序号 项目名称 合同金额", "text"),
        PageRec(11, "8.1.2 发票 - 124 -", "text"),      # 短文本 + 大图 → 选
        PageRec(12, "普通正文" + "x" * 600, "text"),     # 文本够长 → 不选
        PageRec(13, "验收报告 - 126 -", "text"),          # 短文本但无图 → 不选
    ]
    ratios = {11: 0.22, 12: 0.0, 13: 0.02}
    sec = type(locate_section([]))(10, 20, "index_offset", 1.0, 0)
    targets = material_ocr_targets(pages, ratios, sec)
    assert targets == [11]


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------
def test_build_case_digest_bounded_and_has_index() -> None:
    pages = [PageRec(10, "正文" * 500, "text"), PageRec(11, "发票内容" * 500, "text")]
    b = CaseBlock(
        row=CaseRow(3, "某项目", Decimal("1000000"), "2024-01-01", 10),
        pages=[10, 11],
        pages_by_material={"invoice": [11]},
    )
    digest = build_case_digest([b], pages, max_chars=500)
    assert "【案例3】" in digest
    assert len(digest) <= 800  # 允许最后一小段略超
