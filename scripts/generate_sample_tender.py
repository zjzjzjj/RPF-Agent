"""生成招标文件样例 PDF + expected_rules.json（结构化真值）。

用法：python scripts/generate_sample_tender.py
输出：data/samples/tender/ZJZB-2026-0117_招标文件.pdf
      data/samples/tender/expected_rules.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdf_common import out_dir, render_blocks_to_pdf  # noqa: E402
from sample_content import tender  # noqa: E402


def main() -> None:
    target = out_dir() / "tender"
    target.mkdir(parents=True, exist_ok=True)

    pdf_path = target / f"{tender.PROJECT['code']}_招标文件.pdf"
    render_blocks_to_pdf(
        tender.build_blocks(),
        str(pdf_path),
        footer=f"{tender.PROJECT['name']}招标文件（招标编号：{tender.PROJECT['code']}）",
    )
    print(f"已生成：{pdf_path}")

    expected_path = target / "expected_rules.json"
    expected_path.write_text(
        json.dumps(tender.EXPECTED_RULES, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已生成：{expected_path}")


if __name__ == "__main__":
    main()
