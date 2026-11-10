"""评标报告：数据组装（JSON）与 HTML 渲染。

报告必须自证：规则版本哈希、每条分值的证据与核验状态及操作留痕。
自动运行描述实际执行的方法与最终结论，历史运行保留原有辅助评审口径。
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace

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
from app.errors import NotFound, ValidationFailed
from app.services.automation_service import (
    completed_keys as automatic_completed_keys,
    progress as automation_progress,
    public_auto_review,
)
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

AUTOMATIC_REVIEW_METHOD = (
    "AI 按冻结细则核验资格与符合性事实，并按评分细则评定主观分值；"
    "未解决项按需执行定向检索、补充 OCR、视觉核验与 AI 复评，"
    "计数、客观分和价格分由程序按冻结规则确定性重算。"
    "资格与符合性给出“符合”“不符合”或仅针对缺失外部事实的“现有材料无法证实”最终结论；"
    "不符合的投标人仍参与评分和排名。"
    "文件读取与模型故障不得替代材料缺失结论或生成兜底分。"
)


QUALIFICATION_LIGHT_METHOD = (
    "资格与符合性采用确定性预检与每家投标人一次轻量快审，"
    "仅作报告提示，不构成完整资格审查，不阻断计分、排名与价格基准价。"
)


def _report_verdict(status: str | None, automated: bool, lightweight: bool = False) -> str:
    if automated:
        if lightweight:
            return {
                "pass": "预检通过", "fail": "预检不符合",
                "concern": "预检存疑（未深评）", "not_deeply_reviewed": "未深评",
                "execution_failed": "评审执行失败",
            }.get(status or "", "未深评")
        return {
            "pass": "符合", "fail": "不符合", "unverifiable": "现有材料无法证实",
            "execution_failed": "评审执行失败",
        }.get(status or "", "自动评审未完成")
    return MACHINE_VERDICT_LABELS.get(status or "", status or "-")


def _report_method(score_type: str, automated: bool, lightweight: bool = False) -> str:
    if automated:
        if lightweight and score_type in ("veto", "match"):
            return "确定性预检 + 单次轻量快审（不构成完整审查）"
        return {
            "subjective": "AI 按细则评分",
            "count": "AI 核验事实，程序确定性计数",
            "formula": "程序按冻结公式计算",
            "veto": "AI 事实核验与最终符合性判定",
            "match": "AI 事实核验与最终符合性判定",
        }.get(score_type, score_type)
    return SCORE_TYPE_LABELS.get(score_type, score_type)


def _is_lightweight(score) -> bool:
    return ((score.ai_raw_json or {}).get("auto_review") or {}).get("basis") == "lightweight"


def _report_reasoning(score, automated: bool) -> str:
    if automated:
        return score.ai_reasoning or (public_auto_review(score) or {}).get("reasoning") or ""
    return score.ai_reasoning or ""

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
        "action": "该计数项未给出计数口径（数业绩还是数证书），已按 0 分计入总分并标疑问。"
                  "请在规则包中补全该项的计数字段后重跑；如系口径缺失，当前 0 分不代表投标人实际水平。",
    },
    "count_no_facts": {
        "severity": "medium",
        "label": "未提取到计数事实",
        "action": "投标文件中未提取到业绩/证书清单，已按 0 分计入总分并标疑问。"
                  "请人工翻阅原件核对；如确有条目，可补录后重跑。",
    },
    "count_cases_unverified": {
        "severity": "medium",
        "label": "案例核验未完成",
        "action": "LLM 案例核验批失败/未产出（机器故障，非确认缺失）。"
                  "已按现有证据计入并标疑问；建议重跑该格，或人工翻阅原件核对业绩清单后改分。",
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
    "machine_fallback": {
        "severity": "high",
        "label": "兜底分（非评审结论）",
        "action": "机器故障/无有效输出，按最低档或 0 分暂定计入（零未定分政策）。"
                  "兜底分不是评审结论——请人工核看原件后改分，或重跑该格。",
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


def _fallback_marker(score) -> dict | None:
    """兜底分标记（R3-5，2026-11-03）：读 warnings 的结构化 fallback_score 键；
    旧数据兼容：message 含「按最低档兜底」。返回标记 dict 或 None。
    """
    for w in score.warnings_json or []:
        if not isinstance(w, dict):
            continue
        fb = w.get("fallback_score")
        if isinstance(fb, dict):
            return fb
        if "按最低档兜底" in str(w.get("message") or ""):
            return {"kind": "min_level_floor", "value": None, "basis": "legacy_text"}
    return None


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
        if code in ("cases_unverified", "llm_failed"):
            return "count_cases_unverified"
    return None


def _classify_question(score, crit) -> str | None:
    """给一条评分记录定性。返回 QUESTION_KINDS 的键；已处置且无疑问返回 None。"""
    if score.review_status == "not_applicable":
        return None
    _auto_meta = (getattr(score, "ai_raw_json", None) or {}).get("auto_review") or {}
    if ((_auto_meta.get("fallback") or {}).get("kind")) == "duplicate_summary":
        return None
    if _auto_meta.get("basis") == "fallback":
        return "machine_fallback"
    if score.review_status in SETTLED_STATUSES:
        return None

    # 兜底分优先定性（R3-5，2026-11-03）：机器故障按最低档兜底的格不是评审
    # 结论，压过 conflict/needs_judgment 的归类，报告「兜底分清单」直接可读。
    if _fallback_marker(score):
        return "machine_fallback"

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
            # 密封/签字/盖章/实质性响应这类否决条款机器判不了，仍列疑问清单
            # （提示级）；按 2026-11-04「veto 疑问不阻塞定稿」决策不再阻塞 final
            # 判定（定稿看 blocks_final）——但绝不得从清单消失（人工必须能看到）。
            return "veto_needs_judgment"
        # 机器判定通过的资格/符合性项：按设计**仍走复核流**（防错，但默认可批量浏览）。
        # 归入低危而非"已处置"——否则报告宣称"全部 N 项均已人工处置"时，
        # 工作台/复核队列仍把这批列为待复核，两处结论直接打架。
        return "batch_confirmable"

    # 计数项算不出来必须显性标出——0 分/未定分是能力缺失，不是评分结论。
    # 必须在 conflict/no_score 之前判，否则 unresolved 会被"疑问/未定分"吞掉。
    if crit.score_type == "count" and (problem := _count_problem(score)):
        return problem
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
    # 证据核验只对**主观分**有意义：formula/count 的证据是确定性计算过程本身
    # （价格分可复算、计数分有原语与参数），不产生引文，故 evidence_status 恒为 none。
    # 若不区分，所有机器计算分都会被误报为"证据未核验"。
    # veto/match 的 LLM 裁决引文自 2026-11-05 起落 role=veto|match（确定性判定仍
    # 不产引文），但 _doubt_kind 的证据检查保持只对主观分——判定类不适用红线。
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


def _fallback_row(score, crit, bidder) -> dict | None:
    """单格兜底分标记（R3-5）；非兜底格返回 None。结构化键读取，旧数据兼容文案。"""
    marker = _fallback_marker(score)
    zero_reason = None
    for w in score.warnings_json or []:
        if isinstance(w, dict) and w.get("zero_reason") in (
            "no_facts", "field_missing", "unreadable", "cases_unverified",
        ):
            zero_reason = w["zero_reason"]
            break
    if marker is None and not (
        zero_reason and score.final_score is not None
        and float(score.final_score) == 0.0
    ):
        return None
    return {
        "bidder": bidder.name,
        "bidder_id": bidder.id,
        "code": crit.code,
        "criterion": crit.name,
        "final_score": str(score.final_score) if score.final_score is not None else None,
        "kind": (marker or {}).get("kind") or f"zero_{zero_reason}",
        "basis": (marker or {}).get("basis") or zero_reason,
        "machine_message": _machine_message(score),
    }


def _fallback_items(rows: list) -> list[dict]:
    """兜底分清单（R3-5，2026-11-03）：零未定分政策下「按最低档兜底」与
    「机器没抽到事实按 0 计」的格——分数进了总分但**非评审结论**，单列供人工
    一眼看到哪些 0 分是能力缺失而非投标人水平。
    """
    out: list[dict] = []
    for score, crit, bidder in rows:
        item = _fallback_row(score, crit, bidder)
        if item is not None:
            out.append(item)
    return out


def _build_open_questions(rows: list, run_id: int) -> tuple[list[dict], dict, list[dict]]:
    """把待复核记录整理成按严重度排序的疑问清单，并给出处置进度。

    blocks_final（2026-11-04 资格审查弱化）：**按行判定**——veto/match 行与
    veto_flag 格的疑问只作提示（不阻塞定稿）；评分侧（count/subjective/formula/
    fallback/evidence 等）所有疑问仍阻塞。不能按 kind 判：auto 模式下 veto
    need_human 实际落 kind="conflict"（_classify_question 先命中 conflict 分支）。
    """
    questions: list[dict] = []
    settled = 0
    for score, crit, bidder in rows:
        kind = _classify_question(score, crit)
        if kind is None:
            settled += 1
            continue
        meta = QUESTION_KINDS[kind]
        blocks_final = not (
            crit.score_type in ("veto", "match") or score.veto_flag
            # Provisional closure scores are explicit, visible and listed in
            # the fallback appendix; they do not hide a missing matrix cell.
            # The report remains publishable with the caveat banner.
            or kind == "machine_fallback"
        )
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
                "blocks_final": blocks_final,
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
                "blocks_final": q["blocks_final"],
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
        # 阻塞性疑问（评分侧）——定稿判定用；veto/match 提示不计入
        "blocking_open": sum(1 for q in questions if q["blocks_final"]),
        "by_kind": by_kind,
        "criteria_open": len(by_criterion),
    }
    return questions, progress, by_criterion


def _build_automatic_questions(run, rows: list, automation: dict, completed: set) -> tuple[list[dict], dict, list[dict]]:
    """以冻结矩阵列出未完成项，缺失记录和技术故障均不得落入人工裁决文案。"""
    manifest = (run.config_json or {}).get("automation_manifest") or {}
    row_by_key = {(score.bidder_id, score.criterion_id): score for score, _crit, _bidder in rows}
    questions = []
    grouped = {}
    for bidder in manifest.get("bidders", []):
        for crit in manifest.get("criteria", []):
            key = (int(bidder["id"]), int(crit["id"]))
            if key in completed:
                continue
            score = row_by_key.get(key)
            meta = public_auto_review(score) or {}
            failed = meta.get("state") == "failed"
            question = {
                "kind": "execution_failed" if failed else "automatic_incomplete",
                "severity": "high" if failed else "medium",
                "label": "技术故障" if failed else "自动评审未完成",
                "action": "故障恢复后继续本次运行，仅执行未完成项",
                "machine_message": str(meta.get("error") or (score.ai_reasoning if score else "尚未产生评审记录") or "尚未形成有效终态"),
                "bidder": bidder.get("name") or str(bidder["id"]),
                "bidder_id": key[0], "score_id": score.id if score else None,
                "code": crit.get("code"), "criterion": crit.get("name"),
                "category": CATEGORY_LABELS.get(crit.get("category"), crit.get("category")),
                "max_score": crit.get("max_score"),
                "final_score": str(score.final_score) if score and score.final_score is not None else None,
                "machine_status": score.machine_status if score else None,
                "review_status": "评审执行失败" if failed else "自动评审未完成",
                "evidence_status": score.evidence_status if score else "none",
                "source_page": crit.get("source_page"), "source_quote": crit.get("source_quote"),
                "blocks_final": True,
            }
            questions.append(question)
            group = grouped.setdefault(key[1], {
                **{field: question[field] for field in (
                    "code", "criterion", "category", "max_score", "kind", "severity", "label", "action", "blocks_final"
                )},
                "bidders": [],
            })
            group["bidders"].append({field: question[field] for field in (
                "bidder", "bidder_id", "score_id", "final_score", "machine_message"
            )})
            if failed:
                group.update(severity="high", kind="execution_failed", label="技术故障")
    questions.sort(key=lambda q: (_SEVERITY_ORDER.get(q["severity"], 9), q["bidder"], q["code"] or ""))
    by_criterion = sorted(grouped.values(), key=lambda g: (_SEVERITY_ORDER.get(g["severity"], 9), g["code"] or ""))
    progress = {
        "total": automation["expected"], "settled": automation["completed"],
        "open": automation["remaining"], "blocking": automation["failed"],
        "blocking_open": automation["remaining"], "criteria_open": len(grouped),
        "by_kind": {kind: sum(q["kind"] == kind for q in questions) for kind in {q["kind"] for q in questions}},
    }
    return questions, progress, by_criterion


def _doc_name(document_id: int | None) -> str | None:
    """细则来源招标文件名（错配护栏标注用）。"""
    if not document_id:
        return None
    try:
        from app.db.models import Document
        from app.db.session import session_scope

        with session_scope() as s:
            doc = s.get(Document, document_id)
            return doc.original_filename if doc else None
    except Exception:  # noqa: BLE001
        return None


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
        automation = automation_progress(run_id, session=s)
        automated = automation is not None
        completed = automatic_completed_keys(run_id) if automated else set()
        frozen_criteria = {
            int(c["id"]): c for c in ((run.config_json or {}).get("automation_manifest") or {}).get("criteria", [])
        }

        ranking = compute_ranking(project_id, run_id)
        ranking_by_bidder = {r["bidder_id"]: r for r in ranking["rows"]}

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
            frozen = frozen_criteria.get(crit.id, {}) if automated else {}
            auto_review = public_auto_review(score) if automated else None
            cell_completed = (bidder.id, crit.id) in completed
            entry = bidders_detail.setdefault(
                bidder.id,
                {"bidder_id": bidder.id, "name": ranking_by_bidder.get(bidder.id, {}).get("bidder_name", bidder.name),
                 "bid_price": ranking_by_bidder.get(bidder.id, {}).get("bid_price"),
                 "status": bidder.status, "items": []},
            )
            entry["items"].append(
                {
                    "code": frozen.get("code", crit.code),
                    "name": frozen.get("name", crit.name),
                    "category": CATEGORY_LABELS.get(frozen.get("category", crit.category), frozen.get("category", crit.category)),
                    "max_score": str(frozen.get("max_score", crit.max_score)) if frozen.get("max_score", crit.max_score) is not None else None,
                    "score_type": frozen.get("score_type", crit.score_type),
                    "is_veto": frozen.get("score_type", crit.score_type) == "veto",
                    "is_verdict": frozen.get("score_type", crit.score_type) in ("veto", "match"),
                    "machine_status": score.machine_status,
                    "verdict": _report_verdict(score.machine_status, automated, _is_lightweight(score)),
                    "reasoning": _report_reasoning(score, automated) if automated or score.ai_revealed_at is not None or score.review_status in SETTLED_STATUSES else None,
                    "auto_review": auto_review,
                    "auto_completed": cell_completed,
                    "final_score": str(score.final_score) if score.final_score is not None and (not automated or cell_completed) else None,
                    # 防锚定是服务端强制（见 api/scores.py）：未解锁前不得暴露 AI 建议分。
                    # 报告提前到复核前可生成后，这里若无条件输出就成了旁路——
                    # 评审人做独立判断前先看到 AI 建议，独立性即被破坏。
                    "ai_suggested_score": str(score.ai_suggested_score)
                    if (score.ai_suggested_score is not None and score.ai_revealed_at is not None)
                    else None,
                    "ai_revealed": score.ai_revealed_at is not None,
                    "review_status": ("自动评审完成" if cell_completed else
                                      "评审执行失败" if (auto_review or {}).get("state") == "failed" else
                                      "自动评审未完成") if automated else REVIEW_LABELS.get(score.review_status, score.review_status),
                    "evaluator_actor": score.evaluator_actor,
                    "confirm_time": score.confirm_time.isoformat() if score.confirm_time else None,
                    "evidence_status": score.evidence_status,
                    "source_quote": frozen.get("source_quote") if automated else crit.source_quote,
                    "source_page": frozen.get("source_page") if automated else crit.source_page,
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
        if automation is not None:
            open_questions, progress, by_criterion = _build_automatic_questions(run, rows, automation, completed)
        # 报告状态：**评分侧**疑问（count/subjective/formula/fallback/evidence 等）
        # 清零才定稿；veto/match 类疑问按 2026-11-04「资格审查弱化」决策降为提示
        # 不阻塞（仍列疑问清单，人工必须能看到）。草稿可生成但必须自带醒目声明。
        # 另有一条硬约束：评分运行未成功时绝不能出定稿——否则"0 条记录"会让
        # progress 归零，报告宣称"全部 0 项均已人工处置"，把失败读成完成。
        run_ok = (automation["is_final"] if automation is not None
                  else run.status == "succeeded" and bool(rows))
        report_status = ("partial" if automated and run.status == "partial" else
                         "final" if (progress["blocking_open"] == 0 and run_ok) else "draft")
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
            "status_label": ("报告定稿" if report_status == "final" else
                             "评审已结束（含故障跳过项）" if report_status == "partial" else
                             "自动评审未完成" if automation is not None else "报告草稿（待复核）"),
            "progress": progress,
            "automatic_method": (
                QUALIFICATION_LIGHT_METHOD if (run.config_json or {}).get("qualification_mode") == "light"
                else AUTOMATIC_REVIEW_METHOD
            ) if automated else None,
            "qualification_mode": (run.config_json or {}).get("qualification_mode", "full") if automated else None,
            "open_questions": open_questions,
            "fallback_scores": _fallback_items(rows),
            "open_by_criterion": by_criterion,
            # 围串标条款由独立智能体负责：本系统不评审、不评分、不否决。
            # 这里只列示留空范围与依据，避免报告出现"漏评"的误解。
            "out_of_scope": (run.config_json or {}).get("excluded_criteria") or [],
            "run": {
                "id": run.id, "status": run.status,
                "automation": automation,
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
                # 细则来源标注（2026-10-27 错配护栏）：评分依据的招标文件名——
                # GYUU 类事故（投标文件与招标文件不是同一项目）在此一眼可见
                "source_document_name": (
                    _doc_name(pkg.source_document_id) if pkg and pkg.source_document_id else None
                ),
                "origin": pkg.origin if pkg else None,
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
    "unverifiable": "现有材料无法证实",
    "execution_failed": "评审执行失败",
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
    零分/扣分依据）→ 附表（资格审查/符合性/分项得分/价格/AI 评审明细）。
    自动运行使用冻结输入并说明实际评审方法，历史运行保留原有辅助评审口径。
    """
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
        automation = automation_progress(run_id, session=s)
        automated = automation is not None
        if automation is not None and not automation["is_final"]:
            message = (f"自动评审尚有 {automation['remaining']} 项未完成，不能生成正式报告"
                       if automation["remaining"] else "自动评审运行尚未成功结束，不能生成正式报告")
            raise ValidationFailed(message)
        if run.status != "succeeded":
            raise NotFound(f"评分运行 #{run_id} 状态为 {run.status}，未成功不能出正式报告")
        pkg = s.get(RulePackage, run.package_id)

        bidders = s.execute(select(Bidder).where(Bidder.project_id == project_id).order_by(Bidder.id)).scalars().all()
        if automation is not None:
            selected = set((run.config_json or {}).get("bidder_ids", []))
            bidders = [b for b in bidders if b.id in selected]
        run_config = run.config_json or {}
        resolved_prices = {
            str(b["id"]): b.get("bid_price")
            for b in (run_config.get("automation_manifest") or {}).get("bidders", [])
        }
        resolved_prices.update(run_config.get("resolved_prices", {}))
        effective_prices = {
            b.id: Decimal(str(resolved_prices[str(b.id)]))
            if resolved_prices.get(str(b.id)) is not None else b.bid_price
            if automation is None else None for b in bidders
        }
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

        if automated:
            manifest = run_config.get("automation_manifest") or {}
            live_bidders = {b.id: b for b in bidders}
            bidders = [SimpleNamespace(
                id=int(b["id"]), name=b.get("name"),
                bid_price=effective_prices[int(b["id"])],
                status=live_bidders[int(b["id"])].status,
            ) for b in manifest.get("bidders", [])]
            criteria = [SimpleNamespace(**{
                "description": None, "source_quote": None, "source_page": None,
                "max_score": None, **c, "id": int(c["id"]),
            }) for c in manifest.get("criteria", [])]
            frozen_by_id = {c.id: c for c in criteria}
            bidder_by_id = {b.id: b for b in bidders}
            score_rows = [(score, frozen_by_id[crit.id], bidder_by_id[bidder.id])
                          for score, crit, bidder in score_rows
                          if crit.id in frozen_by_id and bidder.id in bidder_by_id]

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
    # hierarchical 模式下的明细是**证据簇**而非独立评委：必须区分口径，
    # 否则报告会宣称"3 个 AI 评委去尾平均"，而实际聚合是跨簇加权/子项平均。
    cluster_mode = False
    for score, crit, _bidder in score_rows:
        if crit.score_type != "subjective":
            continue
        raw = score.ai_raw_json or {}
        if isinstance(raw, dict) and raw.get("committee_semantic") == "evidence_cluster":
            cluster_mode = True
            break
    show_judges = committee_size >= 2
    judge_labels = [f"{JUDGE_LABEL}{i}" for i in range(1, committee_size + 1)] if show_judges else []
    if cluster_mode and show_judges:
        judge_labels = [f"证据簇{i}" for i in range(1, committee_size + 1)]

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
            "bid_price": str(effective_prices[bidder.id]) if effective_prices[bidder.id] is not None else None,
            "price_no_tax": str(_no_tax(effective_prices[bidder.id])) if not automated and effective_prices[bidder.id] is not None else None,
            "judge_totals": per_judge_totals,
            "final": str(rk.get("total")) if rk.get("total") is not None else None,
            "rank": rk.get("rank"),
            "veto_flag": rk.get("veto_flag", False),
            "has_noncompliance": automated and any(
                entry[0].machine_status == "fail" and not _is_lightweight(entry[0])
                for crit in criteria if crit.score_type in ("veto", "match")
                and (entry := score_by_key.get((bidder.id, crit.id))) is not None
            ),
            "has_external_boundary": automated and any(
                entry[0].machine_status == "unverifiable" and not _is_lightweight(entry[0])
                for crit in criteria if crit.score_type in ("veto", "match")
                and (entry := score_by_key.get((bidder.id, crit.id))) is not None
            ),
            "has_precheck_alert": automated and any(
                _is_lightweight(entry[0])
                and entry[0].machine_status in ("fail", "concern", "not_deeply_reviewed")
                for crit in criteria if crit.score_type in ("veto", "match")
                and (entry := score_by_key.get((bidder.id, crit.id))) is not None
            ),
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
            is_reject = score.machine_status in (("fail", "unverifiable") if automated else ("recommend_reject", "conflict_flagged"))
            if not (is_zero or is_reject):
                continue
            reason = _report_reasoning(score, automated)
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
                "verdict": _report_verdict(score.machine_status, automated, _is_lightweight(score)),
                "reason": reason[:500],
            })
        if items:
            review_notes.append({"bidder": bidder.name, "items": items})

    # ---- 兜底分清单（R3-5，2026-11-03）：兜底/无事实 0 分，非评审结论 ----
    fallback_scores = []
    for bidder in bidders:
        for crit in criteria:
            entry = score_by_key.get((bidder.id, crit.id))
            if entry is None:
                continue
            item = _fallback_row(entry[0], crit, bidder)
            if item is not None:
                fallback_scores.append(item)

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
                reason = _report_reasoning(score, automated)
                if not reason:
                    for w in score.warnings_json or []:
                        if isinstance(w, dict) and w.get("message"):
                            reason = str(w["message"])
                            break
                cells.append({
                    "verdict": _report_verdict(score.machine_status, automated, _is_lightweight(score)),
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
                "max_score": str(crit.max_score) if crit.max_score is not None else None,
                "method": _report_method(crit.score_type, automated, _is_lightweight(score)),
                "per_bidder": per_bidder,
            })
        score_groups.append({"category": label, "items": items})

    # ---- 价格评审表 ----
    price_rows = []
    for bidder in bidders:
        rk = rank_by_bidder.get(bidder.id, {})
        price_rows.append({
            "bidder": bidder.name,
            "bid_price": str(effective_prices[bidder.id]) if effective_prices[bidder.id] is not None else None,
            "price_no_tax": str(_no_tax(effective_prices[bidder.id])) if not automated and effective_prices[bidder.id] is not None else None,
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
                "max_score": str(crit.max_score) if crit.max_score is not None else None,
                "method": _report_method(crit.score_type, automated, _is_lightweight(score)),
                "judges": [
                    {
                        "judge": f"{'证据簇' if cluster_mode else JUDGE_LABEL}{m.get('judge_no')}",
                        "score": str(m.get("score")) if m.get("score") is not None else "未给出",
                        "dropped": bool(m.get("dropped")),
                    }
                    for m in committee
                ],
                "final": str(score.final_score) if score.final_score is not None else None,
                "reasoning": _report_reasoning(score, automated)[:600],
                "citations": [
                    {"quote": c.quote[:160], "page_no": c.page_no, "verified": c.verified}
                    for c in citations_by_score.get(score.id, [])[:3]
                ],
            })
        if items:
            ai_detail.append({"bidder": bidder.name, "items": items})

    total_citations = len(citations)
    verified_citations = sum(1 for c in citations if c.verified)
    if automated:
        declaration_text = "本报告由全自动评标系统生成。" + AUTOMATIC_REVIEW_METHOD
    else:
        declaration_text = (
            "本报告由 AI 辅助评审系统自动生成："
            f"按 {committee_size} 个证据簇分层评审主观评分项"
            "（逐簇评分后按子项跨簇平均/证据相关性加权聚合）"
            if cluster_mode and committee_size >= 2
            else (
                "本报告由 AI 辅助评审系统自动生成："
                f"{committee_size} 个 AI 评审 Agent 对主观评分项独立评分，"
                "去除一个最高分和一个最低分后取平均作为该项最终得分"
                if committee_size >= 2
                else "本报告由 AI 辅助评审系统自动生成：AI 评审 Agent 对主观评分项"
                "依据招标文件载明的判据独立评定分值"
            )
        ) + (
            "；否决项、计数项与价格分由确定性引擎计算。本报告供评标委员会参考，"
            "所有存疑事项、否决建议与最终结论须经评标委员会确认。"
        )
    declaration_text += (f"规则包版本 v{run.package_version}（内容哈希 {run.package_content_hash}）；"
                         f"引文核验 {verified_citations}/{total_citations}。")

    return {
        "report_title": "评审报告（全自动评审）" if automated else "评审报告（AI 辅助评审）",
        "method_note": AUTOMATIC_REVIEW_METHOD if automated else None,
        "score_table_title": "分项得分表（自动评审结果）" if automated else
                             "分项得分表（证据簇明细）" if cluster_mode else "分项得分表（AI 评委矩阵）",
        "final_score_label": "最终得分" if automated else "最终得分(去尾)",
        "review_notes_title": "评审情况说明（零分、不符合与外部事实证据边界）" if automated else "评审情况说明（零分/否决依据）",
        "no_review_notes": "无零分、不符合或外部事实无法证实事项。" if automated else "无零分或否决建议事项。",
        "project": {
            "id": project.id, "name": project.name, "purchaser": project.purchaser,
            "code": project.project_code,
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run": {
            "id": run.id, "package_version": run.package_version,
            "automation": automation,
            "package_content_hash": run.package_content_hash,
            "llm_model": run.llm_model,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        },
        "committee_size": committee_size,
        "committee_semantic": "evidence_cluster" if cluster_mode else "judge",
        "judge_labels": judge_labels,
        "summary_rows": summary_rows,
        "review_notes": review_notes,
        "fallback_scores": fallback_scores,
        "qualification_table": qualification_table,
        "compliance_table": compliance_table,
        "score_groups": score_groups,
        "price_rows": price_rows,
        "tax_rate": None if automated else str(TAX_RATE),
        "tax_note": "未提供可核验税率，不推算不含税报价。" if automated else
                    f"不含税价按 {TAX_RATE} 增值税率换算（不含税=含税/1.06）。",
        "ai_detail": ai_detail,
        "declaration": {
            "ai_assisted": not automated,
            "automated": automated,
            "text": declaration_text,
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
    automated = report["run"].get("automation") is not None
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
    run = title.add_run(report["project"]["name"] + "\n" + report["report_title"])
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
            + [r["final"] or "-", str(r["rank"] or "-") + (
                "（存在不符合结论）" if automated and r.get("has_noncompliance") else
                "（外部事实无法证实）" if automated and r.get("has_external_boundary") else
                "（有否决建议）" if not automated and r["veto_flag"] else ""
            )]
        )
    table(headers, rows)
    heading("三、" + report["review_notes_title"], 1)
    if not report["review_notes"]:
        para(report["no_review_notes"])
    for note in report["review_notes"]:
        para(f"{note['bidder']}：")
        for it in note["items"]:
            para(f"　· {it['criterion']}（{it['category']}）：{it['verdict']}"
                 + (f"，得 {it['final']} 分" if it["final"] is not None else "")
                 + f"。依据：{it['reason']}")
    if report.get("fallback_scores"):
        para("以下记录含故障兜底标记，不构成有效最终分；恢复后继续自动评审。" if automated else
             "兜底分清单（非评审结论）：以下格为全自动模式最低档兜底或机器未抽到事实的 "
             "0 分，已按零未定分政策计入总分，请人工核看原件后改分或重跑：")
        for f in report["fallback_scores"]:
            para(f"　· {f['bidder']}：{f['criterion']}（当前 {f['final_score']} 分，"
                 f"类型 {f['kind']}）")
    heading("四、评标结论", 1)
    ordered = [r for r in report["summary_rows"] if r["rank"]]
    ordered.sort(key=lambda r: r["rank"])
    if ordered:
        para("按最终得分排序，推荐中标候选人如下：")
        for i, r in enumerate(ordered[:2], start=1):
            para(f"第{i}中标候选人：{r['bidder']}（最终得分 {r['final']}）")
        para("以上为系统完成全部评审后的评分排序；资格与符合性风险另见审查表。"
             if report["run"].get("automation") is not None else
             "以上排序为 AI 辅助评审结果，须评标委员会确认后生效。")
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
    heading("附表 A3 " + report["score_table_title"], 1)
    for group in report["score_groups"]:
        heading(group["category"], 2)
        for item in group["items"]:
            heading(f"{item['code']} {item['name']}（满分 {item['max_score']}）", 3)
            if automated:
                para(f"评审方法：{item['method']}")
            if item["factor"]:
                para(f"评审因素（原文）：{item['factor'][:400]}")
            table(
                ["投标人"] + report["judge_labels"] + [report["final_score_label"]],
                [[p["bidder"]] + p["judge_scores"] + [p["final"]] for p in item["per_bidder"]],
            )
    # A4 价格评审
    heading("附表 A4 价格评审表", 1)
    table(
        ["投标人", "投标报价(含税)", "投标报价(不含税)", "价格得分", "计算说明"],
        [[p["bidder"], p["bid_price"] or "-", p["price_no_tax"] or "-", p["score"],
          "；".join(p["explanations"])[:200] or "-"] for p in report["price_rows"]],
    )
    para("注：" + report["tax_note"])
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
            if it["judges"]:
                label = "证据簇得分" if report.get("committee_semantic") == "evidence_cluster" else "评委分"
                para(f"{label}：{marks}")
            if it["reasoning"]:
                para(f"评审依据：{it['reasoning']}")
            for c in it["citations"]:
                para(f"　引文（p{c['page_no']}{'✓' if c['verified'] else '未核验'}）：{c['quote']}")

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()
