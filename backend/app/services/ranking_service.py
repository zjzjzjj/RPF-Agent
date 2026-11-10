"""排名服务：从 score_records 汇总 → 确定性排名 + 并列说明。

每次调用都从分数记录重算（确定性、毫秒级）——不存在"过期的排名缓存"。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.db.models import Bidder, Criterion, CriterionCheck, EvaluationRun, ScoreRecord
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

        from app.services.automation_service import progress
        run = s.get(EvaluationRun, run_id)
        if run is None or run.project_id != project_id:
            from app.errors import NotFound
            raise NotFound("评分运行不存在或不属于该项目")
        automation = progress(run_id, session=s)
        run_config = run.config_json or {}
        resolved_prices = {
            str(b["id"]): b.get("bid_price")
            for b in (run_config.get("automation_manifest") or {}).get("bidders", [])
        }
        resolved_prices.update(run_config.get("resolved_prices", {}))
        frozen_criteria = {int(c["id"]): c for c in
                           (run_config.get("automation_manifest") or {}).get("criteria", [])}
        frozen_bidders = {int(b["id"]): b for b in
                          (run_config.get("automation_manifest") or {}).get("bidders", [])}

        # P1-C.6 总分口径：params.in_total=False 的评分项（样品单列，新代商标三部分
        # 口径实证）不进总分与类别合计；breakdown 照常记录（审计可见）。
        _crit_ids = {crit.id for _sc, crit, _b in rows}
        in_total_by_crit: dict[int, bool] = {}
        if automation is not None:
            in_total_by_crit = {cid: False for cid, c in frozen_criteria.items()
                               if (c.get("params") or {}).get("in_total") is False}
        elif _crit_ids:
            for chk in s.execute(
                select(CriterionCheck).where(CriterionCheck.criterion_id.in_(_crit_ids))
            ).scalars():
                _p = chk.params_json or {}
                if _p.get("in_total") is False:
                    in_total_by_crit[chk.criterion_id] = False

        bidders: dict[int, dict] = {}
        for score, crit, bidder in rows:
            frozen = frozen_criteria.get(crit.id, {}) if automation is not None else {}
            if automation is not None and (crit.id not in frozen_criteria or bidder.id not in frozen_bidders):
                continue
            criterion_name = frozen.get("name", crit.name)
            criterion_code = frozen.get("code", crit.code)
            category = frozen.get("category", crit.category)
            score_type = frozen.get("score_type", crit.score_type)
            entry = bidders.setdefault(
                bidder.id,
                {
                    "bidder_id": bidder.id,
                    "name": frozen_bidders.get(bidder.id, {}).get("name", bidder.name),
                    "bid_price": (Decimal(str(resolved_prices[str(bidder.id)]))
                                  if resolved_prices.get(str(bidder.id)) is not None else bidder.bid_price
                                  if automation is None else None),
                    "status": bidder.status,
                    "total": Decimal(0),
                    "tech": Decimal(0),
                    "commercial": Decimal(0),
                    "price": None,
                    "breakdown": [],
                    # 否决标记：人工已确认否决，或存在"建议否决但尚未人工处置"的项
                    "veto_flag": bidder.status in ("recommend_reject", "rejected"),
                    "missing_final": [],
                    "fallback": [],
                },
            )
            if (
                score_type == "veto"
                and score.machine_status == "recommend_reject"
                and score.review_status != "human_confirmed"
            ):
                entry["veto_flag"] = True
            final = score.final_score
            _auto_meta = (score.ai_raw_json or {}).get("auto_review") or {}
            _fallback_kind = (_auto_meta.get("fallback") or {}).get("kind")
            if _auto_meta.get("basis") == "fallback" and _fallback_kind != "duplicate_summary":
                entry["fallback"].append(criterion_name)
            # match 判断项按设计不打分（final 恒 None）：不进 missing_final、不计总分，
            # 但照常落 breakdown（否则法律包的 8 条检查在评分矩阵里彻底消失）。
            if score_type not in ("veto", "match"):
                if final is None:
                    entry["missing_final"].append(criterion_name)
                    continue
                _skip_total = in_total_by_crit.get(crit.id) is False
                if not _skip_total:
                    entry["total"] += Decimal(str(final))
                    if category == "technical":
                        entry["tech"] += Decimal(str(final))
                    elif category == "commercial":
                        entry["commercial"] += Decimal(str(final))
                    elif category == "price":
                        entry["price"] = Decimal(str(final))
            entry["breakdown"].append(
                {
                    "criterion": criterion_name,
                    "code": criterion_code,
                    "category": category,
                    "final_score": str(final) if final is not None else None,
                    "review_status": score.review_status,
                    "veto": score_type == "veto",
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
                    "fallback": e["fallback"],
                    "breakdown": e["breakdown"],
                }
            )

        warnings = []
        if automation and not automation["is_final"]:
            for row in out_rows:
                row["rank"] = None
                row["tie_break_level"] = None
                row["tie_explanations"] = []
            warnings.append("自动评审尚未全部完成，当前仅展示已完成分数，不发布最终排名")
        for row in out_rows:
            if row["missing_final"]:
                warnings.append(
                    f"{row['bidder_name']} 有 {len(row['missing_final'])} 项未定分"
                    f"（{('、'.join(row['missing_final'][:5]))}），总分不含这些项"
                )
            if row.get("fallback"):
                warnings.append(
                    f"{row['bidder_name']} 有 {len(row['fallback'])} 项暂定分"
                    f"（{('、'.join(row['fallback'][:5]))}），需人工复核"
                )
        return {
            "run_id": run_id,
            "rows": out_rows,
            "warnings": warnings,
            "automation": automation,
            "is_final": automation["is_final"] if automation is not None else True,
            "provisional": automation is not None and not automation["is_final"],
        }
