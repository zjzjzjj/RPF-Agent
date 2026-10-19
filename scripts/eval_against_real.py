# -*- coding: utf-8 -*-
"""评分结果 vs 真实评审报告 基准回归（浙江中烟项目 ground truth）。

真实基准来源：《…评审资料-发项目负责人.pdf》（176 页，人工评审委员会结论）：
- 最终得分：龙即 96.59 / 致成 83.63 / 诚道 82.40 / 先进数通 66.80
- 价格分：49.91 / 49.75 / 49.68 / 49.80（与确定性引擎逐分一致）
- 商务分项：业绩 龙即6 致成6 诚道4 先数0；认证 龙即4 致成4 诚道3 先数1
  （先数业绩 0=未提供发票/验收材料；认证 1=未提供网站查询截图——证明材料硬闸门）

用法：python scripts/eval_against_real.py --run 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

REAL = {
    "龙即": {"total": 96.59, "price": 49.91, "biz": 10.0, "tech_biz": 46.68, "rank": 1},
    "致成": {"total": 83.63, "price": 49.75, "biz": 10.0, "tech_biz": 33.88, "rank": 2},
    "诚道": {"total": 82.40, "price": 49.68, "biz": 7.0, "tech_biz": 32.72, "rank": 3},
    "先进数通": {"total": 66.80, "price": 49.80, "biz": 1.0, "tech_biz": 17.00, "rank": 4},
}


def kendall_tau(a: list, b: list) -> float:
    n = len(a)
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            sign_a = (a[i] > a[j]) - (a[i] < a[j])
            sign_b = (b[i] > b[j]) - (b[i] < b[j])
            if sign_a == sign_b:
                concordant += 1
            else:
                discordant += 1
    denom = n * (n - 1) / 2
    return (concordant - discordant) / denom if denom else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--project", default="PRJ-20261014-78MT")
    args = ap.parse_args()

    import sqlite3

    db = Path(__file__).resolve().parents[1] / "data" / "bid_eval.db"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    cur = con.cursor()

    # 排名/总分从 compute_ranking 现算（与 API 同源），不读缓存表
    from app.services.ranking_service import compute_ranking
    ranking = compute_ranking(args.project, args.run)
    ai = {}
    for row in ranking.get("rows", []):
        name = row["bidder_name"]
        key = next((k for k in REAL if k in name), None)
        if key:
            price = None
            for crit_name, val in _item_scores(cur, args.run, row["bidder_id"]):
                if "报价" in crit_name and "分项" not in crit_name:
                    price = float(val) if val is not None else None
            ai[key] = {
                "total": float(row["total"]) if row["total"] is not None else 0.0,
                "rank": row["rank"],
                "tech_biz": float(row["total"] or 0) - float(price or 0),
                "price": float(price) if price is not None else None,
                "biz": float(_biz_score(cur, args.run, row["bidder_id"])),
            }

    print(f"=== run {args.run} vs 真实评审报告 ===")
    print(f"{'公司':<6} {'真实总分':>8} {'AI总分':>8} {'Δ总分':>7} {'真实技+商':>9} {'AI技+商':>8} {'Δ技+商':>7} {'真实名次':>8} {'AI名次':>6}")
    tau_real_rank, tau_ai_rank, tau_real_score, tau_ai_score = [], [], [], []
    for key in ("龙即", "致成", "诚道", "先进数通"):
        real = REAL[key]
        got = ai.get(key)
        if not got:
            print(f"{key:<6}  —— AI 结果缺失 ——")
            continue
        d_total = got["total"] - real["total"]
        d_tb = got["tech_biz"] - real["tech_biz"]
        print(f"{key:<6} {real['total']:>8.2f} {got['total']:>8.2f} {d_total:>+7.2f} "
              f"{real['tech_biz']:>9.2f} {got['tech_biz']:>8.2f} {d_tb:>+7.2f} "
              f"{real['rank']:>8} {got['rank']:>6}")
        tau_real_rank.append(real["rank"])
        tau_ai_rank.append(got["rank"])
        tau_real_score.append(real["total"])
        tau_ai_score.append(got["total"])

    mae_tb = sum(abs(ai[k]["tech_biz"] - REAL[k]["tech_biz"]) for k in ai if k in REAL) / max(1, len(ai))
    print(f"\n技+商 逐公司 MAE: {mae_tb:.2f} 分（满分 50）")
    print(f"排序 Kendall τ（名次）: {kendall_tau(tau_real_rank, tau_ai_rank):+.2f}")
    print(f"排序 Kendall τ（分数）: {kendall_tau(tau_real_score, tau_ai_score):+.2f}")
    price_ok = all(
        ai[k]["price"] is not None and abs(ai[k]["price"] - REAL[k]["price"]) < 0.005
        for k in ai if k in REAL
    )
    print(f"价格分与真实逐分一致: {'是' if price_ok else '否'}")
    con.close()
    return 0


def _item_scores(cur, run_id: int, bidder_id: int):
    return cur.execute(
        """SELECT c.name, s.final_score FROM score_records s
           JOIN criteria c ON c.id=s.criterion_id
           WHERE s.run_id=? AND s.bidder_id=?""",
        (run_id, bidder_id),
    ).fetchall()


def _biz_score(cur, run_id: int, bidder_id: int) -> float:
    row = cur.execute(
        """SELECT SUM(s.final_score) FROM score_records s
           JOIN criteria c ON c.id=s.criterion_id
           WHERE s.run_id=? AND s.bidder_id=? AND c.category='commercial'""",
        (run_id, bidder_id),
    ).fetchone()
    return float(row[0] or 0)


if __name__ == "__main__":
    raise SystemExit(main())
