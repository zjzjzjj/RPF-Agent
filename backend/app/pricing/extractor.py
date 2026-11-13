"""公司文件夹 → 投标价格：四通道按优先级尝试，交叉校验后给出终值。

通道优先级（用户决策）：
1. open_bid_xlsx   开标一览表 Excel（结构化数值，最可靠）
2. open_bid_pdf    开标一览表 PDF（约 95% 真实公司；大小写互校）
3. price_sheet_xlsx 分项报价表 Excel 汇总行
4. main_pdf_text   主投标文件 PDF 文本五级兜底（engine/facts 既有链路）

高优先级出价即采用；低优先级不一致 → 仍取高优先级，cross_check 记
multi_channel_match=False（信息性标记，不卡评分）。
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from app.pricing.dto import PriceExtraction, cross_check_json, derive_status
from app.pricing.open_bid_pdf import parse_open_bid_pdf
from app.pricing.open_bid_xlsx import parse_open_bid_xlsx, parse_price_sheet_xlsx

log = logging.getLogger("bid_eval.pricing")

_OPEN_BID_XLSX_KEYS = ("开标一览表", "报价一览表", "开标一览")
_PRICE_SHEET_KEYS = ("分项报价表", "投标报价表", "报价表", "分项报价")
_OPEN_BID_PDF_KEYS = ("开标一览表", "开标一览", "报价一览表")
_XLS_SUFFIX = ".xls"


def _is_open_bid_xlsx(name: str) -> bool:
    return name.lower().endswith(".xlsx") and any(k in name for k in _OPEN_BID_XLSX_KEYS)


def _is_price_sheet(name: str) -> bool:
    return (
        name.lower().endswith(".xlsx")
        and not _is_open_bid_xlsx(name)
        and any(k in name for k in _PRICE_SHEET_KEYS)
    )


def _is_open_bid_pdf(name: str) -> bool:
    return name.lower().endswith(".pdf") and any(k in name for k in _OPEN_BID_PDF_KEYS)


def extract_company_price(
    files: Sequence[Path], company_name: str | None = None
) -> PriceExtraction:
    """公司文件夹内全部文件 → 抽价结果（永不抛异常；失败返回 missing）。"""
    files = [f for f in files if f.is_file()]
    names = [f.name for f in files]
    warnings: list[str] = []
    attempts: list[dict] = []

    def _attempt(label: str, fn, path: Path) -> PriceExtraction | None:  # noqa: ANN001
        ex, warns = fn(path)
        warnings.extend(warns)
        attempts.append(
            {
                "channel": label,
                "file": path.name,
                "ok": ex is not None and ex.bid_price is not None,
            }
        )
        return ex

    # ---- .xls 老格式：跳过+告警（不引 xlrd） ----
    for f in files:
        if f.name.lower().endswith(_XLS_SUFFIX) and any(
            k in f.name for k in _OPEN_BID_XLSX_KEYS + _PRICE_SHEET_KEYS + ("开标", "报价")
        ):
            warnings.append(f"老格式 .xls 已跳过（请另存为 .xlsx）：{f.name}")

    results: list[PriceExtraction] = []

    # ---- 通道 1：开标一览表 xlsx ----
    for f in files:
        if _is_open_bid_xlsx(f.name):
            if ex := _attempt("open_bid_xlsx", parse_open_bid_xlsx, f):
                if ex.bid_price is not None:
                    results.append(ex)
                break  # 同通道只取首个可读文件

    # ---- 通道 2：开标一览表 pdf ----
    for f in files:
        if _is_open_bid_pdf(f.name):
            if ex := _attempt("open_bid_pdf", parse_open_bid_pdf, f):
                if ex.bid_price is not None:
                    results.append(ex)
                break

    # ---- 通道 3：分项报价表 xlsx 汇总行（兼作跨通道互校） ----
    for f in files:
        if _is_price_sheet(f.name):
            if ex := _attempt("price_sheet_xlsx", parse_price_sheet_xlsx, f):
                if ex.bid_price is not None:
                    results.append(ex)
                break

    # ---- 通道 4：主投标文件 PDF 文本兜底 ----
    if not results:
        main = _pick_main_pdf(files)
        if main is not None:
            ex = _attempt("main_pdf_text", _parse_main_pdf, main)
            if ex is not None and ex.bid_price is not None:
                results.append(ex)

    if not results:
        out = PriceExtraction(source="", warnings=warnings)
        out.cross_check["attempts"] = attempts
        return out

    best = results[0]
    # ---- 跨通道互校（同口径才比） ----
    for other in results[1:]:
        if _comparable(best, other):
            match = abs(best.bid_price - other.bid_price) <= Decimal("0.01")
            key = f"multi_channel_match:{other.source}"
            best.cross_check[key] = match
            if not match:
                best.warnings.append(
                    f"{other.source} 报价 {other.bid_price} 与 {best.source} 的 "
                    f"{best.bid_price} 不一致（取 {best.source}）"
                )
        else:
            best.cross_check[f"multi_channel_match:{other.source}"] = None
    if company_name and best.stated_bidder and company_name.strip() not in best.stated_bidder:
        if best.stated_bidder.strip() not in (company_name or ""):
            best.warnings.append(
                f"表内投标人「{best.stated_bidder}」与文件夹名「{company_name}」不一致"
            )
    best.warnings = warnings + best.warnings
    best.cross_check["attempts"] = attempts
    return best


def _comparable(a: PriceExtraction, b: PriceExtraction) -> bool:
    """同单位同含税口径才比较（元/万套 与 元 不可比）。"""
    if a.unit and b.unit and a.unit != b.unit:
        return False
    if a.tax_basis and b.tax_basis and a.tax_basis != b.tax_basis:
        return False
    return True


def _pick_main_pdf(files: Sequence[Path]) -> Path | None:
    pdfs = [f for f in files if f.name.lower().endswith(".pdf") and not _is_open_bid_pdf(f.name)]
    if not pdfs:
        pdfs = [f for f in files if f.name.lower().endswith(".pdf")]
    if not pdfs:
        return None
    named = [f for f in pdfs if "投标文件" in f.name]
    return max(named or pdfs, key=lambda f: f.stat().st_size)


def _parse_main_pdf(path: Path) -> tuple[PriceExtraction | None, list[str]]:
    """主投标文件文本 → engine/facts 五级兜底（第 4 通道）。"""
    try:
        from app.engine.facts import extract_bid_facts
        from app.parsing.router import extract_document

        extracted = extract_document(str(path))
        text = "\n".join(p.text for p in extracted.pages)
        facts = extract_bid_facts(text)
    except Exception as exc:  # noqa: BLE001  兜底通道失败不阻断
        return None, [f"主投标文件文本抽价失败：{path.name}：{exc}"]
    if facts.bid_price is None:
        return None, [f"主投标文件文本未抽出报价：{path.name}"]
    return (
        PriceExtraction(
            bid_price=facts.bid_price,
            source="main_pdf_text",
            source_file=path.name,
            basis="text",
            upper=facts.bid_price_upper,
            upper_value=facts.bid_price_upper_value,
            cross_check={
                "upper_match": (
                    None
                    if facts.bid_price_upper_value is None
                    else abs(facts.bid_price_upper_value - facts.bid_price) <= Decimal("0.01")
                )
            },
        ),
        [],
    )
