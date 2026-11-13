"""开标一览表 Excel 抽价：K1/K2 族、汇总行兜底、口径记录、坏文件容错。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.parsing.router import extract_document
from app.pricing.extractor import derive_status, extract_company_price
from app.pricing.open_bid_xlsx import parse_open_bid_xlsx, parse_price_sheet_xlsx


def _wb(path: Path, sheets: dict[str, list[list[object]]]) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title[:31])
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


K1_ROWS = [
    ["开标一览表"],
    ["项目编号：B202501BA2001716010029"],
    ["标段", "产品名称", "不含增值税投标报价", "交货周期（是否满足招标文件要求）", "项目负责人"],
    [None, None, "（元/万套）", None, None],
    ["标段1", "利群（新版）商标条盒", 20186.58, "是", "冯向忠"],
    [None, "福样促试商标条盒", 10615.55, None, None],
    ["标段3", "利群（长嘴）商标条盒", "/", "是", "冯向忠"],
    ["注：报价必须包括除增值税外的所有费用。"],
    ["投标人：   乌兰浩特森辉印务有限公司"],
]


def test_k1_multi_row_sum(tmp_path: Path) -> None:
    p = _wb(tmp_path / "开标一览表.xlsx", {"开标一览表": K1_ROWS})
    ex, warns = parse_open_bid_xlsx(p)
    assert ex is not None
    assert ex.bid_price == Decimal("30802.13")  # 20186.58 + 10615.55
    assert ex.basis == "sum_of_rows"
    assert ex.unit == "元/万套"
    assert ex.tax_basis == "不含税"
    assert len(ex.items) == 2  # "/" 行跳过
    assert ex.items[0].product == "利群（新版）商标条盒"
    assert ex.stated_bidder == "乌兰浩特森辉印务有限公司"


def test_k1_total_row_cross_check(tmp_path: Path) -> None:
    rows = K1_ROWS[:7] + [["合计", None, 30802.13]] + K1_ROWS[7:]
    p = _wb(tmp_path / "开标一览表.xlsx", {"开标一览表": rows})
    ex, _ = parse_open_bid_xlsx(p)
    assert ex is not None
    assert ex.bid_price == Decimal("30802.13")
    assert ex.basis == "total_row"
    assert ex.cross_check["sum_match"] is True


def test_k1_total_row_mismatch_takes_total(tmp_path: Path) -> None:
    rows = K1_ROWS[:7] + [["合计", None, 99999.0]] + K1_ROWS[7:]
    p = _wb(tmp_path / "开标一览表.xlsx", {"开标一览表": rows})
    ex, _ = parse_open_bid_xlsx(p)
    assert ex is not None
    assert ex.bid_price == Decimal("99999")
    assert ex.cross_check["sum_match"] is False
    assert any("不一致" in w for w in ex.warnings)


def test_k1_single_row(tmp_path: Path) -> None:
    rows = [K1_ROWS[i] for i in (0, 2, 3, 4, 7)]
    p = _wb(tmp_path / "开标一览表.xlsx", {"开标一览表": rows})
    ex, _ = parse_open_bid_xlsx(p)
    assert ex is not None
    assert ex.bid_price == Decimal("20186.58")
    assert ex.basis == "single_row"


def test_k2_tax_rate_normalized(tmp_path: Path) -> None:
    rows = [
        ["开标一览表", None, "标段1："],
        ["序号", "产品名称", "不含增值税投标报价\n（元/万套）", "税率", "紧急交货周期", "项目负责人"],
        [1, "利群（软长嘴）商标条盒", 17734, 0.13, "是", "张雄"],
        [2, "摩登（南美S）商标条盒", 9770, 0.13, None, None],
        ["注：…"],
        ["投标人： 浙江翌星包装科技有限公司（盖单位公章）"],
    ]
    p = _wb(tmp_path / "浙江中烟开标一览表（标段一）.xlsx", {"开标一览表": rows})
    ex, _ = parse_open_bid_xlsx(p)
    assert ex is not None
    assert ex.source == "open_bid_xlsx_k2"
    assert ex.bid_price == Decimal("27504")  # 17734 + 9770
    assert ex.tax_rate == Decimal("0.13")
    assert ex.cross_check["incl_vat"] == Decimal("27504") * Decimal("1.13")


def test_k2_tax_rate_percent_string(tmp_path: Path) -> None:
    rows = [
        ["序号", "产品名称", "不含增值税投标报价（元/万套）", "税率"],
        [1, "产品甲", 100, "13%"],
    ]
    p = _wb(tmp_path / "开标一览表.xlsx", {"开标一览表": rows})
    ex, _ = parse_open_bid_xlsx(p)
    assert ex is not None
    assert ex.tax_rate == Decimal("0.13")


def test_price_sheet_anchor_rows(tmp_path: Path) -> None:
    rows = [
        ["投标分项报价表"],
        ["序号", "费用名称", "数量单位", "单价（不含增值税）", "合价（不含增值税）", "备注"],
        [1, "驻场服务", "2人", 559350, 1118700, "三年运维"],
        ["投标价（不含增值税,人民币元）", None, None, None, 1695000, "合价之和"],
        ["增值税税率", None, None, None, 0.06, None],
        ["投标价（含增值税,人民币元）", None, None, None, 1796700, None],
    ]
    p = _wb(tmp_path / "分项报价表——厦门海晟融创信息技术有限公司.xlsx", {"表1": rows})
    ex, _ = parse_price_sheet_xlsx(p)
    assert ex is not None
    assert ex.bid_price == Decimal("1796700")
    assert ex.tax_basis == "含税"
    assert ex.tax_rate == Decimal("0.06")
    assert ex.basis == "anchor_row"


def test_price_sheet_excl_only(tmp_path: Path) -> None:
    rows = [
        ["投标价（不含增值税,人民币元）", None, 1000],
        ["增值税税率", None, "13%"],
    ]
    p = _wb(tmp_path / "投标分项报价表.xlsx", {"表1": rows})
    ex, _ = parse_price_sheet_xlsx(p)
    assert ex is not None
    assert ex.bid_price == Decimal("1000")
    assert ex.tax_basis == "不含税"
    assert ex.cross_check["incl_vat"] == Decimal("1130")


def test_corrupt_xlsx_not_crash(tmp_path: Path) -> None:
    bad = tmp_path / "开标一览表.xlsx"
    bad.write_bytes(b"garbage not a workbook")
    ex, warns = parse_open_bid_xlsx(bad)
    assert ex is None
    assert warns and "无法读取" in warns[0]


def test_xls_skipped_with_warning(tmp_path: Path) -> None:
    (tmp_path / "开标分项.xls").write_bytes(b"\xd0\xcf\x11\xe0 fake biff")
    out = extract_company_price([tmp_path / "开标分项.xls"], "公司甲")
    assert out.bid_price is None
    assert derive_status(out) == "missing"
    assert any(".xls 已跳过" in w for w in out.warnings)


def test_extractor_priority_xlsx_over_pdf(tmp_path: Path) -> None:
    """开标一览表 xlsx 与 pdf 并存：xlsx 优先；数值不一致 → conflict 信息标记。"""
    _wb(tmp_path / "开标一览表.xlsx", {"开标一览表": [["产品名称", "不含增值税投标报价（元）"], ["甲", 100]]})
    pdf = tmp_path / "开标一览表.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")  # 通道 2 会失败于文本提取，不阻断
    out = extract_company_price([tmp_path / "开标一览表.xlsx", pdf], "公司甲")
    assert out.bid_price == Decimal("100")
    assert out.source == "open_bid_xlsx_k1"
    assert derive_status(out) in ("extracted", "conflict")


def test_xlsx_pseudo_pages(tmp_path: Path) -> None:
    """xlsx → 伪页文本入库链路（bid_text/证据引文依赖 document_pages）。"""
    p = _wb(
        tmp_path / "报价表.xlsx",
        {"开标一览表": [["产品", "报价"], ["甲", 100]], "明细": [["费用", "金额"], ["材料", 60]]},
    )
    doc = extract_document(str(p))
    assert doc.engine == "openpyxl"
    assert len(doc.pages) == 2
    assert "产品 | 报价" in doc.pages[0].text
    assert doc.pages[0].page_approximate is True
