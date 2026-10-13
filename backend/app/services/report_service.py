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

SETTLED_STATUSES = ("human_confirmed", "human_overridden")

# 疑问分类：报告"先出草稿、把疑问标出"的核心。每条待复核记录归入唯一一类，
# 按严重度排序，让人一眼看到"哪几条真的需要现在裁决"。
# 关键区分：机器判定 pass 且证据已核验的客观项——设计上仍走复核流（防错），
# 但**无疑问**，归入可批量浏览确认的低severity档，不淹没真正的问题。
QUESTION_KINDS: dict[str, dict[str, str]] = {
    "veto_recommended": {
        "severity": "high",
        "label": "建议否决",
        "action": "机器按招标文件载明的证明材料名称逐字匹配未命中，或判据实质不符。"
                  "请人工翻阅原件核实：材料确实存在且符合要求 → 本项通过；"
                  "确属缺失或实质不符 → 否决事由。系统绝不自动否决。",
    },
    "conflict": {
        "severity": "high",
        "label": "判定冲突",
        "action": "核对原文后人工定分；冲突未消解前该分值不计入排名",
    },
    "evidence_unverified": {
        "severity": "high",
        "label": "主观分证据未核验",
        "action": "在证据核验工作台定位原文；核验通过后分值方可采信",
    },
    "count_field_unresolved": {
        "severity": "high",
        "label": "计分口径未确定",
        "action": "该计数项未给出计数口径（数业绩还是数证书），系统按 0 分处理。"
                  "请在规则包中补全该项的计数字段后重跑，勿采信当前 0 分。",
    },
    "count_no_facts": {
        "severity": "medium",
        "label": "未提取到计数事实",
        "action": "投标文件中未提取到业绩/证书清单，请人工翻阅原件核对后补录。",
    },
    "needs_judgment": {
        "severity": "medium",
        "label": "机器不可判定",
        "action": "依招标文件判据由评标委员会人工裁量",
    },
    "veto_needs_judgment": {
        "severity": "high",
        "label": "否决项待人工判定",
        "action": "该否决类条款机器无法自动判定（如密封、签字、盖章、实质性响应）。"
                  "须由评标委员会逐项裁决；未裁决前不得出具定稿报告。",
    },
    "ai_unconfirmed": {
        "severity": "medium",
        "label": "AI 建议待确认",
        "action": "先给独立分再解锁 AI 建议（防锚定），确认或改分后生效",
    },
    "no_score": {
        "severity": "medium",
        "label": "尚未定分",
        "action": "补录分值；未定分项不计入总分",
    },
    "batch_confirmable": {
        "severity": "low",
        "label": "机器判定通过（待浏览确认）",
        "action": "批量浏览确认即可，无实质疑问",
    },
}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# 计数项的"算不出来"由 engine/objective.py 的 CountResult.unresolved 结构化落库，
# 报告侧直接读该键——不再匹配中文措辞（措辞一改就会静默降级）。


def _machine_message(score) -> str:
    for w in score.warnings_json or []:
        if isinstance(w, dict):
            msg = str(w.get("message") or w.get("formula_explanation") or "")
            if msg:
                return msg
    return ""


def _count_problem(score) -> str | None:
    """计数项是否因口径/事实缺失而算不出。返回对应的疑问类型。

    读 scoring_service 落库的**结构化** unresolved 键（engine/objective.py 的 CountResult），
    不匹配中文措辞——措辞一改就会静默降级，该耦合已实际断过一次。
    """
    for w in score.warnings_json or []:
        if not isinstance(w, dict):
            continue
        code = w.get("unresolved")
        if code == "field_missing":
            return "count_field_unresolved"
        if code == "no_facts":
            return "count_no_facts"
    return None


