"""评分详情 / 证据 / 复核 API。

防锚定是**服务端强制**：GET /scores/{sid} 默认不返回 ai_suggested_score，
只有 reveal 之后（或 ?reveal=true 且已解锁）才返回。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.db.models import EvidenceCitation, ScoreRecord, utcnow
from app.db.session import session_scope
from app.deps import get_actor
from app.errors import NotFound, ValidationFailed
from app.services import audit_service

router = APIRouter(tags=["scores"])


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    bidder_id: int
    criterion_id: int
    rule_version: int
    package_content_hash: str | None
    max_score: Decimal | None
    score_type: str | None
    veto_flag: bool
    machine_score: Decimal | None
    machine_status: str | None
    ai_level_label: str | None
    ai_reasoning: str | None
    ai_confidence: float | None
    human_score: Decimal | None
    human_level_label: str | None
    final_score: Decimal | None
    evidence_status: str
    review_status: str
    ai_revealed_at: datetime | None
    evaluator_actor: str | None
    confirm_time: datetime | None
    warnings_json: list | None
    ai_suggested_score: Decimal | None = None  # 仅在已解锁时填充
    # 工作台自足所需的关联信息（只读展示）
    criterion_code: str | None = None
    criterion_name: str | None = None
    category: str | None = None
    required_evidence: str | None = None
    source_quote: str | None = None
    source_page: int | None = None
    levels: list[dict] = []
    bidder_name: str | None = None


class CitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int | None
    page_no: int | None
    node_id: str | None
    quote: str
    bbox_json: list | None
    page_size_json: list | None
    verified: bool
    verify_method: str | None
    candidates_json: list | None
    citation_role: str
    created_by: str


class ReviewBody(BaseModel):
    action: Literal["confirm", "override", "reject_ai", "flag_conflict", "confirm_reject"]
    score: str | None = None
    level_label: str | None = None
    reason: str = Field(min_length=1)


class RevealBody(BaseModel):
    blind_skip: bool = False
    reason: str = ""


class EvidenceBody(BaseModel):
    document_id: int
    quote: str = Field(min_length=1)
    score_record_id: int | None = None
    page_hint: int | None = None


def _load_score(score_id: int) -> ScoreRecord:
    with session_scope() as s:
        row = s.get(ScoreRecord, score_id)
        if row is None:
            raise NotFound("评分记录不存在")
        s.expunge(row)
        return row


@router.get("/scores/{score_id}", response_model=ScoreOut)
def get_score(score_id: int) -> ScoreOut:
    """防锚定：AI 建议分只有在人工解锁（ai_revealed_at 已写）之后才返回。

    注意 ?reveal=true 之类的查询参数**不能**绕过解锁——否则防锚定形同虚设。
    """
    row = _load_score(score_id)
    out = ScoreOut.model_validate(row)
    _attach_context(out, row)
    if row.ai_revealed_at is None:
        out.ai_suggested_score = None
    return out


def _attach_context(out: ScoreOut, row: ScoreRecord) -> None:
    """补上工作台展示所需的评分项/投标人信息（只读）。"""
    from sqlalchemy import select

    from app.db.models import Bidder, Criterion, CriterionLevel

    with session_scope() as s:
        crit = s.get(Criterion, row.criterion_id)
        bidder = s.get(Bidder, row.bidder_id)
        if crit is not None:
            out.criterion_code = crit.code
            out.criterion_name = crit.name
            out.category = crit.category
            out.required_evidence = crit.required_evidence
            out.source_quote = crit.source_quote
            out.source_page = crit.source_page
            levels = s.execute(
                select(CriterionLevel).where(CriterionLevel.criterion_id == crit.id)
                .order_by(CriterionLevel.score_min.desc())
            ).scalars().all()
            out.levels = [
                {"label": lv.level_label, "min": str(lv.score_min), "max": str(lv.score_max),
                 "criteria": lv.description}
                for lv in levels
            ]
        if bidder is not None:
            out.bidder_name = bidder.name


@router.post("/scores/{score_id}/reveal", response_model=ScoreOut)
def reveal_score(score_id: int, body: RevealBody, actor: str = Depends(get_actor)) -> ScoreOut:
    row = _load_score(score_id)
    with session_scope() as s:
        row = s.get(ScoreRecord, score_id)
        if row.ai_revealed_at is not None:
            s.expunge(row)
            return ScoreOut.model_validate(row)
        if row.human_score is None and not (body.blind_skip and body.reason.strip()):
            raise ValidationFailed(
                "请先给出你的独立评分再查看 AI 建议；若决定不先打分，需勾选'跳过盲评'并说明理由"
            )
        row.ai_revealed_at = utcnow()
        s.add(row)
        s.add(
            _review_record(
                subject_type="score", subject_id=score_id,
                action="skip_blind" if body.blind_skip else "reveal",
                actor=actor,
                reason=body.reason or "已完成独立评分，解锁 AI 建议",
            )
        )
        s.flush()
        s.expunge(row)
        out = ScoreOut.model_validate(row)
    return out


@router.post("/scores/{score_id}/review", response_model=ScoreOut)
def review_score(score_id: int, body: ReviewBody, actor: str = Depends(get_actor)) -> ScoreOut:
    from decimal import Decimal, InvalidOperation

    with session_scope() as s:
        row = s.get(ScoreRecord, score_id)
        if row is None:
            raise NotFound("评分记录不存在")
        old = {"final": str(row.final_score) if row.final_score is not None else None,
               "review_status": row.review_status}

        if body.action in ("confirm", "override"):
            if row.score_type == "veto":
                # 否决项不打分：confirm = 人工核查后确认"通过（不否决）"
                if body.action == "override":
                    raise ValidationFailed("否决项不能改分；确认不否决请用 confirm")
                row.review_status = "human_confirmed"
                row.evaluator_actor = actor
                row.confirm_time = utcnow()
                s.add(row)
                s.add(
                    _review_record(
                        subject_type="score", subject_id=score_id, action="confirm",
                        actor=actor, reason=body.reason,
                        old_value_json=old,
                        new_value_json={"review_status": "human_confirmed",
                                        "note": "确认该项不构成否决"},
                    )
                )
                s.flush()
                s.expunge(row)
                out = ScoreOut.model_validate(row)
                if row.ai_revealed_at is None:
                    out.ai_suggested_score = None
                audit_service.log(None, actor, "score.confirm", "score", str(score_id), {})
                return out
            new_score = None
            if body.score is not None:
                try:
                    new_score = Decimal(body.score)
                except InvalidOperation as exc:
                    raise ValidationFailed("分数格式不正确") from exc
                max_score = row.max_score
                if max_score is not None and (new_score < 0 or new_score > Decimal(str(max_score))):
                    raise ValidationFailed(f"分数必须在 0 ~ {max_score} 之间")
                row.human_score = new_score
                row.human_level_label = body.level_label
            elif row.human_score is not None:
                new_score = row.human_score
            elif row.ai_suggested_score is not None:
                # 采纳 AI 建议：把建议值写入 human_score（留痕区分来源）
                new_score = row.ai_suggested_score
                row.human_score = new_score
                row.human_level_label = row.ai_level_label
            else:
                raise ValidationFailed("没有可确认的分值：请先打分或解锁采纳 AI 建议")

            row.final_score = new_score
            row.evaluator_actor = actor
            row.confirm_time = utcnow()
            if body.action == "override" or (
                row.ai_suggested_score is not None and new_score != row.ai_suggested_score
            ):
                row.review_status = "human_overridden"
            else:
                row.review_status = "human_confirmed"

        elif body.action == "confirm_reject":
            if not row.veto_flag:
                raise ValidationFailed("该评分记录不是否决项")
            row.final_score = None
            row.review_status = "human_confirmed"
            row.evaluator_actor = actor
            row.confirm_time = utcnow()
            from app.db.models import Bidder

            bidder = s.get(Bidder, row.bidder_id)
            if bidder is not None:
                bidder.status = "rejected"

        elif body.action == "reject_ai":
            row.review_status = "conflict_flagged"
        elif body.action == "flag_conflict":
            row.review_status = "conflict_flagged"

        s.add(row)
        s.add(
            _review_record(
                subject_type="score", subject_id=score_id, action=body.action,
                actor=actor, reason=body.reason,
                old_value_json=old,
                new_value_json={"final": str(row.final_score) if row.final_score is not None else None,
                                "review_status": row.review_status},
            )
        )
        s.flush()
        s.expunge(row)
        out = ScoreOut.model_validate(row)
        if row.ai_revealed_at is None:
            out.ai_suggested_score = None

    audit_service.log(None, actor, f"score.{body.action}", "score", str(score_id), {})
    return out


@router.get("/scores/{score_id}/evidence", response_model=list[CitationOut])
def score_evidence(score_id: int) -> list[CitationOut]:
    with session_scope() as s:
        rows = s.execute(
            select(EvidenceCitation).where(EvidenceCitation.score_record_id == score_id)
            .order_by(EvidenceCitation.id)
        ).scalars().all()
        return [CitationOut.model_validate(r) for r in rows]


@router.post("/evidence", response_model=CitationOut)
def add_evidence(body: EvidenceBody, actor: str = Depends(get_actor)) -> CitationOut:
    """人工补录证据：立即定位核验（verified 仍只由 verifier 写）。"""
    from app.evidence.verifier import create_citation

    cit_id = create_citation(
        body.document_id, body.quote, page_hint=body.page_hint,
        score_record_id=body.score_record_id, role="score", created_by="human",
    )
    with session_scope() as s:
        cit = s.get(EvidenceCitation, cit_id)
        s.expunge(cit)
        return CitationOut.model_validate(cit)


@router.post("/evidence/{citation_id}/relocate", response_model=CitationOut)
def relocate_evidence(citation_id: int) -> CitationOut:
    from app.evidence.verifier import verify_citation

    cit = verify_citation(citation_id)
    if cit is None:
        raise NotFound("证据不存在")
    return CitationOut.model_validate(cit)


@router.get("/projects/{project_id}/review-queue")
def review_queue(project_id: str, run_id: int | None = None) -> dict:
    """待复核清单：建议否决 > 冲突 > 未核验证据 > 低置信度。"""
    from sqlalchemy import select

    from app.db.models import Bidder, Criterion, EvaluationRun

    with session_scope() as s:
        if run_id is None:
            run = s.execute(
                select(EvaluationRun).where(EvaluationRun.project_id == project_id)
                .order_by(EvaluationRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            if run is None:
                return {"items": []}
            run_id = run.id
        rows = s.execute(
            select(ScoreRecord, Criterion, Bidder)
            .join(Criterion, Criterion.id == ScoreRecord.criterion_id)
            .join(Bidder, Bidder.id == ScoreRecord.bidder_id)
            .where(
                ScoreRecord.run_id == run_id,
                ScoreRecord.review_status.in_(("pending", "ai_suggested", "conflict_flagged")),
            )
        ).all()

    def _priority(row) -> int:  # noqa: ANN001
        score, crit, _bidder = row
        if crit.score_type == "veto" and score.machine_status == "recommend_reject":
            return 0
        if score.review_status == "conflict_flagged":
            return 1
        if score.evidence_status != "verified" and crit.score_type == "subjective":
            return 2
        return 3

    ordered = sorted(rows, key=_priority)
    items = []
    for score, crit, bidder in ordered:
        items.append(
            {
                "score_record_id": score.id,
                "bidder_id": bidder.id,
                "bidder_name": bidder.name,
                "criterion": crit.name,
                "code": crit.code,
                "score_type": crit.score_type,
                "priority": _priority((score, crit, bidder)),
                "review_status": score.review_status,
                "machine_status": score.machine_status,
                "evidence_status": score.evidence_status,
                "ai_confidence": score.ai_confidence,
            }
        )
    return {"run_id": run_id, "items": items}


@router.get("/projects/{project_id}/audit")
def project_audit(project_id: str) -> list[dict]:
    from app.services import audit_service

    return [
        {
            "id": r.id, "actor": r.actor, "action": r.action,
            "subject_type": r.subject_type, "subject_id": r.subject_id,
            "detail": r.detail_json, "created_at": r.created_at.isoformat(),
        }
        for r in audit_service.list_logs(project_id)
    ]


def _review_record(**kwargs: Any):  # noqa: ANN001
    from app.db.models import ReviewRecord

    return ReviewRecord(**kwargs)
