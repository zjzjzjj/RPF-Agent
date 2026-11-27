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

from sqlalchemy import func, select

from app import config as app_config
from app.db.models import (
    Bidder,
    Criterion,
    CriterionCheck,
    CriterionLevel,
    Document,
    DocumentPage,
    EvaluationRun,
    EvidenceCitation,
    ReviewRecord,
    RulePackage,
    ScoreRecord,
    utcnow,
)
from app.db.session import session_scope
from app.errors import NotFound
from app.services.duplicate_summary import duplicate_summary_ids
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

# 主观/客观口径（单一来源，前端与报告只消费投影结果，不各自判断）：
#   主观 = LLM 逐项评审（engine/subjective.py）；客观 = 确定性计算（engine/price.py、objective.py）；
#   判定项（veto/match）只出结论不计分，单列一类，避免被读成"客观计分"。
SUBJECTIVE_SCORE_TYPES = {"subjective"}
OBJECTIVE_SCORE_TYPES = {"formula", "count"}
VERDICT_SCORE_TYPES = {"veto", "match"}
SCORE_TYPE_LABELS = {
    "subjective": "主观（AI 评审）",
    "formula": "客观·公式",
    "count": "客观·计数",
    "veto": "否决项",
    "match": "判断项",
}


def _score_side(score_type: str | None) -> str:
    """score_type → 主观/客观/判定。未知类型按客观处理（宁保守：不宣称 AI 主观分）。"""
    if score_type in SUBJECTIVE_SCORE_TYPES:
        return "subjective"
    if score_type in VERDICT_SCORE_TYPES:
        return "verdict"
    return "objective"


# ---- 二轴制显示口径（2026-11-23 用户拍板）：报告/前端只消费，不各自派生 ----
# 方法轴=执行方式（AI评审/规则评审/混合）；口径轴=评标文件"主/客观分"列值
# （Criterion.score_column，来源 score_column_source）。缺列/措辞推断 → "—"不猜。
METHOD_AI = "AI评审"
METHOD_RULE = "规则评审"
METHOD_MIXED = "混合（AI核验事实+规则计数）"


def scoring_basis_of(score_column: str | None, score_column_source: str | None) -> str:
    """口径轴：严格取文件列值（file=骨架列解析/llm_copy=LLM 照抄列）；
    inferred（措辞推断）与缺列 → "—"（不猜，红线）。"""
    if score_column in ("subjective", "objective") and score_column_source in ("file", "llm_copy"):
        return "主观" if score_column == "subjective" else "客观"
    return "—"


def scoring_method_of(score_type: str | None, params: dict | None = None, *,
                      ai_raw: dict | None = None,
                      verdict_source: str | None = None) -> str:
    """方法轴：执行方式三标签。特例口径表（2026-11-23 方案）：
    主观引擎→AI评审；objective_as_subjective 代判→AI评审（口径=客观）；
    count→混合；presence/样品数量档位→规则评审；formula→规则评审；
    _objective_route 重路由→混合；veto/match 按 verdict_source 分 AI/规则。
    """
    params = params or {}
    raw = ai_raw or {}
    if ((raw.get("auto_review") or {}).get("basis") == "visual_fallback"):
        return METHOD_AI  # ② VL 兜底：视觉AI核验定分（展示层另加"（视觉AI核验）"）
    if score_type in ("veto", "match"):
        return METHOD_AI if (verdict_source or "machine") == "llm" else METHOD_RULE
    if score_type == "formula":
        return METHOD_RULE
    if score_type == "count":
        # presence/样品数量档位 = 零 LLM 确定性（raw.presence 标记）→ 规则评审；
        # 其余计数 = AI 核验事实 + 程序计数 → 混合
        return METHOD_RULE if raw.get("presence") else METHOD_MIXED
    # subjective
    if (params.get("_objective_form") or "") in ("all_or_nothing", "unparsed_objective"):
        return METHOD_AI  # 客观列→主观引擎代判（口径轴如实显示客观）
    if (raw.get("presence") or raw.get("calculation")
            or params.get("_objective_route") or params.get("engine")):
        return METHOD_MIXED  # 重路由/快路径确定性引擎出分
    return METHOD_AI