def _classify_question(score, crit) -> str | None:
    """给一条评分记录定性。返回 QUESTION_KINDS 的键；已处置且无疑问返回 None。"""
    if score.review_status == "not_applicable":
        return None
    if score.review_status in SETTLED_STATUSES:
        return None

    # 否决类优先：建议否决是最高级别的待裁决事项
    if crit.score_type == "veto" or score.veto_flag:
        if score.machine_status == "recommend_reject":
            # 不做"材料未定位"的降级：机器无法区分"材料真的缺失"与"材料名没逐字匹配"
            # ——两者警告文本完全相同（样例中故意缺保证金的真阳性也报"未找到"）。
            # 替机器下这个结论就是猜测，违背"宁缺勿错"。措辞上如实说明两种可能即可。
            return "veto_recommended"
        if score.review_status == "conflict_flagged":
            return "conflict"
        if score.machine_status == "need_human":
            # 密封/签字/盖章/实质性响应这类否决条款机器判不了，**但绝不能当作已处置**：
            # 漏掉它们会让报告在"疑问清零"后宣称定稿，而这些否决事由从未被裁决。
            return "veto_needs_judgment"
        # 机器判定通过的资格/符合性项：按设计**仍走复核流**（防错，但默认可批量浏览）。
        # 归入低危而非"已处置"——否则报告宣称"全部 N 项均已人工处置"时，
        # 工作台/复核队列仍把这批列为待复核，两处结论直接打架。
        return "batch_confirmable"

    if score.review_status == "conflict_flagged":
        return "conflict"
    # match 判断项：按设计不打分（final 恒 None），按机器判定三态定性——
    # 绝不能落入 no_score（"尚未定分/补录分值"对判断项语义完全错误）。
    if crit.score_type == "match":
        if score.machine_status == "recommend_reject":
            return "veto_recommended"   # 防御：仅 is_veto 的 match 可能到此
        if score.machine_status in (None, "need_human"):
            return "needs_judgment"     # 中危：机器不可判，逐项裁决
        return "batch_confirmable"      # 低危：机器 pass，待浏览确认
    # 证据核验只对**主观分**有意义：formula/count/match 的证据是确定性计算过程本身
    # （价格分可复算、计数分有原语与参数），不产生引文，故 evidence_status 恒为 none。
    # 若不区分，所有机器计算分都会被误报为"证据未核验"。
    if crit.score_type == "subjective":
        # 先判证据：AI 给了建议但证据未核验时，final_score 仍为 None，
        # 若先判 final_score 会被归成中危"尚未定分"，掩盖了"无证据不评分"这条红线。
        if score.evidence_status not in ("verified", "not_required") and (
            score.ai_suggested_score is not None or score.final_score is not None
        ):
            return "evidence_unverified"
        if score.review_status == "ai_suggested":
            return "ai_unconfirmed"
        if score.machine_status == "need_human":
            return "needs_judgment"
        if score.final_score is None:
            return "no_score"
        return "batch_confirmable"
    if score.final_score is None:
        return "no_score"
    # 计数项算不出来必须显性标出——0 分是能力缺失，不是评分结论
    if crit.score_type == "count" and (problem := _count_problem(score)):
        return problem
    # 机器已给出分值且计算可复算，只是按设计走复核流
    return "batch_confirmable"


