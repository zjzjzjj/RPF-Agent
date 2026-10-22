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

# 逐评分项 ground truth（PDF 评审报告，代码按包内序号）
REAL_ITEMS = {
    "c18": {"龙即": 6, "致成": 6, "诚道": 4, "先进数通": 0},   # 业绩
    "c19": {"龙即": 4, "致成": 4, "诚道": 3, "先进数通": 1},   # 认证
    "c26": {"龙即": 4, "致成": 2, "诚道": 0, "先进数通": 0},   # 负责人经验（=c59）
    "c27": {"龙即": 6, "致成": 0, "诚道": 6, "先进数通": 0},   # 团队经验（=c60）
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
    ap.add_argument("--detailed", action="store_true", help="逐评分项对比")
    ap.add_argument("--project", default="PRJ-20261014-78MT")
    ap.add_argument("--db", default="", help="数据库路径（默认仓库 data/bid_eval.db）")
    args = ap.parse_args()

    import sqlite3

    db = Path(args.db) if args.db else Path(__file__).resolve().parents[1] / "data" / "bid_eval.db"
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
            # 价格项按 category 取（旧实现按名称找"报价"，编译产物名是"价格得分"
            # 或"报价部分"时找不到 → price=None → tech_biz=total，MAE 无意义）
            price = _bucket(cur, args.run, row["bidder_id"], "price")
            biz = _bucket(cur, args.run, row["bidder_id"], "commercial")
            total = float(row["total"]) if row["total"] is not None else 0.0
            ai[key] = {
                "total": total,
                "rank": row["rank"],
                # 技+商 = 总分 - 价格 - 商务（旧实现 price 缺失时等于 total）
                "tech_biz": total - (price or 0.0) - (biz or 0.0),
                "price": price,
                "biz": biz or 0.0,
                "items": _item_scores(cur, args.run, row["bidder_id"]),
            }

    # 商务桶为 0 但包内确实存在 commercial 计分项 → 类别分桶失真（2026-10-21 事故）
    commercial_items = cur.execute(
        """SELECT COUNT(*) FROM criteria c JOIN score_records s ON s.criterion_id=c.id
           WHERE s.run_id=? AND c.category='commercial'""",
        (args.run,),
    ).fetchone()[0]
    if commercial_items and all(abs(v["biz"]) < 1e-9 for v in ai.values()):
        print("[!] 警告：包内有 commercial 计分项，但商务桶合计为 0——疑似类别分桶失真"
              "（检查 criteria.category 是否被 LLM 标成 technical）\n")

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

    # 商务/技术分项（真实评委口径：商务 10/10/7/1、技术主观 36.68/23.88/25.72/16.00）
    print(f"\n{'公司':<6} {'商务分':>7} {'技术分':>7}  说明")
    for key in ("龙即", "致成", "诚道", "先进数通"):
        got = ai.get(key)
        if not got:
            continue
        tech_rows = [v for v in got["items"] if v[1] == "technical"]
        tech_sum = sum(float(v[3]) for v in tech_rows if v[3] is not None)
        tech_missing = sum(1 for v in tech_rows if v[3] is None)
        print(f"{key:<6} {got['biz']:>7.2f} {tech_sum:>7.2f}  "
              f"技术项 {len(tech_rows)} 项，未定分 {tech_missing} 项")

    # D1: 逐评分项对比
    if args.detailed:
        print(f"\n=== 逐评分项对比 ===")
        print(f"{'评分项':<6} {'公司':<6} {'真实':>6} {'AI':>6} {'Δ':>6}  {'状态':<10}")
        total_abs_err = 0
        total_items = 0
        for code, real_scores in REAL_ITEMS.items():
            for key in ("龙即", "致成", "诚道", "先进数通"):
                got = ai.get(key)
                if not got:
                    continue
                real_val = real_scores.get(key, 0)
                # find AI score for this criterion
                ai_val = None
                for item in got.get("items", []):
                    if item[0] == code:
                        ai_val = float(item[3]) if item[3] is not None else 0.0
                        break
                if ai_val is None:
                    print(f"{code:<6} {key:<6} {real_val:>6.1f} {'—':>6} {'—':>6}  未找到")
                    continue
                delta = ai_val - real_val
                status = "✓" if abs(delta) < 0.5 else ("高" if delta > 0 else "低")
                print(f"{code:<6} {key:<6} {real_val:>6.1f} {ai_val:>6.1f} {delta:>+6.1f}  {status}")
                total_abs_err += abs(delta)
                total_items += 1
        if total_items:
            print(f"\n逐项 MAE: {total_abs_err / total_items:.2f} 分")

    con.close()
    return 0


def _item_scores(cur, run_id: int, bidder_id: int):
    return cur.execute(
        """SELECT c.code, c.category, c.name, s.final_score FROM score_records s
           JOIN criteria c ON c.id=s.criterion_id
           WHERE s.run_id=? AND s.bidder_id=?""",
        (run_id, bidder_id),
    ).fetchall()


def _bucket(cur, run_id: int, bidder_id: int, category: str) -> float | None:
    """按 category 取分桶合计；桶内一条已定分都没有时返回 None（未定分）。"""
    row = cur.execute(
        """SELECT SUM(s.final_score), COUNT(s.final_score) FROM score_records s
           JOIN criteria c ON c.id=s.criterion_id
           WHERE s.run_id=? AND s.bidder_id=? AND c.category=?
             AND c.score_type NOT IN ('veto','match')""",
        (run_id, bidder_id, category),
    ).fetchone()
    if row is None or row[1] == 0:
        return None
    return float(row[0] or 0)


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
