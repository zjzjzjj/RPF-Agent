"""开标一览表 Excel 抽价（优先通道）+ 分项报价表汇总行兜底。

真实格式族（数据001 实证）：
- K1 标段多行型：表头 `标段|产品名称|不含增值税投标报价（元/万套）|交货周期|项目负责人`，
  多行产品，不投的行留空或 `/`，表头下常有"（元/万套）"单位子行。
- K2 单表+税率型：表头含 `税率`（0.13 或 "13%"），其余 sheet 是分项报价明细。
- 分项报价表兜底：锚点行 `投标价（含增值税,人民币元）`/`投标价（不含增值税,人民币元）`/
  `增值税税率`（服务/设备类有；材料类总价只在开标一览表）。

口径：单位（元/万套 vs 元）与含税口径**记录不换算**（同项目同口径）；
税率归一为 Decimal 小数（13%"→0.13，13→0.13）。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.pricing.dto import PriceExtraction, PriceItem

# 表头识别：命中 ≥2 个关键词的行 = 表头行（前 10 行内）
_HEADER_KEYWORDS = (
    "标段", "产品名称", "投标报价", "投标价", "不含增值税", "含增值税",
    "税率", "交货周期", "项目负责人", "序号", "费用名称", "设备名称",
)
_TOTAL_KEYS = ("合计", "总计", "投标总价", "投标价合计")
_STOP_PREFIXES = ("注", "注：", "注:", "投标人", "日  期", "日期", "说明")

_FW_NUM_TRANS = str.maketrans("０１２３４５６７８９．，－＋％", "0123456789.,-+%")


def _to_decimal(v: object) -> Decimal | None:
    """单元格值 → Decimal（数值/文本数字；`/`、空、其它文本 → None）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        try:
            return Decimal(str(v))
        except InvalidOperation:
            return None
    t = str(v).translate(_FW_NUM_TRANS).strip().replace(",", "").replace("，", "").replace(" ", "")
    if not t or not any(ch.isdigit() for ch in t):
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _norm_rate(v: object) -> Decimal | None:
    """税率归一为小数：0.13 / "13%" / 13 → Decimal("0.13")。"""
    if v is None:
        return None
    if isinstance(v, str):
        t = v.translate(_FW_NUM_TRANS).strip().rstrip("%").strip()
        num = _to_decimal(t)
    else:
        num = _to_decimal(v)
    if num is None:
        return None
    if num > 1:
        num = num / 100
    return num


def _cell_text(v: object) -> str:
    return "" if v is None else str(v).strip()


def _pick_price_col(header: list[str]) -> int | None:
    """报价列：表头含"投标报价/投标价/报价"的列（优先最精确的"不含增值税"）。"""
    best: tuple[int, int] | None = None  # (score, col)
    for i, h in enumerate(header):
        if "报价" not in h and "投标价" not in h:
            continue
        score = 0
        if "不含增值税" in h:
            score = 3
        elif "含增值税" in h:
            score = 2
        elif "投标价" in h or "投标报价" in h:
            score = 1
        if best is None or score > best[0]:
            best = (score, i)
    return best[1] if best else None


def _header_meta(header: list[str]) -> dict:
    """表头 → 列定位与口径（单位/含税）。"""
    price_col = _pick_price_col(header)
    section_col = next((i for i, h in enumerate(header) if "标段" in h), None)
    product_col = next((i for i, h in enumerate(header) if "产品名称" in h or "产品" == h.strip()), None)
    tax_col = next((i for i, h in enumerate(header) if "税率" in h), None)
    price_header = header[price_col] if price_col is not None else ""
    unit = "元/万套" if "元/万套" in price_header or "万套" in price_header else (
        "元" if "元" in price_header else None
    )
    if "不含增值税" in price_header:
        tax_basis = "不含税"
    elif "含增值税" in price_header:
        tax_basis = "含税"
    else:
        tax_basis = None
    return {
        "price_col": price_col,
        "section_col": section_col,
        "product_col": product_col,
        "tax_col": tax_col,
        "unit": unit,
        "tax_basis": tax_basis,
    }


