"""评标报告：数据组装（JSON）与 HTML 渲染。

报告必须自证：规则版本哈希、每条分值的证据与核验状态、复核留痕（人/时间/理由）、
以及"AI 为辅助、结论由评标委员会确认"的合规声明。
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from sqlalchemy import select

from app.db.models import (
    AuditLog,
    Bidder,
    Criterion,
    Document,
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
from app.services.score_summary_service import SCORE_TYPE_LABELS

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "report" / "templates"

CATEGORY_LABELS = {
    "qualification": "资格性审查",
    "compliance": "符合性审查",
    "price": "价格分（客观）",
    "technical": "技术分（主观+客观）",
    "commercial": "商务分（客观）",
}

REVIEW_LABELS = {
    "pending": "待复核",
    "ai_suggested": "AI 已建议（待人工确认）",
    "human_confirmed": "已人工确认",
    "human_overridden": "已人工改分",
    "conflict_flagged": "冲突待裁决",
    "not_applicable": "不适用",
    "auto_confirmed": "已自动裁决（LLM 带原文证据）",
}

SETTLED_STATUSES = ("human_confirmed", "human_overridden", "auto_confirmed")

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
        "label": "AI 存疑·待评标委员会认定（不自动否决）",
        "action": "AI 无法从文本确认该条款（扫描件/签章/声明类，文本通道不可见），"
                  "已标疑问；投标文件不作否决，请评标委员会翻阅原件认定。",
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


# ---------------------------------------------------------------------------
# 正式评审报告（借鉴真实评审资料形态：正文四段 + 5 AI 评委矩阵 + 附表）
# ---------------------------------------------------------------------------
TAX_RATE = Decimal("0.06")  # 不含税换算（真实报告口径：不含税=含税/1.06）
JUDGE_LABEL = "AI 评委"

MACHINE_VERDICT_LABELS = {
    "pass": "通过",
    "recommend_reject": "建议否决",
    "need_human": "待人工判定",
    "conflict_flagged": "冲突待裁决",
    "fail": "不通过",
    "unknown": "无法自动判定",
}


def _no_tax(price: Decimal | None) -> Decimal | None:
    if price is None:
        return None
    return (price / (Decimal(1) + TAX_RATE)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _committee_of(score) -> list[dict]:
    """读 ai_raw_json.committee（5 AI 评委明细）；旧记录无则空。"""
    raw = score.ai_raw_json if isinstance(score.ai_raw_json, dict) else {}
    return raw.get("committee") or []


def build_formal_report(project_id: str, run_id: int | None = None) -> dict:
    """组装正式评审报告数据：正文四段 + 评审结果汇总（评委矩阵）+ 附表。

    形态对齐真实评审资料（如浙江中烟项目）：正文（项目简介/过程/程序/结论）
    → 评审结果汇总表（投标人 × 评委 + 最终得分 + 名次）→ 评审情况说明（逐家
    零分/扣分依据）→ 附表（资格审查/符合性/分项得分/价格/AI 评审明细/围串标）。
    合规：全程标注 AI 辅助评审，结论须评标委员会确认。
    """
    from app.engine.collusion import compute_cross_bidder_signals

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
        if run is None or run.project_id != project_id:
            raise NotFound("评分运行不存在或不属于该项目")
        if run.status != "succeeded":
            raise NotFound(f"评分运行 #{run_id} 状态为 {run.status}，未成功不能出正式报告")
        pkg = s.get(RulePackage, run.package_id)

        bidders = s.execute(select(Bidder).where(Bidder.project_id == project_id).order_by(Bidder.id)).scalars().all()
        criteria = s.execute(
            select(Criterion).where(Criterion.package_id == run.package_id, Criterion.status != "retired")
            .order_by(Criterion.sort_order)
        ).scalars().all()
        score_rows = s.execute(
            select(ScoreRecord, Criterion, Bidder)
            .join(Criterion, Criterion.id == ScoreRecord.criterion_id)
            .join(Bidder, Bidder.id == ScoreRecord.bidder_id)
            .where(ScoreRecord.run_id == run_id)
        ).all()
        citations = s.execute(
            select(EvidenceCitation).where(
                EvidenceCitation.score_record_id.in_([r[0].id for r in score_rows] or [-1])
            )
        ).scalars().all()
        docs = s.execute(
            select(Document).where(Document.project_id == project_id, Document.kind == "bid")
        ).scalars().all()

    ranking_rows = compute_ranking(project_id, run_id).get("rows", [])
    rank_by_bidder = {r["bidder_id"]: r for r in ranking_rows}

    # ---- 逐投标人聚合：客观/价格分（共享） + 各评委主观分明细 ----
    score_by_key: dict[tuple[int, int], tuple] = {}
    citations_by_score: dict[int, list] = {}
    for c in citations:
        citations_by_score.setdefault(c.score_record_id or 0, []).append(c)
    for score, crit, bidder in score_rows:
        score_by_key[(bidder.id, crit.id)] = (score, crit)

    base_by_bidder: dict[int, dict] = {}
    judges_scores: dict[int, dict[int, dict[int, Decimal | None]]] = {}  # bidder -> judge -> crit -> score
    for bidder in bidders:
        base = Decimal(0)
        price_explanations: list[str] = []
        for crit in criteria:
            entry = score_by_key.get((bidder.id, crit.id))
            if entry is None:
                continue
            score = entry[0]
            if crit.score_type != "subjective" and score.final_score is not None:
                base += Decimal(str(score.final_score))
            if crit.category == "price":
                for w in score.warnings_json or []:
                    if isinstance(w, dict) and w.get("formula_explanation"):
                        price_explanations.append(str(w["formula_explanation"]))
        base_by_bidder[bidder.id] = {"base": base, "price_explanations": price_explanations}
        judges_scores[bidder.id] = {}
        for crit in criteria:
            if crit.score_type != "subjective":
                continue
            entry = score_by_key.get((bidder.id, crit.id))
            committee = _committee_of(entry[0]) if entry else []
            per_judge: dict[int, Decimal | None] = {}
            for m in committee:
                val = m.get("score")
                per_judge[int(m.get("judge_no") or 0)] = Decimal(str(val)) if val is not None else None
            judges_scores[bidder.id][crit.id] = per_judge

    # 评委数取任一评委明细的最大 judge_no；单评委（size<2）不出评委列，
    # 汇总表退化为「投标人 → 最终得分」的常规形态
    committee_size = 0
    for per_crit in judges_scores.values():
        for per_judge in per_crit.values():
            committee_size = max(committee_size, max(per_judge.keys(), default=0))
    show_judges = committee_size >= 2
    judge_labels = [f"{JUDGE_LABEL}{i}" for i in range(1, committee_size + 1)] if show_judges else []

    # ---- 评审结果汇总表（评委矩阵 + 最终得分 + 名次） ----
    summary_rows = []
    for bidder in bidders:
        rk = rank_by_bidder.get(bidder.id, {})
        base = base_by_bidder[bidder.id]["base"]
        per_judge_totals = []
        for j in (range(1, committee_size + 1) if show_judges else []):
            subtotal = base
            missing = False
            for crit in criteria:
                if crit.score_type != "subjective":
                    continue
                v = judges_scores[bidder.id][crit.id].get(j)
                if v is None:
                    missing = True
                else:
                    subtotal += v
            per_judge_totals.append({"judge": f"{JUDGE_LABEL}{j}", "total": subtotal, "missing": missing})
        summary_rows.append({
            "bidder_id": bidder.id,
            "bidder": bidder.name,
            "bid_price": str(bidder.bid_price) if bidder.bid_price is not None else None,
            "price_no_tax": str(_no_tax(bidder.bid_price)) if bidder.bid_price is not None else None,
            "judge_totals": per_judge_totals,
            "final": str(rk.get("total")) if rk.get("total") is not None else None,
            "rank": rk.get("rank"),
            "veto_flag": rk.get("veto_flag", False),
            "status": bidder.status,
        })

    # ---- 评审情况说明：逐家零分/否决/未决依据 ----
    review_notes = []
    for bidder in bidders:
        items = []
        for crit in criteria:
            entry = score_by_key.get((bidder.id, crit.id))
            if entry is None:
                continue
            score = entry[0]
            is_zero = score.final_score is not None and Decimal(str(score.final_score)) == 0 and crit.score_type != "veto"
            is_reject = score.machine_status in ("recommend_reject", "conflict_flagged")
            if not (is_zero or is_reject):
                continue
            reason = score.ai_reasoning or ""
            if not reason:
                for w in score.warnings_json or []:
                    if isinstance(w, dict) and w.get("message"):
                        reason = str(w["message"])
                        break
            items.append({
                "code": crit.code,
                "criterion": crit.name,
                "category": CATEGORY_LABELS.get(crit.category, crit.category),
                "final": str(score.final_score) if score.final_score is not None else None,
                "verdict": MACHINE_VERDICT_LABELS.get(score.machine_status or "", score.machine_status),
                "reason": reason[:500],
            })
        if items:
            review_notes.append({"bidder": bidder.name, "items": items})

    # ---- 资格 / 符合性审查表（因素原文 × 各家判定） ----
    def _review_table(category: str) -> dict:
        crits = [c for c in criteria if c.category == category]
        rows = []
        for crit in crits:
            cells = []
            for bidder in bidders:
                entry = score_by_key.get((bidder.id, crit.id))
                if entry is None:
                    cells.append({"verdict": "未评", "reason": ""})
                    continue
                score = entry[0]
                reason = score.ai_reasoning or ""
                if not reason:
                    for w in score.warnings_json or []:
                        if isinstance(w, dict) and w.get("message"):
                            reason = str(w["message"])
                            break
                cells.append({
                    "verdict": MACHINE_VERDICT_LABELS.get(score.machine_status or "", score.machine_status or "-"),
                    "reason": reason[:300],
                })
            rows.append({
                "code": crit.code,
                "name": crit.name,
                "factor": crit.source_quote or crit.description or "",
                "cells": cells,
            })
        return {"judges": [b.name for b in bidders], "rows": rows}

    qualification_table = _review_table("qualification")
    compliance_table = _review_table("compliance")

    # ---- 分项得分表（technical/commercial 主观项：因素原文 × 各家 × 评委） ----
    score_groups = []
    for cat, label in (("technical", "技术评审"), ("commercial", "商务评审")):
        crits = [c for c in criteria if c.category == cat]
        if not crits:
            continue
        items = []
        for crit in crits:
            per_bidder = []
            for bidder in bidders:
                entry = score_by_key.get((bidder.id, crit.id))
                score = entry[0] if entry else None
                committee = _committee_of(score) if score else []
                per_judge = [
                    next((str(m.get("score")) for m in committee if m.get("judge_no") == j), "-")
                    for j in (range(1, committee_size + 1) if show_judges else [])
                ]
                per_bidder.append({
                    "bidder": bidder.name,
                    "judge_scores": per_judge,
                    "final": str(score.final_score) if score and score.final_score is not None else "-",
                })
            items.append({
                "code": crit.code,
                "name": crit.name,
                "factor": crit.source_quote or crit.description or "",
                "max_score": str(crit.max_score) if crit.max_score else None,
                "method": SCORE_TYPE_LABELS.get(crit.score_type, crit.score_type),
                "per_bidder": per_bidder,
            })
        score_groups.append({"category": label, "items": items})

    # ---- 价格评审表 ----
    price_rows = []
    for bidder in bidders:
        rk = rank_by_bidder.get(bidder.id, {})
        price_rows.append({
            "bidder": bidder.name,
            "bid_price": str(bidder.bid_price) if bidder.bid_price is not None else None,
            "price_no_tax": str(_no_tax(bidder.bid_price)) if bidder.bid_price is not None else None,
            "score": str(rk.get("price_score")) if rk.get("price_score") is not None else "-",
            "explanations": base_by_bidder[bidder.id]["price_explanations"][:3],
        })

    # ---- AI 评审明细（主观项逐条：评委分 + 弃分 + 引文 + 理由） ----
    ai_detail = []
    for bidder in bidders:
        items = []
        for crit in criteria:
            if crit.score_type != "subjective":
                continue
            entry = score_by_key.get((bidder.id, crit.id))
            if entry is None:
                continue
            score = entry[0]
            committee = _committee_of(score)
            items.append({
                "code": crit.code,
                "name": crit.name,
                "max_score": str(crit.max_score) if crit.max_score else None,
                "method": SCORE_TYPE_LABELS.get(crit.score_type, crit.score_type),
                "judges": [
                    {
                        "judge": f"{JUDGE_LABEL}{m.get('judge_no')}",
                        "score": str(m.get("score")) if m.get("score") is not None else "未给出",
                        "dropped": bool(m.get("dropped")),
                    }
                    for m in committee
                ],
                "final": str(score.final_score) if score.final_score is not None else None,
                "reasoning": (score.ai_reasoning or "")[:600],
                "citations": [
                    {"quote": c.quote[:160], "page_no": c.page_no, "verified": c.verified}
                    for c in citations_by_score.get(score.id, [])[:3]
                ],
            })
        if items:
            ai_detail.append({"bidder": bidder.name, "items": items})

    # ---- 围串标核查附录（系统可得信号 + 平台数据注记） ----
    prices = {b.id: b.bid_price for b in bidders}
    authors = {}
    for d in docs:
        if d.bidder_id is not None and d.author:
            authors.setdefault(d.bidder_id, []).append(d.author)
    signals_map = compute_cross_bidder_signals(prices, authors)
    collusion_rows = []
    for bidder in bidders:
        sig = signals_map.get(bidder.id, {})
        collusion_rows.append({
            "bidder": bidder.name,
            "signals": sig.get("signals") or [],
            "detail": sig.get("detail") or "",
        })
    collusion_note = (
        "本附录为系统可自动核验的信号（报价一致性/等差规律/文档作者相同）。"
        "真实评审资料中的硬件特征码、投标文件上传 IP、购标/投标联系人信息"
        "属电子招投标平台数据，本系统不可得，需评标委员会向平台调取核对。"
    )

    total_citations = len(citations)
    verified_citations = sum(1 for c in citations if c.verified)

    return {
        "project": {
            "id": project.id, "name": project.name, "purchaser": project.purchaser,
            "code": project.project_code,
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run": {
            "id": run.id, "package_version": run.package_version,
            "package_content_hash": run.package_content_hash,
            "llm_model": run.llm_model,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        },
        "committee_size": committee_size,
        "judge_labels": judge_labels,
        "summary_rows": summary_rows,
        "review_notes": review_notes,
        "qualification_table": qualification_table,
        "compliance_table": compliance_table,
        "score_groups": score_groups,
        "price_rows": price_rows,
        "tax_rate": str(TAX_RATE),
        "ai_detail": ai_detail,
        "collusion_rows": collusion_rows,
        "collusion_note": collusion_note,
        "declaration": {
            "ai_assisted": True,
            "text": (
                (
                    "本报告由 AI 辅助评审系统自动生成："
                    f"{committee_size} 个 AI 评审 Agent 对主观评分项独立评分，"
                    "去除一个最高分和一个最低分后取平均作为该项最终得分"
                    if committee_size >= 2
                    else "本报告由 AI 辅助评审系统自动生成：AI 评审 Agent 对主观评分项"
                    "依据招标文件载明的判据独立评定分值"
                )
                + "；否决项、计数项与价格分由确定性引擎计算。本报告供评标委员会参考，"
                "所有存疑事项、否决建议与最终结论须经评标委员会确认。"
                f"规则包版本 v{run.package_version}（内容哈希 {run.package_content_hash}）；"
                f"引文核验 {verified_citations}/{total_citations}。"
            ),
        },
    }


def render_formal_html(report: dict) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("formal_report.html.j2")
    return template.render(**report)


def render_docx(report: dict) -> bytes:
    """正式评审报告 → docx（python-docx；Word 可直接打开编辑/打印/签章）。"""
    from io import BytesIO

    from docx import Document as DocxDocument
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    doc = DocxDocument()
    style = doc.styles["Normal"]
    style.font.name = "宋体"
    style.font.size = Pt(11)

    def heading(text: str, level: int = 1) -> None:
        doc.add_heading(text, level=level)

    def para(text: str) -> None:
        doc.add_paragraph(text)

    def table(headers: list[str], rows: list[list[str]]) -> None:
        t = doc.add_table(rows=1, cols=len(headers))
        t.style = "Table Grid"
        for i, h in enumerate(headers):
            t.rows[0].cells[i].text = h
        for r in rows:
            cells = t.add_row().cells
            for i, v in enumerate(r):
                cells[i].text = str(v)

    # 封面
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(report["project"]["name"] + "\n评审报告（AI 辅助评审）")
    run.bold = True
    run.font.size = Pt(18)
    para(f"项目编号：{report['project']['code'] or '-'}")
    para(f"生成时间：{report['generated_at']}    评分运行：#{report['run']['id']}")
    doc.add_page_break()

    # 正文
    heading("一、项目与评审说明", 1)
    para(report["declaration"]["text"])
    heading("二、评审结果汇总表", 1)
    headers = ["投标人", "投标报价(含税)", "投标报价(不含税)"] + report["judge_labels"] + ["最终得分", "名次"]
    rows = []
    for r in report["summary_rows"]:
        rows.append(
            [r["bidder"], r["bid_price"] or "-", r["price_no_tax"] or "-"]
            + [j["total"] for j in r["judge_totals"]]
            + [r["final"] or "-", str(r["rank"] or "-") + ("（有否决建议）" if r["veto_flag"] else "")]
        )
    table(headers, rows)
    heading("三、评审情况说明（零分/否决依据）", 1)
    if not report["review_notes"]:
        para("无零分或否决建议事项。")
    for note in report["review_notes"]:
        para(f"{note['bidder']}：")
        for it in note["items"]:
            para(f"　· {it['criterion']}（{it['category']}）：{it['verdict']}"
                 + (f"，得 {it['final']} 分" if it["final"] is not None else "")
                 + f"。依据：{it['reason']}")
    heading("四、评标结论", 1)
    ordered = [r for r in report["summary_rows"] if r["rank"]]
    ordered.sort(key=lambda r: r["rank"])
    if ordered:
        para("按最终得分排序，推荐中标候选人如下：")
        for i, r in enumerate(ordered[:2], start=1):
            para(f"第{i}中标候选人：{r['bidder']}（最终得分 {r['final']}）")
        para("以上排序为 AI 辅助评审结果，须评标委员会确认后生效。")
    doc.add_page_break()

    # 附表 A1 资格 / A2 符合性
    for idx, (tname, tab) in enumerate(
        [("A1 资格性审查表", report["qualification_table"]),
         ("A2 符合性审查表", report["compliance_table"])], start=1
    ):
        heading(f"附表 {tname}", 1)
        for row in tab["rows"]:
            heading(f"{row['code']} {row['name']}", 3)
            if row["factor"]:
                para(f"评审因素（招标文件原文）：{row['factor'][:400]}")
            table(
                ["投标人", "判定", "依据"],
                [[tab["judges"][i], c["verdict"], c["reason"] or "-"] for i, c in enumerate(row["cells"])],
            )
    # A3 分项得分表
    heading("附表 A3 分项得分表（AI 评委矩阵）", 1)
    for group in report["score_groups"]:
        heading(group["category"], 2)
        for item in group["items"]:
            heading(f"{item['code']} {item['name']}（满分 {item['max_score']}）", 3)
            if item["factor"]:
                para(f"评审因素（原文）：{item['factor'][:400]}")
            table(
                ["投标人"] + report["judge_labels"] + ["最终得分(去尾)"],
                [[p["bidder"]] + p["judge_scores"] + [p["final"]] for p in item["per_bidder"]],
            )
    # A4 价格评审
    heading("附表 A4 价格评审表", 1)
    table(
        ["投标人", "投标报价(含税)", "投标报价(不含税)", "价格得分", "计算说明"],
        [[p["bidder"], p["bid_price"] or "-", p["price_no_tax"] or "-", p["score"],
          "；".join(p["explanations"])[:200] or "-"] for p in report["price_rows"]],
    )
    para(f"注：不含税价按 {report['tax_rate']} 增值税率换算（不含税=含税/1.06）。")
    # A5 AI 评审明细
    heading("附表 A5 AI 评审明细（主观项）", 1)
    for detail in report["ai_detail"]:
        heading(detail["bidder"], 2)
        for it in detail["items"]:
            marks = "，".join(
                f"{j['judge']} {j['score']}" + ("（已弃）" if j["dropped"] else "")
                for j in it["judges"]
            )
            heading(f"{it['code']} {it['name']}（满分 {it['max_score']}）→ 最终 {it['final']}", 3)
            para(f"评委分：{marks}")
            if it["reasoning"]:
                para(f"评审依据：{it['reasoning']}")
            for c in it["citations"]:
                para(f"　引文（p{c['page_no']}{'✓' if c['verified'] else '未核验'}）：{c['quote']}")
    # A6 围串标
    heading("附表 A6 围串标核查", 1)
    table(
        ["投标人", "命中信号", "说明"],
        [[r["bidder"], "、".join(r["signals"]) or "未命中", r["detail"] or "-"] for r in report["collusion_rows"]],
    )
    para(report["collusion_note"])

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()
