# -*- coding: utf-8 -*-
"""从真实招标文件 PDF 固化「评标细则」骨架测试 fixture（只读，不入库整本 PDF）。

用法（在有目标 PDF 的机器上人工跑一次）：
    python scripts/dump_scoring_fixture.py <招标文件.pdf> <fixture目录> --pages 135-137

产物：
    tables_pNN_MMM.json  find_tables 逐页逐表 rows（骨架解析器输入）
    pages_pNN_MMM.json   对应页文本（quote 回定位 / 续行合并用）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("out_dir")
    ap.add_argument("--pages", required=True, help="页码区间（1 起），如 135-137")
    args = ap.parse_args()

    lo, _, hi = args.pages.partition("-")
    p_lo, p_hi = int(lo), int(hi or lo)

    import pymupdf  # 延迟导入：fixture 再生才需要

    doc = pymupdf.open(args.pdf)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    tables: dict[str, list] = {}
    pages: dict[str, str] = {}
    for pno in range(p_lo, p_hi + 1):
        page = doc[pno - 1]
        tabs = page.find_tables()
        tables[str(pno)] = [t.extract() for t in tabs.tables]
        pages[str(pno)] = page.get_text()

    (out / f"tables_p{p_lo}_{p_hi}.json").write_text(
        json.dumps(tables, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"pages_p{p_lo}_{p_hi}.json").write_text(
        json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"dumped {len(tables)} pages -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
