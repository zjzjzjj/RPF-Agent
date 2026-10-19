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

    if ctx:
        ctx.advance(message=f"客观审查 {len(veto_items)} 项")

    # ---- ① 资格/符合性（三态）----
    stats = {"objective": 0, "price": 0, "subjective": 0, "match": 0,
             "need_human": 0, "citations_verified": 0, "citations_total": 0}
    excluded_bidders: set[int] = set()  # 客观审查未通过 → 不进基准价（exclude_invalid）
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

    # ---- 证明材料页索引（count 逐案例核验与主观评审共用） ----
    _MAT_KEYWORDS = ("发票", "验收", "业主证明", "查询截图", "证书复印件", "承诺书", "营业执照")
    material_hints: dict[int, str] = {}
    with session_scope() as s:
        from app.db.models import DocumentPage

        for bid_id in bidder_ids:
            doc_id = main_doc_by_bidder.get(bid_id)
            if doc_id is None:
                material_hints[bid_id] = "（无）"
                continue
            pages: dict[str, list[int]] = {}
            for page_no, ptext in s.execute(
                select(DocumentPage.page_no, DocumentPage.text)
                .where(DocumentPage.document_id == doc_id)
                .order_by(DocumentPage.page_no)
            ):
                for kw in _MAT_KEYWORDS:
                    if kw in (ptext or ""):
                        pages.setdefault(kw, []).append(int(page_no))
            hint_parts = [
                f"{kw}：第{'、'.join(str(p) for p in nos[:12])} 页"
                for kw, nos in pages.items() if nos
            ]
            material_hints[bid_id] = "；".join(hint_parts) if hint_parts else "（未见证明材料相关内容）"

    # 招标文件原文页（归一化）：恢复评分项的【证明材料】要求段
    tender_pages: list[tuple[int, str]] = []
    with session_scope() as s:
        from app.db.models import DocumentPage
        from app.parsing.normalize import normalize_no_ws as _nnw

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

    # ---- ③ 商务 count 分（带【证明材料】的走 LLM 逐案例核验） ----
    # 认证查询截图节检测（2026-10-16 用户授权 AI 按规则裁量）：招标明文
    # 「管理体系认证还需提供认证认可平台查询结果截图，未按要求提供不得分」。
    # 截图内容 OCR 不可得，但"截图节"的存在性可判（正文标题页不含目录/要求页）——
    # 龙即有 3 个截图节（真实认证 4 分），先数/致成/诚道无（真实 1/4/3 分，
    # 先数与规则执行一致）。
    import re as _re_cert

    cert_sections: dict[int, bool] = {}
    # 行级标题模式：截图节标题独立成行（允许节号前缀与页码后缀）
    _cert_title_pat = _re_cert.compile(
        r"(?m)^\s*(?:\d+(?:\.\d+){1,3}\s*)?[^\n]{0,30}查询结果截图[^\n]{0,20}$"
    )
    with session_scope() as s:
        from app.db.models import DocumentPage

        for bid_id in bidder_ids:
            doc_id = main_doc_by_bidder.get(bid_id)
            found = False
            if doc_id is not None:
                for ptext in s.execute(
                    select(DocumentPage.text).where(DocumentPage.document_id == doc_id)
                ).scalars():
                    t = ptext or ""
                    # 排除目录页（点线）与长要求文本页（索引表复述要求常超 300 字）
                    if t.count("....") >= 2 or len(t) > 300:
                        continue
                    if _cert_title_pat.search(t):
                        found = True
                        break
            cert_sections[bid_id] = found

    for bid_id in bidder_ids:
        facts = facts_by_bidder[bid_id]
        for c in count_items:
            params = c.get("params") or {}
            artifacts = recover_artifacts_from_tender(c, tender_pages)
            c_scoring = {**c, "required_artifacts": artifacts}
            # field 可能缺省（真实编译产物里常由名称推断）——与 score_count 用
            # 同一推断函数，避免两处口径漂移（2026-10-16 实证：params 无 field
            # 导致 cert_section_present 未传递、认证门槛静默失效）
            _field = params.get("field") or ""
            if not _field:
                from app.engine.objective import _infer_count_field

                _field = _infer_count_field(c) or ""
            if _field == "certificates":
                c_scoring = {**c_scoring, "cert_section_present": cert_sections.get(bid_id, False)}
            # LLM 逐案例核验仅用于**业绩类**（按案例核对合同/发票/验收）。
            # 证书类（认证/资质）不适用：案例核验的 contract/invoice/acceptance
            # 三标记语义与"证书复印件+查询截图"错位，会把有证书的投标人整体清零
            # （2026-10-16 实证：致成 4→0、诚道 3→0、先数 1→0）。证书类走
            # 确定性计数（facts.certificates=表内有复印件的），材料截图缺失
            # 由 doubt 标疑问转人工。
            is_performance = (params.get("field") == "performance") or (
                params.get("field") != "certificates"
                and any(k in str(c.get("name") or "") for k in ("业绩", "案例"))
            )
            if gateway is not None and artifacts and is_performance:
                cases = _llm_verify_cases(
                    gateway, c, facts, material_hints.get(bid_id), artifacts, data_class
                )
                if cases:
                    c_scoring = {**c_scoring, "verified_cases": cases}
            result = score_count(c_scoring, params, facts)  # type: ignore[arg-type]
            # unresolved 以结构化键落库：下游（报告疑问清单）直接判读，
            # 不再靠匹配 detail 的中文措辞——那种耦合已经断过一次。
            # conflict（如证明材料链缺失/部分缺失）→ doubt 保留疑问态转人工，
            # 分数先行计算但复核前不得视为定稿。
            _upsert_score(
                run_id, bid_id, c,
                machine_score=result.score,
                machine_status="pass",
                review_status="conflict_flagged" if result.conflict else "pending",
                final_score=result.score,
                warnings_json=([{"message": result.detail, "unresolved": result.unresolved}]
                               if result.unresolved else [{"message": result.detail}]),
                doubt=result.conflict,
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
        if ctx:
            ctx.advance(message=f"主观分评审（{config.review_mode()}） {len(subjective_items)} 项 × {len(bidder_ids)} 家")
        for bid_id in bidder_ids:
            for c in subjective_items:
                hint = material_hints.get(bid_id)
                if use_hierarchical:
                    suggestion, committee = suggest_score_hierarchical(
                        gateway, c, text_by_bidder[bid_id], data_class=data_class,
                        material_pages=hint,
                    )
                    if suggestion is None:
                        # 证据地图不可用（嵌入服务故障）→ 回退单次评审
                        suggestion, committee = suggest_score_committee(
                            gateway, c, text_by_bidder[bid_id], data_class=data_class,
                            material_pages=hint,
                        )
                else:
                    suggestion, committee = suggest_score_committee(
                        gateway, c, text_by_bidder[bid_id], data_class=data_class,
                        material_pages=hint,
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


def _llm_verify_cases(gateway, c: dict, facts, material_pages: str | None,
                      artifacts: str, data_class: str) -> list[dict] | None:
    """带【证明材料】的计数项：LLM 逐案例列出并核对材料齐全性。

    返回案例清单（含 contract/invoice/acceptance 布尔），失败返回 None
    （调用方退回文本级校验）。余额/配额类错误向上抛（fail-fast）。
    """
    from pydantic import BaseModel, Field

    from app.engine.selection import (
        select_chunks_for_criterion, split_text_chunks,
    )
    from app.llm.base import LLMError
    from app.rules import prompts

    if not artifacts:
        return None

    class CaseList(BaseModel):
        cases: list[dict] = Field(default_factory=list)

    text = facts.text
    if len(text) > 15000:
        picked, _meta = select_chunks_for_criterion(
            split_text_chunks(text), c, 15000
        )
        text = "\n".join(t for _i, t in picked)
    user = prompts.MATERIAL_COUNT_USER_TEMPLATE.format(
        name=c.get("name") or "",
        max_score=c.get("max_score") or "",
        source_quote=(c.get("source_quote") or "")[:800],
        artifacts="、".join(artifacts),
        material_pages=material_pages or "（无）",
        bid_text=text,
    )
    try:
        out, _res = gateway.chat_json(
            prompts.MATERIAL_COUNT_SYSTEM_PROMPT, user, CaseList,
            data_class=data_class,
        )
        return [_sanitize_case(x, artifacts) for x in out.cases if isinstance(x, dict)]
    except LLMError as exc:
        msg = str(exc)
        if any(k in msg for k in ("402", "insufficient", "quota", "30001", "arrears")):
            raise
        return None


def _sanitize_case(case: dict, artifacts: str) -> dict:
    """核验结果代码层校验：quote 是要求文本影子的案例，材料判定不可信 → 全部降 false。

    8B 模型的作弊模式（2026-10-15 实证）：拿"须提供合同复印件、发票复印件…"这句
    **要求文本**当 quote，把四个业绩案例全判材料齐全。判据：quote 与材料要求段
    互相包含（≥12 字重合）或 quote 空 → 该案例三件套一律 false。
    """
    case = dict(case)
    quote = str(case.get("quote") or "").strip()
    artifacts_n = artifacts.replace(" ", "")
    quote_n = quote.replace(" ", "")
    cheat = True
    if len(quote_n) >= 12:
        if quote_n not in artifacts_n and artifacts_n[:40] not in quote_n:
            cheat = False
    if cheat:
        for key in ("contract", "invoice", "acceptance"):
            case[key] = False
        case["sanitized"] = "quote 无效（要求文本/空引文），材料判定降为未提供"
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
            # 评审委员会明细：每位 AI 评委的独立分与弃分标记，报告汇总表/审计直接可用
            "committee": committee,
            "aggregation": f"trimmed_mean_{len(committee)}_drop_high_low" if committee else None,
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
            row.review_status = "auto_confirmed"
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
