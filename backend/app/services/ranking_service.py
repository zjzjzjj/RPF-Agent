"""排名服务：从 score_records 汇总 → 确定性排名 + 并列说明。

每次调用都从分数记录重算（确定性、毫秒级）——不存在"过期的排名缓存"。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.db.models import Bidder, Criterion, EvaluationRun, ScoreRecord
from app.db.session import session_scope
from app.engine.aggregate import rank_rows
from app.engine.models import RankRow


def compute_ranking(project_id: str, run_id: int | None = None) -> dict:
    with session_scope() as s:
        if run_id is None:
            run = s.execute(
                select(EvaluationRun).where(EvaluationRun.project_id == project_id)
                .order_by(EvaluationRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            if run is None:
                return {"run_id": None, "rows": [], "warnings": ["尚未发起评分运行"]}
            run_id = run.id

        rows = s.execute(
            select(ScoreRecord, Criterion, Bidder)
            .join(Criterion, Criterion.id == ScoreRecord.criterion_id)
            .join(Bidder, Bidder.id == ScoreRecord.bidder_id)
            .where(ScoreRecord.run_id == run_id)
        ).all()

        bidders: dict[int, dict] = {}
        for score, crit, bidder in rows:
            entry = bidders.setdefault(
                bidder.id,
                {
                    "bidder_id": bidder.id,
                    "name": bidder.name,
                    "bid_price": bidder.bid_price,
                    "status": bidder.status,
                    "total": Decimal(0),
                    "tech": Decimal(0),
                    "commercial": Decimal(0),
                    "price": None,
                    "breakdown": [],
                    # 否决标记：人工已确认否决，或存在"建议否决但尚未人工处置"的项
                    "veto_flag": bidder.status in ("recommend_reject", "rejected"),
                    "missing_final": [],
                },
            )
            if (
                crit.score_type == "veto"
                and score.machine_status == "recommend_reject"
                and score.review_status != "human_confirmed"
            ):
                entry["veto_flag"] = True
            final = score.final_score
            # match 判断项按设计不打分（final 恒 None）：不进 missing_final、不计总分，
            # 但照常落 breakdown（否则法律包的 8 条检查在评分矩阵里彻底消失）。
            if crit.score_type not in ("veto", "match"):
                if final is None:
                    entry["missing_final"].append(crit.name)
                    continue
                entry["total"] += Decimal(str(final))
                if crit.category == "technical":
                    entry["tech"] += Decimal(str(final))
                elif crit.category == "commercial":
                    entry["commercial"] += Decimal(str(final))
                elif crit.category == "price":
                    entry["price"] = Decimal(str(final))
            entry["breakdown"].append(
                {
                    "criterion": crit.name,
                    "code": crit.code,
                    "category": crit.category,
                    "final_score": str(final) if final is not None else None,
                    "review_status": score.review_status,
                    "veto": crit.score_type == "veto",
                }
            )

        rank_rows_in = [
            RankRow(
                bidder_id=e["bidder_id"],
                name=e["name"],
                total=e["total"],
                bid_price=e["bid_price"],
                tech_total=e["tech"],
                commercial_total=e["commercial"],
                veto_flag=e["veto_flag"],
            )
            for e in bidders.values()
        ]
        ranked = rank_rows(rank_rows_in)

        out_rows = []
        for r in ranked:
            e = bidders[r.bidder_id]
            out_rows.append(
                {
                    "rank": r.rank,
                    "bidder_id": r.bidder_id,
                    "bidder_name": r.name,
                    "bid_price": str(r.bid_price) if r.bid_price is not None else None,
                    "total": str(r.total),
                    "tech_total": str(r.tech_total),
                    "commercial_total": str(r.commercial_total),
                    "price_score": str(e["price"]) if e["price"] is not None else None,
                    "status": e["status"],
                    "veto_flag": r.veto_flag,
                    "tie_break_level": r.tie_break_level,
                    "tie_explanations": r.tie_explanations,
                    "missing_final": e["missing_final"],
                    "breakdown": e["breakdown"],
                }
            )

        warnings = []
        for row in out_rows:
            if row["missing_final"]:
                warnings.append(
                    f"{row['bidder_name']} 有 {len(row['missing_final'])} 项未定分"
                    f"（{('、'.join(row['missing_final'][:5]))}），总分不含这些项"
                )
        return {
            "run_id": run_id,
            "rows": out_rows,
            "warnings": warnings,
        }
