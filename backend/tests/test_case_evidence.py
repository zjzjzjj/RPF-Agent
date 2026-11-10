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
    parse_case_table,
    parse_case_table_at_section,
    resolve_index_pages,
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


# ---------------------------------------------------------------------------
# resolve_index_pages（索引页码指针泛化，2026-11-02 run77 实证）
# ---------------------------------------------------------------------------
def test_resolve_index_pages_run77_service_commitment_split_lines() -> None:
    """run77 c54 实形态：标签「服务承诺」与指针「见投标文件（342-344）页」被 PDF
    表格拆行隔开 2 行，且该页无"索引"字样（只有"见投标文件"）。line_window=3
    应能关联；旧口径（同线 + 索引门）整页扑空。"""
    pages = [
        PageRec(26,
                "针对本项目的服务承诺、服务响应，当系统发生\n"
                "驻厂人员不能解决的重大故障时，承诺远程服务\n"
                "团队在4 小时内到达现场得4 分,承诺5 小时内到\n"
                "服务承诺\n"
                "达现场得3 分,承诺6 小时内到达现场得2 分,承\n"
                "17 客观分 见投标文件（342-344）页\n"
                "诺7 小时内到达现场得1 分，其他不得分。\n"
                "书面承诺函为准，承诺函需加盖投标人公章，并\n"
                "附具体实施方案。响应地点为：\n"
                "26", "text"),
    ]
    sec = resolve_index_pages(pages, "服务承诺", line_window=3)
    assert sec.found
    assert (sec.start, sec.end) == (342, 344)
    assert sec.offset == 0  # 印刷页码 26 == 物理页 26


def test_resolve_index_pages_single_page_paren() -> None:
    """单页指针变体「见投标文件（39）页」（run77 c38 ITSS 形态）→ (39, 39)。"""
    pages = [
        PageRec(26,
                "1 投标人综合实力-ITSS信息技术服务标准符合性证书\n"
                "17 客观分 见投标文件（39）页\n26", "text"),
    ]
    sec = resolve_index_pages(pages, "ITSS", line_window=3)
    assert sec.found
    assert (sec.start, sec.end) == (39, 39)


def test_resolve_index_pages_prefers_keyword_line_over_other_modules() -> None:
    """同页多模块指针：不得抢配邻近行其他模块的（X-Y）页——取关键词行距最小者。"""
    pages = [
        PageRec(10,
                "详细评审索引表\n"
                "技术文件（112-127）页\n"
                "16 质量保障措施 见投标文件（322-342）页\n"
                "17 服务承诺 见投标文件（342-344）页\n"
                "10", "text"),
    ]
    sec = resolve_index_pages(pages, "服务承诺", line_window=3)
    assert sec.found
    assert (sec.start, sec.end) == (342, 344), f"抢配到他模块区间：{sec}"


def test_resolve_index_pages_line_window_zero_requires_same_line() -> None:
    """line_window=0（业绩旧口径）：指针与关键词必须同线，拆行形态不认。"""
    pages = [
        PageRec(26,
                "服务承诺\n"
                "17 客观分 见投标文件（342-344）页\n26", "text"),
    ]
    assert not resolve_index_pages(pages, "服务承诺", line_window=0).found
    # 同线形态照认
    pages2 = [
        PageRec(26, "服务承诺 见投标文件（342-344）页\n26", "text"),
    ]
    sec = resolve_index_pages(pages2, "服务承诺", line_window=0)
    assert sec.found and (sec.start, sec.end) == (342, 344)


def test_resolve_index_pages_no_index_gate_page_ignored() -> None:
    """非索引页（无"索引/详细评审/见投标文件"字样）不解析。"""
    pages = [
        PageRec(5, "服务承诺（342-344）页随便写写", "text"),
    ]
    assert not resolve_index_pages(pages, "服务承诺", line_window=3).found


def test_resolve_index_pages_offset_shifts_physical() -> None:
    """印刷页码与物理页错位时按 offset 换算（页脚 - 86 - @物理 94）。"""
    pages = [
        PageRec(94, "详细评审索引表\n服务承诺 见投标文件（342-344）页\n- 86 -", "text"),
    ]
    sec = resolve_index_pages(pages, "服务承诺", line_window=3)
    assert sec.found
    assert (sec.start, sec.end) == (350, 352)
    assert sec.offset == 8


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
# 业绩表路径（阶段3a 2026-10-30）：表头变体 + 多起点重试
# ---------------------------------------------------------------------------
def _make_table_pdf(path, specs):
    """合成 PDF（真实 PyMuPDF find_tables 路径）：specs=[(kind, payload)]。

    kind='text' → payload 为页文本；kind='table' → payload=(header, data_rows)，
    画线表格 + china-s 内置中文字体（helv 无 CJK 字形，2026-10-30 实测）。
    """
    import pymupdf

    doc = pymupdf.open()
    try:
        for kind, payload in specs:
            page = doc.new_page()
            if kind == "text":
                page.insert_text((50, 50), payload, fontname="china-s")
                continue
            header, data = payload
            col_w, row_h = 120, 36  # 4 列 520pt < A4 宽 595，防末列被页边裁掉
            for r, row in enumerate([header] + data):
                for c, cell in enumerate(row):
                    rect = pymupdf.Rect(
                        40 + c * col_w, 40 + r * row_h,
                        40 + (c + 1) * col_w, 40 + (r + 1) * row_h,
                    )
                    page.draw_rect(rect, color=(0, 0, 0), width=0.5)
                    page.insert_textbox(rect, cell, fontsize=10, fontname="china-s")
        doc.save(str(path))
    finally:
        doc.close()
    return str(path)


