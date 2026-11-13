"""xlsx → 伪页文本（每工作表一页），供 DocumentPage 入库。

用途：投标包里的报价 Excel 也要进文本库——bid_text 拼接、证据引文、
全文检索都吃 document_pages；没有文本层，Excel 就成了"存在但看不见"的文件。
不参与条款树编号扫描（page_approximate=True）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.parsing.base import DocumentRejectedError, ExtractedDocument, ExtractedPage

log = logging.getLogger("bid_eval.parsing.xlsx")

# 伪页尺寸：A4 points（证据 bbox 归一化分母，xlsx 无真实版面）
_PAGE_W, _PAGE_H = 595.0, 842.0


def extract_xlsx(path: str) -> ExtractedDocument:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # 依赖缺失：明确拒绝而非崩溃
        raise DocumentRejectedError("NOT_SUPPORTED", "openpyxl 未安装，无法解析 .xlsx") from exc

    p = Path(path)
    try:
        wb = load_workbook(p, read_only=True, data_only=True)
    except Exception as exc:  # 加密/损坏/非 xlsx 容器
        raise DocumentRejectedError(
            "EXTRACTION_FAILED", f"Excel 无法读取（可能加密或损坏）：{exc}"
        ) from exc

    pages: list[ExtractedPage] = []
    warnings: list[str] = []
    try:
        for i, ws in enumerate(wb.worksheets, start=1):
            lines: list[str] = [f"[工作表：{ws.title}]"]
            try:
                for row in ws.iter_rows(values_only=True):
                    cells = ["" if c is None else str(c) for c in row]
                    lines.append(" | ".join(cells).rstrip(" |"))
            except Exception as exc:  # 单表读取失败不拖垮全簿
                warnings.append(f"工作表「{ws.title}」读取失败：{exc}")
                continue
            pages.append(
                ExtractedPage(
                    page_no=i,
                    text="\n".join(lines),
                    width=_PAGE_W,
                    height=_PAGE_H,
                    has_text_layer=True,
                    page_approximate=True,
                    source="text",
                )
            )
    finally:
        wb.close()

    if not pages:
        raise DocumentRejectedError("EMPTY_FILE", "Excel 内没有任何可读工作表")
    return ExtractedDocument(pages=pages, warnings=warnings, engine="openpyxl")
