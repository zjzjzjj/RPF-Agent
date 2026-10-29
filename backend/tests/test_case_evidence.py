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


def test_locate_section_index_range_page_before_业绩() -> None:
    """索引行"第45页到第71页 …… 业绩案例"（页码在前、业绩在后）必须命中。

    7.10 用友实证：旧正则只认"业绩在前"语序 → 不命中 → 被无邻近约束的 PAREN
    变体抢配到同页其他模块的（112-127）页 → 段错位到技术方案章节 → rows(0)。
    """
    pages = [
        PageRec(25, "商务技术文件索引目录\n第27页到第28页 2.1 投标保证金\n"
                    "第45页到第71页 2.8 投标人类似项目业绩案例\n"
                    "第112页到第127页 2.11 技术服务方案\n- 25 -", "text"),
        PageRec(45, "2.8 投标人类似项目业绩案例\n序号 项目名称 合同金额 合同签订时间", "text"),
    ]
    sec = locate_section(pages)
    assert sec.found
    assert (sec.start, sec.end) == (45, 71)
    assert sec.method == "index_offset"


def test_locate_section_paren_requires_业绩_line() -> None:
    """PAREN 变体只在含"业绩"的行内匹配（同页其他模块的（X-Y）页不得抢配）。"""
    pages = [
        PageRec(10, "详细评审索引表\n技术文件（112-127）页\n"
                    "见投标文件（45-71）页 投标人业绩\n- 10 -", "text"),
        PageRec(45, "投标人类似项目业绩案例 序号 项目名称 合同金额", "text"),
    ]
    sec = locate_section(pages)
    assert sec.found
    assert (sec.start, sec.end) == (45, 71), f"抢配到他模块区间：{sec}"


def test_locate_section_boundary_body_hit_not_truncated() -> None:
    """章节边界关键词命中在**正文深处**（页首 300 字符外）不得截断证据段。

    7.10 云锦实证：案例#1 合同扫描页正文「项目运维服务方案一份」@807 触发
    _BOUNDARY → 段被截在 p59，其后 60 余页材料全部丢失。真正的下章节标题
    （「乙方项目团队」@40）在页首区，正常截断。
    """
    pages = [
        PageRec(20, "业绩案例 序号 项目名称 合同金额", "text"),
        # 边界词在正文深处（>300 字符标题区之外）：前置足量正文
        PageRec(21, "合同正文开头" + "正文" * 200
                    + "（1）经甲方审核通过的项目运维服务方案一份；", "text"),
        PageRec(22, "乙方项目团队\n" + "表格" * 200, "text"),  # 真边界：页首标题
        PageRec(23, "后续章节", "text"),
    ]
    sec = locate_section(pages)
    assert sec.method == "heading_window"
    assert sec.end == 21, f"正文命中不应截断：{sec}"


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


def test_classify_vat_clause_not_invoice() -> None:
    """合同付款条款含'增值税专用发票'→ 不算发票（2026-10-23 先数假信号实证）。

    真实故障：先数全文无一张真发票，合同条款'收到乙方出具的…发票类型
    增值税专用发票…'与报价表'增值税'列被 _RE_INVOICE 误判 invoice=True
    → 业绩案例计分（真实评审 0）。
    """
    pages = [PageRec(10, "第2种方式支付合同价款：收到乙方出具的相应金额的合法发票"
                        "（发票类型增值税专用发票，货物或应税劳务，税率6%）后付款", "text")]
    b = _run_classify(pages, {}, [10])
    assert b.status["invoice"] is False


def test_classify_contract_account_block_not_invoice() -> None:
    """合同乙方账户信息含'纳税人识别号'→ 不算发票（先数 p165 实证）。"""
    pages = [PageRec(10, "纳税人识别号：91630000698540235A 税务开户银行：青海银行 "
                        "开户银行账号：400074673105017 4.6 甲方按约定向乙方支付合同款", "text")]
    b = _run_classify(pages, {}, [10])
    assert b.status["invoice"] is False