def test_parse_case_table_header_variant_zhongbiao_amount(tmp_path) -> None:
    """表头变体「中标金额/签订日期」与「合同金额/合同签订时间」解析出同样 CaseRow。

    旧 _is_header_row/_has_case_table_header 硬绑"项目名称"等词——真实标书常见
    业绩名称/中标金额变体（阶段3a 放宽为通用三元组后必须行为一致）。
    """
    data = [
        ["1", "甲项目", "160万元", "2024年1月1日"],
        ["2", "乙项目", "80万元", "2024年3月5日"],
    ]
    pa = _make_table_pdf(
        tmp_path / "classic.pdf",
        [("table", (["序号", "项目名称", "合同金额", "合同签订时间"], data))],
    )
    pb = _make_table_pdf(
        tmp_path / "variant.pdf",
        [("table", (["序号", "项目名称", "中标金额", "签订日期"], data))],
    )
    rows_a = parse_case_table(pa, 1, 1, 4)
    rows_b = parse_case_table(pb, 1, 1, 4)
    assert len(rows_a) == 2 and len(rows_b) == 2
    assert [(r.name, r.amount_yuan, r.sign_date) for r in rows_a] == [
        (r.name, r.amount_yuan, r.sign_date) for r in rows_b
    ]
    assert rows_b[0].name == "甲项目"
    assert rows_b[0].amount_yuan == Decimal("1600000")
    assert rows_b[0].sign_date == "2024-01-01"


# ---------------------------------------------------------------------------
# 材料三态
# ---------------------------------------------------------------------------
def _run_classify(pages, ratios, block_pages, tag_pages=None):
    block = CaseBlock(row=CaseRow(1, "某项目", Decimal("1000000"), None, 10), pages=block_pages)
    classify_materials([block], pages, ratios, tag_pages=tag_pages)
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
# 材料索引页级标签先验（tag_signal，阶段4 2026-10-30）
# ---------------------------------------------------------------------------
def test_classify_tag_signal_on_scan_unverified_page() -> None:
    """scan_unverified 候选页命中页级强标签 → True/reason=tag_signal。

    扫描页文本层看不见 ≠ 不存在；material_tags 页级强标签（rule+CONF_STRONG）
    是入库时对同类页的确定性盖章，借为三态先验。
    """
    pages = [
        PageRec(10, "某某项目合同 甲方 乙方 合同金额", "text"),
        # >800 字 + 图占比 0.5 → 只进 scan_pages（不进 unreadable）
        PageRec(11, "整页扫描图占位" + "扫描件文字" * 160, "text"),
    ]
    b = _run_classify(pages, {11: 0.5}, [10, 11],
                      tag_pages={11: {"invoice": ("发票", 0.85)}})
    assert b.status["invoice"] is True
    assert b.reasons["invoice"] == "tag_signal"
    assert b.pages_by_material["invoice"] == [11]


def test_classify_tag_signal_on_unreadable_low_evidence_page() -> None:
    """低证据页（unreadable）命中 acceptance 标签 → True/tag_signal。"""
    pages = [
        PageRec(10, "某某项目合同 甲方 乙方 合同金额", "text"),
        PageRec(11, "材料扫描件 - 120 -", "text"),  # 短文本 + 图 0.22 → unreadable
    ]
    b = _run_classify(pages, {11: 0.22}, [10, 11],
                      tag_pages={11: {"acceptance": ("业主证明", 0.9)}})
    assert b.status["acceptance"] is True
    assert b.reasons["acceptance"] == "tag_signal"
    assert b.pages_by_material["acceptance"] == [11]


