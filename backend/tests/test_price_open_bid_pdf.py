"""开标一览表 PDF 抽价：服务/材料/设备三形态、大小写互校、真 PDF 冒烟。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.pricing.open_bid_pdf import parse_open_bid_pdf

SERVICE_TEXT = """开标一览表
招标编号: C202501BA2001716013881
招标名称: 浙江中烟工业有限责任公司2025-2028年行业烟叶平台运维项目
投标总价（含税）(元)   税率   项目负责人   投标保证金   备注
大写:壹佰柒拾玖万陆仟柒佰元整
小写：1796700          6%     高文强       10000元      无
投标人：厦门海晟融创信息技术有限公司
"""

MATERIAL_TEXT = """开标一览表
项目编号：B202501BA2001716010029
利群（新版）商标条盒不含增值税投标报价（元/万套） 大写:贰万零壹佰捌拾陆元伍角捌分 小写：20186.58
福样促试商标条盒不含增值税投标报价（元/万套） 大写:壹万零陆佰壹拾伍元伍角伍分
小写：10615.55
利群（长嘴）商标条盒不含增值税投标报价（元/万套）
投标人：乌兰浩特森辉印务有限公司
"""

EQUIP_TEXT = """开标一览表
投标总价（含增值税，人民币元）
大写:贰佰壹拾玖万柒仟肆佰柒拾柒元壹角
小写：2197477.1
"""

INCONSISTENT_TEXT = """开标一览表
投标总价（含税）(元)   税率
大写:壹佰柒拾万元整
小写：1796700
"""


# 真实样本（11.19 浩鲸，经 extract_document 后的原行序）：大写金额被拆成不相邻两处，
# 前半截后紧跟税率/负责人/保证金列；旧实现只取首行
# "壹佰捌拾伍万柒仟玖佰玖"=1857909 与小写 1857999.8 对不上 → 互校误报冲突。
WRAPPED_UPPER_TEXT = (
    "开标一览表\n招标编号: C202501BA2001716013881\n"
    "招标名称: 浙江中烟工业有限责任公司2025-2028年行业烟叶平台运维项目\n"
    "投标总价（含税）(元) 税率 项目负责人 投标保证金 备注\n"
    "大写:壹佰捌拾伍万柒仟玖佰玖 6 王伟 10000 无\n"
    "拾玖元捌角\n小写：1857999.8"
)


def test_upper_amount_wrapped_across_lines(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    _fake_pdf(monkeypatch, WRAPPED_UPPER_TEXT)
    p = tmp_path / "开标一览表.pdf"
    p.write_bytes(b"%PDF-1.4")
    ex, _ = parse_open_bid_pdf(p)
    assert ex is not None
    assert ex.bid_price == Decimal("1857999.8")
    assert ex.upper_value == Decimal("1857999.8")  # 跨行拼接后与小写一致
    assert ex.cross_check["upper_match"] is True
    assert not ex.warnings


def _fake_pdf(monkeypatch, text: str) -> None:  # noqa: ANN001
    monkeypatch.setattr("app.pricing.open_bid_pdf._pdf_text", lambda p: text)


def test_service_form(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    _fake_pdf(monkeypatch, SERVICE_TEXT)
    p = tmp_path / "开标一览表.pdf"
    p.write_bytes(b"%PDF-1.4")
    ex, _ = parse_open_bid_pdf(p)
    assert ex is not None
    assert ex.bid_price == Decimal("1796700")
    assert ex.basis == "total_label"
    assert ex.upper_value == Decimal("1796700")
    assert ex.cross_check["upper_match"] is True
    assert ex.tax_rate == Decimal("0.06")
    assert ex.stated_bidder == "厦门海晟融创信息技术有限公司"


def test_upper_lower_mismatch_takes_lower(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    _fake_pdf(monkeypatch, INCONSISTENT_TEXT)
    p = tmp_path / "开标一览表.pdf"
    p.write_bytes(b"%PDF-1.4")
    ex, _ = parse_open_bid_pdf(p)
    assert ex is not None
    assert ex.bid_price == Decimal("1796700")  # 取小写
    assert ex.cross_check["upper_match"] is False
    assert any("大小写金额不一致" in w for w in ex.warnings)


def test_material_multi_row(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    _fake_pdf(monkeypatch, MATERIAL_TEXT)
    p = tmp_path / "开标一览表.pdf"
    p.write_bytes(b"%PDF-1.4")
    ex, _ = parse_open_bid_pdf(p)
    assert ex is not None
    assert ex.basis == "sum_of_rows"
    assert ex.bid_price == Decimal("30802.13")
    assert ex.unit == "元/万套"
    assert ex.tax_basis == "不含税"
    assert [i.product for i in ex.items] == ["利群（新版）商标条盒", "福样促试商标条盒"]
    assert ex.cross_check["upper_match"] is True  # 逐行大小写互校
    assert ex.stated_bidder == "乌兰浩特森辉印务有限公司"


def test_equipment_form(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    _fake_pdf(monkeypatch, EQUIP_TEXT)
    p = tmp_path / "开标一览表.pdf"
    p.write_bytes(b"%PDF-1.4")
    ex, _ = parse_open_bid_pdf(p)
    assert ex is not None
    assert ex.bid_price == Decimal("2197477.1")
    assert ex.cross_check["upper_match"] is True
    assert ex.tax_basis == "含税"


def test_no_price_returns_none(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    _fake_pdf(monkeypatch, "开标一览表\n投标人：某某公司\n")
    p = tmp_path / "开标一览表.pdf"
    p.write_bytes(b"%PDF-1.4")
    ex, warns = parse_open_bid_pdf(p)
    assert ex is None
    assert warns and "未抽出报价" in warns[0]


def test_real_pdf_roundtrip(tmp_path: Path) -> None:
    """真 PDF（reportlab CID 中文）→ pymupdf 文本 → 抽价，全链路冒烟。"""
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    p = tmp_path / "开标一览表.pdf"
    c = canvas.Canvas(str(p))
    c.setFont("STSong-Light", 12)
    for i, line in enumerate(SERVICE_TEXT.strip().split("\n")):
        c.drawString(72, 750 - i * 24, line)
    c.save()
    assert reportlab

    ex, warns = parse_open_bid_pdf(p)
    assert ex is not None, warns
    assert ex.bid_price == Decimal("1796700")
    assert ex.cross_check["upper_match"] is True
