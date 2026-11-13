"""开标一览表 PDF 抽价（文本通道，约 95% 真实公司的载体）。

真实形态（数据001 实证）：
- 服务类：`投标总价（含税）(元) 税率…` / `大写:壹佰柒拾玖万陆仟柒佰元整` /
  `小写：1796700  6%  高文强  10000元  无`
- 材料类：按产品分行 `利群（新版）商标条盒不含增值税投标报价（元/万套）
  大写:贰万零壹佰捌拾陆元伍角捌分 小写：20186.58`（不投的行无小写）
- 设备类：`投标总价（含增值税，人民币元） 大写:… 小写：2197477.1`

复用 engine/facts.py 既有解析件（只调用不改动）：
_price_near_total_label / _price_from_open_bid_section / _RE_PRICE_LOWER_LABEL /
_RUN_UPPER / parse_upper_amount / _money。

大小写互校：大写解析值 vs 小写数值，不等 → cross_check.upper_match=False，
**取小写**（数值原文，宁信机器数），两者都记。
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from app.engine.facts import (
    _money,
    _price_from_open_bid_section,
    _price_near_total_label,
    _RE_PRICE_LOWER_LABEL,
    _RE_TAX_RATE,
    _RE_TOTAL_LABEL,
    _RUN_UPPER,
    parse_upper_amount,
)
from app.parsing.base import DocumentRejectedError
from app.pricing.dto import PriceExtraction, PriceItem

# 行内产品名切割：`产品名 不含增值税投标报价（元/万套）` → 产品名
_RE_ROW_SPLIT = re.compile(r"(?:不含增值税|含增值税)?投标报价|报价[（(]|大写")
_RE_BIDDER_LINE = re.compile(r"投标人\s*[：:]\s*([^\s（(｜|]+)")
_PRICE_LABEL_WINDOW = 160


def _pdf_text(path: Path) -> str:
    from app.parsing.router import extract_document

    extracted = extract_document(str(path))
    return "\n".join(p.text for p in extracted.pages)


def _row_items(text: str) -> list[tuple[PriceItem, str | None, Decimal | None]]:
    """逐行小写金额 → (item, 该行大写原文, 大写解析值)。

    材料类一行一产品；服务/设备类的"小写"行无产品名（product=None）。
    大写与小写可能同行（`大写:… 小写：…`）或大写在上一行。
    """
    rows: list[tuple[PriceItem, str | None, Decimal | None]] = []
    ctx = ""
    ctx_upper: tuple[str, Decimal | None] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "小写" not in line:
            if "投标报价" in line or "报价（" in line or "报价(" in line:
                ctx = line
                ctx_upper = _first_upper(line)
            continue
        m = _RE_PRICE_LOWER_LABEL.search(line)
        if not m:
            continue
        value = _money(m.group(1))
        if value is None:
            continue
        src = line if ("投标报价" in line or "增值税" in line) else ctx
        product = None
        if src:
            head = _RE_ROW_SPLIT.split(src)[0]
            product = re.sub(r"^[\s|｜]*\d{0,3}[.、．]?\s*", "", head).strip() or None
        combined = line + " " + ctx
        unit = "元/万套" if "万套" in combined else "元"
        tax_basis = (
            "不含税"
            if ("不含增值税" in combined or "不含税" in combined)
            else ("含税" if ("含税" in combined or "含增值税" in combined) else None)
        )
        upper_text, upper_val = _first_upper(line) or (None, None)
        if upper_text is None and product is not None:
            upper_text, upper_val = ctx_upper or (None, None)
        rows.append(
            (
                PriceItem(product=product, price=value, unit=unit, tax_basis=tax_basis),
                upper_text,
                upper_val,
            )
        )
    return rows


def _first_upper(line: str) -> tuple[str, Decimal | None] | None:
    m = _RUN_UPPER.search(line)
    if not m:
        return None
    return m.group(0), parse_upper_amount(m.group(0))


# 大写金额被 PDF 拆成不相邻的两处（真实样本，浩鲸经 extract_document 后的行序：
#   "大写:壹佰捌拾伍万柒仟玖佰玖 6 王伟 10000 无" ⏎ "拾玖元捌角" ⏎ "小写：1857999.8"
# 大写前半截后面紧跟着税率/负责人/保证金列）。只在"大写"标签之后、"小写"标签之前的
# 区间内，**只抽大写金额字符**依次拼接（数字/姓名/空白一律丢），再按完整串解析。
_RE_UPPER_SPAN = re.compile(r"大写\s*[：:]\s*([^小]{0,120})")
_RE_UPPER_CHARS = re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元整角分]+")


def _joined_upper(tail: str) -> tuple[str, Decimal | None] | None:
    m = _RE_UPPER_SPAN.search(tail)
    if not m:
        return None
    joined = "".join(_RE_UPPER_CHARS.findall(m.group(1)))
    return _first_upper(joined)


def _upper_near_total(text: str) -> tuple[str | None, Decimal | None]:
    """投标总价标签后窗口内的大写金额（服务/设备类的互校对象）。

    先按"大写：…"区间跨行拼接取（处理硬换行拆分），拼不出再退回逐行取首个。
    """
    for m in _RE_TOTAL_LABEL.finditer(text):
        tail = text[m.end(): m.end() + _PRICE_LABEL_WINDOW]
        if got := _joined_upper(tail):
            return got
        for line in tail.split("\n")[:3]:
            if got := _first_upper(line):
                return got
    return None, None


def parse_open_bid_pdf(path: Path) -> tuple[PriceExtraction | None, list[str]]:
    """开标一览表 .pdf → PriceExtraction；抽不出返回 (None, 警告)。"""
    try:
        text = _pdf_text(path)
    except DocumentRejectedError as exc:
        return None, [f"开标一览表 PDF 无法提取文本：{path.name}：{exc.args[0] if exc.args else exc}"]
    except Exception as exc:  # noqa: BLE001  解析失败不阻断导入
        return None, [f"开标一览表 PDF 读取失败：{path.name}：{exc}"]
    if not text.strip():
        return None, [f"开标一览表 PDF 无文本层：{path.name}"]

    ex = PriceExtraction(source="open_bid_pdf", source_file=path.name)
    rows = _row_items(text)
    total = _price_near_total_label(text)

    if total is not None:
        ex.bid_price = total
        ex.basis = "total_label"
        ex.items = [it for it, _, _ in rows if it.product]
        # 含税口径从"投标总价（含税）/（含增值税…）"标签窗口判（不含税先判，含子串）
        if m := _RE_TOTAL_LABEL.search(text):
            tail = text[m.end(): m.end() + _PRICE_LABEL_WINDOW]
            if "不含增值税" in tail or "不含税" in tail:
                ex.tax_basis = "不含税"
            elif "含增值税" in tail or "含税" in tail:
                ex.tax_basis = "含税"
    elif rows:
        priced = [it for it, _, _ in rows]
        ex.items = priced
        ex.unit = priced[0].unit
        ex.tax_basis = priced[0].tax_basis
        if len(priced) == 1:
            ex.bid_price = priced[0].price
            ex.basis = "single_row"
        else:
            ex.bid_price = sum((i.price for i in priced), Decimal(0))
            ex.basis = "sum_of_rows"
    elif (ob := _price_from_open_bid_section(text)) is not None:
        ex.bid_price = ob
        ex.basis = "window_scan"
    else:
        return None, [f"开标一览表 PDF 未抽出报价：{path.name}"]

    # ---- 大小写互校 ----
    upper_text, upper_val = _upper_near_total(text)
    if upper_text is None and rows:
        # 材料类：逐行大写与本行小写互校
        pairs = [(it.price, uv) for it, _, uv in rows if uv is not None]
        if pairs:
            ex.cross_check["upper_match"] = all(
                abs(p - u) <= Decimal("0.01") for p, u in pairs
            )
            ex.upper = next(ut for _, ut, _ in rows if ut)
            ex.upper_value = pairs[0][1]
    if upper_text is not None:
        ex.upper = upper_text
        ex.upper_value = upper_val
        if upper_val is not None and ex.bid_price is not None:
            ex.cross_check["upper_match"] = abs(upper_val - ex.bid_price) <= Decimal("0.01")
    if ex.cross_check.get("upper_match") is False:
        ex.warnings.append("大小写金额不一致（取小写数值）")

    # ---- 税率（投标总价窗口内首个 %） ----
    for m in _RE_TOTAL_LABEL.finditer(text):
        tail = text[m.end(): m.end() + _PRICE_LABEL_WINDOW]
        rm = _RE_TAX_RATE.search(tail)
        if rm and Decimal(rm.group(1)) < 50:
            ex.tax_rate = Decimal(rm.group(1)) / 100
            break

    # ---- 表内"投标人："与文件夹名互校的素材 ----
    if bm := _RE_BIDDER_LINE.search(text):
        ex.stated_bidder = bm.group(1)

    return ex, ex.warnings
