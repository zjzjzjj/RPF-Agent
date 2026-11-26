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
                    # P0-1（测试10 run113）：故障兜底暂定分的合计（不含进 total），
                    # 供报告双分值展示「已定分合计 / 含暂定合计」。
                    "provisional": Decimal(0),
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
            _is_fallback_cell = (
                _auto_meta.get("basis") == "fallback" and _fallback_kind != "duplicate_summary"
            )
            if _is_fallback_cell:
                entry["fallback"].append(criterion_name)
                from app.services.automation_service import technical_fallback_kind
                if technical_fallback_kind(score):
                    # Technical fallback values are audit subtotals only. They
                    # never enter totals, provisional totals or published rank.
                    final = None
            # match 判断项按设计不打分（final 恒 None）：不进 missing_final、不计总分，
            # 但照常落 breakdown（否则法律包的 8 条检查在评分矩阵里彻底消失）。
            if score_type not in ("veto", "match"):
                _skip_total = in_total_by_crit.get(crit.id) is False
                if final is None:
                    # 零未定分（2026-11-24 拍板）后计分格必有分；判定项恒 None
                    # 不进这里。防御分支保留。
                    entry["missing_final"].append(criterion_name)
                    continue
                if not _skip_total:
                    entry["total"] += Decimal(str(final))
                    if _is_fallback_cell:
                        # 零未定分口径（2026-11-24 用户拍板，覆盖 P0-1 旧口径）：
                        # 兜底暂定分计入总分、直接排名——疑问标记+need_human 清单
                        # 保底，人工改分后可重汇总。provisional=其中暂定分合计
                        # （审计/报告"其中含暂定"展示）。
                        entry["provisional"] += Decimal(str(final))
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
                    "provisional": _is_fallback_cell or None,
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
            # 技术待复核格不计分；此字段仅为旧版非技术兜底兼容展示。
            provisional_total = e["total"]
            out_rows.append(
                {
                    "rank": r.rank,
                    # 包B：参考名次=原排序位次（aggregate.rank_rows 算好、此处原样保留）。
                    # 正式名次在下面两个分支被抑制为 None 时，参考名次照发——仅供
                    # 人工定位，不构成评审结论（发布语义零改动，纯增量字段）。
                    "reference_rank": r.rank,
                    "bidder_id": r.bidder_id,
                    "bidder_name": r.name,
                    "bid_price": str(r.bid_price) if r.bid_price is not None else None,
                    "total": str(r.total),
                    # P0-1 双分值：total=已定分合计（不含暂定兜底）；
                    # provisional_total=含暂定分合计（审计对照用）。
                    "provisional_total": str(provisional_total),
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
        # 包B：正式名次被抑制时（下面两个分支），参考名次（=原排序位次）照发。
        rank_suppressed = False
        if automation and not automation["is_final"]:
            for row in out_rows:
                row["rank"] = None
                row["tie_break_level"] = None
                row["tie_explanations"] = []
            warnings.append("自动评审尚未全部完成，当前仅展示已完成分数，不发布最终排名")
            rank_suppressed = True
        elif any(row["fallback"] for row in out_rows):
            # 零未定分口径（2026-11-24 用户拍板，覆盖 P0-1 旧口径）：兜底暂定分
            # 计入总分、直接排名——只发疑问提示（人工改分后可重汇总），不抑制名次
            warnings.append("存在待人工复核格，未计入总分或正式排名")
        if rank_suppressed and any(row.get("reference_rank") is not None for row in out_rows):
            warnings.append("已提供参考名次（含暂定分/未完成项，仅供人工定位，不构成评审结论）")
        for row in out_rows:
            if row["missing_final"]:
                warnings.append(
                    f"{row['bidder_name']} 有 {len(row['missing_final'])} 项未定分"
                    f"（{('、'.join(row['missing_final'][:5]))}），总分不含这些项"
                )
            if row.get("fallback"):
                warnings.append(
                    f"{row['bidder_name']} 有 {len(row['fallback'])} 项待复核"
                    f"（{('、'.join(row['fallback'][:5]))}），未计入排名"
                )
        return {
            "run_id": run_id,
            "rows": out_rows,
            "warnings": warnings,
            "automation": automation,
            "is_final": automation["is_final"] if automation is not None else True,
            "provisional": automation is not None and not automation["is_final"],
            "has_fallback": any(row["fallback"] for row in out_rows),
        }