def test_classify_regex_signal_wins_over_tag() -> None:
    """正则与标签同时命中（不同页）→ reason 仍 signal（正则优先）。"""
    pages = [
        PageRec(10, "某某项目合同 甲方 乙方 合同金额", "text"),
        PageRec(11, "电子发票 发票号码 12345 开票日期 2024年1月1日 价税合计", "text"),
        PageRec(12, "整页扫描图占位" + "扫描件文字" * 160, "text"),
    ]
    b = _run_classify(pages, {12: 0.5}, [10, 11, 12],
                      tag_pages={11: {"invoice": ("发票", 0.9)},
                                 12: {"invoice": ("发票", 0.85)}})
    assert b.status["invoice"] is True
    assert b.reasons["invoice"] == "signal"
    assert b.pages_by_material["invoice"] == [11]


def test_classify_tag_never_flips_absent() -> None:
    """全页可读且无信号 + 标签注入 → 仍 False/absent（锁死 absent 不翻转）。"""
    pages = [
        PageRec(10, "甲方 乙方 项目合同", "text"),
        PageRec(11, "本页只有技术方案正文，没有材料" + "x" * 300, "text"),
    ]
    b = _run_classify(pages, {}, [10, 11],
                      tag_pages={11: {"invoice": ("发票", 0.9)}})
    assert b.status["invoice"] is False
    assert b.reasons["invoice"] == "absent"


def test_classify_tag_pages_none_keeps_legacy_behavior() -> None:
    """tag_pages=None → 与旧口径完全一致（低证据页 None/unreadable）。"""
    pages = [PageRec(10, "某某合同 甲方 乙方", "text"), PageRec(11, "8.1 发票 - 120 -", "text")]
    b = _run_classify(pages, {11: 0.22}, [10, 11], tag_pages=None)
    assert b.status["contract"] is True
    assert b.status["invoice"] is None
    assert b.reasons["invoice"] == "unreadable"


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


# --- R1-1 窗口延伸（2026-11-03 run78 ITSS 实证）：双向防线 -------------------

def _pages_with_single_pointer() -> list[PageRec]:
    """「见投标文件（39）页」指针：p39 标题页、p40-42 证书内容、p43 下一节。"""
    pages = [
        PageRec(21, "详细评审索引表\nITSS 信息技术服务标准符合性证书 见投标文件（39）页\n", "text"),
        PageRec(39, "2.7 投标人相关资质文件\n2.7.1 ITSS 信息技术服务标准符合性证书\n39", "text"),
        PageRec(40, "证书编号 ITSS-123 有效期至 2027年 发证机构 中国电子工业标准化技术协会", "ocr"),
        PageRec(41, "复印件加盖投标人公章 备查", "text"),
        PageRec(42, "2.7.2 营业执照复印件 统一社会信用代码 91330100XXXXXXXX", "text"),
        PageRec(43, "售后服务方案\n" + "本投标人承诺提供全面的售后服务体系与培训服务保障。" * 30, "text"),
        PageRec(44, "售后服务方案续页正文" + "内容" * 300, "text"),
    ]
    return pages


def test_resolve_index_pages_extend_single_pointer_title_page() -> None:
    """用友式：单页指针落标题页 → 延伸到下节边界，证书页进窗口（(39,42)）。"""
    sec = resolve_index_pages(_pages_with_single_pointer(), "ITSS",
                              line_window=3, extend=True)
    assert sec.found
    assert (sec.start, sec.end) == (39, 42)


def test_resolve_index_pages_default_no_extend() -> None:
    """兼容门：默认 extend=False 行为逐字节不变（单页指针仍是 (39,39)）。"""
    sec = resolve_index_pages(_pages_with_single_pointer(), "ITSS", line_window=3)
    assert sec.found
    assert (sec.start, sec.end) == (39, 39)


def test_resolve_index_pages_extend_skips_non_title_page() -> None:
    """德勤式：目标页是长正文（非标题页）→ 不延伸（延伸只救标题页死区间）。"""
    pages = _pages_with_single_pointer()
    pages = [p for p in pages if p.page_no != 39]
    pages.insert(1, PageRec(39, "本节收录证书资料。" + "详细内容" * 200, "text"))
    sec = resolve_index_pages(pages, "ITSS", line_window=3, extend=True)
    assert sec.found
    assert (sec.start, sec.end) == (39, 39)


def test_resolve_index_pages_extend_capped() -> None:
    """15 页封顶：无下节边界时不得吞掉后续大半本。"""
    pages = [
        PageRec(21, "详细评审索引表\nITSS 见投标文件（39）页\n", "text"),
        PageRec(39, "2.7 资质文件\n39", "text"),
    ]
    for pno in range(40, 80):
        pages.append(PageRec(pno, f"第{pno}页正文" + "内容" * 100, "text"))
    sec = resolve_index_pages(pages, "ITSS", line_window=3, extend=True)
    assert sec.found
    assert sec.end == 39 + 15 - 1  # extend_max_pages=15 封顶（含起始页 39..53）
