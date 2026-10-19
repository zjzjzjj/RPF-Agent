"""评分结果聚合：一次返回排名、四分桶与逐项加减分明细 + 理由。

设计约束（与前端「评分结果」单页对应）：

- **总分/名次/价格分/技术分/商务分 一律取自 `compute_ranking`**——排名页与结果页
  必须逐字相等，本模块不另立口径。
- **骨架是 criteria × bidder 左连接 score_records**：本次运行没执行到的项
  （`not_run`）也会显式出现，而不是像 matrix/ranking 那样静默消失。
- **match / veto 不计分**（final 恒 None）：state 由 score_type 先判定，
  不能被当成「未定分」。
- **防锚定**：AI 建议分 / AI 理由只在「已定分终态或已解锁」时返回；
  未解锁时整块置空并置 `ai_locked=true`。
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select

from app.db.models import (
    Bidder,
    Criterion,
    CriterionLevel,
    EvaluationRun,
    EvidenceCitation,
    ReviewRecord,
    RulePackage,
    ScoreRecord,
    utcnow,
)
from app.db.session import session_scope
from app.errors import NotFound
from app.services.ranking_service import compute_ranking

# 分桶定义：key → category 映射。kind=score 的桶有分值；kind=verdict 的桶只统计判定。
BUCKET_DEFS: list[dict[str, Any]] = [
    {"key": "price", "label": "价格分", "kind": "score", "categories": ("price",)},
    {"key": "technical", "label": "技术分", "kind": "score", "categories": ("technical",)},
    {"key": "commercial", "label": "商务分", "kind": "score", "categories": ("commercial",)},
    {
        "key": "qualification_compliance",
        "label": "资格与符合性",
        "kind": "verdict",
        "categories": ("qualification", "compliance"),
    },
]
BUCKET_BY_CATEGORY = {cat: d["key"] for d in BUCKET_DEFS for cat in d["categories"]}
BUCKET_DEF_BY_KEY = {d["key"]: d for d in BUCKET_DEFS}

# AI 内容可见的终态：已定分后 AI 建议不可能再影响独立判断，可作审计展示。
SETTLED_STATUSES = {"human_confirmed", "human_overridden", "auto_confirmed", "not_applicable"}
# 会产生最终分的类型（用于桶满分汇总）
SCORING_TYPES = {"formula", "count", "subjective"}

MACHINE_VERDICT_LABELS = {
    "pass": "通过",
    "recommend_reject": "建议否决",
    "need_human": "待人工判定",
    "conflict_flagged": "冲突待裁决",
    "fail": "不通过",
    "unknown": "无法自动判定",
}

REASON_LIMIT_DEFAULT = 1200
QUOTE_MAX_CHARS = 300
SOURCE_QUOTE_MAX_CHARS = 400


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 —— 历史脏数据不阻塞整页
        return None


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _clip(text: Any, limit: int) -> tuple[str | None, int, bool]:
    """截断文本；返回 (文本, 原长, 是否截断)。None/空串返回 (None, 0, False)。"""
    if text is None:
        return None, 0, False
    raw = str(text)
    if not raw:
        return None, 0, False
    if len(raw) <= limit:
        return raw, len(raw), False
    return raw[:limit] + "…", len(raw), True


def _warnings_list(score: ScoreRecord | None) -> list[dict]:
    if score is None or not isinstance(score.warnings_json, list):
        return []
    return [w for w in score.warnings_json if isinstance(w, dict)]


def _machine_reason(score: ScoreRecord | None, limit: int) -> tuple[str | None, int, bool]:
    """机器判定理由：价格公式说明 / 计数说明 / 否决与 match 的 message。"""
    parts: list[str] = []
    unresolved = None
    for w in _warnings_list(score):
        if w.get("formula_explanation"):
            parts.append(str(w["formula_explanation"]))
        elif w.get("message"):
            parts.append(str(w["message"]))
        if w.get("unresolved"):
            unresolved = str(w["unresolved"])
    if not parts:
        return None, 0, False
    return _clip("\n".join(parts), limit)


def _unresolved(score: ScoreRecord | None) -> str | None:
    for w in _warnings_list(score):
        if w.get("unresolved"):
            return str(w["unresolved"])
    return None


def _ai_visible(score: ScoreRecord | None) -> bool:
    if score is None:
        return False
    return score.ai_revealed_at is not None or score.review_status in SETTLED_STATUSES


def _committee(score: ScoreRecord | None) -> list[dict]:
    raw = score.ai_raw_json if score is not None else None
    if not isinstance(raw, dict):
        return []
    members = raw.get("committee")
    if not isinstance(members, list):
        return []
    out = []
    for m in members:
        if not isinstance(m, dict):
            continue
        out.append(
            {
                "judge_no": m.get("judge_no"),
                "score": str(m["score"]) if m.get("score") is not None else None,
                "level_label": m.get("level_label"),
                "status": m.get("status"),
                "insufficient": bool(m.get("insufficient")),
                "dropped": bool(m.get("dropped")),
                "warnings": m.get("warnings") or [],
                "cluster_chars": m.get("cluster_chars"),
            }
        )
    return out


def _sub_scores(score: ScoreRecord | None) -> list[dict]:
    raw = score.ai_raw_json if score is not None else None
    if not isinstance(raw, dict):
        return []
    subs = raw.get("sub_scores")
    if not isinstance(subs, list):
        return []
    return [
        {
            "name": str(s.get("name") or ""),
            "score": s.get("score"),
            "max": s.get("max"),
        }
        for s in subs
        if isinstance(s, dict)
    ]


def _artifacts_check(score: ScoreRecord | None) -> list[dict]:
    raw = score.ai_raw_json if score is not None else None
    if not isinstance(raw, dict):
        return []
    items = raw.get("artifacts_check")
    if not isinstance(items, list):
        return []
    return [
        {
            "artifact": str(a.get("artifact") or ""),
            "found": bool(a.get("found")),
            "quote": _clip(a.get("quote"), QUOTE_MAX_CHARS)[0],
        }
        for a in items
        if isinstance(a, dict)
    ]


# ---------------------------------------------------------------------------
# 单条明细
# ---------------------------------------------------------------------------
def _build_item(
    *,
    crit: Criterion,
    score: ScoreRecord | None,
    levels: list[CriterionLevel],
    citations: list[EvidenceCitation],
    review: ReviewRecord | None,
    max_reason_chars: int,
    max_score_parts: list[Decimal],
) -> dict:
    score_type = (score.score_type if score is not None and score.score_type else None) or crit.score_type
    is_veto = bool(score.veto_flag) if score is not None else bool(crit.is_veto)

    if score is not None and score.max_score is not None:
        max_dec, max_source = _dec(score.max_score), "snapshot"
    else:
        max_dec, max_source = _dec(crit.max_score), "criterion"
    if max_dec is None:
        max_source = None

    final_dec = _dec(score.final_score) if score is not None else None
    machine_dec = _dec(score.machine_score) if score is not None else None

    # state 判定顺序：not_run → veto → match → missing_final → scored
    if score is None:
        state = "not_run"
    elif score_type == "veto":
        state = "veto"
    elif score_type == "match":
        state = "match"
    elif final_dec is None:
        state = "missing_final"
    else:
        state = "scored"

    if state in ("veto", "match", "not_run"):
        deduction = {"kind": "n_a", "points": None, "formula": None}
    elif state == "missing_final":
        deduction = {"kind": "undetermined", "points": None, "formula": None}
    elif max_dec is not None and final_dec is not None:
        points = (max_dec - final_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        deduction = {
            "kind": "points",
            "points": str(points),
            "formula": f"满分 {max_dec} − 得分 {final_dec}",
        }
    else:
        deduction = {"kind": "undetermined", "points": None, "formula": None}

    # 桶满分只统计会产生最终分的项
    if score_type in SCORING_TYPES and max_dec is not None:
        max_score_parts.append(max_dec)

    ai_visible = _ai_visible(score)
    ai_reason, ai_reason_chars, ai_truncated = (
        _clip(score.ai_reasoning, max_reason_chars) if ai_visible and score is not None else (None, 0, False)
    )
    machine_reason, _, machine_truncated = _machine_reason(score, max_reason_chars)
    human_reason, _, human_truncated = (
        _clip(review.reason, max_reason_chars) if review is not None else (None, 0, False)
    )

    verdict = None
    if state in ("veto", "match") and score is not None and score.machine_status:
        verdict = MACHINE_VERDICT_LABELS.get(score.machine_status, score.machine_status)

    cit_out = [
        {
            "id": c.id,
            "document_id": c.document_id,
            "page_no": c.page_no,
            "node_id": c.node_id,
            "quote": _clip(c.quote, QUOTE_MAX_CHARS)[0],
            "verified": bool(c.verified),
            "verify_method": c.verify_method,
            "bbox_json": c.bbox_json,
            "page_size_json": c.page_size_json,
            "citation_role": c.citation_role,
            "created_by": c.created_by,
        }
        for c in citations
    ]

    return {
        "criterion_id": crit.id,
        "score_record_id": score.id if score is not None else None,
        "code": crit.code,
        "name": crit.name,
        "category": crit.category,
        "score_type": score_type,
        "is_veto": is_veto,
        "bucket": BUCKET_BY_CATEGORY.get(crit.category, "qualification_compliance"),
        "state": state,
        "max_score": _s(max_dec),
        "max_score_source": max_source,
        "final": _s(final_dec),
        "machine_score": _s(machine_dec),
        "machine_status": score.machine_status if score is not None else None,
        "ai_suggested": _s(_dec(score.ai_suggested_score)) if ai_visible and score is not None else None,
        "ai_level_label": score.ai_level_label if ai_visible and score is not None else None,
        "ai_confidence": score.ai_confidence if ai_visible and score is not None else None,
        "human_score": _s(_dec(score.human_score)) if score is not None else None,
        "human_level_label": score.human_level_label if score is not None else None,
        "review_status": score.review_status if score is not None else "not_run",
        "evidence_status": score.evidence_status if score is not None else "none",
        "verdict": verdict,
        "deduction": deduction,
        "reason": {
            "ai_reasoning": ai_reason,
            "ai_reasoning_chars": ai_reason_chars,
            "truncated": bool(ai_truncated or machine_truncated or human_truncated),
            "machine_reason": machine_reason,
            "human_reason": human_reason,
            "human_action": review.action if review is not None else None,
            "human_actor": review.actor if review is not None else None,
            "human_at": _iso(review.created_at) if review is not None else None,
            "unresolved": _unresolved(score),
        },
        "ai_locked": score is not None and not ai_visible,
        "committee": _committee(score) if ai_visible else [],
        "sub_scores": _sub_scores(score) if ai_visible else [],
        "artifacts_check": _artifacts_check(score) if ai_visible else [],
        "evidence": {
            "status": score.evidence_status if score is not None else "none",
            "verified_count": sum(1 for c in citations if c.verified),
            "total_count": len(citations),
            "citations": cit_out,
        },
        "source": {
            "source_quote": _clip(crit.source_quote, SOURCE_QUOTE_MAX_CHARS)[0],
            "source_page": crit.source_page,
            "source_node_id": crit.source_node_id,
            "levels": [
                {
                    "label": lv.level_label,
                    "min": _s(lv.score_min),
                    "max": _s(lv.score_max),
                    "criteria": lv.description,
                }
                for lv in levels
            ],
        },
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_score_summary(
    project_id: str, run_id: int, *, max_reason_chars: int = REASON_LIMIT_DEFAULT
) -> dict:
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        if run is None or run.project_id != project_id:
            raise NotFound("评分运行不存在或不属于该项目")
        pkg = s.get(RulePackage, run.package_id)

        criteria = list(
            s.execute(
                select(Criterion)
                .where(
                    Criterion.package_id == run.package_id,
                    Criterion.status != "retired",
                )
                .order_by(Criterion.sort_order, Criterion.id)
            ).scalars()
        )
        all_bidders = list(
            s.execute(select(Bidder).where(Bidder.project_id == project_id)).scalars()
        )
        scores = list(
            s.execute(select(ScoreRecord).where(ScoreRecord.run_id == run_id)).scalars()
        )
        score_ids = [x.id for x in scores]

        citations = (
            list(
                s.execute(
                    select(EvidenceCitation).where(
                        EvidenceCitation.score_record_id.in_(score_ids)
                    )
                ).scalars()
            )
            if score_ids
            else []
        )
        reviews = (
            list(
                s.execute(
                    select(ReviewRecord)
                    .where(
                        ReviewRecord.subject_type == "score",
                        ReviewRecord.subject_id.in_([str(x) for x in score_ids]),
                    )
                    .order_by(ReviewRecord.id)
                ).scalars()
            )
            if score_ids
            else []
        )
        level_rows = (
            list(
                s.execute(
                    select(CriterionLevel)
                    .where(CriterionLevel.criterion_id.in_([c.id for c in criteria]))
                    .order_by(CriterionLevel.score_min)
                ).scalars()
            )
            if criteria
            else []
        )

        config = run.config_json if isinstance(run.config_json, dict) else {}
        participant_ids = [int(x) for x in config.get("bidder_ids") or []]
        auto_dropped = [str(x) for x in config.get("auto_dropped_bidders") or []]
        run_meta = {
            "id": run.id,
            "status": run.status,
            "ready": run.status == "succeeded" and bool(scores),
            "package_id": run.package_id,
            "package_version": run.package_version,
            "package_content_hash": run.package_content_hash,
            "package_origin": pkg.origin if pkg is not None else None,
            "llm_provider": run.llm_provider,
            "llm_model": run.llm_model,
            "stages": run.stages_json or [],
            "bidder_ids": participant_ids,
            "auto_dropped_bidders": auto_dropped,
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
        }

    # 分数索引
    score_by_key = {(x.bidder_id, x.criterion_id): x for x in scores}
    citations_by_score: dict[int, list[EvidenceCitation]] = {}
    for c in citations:
        if c.score_record_id is not None:
            citations_by_score.setdefault(c.score_record_id, []).append(c)
    review_by_score: dict[int, ReviewRecord] = {}
    for r in reviews:  # 已按 id 升序 → 后写覆盖 = 最新一条
        try:
            review_by_score[int(r.subject_id)] = r
        except (TypeError, ValueError):
            continue
    levels_by_crit: dict[int, list[CriterionLevel]] = {}
    for lv in level_rows:
        levels_by_crit.setdefault(lv.criterion_id, []).append(lv)

    ranking = compute_ranking(project_id, run_id)
    rank_by_bidder = {row["bidder_id"]: row for row in ranking["rows"]}

    # 自动剔除按名称匹配；重名时显式告警，不静默丢人
    auto_dropped_set = set(auto_dropped)
    name_hits = [b.id for b in all_bidders if b.name in auto_dropped_set]
    warnings: list[str] = list(ranking.get("warnings") or [])

    if not run_meta["ready"]:
        run_meta["incomplete_reason"] = (
            f"评分运行 #{run_meta['id']} 状态为 {run_meta['status']}，结果可能不完整"
        )
        warnings.append(run_meta["incomplete_reason"])
    else:
        run_meta["incomplete_reason"] = None

    if auto_dropped:
        unique_names = {b.name for b in all_bidders if b.name in auto_dropped_set}
        if len(unique_names) < len(auto_dropped_set):
            warnings.append("自动剔除名单含重名投标人，无法唯一对应，请人工核对："
                            + "、".join(sorted(auto_dropped_set - unique_names)))
        warnings.append("全自动模式已自动剔除未抽取到报价的投标人：" + "、".join(auto_dropped))

    if len(criteria) and participant_ids:
        unselected = [b for b in all_bidders if participant_ids and b.id not in set(participant_ids)
                      and b.name not in auto_dropped_set]
        if unselected:
            warnings.append(
                "以下投标人未参与本次运行（发起时未选择）：" + "、".join(b.name for b in unselected)
            )

    bidders_out: list[dict] = []
    for bidder in all_bidders:
        is_dropped = bidder.name in auto_dropped_set
        is_participant = (not participant_ids) or bidder.id in set(participant_ids)
        if is_dropped or not is_participant:
            reason = (
                "未抽取到投标报价，已自动剔除（全自动模式）"
                if is_dropped
                else "未参与本次运行（发起时未选择）"
            )
            bidders_out.append(
                {
                    "bidder_id": bidder.id,
                    "name": bidder.name,
                    "state": "excluded",
                    "excluded_reason": reason,
                    "bid_price": _s(_dec(bidder.bid_price)),
                    "bid_price_is_run_snapshot": False,
                    "bidder_status": bidder.status,
                    "bidder_status_is_run_snapshot": False,
                    "rank": None,
                    "veto_flag": False,
                    "tie_break_level": None,
                    "tie_explanations": [],
                    "summary": None,
                    "buckets": [],
                }
            )
            continue

        rank_row = rank_by_bidder.get(bidder.id)

        buckets_out: list[dict] = []
        for bdef in BUCKET_DEFS:
            items: list[dict] = []
            max_parts: list[Decimal] = []
            for crit in criteria:
                if crit.category not in bdef["categories"]:
                    continue
                score = score_by_key.get((bidder.id, crit.id))
                items.append(
                    _build_item(
                        crit=crit,
                        score=score,
                        levels=levels_by_crit.get(crit.id, []),
                        citations=citations_by_score.get(score.id, []) if score is not None else [],
                        review=review_by_score.get(score.id) if score is not None else None,
                        max_reason_chars=max_reason_chars,
                        max_score_parts=max_parts,
                    )
                )

            scored_count = sum(1 for i in items if i["state"] == "scored")
            missing_count = sum(1 for i in items if i["state"] == "missing_final")
            verdicts = None
            if bdef["kind"] == "verdict":
                verdicts = {"pass": 0, "recommend_reject": 0, "need_human": 0, "fail": 0}
                for i in items:
                    ms = i["machine_status"] or "need_human"
                    if ms in verdicts:
                        verdicts[ms] += 1
                    elif i["state"] != "not_run":
                        verdicts["need_human"] += 1

            if bdef["key"] == "price" and rank_row is not None:
                bucket_score = rank_row.get("price_score")
            elif bdef["key"] == "technical" and rank_row is not None:
                bucket_score = rank_row.get("tech_total")
            elif bdef["key"] == "commercial" and rank_row is not None:
                bucket_score = rank_row.get("commercial_total")
            else:
                bucket_score = None

            all_items = sum(
                (_dec(i["final"]) for i in items if i["final"] is not None),
                Decimal(0),
            ) if any(i["final"] is not None for i in items) else None

            buckets_out.append(
                {
                    "key": bdef["key"],
                    "label": bdef["label"],
                    "kind": bdef["kind"],
                    "max": _s(sum(max_parts, Decimal(0))) if max_parts else None,
                    "score": bucket_score,
                    "all_items_score": _s(all_items),
                    "item_count": len(items),
                    "scored_count": scored_count,
                    "missing_count": missing_count,
                    "verdicts": verdicts,
                    "items": items,
                }
            )

        if rank_row is not None:
            total = _dec(rank_row["total"]) or Decimal(0)
            price = _dec(rank_row.get("price_score"))
            tech = _dec(rank_row.get("tech_total")) or Decimal(0)
            comm = _dec(rank_row.get("commercial_total")) or Decimal(0)
            bucket_sum = (price or Decimal(0)) + tech + comm
            other = (total - bucket_sum).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            consistent = other == Decimal("0.00")
            if not consistent:
                warnings.append(
                    f"{bidder.name}：总分 {total} 与价格/技术/商务三桶之和 {bucket_sum} 不一致，"
                    f"差额 {other} 来自其它计分项（如多个价格项或资格/符合性项带分值）"
                )
            summary = {
                "total": rank_row["total"],
                "price_score": rank_row.get("price_score"),
                "tech_score": rank_row.get("tech_total"),
                "commercial_score": rank_row.get("commercial_total"),
                "other_score": str(other),
                "bucket_sum": str(bucket_sum),
                "consistent": consistent,
                "total_items": sum(b["item_count"] for b in buckets_out),
                "scored_items": sum(b["scored_count"] for b in buckets_out),
                "missing_count": sum(b["missing_count"] for b in buckets_out),
                "missing_criteria": list(rank_row.get("missing_final") or []),
            }
        else:
            summary = None

        bidders_out.append(
            {
                "bidder_id": bidder.id,
                "name": bidder.name,
                "state": "scored",
                "excluded_reason": None,
                "bid_price": _s(_dec(bidder.bid_price)),
                "bid_price_is_run_snapshot": False,
                "bidder_status": bidder.status,
                "bidder_status_is_run_snapshot": False,
                "rank": rank_row.get("rank") if rank_row else None,
                "veto_flag": bool(rank_row.get("veto_flag")) if rank_row else False,
                "tie_break_level": rank_row.get("tie_break_level") if rank_row else None,
                "tie_explanations": list(rank_row.get("tie_explanations") or []) if rank_row else [],
                "summary": summary,
                "buckets": buckets_out,
            }
        )

    # 排序：有排名的按名次，被剔除的放最后
    bidders_out.sort(key=lambda b: (b["rank"] is None, b["rank"] if b["rank"] is not None else 10**9))

    return {
        "project_id": project_id,
        "run": run_meta,
        "generated_at": utcnow().isoformat(),
        "criteria_count": len(criteria),
        "ai_gating": {
            "rule": "settled_or_revealed",
            "settled_statuses": sorted(SETTLED_STATUSES),
        },
        "warnings": warnings,
        "bidders": bidders_out,
    }
