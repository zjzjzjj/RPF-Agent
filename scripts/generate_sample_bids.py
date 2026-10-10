"""生成 5 家投标文件样例 PDF + expected_scores.json。

价格分真值在**生成侧独立计算**（与引擎实现分开写，构成双向核对）：
基准价 = 有效投标报价算术平均 × 0.95；价格分 = 40 × 基准价/评标价，上限 40，保留 2 位。

用法：python scripts/generate_sample_bids.py
"""
from __future__ import annotations

import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdf_common import out_dir, render_blocks_to_pdf  # noqa: E402
from sample_content import bids, tender  # noqa: E402

PRICE_MAX = Decimal("40")
K = Decimal("0.95")
UPPER = Decimal("40")


def compute_price_truth() -> dict:
    """独立计算价格分真值（不依赖 app.engine）。"""
    prices = {b.key: Decimal(b.price_yuan) for b in bids.BIDDERS}
    # 丙缺保证金 → 建议否决 → 不计入基准价（exclude_invalid=true）
    invalid = {"bing"}
    valid = [p for k, p in prices.items() if k not in invalid]
    benchmark = (sum(valid) / Decimal(len(valid))) * K
    scores: dict[str, str] = {}
    for key, price in prices.items():
        raw = PRICE_MAX * benchmark / price
        capped = min(raw, UPPER)
        scores[key] = str(capped.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {
        "benchmark_yuan": str(benchmark.quantize(Decimal("0.01"))),
        "valid_for_benchmark": [k for k in prices if k not in invalid],
        "excluded": sorted(invalid),
        "price_scores": scores,
        "formula": "价格分 = 40 ×（基准价 / 评标价），基准价 = 有效报价算术平均 × 0.95，上限 40，保留两位",
    }


def main() -> None:
    target = out_dir() / "bids"
    target.mkdir(parents=True, exist_ok=True)

    for spec in bids.BIDDERS:
        pdf_path = target / f"{spec.key}_{spec.name}_投标文件.pdf"
        render_blocks_to_pdf(
            bids.build_blocks(spec),
            str(pdf_path),
            footer=f"{spec.name} 投标文件（招标编号：{tender.PROJECT['code']}）",
        )
        print(f"已生成：{pdf_path}")

    price_truth = compute_price_truth()
    expected = {
        "project_code": tender.PROJECT["code"],
        "prices_yuan": {b.key: str(b.price_yuan) for b in bids.BIDDERS},
        "price": price_truth,
        "qualification": bids.EXPECTED_QUALIFICATION,
        "commercial": {k: {"performance": v[0], "certificates": v[1]}
                       for k, v in bids.EXPECTED_COMMERCIAL.items()},
        "tech_levels": {k: list(v) for k, v in bids.EXPECTED_TECH_LEVEL.items()},
        "anomalies": {b.key: b.anomalies for b in bids.BIDDERS if b.anomalies},
    }
    expected_path = target / "expected_scores.json"
    expected_path.write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成：{expected_path}")
    print(f"基准价 = {price_truth['benchmark_yuan']} 元；价格分 = {price_truth['price_scores']}")


if __name__ == "__main__":
    main()