def _find_header(rows: list[tuple], max_scan: int = 10) -> tuple[int, dict] | None:
    for i, row in enumerate(rows[:max_scan]):
        header = [_cell_text(c) for c in row]
        hits = sum(1 for h in header for k in _HEADER_KEYWORDS if k in h)
        if hits >= 2:
            meta = _header_meta(header)
            if meta["price_col"] is not None:
                return i, meta
    return None


def _is_stop_row(row: tuple) -> bool:
    for c in row:
        t = _cell_text(c)
        if not t:
            continue
        return any(t.startswith(p) for p in _STOP_PREFIXES) or t.startswith("投标人")
    return False


def parse_open_bid_xlsx(path: Path) -> tuple[PriceExtraction | None, list[str]]:
    """开标一览表 .xlsx → PriceExtraction（K1/K2 族）。

    返回 (结果, 警告)；不适用/读不出返回 (None, 警告)。
    """
    warnings: list[str] = []
    try:
        from openpyxl import load_workbook
    except ImportError:
        return None, ["openpyxl 未安装，跳过 Excel 抽价通道"]
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        return None, [f"Excel 无法读取（可能加密或损坏）：{path.name}：{exc}"]

    try:
        # 名字含"开标"的 sheet 优先（K2 的明细 sheet 不含"开标"）
        sheets = sorted(wb.worksheets, key=lambda w: 0 if "开标" in w.title else 1)
        for ws in sheets:
            rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
            found = _find_header(rows)
            if found is None:
                continue
            h_idx, meta = found
            ex = _parse_rows(rows, h_idx, meta, path)
            if ex is not None:
                return ex, warnings
        return None, warnings + [f"开标一览表 Excel 未识别出报价行：{path.name}"]
    finally:
        wb.close()


def _parse_rows(rows: list[tuple], h_idx: int, meta: dict, path: Path) -> PriceExtraction | None:
    price_col: int | None = meta["price_col"]
    ex = PriceExtraction(
        source="open_bid_xlsx_k2" if meta["tax_col"] is not None else "open_bid_xlsx_k1",
        source_file=path.name,
        unit=meta["unit"],
        tax_basis=meta["tax_basis"],
    )
    # 表内"投标人：xxx"（与文件夹名互校素材）——全表扫，尾行不进数据循环
    for row in rows:
        for c in row:
            t = _cell_text(c)
            if t.startswith("投标人"):
                name = re.sub(r"^投标人\s*[：:]\s*", "", t).strip()
                name = re.sub(r"[（(][^（()）]*[）)]\s*$", "", name).strip()  # 去尾注（盖章）
                if name:
                    ex.stated_bidder = name
                break
    total_row: Decimal | None = None
    tax_rate: Decimal | None = None
    for row in rows[h_idx + 1:]:
        if _is_stop_row(row):
            break
        texts = [_cell_text(c) for c in row]
        joined = "｜".join(texts)
        # 单位子行（"（元/万套）"）/ 合计行 / 数据行
        vals = [_to_decimal(c) for c in row]
        price = vals[price_col] if price_col is not None and price_col < len(vals) else None
        is_total = any(k in t for t in texts for k in _TOTAL_KEYS)
        if is_total and price is not None:
            total_row = price
            continue
        if price is None:
            if "万套" in joined and ex.unit is None:
                ex.unit = "元/万套"
            continue
        if meta["tax_col"] is not None and tax_rate is None:
            tax_rate = _norm_rate(row[meta["tax_col"]]) if meta["tax_col"] < len(row) else None
        item = PriceItem(price=price, unit=ex.unit, tax_basis=ex.tax_basis)
        if meta["section_col"] is not None and meta["section_col"] < len(texts):
            item.section = texts[meta["section_col"]] or None
        if meta["product_col"] is not None and meta["product_col"] < len(texts):
            item.product = texts[meta["product_col"]] or None
        ex.items.append(item)

    if not ex.items and total_row is None:
        return None

    if total_row is not None:
        ex.bid_price = total_row
        ex.basis = "total_row"
        if ex.items:
            s = sum((i.price for i in ex.items), Decimal(0))
            ex.cross_check["sum_match"] = abs(s - total_row) <= Decimal("0.01")
            if not ex.cross_check["sum_match"]:
                ex.warnings.append(
                    f"逐行合计 {s} 与合计行 {total_row} 不一致（取合计行）"
                )
    elif len(ex.items) == 1:
        ex.bid_price = ex.items[0].price
        ex.basis = "single_row"
    else:
        ex.bid_price = sum((i.price for i in ex.items), Decimal(0))
        ex.basis = "sum_of_rows"

    if tax_rate is not None:
        ex.tax_rate = tax_rate
        if ex.tax_basis == "不含税" and ex.bid_price is not None:
            ex.cross_check["incl_vat"] = ex.bid_price * (1 + tax_rate)
    return ex


