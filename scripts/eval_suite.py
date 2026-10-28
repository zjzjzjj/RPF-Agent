# -*- coding: utf-8 -*-
"""评分结果 vs 人工评审报告 多项目评测套件（2026-10-27 训练集校准）。

金标准：scripts/ground_truth/<name>.json（从训练集人工 PDF 提取的逐项分）。
用法：
    python scripts/eval_suite.py --ground-truth test1 --run 58 --project PRJ-20261021-O740 [--db <path>]
    python scripts/eval_suite.py --ground-truth test2 --run 56 --project PRJ-20261026-ABJX --validate

--validate 门槛（非 0 退出 = 回归）：
    已映射逐项 |Δ|≤0.5（价格逐分）、总分 MAE≤1.5、Kendall τ(分数)≥0.9、可复算校验通过。
校准流程：测试1 调参（看 Δ 表）→ 测试2 --validate 过线才算完成（防过拟合）。
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

GT_DIR = Path(__file__).resolve().parent / "ground_truth"


def kendall_tau(a: list, b: list) -> float:
    n = len(a)
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            sign_a = (a[i] > a[j]) - (a[i] < a[j])
            sign_b = (b[i] > b[j]) - (b[i] < b[j])
            if sign_a == sign_b:
                concordant += 1
            elif sign_a and sign_b:
                discordant += 1
    denom = n * (n - 1) / 2
    return (concordant - discordant) / denom if denom else float("nan")


def _norm(s: str) -> str:
    import re
    return re.sub(r"[\s—\-－:：()（）]+", "", s or "")


def _match_item(gt_item: dict, crit_name: str) -> bool:
    n = _norm(crit_name)
    # match = 任一关键词命中（all-of 会让「报价部分—投标报价得分」匹配不上）
    if not any(_norm(k) in n for k in gt_item.get("match", [])):
        return False
    for ex in gt_item.get("exclude_match", []):
        if _norm(ex) in n:
            return False
    return True


def _recomputability_check(cur, run_id: int) -> tuple[int, int, list[str]]:
    """I1 校验：final_score 可由 ai_raw_json.aggregation_v2 重算。"""
    ok = bad = 0
    errs: list[str] = []
    rows = cur.execute(
        """SELECT s.id, s.bidder_id, s.criterion_id, s.final_score, s.ai_raw_json
           FROM score_records s WHERE s.run_id=? AND s.ai_raw_json IS NOT NULL""",
        (run_id,),
    ).fetchall()
    for sid, bid, crit, final, raw in rows:
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:  # noqa: BLE001
            continue
        agg = data.get("aggregation_v2")
        if not agg or final is None:
            continue
        inputs = [Decimal(str(i["score"])) for i in agg.get("inputs", []) if i.get("score") is not None]
        if not inputs:
            continue
        rule = agg.get("rule")
        try:
            if rule == "cluster_equal_mean":
                recomputed = (sum(inputs) / Decimal(len(inputs))).quantize(Decimal("0.01"))
            elif rule == "trimmed_mean_drop_high_low":
                ordered = sorted(inputs)
                middle = ordered[1:-1]
                recomputed = (sum(middle) / Decimal(len(middle))).quantize(Decimal("0.01")) if middle else ordered[0]
            elif rule == "subitem_cluster_mean":
                recomputed = sum(inputs, Decimal(0))
            else:
                continue
            recomputed = min(recomputed, Decimal(str(final)) if False else recomputed)
            if abs(recomputed - Decimal(str(final))) <= Decimal("0.01"):
                ok += 1
            else:
                bad += 1
                errs.append(f"score#{sid} final={final} 重算={recomputed} rule={rule}")
        except Exception as exc:  # noqa: BLE001
            bad += 1
            errs.append(f"score#{sid} 重算异常: {exc}")
    return ok, bad, errs


def _stage_timing(cur, run_id: int) -> list[tuple[str, str, str]]:
    """阶段耗时线索：score_records.created_at 按 score_type 分段的首末时刻。"""
    rows = cur.execute(
        """SELECT c.score_type, MIN(s.created_at), MAX(s.created_at), COUNT(*)
           FROM score_records s JOIN criteria c ON c.id=s.criterion_id
           WHERE s.run_id=? GROUP BY c.score_type""",
        (run_id,),
    ).fetchall()
    return [(st, str(a), str(b)) for st, a, b, _n in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ground-truth", required=True, help="ground_truth 目录下的名字（test1/test2）")
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--db", default="")
    ap.add_argument("--validate", action="store_true", help="按门槛断言（非 0 退出）")
    args = ap.parse_args()

    gt = json.loads((GT_DIR / f"{args.ground_truth}.json").read_text(encoding="utf-8"))
    import sqlite3

    db = Path(args.db) if args.db else Path(__file__).resolve().parents[1] / "data" / "bid_eval.db"
    # app 服务层（compute_ranking）读 config.db_path()——必须在 import 前指向同一副本
    import os
    os.environ["BID_EVAL_DB_PATH"] = db.as_posix()
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    cur = con.cursor()

    from app.services.ranking_service import compute_ranking

    ranking = compute_ranking(args.project, args.run)
    ai: dict[str, dict] = {}
    for row in ranking.get("rows", []):
        name = row["bidder_name"]
        key = next((k for k in gt["bidders"] if k in name), None)
        if key:
            ai[key] = {
                "total": float(row["total"]) if row["total"] is not None else 0.0,
                "rank": row["rank"],
                "bidder_id": row["bidder_id"],
            }

    print(f"=== {gt['name']}（{gt['project']}）run {args.run} vs 人工评审报告 ===")
    print(f"{'<公司>':<10} {'真实总分':>8} {'AI总分':>8} {'Δ':>7} {'真实名次':>8} {'AI名次':>6}")
    tau_r, tau_a, tau_rs, tau_as = [], [], [], []
    total_abs = 0.0
    for key in gt["bidders"]:
        real = gt["totals"][key]
        got = ai.get(key)
        if not got:
            print(f"{key:<10}  —— AI 结果缺失 ——")
            continue
        d = got["total"] - real
        total_abs += abs(d)
        print(f"{key:<10} {real:>8.2f} {got['total']:>8.2f} {d:>+7.2f} "
              f"{gt['ranks'][key]:>8} {got['rank']:>6}")
        tau_r.append(gt["ranks"][key])
        tau_a.append(got["rank"])
        tau_rs.append(real)
        tau_as.append(got["total"])
    n = max(1, len(ai))
    total_mae = total_abs / n
    tau_score = kendall_tau(tau_rs, tau_as)
    print(f"\n总分 MAE: {total_mae:.2f}  Kendall τ(分数): {tau_score:+.2f}")

    # 分桶
    print(f"\n{'公司':<10} {'桶':<6} {'真实':>7} {'AI':>7} {'Δ':>7}")
    bucket_abs = {"biz": 0.0, "tech": 0.0, "price": 0.0}
    bucket_names = {"biz": "商务", "tech": "技术", "price": "价格"}
    for key in gt["bidders"]:
        got = ai.get(key)
        if not got:
            continue
        for bkey, bname in bucket_names.items():
            row = cur.execute(
                """SELECT SUM(s.final_score) FROM score_records s
                   JOIN criteria c ON c.id=s.criterion_id
                   WHERE s.run_id=? AND s.bidder_id=? AND c.category=?
                     AND c.score_type NOT IN ('veto','match')""",
                (args.run, got["bidder_id"], {"biz": "commercial", "tech": "technical", "price": "price"}[bkey]),
            ).fetchone()
            ai_val = float(row[0] or 0)
            real_val = gt["buckets"][bkey][key]
            bucket_abs[bkey] += abs(ai_val - real_val)
            print(f"{key:<10} {bname:<6} {real_val:>7.2f} {ai_val:>7.2f} {ai_val - real_val:>+7.2f}")
    bucket_mae = max(bucket_abs[b] / n for b in bucket_abs)

    # 逐项
    print(f"\n=== 逐项对比（Δ=AI−真实，|Δ|≤0.5 为过线）===")
    print(f"{'评分项':<14} {'公司':<10} {'真实':>6} {'AI':>6} {'Δ':>7}  过线")
    item_abs = 0.0
    item_n = 0
    item_fail = 0
    unmatched: list[str] = []
    for it in gt["items"]:
        matched_crits = []
        for key in gt["bidders"]:
            got = ai.get(key)
            if not got:
                continue
            rows = cur.execute(
                """SELECT c.code, c.name, s.final_score FROM score_records s
                   JOIN criteria c ON c.id=s.criterion_id
                   WHERE s.run_id=? AND s.bidder_id=? AND c.score_type NOT IN ('veto','match')""",
                (args.run, got["bidder_id"]),
            ).fetchall()
            hits = [(code, name, fin) for code, name, fin in rows if _match_item(it, name)]
            if not hits:
                continue
            ai_val = sum(float(h[2] or 0) for h in hits)
            matched_crits.append(hits[0][0])
            real_val = it["scores"].get(key, 0.0)
            d = ai_val - real_val
            ok = abs(d) <= 0.5
            item_abs += abs(d)
            item_n += 1
            if not ok:
                item_fail += 1
            print(f"{it['key']:<14} {key:<10} {real_val:>6.2f} {ai_val:>6.2f} {d:>+7.2f}  {'✓' if ok else '✗'}")
        if not matched_crits:
            unmatched.append(it["key"])
    if unmatched:
        print(f"\n[!] 未能映射到 AI 评分项的金标准项: {'、'.join(unmatched)}")
    item_mae = item_abs / max(1, item_n)

    # 可复算 + 阶段耗时
    ok, bad, errs = _recomputability_check(cur, args.run)
    print(f"\n可复算校验（I1）: {ok} 通过 / {bad} 不通过")
    for e in errs[:5]:
        print("  ", e)
    print("\n阶段落库时段（created_at 首/末）:")
    for st, a, b in _stage_timing(cur, args.run):
        print(f"  {st:<10} {a} → {b}")

    con.close()

    if args.validate:
        failures = []
        if item_fail:
            failures.append(f"{item_fail} 个逐项 |Δ|>0.5")
        if total_mae > 1.5:
            failures.append(f"总分 MAE {total_mae:.2f}>1.5")
        if bucket_mae > 1.5:
            failures.append(f"分桶 MAE {bucket_mae:.2f}>1.5")
        if tau_score < 0.9:
            failures.append(f"τ {tau_score:.2f}<0.9")
        if bad:
            failures.append(f"{bad} 格不可复算")
        if failures:
            print(f"\n✗ VALIDATE FAIL: {'；'.join(failures)}")
            return 1
        print(f"\n✓ VALIDATE PASS（逐项 MAE {item_mae:.2f}、总分 MAE {total_mae:.2f}、"
              f"分桶 MAE {bucket_mae:.2f}、τ {tau_score:+.2f}、可复算 {ok}/{ok + bad}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
