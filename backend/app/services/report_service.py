"""评标报告：数据组装（JSON）与 HTML 渲染。

报告必须自证：规则版本哈希、每条分值的证据与核验状态、复核留痕（人/时间/理由）、
以及"AI 为辅助、结论由评标委员会确认"的合规声明。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.db.models import (
    AuditLog,
    Bidder,
    Criterion,
    EvidenceCitation,
    EvaluationRun,
    Project,
    ReviewRecord,
    RulePackage,
    ScoreRecord,
)
from app.db.session import session_scope
from app.errors import NotFound
from app.services.ranking_service import compute_ranking

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "report" / "templates"

CATEGORY_LABELS = {
    "qualification": "资格性审查",
    "compliance": "符合性审查",
    "price": "价格分",
    "technical": "技术分",
    "commercial": "商务分",
}

REVIEW_LABELS = {
    "pending": "待复核",
    "ai_suggested": "AI 已建议（待人工确认）",
    "human_confirmed": "已人工确认",
    "human_overridden": "已人工改分",
    "conflict_flagged": "冲突待裁决",
    "not_applicable": "不适用",
}


def build_report(project_id: str, run_id: int | None = None) -> dict:
    with session_scope() as s:
        project = s.get(Project, project_id)
        if project is None:
            raise NotFound("项目不存在")
        if run_id is None:
            run = s.execute(
                select(EvaluationRun).where(EvaluationRun.project_id == project_id)
                .order_by(EvaluationRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            run_id = run.id if run else None
        if run_id is None:
            raise NotFound("该项目尚无评分运行，无法生成报告")
        run = s.get(EvaluationRun, run_id)
        pkg = s.get(RulePackage, run.package_id) if run else None

        ranking = compute_ranking(project_id, run_id)

        rows = s.execute(
            select(ScoreRecord, Criterion, Bidder)
            .join(Criterion, Criterion.id == ScoreRecord.criterion_id)
            .join(Bidder, Bidder.id == ScoreRecord.bidder_id)
            .where(ScoreRecord.run_id == run_id)
            .order_by(Bidder.id, Criterion.sort_order)
        ).all()

        citation_rows = s.execute(
            select(EvidenceCitation).where(
                EvidenceCitation.score_record_id.in_([r[0].id for r in rows] or [-1])
            )
        ).scalars().all()
        citations_by_score: dict[int, list[EvidenceCitation]] = {}
        for c in citation_rows:
            citations_by_score.setdefault(c.score_record_id or 0, []).append(c)

        bidders_detail: dict[int, dict] = {}
        for score, crit, bidder in rows:
            entry = bidders_detail.setdefault(
                bidder.id,
                {"bidder_id": bidder.id, "name": bidder.name,
                 "bid_price": str(bidder.bid_price) if bidder.bid_price else None,
                 "status": bidder.status, "items": []},
            )
            entry["items"].append(
                {
                    "code": crit.code,
                    "name": crit.name,
                    "category": CATEGORY_LABELS.get(crit.category, crit.category),
                    "max_score": str(crit.max_score) if crit.max_score else None,
                    "score_type": crit.score_type,
                    "is_veto": crit.score_type == "veto",
                    "machine_status": score.machine_status,
                    "final_score": str(score.final_score) if score.final_score is not None else None,
                    "ai_suggested_score": str(score.ai_suggested_score)
                    if score.ai_suggested_score is not None else None,
                    "review_status": REVIEW_LABELS.get(score.review_status, score.review_status),
                    "evaluator_actor": score.evaluator_actor,
                    "confirm_time": score.confirm_time.isoformat() if score.confirm_time else None,
                    "evidence_status": score.evidence_status,
                    "source_quote": crit.source_quote,
                    "source_page": crit.source_page,
                    "citations": [
                        {
                            "quote": c.quote,
                            "page_no": c.page_no,
                            "node_id": c.node_id,
                            "verified": c.verified,
                            "verify_method": c.verify_method,
                            "created_by": c.created_by,
                        }
                        for c in citations_by_score.get(score.id, [])
                    ],
                }
            )

        reviews = s.execute(
            select(ReviewRecord).order_by(ReviewRecord.id.desc()).limit(300)
        ).scalars().all()
        audits = s.execute(
            select(AuditLog).where(AuditLog.project_id == project_id)
            .order_by(AuditLog.id.desc()).limit(200)
        ).scalars().all()

        return {
            "project": {
                "id": project.id, "name": project.name,
                "purchaser": project.purchaser, "code": project.project_code,
                "max_price": str(project.max_price) if project.max_price else None,
            },
            "run": {
                "id": run.id, "status": run.status,
                "package_version": run.package_version,
                "package_content_hash": run.package_content_hash,
                "llm_provider": run.llm_provider, "llm_model": run.llm_model,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            },
            "rule_package": {
                "version": pkg.version if pkg else None,
                "status": pkg.status if pkg else None,
                "content_hash": pkg.content_hash if pkg else None,
                "frozen_by": pkg.frozen_by if pkg else None,
                "frozen_at": pkg.frozen_at.isoformat() if pkg and pkg.frozen_at else None,
            },
            "ranking": ranking,
            "bidders": list(bidders_detail.values()),
            "reviews": [
                {
                    "subject_type": r.subject_type, "subject_id": r.subject_id,
                    "action": r.action, "actor": r.actor, "reason": r.reason,
                    "created_at": r.created_at.isoformat(),
                }
                for r in reviews
            ],
            "audit": [
                {
                    "action": a.action, "actor": a.actor,
                    "subject": f"{a.subject_type or ''}:{a.subject_id or ''}",
                    "created_at": a.created_at.isoformat(),
                }
                for a in audits
            ],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }


def render_html(report: dict) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report.html.j2")
    return template.render(**report)