MACHINE_VERDICT_LABELS = {
    "unverifiable": "现有材料无法证实",
    "execution_failed": "评审执行失败",
    "pass": "通过",
    "recommend_reject": "建议否决",
    "need_human": "待人工判定",
    "conflict_flagged": "冲突待裁决",
    "fail": "不通过",
    "unknown": "无法自动判定",
    "concern": "存疑",
    "not_deeply_reviewed": "未深评",
}

LIGHTWEIGHT_VERDICT_LABELS = {
    "pass": "通过", "fail": "建议否决", "concern": "存疑",
    "not_deeply_reviewed": "未深评",  # legacy：仅历史 v1 行显示
}

# 二值化（2026-11-23 决策 0017，仅 schema v3）：通过=未见确定性否决点；
# fail 显示"存疑"（不写"不通过"），建议否决语义在理由/每家建议里表达。
LIGHTWEIGHT_VERDICT_LABELS_V3 = {"pass": "通过", "fail": "存疑"}

# 每家资格建议（纯投影，2026-11-16 三态化）：任一否决→建议否决；有存疑无否决→存疑；
# 全过→通过；无轻量资格判定条目（full 模式/未评）→ None。
QUALIFICATION_SUGGESTION_LABELS = {"pass": "通过", "concern": "存疑", "reject": "建议否决"}
# 二值化每家建议（schema v3）：只有确定性否决点才落"存疑（建议否决）"；
# 其余一律"通过"（附待人工核对注记）——"没发现证明材料"绝不否决。
QUALIFICATION_SUGGESTION_LABELS_V3 = {
    "pass": "通过", "reject": "存疑（建议否决，需人工确认）", "concern": "存疑",
}


def derive_qualification_suggestion(items: list[dict]) -> dict | None:
    """每家资格建议（纯投影）。仅对轻量运行（存在 lightweight 格）产出。

    范围=该家**全部** veto/match 条目。schema v3（二值化）：任一确定性否决点
    （machine_status=fail）→"存疑（建议否决，需人工确认）"；否则→"通过"，
    并附 needs_human_check 计数作待人工核对注记（缺证据/未查验绝不否决）。
    v1/v2 旧运行保持三态投影逐字不变：任一 fail/recommend_reject→建议否决；
    其余任一非 pass（含 None/未评）→存疑；全 pass→通过。
    """
    verdict_items = [i for i in items if i.get("score_type") in ("veto", "match")]
    if not any(i.get("lightweight") for i in verdict_items):
        return None  # full 模式不出轻量建议
    if any((i.get("lightweight_schema_version") or 0) >= 3 for i in verdict_items if i.get("lightweight")):
        reject_items = [i for i in verdict_items if i.get("machine_status") in ("fail", "recommend_reject")]
        if reject_items:
            return {"code": "reject", "label": QUALIFICATION_SUGGESTION_LABELS_V3["reject"],
                    "reject_count": len(reject_items)}
        out: dict = {"code": "pass", "label": QUALIFICATION_SUGGESTION_LABELS_V3["pass"]}
        # 待人工核对注记=所有"非清洁通过"格：needs_human_check 的 pass 格
        # （签章/材料未见/节选覆盖有限/快审存疑线索/快审失败/引文核验不过）
        # + 故障兜底/未评/外部边界等残留格（2026-11-16 教训：残留在"通过"注记里必须可见）。
        manual = sum(1 for i in verdict_items
                     if not (i.get("machine_status") == "pass" and not i.get("needs_human_check")))
        if manual:
            out["manual_check_count"] = manual
        return out
    if any(i.get("machine_status") in ("fail", "recommend_reject") for i in verdict_items):
        return {"code": "reject", "label": QUALIFICATION_SUGGESTION_LABELS["reject"]}
    if any(i.get("machine_status") != "pass" for i in verdict_items):
        return {"code": "concern", "label": QUALIFICATION_SUGGESTION_LABELS["concern"]}
    return {"code": "pass", "label": QUALIFICATION_SUGGESTION_LABELS["pass"]}

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


def _fallback(score: ScoreRecord | None) -> dict | None:
    """兜底分标记（R3-5，2026-11-03）：warnings 结构化 fallback_score 键。"""
    for w in _warnings_list(score):
        fb = w.get("fallback_score")
        if isinstance(fb, dict):
            return fb
    return None


