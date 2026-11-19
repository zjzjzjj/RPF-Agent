"""旧版 .xls → 伪页文本（每工作表一页），供 DocumentPage 入库。

与 xlsx_text 同契约：投标包里的供应商信息收集表等旧版 Excel 也要进文本库。
xlrd 2.x 只读 .xls（BIFF），xlsx 走 openpyxl，二者互不覆盖。
不参与条款树编号扫描（page_approximate=True）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.parsing.base import DocumentRejectedError, ExtractedDocument, ExtractedPage

log = logging.getLogger("bid_eval.parsing.xls")

# 伪页尺寸：A4 points（与 xlsx_text 保持一致）
_PAGE_W, _PAGE_H = 595.0, 842.0


def _fmt_cell(value, cell_type: int) -> str:  # noqa: ANN001
    """单元格值 → 文本；xlrd 的整数年份/金额会读成 float（1.0），这里归一。"""
    if value is None or value == "":
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def extract_xls(path: str, on_progress=None) -> ExtractedDocument:  # noqa: ANN001
    try:
        import xlrd
    except ImportError as exc:  # 依赖缺失：明确拒绝而非崩溃
        raise DocumentRejectedError("NOT_SUPPORTED", "xlrd 未安装，无法解析旧版 .xls") from exc

    p = Path(path)
    try:
        book = xlrd.open_workbook(p, on_demand=True)
    except Exception as exc:  # 加密/损坏/非 BIFF 容器
        raise DocumentRejectedError(
            "EXTRACTION_FAILED", f"Excel 无法读取（可能加密或损坏）：{exc}"
        ) from exc

    pages: list[ExtractedPage] = []
    warnings: list[str] = []
    try:
        for i in range(book.nsheets):
            sh = book.sheet_by_index(i)
            if on_progress is not None:
                on_progress(i + 1, book.nsheets)
            lines: list[str] = [f"[工作表：{sh.name}]"]
            try:
                for r in range(sh.nrows):
                    cells = [
                        _fmt_cell(sh.cell_value(r, c), sh.cell_type(r, c))
                        for c in range(sh.ncols)
                    ]
                    lines.append(" | ".join(cells).rstrip(" |"))
            except Exception as exc:  # 单表读取失败不拖垮全簿
                warnings.append(f"工作表「{sh.name}」读取失败：{exc}")
                continue
            pages.append(
                ExtractedPage(
                    page_no=i + 1,
                    text="\n".join(lines),
                    width=_PAGE_W,
                    height=_PAGE_H,
                    has_text_layer=True,
                    page_approximate=True,
                    source="text",
                )
            )
    finally:
        book.release_resources()

    if not pages:
        raise DocumentRejectedError("EMPTY_FILE", "Excel 内没有任何可读工作表")
    return ExtractedDocument(pages=pages, warnings=warnings, engine="xlrd")
