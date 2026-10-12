"""评分运行编排：客观 → 价格 → 主观 → 汇总。

前置（422 拦截）：规则包已冻结；所选投标人报价全部已确认（卡点 ③）。
评分只认冻结快照（snapshot_json），与业务表解耦。
final_score 写入权限：确定性引擎（价格分/count）、人工确认、人工改分——本模块
只行使第一种权限；主观分永远只写 ai_* 字段。
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import (
    Bidder,
    EvidenceCitation,
    EvaluationRun,
    RulePackage,
    ScoreRecord,
    utcnow,
)
from app.db.session import session_scope
from app.engine.facts import extract_bid_facts
from app.engine.models import PriceBid
from app.engine.objective import run_check, score_count, verdict_for
from app.engine.price import compute_price_scores
from app.engine.subjective import suggest_score
from app.errors import NotFound, ValidationFailed
from app.jobs.handlers import register
from app.jobs.queue import JobContext
from app.services import bidder_service

log = logging.getLogger("bid_eval.scoring")


# ---------------------------------------------------------------------------
# 发起
# ---------------------------------------------------------------------------
def create_run(
    project_id: str,
    package_id: int,
    bidder_ids: list[int],
    stages: list[str],
    actor: str,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[int, int]:
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is None or pkg.project_id != project_id:
            raise NotFound("规则包不存在")
        if pkg.status != "frozen":
            raise ValidationFailed("规则包尚未冻结，不能发起评分（先完成规则确认与冻结）")
        if not bidder_ids:
            raise ValidationFailed("未选择投标人")
        unconfirmed = []
        for bid_id in bidder_ids:
            bidder = s.get(Bidder, bid_id)
            if bidder is None or bidder.project_id != project_id:
                raise NotFound(f"投标人 {bid_id} 不存在")
            if bidder.price_status != "confirmed":
                unconfirmed.append(bidder.name)
        if unconfirmed:
            raise ValidationFailed(
                f"以下投标人报价尚未人工确认：{'、'.join(unconfirmed)}（价格分输入必须经人工确认）"
            )
        run = EvaluationRun(
            project_id=project_id,
            package_id=package_id,
            package_version=pkg.version,
            package_content_hash=pkg.content_hash,
            status="queued",
            stages_json=stages or ["objective", "price", "subjective"],
            llm_provider=provider,
            config_json={"bidder_ids": bidder_ids},
        )
        s.add(run)
        s.flush()
        run_id = run.id

    from app.jobs.queue import submit

    job_id = submit(
        "evaluate",
        {"run_id": run_id, "provider": provider, "model": model, "actor": actor},
        project_id=project_id,
    )
    return run_id, job_id


@register("evaluate")
def evaluate_job(ctx: JobContext, payload: dict) -> dict:
    # run_id 解析与 ctx.start 都放进 try：create_run 是先提交 status='queued' 的运行
    # 再 submit 任务，任何在 execute_run 之前抛出的异常（payload 缺键、ctx 写库失败）
    # 都会让运行永久停在 queued——既不是 running 也不是终态，谁也回收不了。
    run_id: int | None = None
    try:
        run_id = int(payload["run_id"])
        provider = payload.get("provider")
        model = payload.get("model")
        ctx.start(total=4, message="加载规则快照")
        summary = execute_run(run_id, provider=provider, model=model, ctx=ctx)
    except Exception as exc:
        # 运行状态必须与任务结局一致：失败不回写会留下"永远运行中/永远排队"的僵尸运行，
        # 工作台与报告都取最新运行，于是整个项目卡死再也出不来。
        if run_id is not None:
            with session_scope() as s:
                run = s.get(EvaluationRun, run_id)
                if run is not None and run.status in ("queued", "running"):
                    run.status = "failed"
                    run.finished_at = utcnow()
                    run.config_json = {
                        **(run.config_json or {}),
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                    s.add(run)
        raise
    ctx.advance(message="完成")
    return summary


def reconcile_stale_runs(project_id: str) -> int:
    """把"已无对应任务"的非终态运行标记为失败，返回修复条数。

    与 jobs 的僵尸回收同一思路：进程被杀、任务异常未回写等情况下，运行会永久停在
    queued/running，挡住工作台与报告。**必须同时扫 queued**——create_run 先落库
    queued 再 submit，submit 失败或任务在进入 execute_run 前就抛错时，
    运行停在 queued，只扫 running 会把它漏掉。
    """
    from app.db.models import Job

    fixed = 0
    with session_scope() as s:
        runs = s.execute(
            select(EvaluationRun).where(
                EvaluationRun.project_id == project_id,
                EvaluationRun.status.in_(("queued", "running")),
            )
        ).scalars().all()
        for run in runs:
            alive = s.execute(
                select(Job.id).where(
                    Job.kind == "evaluate",
                    Job.status.in_(("queued", "running")),
                    Job.payload_json["run_id"].as_integer() == run.id,
                ).limit(1)
            ).first()
            if alive is None:
                run.status = "failed"
                run.finished_at = utcnow()
                run.config_json = {
                    **(run.config_json or {}),
                    "error": "运行中断（对应任务已结束或进程重启），已自动标记为失败",
                }
                s.add(run)
                fixed += 1
    return fixed


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def _snapshot_criteria(package_id: int) -> tuple[dict, list[dict]]:
    from app.services.rule_service import load_criteria_for_snapshot

    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is None:
            raise NotFound("规则包不存在")
        criteria = load_criteria_for_snapshot(s, package_id)
        snapshot = pkg.snapshot_json
    if snapshot:
        return snapshot, snapshot.get("criteria", criteria)
    # 冻结但无快照（异常防御）：现场构建
    return {"criteria": criteria}, criteria


def execute_run(
    run_id: int, provider: str | None = None, model: str | None = None, ctx: JobContext | None = None
) -> dict:
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("评分运行不存在")
        run.status = "running"
        run.started_at = utcnow()
        run.llm_provider = provider or run.llm_provider or "mock"
        if model:
            run.llm_model = model
        s.add(run)
        project_id = run.project_id
        bidder_ids = list((run.config_json or {}).get("bidder_ids") or [])
        stages = list(run.stages_json or ["objective", "price", "subjective"])

    from app.llm.gateway import get_gateway
    from app.llm.registry import get_provider

    # provider/model 都用默认配置时走进程级单例（共享缓存与预算）；
    # 任一被显式指定（前端选了模型）则构造临时网关。
    if provider is None and model is None:
        gateway = get_gateway()
    else:
        from app.llm.gateway import Gateway

        gateway = Gateway(get_provider(provider, model))

    snapshot, criteria = _snapshot_criteria(run.package_id)
    # 把规则版本绑定注入每个评分项（score_records 落库用）
    for c in criteria:
        c["package_id"] = run.package_id
        c["rule_version"] = run.package_version
        c["package_content_hash"] = run.package_content_hash
    veto_items = [c for c in criteria if c["score_type"] == "veto"]
    count_items = [c for c in criteria if c["score_type"] == "count"]
    price_items = [c for c in criteria if c["score_type"] == "formula"]
    subjective_items = [c for c in criteria if c["score_type"] == "subjective"]

    # ---- 事实与文本 ----
    facts_by_bidder: dict[int, object] = {}
    text_by_bidder: dict[int, str] = {}
    main_doc_by_bidder: dict[int, int | None] = {}
    data_class = "synthetic"
    for bid_id in bidder_ids:
        text, _titles, doc_ids = bidder_service.bid_text(bid_id)
        text_by_bidder[bid_id] = text
        facts_by_bidder[bid_id] = extract_bid_facts(text)
        main_doc_by_bidder[bid_id] = doc_ids[0] if doc_ids else None
        with session_scope() as s:
            from app.db.models import Document

            if doc_ids:
                # 数据分级必须覆盖**全部**送审文本：任何一份涉密文件都足以
                # 禁止出域——只查第一份会让涉密附件漏网。
                confidential = s.execute(
                    select(Document.id).where(
                        Document.id.in_(doc_ids), Document.data_class == "confidential"
                    ).limit(1)
                ).first()
                if confidential is not None:
                    data_class = "confidential"

    if ctx:
        ctx.advance(message=f"客观审查 {len(veto_items)} 项")

    # ---- ① 资格/符合性（三态）----
    stats = {"objective": 0, "price": 0, "subjective": 0, "need_human": 0, "citations_verified": 0,
             "citations_total": 0}
    excluded_bidders: set[int] = set()  # 客观审查未通过 → 不进基准价（exclude_invalid）
    for bid_id in bidder_ids:
        facts = facts_by_bidder[bid_id]
        for c in veto_items:
            result = _eval_veto(c, facts)  # type: ignore[arg-type]
            _upsert_score(
                run_id, bid_id, c,
                machine_status=result["verdict"],
                review_status=_review_status_for_verdict(result),
                warnings_json=[{"message": result.get("message")}] if result.get("message") else None,
            )
            if result["verdict"] == "recommend_reject":
                excluded_bidders.add(bid_id)
            stats["objective"] += 1

    # ---- ② 价格分 ----
    if price_items and "price" in stages:
        if ctx:
            ctx.advance(message="计算价格分")
        price_criterion = price_items[0]
        formula = dict(price_criterion.get("formula") or {})
        formula["max_score"] = price_criterion.get("max_score")
        bids: list[PriceBid] = []
        with session_scope() as s:
            for bid_id in bidder_ids:
                bidder = s.get(Bidder, bid_id)
                excluded = (
                    bid_id in excluded_bidders
                    or bidder.status in ("recommend_reject", "rejected")
                )
                bids.append(PriceBid(bid_id, bidder.name, Decimal(str(bidder.bid_price)), excluded))
        comp = compute_price_scores(bids, formula)
        for r in comp.results:
            _upsert_score(
                run_id, r.bidder_id, price_criterion,
                machine_score=r.price_score,
                machine_status="pass",
                review_status="pending",
                final_score=r.price_score,  # 确定性引擎有权直接写 final
                warnings_json=[{"formula_explanation": r.explanation}],
            )
            stats["price"] += 1

    # ---- ③ 商务 count 分 ----
    for bid_id in bidder_ids:
        facts = facts_by_bidder[bid_id]
        for c in count_items:
            params = c.get("params") or {}
            result = score_count(c, params, facts)  # type: ignore[arg-type]
            # unresolved 以结构化键落库：下游（报告疑问清单）直接判读，
            # 不再靠匹配 detail 的中文措辞——那种耦合已经断过一次。
            _upsert_score(
                run_id, bid_id, c,
                machine_score=result.score,
                machine_status="pass",
                review_status="conflict_flagged" if result.conflict else "pending",
                final_score=result.score,
                warnings_json=([{"message": result.detail, "unresolved": result.unresolved}]
                               if result.unresolved else [{"message": result.detail}]),
            )
            stats["objective"] += 1

    # ---- ④ 主观分建议 ----
    if "subjective" in stages and subjective_items:
        if ctx:
            ctx.advance(message=f"主观分建议 {len(subjective_items)} 项 × {len(bidder_ids)} 家")
        for bid_id in bidder_ids:
            for c in subjective_items:
                suggestion = suggest_score(
                    gateway, c, text_by_bidder[bid_id], data_class=data_class
                )
                _persist_subjective(run_id, bid_id, c, suggestion, main_doc_by_bidder.get(bid_id), stats)

    # ---- ⑤ 收尾 ----
    need_human = 0
    with session_scope() as s:
        need_human = s.execute(
            select(func.count(ScoreRecord.id)).where(
                ScoreRecord.run_id == run_id,
                ScoreRecord.review_status.in_(("pending", "ai_suggested", "conflict_flagged")),
            )
        ).scalar_one()
        run = s.get(EvaluationRun, run_id)
        run.status = "succeeded"
        run.finished_at = utcnow()
        s.add(run)

    return {
        "run_id": run_id,
        "objective": stats["objective"],
        "price": stats["price"],
        "subjective": stats["subjective"],
        "need_human": int(need_human),
        "citations_total": stats["citations_total"],
        "citations_verified": stats["citations_verified"],
    }


def _eval_veto(c: dict, facts) -> dict:  # noqa: ANN001
    check_type = c.get("check_type")
    params = c.get("params") or {}
    if not check_type:
        return {"verdict": "need_human", "message": "该条款缺少可执行的检查定义"}
    result = run_check(check_type, params, facts)
    verdict = verdict_for(c["code"], c["name"], result, on_fail="recommend_reject")
    return {"verdict": verdict, "message": result.message, "evidence": result.evidence_quote}


def _review_status_for_verdict(result: dict) -> str:
    v = result["verdict"]
    if v == "pass":
        return "pending"  # 客观通过项仍走人工复核流（防错，但默认可批量浏览）
    if v == "recommend_reject":
        return "conflict_flagged"  # 建议否决必须人工确认
    if v == "conflict_flagged":
        return "conflict_flagged"
    return "pending"


def _upsert_score(
    run_id: int,
    bidder_id: int,
    criterion: dict,
    *,
    machine_score: Decimal | None = None,
    machine_status: str | None = None,
    review_status: str = "pending",
    final_score: Decimal | None = None,
    warnings_json: list | None = None,
) -> ScoreRecord:
    with session_scope() as s:
        row = s.execute(
            select(ScoreRecord).where(
                ScoreRecord.run_id == run_id,
                ScoreRecord.bidder_id == bidder_id,
                ScoreRecord.criterion_id == _criterion_pk(s, criterion),
            )
        ).scalar_one_or_none()
        if row is None:
            row = ScoreRecord(
                run_id=run_id,
                bidder_id=bidder_id,
                criterion_id=_criterion_pk(s, criterion),
            )
        row.rule_version = int((criterion.get("rule_version") or 1))
        row.package_content_hash = criterion.get("package_content_hash")
        row.max_score = criterion.get("max_score")
        row.score_type = criterion.get("score_type")
        row.veto_flag = bool(criterion.get("is_veto"))
        if machine_score is not None:
            row.machine_score = machine_score
        if machine_status is not None:
            row.machine_status = machine_status
        # 重跑保护：人工已确认/已改分的记录不得被机器结果回退
        # （重跑是异常恢复路径，不是推翻人工结论的入口；要重新评请新建 run）。
        if row.review_status in ("human_confirmed", "human_overridden"):
            s.add(row)
            s.flush()
            s.expunge(row)
            return row
        row.review_status = review_status
        if final_score is not None:
            row.final_score = final_score
        elif row.review_status not in ("human_confirmed", "human_overridden") and machine_status is not None:
            row.final_score = None  # 机器结果变化时清掉旧 final（人工结论除外）
        if warnings_json:
            row.warnings_json = warnings_json
        s.add(row)
        s.flush()
        s.expunge(row)
        return row


def _criterion_pk(s, criterion: dict) -> int:  # noqa: ANN001
    """快照条目 → DB 主键（按 code 定位当前包的评分项）。"""
    from app.db.models import Criterion

    row = s.execute(
        select(Criterion.id).where(
            Criterion.package_id == criterion["package_id"], Criterion.code == criterion["code"]
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"评分项不存在：{criterion['code']}")
    return row


def _persist_subjective(
    run_id: int,
    bidder_id: int,
    criterion: dict,
    suggestion,  # SubjectiveSuggestion
    main_doc_id: int | None,
    stats: dict,
) -> None:
    # 人工已确认/改分的主观项：重跑时不得被新的 AI 建议覆盖
    with session_scope() as s:
        existing = s.execute(
            select(ScoreRecord).where(
                ScoreRecord.run_id == run_id,
                ScoreRecord.bidder_id == bidder_id,
                ScoreRecord.criterion_id == _criterion_pk(s, criterion),
            )
        ).scalar_one_or_none()
        if existing is not None and existing.review_status in ("human_confirmed", "human_overridden"):
            return

    row = _upsert_score(
        run_id, bidder_id, criterion,
        machine_status="need_human" if suggestion.status == "need_human" else None,
        review_status="pending",
    )
    with session_scope() as s:
        row = s.get(ScoreRecord, row.id)
        row.ai_suggested_score = suggestion.score
        row.ai_level_label = suggestion.level_label
        row.ai_reasoning = suggestion.reasoning
        row.ai_confidence = suggestion.confidence
        row.ai_raw_json = {
            "warnings": suggestion.warnings,
            "evidence": suggestion.evidence,
        }
        row.warnings_json = [{"message": w} for w in suggestion.warnings]
        row.review_status = "ai_suggested" if suggestion.status == "ai_suggested" else "pending"
        s.add(row)

    # 证据引用 + 核验
    evidence_status = "none"
    verified_any = False
    found_any = False
    for ev in suggestion.evidence[:5]:
        quote = str(ev.get("quote") or "")
        if len(quote.strip()) < 8:
            continue
        from app.evidence.verifier import create_citation

        cit_id = create_citation(
            main_doc_id,
            quote,
            page_hint=ev.get("page_hint"),
            role="score",
            score_record_id=row.id,
            created_by="llm",
        )
        stats["citations_total"] += 1
        with session_scope() as s:
            cit = s.get(EvidenceCitation, cit_id)
            if cit.verified:
                verified_any = True
            if cit.verify_method is not None:
                found_any = True
    if verified_any:
        evidence_status = "verified"
    elif found_any:
        evidence_status = "unverified"
    stats["citations_verified"] += int(verified_any)

    with session_scope() as s:
        row = s.get(ScoreRecord, row.id)
        row.evidence_status = evidence_status
        if suggestion.status == "ai_suggested" and evidence_status != "verified":
            # 无已核验证据 → 建议仍记录，但降级为待人工
            row.review_status = "pending"
            row.warnings_json = (row.warnings_json or []) + [
                {"message": "证据未能核验，需人工核查原文"}
            ]
            s.add(row)
        elif suggestion.status == "ai_suggested":
            s.add(row)