def test_classify_real_invoice_form_page_is_true() -> None:
    """真发票表单（购买方/发票号码字段）→ invoice=True 不误伤。"""
    pages = [PageRec(10, "浙江增值税专用发票 购买方：杭州诚道科技股份有限公司 "
                        "发票号码 3300241130 开票日期 2024年6月 价税合计 58000", "ocr")]
    b = _run_classify(pages, {}, [10])
    assert b.status["invoice"] is True


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


def test_classify_medium_text_image_page_is_unreadable() -> None:
    """200~800 字 + 图≥0.10 的「标题+扫描图」页 → None/unreadable（可救援）。

    7.10 实证：旧口径（<200）把这类页判 False/absent（"确认缺失"）且永远
    进不了救援名单。统一为 _low_evidence（<800）口径。
    """
    pages = [
        PageRec(10, "某某项目合同 甲方 乙方 合同金额", "text"),
        PageRec(11, "2. 发票扫描件\n" + "说明文字" * 40, "text"),  # ~330 字
    ]
    b = _run_classify(pages, {11: 0.20}, [10, 11])
    assert b.status["invoice"] is None
    assert b.reasons["invoice"] == "unreadable"


def test_classify_medium_text_no_image_stays_absent() -> None:
    """对照：同等文本量但无图 → 仍 False/absent（可读页缺席判定不受影响）。"""
    pages = [
        PageRec(10, "某某项目合同 甲方 乙方 合同金额", "text"),
        PageRec(11, "普通正文无图" + "说明文字" * 40, "text"),
    ]
    b = _run_classify(pages, {11: 0.0}, [10, 11])
    assert b.status["invoice"] is False
    assert b.reasons["invoice"] == "absent"


def test_material_ocr_targets_includes_scan_unverified_block() -> None:
    """scan_unverified 块同样进救援靶（OCR 后才能核验信号）。"""
    pages = [PageRec(i, "扫描页" * 30, "text") for i in range(10, 14)]
    ratios = {i: 0.5 for i in range(10, 14)}
    block = CaseBlock(
        row=CaseRow(1, "某项目", None, None, 10),
        pages=list(range(10, 14)),
        status={"contract": True, "invoice": None, "acceptance": None},
        reasons={"contract": "signal", "invoice": "scan_unverified",
                 "acceptance": "scan_unverified"},
    )
    sec = SectionGuess(10, 20, "index_offset", 1.0, 0)
    targets = material_ocr_targets(pages, ratios, sec, blocks=[block], per_block=2)
    assert targets == [10, 11]


def test_material_ocr_targets_fair_budget_across_blocks() -> None:
    """总预算块间轮转：10 块各需救援、max_total=6 → 覆盖 6 个不同块（防顺序饿死）。"""
    pages = [PageRec(i, "短文本页", "text") for i in range(10, 70)]
    ratios = {i: 0.3 for i in range(10, 70)}
    blocks = []
    for bno in range(10):
        p0 = 10 + bno * 5
        blocks.append(CaseBlock(
            row=CaseRow(bno + 1, f"项目{bno}", None, None, p0),
            pages=list(range(p0, p0 + 5)),
            status={"contract": None, "invoice": None, "acceptance": None},
            reasons={"contract": "scan_unverified", "invoice": "scan_unverified",
                     "acceptance": "scan_unverified"},
        ))
    sec = SectionGuess(10, 69, "index_offset", 1.0, 0)
    targets = material_ocr_targets(pages, ratios, sec, blocks=blocks, per_block=2, max_total=6)
    assert len(targets) == 6
    owners = {(t - 10) // 5 for t in targets}
    assert len(owners) == 6, f"预算必须块间轮转而非顺序截断：{targets}"


def test_material_ocr_targets_skips_ocr_source_pages() -> None:
    """已 OCR 页（source != text）不入靶（幂等，2026-10-21 语义）。"""
    pages = [PageRec(10, "短", "ocr"), PageRec(11, "短", "text")]
    ratios = {10: 0.5, 11: 0.2}
    sec = SectionGuess(10, 20, "index_offset", 1.0, 0)
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
