"""样例 PDF 生成公共工具：中文字体注册 + 块渲染。

字体策略（沿用 RPF 已验证方案）：优先 Windows 自带 TTF（微软雅黑），
退化到黑体，再退化到 reportlab 内置 CID 字体 STSong-Light。
所有分支都必须 registerFontFamily，否则正文里的 <b> 会静默失效。
"""
from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ACCENT = colors.HexColor("#1f4e79")

_FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),
    (r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simhei.ttf"),
    (r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\simhei.ttf"),
]


def _register_cjk_fonts() -> tuple[str, str]:
    for body_path, bold_path in _FONT_CANDIDATES:
        if Path(body_path).exists() and Path(bold_path).exists():
            pdfmetrics.registerFont(TTFont("CJK", body_path))
            pdfmetrics.registerFont(TTFont("CJK-Bold", bold_path))
            pdfmetrics.registerFontFamily("CJK", normal="CJK", bold="CJK-Bold")
            return "CJK", "CJK-Bold"
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdfmetrics.registerFontFamily(
        "STSong-Light", normal="STSong-Light", bold="STSong-Light", italic="STSong-Light"
    )
    return "STSong-Light", "STSong-Light"


BODY_FONT, BOLD_FONT = _register_cjk_fonts()


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "t", parent=base["Title"], fontName=BOLD_FONT, fontSize=20, leading=26,
            textColor=ACCENT, spaceAfter=6,
        ),
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"], fontName=BOLD_FONT, fontSize=14, leading=19,
            textColor=ACCENT, spaceBefore=12, spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontName=BOLD_FONT, fontSize=11.5, leading=16,
            spaceBefore=8, spaceAfter=4,
        ),
        "para": ParagraphStyle(
            "p", parent=base["BodyText"], fontName=BODY_FONT, fontSize=10, leading=15.5,
            firstLineIndent=0, spaceAfter=3,
        ),
        "meta": ParagraphStyle(
            "m", parent=base["Normal"], fontName=BODY_FONT, fontSize=10, leading=15,
            textColor=colors.HexColor("#444444"),
        ),
        "cell": ParagraphStyle("c", parent=base["BodyText"], fontName=BODY_FONT,
                               fontSize=8.6, leading=12),
        "cellhead": ParagraphStyle("ch", parent=base["BodyText"], fontName=BOLD_FONT,
                                   fontSize=9, leading=12, textColor=colors.white),
    }


def render_blocks_to_pdf(blocks: list[dict], out_path: str, footer: str = "") -> None:
    """把块列表渲染为 PDF。块类型：title/h1/h2/para/meta/table/pagebreak/spacer。"""
    styles = _styles()
    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=16 * mm,
        title=footer,
    )
    story = []
    for block in blocks:
        kind = block["kind"]
        if kind == "title":
            story.append(Paragraph(block["text"], styles["title"]))
        elif kind == "h1":
            story.append(Paragraph(block["text"], styles["h1"]))
        elif kind == "h2":
            story.append(Paragraph(block["text"], styles["h2"]))
        elif kind == "para":
            story.append(Paragraph(block["text"], styles["para"]))
        elif kind == "meta":
            story.append(Paragraph(block["text"], styles["meta"]))
        elif kind == "spacer":
            story.append(Spacer(1, block.get("height", 6)))
        elif kind == "pagebreak":
            story.append(PageBreak())
        elif kind == "table":
            rows = [
                [Paragraph(str(c), styles["cellhead"] if i == 0 else styles["cell"])
                 for c in row]
                for i, row in enumerate(block["rows"])
            ]
            widths = block.get("col_widths")
            if widths:
                avail = A4[0] - 38 * mm
                widths = [w / sum(widths) * avail for w in widths]
            table = Table(rows, colWidths=widths, repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9bb3c8")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                         [colors.white, colors.HexColor("#f2f6fa")]),
                    ]
                )
            )
            story.append(table)
            story.append(Spacer(1, 6))
        else:
            raise ValueError(f"未知块类型：{kind}")

    def _footer(canvas, _doc):  # noqa: ANN001
        if not footer:
            return
        canvas.saveState()
        canvas.setFont(BODY_FONT, 7.6)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(20 * mm, 9 * mm, footer)
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"第 {canvas.getPageNumber()} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)


def out_dir() -> Path:
    """样例输出目录：<repo>/data/samples。"""
    root = Path(__file__).resolve().parents[1]  # scripts/ -> repo 根
    d = root / "data" / "samples"
    d.mkdir(parents=True, exist_ok=True)
    return d


if __name__ == "__main__":
    sys.exit("这是库模块，请运行 generate_sample_tender.py / generate_sample_bids.py")