def _verdict_source(score: ScoreRecord | None, score_type: str | None) -> str | None:
    """判定来源（2026-11-05）：warnings 结构化 verdict_source 键（llm|machine）。

    仅判定类（veto/match）条目有值——展示层据此区分「AI 裁决」与「确定性
    判定项（过程即证据）」；LLM 裁决但没取到逐字引文的格子不能标成确定性判定。
    旧数据缺键按 "machine" 兜底（确定性判定展示口径）。
    """
    if score_type not in ("veto", "match"):
        return None
    for w in _warnings_list(score):
        if w.get("verdict_source"):
            return str(w["verdict_source"])
    return "machine"


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
    params: dict | None = None,
) -> dict:
    from app.services.automation_service import public_auto_review

    score_type = (score.score_type if score is not None and score.score_type else None) or crit.score_type
    is_veto = bool(score.veto_flag) if score is not None else bool(crit.is_veto)

    if score is not None and score.max_score is not None:
        max_dec, max_source = _dec(score.max_score), "snapshot"
    else:
        max_dec, max_source = _dec(crit.max_score), "criterion"
    if max_dec is None:
        max_source = None

    from app.services.automation_service import settled_fallback_score
    final_dec = _dec(score.final_score) if score is not None else None
    if final_dec is None and score is not None:
        final_dec = _dec(settled_fallback_score(score))
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

    auto_review_out = public_auto_review(score)
    lightweight = (auto_review_out or {}).get("basis") == "lightweight"
    light_payload = (auto_review_out or {}).get("lightweight") if lightweight else None
    light_schema = light_payload.get("schema_version") if isinstance(light_payload, dict) else None

    verdict = None
    if state in ("veto", "match") and score is not None and score.machine_status:
        if lightweight and light_schema == 3:
            # 二值化 v3：fail 显示"存疑"（建议否决语义在理由里），不写"不通过/建议否决"
            labels = LIGHTWEIGHT_VERDICT_LABELS_V3
        elif lightweight:
            labels = LIGHTWEIGHT_VERDICT_LABELS
        else:
            labels = MACHINE_VERDICT_LABELS
        verdict = ("自动核验已完成" if (auto_review_out or {}).get("material_review_pending")
                   else labels.get(score.machine_status, score.machine_status))

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
        "side": _score_side(score_type),
        # 二轴制：口径轴=文件列值（—=无列不猜）；方法轴=执行方式三标签
        "scoring_basis": scoring_basis_of(getattr(crit, "score_column", None),
                                          getattr(crit, "score_column_source", None)),
        "scoring_basis_source": getattr(crit, "score_column_source", None),
        "scoring_method": scoring_method_of(
            score_type,
            params=params or {},
            ai_raw=score.ai_raw_json if score is not None else None,
            verdict_source=_verdict_source(score, score_type),
        ),
        "is_veto": is_veto,
        "lightweight": lightweight,
        "lightweight_schema_version": light_schema if lightweight else None,
        "needs_human_check": (
            bool(light_payload.get("needs_human_check")) if isinstance(light_payload, dict) else None
        ),
        "bucket": BUCKET_BY_CATEGORY.get(crit.category, "qualification_compliance"),
        "state": state,
        "max_score": _s(max_dec),
        "max_score_source": max_source,
        "final": _s(final_dec),
        "machine_score": _s(machine_dec),
        "machine_status": score.machine_status if score is not None else None,
        "auto_review": auto_review_out,
        "fallback": (
            (auto_review_out or {}).get("basis") == "fallback"
            and (((score.ai_raw_json or {}).get("auto_review") or {}).get("fallback") or {}).get("kind")
            != "duplicate_summary"
            and settled_fallback_score(score) is None
        ),
        "ai_suggested": _s(_dec(score.ai_suggested_score)) if ai_visible and score is not None else None,
        "ai_level_label": score.ai_level_label if ai_visible and score is not None else None,
        "ai_confidence": score.ai_confidence if ai_visible and score is not None else None,
        "human_score": _s(_dec(score.human_score)) if score is not None else None,
        "human_level_label": score.human_level_label if score is not None else None,
        "review_status": score.review_status if score is not None else "not_run",
        "evidence_status": score.evidence_status if score is not None else "none",
        "verdict": verdict,
        "verdict_source": _verdict_source(score, score_type),
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
            "fallback_score": _fallback(score),
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
        from app.services.automation_service import progress as automation_progress
        automation = automation_progress(run_id, session=s)

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
        # 投标文件解析健康度（2026-11-18 run129 教训）：主标书 TOO_LARGE/解析
        # 任务放弃时，评分只看到报价表却静默出 0 分。这里显性告警，防止把
        # "材料缺失"误读成"评审结论"。
        bid_documents = list(
            s.execute(
                select(Document).where(
                    Document.project_id == project_id, Document.kind == "bid"
                )
            ).scalars()
        )
        # 每家投标文件的可读文本量：判"主标书是否真的进来了"（报价表伪页只有
        # 几百到几千字）。解析完成后这里会是数十万字符。
        bid_doc_chars: dict[int, int] = {}
        bid_doc_ids = [d.id for d in bid_documents]
        if bid_doc_ids:
            for doc_id, chars in s.execute(
                select(
                    DocumentPage.document_id,
                    func.coalesce(func.sum(func.length(DocumentPage.text)), 0),
                )
                .where(DocumentPage.document_id.in_(bid_doc_ids))
                .group_by(DocumentPage.document_id)
            ):
                bid_doc_chars[int(doc_id)] = int(chars or 0)
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
        # 二轴制：方法轴要读形态标记（_objective_form 等），补一次轻量 check join
        check_rows = (
            list(
                s.execute(
                    select(CriterionCheck).where(
                        CriterionCheck.criterion_id.in_([c.id for c in criteria]))
                ).scalars()
            )
            if criteria
            else []
        )
        params_by_crit = {c.criterion_id: (c.params_json or {}) for c in check_rows}

        # 重复汇总父项（c38 类 match 带分值 + 同 node 计分子项平铺）展示层隐藏：
        # 分数层已有 duplicate_summary 兜底不给分，但条目仍进列表会让用户看到
        # 「重复计分项」（2026-11-26 用户口径：项目中不要出现）。桶满分只累计
        # 计分类型，隐藏不改 max/总分/排名。
        hidden_crit_ids = duplicate_summary_ids(criteria)

        config = run.config_json if isinstance(run.config_json, dict) else {}
        participant_ids = [int(x) for x in config.get("bidder_ids") or []]
        auto_dropped = [str(x) for x in config.get("auto_dropped_bidders") or []]
        run_meta = {
            "id": run.id,
            "status": run.status,
            "ready": (automation["is_final"] if automation is not None
                      else run.status == "succeeded" and bool(scores)),
            "automation": automation,
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

    # 投标文件解析健康度告警：某家主标书全部未解析成功时，分数不是评审结论。
    participants = (
        [b for b in all_bidders if b.id in set(participant_ids)]
        if participant_ids else list(all_bidders)
    )
    docs_by_bidder: dict[int, list[Document]] = {}
    for doc in bid_documents:
        if doc.bidder_id is not None:
            docs_by_bidder.setdefault(doc.bidder_id, []).append(doc)
    for bidder in participants:
        docs = docs_by_bidder.get(bidder.id, [])
        if not docs:
            continue
        broken = [d for d in docs if d.parse_status in ("failed", "pending")]
        usable = [d for d in docs if d.parse_status in ("parsed", "degraded")]
        usable_chars = sum(bid_doc_chars.get(d.id, 0) for d in usable)
        filenames = "、".join(
            f"{d.original_filename}（{'待解析' if d.parse_status == 'pending' else '解析失败'}）"
            for d in broken[:3]
        )
        suffix = f" 等 {len(broken)} 份" if len(broken) > 3 else ""
        if usable_chars < app_config.bid_text_min_chars():
            # 主标书没进来：剩下的只是报价表（run129 实证 836–3340 字符，正常
            # 解析后数十万字符）。这种"0 分"不是评审结论，必须显著告警。
            detail = f"（{filenames}{suffix}）" if broken else ""
            warnings.append(
                f"{bidder.name}：投标文件可读文本仅约 {usable_chars} 字{detail}，"
                "主标书疑似未解析成功——技术/商务结论不代表实际投标内容"
            )
        elif broken:
            warnings.append(
                f"{bidder.name}：{len(broken)} 份投标文件解析失败或待解析"
                f"（{filenames}{suffix}），相关评分可能不完整"
            )

    if not run_meta["ready"]:
        if automation and run_meta["status"] == "partial":
            details = [f"已完成 {automation['completed']} 项"]
            if automation["material_pending"]:
                details.append(f"{automation['material_pending']} 项已自动核验")
            if automation["skipped"]:
                details.append(f"故障跳过 {automation['skipped']} 项")
            run_meta["incomplete_reason"] = "，".join(details) + "。下表合计为已评分小计。"
        else:
            run_meta["incomplete_reason"] = f"评分运行 #{run_meta['id']} 状态为 {run_meta['status']}，结果可能不完整"
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
                    "qualification_suggestion": None,
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
                if crit.id in hidden_crit_ids:
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
                        params=params_by_crit.get(crit.id),
                    )
                )

            scored_count = sum(1 for i in items if i["state"] == "scored")
            missing_count = sum(1 for i in items if i["state"] == "missing_final")
            fallback_count = sum(1 for i in items if i.get("fallback"))
            # 二轴制：口径轴计数（主/客/—）+ 方法轴计数（AI/规则/混合）
            subjective_count = sum(1 for i in items if i.get("scoring_basis") == "主观")
            objective_count = sum(1 for i in items if i.get("scoring_basis") == "客观")
            unlabeled_count = sum(1 for i in items if i.get("scoring_basis") == "—")
            ai_count = sum(1 for i in items if i.get("scoring_method") == METHOD_AI)
            rule_count = sum(1 for i in items if i.get("scoring_method") == METHOD_RULE)
            mixed_count = sum(1 for i in items if i.get("scoring_method") == METHOD_MIXED)
            verdicts = None
            if bdef["kind"] == "verdict":
                verdicts = {"pass": 0, "recommend_reject": 0, "need_human": 0, "fail": 0,
                            "concern": 0, "unverifiable": 0, "execution_failed": 0}
                for i in items:
                    ms = i["machine_status"] or "need_human"
                    if i.get("lightweight"):
                        # 轻量计桶：pass/fail 各归各桶（v3 fail=存疑/建议否决）；
                        # legacy concern/not_deeply_reviewed 归存疑桶
                        verdicts[ms if ms in ("pass", "fail") else "concern"] += 1
                    elif ms in ("pass", "fail", "recommend_reject", "need_human",
                                "unverifiable", "execution_failed"):
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
                    "fallback_count": fallback_count,
                    "subjective_count": subjective_count,
                    "objective_count": objective_count,
                    "unlabeled_count": unlabeled_count,
                    "ai_count": ai_count,
                    "rule_count": rule_count,
                    "mixed_count": mixed_count,
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
                "fallback_count": sum(b["fallback_count"] for b in buckets_out),
                "fallback_criteria": [
                    i["name"] for b in buckets_out for i in b["items"] if i.get("fallback")
                ],
            }
        else:
            summary = None

        bidders_out.append(
            {
                "bidder_id": bidder.id,
                "name": bidder.name,
                "state": "scored",
                "excluded_reason": None,
                "bid_price": _s(_dec((rank_row or {}).get("bid_price", bidder.bid_price))),
                "bid_price_is_run_snapshot": automation is not None,
                "bidder_status": bidder.status,
                "bidder_status_is_run_snapshot": False,
                "rank": rank_row.get("rank") if rank_row else None,
                "veto_flag": bool(rank_row.get("veto_flag")) if rank_row else False,
                "tie_break_level": rank_row.get("tie_break_level") if rank_row else None,
                "tie_explanations": list(rank_row.get("tie_explanations") or []) if rank_row else [],
                "qualification_suggestion": derive_qualification_suggestion(
                    [i for b in buckets_out for i in b["items"]]
                ),
                "summary": summary,
                "buckets": buckets_out,
            }
        )

    # 排序：有排名的按名次；无排名（含故障兜底/评审未完）按总分降序展示；被剔除的放最后
    bidders_out.sort(key=lambda b: (
        b["rank"] is None and b.get("state") == "excluded",  # 被剔除的排最后
        b["rank"] if b["rank"] is not None else 10**9,        # 有名次的按名次
        -float((b.get("summary") or {}).get("total") or 0),   # 无名次的按总分降序
    ))

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
