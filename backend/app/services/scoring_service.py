"""评分运行编排：客观 → 价格 → 主观 → 汇总。

前置（422 拦截）：规则包已冻结；所选投标人报价全部已确认（卡点 ③）。
评分只认冻结快照（snapshot_json），与业务表解耦。
final_score 写入权限：确定性引擎（价格分/count）、人工确认、人工改分——本模块
只行使第一种权限；主观分永远只写 ai_* 字段。
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from sqlalchemy import func, select

from app import config
from app.db.models import (
    Bidder,
    EvidenceCitation,
    EvaluationRun,
    ReviewRecord,
    RulePackage,
    ScoreRecord,
    utcnow,
)
from app.db.session import session_scope
from app.engine.facts import extract_bid_facts
from app.engine.models import PriceBid
from app.engine.objective import (
    cert_evidence_present,
    parse_required_artifacts,
    recover_artifacts_from_tender,
    run_check,
    score_count,
    verdict_for,
)
from app.engine.price import compute_price_scores
from app.engine.subjective import (
    SubjectiveSuggestion,
    suggest_score,
    suggest_score_committee,
    suggest_score_hierarchical,
)
from app.errors import NotFound, ValidationFailed
from app.jobs.handlers import register
from app.jobs.queue import JobCancelled, JobContext
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
        unconfirmed: list[str] = []
        dropped_no_price: list[str] = []
        auto = config.auto_mode()
        kept: list[int] = []
        for bid_id in bidder_ids:
            bidder = s.get(Bidder, bid_id)
            if bidder is None or bidder.project_id != project_id:
                raise NotFound(f"投标人 {bid_id} 不存在")
            if bidder.price_status == "confirmed":
                kept.append(bid_id)
                continue
            if not auto:
                unconfirmed.append(bidder.name)
                continue
            # 全自动模式：抽取报价直接生效（报价冲突项由评分内 LLM 裁决并留痕）；
            # 连报价都没抽到的无法评分，自动剔除并留痕，不阻塞整场。
            if bidder.bid_price is None:
                dropped_no_price.append(bidder.name)
                s.add(
                    ReviewRecord(
                        subject_type="price", subject_id=str(bidder.id), action="flag_conflict",
                        actor=actor,
                        reason="全自动模式：未抽取到报价，无法参与评分，已自动剔除",
                        old_value_json={"price_status": bidder.price_status},
                    )
                )
                continue
            bidder.price_status = "confirmed"
            s.add(bidder)
            s.add(
                ReviewRecord(
                    subject_type="price", subject_id=str(bidder.id), action="confirm",
                    actor=actor,
                    reason="全自动模式：抽取报价直接生效（冲突项由评分内 LLM 裁决并留痕）",
                    old_value_json={"price_status": bidder.price_status},
                    new_value_json={"price_status": "confirmed",
                                    "price": str(bidder.bid_price)},
                )
            )
            kept.append(bid_id)
        if unconfirmed:
            raise ValidationFailed(
                f"以下投标人报价尚未人工确认：{'、'.join(unconfirmed)}（价格分输入必须经人工确认）"
            )
        if auto:
            bidder_ids = kept
            if not bidder_ids:
                raise ValidationFailed(
                    f"所选投标人报价均无法自动确认（未抽取到报价：{'、'.join(dropped_no_price)}）"
                )
        run = EvaluationRun(
            project_id=project_id,
            package_id=package_id,
            package_version=pkg.version,
            package_content_hash=pkg.content_hash,
            status="queued",
            stages_json=stages or ["objective", "price", "subjective"],
            llm_provider=provider,
            config_json={
                "bidder_ids": bidder_ids,
                **({"auto_dropped_bidders": dropped_no_price} if auto and dropped_no_price else {}),
            },
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
        summary = execute_run(run_id, provider=provider, model=model, ctx=ctx)
    except JobCancelled:
        # 用户取消：运行置 cancelled（不是 failed——那是另一种结局，语义不能混）
        if run_id is not None:
            with session_scope() as s:
                run = s.get(EvaluationRun, run_id)
                if run is not None and run.status in ("queued", "running"):
                    run.status = "cancelled"
                    run.finished_at = utcnow()
                    run.config_json = {**(run.config_json or {}), "error": "用户取消"}
                    s.add(run)
        raise
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

    任务是**被用户取消**的，运行同步置 cancelled（不是 failed——排队中取消是正常
    操作，标成失败会把用户吓回去重新发起，2026-10-20 实证）。
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
            if alive is not None:
                continue
            cancelled_job = s.execute(
                select(Job.id).where(
                    Job.kind == "evaluate",
                    Job.status == "cancelled",
                    Job.payload_json["run_id"].as_integer() == run.id,
                ).limit(1)
            ).first()
            run.status = "cancelled" if cancelled_job else "failed"
            run.finished_at = utcnow()
            run.config_json = {
                **(run.config_json or {}),
                "error": (
                    "用户取消"
                    if cancelled_job
                    else "运行中断（对应任务已结束或进程重启），已自动标记为失败"
                ),
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
        run.llm_provider = provider or run.llm_provider or config.llm_provider()
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

    # 新运行 = 重新评审：重置参评投标人状态，防止上一次运行的 rejected/scored
    # 泄漏进本次（否则一次否决建议会让此后所有运行的价格基准价/排名永久排除该
    # 投标人——2026-10-14 浙江中烟项目实证：4 家全部因 run1 的状态被挤出基准价）。
    # 人工异议走 review 记录，不在 bidder.status 上跨运行传递。
    with session_scope() as s:
        for bid_row in s.execute(select(Bidder).where(Bidder.id.in_(bidder_ids))).scalars():
            if bid_row.status != "pending":
                bid_row.status = "pending"
                s.add(bid_row)

    veto_items = [c for c in criteria if c["score_type"] == "veto"]
    count_items = [c for c in criteria if c["score_type"] == "count"]
    price_items = [c for c in criteria if c["score_type"] == "formula"]
    subjective_items = [c for c in criteria if c["score_type"] == "subjective"]
    match_items = [c for c in criteria if c["score_type"] == "match"]

    # 进度按「评分项 × 投标人」粒度上报：客观审查是全链最耗时的一段（本地 8B
    # 每条 30-90s、串行），只按阶段上报会让 UI 停在 25% 长达数十分钟
    # （2026-10-20 实证：job 50 在「客观审查 85 项」停 20 分钟，用户以为没开始）。
    # advance 内含取消检查——不逐项上报 = 整个客观阶段无法取消。
    if ctx is not None:
        obj_total = len(veto_items) * len(bidder_ids)
        subj_total = len(subjective_items) * len(bidder_ids) if "subjective" in stages else 0
        price_total = 1 if (price_items and "price" in stages) else 0
        ctx.start(total=obj_total + subj_total + price_total + 1, message="客观审查准备")

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

    # ---- 串通信号预计算（跨投标人视角；事实/判定分离：run_check 只读信号）----
    # 设计见 engine/collusion.py：报价完全相同/等差规律/同文档作者 → 信号而非结论。
    from app.db.models import Document
    from app.engine.collusion import compute_cross_bidder_signals

    with session_scope() as s:
        prices = {
            b.id: (Decimal(str(b.bid_price)) if b.bid_price is not None else None)
            for b in s.execute(select(Bidder).where(Bidder.id.in_(bidder_ids))).scalars()
        }
        authors: dict[int, list[str]] = {}
        for _doc_id, doc_bidder_id, author in s.execute(
            select(Document.id, Document.bidder_id, Document.author).where(
                Document.bidder_id.in_(bidder_ids), Document.author.isnot(None)
            )
        ).all():
            if (author or "").strip():
                authors.setdefault(doc_bidder_id, []).append(author.strip())
    for bid_id, sig in compute_cross_bidder_signals(prices, authors).items():
        if bid_id in facts_by_bidder:
            facts_by_bidder[bid_id].cross_bidder = sig

    # ---- ① 资格/符合性（三态）----
    stats = {"objective": 0, "price": 0, "subjective": 0, "match": 0,
             "need_human": 0, "citations_verified": 0, "citations_total": 0}
    excluded_bidders: set[int] = set()  # 客观审查未通过 → 不进基准价（exclude_invalid）
    obj_done = 0
    for bid_id in bidder_ids:
        facts = facts_by_bidder[bid_id]
        for c in veto_items:
            result = _eval_veto(  # type: ignore[arg-type]
                c, facts,
                text=text_by_bidder.get(bid_id), gateway=gateway, data_class=data_class,
            )
            review_status = _review_status_for_verdict(result)
            _upsert_score(
                run_id, bid_id, c,
                machine_status=result["verdict"],
                review_status=review_status,
                warnings_json=[{"message": result.get("message")}] if result.get("message") else None,
            )
            # 剔除出基准价只认**人工已确认**的否决。未确认的否决建议（含 auto 模式
            # LLM 裁决）照常展示 veto_flag，但不改变有效投标人集合——证照/签章类
            # 材料是扫描件，文本抽取看不见≠不存在（浙江中烟项目实证：4 家全部因此
            # 被误否决剔除，基准价直接归零）。与 aggregate.py 的哲学一致：建议否决
            # 者同样参与排序，供人工交叉验证"如果否决不成立他会排第几"。
            if result["verdict"] == "recommend_reject" and review_status == "human_confirmed":
                excluded_bidders.add(bid_id)
            stats["objective"] += 1
            if ctx is not None:
                obj_done += 1
                ctx.advance(message=f"客观审查 {obj_done} 项")

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
                # 报价缺失/0 不能崩掉整个 run：价格引擎会按"无效报价"记 0 并排除基准
                price_dec = (
                    Decimal(str(bidder.bid_price))
                    if bidder.bid_price is not None
                    else Decimal(0)
                )
                bids.append(PriceBid(bid_id, bidder.name, price_dec, excluded))
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

    # ---- 证明材料页索引（count 逐案例核验与主观评审共用） ----
    _MAT_KEYWORDS = ("发票", "验收", "业主证明", "查询截图", "证书复印件", "承诺书", "营业执照")
    material_hints: dict[int, str] = {}
    bid_pages: dict[int, list[tuple[int, str, str]]] = {}
    with session_scope() as s:
        from app.db.models import DocumentPage

        for bid_id in bidder_ids:
            doc_id = main_doc_by_bidder.get(bid_id)
            if doc_id is None:
                material_hints[bid_id] = "（无）"
                continue
            pages: dict[str, list[int]] = {}
            page_rows: list[tuple[int, str, str]] = []
            for page_no, ptext, psrc in s.execute(
                select(DocumentPage.page_no, DocumentPage.text, DocumentPage.source)
                .where(DocumentPage.document_id == doc_id)
                .order_by(DocumentPage.page_no)
            ):
                page_rows.append((int(page_no), ptext or "", psrc or "text"))
                for kw in _MAT_KEYWORDS:
                    if kw in (ptext or ""):
                        pages.setdefault(kw, []).append(int(page_no))
            bid_pages[bid_id] = page_rows
            hint_parts = [
                f"{kw}：第{'、'.join(str(p) for p in nos[:12])} 页"
                for kw, nos in pages.items() if nos
            ]
            material_hints[bid_id] = "；".join(hint_parts) if hint_parts else "（未见证明材料相关内容）"

    # 每家投标人归一化全文：count 引文核验（回标书定位）与主观分引文闸门共用一份，
    # 只归一化一次（旧行为各处重复 normalize_no_ws，浪费且口径易漂移）。
    bid_norm_by_bidder: dict[int, str] = {
        bid_id: _nnw(text_by_bidder.get(bid_id) or "") for bid_id in bidder_ids
    }

    # 招标文件原文页（归一化）：恢复评分项的【证明材料】要求段
    tender_pages: list[tuple[int, str]] = []
    with session_scope() as s:
        from app.db.models import DocumentPage

        tender_doc = s.execute(
            select(Document.id).where(
                Document.project_id == project_id,
                Document.kind == "tender",
            ).limit(1)
        ).scalar_one_or_none()
        if tender_doc is not None:
            tender_pages = [
                (int(page_no), _nnw(ptext or ""))
                for page_no, ptext in s.execute(
                    select(DocumentPage.page_no, DocumentPage.text)
                    .where(DocumentPage.document_id == tender_doc)
                    .order_by(DocumentPage.page_no)
                )
            ]

    # ---- ③ 商务 count 分（带证明材料要求的走 LLM 逐案例核验） ----
    # 认证"查询截图"证据双通道检测（2026-10-21 重做）：通道 A 文本标题
    # （致成写"查询网址/查询截图："，旧正则只认整行"查询结果截图"且跳过
    # >300 字的页，全部漏检）；通道 B OCR 正文平台指纹（诚道的证书页图片
    # 就是认证平台查询截图；先数只有证书扫描件 → 判无，与真实评审一致）。
    cert_sections: dict[int, tuple[bool, str]] = {}
    for bid_id in bidder_ids:
        rows = bid_pages.get(bid_id) or []
        cert_sections[bid_id] = cert_evidence_present([(src, t) for _p, t, src in rows])

    for bid_id in bidder_ids:
        facts = facts_by_bidder[bid_id]
        for c in count_items:
            params = c.get("params") or {}
            # 材料要求拆成两个变量：清单（供三件套判断）与要求原文（供防作弊比对）。
            # 旧实现把整句 required_evidence 当 artifacts——"不提供或提供不全的
            # 不得分"这类噪声句混进关键词判断，材料门槛被静默加严。
            requirement_text = (
                (c.get("required_evidence") or "").strip()
                or recover_artifacts_from_tender(c, tender_pages)
            )
            artifacts_list = (
                parse_required_artifacts(requirement_text)
                or parse_required_artifacts(c.get("source_quote") or "")
            )
            artifacts = "、".join(artifacts_list) if artifacts_list else requirement_text
            c_scoring = {
                **c,
                "required_artifacts": artifacts,
                "required_artifacts_raw": requirement_text,
            }
            # field 可能缺省（真实编译产物里常由名称推断）——与 score_count 用
            # 同一推断函数，避免两处口径漂移（2026-10-16 实证：params 无 field
            # 导致 cert_section_present 未传递、认证门槛静默失效）
            _field = params.get("field") or ""
            if not _field:
                from app.engine.objective import _infer_count_field

                _field = _infer_count_field(c) or ""
            if _field == "certificates":
                c_scoring = {**c_scoring, "cert_section_present": cert_sections.get(bid_id, (True, ""))[0]}
            # LLM 逐案例核验仅用于**业绩类**（按案例核对合同/发票/验收）。
            # 证书类（认证/资质）不适用：案例核验的 contract/invoice/acceptance
            # 三标记语义与"证书复印件+查询截图"错位，会把有证书的投标人整体清零
            # （2026-10-16 实证：致成 4→0、诚道 3→0、先数 1→0）。证书类走
            # 确定性计数（facts.certificates=表内有复印件的），材料截图缺失
            # 由 doubt 标疑问转人工。
            is_performance = (
                params.get("field") == "performance"
                # 人员级条目天然是"按案例计"（负责人/团队经验），必须走逐案例核验
                or bool(params.get("person_scope"))
                or (
                    params.get("field") != "certificates"
                    and any(k in str(c.get("name") or "") for k in ("业绩", "案例", "经验"))
                )
            )
            case_stats: dict = {}
            if (
                config.auto_mode()
                and gateway is not None
                and artifacts
                and is_performance
            ):
                cases = _llm_verify_cases(
                    gateway, c, requirement_text, artifacts_list,
                    material_hints.get(bid_id),
                    bid_pages.get(bid_id) or [], data_class,
                    bid_norm=bid_norm_by_bidder.get(bid_id),
                    stats_out=case_stats,
                )
                if cases:
                    c_scoring = {**c_scoring, "verified_cases": cases}
            result = score_count(c_scoring, params, facts)  # type: ignore[arg-type]
            # unresolved 以结构化键落库：下游（报告疑问清单）直接判读，
            # 不再靠匹配 detail 的中文措辞——那种耦合已经断过一次。
            # "机器没抽到事实"不是 0 分结论：不进总分（final=None），报告标未定分。
            # conflict（证明材料链缺失/部分缺失）分数照算但保留疑问态。
            unresolved = bool(result.unresolved)
            doubt = result.conflict or unresolved
            # 审计留痕：把逐案例核验结果 + 候选页统计落 ai_raw_json，否则判"照抄/
            # 未在标书定位"后无法复盘是抽取错还是闸门误杀（2026-10-21 实证为空）。
            audit = None
            _vc = c_scoring.get("verified_cases")
            if _vc:
                audit = {
                    "verified_cases": [
                        {
                            k: (str(v)[:160] if isinstance(v, str) else v)
                            for k, v in cs.items()
                            if k in (
                                "name", "person", "role", "quote", "sanitized",
                                "match_tier", "page_hint", "amount_yuan",
                                "contract", "invoice", "acceptance", "duplicate",
                            )
                        }
                        for cs in _vc
                    ],
                    "meta": case_stats,
                }
            _upsert_score(
                run_id, bid_id, c,
                machine_score=result.score,
                machine_status="pass",
                review_status="conflict_flagged" if doubt else "pending",
                final_score=None if unresolved else result.score,
                warnings_json=([{"message": result.detail, "unresolved": result.unresolved}]
                               if result.unresolved else [{"message": result.detail}]),
                doubt=doubt,
                ai_raw_json=audit,
            )
            stats["objective"] += 1

    # ---- ③+ match 判断项：不计分，落三态判定记录 ----
    # 法律包的 8 条形式/响应性检查（score_type=match）：此前被静默跳过。
    # 不传 machine_score/final_score → 两者保持 NULL（判断项不打分）；
    # 非否决 match 的 on_fail=need_human → 绝不产生 recommend_reject。
    for bid_id in bidder_ids:
        facts = facts_by_bidder[bid_id]
        for c in match_items:
            result = _eval_veto(  # type: ignore[arg-type]
                c, facts,
                on_fail="recommend_reject" if c.get("is_veto") else "need_human",
                text=text_by_bidder.get(bid_id), gateway=gateway, data_class=data_class,
            )
            _upsert_score(
                run_id, bid_id, c,
                machine_status=result["verdict"],
                review_status=_review_status_for_verdict(result),
                warnings_json=[{"message": result.get("message")}] if result.get("message") else None,
            )
            stats["match"] += 1

    # ---- ④ 主观分建议（评审委员会：单评委/多评委去尾平均） ----
    if "subjective" in stages and subjective_items:
        # 人员经验项：注入招标原文的证明材料硬门槛（5 年证明/姓名反映/社保）
        for c in subjective_items:
            if any(k in str(c.get("name") or "") for k in ("项目负责人", "团队成员")):
                seg = recover_artifacts_from_tender(c, tender_pages)
                if seg:
                    base = str(c.get("required_evidence") or c.get("source_quote") or "")
                    c["required_evidence"] = base + "\n【证明材料】" + seg[:600]

        # 人员经验的材料存在性统计（2026-10-16 用户授权 AI 按规则裁量）：
        # 「五年及以上经验证明」「近六个月社保」是招标明示的材料门槛。真实评审
        # 对无证明者硬判 0。按正文页计数做确定性门槛（排除目录/要求页）：
        # 5 年经验正文页 <3 → 负责人 0；社保页 <16 → 团队 0。
        # 实证校准（对真实评审 4 家的判别）：社保页 26/32（龙即/诚道，真实团队 6/6）
        # vs 4/13（致成/先数，真实 0/0）——断崖式可分；5 年经验页 14（龙即 4 分）
        # vs 1-2（其余，真实 0-2）。
        import re as _re_mat

        person_stats: dict[int, dict[str, int]] = {}
        with session_scope() as s:
            from app.db.models import DocumentPage

            for bid_id in bidder_ids:
                doc_id = main_doc_by_bidder.get(bid_id)
                five, social = 0, 0
                if doc_id is not None:
                    for ptext in s.execute(
                        select(DocumentPage.text).where(DocumentPage.document_id == doc_id)
                    ).scalars():
                        t = ptext or ""
                        if "【证明材料】" in t or t.count("....") >= 3:
                            continue  # 目录/要求页不计
                        if _re_mat.search(r"五年|5\s*年", t) and "经验" in t:
                            five += 1
                        if _re_mat.search(r"社会保险|社保|参保缴费|参保人员", t):
                            social += 1
                person_stats[bid_id] = {"five_year": five, "social": social}

        use_hierarchical = config.review_mode() == "hierarchical"
        subj_done = 0
        subj_total = len(subjective_items) * len(bidder_ids)
        # 子项引文闸门的核验器：引文必须真实存在于该投标人的文档文本中
        # （空白不敏感；<12 字不算引文）。复用上面预计算的 bid_norm_by_bidder。
        verifiers: dict[int, object] = {}
        for bid_id in bidder_ids:
            norm_text = bid_norm_by_bidder.get(bid_id) or ""

            def _verify(quote: str, _norm=norm_text) -> bool:
                q = _nnw(quote)
                return len(q) >= 12 and q in _norm

            verifiers[bid_id] = _verify
        for bid_id in bidder_ids:
            for c in subjective_items:
                if ctx is not None:
                    subj_done += 1
                    ctx.advance(message=f"主观分评审 {subj_done}/{subj_total}（{config.review_mode()}）")
                hint = material_hints.get(bid_id)
                if use_hierarchical:
                    suggestion, committee = suggest_score_hierarchical(
                        gateway, c, text_by_bidder[bid_id], data_class=data_class,
                        material_pages=hint, verify_quote=verifiers.get(bid_id),
                    )
                    if suggestion is None:
                        # 证据地图不可用（嵌入服务故障）→ 回退单次评审
                        suggestion, committee = suggest_score_committee(
                            gateway, c, text_by_bidder[bid_id], data_class=data_class,
                            material_pages=hint, verify_quote=verifiers.get(bid_id),
                        )
                else:
                    suggestion, committee = suggest_score_committee(
                        gateway, c, text_by_bidder[bid_id], data_class=data_class,
                        material_pages=hint, verify_quote=verifiers.get(bid_id),
                    )

                # 人员经验材料门槛（确定性，规则执行）：缺证明按"否则该项不得分"计 0
                cname = str(c.get("name") or "")
                ps = person_stats.get(bid_id, {})
                forced_zero = None
                if "项目负责人" in cname and ps.get("five_year", 0) < 3:
                    forced_zero = (
                        f"未见五年及以上工作经验证明材料（正文相关页 {ps.get('five_year', 0)} 页），"
                        "按评分因素「无证明材料不得分」计 0"
                    )
                elif "团队成员" in cname and ps.get("social", 0) < 16:
                    forced_zero = (
                        f"未见团队成员社保证明材料（相关页 {ps.get('social', 0)} 页），"
                        "按评分因素「不提供或不能提供有效证明的不得分」计 0"
                    )
                if forced_zero is not None:
                    suggestion = SubjectiveSuggestion(
                        status="ai_suggested",
                        score=Decimal(0),
                        level_label=None,
                        reasoning=forced_zero,
                        evidence=[],
                        warnings=[forced_zero] + list(suggestion.warnings),
                    )

                _persist_subjective(run_id, bid_id, c, suggestion, main_doc_by_bidder.get(bid_id), stats,
                                    committee=committee)

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

    if config.auto_mode():
        # 全自动模式：客观裁决直接落投标人状态（否决/进入评分），无需人工确认
        with session_scope() as s:
            for bid_id in bidder_ids:
                bidder = s.get(Bidder, bid_id)
                if bidder is None:
                    continue
                bidder.status = "rejected" if bid_id in excluded_bidders else "scored"
                s.add(bidder)

    return {
        "run_id": run_id,
        "objective": stats["objective"],
        "price": stats["price"],
        "subjective": stats["subjective"],
        "match": stats["match"],
        "need_human": int(need_human),
        "citations_total": stats["citations_total"],
        "citations_verified": stats["citations_verified"],
    }


_CASE_BATCH_CHARS = 15000  # 单次核验调用的上下文预算
_CASE_MAX_BATCHES = 4  # 多段调用上限（超出按锚点页优先截断并记警告）
_ECHO_NGRAM_W = 12  # 判"要求复述"的 n-gram 窗口（与 MIN_QUOTE_CHARS 同阶）
_ECHO_SHADOW_FRACTION = 0.6  # 引文被判"照抄要求"的覆盖率阈值
_ECHO_PAGE_FRACTION = 0.4  # 整页被判"要求复述页"的覆盖率阈值
_ECHO_PAGE_SHORT_CHARS = 300  # 短页另用"最长连续 echo 段"判活的字符上限
_ECHO_PAGE_MIN_RUN = 60  # 短页最长连续 echo 段阈值


def _nnw(text: str) -> str:
    from app.parsing.normalize import normalize_no_ws

    return normalize_no_ws(text)


def _ngram_fraction(text_n: str, ref_n: str, w: int = _ECHO_NGRAM_W) -> float:
    """`text_n` 里有多大比例的 w-gram 也出现在 `ref_n` 中。

    用于把"引文是不是要求文本的影子"从**包含方向**（旧实现 `quote in req or
    req[:40] in quote`，会误杀"要求头 + 真证据尾"的合法引文）改为**覆盖率**：
    纯照抄要求/整条规则复述 → 覆盖率≈1；真业绩表引文 → ≈0；要求头+证据尾 → 居中。
    """
    if not ref_n:
        return 0.0
    if len(text_n) < w:
        return 1.0 if text_n in ref_n else 0.0
    ref_grams = {ref_n[i:i + w] for i in range(len(ref_n) - w + 1)}
    total = len(text_n) - w + 1
    hit = sum(1 for i in range(total) if text_n[i:i + w] in ref_grams)
    return hit / total


def _has_long_echo_run(page_n: str, ref_n: str, run_len: int = _ECHO_PAGE_MIN_RUN) -> bool:
    """短页里是否存在 ≥run_len 的连续片段完全落在 `ref_n` 中（整段抄要求）。"""
    if not ref_n or len(page_n) < run_len:
        return False
    return any(page_n[i:i + run_len] in ref_n for i in range(len(page_n) - run_len + 1))


def _is_echo_page(page_text: str, echo_n: str) -> bool:
    """整页是"要求复述页"（投标人把评分规则抄进标书的索引/响应对照表）→ 应剔除。

    判据①（主力）：页面内嵌 ≥60 字**连续**的要求复述段。真实标书里「详细评审索引表 /
    响应对照表」逐字抄评分规则，但同页还含其它评分项、封面噪声——整页覆盖率反而低
    （2026-10-21 实证：b13 第 95 页覆盖率仅 0.20，但内嵌 68 字连续规则段；真业绩表
    第 126 页连续段仅 10 字）。故用"连续段"而非"覆盖率"判活。
    判据②（兜底）：短页（≤300 字）且整页几乎都是要求（覆盖率高）——纯要求碎片页。
    真业绩表页即便带一行【证明材料】头（≈40 字 < 60）→ 保留。
    """
    page_n = _nnw(page_text)
    if not page_n or not echo_n:
        return False
    if _has_long_echo_run(page_n, echo_n, _ECHO_PAGE_MIN_RUN):
        return True
    if len(page_n) <= _ECHO_PAGE_SHORT_CHARS and _ngram_fraction(page_n, echo_n) >= _ECHO_PAGE_FRACTION:
        return True
    return False


def _locate_in_bid(quote_n: str, bid_norm: str) -> str | None:
    """引文回投标文件定位：精确 → 前缀梯（含去逗号数字变体）。返回命中层级或 None。

    与主观分闸门同一语义（引文必须真实存在于标书）。OCR 表格串行/货币符号差异会让
    逐字匹配失败，故退 60/40/25/15 前缀梯兜住改写；15 字前缀偶然伪造的概率极低。
    """
    if not quote_n or not bid_norm:
        return None
    variants = [quote_n]
    stripped = quote_n.replace(",", "").replace("，", "")
    if stripped != quote_n:
        variants.append(stripped)
    for v in variants:
        if v in bid_norm:
            return "exact"
    for L in config.PREFIX_FALLBACK_LENGTHS:
        for v in variants:
            if len(v) >= L and v[:L] in bid_norm:
                return f"prefix{L}"
    return None


def _case_candidate_pages(
    pages: list[tuple[int, str, str]],
    echo_n: str | None = None,
    stats_out: dict | None = None,
) -> list[list[tuple[int, str]]]:
    """业绩/人员案例核验的候选页 → 剔除"要求复述页" → 按预算切批。

    旧实现把 `facts.text`（全部文档拼接）关键词选段后只送 15k 字符——几百页
    标书里案例页根本进不了上下文，团队成员经验项四家全 0（2026-10-21 实证）。
    这里按"锚点词命中页 ±1 页"圈定候选，再按预算切批多段调用。

    `echo_n`（归一化的 要求原文 source_quote + required_evidence）非空时，逐页
    剔除"要求复述页"：投标人常把评分规则整段抄进标书的索引/响应对照表页，模型
    会被它带偏、拿要求文本当引文（2026-10-21 实证：b13 第 95 页复述规则、真业绩
    在第 126 页，两页同送 → 引文全被判照抄 → 未定分）。剔除后模型只看真证据页。
    """
    import re as _re

    anchor = _re.compile(r"合同|项目名称|甲方|乙方|发票|验收|中标通知书|业绩|项目团队|负责人")
    hits = [i for i, (_no, t, _s) in enumerate(pages) if anchor.search(t or "")]
    idx: set[int] = set()
    for i in hits:
        idx.update({i - 1, i, i + 1})
    ordered = sorted(i for i in idx if 0 <= i < len(pages))
    n_candidates = len(ordered)
    dropped_nos: list[int] = []
    if echo_n:
        kept = []
        for i in ordered:
            if _is_echo_page(pages[i][1] or "", echo_n):
                dropped_nos.append(pages[i][0])
            else:
                kept.append(i)
        ordered = kept
    batches: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    cur_chars = 0
    for i in ordered:
        no, text = pages[i][0], pages[i][1] or ""
        if cur and cur_chars + len(text) > _CASE_BATCH_CHARS:
            batches.append(cur)
            cur, cur_chars = [], 0
            if len(batches) >= _CASE_MAX_BATCHES:
                break
        cur.append((no, text))
        cur_chars += len(text)
    if cur and len(batches) < _CASE_MAX_BATCHES:
        batches.append(cur)
    covered = sum(len(b) for b in batches)
    if stats_out is not None:
        stats_out["pages_candidates"] = n_candidates
        stats_out["pages_sent"] = covered
        stats_out["pages_dropped_echo"] = dropped_nos
        stats_out["batches_sent"] = len(batches)
    if len(batches) >= _CASE_MAX_BATCHES and covered < len(ordered):
        log.warning(
            "案例核验候选页超出 %d 批上限，已按锚点页优先截断（候选 %d 页，送审 %d 页）",
            _CASE_MAX_BATCHES, len(ordered), covered,
        )
    return batches


def _llm_verify_cases(
    gateway,
    c: dict,
    requirement_text: str,
    artifacts_list: list[str],
    material_pages: str | None,
    pages: list[tuple[int, str, str]],
    data_class: str,
    bid_norm: str | None = None,
    stats_out: dict | None = None,
) -> list[dict] | None:
    """带证明材料要求的计数项：LLM 逐案例列出并核对材料齐全性。

    返回案例清单（含 contract/invoice/acceptance 布尔与 sanitized 原因码），
    无候选页或全部批次失败返回 None（调用方退回文本级校验）。
    余额/配额类错误向上抛（fail-fast）。

    `bid_norm`（该投标人归一化全文）非空时，`_sanitize_case` 会把引文回标书
    定位（与主观分闸门同一语义）；`echo_n` 用于剔除要求复述页 + 判照抄。
    """
    from pydantic import BaseModel, Field

    from app.llm.base import LLMError
    from app.rules import prompts

    if not artifacts_list and not (requirement_text or "").strip():
        return None
    # 要求复述文本 = 完整评分规则（source_quote）+【证明材料】块：索引表复述的是
    # 规则本身，必须含 source_quote 才能识别（只比 required_evidence 会漏掉大部分）。
    echo_n = _nnw((c.get("source_quote") or "") + (requirement_text or ""))
    batches = _case_candidate_pages(pages, echo_n=echo_n, stats_out=stats_out)
    if not batches:
        return None

    class CaseList(BaseModel):
        cases: list[dict] = Field(default_factory=list)

    art_list = [a.strip() for a in artifacts_list if a.strip()] or [requirement_text]
    person_scope = str((c.get("params") or {}).get("person_scope") or "（不区分人员）")
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for batch_no, batch in enumerate(batches, start=1):
        text = "\n".join(f"【第 {no} 页】\n{t}" for no, t in batch)
        user = prompts.MATERIAL_COUNT_USER_TEMPLATE.format(
            name=c.get("name") or "",
            max_score=c.get("max_score") or "",
            source_quote=(c.get("source_quote") or "")[:800],
            artifacts="、".join(art_list),
            person_scope=person_scope,
            material_pages=material_pages or "（无）",
            bid_text=text,
        )
        try:
            out, _res = gateway.chat_json(
                prompts.MATERIAL_COUNT_SYSTEM_PROMPT, user, CaseList,
                data_class=data_class,
            )
        except LLMError as exc:
            msg = str(exc)
            if any(k in msg for k in ("402", "insufficient", "quota", "30001", "arrears")):
                raise
            log.warning("案例核验第 %d/%d 批失败：%s", batch_no, len(batches), exc)
            continue
        for x in out.cases:
            if not isinstance(x, dict):
                continue
            case = _sanitize_case(x, requirement_text, bid_norm=bid_norm, echo_n=echo_n)
            key = (
                str(case.get("name") or "").strip().lower(),
                str(case.get("person") or "").strip(),
            )
            if key in seen:
                case["duplicate"] = True
                continue
            seen.add(key)
            if not case.get("page_hint"):
                case["page_hint"] = batch[0][0]
            merged.append(case)
    return merged


def _sanitize_case(
    case: dict,
    requirement_text: str,
    bid_norm: str | None = None,
    echo_n: str | None = None,
) -> dict:
    """核验结果代码层校验：quote 是要求文本影子的案例，材料判定不可信 → 全部降 false。

    8B 模型的作弊模式（2026-10-15 实证）：拿"须提供合同复印件、发票复印件…"这句
    **要求文本**当 quote，把四个业绩案例全判材料齐全。

    判据（2026-10-21 重做）：
    - `requirement_shadow`：引文的 n-gram 覆盖率在要求复述文本（source_quote +
      required_evidence）中 ≥ `_ECHO_SHADOW_FRACTION`。用覆盖率取代旧的包含方向判据，
      同时正确处理三种形状——纯要求块 ⊂ 引文、整条规则 ⊃ 引文（索引表复述）、
      要求头 + 真证据尾（旧 `req[:40] in quote` 会误杀最后这种，现放行）。
    - `quote_not_in_bid`：给了 `bid_norm` 且引文（精确/前缀梯）回不到标书 → 判伪造。
      与主观分引文闸门同一语义；失败态是"未定分转人工"（安全态）而非错分。
    - `bid_norm is None`：无法回标书核验时维持旧的宽松语义（不误杀未接线的调用方）。
    - `quote_too_short`：过短且无金额/年份等实体。

    非 `ok` 一律把 contract/invoice/acceptance 三件套降 false。
    `sanitized` 原因码：ok / requirement_shadow / quote_not_in_bid / quote_too_short。
    """
    case = dict(case)
    quote = str(case.get("quote") or "").strip()
    quote_n = _nnw(quote)
    if echo_n is None:
        echo_n = _nnw(requirement_text or "")
    cheat = True
    reason = "quote_too_short"
    tier: str | None = None
    if len(quote_n) >= config.MIN_QUOTE_CHARS:
        frac = _ngram_fraction(quote_n, echo_n) if echo_n else 0.0
        if echo_n and frac >= _ECHO_SHADOW_FRACTION:
            reason = "requirement_shadow"  # 照抄要求/整条规则复述
        elif bid_norm is not None:
            tier = _locate_in_bid(quote_n, bid_norm)
            if tier is not None:
                cheat = False
                reason = "ok"  # 引文真实存在于标书 = 真证据
            else:
                reason = "quote_not_in_bid"
        else:
            cheat = False
            reason = "ok"  # 旧行为：无标书可核验时信任模型
    elif bool(re.search(r"\d", quote)):
        # 短引文但含金额/年份等实体（如"2024年6月"）仍可信
        cheat = False
        reason = "ok"
    if cheat:
        for key in ("contract", "invoice", "acceptance"):
            case[key] = False
    case["sanitized"] = reason
    if tier is not None:
        case["match_tier"] = tier
    return case


def _eval_veto(  # noqa: ANN001
    c: dict,
    facts,
    on_fail: str = "recommend_reject",
    text: str | None = None,
    gateway=None,
    data_class: str = "synthetic",
) -> dict:
    check_type = c.get("check_type")
    params = c.get("params") or {}
    if not check_type:
        return {"verdict": "need_human", "message": "该条款缺少可执行的检查定义"}
    result = run_check(check_type, params, facts)
    if (
        config.auto_mode()
        and gateway is not None
        and (result.status == "unknown" or (result.status == "fail" and result.need_human))
    ):
        # 全自动模式：human_only / 冲突 / 信号命中 → LLM 带原文证据裁决。
        # AI 不否决（2026-10-14 用户指令，恢复"绝不自动否决"原则）：LLM 的 fail
        # 一律降级为"疑问"——machine_status=need_human，由评标委员会认定；
        # 投标人照常参与评分与排名。pass 才是唯一通过终态。
        from app.engine.auto_judge import refine_with_llm

        result = refine_with_llm(gateway, c, facts, text or "", result, data_class=data_class)
        if result.status == "pass":
            return {"verdict": "pass", "message": result.message, "evidence": result.evidence_quote}
        return {
            "verdict": "need_human",
            "message": f"AI 存疑（不自动否决，转评标委员会认定）：{result.message}",
            "evidence": result.evidence_quote,
        }
    verdict = verdict_for(c["code"], c["name"], result, on_fail=on_fail)
    return {"verdict": verdict, "message": result.message, "evidence": result.evidence_quote}


def _review_status_for_verdict(result: dict) -> str:
    # 全自动模式：pass/确定性判定收敛为 auto_confirmed；AI 存疑（need_human）
    # 保留 conflict_flagged —— 疑问进复核清单，报告不得宣称定稿
    if config.auto_mode():
        return "conflict_flagged" if result["verdict"] == "need_human" else "auto_confirmed"
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
    doubt: bool = False,
    ai_raw_json: dict | None = None,
) -> ScoreRecord:
    # 全自动模式：复核态收敛为终态；但 AI 存疑项（need_human）与人工核验项
    # （doubt，如 count 的证明材料链冲突）保留 conflict_flagged 进疑问清单——
    # "标疑问"正是它们的终态，不得被抹成"已自动确认"（2026-10-14/15）
    if (
        config.auto_mode()
        and not doubt
        and review_status in ("pending", "ai_suggested", "conflict_flagged")
        and machine_status != "need_human"
    ):
        review_status = "auto_confirmed"
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
        if ai_raw_json is not None:
            row.ai_raw_json = ai_raw_json
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
    committee: list[dict] | None = None,
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
            # 明细（评委或证据簇）：报告汇总表/审计直接可用。注意 semantic 字段——
            # hierarchical 模式下这些条目是**证据簇**，不是独立评委；旧实现一律
            # 写 trimmed_mean 并称作"AI 评委"，与真实聚合方式不符（审计误导）。
            "committee": committee,
            "committee_semantic": (
                "evidence_cluster" if config.review_mode() == "hierarchical" else "judge"
            ),
            "aggregation": (
                f"trimmed_mean_{len(committee)}_drop_high_low"
                if committee and config.review_mode() != "hierarchical"
                else ("evidence_cluster_weighted" if committee else None)
            ),
            # 证明材料链核对与复合因素子项得分（评分透明度）
            "artifacts_check": suggestion.artifacts_check,
            "sub_scores": suggestion.sub_scores,
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
        # 子项引文闸门未执行（模型整体没给引文）→ 分数不可复核，标疑问
        gate_not_executed = getattr(suggestion, "quote_gate_executed", None) is False
        if config.auto_mode():
            # 全自动模式：AI 建议直接生效；给不出分的按最低档兜底（宁严勿纵），
            # 警告与 reasoning 全部保留在 ai_raw_json / warnings 供事后审计。
            # 例外：模型输出截断/解析失败（parse_failed）→ 疑问 + 不进总分——
            # 兜底 0 分会把"模型输出坏了"伪装成"投标人做得差"
            # （2026-10-15 诚道运维方案 0 分实证）。
            parse_failed = any(
                isinstance(w, dict) and w.get("message") in ("parse_failed", "llm_failed")
                for w in (row.warnings_json or [])
            )
            if gate_not_executed:
                # 保留分值（进总分，不影响排名），但标疑问进复核清单：
                # 若整项直接判未定分，8B 偶尔不给逐子项引文就会让整份报告失效
                row.warnings_json = (row.warnings_json or []) + [
                    {"message": "子项引文未提供，证据闸门未执行，建议人工复核"}
                ]
            if suggestion.status == "ai_suggested":
                row.final_score = suggestion.score
            elif parse_failed:
                row.final_score = None
                row.machine_status = "need_human"
                row.review_status = "conflict_flagged"
                row.warnings_json = (row.warnings_json or []) + [
                    {"message": "全自动模式：模型调用失败/输出解析失败，按疑问处理不进总分，须人工补分"}
                ]
                s.add(row)
                return
            else:
                mins = [Decimal(str(lv.get("min")))
                        for lv in (criterion.get("levels") or []) if lv.get("min") is not None]
                fallback = min(mins) if mins else Decimal(0)
                row.final_score = fallback
                row.warnings_json = (row.warnings_json or []) + [
                    {"message": f"全自动模式：模型未给出有效分值，按最低档兜底（{fallback} 分）"}
                ]
            row.machine_status = "pass"  # 终态：已定分（兜底详情在 warnings）
            row.review_status = "conflict_flagged" if gate_not_executed else "auto_confirmed"
            s.add(row)
        elif suggestion.status == "ai_suggested" and evidence_status != "verified":
            # 无已核验证据 → 建议仍记录，但降级为待人工
            row.review_status = "pending"
            row.warnings_json = (row.warnings_json or []) + [
                {"message": "证据未能核验，需人工核查原文"}
            ]
            s.add(row)
        elif suggestion.status == "ai_suggested":
            s.add(row)
