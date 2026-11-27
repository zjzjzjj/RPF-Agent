from __future__ import annotations
import argparse, json, statistics, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(r"E:\ZJUBJ\数据001\评分对比\_对比工作")
OUT = Path(r"E:\codex work\RPF-Agent-12项目实时回归结果.json")
BASE = "http://100.100.30.150:18200/api"
DEFAULT_RUNS = {
    1: ("PRJ-20261104-1X7E", 191),
    2: ("PRJ-20261106-KDK2", 190),
    3: ("PRJ-20261029-J0AH", 192),
    4: ("PRJ-20261030-1KJP", 193),
    5: ("PRJ-20261103-N7UE", 194),
    6: ("PRJ-20261104-B3KP", 195),
    7: ("PRJ-20261118-RGX5", 196),
    8: ("PRJ-20261111-YIE5", 197),
    9: ("PRJ-20261112-9QCT", 198),
    10: ("PRJ-20261116-Y3DS", 199),
    11: ("PRJ-20261117-10G5", 200),
    12: ("PRJ-20261118-42FO", 203),
}


def norm(value: str) -> str:
    return "".join(str(value or "").split()).replace("（", "(").replace("）", ")")


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def manual_item_scores(items, names):
    wanted = {norm(x) for x in names}
    out = {}
    for item in items or []:
        if norm(item.get("item")) in wanted:
            for bidder, score in (item.get("scores") or {}).items():
                out[bidder] = out.get(bidder, 0.0) + float(score)
    return out


def compare(test_no: int, project_id: str, run_id: int) -> dict:
    data = json.loads((ROOT / f"测试{test_no}.json").read_text(encoding="utf-8"))
    summary = get_json(f"{BASE}/runs/{run_id}/score-summary?{urllib.parse.urlencode({'project_id': project_id, 'max_reason_chars': 200})}")
    ai_items = {}
    ai_rows = {row["name"]: row for row in summary.get("bidders") or []}
    for row in summary.get("bidders") or []:
        for bucket in row.get("buckets") or []:
            for item in bucket.get("items") or []:
                if item.get("final") is not None:
                    ai_items[(str(item.get("code")), row["name"])] = float(item["final"])
    item_errors = []
    item_rows = []
    for align in data.get("alignment") or []:
        code = align.get("ai_code")
        if not code:
            continue
        expected = manual_item_scores((data.get("manual") or {}).get("items"), align.get("manual_items") or [])
        for bidder, exp in expected.items():
            got = ai_items.get((str(code), bidder))
            if got is None:
                continue
            err = abs(got - exp)
            item_errors.append(err)
            item_rows.append({"code": code, "bidder": bidder, "manual": exp, "ai": got, "abs_error": err})
    manual_summary = {x["name"]: x for x in (data.get("manual") or {}).get("summary") or []}
    total_errors = []
    total_rows = []
    for name, exp in manual_summary.items():
        row = ai_rows.get(name)
        if not row:
            continue
        got = float((row.get("summary") or {}).get("total") or 0)
        exp_total = float(exp.get("total") or 0)
        total_errors.append(abs(got - exp_total))
        total_rows.append({"bidder": name, "manual": exp_total, "ai": got, "abs_error": abs(got-exp_total)})
    common = [name for name in manual_summary if name in ai_rows]
    manual_order = [n for n, _ in sorted(manual_summary.items(), key=lambda kv: (kv[1].get("rank") is None, kv[1].get("rank") or 999))]
    ai_order = [n for n, _ in sorted(((n, ai_rows[n]) for n in common), key=lambda kv: float((kv[1].get("summary") or {}).get("total") or 0), reverse=True)]
    rank_exact = sum(manual_order.index(n) == ai_order.index(n) for n in common)
    price_rows = []
    for name in common:
        exp = next((x for x in (data.get("manual") or {}).get("summary") or [] if x.get("name") == name), {})
        price_rows.append({
            "bidder": name,
            "manual_price": exp.get("price"),
            "ai_price": (ai_rows[name].get("summary") or {}).get("price_score"),
        })
    return {
        "test": f"测试{test_no}",
        "project_id": project_id,
        "run_id": run_id,
        "item_mae": round(statistics.mean(item_errors), 3) if item_errors else None,
        "item_max_abs": round(max(item_errors), 3) if item_errors else None,
        "item_count": len(item_errors),
        "total_mae": round(statistics.mean(total_errors), 3) if total_errors else None,
        "total_max_abs": round(max(total_errors), 3) if total_errors else None,
        "rank_exact_count": rank_exact,
        "rank_common_count": len(common),
        "price_rows": price_rows,
        "totals": total_rows,
        "largest_item_errors": sorted(item_rows, key=lambda x: x["abs_error"], reverse=True)[:20],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    results = [compare(n, pid, run_id) for n, (pid, run_id) in sorted(DEFAULT_RUNS.items())]
    aggregate = {
        "item_mae": round(statistics.mean(x["item_mae"] for x in results if x["item_mae"] is not None), 3),
        "total_mae": round(statistics.mean(x["total_mae"] for x in results if x["total_mae"] is not None), 3),
        "rank_exact_total": sum(x["rank_exact_count"] for x in results),
        "rank_common_total": sum(x["rank_common_count"] for x in results),
    }
    payload = {"aggregate": aggregate, "tests": results}
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(aggregate, ensure_ascii=False))
    for row in results:
        print(json.dumps({k: row[k] for k in ("test", "run_id", "item_mae", "total_mae", "rank_exact_count", "rank_common_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