def _build_open_questions(rows: list, run_id: int) -> tuple[list[dict], dict]:
    """把待复核记录整理成按严重度排序的疑问清单，并给出处置进度。"""
    questions: list[dict] = []
    settled = 0
    for score, crit, bidder in rows:
        kind = _classify_question(score, crit)
        if kind is None:
            settled += 1
            continue
        meta = QUESTION_KINDS[kind]
        questions.append(
            {
                "kind": kind,
                "severity": meta["severity"],
                "label": meta["label"],
                "action": meta["action"],
                "machine_message": _machine_message(score),
                "bidder": bidder.name,
                "bidder_id": bidder.id,
                "score_id": score.id,
                "code": crit.code,
                "criterion": crit.name,
                "category": CATEGORY_LABELS.get(crit.category, crit.category),
                "max_score": str(crit.max_score) if crit.max_score else None,
                "final_score": str(score.final_score) if score.final_score is not None else None,
                "machine_status": score.machine_status,
                "review_status": REVIEW_LABELS.get(score.review_status, score.review_status),
                "evidence_status": score.evidence_status,
                "source_page": crit.source_page,
                "source_quote": crit.source_quote,
            }
        )
    questions.sort(key=lambda q: (_SEVERITY_ORDER.get(q["severity"], 9), q["bidder"], q["code"]))
    by_kind: dict[str, int] = {}
    for q in questions:
        by_kind[q["kind"]] = by_kind.get(q["kind"], 0) + 1

    # 按评分项聚合：同一否决条款在每家投标人上各报一条，逐条看是 5 倍的重复劳动。
    # 聚合后"投标文件签章"是一件事（对 5 家各判一次），处置入口一致，可批量裁决。
    grouped: dict[tuple[str, str], dict] = {}
    for q in questions:
        key = (q["code"], q["criterion"])
        g = grouped.setdefault(
            key,
            {
                "code": q["code"],
                "criterion": q["criterion"],
                "category": q["category"],
                "max_score": q["max_score"],
                "kind": q["kind"],
                "severity": q["severity"],
                "label": q["label"],
                "action": q["action"],
                "bidders": [],
            },
        )
        g["bidders"].append(
            {"bidder": q["bidder"], "bidder_id": q["bidder_id"], "score_id": q["score_id"],
             "final_score": q["final_score"], "machine_message": q["machine_message"]}
        )
    by_criterion = sorted(
        grouped.values(),
        key=lambda g: (_SEVERITY_ORDER.get(g["severity"], 9), -len(g["bidders"]), g["code"]),
    )

    progress = {
        "total": len(rows),
        "settled": settled,
        "open": len(questions),
        "blocking": sum(1 for q in questions if q["severity"] == "high"),
        "by_kind": by_kind,
        "criteria_open": len(by_criterion),
    }
    return questions, progress, by_criterion


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
        # run_id 是公开查询参数（过期链接、被清理的运行、或手工拼别的项目的 id）。
        # 不校验归属会生成"项目头是 A、数据全来自 B"的混搭报告——这是要对外提交的
        # 正式文书，串台比报错严重得多。取不到或不属于本项目一律 404。
        if run is None or run.project_id != project_id:
            raise NotFound("评分运行不存在或不属于该项目")
        pkg = s.get(RulePackage, run.package_id)

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
                    # 防锚定是服务端强制（见 api/scores.py）：未解锁前不得暴露 AI 建议分。
                    # 报告提前到复核前可生成后，这里若无条件输出就成了旁路——
                    # 评审人做独立判断前先看到 AI 建议，独立性即被破坏。
                    "ai_suggested_score": str(score.ai_suggested_score)
                    if (score.ai_suggested_score is not None and score.ai_revealed_at is not None)
                    else None,
                    "ai_revealed": score.ai_revealed_at is not None,
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

        open_questions, progress, by_criterion = _build_open_questions(rows, run_id)
        # 报告状态：还有待裁决项就是草稿。草稿**可以生成**（先出报告、把疑问标出），
        # 但必须自带醒目声明，避免被当成最终结论使用。
        # 另有一条硬约束：评分运行未成功时绝不能出定稿——否则"0 条记录"会让
        # progress.open 归零，报告宣称"全部 0 项均已人工处置"，把失败读成完成。
        run_ok = run.status == "succeeded" and bool(rows)
        report_status = "final" if (progress["open"] == 0 and run_ok) else "draft"
        if not run_ok:
            progress["incomplete_reason"] = (
                f"评分运行 #{run.id} 状态为 {run.status}，报告内容不完整"
            )

        return {
            "project": {
                "id": project.id, "name": project.name,
                "purchaser": project.purchaser, "code": project.project_code,
                "max_price": str(project.max_price) if project.max_price else None,
            },
            "status": report_status,
            "status_label": "报告定稿" if report_status == "final" else "报告草稿（待复核）",
            "progress": progress,
            "open_questions": open_questions,
            "open_by_criterion": by_criterion,
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
