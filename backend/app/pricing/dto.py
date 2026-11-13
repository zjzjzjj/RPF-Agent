"""抽价统一数据类：Excel/PDF 双通道输出归一。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class PriceItem:
    """逐行报价（材料类多产品形态）。"""

    section: str | None = None  # 标段
    product: str | None = None  # 产品名称
    price: Decimal = Decimal(0)
    unit: str | None = None  # "元/万套" | "元"（记录不换算，同项目同口径）
    tax_basis: str | None = None  # "不含税" | "含税"


@dataclass
class PriceExtraction:
    """单通道抽价结果。

    source 取值（通道标识，落 Bidder.price_breakdown_json 留痕）：
    - open_bid_xlsx_k1 / open_bid_xlsx_k2：开标一览表 Excel（标段多行 / 单表+税率）
    - price_sheet_xlsx：分项报价表 Excel 汇总行兜底
    - open_bid_pdf：开标一览表 PDF（文本大小写金额）
    - main_pdf_text：主投标文件 PDF 文本五级兜底（engine/facts 既有链路）
    """

    bid_price: Decimal | None = None
    source: str = ""
    source_file: str | None = None
    basis: str | None = None  # total_row|single_row|sum_of_rows|anchor_row|total_label|window_scan|text
    unit: str | None = None
    tax_basis: str | None = None
    tax_rate: Decimal | None = None
    upper: str | None = None  # 大写金额原文
    upper_value: Decimal | None = None
    stated_bidder: str | None = None  # 表内"投标人：xxx"（与文件夹名互校）
    items: list[PriceItem] = field(default_factory=list)
    cross_check: dict = field(default_factory=dict)  # upper_match|sum_match|incl_vat 等
    warnings: list[str] = field(default_factory=list)


def derive_status(extraction: "PriceExtraction | None") -> str:
    """终态：missing（无价）| conflict（互校有出入，信息性）| extracted。"""
    if extraction is None or extraction.bid_price is None:
        return "missing"
    if any(v is False for v in extraction.cross_check.values()):
        return "conflict"
    return "extracted"


def cross_check_json(extraction: "PriceExtraction") -> dict:
    """cross_check → 可入 JSON 列的形态（Decimal → str；attempts 剔除）。"""
    return {
        k: (str(v) if isinstance(v, Decimal) else v)
        for k, v in extraction.cross_check.items()
        if k != "attempts"
    }
