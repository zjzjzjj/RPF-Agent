"""只读诊断：对真实投标 PDF 跑业绩案例证据抽取，打印定位/案例行/材料矩阵/OCR 目标。

不写任何数据、不调 LLM。用于在接入评分前验证确定性抽取是否正确。

用法（在 backend 目录下）：
    python scripts/diagnose_case_evidence.py                # 全部投标文档
    python scripts/diagnose_case_evidence.py --project PRJ-xxx
    python scripts/diagnose_case_evidence.py --document-id 2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # Windows GBK 控制台打不出 ✓/✗/△
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.db.models import Bidder, Document, DocumentPage  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.evidence.case_evidence import (  # noqa: E402
    PageRec,
    classify_materials,
    link_case_blocks,
    locate_section,
    material_ocr_targets,
    parse_case_table,
    summarize_status,
)


def image_ratios(pdf_path: str) -> dict[int, float]:
    """每页图片覆盖占比（图片块面积 / 页面积）。"""
    import pymupdf as fitz

    out: dict[int, float] = {}
    with fitz.open(pdf_path) as doc:
        for i in range(doc.page_count):
            page = doc[i]
            area = max(1.0, float(page.rect.width * page.rect.height))
            img_area = 0.0
            try:
                blocks = page.get_text("dict").get("blocks", [])
            except Exception:
                blocks = []
            for b in blocks:
                if b.get("type") == 1:
                    x0, y0, x1, y1 = b["bbox"]
                    img_area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
            out[i + 1] = min(1.0, img_area / area)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project")
    ap.add_argument("--document-id", type=int)
    args = ap.parse_args()

    with session_scope() as db:
        q = db.query(Document).filter(Document.kind == "bid")
        if args.project:
            q = q.filter(Document.project_id == args.project)
        if args.document_id:
            q = q.filter(Document.id == args.document_id)
        docs = q.order_by(Document.id).all()
        for d in docs:
            b = db.get(Bidder, d.bidder_id) if d.bidder_id else None
            print("=" * 78)
            print(
                f"doc {d.id} bidder {d.bidder_id} {(b.name if b else '')[:22]} "
                f"pages={d.page_count} file={d.original_filename[:40]}"
            )
            recs = [
                PageRec(int(p.page_no), p.text or "", p.source or "text")
                for p in db.query(DocumentPage)
                .filter(DocumentPage.document_id == d.id)
                .order_by(DocumentPage.page_no)
            ]
            sec = locate_section(recs)
            print(
                f"section: {sec.start}-{sec.end} method={sec.method} "
                f"conf={sec.confidence} offset={sec.offset}"
            )
            if not sec.found:
                print("  (未定位到证据段)")
                continue
            pdf = d.stored_path
            if not pdf or not Path(pdf).exists():
                print(f"  (PDF 不存在：{pdf})")
                continue
            rows = parse_case_table(pdf, sec.start, sec.end)
            print(f"rows({len(rows)}):")
            for r in rows:
                print(
                    f"  #{r.index} {r.name[:34]:<34} | 金额={r.amount_yuan} | "
                    f"签订={r.sign_date} | p{r.page}"
                )
            blocks = link_case_blocks(rows, recs, sec)
            ratios = image_ratios(pdf)
            classify_materials(blocks, recs, ratios)
            print("materials:", summarize_status(blocks))
            for b in blocks:
                rng = f"{b.pages[0]}-{b.pages[-1]}" if b.pages else "未绑定"
                print(
                    f"  #{b.row.index} pages={rng} ambiguous={b.ambiguous} "
                    f"status={b.status} reasons={b.reasons}"
                )
            targets = material_ocr_targets(recs, ratios, sec)
            print(f"ocr_targets({len(targets)}):", targets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
