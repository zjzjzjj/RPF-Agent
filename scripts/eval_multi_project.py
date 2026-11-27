from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

ROOT = Path(r"E:\ZJUBJ\数据001\评分对比\_对比工作")


def canonical_tests():
    return [ROOT / f"测试{i}.json" for i in range(1, 13)]


def norm(value: str) -> str:
    return "".join(str(value or "").split()).replace("（", "(").replace("）", ")")


def item_scores(items, code):
    for item in items or []:
        if str(item.get("code")) == str(code):
            return item.get("scores") or {}
    return {}


def manual_item_scores(items, names):
    out = {}
    for item in items or []:
        if item.get("item") in names or norm(item.get("item")) in {norm(x) for x in names}:
            for bidder, score in (item.get("scores") or {}).items():
                out[bidder] = out.get(bidder, 0.0) + float(score)
    return out


def compare(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    manual = data.get("manual") or {}
    ai = data.get("ai") or {}
    rows = []
    for align in data.get("alignment") or []:
        code = align.get("ai_code")
        if not code:
            continue
        expected = manual_item_scores(manual.get("items"), align.get("manual_items") or [])
        actual = item_scores(ai.get("items"), code)
        for bidder, exp in expected.items():
            got = actual.get(bidder)
            if got is not None:
                rows.append((code, bidder, float(exp), float(got)))
    item_errors = [abs(g - e) for _, _, e, g in rows]
    # summary comparison
    manual_summary = {x["name"]: x for x in manual.get("summary") or []}
    ai_summary = {x["name"]: x for x in ai.get("summary") or []}
    total_rows = []
    for name, exp_row in manual_summary.items():
        if name not in ai_summary:
            continue
        exp = float(exp_row.get("total", 0))
        got = float(ai_summary[name].get("total", 0))
        total_rows.append((exp, got))
    total_errors = [abs(g - e) for e, g in total_rows]
    # rank agreement by total
    manual_order = [x["name"] for x in sorted(manual_summary.values(), key=lambda x: (x.get("rank") is None, x.get("rank") or 999))]
    ai_order = [x["name"] for x in sorted(ai_summary.values(), key=lambda x: (x.get("rank") is None, x.get("rank") or 999))]
    common = [n for n in manual_order if n in ai_summary]
    rank_pairs = [(manual_order.index(n), ai_order.index(n)) for n in common]
    rank_exact = [n for n in common if manual_order.index(n) == ai_order.index(n)]
    return {
        "test": data.get("test_id") or path.stem,
        "project": data.get("project_name"),
        "item_mae": round(mean(item_errors), 3) if item_errors else None,
        "item_max_abs": round(max(item_errors), 3) if item_errors else None,
        "item_count": len(item_errors),
        "total_mae": round(mean(total_errors), 3) if total_errors else None,
        "total_max_abs": round(max(total_errors), 3) if total_errors else None,
        "rank_exact_count": len(rank_exact),
        "rank_common_count": len(common),
        "rank_exact": rank_exact,
        "rows": rows,
    }


def main():
    results = [compare(path) for path in canonical_tests()]
    summary = {
        "tests": results,
        "aggregate": {
            "item_mae": round(mean(r["item_mae"] for r in results if r["item_mae"] is not None), 3),
            "total_mae": round(mean(r["total_mae"] for r in results if r["total_mae"] is not None), 3),
            "rank_exact_total": sum(r["rank_exact_count"] for r in results),
            "rank_common_total": sum(r["rank_common_count"] for r in results),
        },
    }
    out = Path(r"E:\codex work\RPF-Agent-12项目评分准确率基线.json")
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary["aggregate"], ensure_ascii=False))
    for r in results:
        print(json.dumps({k: r[k] for k in ("test", "item_mae", "item_max_abs", "total_mae", "total_max_abs", "rank_exact_count", "rank_common_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