# ---------------------------------------------------------------------------
# 分项报价表汇总行兜底
# ---------------------------------------------------------------------------
_RE_ANCHOR_TAX = re.compile(r"增值税?税率|^税率$")


def _anchor_kind(text: str) -> str | None:
    t = re.sub(r"\s+", "", text)
    if "投标价" in t and "含增值税" in t:
        return "excl" if "不含增值税" in t else "incl"
    if "投标价合计" in t:
        return "excl" if "不含增值税" in t else "incl"
    return None


def parse_price_sheet_xlsx(path: Path) -> tuple[PriceExtraction | None, list[str]]:
    """分项报价表 .xlsx 汇总行兜底：锚点 `投标价（含/不含增值税,人民币元）`。

    锚点行同行取数；同行无数值时取下一行首个数值（真实表金额常折行）。
    """
    warnings: list[str] = []
    try:
        from openpyxl import load_workbook
    except ImportError:
        return None, ["openpyxl 未安装，跳过 Excel 抽价通道"]
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        return None, [f"分项报价表无法读取：{path.name}：{exc}"]

    ex = PriceExtraction(source="price_sheet_xlsx", source_file=path.name)
    found_incl: Decimal | None = None
    found_excl: Decimal | None = None
    try:
        for ws in wb.worksheets:
            rows = [[("" if c is None else str(c).strip()) for c in r] for r in ws.iter_rows(values_only=True)]
            for ri, texts in enumerate(rows):
                for i, t in enumerate(texts):
                    if not t:
                        continue
                    kind = _anchor_kind(t)
                    if kind is not None:
                        val = _next_num(texts[i + 1:])
                        if val is None and ri + 1 < len(rows):
                            val = _next_num(rows[ri + 1])
                        if val is None:
                            continue
                        if kind == "incl" and found_incl is None:
                            found_incl = val
                        elif kind == "excl" and found_excl is None:
                            found_excl = val
                        if ex.unit is None and "万套" in t:
                            ex.unit = "元/万套"
                        elif ex.unit is None and "元" in t:
                            ex.unit = "元"
                    elif _RE_ANCHOR_TAX.match(re.sub(r"\s+", "", t)) and ex.tax_rate is None:
                        ex.tax_rate = _next_rate(texts[i + 1:])
    finally:
        wb.close()

    if found_incl is None and found_excl is None:
        return None, warnings
    if found_incl is not None:
        ex.bid_price = found_incl
        ex.tax_basis = "含税"
    else:
        ex.bid_price = found_excl
        ex.tax_basis = "不含税"
        if ex.tax_rate is not None:
            ex.cross_check["incl_vat"] = found_excl * (1 + ex.tax_rate)
    ex.basis = "anchor_row"
    return ex, warnings


def _next_num(cells: list[str]) -> Decimal | None:
    for c in cells:
        v = _to_decimal(c)
        if v is not None:
            return v
    return None


def _next_rate(cells: list[str]) -> Decimal | None:
    for c in cells:
        v = _norm_rate(c)
        if v is not None:
            return v
    return None
