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
    score_attribute_tier,
    score_count,
    score_count_tier,
    score_deviation_deduct_table,
    score_value_linear,
    score_value_tier,
    verdict_for,
)

# P1-E 新计分原语分发：按规则包 params.engine 路由（阈值全部来自项目细则原文）
_P1E_ENGINES = {
    "attribute_tier": score_attribute_tier,
    "count_tier": score_count_tier,
    "deviation_deduct_table": score_deviation_deduct_table,
    "value_tier": score_value_tier,
    "value_linear": score_value_linear,
}
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


def _is_fatal_llm_error(exc: BaseException) -> bool:
    """配额/预算/数据分级类错误必须 fail-fast 整场（不能被单格隔离吞掉）。"""
    from app.errors import DataClassDenied, LLMBudgetExceeded

    if isinstance(exc, (LLMBudgetExceeded, DataClassDenied)):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in ("402", "insufficient", "quota", "30001", "arrears"))


def _forced_zero_reason(cname: str, ps: dict, c: dict | None = None) -> str | None:
    """人员经验材料门槛（确定性，规则执行）：细则明文「不得分」且材料**确认**不存在 → 0。

    两个条件缺一不可——细则没写罚则的不越权置 0。材料存在性是**三态**（2026-11-02）：
    True=材料确认在 / False=材料确认不在（积极确认缺失）/ None=机器看不见（扫描件/
    OCR 漏读/正则未覆盖）。**只有 False 才强制 0**——"没看见"不等于"没有"（run77
    c48 实证：简历+职称证书在 p121-122 文本层，旧正则未覆盖"简历"→误判无材料→
    清零，人工给满分）。None 不短路，"未见材料"提示并入 LLM 输入由其判档。
    Phase1（跳过 LLM 短路）与 Phase2（权威置 0）共用本函数，单一来源防口径漂移。
    页数阈值（五年<3 页/社保<16 页）是按旧项目 4 家拟合的产物，换项目即误杀，
    已删除（2026-10-26 run 54 实证）。
    """
    rule_text = ""
    if c is not None:
        rule_text = (
            f"{c.get('required_evidence') or ''}"
            f"{c.get('source_quote') or ''}"
            f"{c.get('description') or ''}"
        )
    if "不得分" not in rule_text:
        return None
    if "项目负责人" in cname and ps.get("exp_material") is False:
        return "已确认未提供工作经验证明材料，按评分因素「无证明材料不得分」计 0"
    if "团队成员" in cname and ps.get("social_material") is False:
        return (
            "已确认未提供团队成员社保证明材料，"
            "按评分因素「不提供或不能提供有效证明的不得分」计 0"
        )
    return None


def _missing_material_hint(cname: str, ps: dict, c: dict | None = None) -> str | None:
    """材料存在性 None（机器看不见）时给 LLM 的提示：尽力定位、定位不到按低档并标疑问。"""
    rule_text = ""
    if c is not None:
        rule_text = (
            f"{c.get('required_evidence') or ''}"
            f"{c.get('source_quote') or ''}"
            f"{c.get('description') or ''}"
        )
    if "不得分" not in rule_text:
        return None
    if "项目负责人" in cname and ps.get("exp_material") is None:
        return "【材料提示】工作经验证明材料未在文本层检出（可能为扫描件/图片页），请在选段中尽力定位；确认无材料按细则「不得分」判低档或 0"
    if "团队成员" in cname and ps.get("social_material") is None:
        return "【材料提示】团队成员社保证明材料未在文本层检出（可能为扫描件/图片页），请在选段中尽力定位；确认无材料按细则「不得分」判低档或 0"
    return None


def _parallel_map(worker_fn, cells: list, *, ctx: JobContext | None, message_fmt: str):
    """Phase1 并行执行（LLM 调用），带锁 advance；结果与输入同序。

    参照主观分 Phase1/Phase2 模板：LLM 并行、DB 写留在调用方串行段（SQLite
    单写者）；单格非致命异常由 worker 自行隔离，fatal（配额/预算/涉密）穿透。
    并发度 = min(格数, BID_EVAL_LLM_CONCURRENCY)——网关信号量是真闸门，这里
    不再硬顶 4（硬顶让服务器 8 并发静默减半）。
    """
    import threading as _threading
    from concurrent.futures import ThreadPoolExecutor

    total = len(cells)
    if total == 0:
        return []
    prog_lock = _threading.Lock()
    done = [0]

    def _wrapped(args):  # noqa: ANN001
        result = worker_fn(args)
        with prog_lock:
            done[0] += 1
            if ctx is not None:
                ctx.advance(message=message_fmt.format(done=done[0], total=total))
        return result

    workers = min(total, max(1, config.llm_max_concurrency()))
    if workers <= 1:
        return [_wrapped(a) for a in cells]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_wrapped, cells))


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

    # R0 客观分路由（2026-11-03 run78 取证）：score_type=subjective 但细则是
    # 明示「X得N分」档表的格，评分时改走客观引擎（服务承诺"4小时内到达得4分…"
    # 被编译成 subjective 后丢确定性引擎，中科 12 分「该给没给」主因之一）。
    # 浅拷贝 params（snapshot criteria 被全部 bidder 格共享，就地改会跨格泄漏）；
    # score_type 保持原样（冻结包事实镜像，篡改会断审计链）。歧义不猜：路由不出
    # 的格照走主观路径。
    if config.objective_route_enabled():
        from app.rules.check_infer import infer_objective_route

        _kept_subj = []
        for c in subjective_items:
            route = infer_objective_route(c)
            if route is None:
                _kept_subj.append(c)
                continue
            engine_kind, rparams = route
            c2 = {**c, "params": {**(c.get("params") or {}), **rparams}}
            if engine_kind != "count":
                c2["params"]["engine"] = engine_kind
            c2["_objective_route"] = {"engine": engine_kind, "params": rparams}
            count_items.append(c2)
        subjective_items = _kept_subj

    # 进度按「评分项 × 投标人」粒度上报：客观审查是全链最耗时的一段（本地 8B
    # 每条 30-90s、串行），只按阶段上报会让 UI 停在 25% 长达数十分钟
    # （2026-10-20 实证：job 50 在「客观审查 85 项」停 20 分钟，用户以为没开始）。
    # advance 内含取消检查——不逐项上报 = 整个客观阶段无法取消。
    # total 必须含 count/match 格：旧公式漏了这两段，进度冻结在「计算价格分」
    # 近一小时且无法取消（2026-10-26 run 54 实证：330/380=87% 的卡死假象）。
    if ctx is not None:
        obj_total = len(veto_items) * len(bidder_ids)
        count_total = len(count_items) * len(bidder_ids)
        match_total = len(match_items) * len(bidder_ids)
        subj_total = len(subjective_items) * len(bidder_ids) if "subjective" in stages else 0
        price_total = 1 if (price_items and "price" in stages) else 0
        ctx.start(
            total=obj_total + count_total + match_total + subj_total + price_total + 1,
            message="客观审查准备",
        )

    # ---- 事实与文本 ----
    facts_by_bidder: dict[int, object] = {}
    text_by_bidder: dict[int, str] = {}
    main_doc_by_bidder: dict[int, int | None] = {}
    doc_ids_by_bidder: dict[int, list[int]] = {}
    scan_hints: dict[int, bool] = {}
    data_class = "synthetic"
    for bid_id in bidder_ids:
        text, _titles, doc_ids = bidder_service.bid_text(bid_id)
        text_by_bidder[bid_id] = text
        facts_by_bidder[bid_id] = extract_bid_facts(text)
        main_doc_by_bidder[bid_id] = doc_ids[0] if doc_ids else None
        doc_ids_by_bidder[bid_id] = list(doc_ids)
        with session_scope() as s:
            from app.db.models import Document, DocumentPage

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
                # 扫描件页提示（「看不见≠不存在」豁免的依据）：OCR 页/图片页的
                # 文本层不可信，确定性检索 fail 可能是看不见造成的。
                ocr_pages = s.execute(
                    select(func.count(DocumentPage.id)).where(
                        DocumentPage.document_id.in_(doc_ids),
                        DocumentPage.source != "text",
                    )
                ).scalar_one()
                scan_hints[bid_id] = bool(ocr_pages)

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

    # ---- 材料类别映射（选段软过滤/案例页优先用）----
    # 材料标签已在 parse 后自动分类落库；这里读映射（文本已变的失效标签自动跳过）。
    # rescue 改写页文本后在材料段重建一次。
    page_cats_by_bidder: dict[int, dict[int, str]] = {}
    page_cat_confs_by_bidder: dict[int, dict[int, float]] = {}
    chunk_cats_by_bidder: dict[int, list[str | None]] = {}
    chunk_cat_confs_by_bidder: dict[int, list[float]] = {}
    chunk_srcs_by_bidder: dict[int, list[tuple[int, int]]] = {}  # P1-6 选段加成用
    if config.material_index_enabled():
        try:
            (
                page_cats_by_bidder,
                page_cat_confs_by_bidder,
                chunk_cats_by_bidder,
                chunk_cat_confs_by_bidder,
                chunk_srcs_by_bidder,
            ) = _load_material_maps(doc_ids_by_bidder, main_doc_by_bidder, bidder_ids)
        except Exception:  # noqa: BLE001 —— 分类是增强不是主链
            log.warning("材料类别映射构建失败，选段退化为无过滤", exc_info=True)

    # ---- ① 资格/符合性（三态）----
    stats = {"objective": 0, "price": 0, "subjective": 0, "match": 0,
             "need_human": 0, "citations_verified": 0, "citations_total": 0}
    excluded_bidders: set[int] = set()  # 客观审查未通过 → 不进基准价（exclude_invalid）
    # 确定性资格硬 fail（auto 模式下 recommend_reject 只来自确定性判定——LLM 的
    # fail 已降级 need_human）→ 仅剔出价格基准价（benchmark_ineligible），
    # 「AI 不否决」不变：照常评分/排名/展示。对齐人工"有效标均价"口径
    # （2026-10-27 测试2 实证：无效标进基准 → 价格分系统性 +4~7）。
    benchmark_ineligible: set[int] = set()

    # 两段式（阶段 2，2026-10-30）：Phase0 全格确定性预检（无 LLM）→ Phase1
    # human_only 组批（refine_with_llm_batch）+ single 单格 LLM 并行。开关
    # BID_EVAL_VETO_HUMAN_ONLY_BATCH=0 回退全部走 single 旧路径。
    veto_cells = [(bid_id, c) for bid_id in bidder_ids for c in veto_items]
    veto_results = _resolve_veto_results(
        veto_cells,
        facts_by_bidder=facts_by_bidder,
        text_by_bidder=text_by_bidder,
        scan_hints=scan_hints,
        gateway=gateway,
        data_class=data_class,
        chunk_cats_by_bidder=chunk_cats_by_bidder,
        chunk_cat_confs_by_bidder=chunk_cat_confs_by_bidder,
        ctx=ctx,
    )
    # Phase2：串行落库（SQLite 单写者；顺序 = 提交序 = 旧串行循环序，行内容同构）
    for (bid_id, c), result in zip(veto_cells, veto_results):
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
        # 基准价剔除（第 5 类，2026-10-27）：确定性硬 fail（auto 模式 recommend_reject
        # 必来自 run_check 结构化/文本事实，LLM fail 已降级）→ benchmark_ineligible
        # 仅剔基准价，不否决。开关 BID_EVAL_BENCHMARK_EXCLUDE_SUSPECT 可回退。
        if result["verdict"] == "recommend_reject" and config.benchmark_exclude_suspect():
            benchmark_ineligible.add(bid_id)
        stats["objective"] += 1

    # ---- ② 价格分 ----
    # 价格分已移至主观分之后（P1-C）：基准价资格门槛（技术/商技得分≥N%）依据的是
    # 该部分**已算完的全部得分**（含主观项），在主观分之前算会把不足额的客观部分
    # 误判为未达门槛（14/21 份语料带门槛）。价格分本身不依赖计数/主观结果，后移
    # 无副作用；材料页 OCR 救援仍紧贴计数段之前（下方原位不动）。

    # ---- 业绩案例证据：材料页 OCR 救援（必须在读页文本之前） ----
    # 发票/验收页常是"标题 43 字 + 占比 0.19–0.23 的扫描图"——字符数 >30、图片占比
    # <0.35，正常解析不触发 OCR，文本里永远没有发票内容。评分前对证据段做定向救援，
    # 否则任何"选页/摘录"策略都会判"材料缺失"（2026-10-21 doc2 实证）。
    _has_perf = any(
        (ci.get("params") or {}).get("field") == "performance"
        or (ci.get("params") or {}).get("person_scope")
        or any(k in str(ci.get("name") or "") for k in ("业绩", "案例"))
        for ci in count_items
    )
    if _has_perf and config.auto_mode():
        from app.services.ingest_service import rescue_material_pages, rescue_person_material_pages

        _has_person = any((ci.get("params") or {}).get("person_scope") for ci in count_items)
        for bid_id in bidder_ids:
            doc_id = main_doc_by_bidder.get(bid_id)
            if doc_id is None:
                continue
            try:
                rr = rescue_material_pages(doc_id)
                if rr.get("rescued"):
                    log.info("材料页 OCR 救援 doc=%s：%s 页", doc_id, rr.get("rescued"))
            except Exception as exc:  # noqa: BLE001 —— 救援失败不阻断评分
                log.warning("材料页 OCR 救援失败 doc=%s：%s", doc_id, exc)
            if _has_person:
                # 人员级：社保证明/经验证明章节的嵌入扫描图救援——个人参保证明
                # 常是"48 字标题 + 大图"同页，文本层看不到 → social_security 恒 False。
                try:
                    pr = rescue_person_material_pages(doc_id)
                    if pr.get("rescued"):
                        log.info("人员材料页 OCR 救援 doc=%s：%s 页", doc_id, pr.get("rescued"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("人员材料页 OCR 救援失败 doc=%s：%s", doc_id, exc)

    # ---- 证明材料页索引（count 逐案例核验与主观评审共用） ----
    _MAT_KEYWORDS = ("发票", "验收", "业主证明", "查询截图", "证书复印件", "承诺书", "营业执照",
                     "社保证明", "工作证明", "简历", "职称", "人员名单", "经验证明",
                     "项目经理", "运维负责人", "主要实施", "项目总监", "团队成员")
    material_hints: dict[int, str] = {}
    bid_pages: dict[int, list[tuple[int, str, str]]] = {}
    # 每家主文档解析警告（Document.warnings_json）：ISO 截图节门槛判
    # "unreadable vs confirmed_absent" 的依据（OCR 超限跳过/不可用/识别失败
    # → 页面可能根本没进文本层，0 分不能伪装成确认缺失）。取不到 → 空列表，
    # objective 侧退化 confirmed_absent。
    doc_warnings_by_bidder: dict[int, list] = {}
    with session_scope() as s:
        from app.db.models import DocumentPage

        for bid_id in bidder_ids:
            doc_id = main_doc_by_bidder.get(bid_id)
            if doc_id is None:
                material_hints[bid_id] = "（无）"
                continue
            # 评分前同步补规则层（rescue 改写页文本后的失效标签，毫秒级无 LLM）
            if config.material_index_enabled():
                try:
                    from app.material_index.service import ensure_index

                    ensure_index(doc_id)
                except Exception:  # noqa: BLE001 —— 分类是增强不是主链
                    log.warning("材料索引补判失败 doc=%s", doc_id, exc_info=True)
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
            from app.db.models import Document as _Document

            _warn_doc = s.get(_Document, doc_id)
            doc_warnings_by_bidder[bid_id] = list(_warn_doc.warnings_json or []) if _warn_doc else []
            hint_parts = [
                f"{kw}：第{'、'.join(str(p) for p in nos[:12])} 页"
                for kw, nos in pages.items() if nos
            ]
            _kw_hint = "；".join(hint_parts) if hint_parts else "（未见证明材料相关内容）"
            material_hints[bid_id] = _kw_hint
            # 类别化索引优先（四类分组 + 条目标签）；无有效标签回退关键词扫描
            cats = page_cats_by_bidder.get(bid_id) or {}
            if config.material_index_enabled() and cats:
                try:
                    from app.material_index.hints import material_index_hint

                    _mi_hint = material_index_hint(
                        [doc_id], {(doc_id, no): t for no, t, _s in page_rows}
                    )
                    if _mi_hint:
                        material_hints[bid_id] = _mi_hint + "；关键词线索：" + _kw_hint
                except Exception:  # noqa: BLE001 —— 提示文本增强失败不阻塞
                    log.debug("材料类别索引提示生成失败", exc_info=True)

    # rescue 改写页文本后重建类别映射（ensure_index 已在上面补判失效页）
    if config.material_index_enabled():
        try:
            (
                page_cats_by_bidder,
                page_cat_confs_by_bidder,
                chunk_cats_by_bidder,
                chunk_cat_confs_by_bidder,
                chunk_srcs_by_bidder,
            ) = _load_material_maps(doc_ids_by_bidder, main_doc_by_bidder, bidder_ids)
        except Exception:  # noqa: BLE001
            log.warning("材料类别映射重建失败", exc_info=True)

    # 业绩案例证据（确定性）：索引+页脚定位证据段 → PyMuPDF 表 → 案例 → 材料三态。
    # 每投标人只做一次，同一家所有业绩类计数项复用。
    # section 常驻（7.10 用友实证）：表解析失败时旧代码把已定位的证据段一并丢弃
    # → LLM 候选页拿不到 section → 材料扫描页（无锚点词）进不了送审窗口。
    case_evidence_by_bidder: dict[int, dict] = {}
    section_by_bidder: dict[int, tuple[int, int]] = {}
    if _has_perf:
        from app.db.models import Document as _Doc
        from app.evidence.case_evidence import (
            PageRec,
            classify_materials,
            link_case_blocks,
            locate_section,
            page_image_ratios,
            parse_case_table_at_section,
        )

        for bid_id in bidder_ids:
            rows = bid_pages.get(bid_id) or []
            if not rows:
                continue
            recs = [PageRec(no, t, src) for no, t, src in rows]
            sec = locate_section(recs)
            if not sec.found:
                continue
            section_by_bidder[bid_id] = (sec.start, sec.end)
            doc_id = main_doc_by_bidder.get(bid_id)
            pdf_path = None
            with session_scope() as s:
                if doc_id is not None:
                    pdf_path = s.execute(
                        select(_Doc.stored_path).where(_Doc.id == doc_id)
                    ).scalar_one_or_none()
            if not pdf_path:
                continue
            # 阶段3a（2026-10-30）：首窗 0 行时按表头就近重试（索引段首偏移几页的
            # 常见情形不再整条放弃表路径），公共辅助与 ingest_service 共用。
            table_rows = parse_case_table_at_section(
                pdf_path, sec, recs, config.case_table_scan_pages()
            )
            if not table_rows:
                continue
            blocks = link_case_blocks(table_rows, recs, sec)
            # 阶段4（2026-10-30）：材料索引页级强标签接入三态（tag_signal 先验）。
            # 关断（material_index/case_tag_prior 任一为 0）或查询失败 → tag_pages
            # 保持 None，classify_materials 行为与旧口径完全一致。
            tag_pages = None
            if config.material_index_enabled() and config.case_tag_prior_enabled():
                try:
                    from app.material_index.hints import page_material_tags

                    tag_pages = page_material_tags(
                        doc_id,
                        {no for b in blocks for no in b.pages},
                        {no: t for no, t, _s in rows},
                    )
                except Exception:  # noqa: BLE001 —— 标签先验失败不阻塞三态
                    tag_pages = None
            classify_materials(
                blocks, recs, page_image_ratios(pdf_path), tag_pages=tag_pages
            )
            case_evidence_by_bidder[bid_id] = {
                "section": sec, "rows": table_rows, "blocks": blocks,
            }

    # 每家投标人归一化全文：count 引文核验（回标书定位）与主观分引文闸门共用一份，
    # 只归一化一次（旧行为各处重复 normalize_no_ws，浪费且口径易漂移）。
    # OCR 标签必须先清再归一——否则引文"项目经理：蒋立华"回不到含标签的原文。
    bid_norm_by_bidder: dict[int, str] = {
        bid_id: _nnw(_clean_ocr(text_by_bidder.get(bid_id) or "")) for bid_id in bidder_ids
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

    # 承诺值抽取窗口（2026-11-02 run77 c54 实证）：偏离表通用条款"8小时"首匹配
    # 抢掉承诺函"4小时"。窗口 = 索引页码指针（"服务承诺…见投标文件（342-344）页"）
    # 解析出的目标页文本；指针缺失回退承诺章节（页首标题区命中 承诺函|服务承诺|
    # 响应承诺|售后…承诺）文本。都没解析到 → None（抽取侧回退全文语境分排序）。
    _RE_COMMIT_HEADING = re.compile(r"承诺函|服务承诺|响应承诺|售后.{0,6}承诺")

    def _window_pages_for(bid_id: int, c) -> set[int]:  # noqa: ANN001
        """评分项索引指针窗口的页码集合（P1-6 选段加成用；空集=无指针）。"""
        from app.evidence.case_evidence import PageRec, resolve_index_pages

        rows = bid_pages.get(bid_id) or []
        if not rows:
            return set()
        recs = [PageRec(no, t, src) for no, t, src in rows]
        name = str(c.get("name") or "")
        kws = [name] if name else []
        kws += [k for k in re.split(r"[-—–、（）()：:（）\s]+", name) if len(k) >= 2]
        for kw in kws:
            guess = resolve_index_pages(
                recs, kw, line_window=3,
                extend=config.window_extend_enabled(),
            )
            if guess.found:
                return {p for p in range(guess.start, guess.end + 1)}
        return set()

    def _value_window_for(bid_id: int, c) -> str | None:  # noqa: ANN001
        rows = bid_pages.get(bid_id) or []
        if not rows:
            return None
        pages = _window_pages_for(bid_id, c)
        if pages:
            picked = [t for no, t, _s in rows if no in pages]
            text = "\n".join(x for x in picked if x).strip()
            if text:
                return text
        # 回退：承诺章节页（页首标题区命中）
        picked = [t for _no, t, _s in rows if _RE_COMMIT_HEADING.search(t[:300])]
        return "\n".join(x for x in picked if x).strip() or None

    # P1-6 选段加成（2026-11-02 run77 c54）：索引指针窗口页 chunk ×1.3（优先
    # 送审），偏离表/偏差表页 chunk ×0.25（通用条款降权不移除——真承诺写在
    # 偏离表的标书保留入选资格）。与 chunk_srcs（split_page_chunks 同口径）对齐。
    _RE_DEV_PAGE = re.compile(r"技术偏离表|技术响应偏离表|偏离表|偏差表")

    def _score_scale_extra_for(bid_id: int, c) -> list[float] | None:  # noqa: ANN001
        srcs = chunk_srcs_by_bidder.get(bid_id)
        if not srcs:
            return None
        win = _window_pages_for(bid_id, c)
        main_id = main_doc_by_bidder.get(bid_id)
        dev_pages = {
            no for no, t, _s in (bid_pages.get(bid_id) or [])
            if _RE_DEV_PAGE.search(t[:600] or "")
        }
        out: list[float] = []
        for d, pno in srcs:
            w = 1.0
            if d == main_id:
                if win and pno in win:
                    w *= 1.3
                if pno in dev_pages:
                    w *= 0.25
            out.append(w)
        return out

    def _window_chunk_ids_for(bid_id: int, c) -> list[int]:  # noqa: ANN001
        """评分项索引指针窗口页对应的块下标（R1-2 强制进选段/进簇，2026-11-03）。

        ×1.3 软加权救不了低原始相似度的材料页（run78 ITSS：证书扫描件排不进
        top-3 簇静默落选）——窗口块必须保底入选。长度口径与 score_scale_extra
        同一假设（split_page_chunks 对齐），不符弃用不猜。
        """
        srcs = chunk_srcs_by_bidder.get(bid_id)
        if not srcs:
            return []
        win = _window_pages_for(bid_id, c)
        if not win:
            return []
        main_id = main_doc_by_bidder.get(bid_id)
        return [i for i, (d, pno) in enumerate(srcs) if d == main_id and pno in win]

    def _count_cell(args):  # noqa: ANN001
        """count 格计算（无 DB 写）：参数装配 → 确定性/LLM 核验 → 计分 → 审计。"""
        bid_id, c = args
        try:
            return _count_cell_impl(bid_id, c)
        except Exception as exc:  # noqa: BLE001 —— 单格失败不让整场失败
            if _is_fatal_llm_error(exc):
                raise
            log.warning("计数单元格失败 bid=%s criterion=%s：%s", bid_id, c.get("code"), exc)
            from types import SimpleNamespace

            fallback = SimpleNamespace(
                score=Decimal(0), detail=f"计数执行失败：{exc}",
                # llm_failed 是机器故障，不是"没抽到事实"——诚实标 cases_unverified
                # （2026-11-04），不得伪装 no_facts/confirmed_absent。
                unresolved="llm_failed", conflict=True, zero_reason="cases_unverified",
            )
            # audit 带 zero_reason：异常兜底的 0 分同样要过"score=0 必有理由"
            # 的审计约定（2026-10-29 审计 P2：曾因 audit=None 丢理由且 machine_status=pass）
            return bid_id, c, fallback, "llm_failed", True, {"zero_reason": "cases_unverified"}

    def _count_cell_impl(bid_id, c):  # noqa: ANN001
        facts = facts_by_bidder[bid_id]
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
            c_scoring = {
                **c_scoring,
                "cert_section_present": cert_sections.get(bid_id, (True, ""))[0],
                # ISO 截图节门槛的 unreadable/confirmed_absent 分离依据
                "doc_parse_warnings": doc_warnings_by_bidder.get(bid_id) or [],
            }
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
            # 通用按件计数（"每提供一项…得 N 分"）：计数对象是条目本身
            or params.get("field") == "items"
            or (
                params.get("field") != "certificates"
                and any(k in str(c.get("name") or "") for k in ("业绩", "案例", "经验"))
            )
        )
        case_stats: dict = {}
        _ce = case_evidence_by_bidder.get(bid_id)
        if _ce and is_performance and not params.get("person_scope"):
            # 确定性优先（业绩类）：业绩表 → 案例（名/金额/日期/表页）+ 材料三态。
            # 这是"从标书解析评分依据"的主路径，不依赖 LLM 猜案例/金额。
            c_scoring = {**c_scoring, "verified_cases": _cases_from_blocks(_ce["blocks"])}
            case_stats = {
                "method": _ce["section"].method,
                "section": [_ce["section"].start, _ce["section"].end],
                "offset": _ce["section"].offset,
                "case_count": len(_ce["rows"]),
            }
        elif (
            config.auto_mode()
            and gateway is not None
            and is_performance
        ):
            cases = _llm_verify_cases(
                gateway, c, requirement_text, artifacts_list,
                material_hints.get(bid_id),
                bid_pages.get(bid_id) or [], data_class,
                bid_norm=bid_norm_by_bidder.get(bid_id),
                stats_out=case_stats,
                page_cats=page_cats_by_bidder.get(bid_id),
                prefer={"qualification", "commercial"},
                section=section_by_bidder.get(bid_id),
            )
            if cases:
                c_scoring = {**c_scoring, "verified_cases": cases}
            # 批失败语义（2026-11-04 诚实化）：LLM 核验批失败 = 机器故障，
            # 案例清单可能不完整——注入 score_count 标 cases_unverified，
            # 不得伪装"确认未提供"（no_facts/confirmed_absent）。
            if case_stats.get("batches_failed"):
                c_scoring["case_batches_failed"] = case_stats["batches_failed"]
                c_scoring["case_batches_total"] = case_stats.get("batches_total", 0)
        # 人员级社保判定确定性覆盖（2026-10-23 run 49 实证：LLM 方差让
        # 先数 c60 0→6、龙即 6→4.5 来回摆）。名单/单位参保证明是可机械
        # 判定的：person_name 在社保材料页 → True；单位专用参保证明覆盖
        # 全员 → True；逐人缴费名单不含该人 → False。
        if params.get("person_scope") and c_scoring.get("verified_cases"):
            _overlay_social_security(
                c_scoring["verified_cases"], bid_pages.get(bid_id) or []
            )
            # 成员级 work_proof 确定性覆盖（简历表行不算业绩证明）
            if str(params.get("person_scope")) == "member":
                _overlay_work_proof(
                    c_scoring["verified_cases"], bid_pages.get(bid_id) or []
                )
            # 负责人 experience_proof 文档级救援（五年证明在独立人员章节，
            # 案例核验链不上 → False 误杀材料齐全的案例）
            if str(params.get("person_scope")) == "leader":
                _overlay_experience_proof(
                    c_scoring["verified_cases"], bid_pages.get(bid_id) or []
                )
                # 负责人情况表宽松口径（2026-11-02）：通用「项目负责人情况表」
                # 含该人姓名 → work_proof 升 True（用友 c47：吴丽君 p73 情况表）
                _overlay_leader_table_proof(
                    c_scoring["verified_cases"], bid_pages.get(bid_id) or []
                )
        # P1-E 新计分原语：与业绩案例三态机器无关的确定性评分，直接分发。
        # CountResult 同构 → 下面的 unresolved/doubt/落库路径完全复用。
        _engine = params.get("engine")
        if _engine in ("value_tier", "value_linear"):
            # 承诺值抽取窗口：索引指针页/承诺章节文本（防偏离表通用条款首匹配抢值）
            _vw = _value_window_for(bid_id, c)
            if _vw:
                c_scoring = {**c_scoring, "value_window_text": _vw}
        if _engine in ("count_tier", "attribute_tier") or _field == "certificates":
            # 证书背书三态（P0-5，2026-11-03 R3-4 扩到普通 certificates 计数）：
            # count_tier/attribute_tier/证书计数的软证据闸门（逐本定位复印件页）
            from app.engine.objective import classify_cert_backing

            _pages2 = [(src, t) for _p, t, src in (bid_pages.get(bid_id) or [])]
            _backing = classify_cert_backing(
                facts, _pages2, cert_sections.get(bid_id)
            )
            if _backing:
                c_scoring = {**c_scoring, "cert_backing": _backing}
        if _engine in _P1E_ENGINES:
            result = _P1E_ENGINES[_engine](c_scoring, params, facts)  # type: ignore[arg-type]
        else:
            result = score_count(c_scoring, params, facts)  # type: ignore[arg-type]
        # unresolved 以结构化键落库：下游（报告疑问清单）直接判读，
        # 不再靠匹配 detail 的中文措辞——那种耦合已经断过一次。
        # "机器没抽到事实"不是 0 分结论：不进总分（final=None），报告标未定分。
        # conflict（证明材料链缺失/部分缺失）分数照算但保留疑问态。
        unresolved = bool(result.unresolved)
        # 零分理由（W1-C）：score=0 时落 ai_raw_json["zero_reason"]；
        # "unreadable"（机器不可见）强制 need_human + conflict_flagged——
        # 0 分维持规则硬口径，但不得伪装成确定性评审结论（run 60 实证）。
        zero_reason = getattr(result, "zero_reason", None)
        if result.score == 0 and zero_reason is None:
            # P1E 引擎（count_tier/value_tier 等）与异常兜底未逐一设置 zero_reason →
            # 从 unresolved 统一映射：有任何 unresolved（含 llm_failed）都是机器侧
            # 原因 → no_facts/cases_unverified（诚实细分，2026-11-04：核验未做完
            # 不得并进"没抽到事实"）；无 unresolved 的 0 是计数有依据 → confirmed_absent。
            # 保证"score=0 必有理由"约定全覆盖。
            if result.unresolved == "cases_unverified":
                zero_reason = "cases_unverified"
            elif result.unresolved:
                zero_reason = "no_facts"
            else:
                zero_reason = "confirmed_absent"
        unreadable_zero = zero_reason in ("unreadable", "cases_unverified") and result.score == 0
        doubt = result.conflict or unresolved or unreadable_zero
        # 审计留痕：把逐案例核验结果 + 候选页统计落 ai_raw_json，否则判"照抄/
        # 未在标书定位"后无法复盘是抽取错还是闸门误杀（2026-10-21 实证为空）。
        # 总批失败的格（无 cases 但有 case_stats）也要落 meta——复盘必须有据。
        audit = None
        _vc = c_scoring.get("verified_cases")
        if _vc or case_stats:
            audit = {
                "verified_cases": [
                    {
                        k: (str(v)[:160] if isinstance(v, str) else v)
                        for k, v in cs.items()
                        if k in _AUDIT_CASE_KEYS
                    }
                    for cs in _vc
                ],
                "meta": case_stats,
            }
        if result.score == 0 and (zero_reason or audit):
            audit = dict(audit or {})
            audit["zero_reason"] = zero_reason
        # 双分值可复算（2026-11-02）：剔除未核验案例的分数落审计，人工一眼算账
        _sv = getattr(result, "score_verified_only", None)
        if _sv is not None:
            audit = dict(audit or {})
            audit["score_verified_only"] = str(_sv)
        # R0 客观路由留痕（2026-11-03）：依据是包里写的还是执行时路由的，审计可见
        if c.get("_objective_route"):
            audit = dict(audit or {})
            audit["objective_route"] = c["_objective_route"]
        return bid_id, c, result, unresolved, doubt, audit

    # Phase1：并行计算（业绩表快路径不走 LLM；LLM 逐案例核验 4 并发→8 并发）
    count_cells = [(bid_id, c) for bid_id in bidder_ids for c in count_items]
    count_results = _parallel_map(_count_cell, count_cells, ctx=ctx, message_fmt="案例核验 {done}/{total}")
    # Phase2：串行落库（SQLite 单写者）
    for bid_id, c, result, unresolved, doubt, audit in count_results:
        _warn_entry, _unreadable_zero = _count_zero_warn_entry(result, audit)
        _unverified_n = getattr(result, "unverified_counted", 0) or 0
        _upsert_score(
            run_id, bid_id, c,
            machine_score=result.score,
            # unreadable/cases_unverified 零分：机器不可见/核验未做完 ≠ 确认缺失
            # → 强制转人工（不因全自动模式被收敛成 auto_confirmed——doubt=True
            # 已保住复核态）。含未核验案例（>0）同理转人工：分数进了总分但口径
            # 未定（双分值）。批失败（unresolved=cases_unverified）无论分数
            # 大小都转人工：案例清单可能不完整。
            machine_status="need_human"
            if (_unreadable_zero or _unverified_n or result.unresolved == "cases_unverified")
            else "pass",
            review_status="conflict_flagged" if doubt else "pending",
            # 全自动化评分（用户要求零未定分）：计分项**一律写分**。
            # unresolved 只作疑问标记（落 warnings 供报告判读），不再决定弃评；
            # 依据提取不到的兜底 0 分同样进总分，并在 detail/warnings 里说明。
            final_score=result.score,
            warnings_json=[_warn_entry],
            doubt=doubt,
            ai_raw_json=audit,
        )
        stats["objective"] += 1

    # ---- ③+ match 判断项：不计分，落三态判定记录 ----
    # 法律包的 8 条形式/响应性检查（score_type=match）：此前被静默跳过。
    # 不传 machine_score/final_score → 两者保持 NULL（判断项不打分）；
    # 非否决 match 的 on_fail=need_human → 绝不产生 recommend_reject。
    def _match_cell(args):  # noqa: ANN001
        bid_id, c = args
        try:
            return _eval_veto(  # type: ignore[arg-type]
                c, facts_by_bidder[bid_id],
                on_fail="recommend_reject" if c.get("is_veto") else "need_human",
                text=text_by_bidder.get(bid_id), gateway=gateway, data_class=data_class,
                scan_hint=scan_hints.get(bid_id, False),
                chunk_cats=chunk_cats_by_bidder.get(bid_id),
                chunk_cat_confs=chunk_cat_confs_by_bidder.get(bid_id),
            )
        except Exception as exc:  # noqa: BLE001 —— 单格失败不让整场失败
            if _is_fatal_llm_error(exc):
                raise
            log.warning("判断项单元格失败 bid=%s criterion=%s：%s", bid_id, c.get("code"), exc)
            return {"verdict": "need_human", "message": f"判断项执行失败：{exc}"}

    match_cells = [(bid_id, c) for bid_id in bidder_ids for c in match_items]
    match_results = _parallel_map(_match_cell, match_cells, ctx=ctx, message_fmt="判断项 {done}/{total}")
    for (bid_id, c), result in zip(match_cells, match_results):
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

        # 人员经验材料**存在性**统计（细则文本驱动）：「五年及以上经验证明」
        # 「近六个月社保」是招标明示的材料门槛——判定依据是**材料在不在**
        # （经验证明/社保证明类页面出现与否），不数页数。旧实现的 <3 页/<16 页
        # 阈值是按旧项目 4 家校准的拟合产物，换项目即误杀（2026-10-26 run 54
        # 实证），已删除；claim 句（"具有五年工作经验"）不是证明材料。
        # **三态**（2026-11-02 run77 c48 实证）：True=检出 / False=可读文本层
        # 确认无（且无扫描页遮挡）/ None=机器看不见（有扫描/OCR 页，不能确认
        # 缺失）。旧二态把"没扫到"当"确认没有"→ 清零误杀。正则补 简历|职称证|
        # 人员情况表|履历（c48 实证：p121-122"简历+职称证书"旧正则匹配不到），
        # 并消费 material_tags 页级标签作存在性信号（目录式页被跳过条件误跳时的补救）。
        person_stats: dict[int, dict[str, bool | None]] = {}
        with session_scope() as s:
            from app.db.models import DocumentPage, MaterialTag

            for bid_id in bidder_ids:
                doc_id = main_doc_by_bidder.get(bid_id)
                exp_mat: bool | None = None
                social_mat: bool | None = None
                if doc_id is not None:
                    exp_hit = social_hit = False
                    has_scan = False
                    for ptext, psrc in s.execute(
                        select(DocumentPage.text, DocumentPage.source)
                        .where(DocumentPage.document_id == doc_id)
                    ):
                        t = ptext or ""
                        if (psrc or "text") != "text":
                            has_scan = True  # 扫描/OCR 页 → 文本层不可信，缺席不可确认
                        if "【证明材料】" in t or t.count("....") >= 3:
                            continue  # 目录/要求页不算材料页
                        if re.search(
                            r"工作经验证明|工作证明|工作经历证明|劳动合同|聘书|任职证明"
                            r"|工作履历|简历|职称证|职称|人员情况表|履历",
                            t,
                        ):
                            exp_hit = True
                        if re.search(r"社会保险|社保|参保缴费|参保人员|参保证明|缴费证明", t):
                            social_hit = True
                    if not (exp_hit and social_hit):
                        # material_tags 页级标签存在性信号（补救目录式页误跳）
                        for (tags_json,) in s.execute(
                            select(MaterialTag.tags_json).where(
                                MaterialTag.document_id == doc_id,
                                MaterialTag.seg_kind == "page",
                            )
                        ):
                            joined = "、".join(str(x) for x in (tags_json or []))
                            if re.search(r"工作|经验证明|简历|职称|人员", joined):
                                exp_hit = True
                            if re.search(r"社保|参保", joined):
                                social_hit = True
                    # 三态归位：命中→True；未命中且文本层可读（无扫描页）→False；
                    # 未命中但有扫描页 → None（机器看不见，不确认缺失）
                    exp_mat = True if exp_hit else (False if not has_scan else None)
                    social_mat = True if social_hit else (False if not has_scan else None)
                person_stats[bid_id] = {"exp_material": exp_mat, "social_material": social_mat}

        use_hierarchical = config.review_mode() == "hierarchical"
        subj_done = 0
        subj_total = len(subjective_items) * len(bidder_ids)
        # 子项引文闸门的核验器：引文必须真实存在于该投标人的文档文本中
        # （空白不敏感；<12 字不算引文）。复用上面预计算的 bid_norm_by_bidder。
        verifiers: dict[int, object] = {}
        for bid_id in bidder_ids:
            norm_text = bid_norm_by_bidder.get(bid_id) or ""

            def _verify(quote: str, _norm=norm_text) -> bool:
                # 引文核验：与计数路径同一语义——精确 → 前缀梯 → 去逗号变体。
                # 旧实现只做精确子串匹配，OCR 表格串行/货币符号差异会把真实子项
                # 引文误判为 0（2026-10-22 实证：主观分多项子项分被误杀归 0）。
                if not quote or not _norm:
                    return False
                q = _nnw(quote)
                if len(q) < 12:
                    return False
                if q in _norm:
                    return True
                for _L in config.PREFIX_FALLBACK_LENGTHS:
                    if len(q) >= _L and q[:_L] in _norm:
                        return True
                stripped = q.replace(",", "").replace("，", "")
                if stripped != q:
                    if stripped in _norm:
                        return True
                    for _L in config.PREFIX_FALLBACK_LENGTHS:
                        if len(stripped) >= _L and stripped[:_L] in _norm:
                            return True
                return False

            verifiers[bid_id] = _verify

        # Phase 1：并行 LLM 调用（ThreadPoolExecutor，参照 subjective.py:446 committee 模式）。
        # 旧实现 for bid_id: for c: 严格串行，288 次调用（4家×24项×3簇）排队 ~2.5h。
        # 并行化后 4 并发 × 每格 1 次簇调用 ≈ 36 分钟（网关信号量 BID_EVAL_LLM_CONCURRENCY 控并发）。
        from concurrent.futures import ThreadPoolExecutor
        import threading as _threading

        _prog_lock = _threading.Lock()
        _prog_done = [0]

        def _score_cell(args):  # noqa: ANN001
            bid_id, c = args
            _zero_reason = _forced_zero_reason(
                str(c.get("name") or ""), person_stats.get(bid_id, {}), c
            )
            if _zero_reason is not None:
                # 确定性短路：材料**确认**缺失（三态 False）才跳过 LLM——Phase2 会
                # 权威置 0，3 次簇调用跑完也是白跑（省 0-40% 主观分 LLM 调用）。
                # 注意返回形状必须与主路径一致（code 而非 dict）——单出口统一在
                # 函数末尾，防止形状漂移（审查实锤：dict 当键 → unhashable 崩整场）。
                suggestion, committee = SubjectiveSuggestion(
                    status="ai_suggested", score=Decimal(0), level_label=None,
                    reasoning=_zero_reason, evidence=[], warnings=[],
                    zero_reason="confirmed_absent",
                ), None
            else:
                hint = material_hints.get(bid_id)
                # 材料存在性 None（机器看不见）→ 提示并入 LLM 输入（三态哲学：
                # 没看见≠不符合，让 LLM 带提示判档而不是被硬门槛短路清零）
                _miss = _missing_material_hint(
                    str(c.get("name") or ""), person_stats.get(bid_id, {}), c
                )
                if _miss:
                    hint = f"{hint}；{_miss}" if hint else _miss
                _cc = chunk_cats_by_bidder.get(bid_id)
                _cf = chunk_cat_confs_by_bidder.get(bid_id)
                # P1-6 选段加成：索引指针窗口页优先 / 偏离表通用条款降权
                _sce = _score_scale_extra_for(bid_id, c)
                _wci = _window_chunk_ids_for(bid_id, c) or None
                try:
                    if use_hierarchical:
                        suggestion, committee = suggest_score_hierarchical(
                            gateway, c, text_by_bidder[bid_id], data_class=data_class,
                            material_pages=hint, verify_quote=verifiers.get(bid_id),
                            chunk_cats=_cc, chunk_cat_confs=_cf,
                            score_scale_extra=_sce, must_include=_wci,
                        )
                        if suggestion is None:
                            suggestion, committee = suggest_score_committee(
                                gateway, c, text_by_bidder[bid_id], data_class=data_class,
                                material_pages=hint, verify_quote=verifiers.get(bid_id),
                                chunk_cats=_cc, chunk_cat_confs=_cf,
                                score_scale_extra=_sce, must_include=_wci,
                            )
                    else:
                        suggestion, committee = suggest_score_committee(
                            gateway, c, text_by_bidder[bid_id], data_class=data_class,
                            material_pages=hint, verify_quote=verifiers.get(bid_id),
                            chunk_cats=_cc, chunk_cat_confs=_cf,
                            score_scale_extra=_sce, must_include=_wci,
                        )
                except Exception as exc:  # noqa: BLE001 —— 单格失败不让整场失败
                    if _is_fatal_llm_error(exc):
                        raise  # 配额/预算/涉密必须 fail-fast（与其他 worker 同口径）
                    log.warning("主观分单元格失败 bid=%s criterion=%s：%s", bid_id, c.get("code"), exc)
                    suggestion, committee = SubjectiveSuggestion(
                        status="need_human", warnings=[f"llm_failed: {exc}"],
                    ), None
            with _prog_lock:
                _prog_done[0] += 1
                if ctx is not None:
                    ctx.advance(
                        message=f"主观分评审 {_prog_done[0]}/{subj_total}（{config.review_mode()}）"
                    )
            return bid_id, c.get("code"), suggestion, committee

        # 并发度 = min(格数, BID_EVAL_LLM_CONCURRENCY)：Phase1 无 DB 写，网关
        # 信号量（服务器 8）是唯一真闸门；旧硬顶 4 让 8 并发静默减半。
        # 每格内部 K=3 簇仍串行（suggest_score_hierarchical 自身逻辑不变）。
        _cells = [(bid_id, c) for bid_id in bidder_ids for c in subjective_items]
        _cell_workers = min(len(_cells) or 1, max(1, config.llm_max_concurrency()))
        _results: dict[tuple[int, str], tuple] = {}
        with ThreadPoolExecutor(max_workers=_cell_workers) as _pool:
            for bid_id, c_code, suggestion, committee in _pool.map(_score_cell, _cells):
                _results[(bid_id, c_code)] = (suggestion, committee)

        # Phase 2：串行后处理（forced_zero + DB 写入——SQLite 单写者）
        for bid_id in bidder_ids:
            for c in subjective_items:
                suggestion, committee = _results.get(
                    (bid_id, c.get("code")),
                    (SubjectiveSuggestion(status="need_human", warnings=["llm_failed"]), None),
                )

                # 人员经验材料门槛（确定性，规则执行）：与 Phase1 短路共用
                # _forced_zero_reason（单一来源）；Phase2 是唯一权威写入口。
                cname = str(c.get("name") or "")
                ps = person_stats.get(bid_id, {})
                forced_zero = _forced_zero_reason(cname, ps, c)
                if forced_zero is not None:
                    suggestion = SubjectiveSuggestion(
                        status="ai_suggested",
                        score=Decimal(0),
                        level_label=None,
                        reasoning=forced_zero,
                        evidence=[],
                        warnings=[forced_zero] + list(suggestion.warnings),
                        zero_reason="confirmed_absent",
                    )

                _persist_subjective(run_id, bid_id, c, suggestion, main_doc_by_bidder.get(bid_id), stats,
                                    committee=committee)
                stats["subjective"] += 1

    # ---- ⑤ 价格分（P1-C 后移至此：门槛需技术/商技合计，必须等计数+主观算完） ----
    if price_items and "price" in stages:
        if ctx:
            ctx.advance(message="计算价格分")
        price_criterion = price_items[0]
        formula = dict(price_criterion.get("formula") or {})
        formula["max_score"] = price_criterion.get("max_score")

        # 门槛输入：按 gate_on 类别从本 run 的 score_records 汇总实际得分与分母。
        # 该类别任一评分项缺终分（如主观项 pending/未跑）→ gate_value=None →
        # 引擎不执行门槛（机器看不见≠不符合，explanation 会注明人工复核）。
        _ext = formula.get("params_json") or {}
        _gate = _ext.get("benchmark_gate") if isinstance(_ext.get("benchmark_gate"), dict) else None
        gate_by_bid: dict[int, tuple[Decimal | None, Decimal | None]] = {}
        if _gate:
            _on = _gate.get("gate_on") or "tech"
            _cats = (
                {"tech": ("technical",), "tech_qualification": ("technical",),
                 "biz_tech": ("technical", "commercial"),
                 "sample_or_composite": ("technical", "commercial")}
            ).get(_on, ("technical",))
            _want = [c for c in criteria
                     if c.get("category") in _cats and c.get("max_score") is not None]
            _basis = (
                sum((Decimal(str(c["max_score"])) for c in _want), Decimal(0))
                if _want else None
            )
            _codes = {c.get("code") for c in _want}
            with session_scope() as s:
                from app.db.models import Criterion as CriterionRow

                _rows = s.execute(
                    select(ScoreRecord.bidder_id, ScoreRecord.final_score, CriterionRow.code)
                    .join(CriterionRow, CriterionRow.id == ScoreRecord.criterion_id)
                    .where(ScoreRecord.run_id == run_id)
                ).all()
            _sums: dict[int, dict[str, Decimal]] = {}
            _got: dict[int, set[str]] = {}
            for _bid, _fs, _code in _rows:
                if _code in _codes and _fs is not None:
                    _sums.setdefault(_bid, {})[_code] = Decimal(str(_fs))
                    _got.setdefault(_bid, set()).add(_code)
            for bid_id in bidder_ids:
                if _got.get(bid_id) == _codes and _basis is not None:
                    gate_by_bid[bid_id] = (sum(_sums[bid_id].values(), Decimal(0)), _basis)
                else:
                    gate_by_bid[bid_id] = (None, _basis)

        bids: list[PriceBid] = []
        with session_scope() as s:
            for bid_id in bidder_ids:
                bidder = s.get(Bidder, bid_id)
                excluded = (
                    bid_id in excluded_bidders
                    or bidder.status in ("recommend_reject", "rejected")
                    or bid_id in benchmark_ineligible  # 确定性资格硬 fail → 剔基准价
                )
                # 报价缺失/0 不能崩掉整个 run：价格引擎会按"无效报价"记 0 并排除基准
                price_dec = (
                    Decimal(str(bidder.bid_price))
                    if bidder.bid_price is not None
                    else Decimal(0)
                )
                gv, gb = gate_by_bid.get(bid_id, (None, None))
                bids.append(PriceBid(bid_id, bidder.name, price_dec, excluded,
                                     gate_value=gv if _gate else None,
                                     gate_basis=gb if _gate else None))
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

    # ---- ⑥ 收尾 ----
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
def _case_max_batches() -> int:
    """多段调用上限（超出按锚点页优先截断并记警告）。

    接线 env `BID_EVAL_CASE_MAX_BATCHES`（config.case_max_batches()）——旧实现
    硬编码 4 而 env 是死配置（全库无调用点），部署设 2 完全不生效（2026-10-27 实证）。
    批间已并行，批数上限不再是墙钟负担。
    """
    return max(1, config.case_max_batches())
_ECHO_NGRAM_W = 12  # 判"要求复述"的 n-gram 窗口（与 MIN_QUOTE_CHARS 同阶）
_ECHO_SHADOW_FRACTION = 0.6  # 引文被判"照抄要求"的覆盖率阈值
_ECHO_PAGE_FRACTION = 0.4  # 整页被判"要求复述页"的覆盖率阈值
_ECHO_PAGE_SHORT_CHARS = 300  # 短页另用"最长连续 echo 段"判活的字符上限
_ECHO_PAGE_MIN_RUN = 60  # 短页最长连续 echo 段阈值

# OCR 格式标签清理提为共享模块（app/parsing/ocr_tags.py）——招标编译链路同样
# 需要（纯扫描细则页的标签会污染选段/LLM 抽取/quote 核验）。
from app.parsing.ocr_tags import clean_ocr_tags as _clean_ocr  # noqa: E402


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
    person_scope: str = "",
    *,
    page_cats: dict[int, str] | None = None,
    prefer: set[str] | None = None,
    section: tuple[int, int] | None = None,
) -> list[list[tuple[int, str]]]:
    """业绩/人员案例核验的候选页 → 剔除"要求复述页" → 按预算切批。

    旧实现把 `facts.text`（全部文档拼接）关键词选段后只送 15k 字符——几百页
    标书里案例页根本进不了上下文，团队成员经验项四家全 0（2026-10-21 实证）。
    这里按"锚点词命中页 ±1 页"圈定候选，再按预算切批多段调用。

    `echo_n`（归一化的 要求原文 source_quote + required_evidence）非空时，逐页
    剔除"要求复述页"：投标人常把评分规则整段抄进标书的索引/响应对照表页，模型
    会被它带偏、拿要求文本当引文（2026-10-21 实证：b13 第 95 页复述规则、真业绩
    在第 126 页，两页同送 → 引文全被判照抄 → 未定分）。剔除后模型只看真证据页。

    `person_scope` 非空（leader/member）时：排除项目合同页（含"合同金额+甲方/乙方"
    但无人员姓名的页），只保留人员证据页（含"项目经理：/运维负责人：/人员名单"等），
    避免 LLM 被项目合同头带偏（2026-10-22 实证：c59/c60 全 0 分）。
    """
    import re as _re

    anchor = _re.compile(r"合同|项目名称|甲方|乙方|发票|验收|中标通知书|业绩|项目团队|负责人|简历|工作证明|社保|职称|人员名单|经验证明|项目经理|运维负责人|主要实施|项目总监|团队成员")

    # 人员证据模式（清理 OCR 标签后匹配）："角色：姓名"或"角色 姓名"
    _person_re = _re.compile(
        r"(?:项目经理|运维负责人|主要实施|项目总监|团队成员|人员名单|项目负责人)"
        r"[：:\s]*(?:[一-鿿]{2,5})"
    )
    # 项目合同页特征（清理后）：合同金额 + 甲方/乙方
    _contract_re = _re.compile(r"(?:合同金额|合同价款|中标金额)[：:：]?\s*[\d,.万元]+")
    _parties_re = _re.compile(r"(?:甲方|乙方|发包方|承包方|采购人|供应商)")

    def _cleaned(t: str) -> str:
        return _clean_ocr(t or "")

    hits = [i for i, (_no, t, _s) in enumerate(pages) if anchor.search(_cleaned(t))]
    idx: set[int] = set()
    for i in hits:
        idx.update({i - 1, i, i + 1})
    anchor_idx = {i for i in idx if 0 <= i < len(pages)}
    # section 感知（7.10 用友实证）：确定性业绩表解析失败时，锚点±1 圈不住材料页
    # （扫描页无锚点词）→ LLM 视野里没有证据页。section 内全部页并入候选，锚点页
    # 仍排前（排序键），4 批预算与 pages_omitted 留痕不变。
    if section:
        lo, hi = section
        for i, (no, _t, _s) in enumerate(pages):
            if lo <= no <= hi:
                idx.add(i)
    ordered = sorted(i for i in idx if 0 <= i < len(pages))
    n_candidates = len(ordered)

    if person_scope:
        # 人员级：只保留含人员证据的页 + 社保/工作证明/经验证明/简历页
        _person_evidence = _re.compile(
            r"项目经理|运维负责人|主要实施|项目总监|团队成员|人员名单|项目负责人"
            r"|社保|社会保险|参保|缴费|工作证明|经验证明|简历|职称"
        )
        # 社保/经验证明/劳动合同材料页优先（真实社保在 200+ 页的人员资料区，
        # 按文档顺序填预算会把它挤出送审窗口）
        _person_mat = _re.compile(
            r"社保|参保|缴纳|经验证明|工作经验证明|劳动合同|社会保障号|缴费明细"
        )
        # 材料标题页：人员业绩的证明材料（发票/验收/中标通知书/业主证明/合同复印件
        # 扫描页）**必须**送审——7.10 实证：旧词表把这些页整页排除，LLM 视野里 0 张
        # 材料页 → 逐案例判"缺证明材料"（航天拓扑业绩段 32 页仅 3 页进送审、派克
        # 业绩段 44-78 共 0 页）。只放行带**明确材料标题**的页；纯合同头页（合同
        # 金额+甲方乙方、无人员词）仍排除——c59/c60 误带偏回归防线不变。
        _person_material_page = _re.compile(
            r"发票扫描|发票|验收报告|验收单|中标通知书|业主证明|用户证明"
            r"|合同复印件|合同扫描"
        )
        kept = []
        for i in ordered:
            t = _cleaned(pages[i][1])
            if not (_person_evidence.search(t) or _person_material_page.search(t)):
                continue  # 无人物证据且非材料标题页 → 排除（纯项目合同页等）
            kept.append(i)
        # 类内页（材料类别标签命中 prefer）优先于人员材料词命中，装批截断时保住类内页；
        # 排序层：社保/经验证明 > 材料标题页 > 其余人员证据页
        _prefer = prefer or set()
        kept.sort(key=lambda i: (
            0 if (page_cats or {}).get(pages[i][0]) in _prefer else 1,
            0 if _person_mat.search(_cleaned(pages[i][1])) else
            (1 if _person_material_page.search(_cleaned(pages[i][1])) else 2),
            i,
        ))
        ordered = kept
    else:
        # 项目级：锚点页 → 类内页 → 人员证据页 → 其余（让 LLM 优先看到证据页；
        # section 并入的非锚点页排在锚点页之后）
        _prefer = prefer or set()
        ordered.sort(key=lambda i: (
            0 if i in anchor_idx else 1,
            0 if (page_cats or {}).get(pages[i][0]) in _prefer else 1,
            0 if _person_re.search(_cleaned(pages[i][1])) else 1,
            i,
        ))

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
    # 自适应送审（2026-10-30）：旧实现按固定批数截断（run 72 实证：87 候选页只
    # 送审 71 页、16 页从没给核验模型看，模型"没找到材料"实为没看到）。改为按
    # 内容算所需批数、只在超硬顶（ceiling=12）才截断；地板仍是 case_max_batches。
    import math as _math

    _total_chars = sum(len(pages[i][1] or "") for i in ordered)
    _needed = _math.ceil(_total_chars / _CASE_BATCH_CHARS) if _total_chars else 1
    _max_batches = max(
        _case_max_batches(),
        min(_needed, config.case_max_batches_ceiling()),
    )
    for i in ordered:
        no, text = pages[i][0], pages[i][1] or ""
        if cur and cur_chars + len(text) > _CASE_BATCH_CHARS:
            batches.append(cur)
            cur, cur_chars = [], 0
            if len(batches) >= _max_batches:
                break
        cur.append((no, text))
        cur_chars += len(text)
    if cur and len(batches) < _max_batches:
        batches.append(cur)
    covered = sum(len(b) for b in batches)
    if stats_out is not None:
        stats_out["pages_candidates"] = n_candidates
        stats_out["pages_sent"] = covered
        stats_out["pages_dropped_echo"] = dropped_nos
        stats_out["batches_sent"] = len(batches)
        stats_out["batches_total"] = len(batches)
        # 截断透明化：未送审页数必须可审计（静默截断会让核验结论看起来完整）
        stats_out["pages_omitted"] = max(0, len(ordered) - covered)
    if len(batches) >= _max_batches and covered < len(ordered):
        log.warning(
            "案例核验候选页超出 %d 批上限，已按锚点页优先截断（候选 %d 页，送审 %d 页）",
            _max_batches, len(ordered), covered,
        )
    return batches


def _cases_from_blocks(blocks: list) -> list[dict]:
    """确定性案例块 → score_count 的 verified_cases（带三态材料与评分依据）。"""
    out: list[dict] = []
    for b in blocks:
        r = b.row
        st = b.status or {}
        out.append(
            {
                "name": r.name,
                "amount_yuan": str(r.amount_yuan) if r.amount_yuan is not None else None,
                "sign_date": r.sign_date,
                "contract": st.get("contract"),
                "invoice": st.get("invoice"),
                "acceptance": st.get("acceptance"),
                "quote": r.name,  # 表行本身即评分依据
                "quote_source": "table_row",
                "case_source": "table",
                "table_page": r.page,
                "material_pages": b.pages_by_material,
                "material_reasons": b.reasons,
                "sanitized": "ok",
                "page_hint": r.page,
            }
        )
    return out


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
    *,
    page_cats: dict[int, str] | None = None,
    prefer: set[str] | None = None,
    section: tuple[int, int] | None = None,
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
    person_scope_raw = str((c.get("params") or {}).get("person_scope") or "")
    batches = _case_candidate_pages(
        pages, echo_n=echo_n, stats_out=stats_out, person_scope=person_scope_raw,
        page_cats=page_cats, prefer=prefer, section=section,
    )
    if not batches:
        return None

    class CaseList(BaseModel):
        cases: list[dict] = Field(default_factory=list)

    art_list = [a.strip() for a in artifacts_list if a.strip()] or [requirement_text]
    person_scope = person_scope_raw or "（不区分人员）"
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _one_batch(args):  # noqa: ANN001
        batch_no, batch = args
        text = "\n".join(f"【第 {no} 页】\n{_clean_ocr(t)}" for no, t in batch)
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
            # 负责人宽松口径 CLAUSE 只对 leader 拼接（条款只讲负责人佐证；
            # 开关/范围天然进缓存键——system 全文参与键，leader 格换键、
            # 其余格照常命中缓存，不作废无关调用）
            _system = prompts.MATERIAL_COUNT_SYSTEM_PROMPT
            if person_scope_raw == "leader" and config.leader_table_proof_enabled():
                _system += prompts.LEADER_TABLE_PROOF_CLAUSE
            out, _res = gateway.chat_json(
                _system, user, CaseList,
                data_class=data_class,
            )
            return batch_no, batch, out.cases, None
        except LLMError as exc:
            msg = str(exc)
            if any(k in msg for k in ("402", "insufficient", "quota", "30001", "arrears")):
                raise  # 配额类 fail-fast（穿透 pool）
            return batch_no, batch, [], exc

    # 批间并行（2026-10-27 提速）：单批 15k 上下文的 LLM 调用 ≈135-150s，4 批串行
    # = 每格 9-10 分钟的阶段墙钟主力。网关信号量是唯一真闸门；**按批序归并**
    # 保证 seen 去重顺序与串行一致 → merged 逐字节不变（不变式 I3）。
    import threading as _threading
    from concurrent.futures import ThreadPoolExecutor

    items = list(enumerate(batches, start=1))
    workers = min(len(items), max(1, config.llm_max_concurrency()))
    if workers <= 1:
        batch_results = [_one_batch(it) for it in items]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            batch_results = list(pool.map(_one_batch, items))

    for batch_no, batch, cases, exc in batch_results:
        if exc is not None:
            log.warning("案例核验第 %d/%d 批失败：%s", batch_no, len(batches), exc)
            if stats_out is not None:
                stats_out["batches_failed"] = stats_out.get("batches_failed", 0) + 1
            continue
        for x in cases:
            if not isinstance(x, dict):
                continue
            case = _sanitize_case(x, requirement_text, bid_norm=bid_norm, echo_n=echo_n,
                                  person_scope=person_scope_raw)
            # D4 审计留痕：LLM 路径案例显式标 source——闸门的 table 救援分支
            # （case_source=="table"）明确不适用于 LLM 案例
            case.setdefault("case_source", "llm")
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
    # 截断降级（2026-10-30）：候选页超硬顶被截断时，模型的"确认缺失"(False) 不可信
    # ——它没看到省略页。把 sanitized=="ok"（非防作弊拒绝）案例的 False 降为 None
    # （unverified），走窄版"计入+标注"而非 0。防作弊拒绝（sanitized!=ok）的 False
    # 是"案例造假"不是"材料缺失"，不动。run 72 实证：87 页只送审 71、16 页没看，
    # 模型"没找到材料"实为没看到 → 误判 0。
    if stats_out is not None and stats_out.get("pages_omitted", 0) > 0:
        _mat_keys = ("contract", "invoice", "acceptance",
                     "work_proof", "experience_proof", "social_security",
                     "cert_proof")
        for case in merged:
            if case.get("sanitized") != "ok":
                continue  # 防作弊拒绝的 False 不是材料缺失
            if any(case.get(k) is False for k in _mat_keys):
                for k in _mat_keys:
                    if case.get(k) is False:
                        case[k] = None  # False → 未核验（计入+标注）
                case["truncated_review"] = True
    return merged


def _sanitize_case(
    case: dict,
    requirement_text: str,
    bid_norm: str | None = None,
    echo_n: str | None = None,
    person_scope: str = "",
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

    def _case_name_hit() -> tuple[str, str] | None:
        """案例名回标书：全名 → 剥尾部（日期/括号注记）→ 前缀（跨页表格行的
        案例名常含"（2019年4月1日-…）"日期尾，全名回不到任何一页）。"""
        _name = str(case.get("name") or "").strip()
        _name_n = _nnw(_name)
        if len(_name_n) < 8 or bid_norm is None:
            return None
        if _name_n in bid_norm:
            return _name, _name_n
        _cut = re.split(r"[（(]", _name_n, maxsplit=1)[0]
        if len(_cut) >= 8 and _cut in bid_norm:
            return _cut, _cut
        # 前缀梯尾接 config.case_name_prefix_min()（默认 8）——与 case_evidence
        # 的 _name_prefixes 同源（C2：消除两处硬编码漂移）
        for _plen in (24, 16, 12, config.case_name_prefix_min()):
            if len(_name_n) > _plen and _name_n[:_plen] in bid_norm:
                return _name_n[:_plen], _name_n[:_plen]
        return None

    def _person_anchor_hit() -> tuple[str, str] | None:
        """人员级案例锚：人名 + 证件/角色关键词都能回标书 → 案例真实存在。

        「顾爱军 电工证」这类人证组合常无逐字引文（证照是扫描件/表格行），
        8 字案例名门槛 + quote_too_short 会把整案丢弃（2026-10-26 run 54
        c60 实证：LLM 找到持证人却全员 0 分）。人名与证件词都出现在标书
        （人员表/证书表行）即证明模型看的是真实材料；都回不到才判伪造。
        """
        if bid_norm is None:
            return None
        person = str(case.get("person_name") or case.get("person") or "").strip()
        person_n = _nnw(person)
        if not (2 <= len(person_n) <= 6) or person_n not in bid_norm:
            return None
        rest = _nnw(str(case.get("name") or "").replace(person, ""))
        if not rest:
            return None
        if rest in bid_norm:
            return f"{person} {rest}", f"{person_n}{rest}"
        # LLM 常把证件名归一化（特种作业操作证→电工证）：剥出的残余词按
        # 2 字窗口回标书，任一命中即可（人名已在册，此处只校验证件相关性）
        for i in range(len(rest) - 1):
            if rest[i:i + 2] in bid_norm:
                return f"{person} {rest}", f"{person_n}{rest}"
        return None

    # 引文缺失但案例名是可核验实体 → 以案例名作核验锚。业绩表/合同台账行常"有名有
    # 金额、无逐字引文"（模型把实质材料理解成合同正文/发票，不愿引表格行）；2026-10-21
    # 实证：模型给全 8 个真实案例名 + 金额却 quote 留空 → 旧逻辑全判 quote_too_short
    # → 整项未定分。案例名回得到标书即证明案例真实存在（伪造名回不到标书，仍拒）。
    if len(quote_n) < config.MIN_QUOTE_CHARS and bid_norm is not None:
        hit = _case_name_hit() or _person_anchor_hit()
        if hit:
            case["quote"] = hit[0]
            case["quote_source"] = "case_name"
            quote_n = hit[1]
            _anchored = True
        else:
            _anchored = False
    else:
        _anchored = False
    cheat = True
    reason = "quote_too_short"
    tier: str | None = None
    # 人员级引文可以很短（"项目经理：蒋立华"只有 8 字），放宽到 6 字。
    # 同时检测"角色：姓名"模式——这是人员证据的典型形态。
    _person_quote = bool(re.search(r"(?:项目经理|运维负责人|主要实施|项目总监|团队成员|人员名单|身份证)[：:]", quote))
    _min_len = 6 if _person_quote else config.MIN_QUOTE_CHARS
    if len(quote_n) >= _min_len:
        frac = _ngram_fraction(quote_n, echo_n) if echo_n else 0.0
        if echo_n and frac >= _ECHO_SHADOW_FRACTION:
            reason = "requirement_shadow"  # 照抄要求/整条规则复述
        elif bid_norm is not None:
            tier = _locate_in_bid(quote_n, bid_norm)
            if tier is not None:
                cheat = False
                reason = "ok"  # 引文真实存在于标书 = 真证据
            else:
                # 引文回不到标书（OCR 表格串行重排，模型重写成通顺句）但案例名
                # 是可定位实体 → 以案例名为锚放行（案例存在性由名称保证）。
                # 2026-10-23 实证：致成王兆君案例引文重排后定位失败 → 整项 0 分，
                # 真实评审给 2 分；名称"国网陕西渭南…运维服务项目"逐字在 p143。
                hit = _case_name_hit() or _person_anchor_hit()
                if hit:
                    case["quote"] = hit[0]
                    case["quote_source"] = "case_name"
                    quote_n = hit[1]
                    cheat = False
                    reason = "ok"
                    tier = "case_name"
                else:
                    reason = "quote_not_in_bid"
        else:
            cheat = False
            reason = "ok"  # 旧行为：无标书可核验时信任模型
    elif bool(re.search(r"\d", quote)):
        # 短引文但含金额/年份等实体（如"2024年6月"）仍可信
        cheat = False
        reason = "ok"

    if cheat and _anchored:
        # 锚点（案例名/人名+证件词）本身是标书子串检索命中 = 案例存在性已证实；
        # 短引文/空引文走不到上面的引文定位分支，以锚为核验通过依据
        # （2026-10-26 run 54 c60 实证：锚点救了 quote 却仍被 quote_too_short 杀）。
        cheat = False
        reason = "ok"
        tier = "case_name"

    # A4: 人员级引文校验——quote 必须含人员姓名/角色模式，不能只含项目合同头。
    # 人员级判定**必须查值非空**：LLM 输出的公司级业绩案例常带 "person_name": null
    # 空键，旧的键存在判定会把公司级案例误套人员校验——合同签署页 quote（含甲方/
    # 乙方）触发 project_header_not_person → 整案材料强刷 False → 业绩全灭
    # （2026-10-27 run 57 实证：project_header_not_person=5，人工业绩 23/30 vs 机 4/20）。
    # 显式 person_scope 参数（准则级）优先于案例内证据推断。
    def _has_person_value(key: str) -> bool:
        v = case.get(key)
        return v is not None and str(v).strip() not in ("", "None", "null", "False", "false")

    _is_person_scope = bool(person_scope) or any(
        _has_person_value(k) for k in ("work_proof", "experience_proof", "social_security", "person_name"))
    if not cheat and _is_person_scope:
        _has_person = bool(re.search(
            r"(?:项目经理|运维负责人|主要实施|项目总监|团队成员|人员名单|项目负责人|身份证)[：:\s]*[一-鿿]{2,5}"
            r"|[一-鿿]{2,4}[：:\s]*(?:项目经理|运维负责人|主要实施|项目总监)",
            quote,
        ))
        _has_contract_header = bool(re.search(r"甲方|乙方|合同金额|合同价款|中标金额|发包方|承包方", quote))
        if not _has_person and _has_contract_header:
            reason = "project_header_not_person"
            cheat = True

    if cheat:
        # 按人员/项目级降级对应材料键
        _person_keys = ("work_proof", "experience_proof", "social_security")
        _project_keys = ("contract", "invoice", "acceptance")
        # 有 person 值（非空）→ 人员级；否则项目级
        _is_person = _is_person_scope or any(_has_person_value(k) for k in _person_keys)
        keys = _person_keys if _is_person else _project_keys
        if reason in ("quote_not_in_bid", "quote_too_short"):
            # 引文定位失败型（OCR 串行噪声/过短且无锚）≠ 作弊证据：机器没核上
            # ≠ 材料确认缺失。置 None + 记 unverified，闸门侧归"不可读"转人工
            # （与 objective 侧丢弃分账配套，否则这类 0 分伪装 confirmed_absent）。
            # 作弊型（requirement_shadow/project_header_not_person）维持 False。
            for key in keys:
                case[key] = None
            case["unverified"] = reason
        else:
            for key in keys:
                case[key] = False
    case["sanitized"] = reason
    if tier is not None:
        case["match_tier"] = tier
    return case


# 单位级参保证明（"单位专用"+参保总人数/人员信息表 → 覆盖本单位全员）
_SS_UNIT_WIDE = re.compile(r"单位专用|参保缴费总人数|参保人员信息如下")
# 企业年报公示噪声（"企业选择不公示"的缴费基数/人数不是缴纳证明）
_SS_ANNUAL_REPORT = re.compile(r"企业选择不公示|年度报告|企业年度报告")


def _overlay_social_security(cases: list[dict], pages: list[tuple[int, str, str]]) -> None:
    """人员级社保判定确定性覆盖（就地修改 social_security 布尔）。

    LLM 判社保在 run 间来回摆（run 48 全对，run 49 先数 0→6 龙即 6→4.5）。
    社保名单/参保证明是结构化材料，可机械判定：
    - 该人员姓名出现在社保材料页 → True；
    - 社保材料含单位级参保证明（"单位专用"/参保总人数/人员信息表）→ True
      （本单位员工全员覆盖，截断名单找不到姓名不因此判 False）；
    - 逐人缴费名单（无单位级表述）且姓名不在其中 → False；
    - 完全无社保材料 → False。
    """
    ss_parts: list[str] = []
    for _no, t, _src in pages:
        t = t or ""
        if not re.search(r"社保|参保|缴费|社会保险|养老|医疗", t):
            continue
        if _SS_ANNUAL_REPORT.search(t) and not re.search(r"社会保障号|缴费月数", t):
            continue  # 企业年报公示的"社保信息"节不算缴纳证明
        ss_parts.append(t)
    ss_text = "\n".join(ss_parts)
    if not ss_text.strip():
        for c in cases:
            c["social_security"] = False
        return
    unit_wide = bool(_SS_UNIT_WIDE.search(ss_text))
    for c in cases:
        person = str(c.get("person_name") or c.get("person") or "").strip()
        if not person or len(person) < 2:
            c["social_security"] = None  # 无法对应到人 → 留待严格闸门处置
            continue
        if person in ss_text:
            c["social_security"] = True
        elif unit_wide:
            c["social_security"] = True  # 单位参保证明覆盖全员
        else:
            c["social_security"] = False


# 业绩证明材料页标记（work_proof 的实体证据：合同/用户证明/经验证明小节）
_WP_EV_PAGE = re.compile(
    r"合同扫描件|合同编号|技术服务合同|项目合同|委托方|用户证明|业主证明"
    r"|使用证明|经验证明|案例证明|合同复印件|验收报告"
)


def _overlay_work_proof(cases: list[dict], pages: list[tuple[int, str, str]]) -> None:
    """成员级 work_proof 确定性覆盖：人员姓名须出现在业绩证明材料页上。

    2026-10-23 实证：先数 c60 的 12 个案例全部来自技术方案简历表
    （"59 宋宏 男 硕士研 行业专家…"），其姓名在任何合同/用户证明/经验证明
    页上都不出现（ev_pages=[]）→ 真实评审 0 分；龙即/诚道成员均有证明
    材料页（吴奇钢 p253 用户证明、李卓敏 p211 经验证明）→ 真实 6 分。
    LLM 三轮 prompt 引导均放行简历行，故对 member 级做机械判定。
    """
    ev_pages_text = [t for _n, t, _s in pages if t and _WP_EV_PAGE.search(t)]
    for c in cases:
        person = str(c.get("person_name") or c.get("person") or "").strip()
        if not person or len(person) < 2:
            c["work_proof"] = None  # 无法对应到人 → 留待闸门处置
            continue
        c["work_proof"] = any(person in t for t in ev_pages_text)


# 五年工作经验证明页标记（负责人经验项的独立材料章节）
_EXP_PROOF_PAGE = re.compile(
    r"工作(?:经验证明|年限证明|经历)|经验证明|个人简历|工作简历|"
    r"职称(?:证书|证明)|(?:工作|从业|参加工作)\s*年限"
)


def _overlay_experience_proof(cases: list[dict], pages: list[tuple[int, str, str]]) -> None:
    """负责人 experience_proof 文档级救援（**只升不降**）。

    五年经验证明通常在独立人员资料章节（简历/工作证明扫描页），案例核验只看
    案例页附近——链不上 → False 把材料齐全的整案例判缺（7.10 run 64 实证：
    云锦姜维 work_proof=True/social_security=True 仍 0 可计）。有证明页且
    含该人员姓名 → 置 True（锚点即证据，与 _person_anchor_hit 同构）；
    找不到证明页 → 保持 LLM 原判（机器没看见 ≠ 确认缺失，不强 False）。
    """
    proof_pages = [t for _n, t, _s in pages if t and _EXP_PROOF_PAGE.search(t)]
    if not proof_pages:
        return
    for c in cases:
        if c.get("experience_proof") is True:
            continue
        person = str(c.get("person_name") or c.get("person") or "").strip()
        if len(person) < 2:
            continue
        if any(person in t for t in proof_pages):
            c["experience_proof"] = True


# 负责人情况表（表头 + 明细列信号，两词同页才认定——防泛员工花名册）
_LEADER_TABLE_PAGE = re.compile(r"项目负责人情况表|负责人情况表|人员情况表")
_LEADER_TABLE_DETAIL = re.compile(r"担任|项目经理|项目经历|类似项目|业绩")


def _overlay_leader_table_proof(cases: list[dict], pages: list[tuple[int, str, str]]) -> None:
    """leader work_proof 宽松口径（2026-11-02，**只升不降**）。

    认通用「项目负责人情况表/人员情况表」作 leader 佐证：页面同时命中
    (a) 表头词表 (b) 明细列信号 (c) 含该人员姓名 → 该人员案例 work_proof 升 True。
    用友 c47 实证：吴丽君在 p73 情况表（枚举她担任项目经理的项目）+ p74 身份证 +
    p75 社保，LLM 严判"姓名须出现在该案例证明页"→ False → 0，人工给 4。
    真实评委认这种正式表格形态，与 member「项目团队人员情况表」放宽（prompts
    :201-203）对称口径；不纳入身份证/社保页（证明身份/在职不证明业绩）。
    只升不降（与 _overlay_experience_proof 同构，与 member 严口径 _overlay_work_proof
    刻意区分）；无表页 → return 不强 False；person 空 → 不改写。
    窄词表三条件防先数/西川虚高（test1 先数负责人=0 由类型门槛结构性保护）。
    """
    if not config.leader_table_proof_enabled():
        return
    table_pages = [
        t for _n, t, _s in pages
        if t and _LEADER_TABLE_PAGE.search(t) and _LEADER_TABLE_DETAIL.search(t)
    ]
    if not table_pages:
        return
    for c in cases:
        if c.get("work_proof") is True:
            continue
        person = str(c.get("person_name") or c.get("person") or "").strip()
        if len(person) < 2:
            continue
        if any(person in t for t in table_pages):
            c["work_proof"] = True



# ai_raw_json["verified_cases"] 白名单：落库保留的案例键。三态/审计补键（2026-11-02）：
# unverified 标记与三因细分曾不在白名单、落库即丢，只能从 warnings 中文文案反读。
_AUDIT_CASE_KEYS = (
    "name", "person", "role", "quote", "sanitized",
    "match_tier", "quote_source", "case_source", "page_hint",
    "amount_yuan", "sign_date", "table_page", "material_pages",
    "material_reasons", "contract", "invoice", "acceptance",
    "work_proof", "experience_proof", "social_security", "cert_proof",
    "person_name", "duplicate",
    "unverified", "unverified_reason", "truncated_review",
)


def _load_material_maps(
    doc_ids_by_bidder: dict[int, list[int]],
    main_doc_by_bidder: dict[int, int | None],
    bidder_ids: list[int],
) -> tuple[dict[int, dict[int, str]], dict[int, dict[int, float]],
          dict[int, list[str | None]], dict[int, list[float]],
          dict[int, list[tuple[int, int]]]]:
    """材料类别映射：页级 (page→cat/conf) + chunk 级（与 split_text_chunks 同口径）。

    只认 text_sha 未失效的页级标签；无标签的投标人不进映射（调用方退化无过滤）。
    重建是廉价操作（字符串切分 + 一次索引表读），rescue 后可重跑。
    第 5 返回值 chunk_srcs（(doc_id, page_no) 逐 chunk）：**无标签也返回**——
    P1-6 选段加成（窗口页/偏离表页）不依赖标签，只依赖页出处。
    """
    from app.engine.selection import split_page_chunks
    from app.material_index.service import text_sha as _tsha

    page_cats: dict[int, dict[int, str]] = {}
    page_confs: dict[int, dict[int, float]] = {}
    chunk_cats: dict[int, list[str | None]] = {}
    chunk_confs: dict[int, list[float]] = {}
    chunk_srcs: dict[int, list[tuple[int, int]]] = {}
    with session_scope() as s:
        from app.db.models import DocumentPage as _DP
        from app.db.models import MaterialTag

        for bid_id in bidder_ids:
            cats: dict[int, str] = {}
            confs: dict[int, float] = {}
            pages_seq: list[tuple[int, int, str]] = []
            for d_id in doc_ids_by_bidder.get(bid_id) or []:
                text_by_no: dict[int, str] = {}
                for pno, ptext in s.execute(
                    select(_DP.page_no, _DP.text)
                    .where(_DP.document_id == d_id)
                    .order_by(_DP.page_no)
                ):
                    text_by_no[int(pno)] = ptext or ""
                for pno, ptext in text_by_no.items():
                    pages_seq.append((d_id, pno, ptext))
                for pno, cat, conf, sha in s.execute(
                    select(MaterialTag.page_no, MaterialTag.category,
                           MaterialTag.confidence, MaterialTag.text_sha)
                    .where(MaterialTag.document_id == d_id, MaterialTag.seg_kind == "page",
                           MaterialTag.category != "other")
                ):
                    pno_i = int(pno)
                    if sha and _tsha(text_by_no.get(pno_i, "")) != sha:
                        continue  # 标签已过期
                    cats[pno_i] = cat
                    confs[pno_i] = float(conf or 0)
            if not pages_seq:
                continue
            _chunks, srcs = split_page_chunks(pages_seq)
            chunk_srcs[bid_id] = srcs
            if not cats:
                continue
            page_cats[bid_id] = cats
            page_confs[bid_id] = confs
            main_id = main_doc_by_bidder.get(bid_id)
            chunk_cats[bid_id] = [
                cats.get(pno) if d == main_id else None for d, pno in srcs
            ]
            chunk_confs[bid_id] = [
                confs.get(pno, 0.0) if d == main_id else 0.0 for d, pno in srcs
            ]
    return page_cats, page_confs, chunk_cats, chunk_confs, chunk_srcs


def _veto_precheck(  # noqa: ANN001
    c: dict,
    facts,
    text: str | None = None,  # noqa: ARG001 —— 属 LLM 阶段输入，签名与 _eval_veto 对齐
    scan_hint: bool = False,
) -> tuple[object, str]:
    """Phase0 确定性预检（无 LLM，2026-10-30 阶段 2）：run_check + 扫描件豁免
    （原 _eval_veto :1985-2007 逻辑原样搬出），并给出该格的 LLM 去向 llm_kind：

    - "human_batch"：human_only（或无 check_type）且需 LLM → 进组批裁决；
    - "single"：其余 unknown/fail+need_human（冲突/信号命中）→ 逐格单条 LLM；
    - "none"：终态（pass / 硬 fail），或 math_relation 冲突且跳 LLM 开关开
      （结构化比对已定论，LLM 不改变结论 → 直接 verdict_for 出 conflict_flagged）。

    返回 (PrimitiveResult, llm_kind)。
    """
    check_type = c.get("check_type")
    params = c.get("params") or {}
    if not check_type:
        # 无检查定义 ≠ 永远人工死项：按 human_only 语义走（确定性判不了），
        # auto 模式下 LLM 会接手语义裁决，LLM 也定不了才 need_human
        # （2026-10-26 run 54 实证：c51/c57/c63 无 check → 7/7 恒 need_human）
        check_type = "human_only"
    result = run_check(check_type, params, facts)
    if scan_hint and result.status == "fail" and not result.need_human:
        # 「看不见≠不存在」豁免（6b 对称化）：标书含扫描件/OCR 页时，**检索/存在性
        # 类**硬 fail 可能是文本层不可见造成——不硬否决，升级 LLM/人工裁决。
        # 误否决=0 的机制保证之一（2026-10-26 run 54 c13 类误否决实证）。
        # 2026-10-27 收窄：计数/算术类（count_at_least/math_relation）的输入是
        # 结构化事实（facts 已抽取），不受扫描件文本可见性影响——确定性 fail 保留，
        # 否则 LLM 触发率 60-76% 全是绕路（每格 +20s）且引入模型方差。
        if check_type in ("document_present", "contains_phrase", "human_only"):
            from app.engine.objective import PrimitiveResult

            result = PrimitiveResult(
                "unknown",
                f"{result.message}（标书含扫描件/OCR 页，文本层不可见可能导致误判，不作为否决依据）",
                need_human=True,
            )
    needs_llm = result.status == "unknown" or (result.status == "fail" and result.need_human)
    if not needs_llm:
        return result, "none"
    # math_relation 冲突跳过 LLM（阶段 2，2026-10-30）：fail+need_human 来自结构化
    # 数值比对（大小写/分项合计/限价），LLM 读同一文本无法改变结论——直接
    # verdict_for 出 conflict_flagged 转人工；unknown（未抽到报价）仍需 LLM 语义兜底。
    if (
        config.math_skip_llm_enabled()
        and check_type == "math_relation"
        and result.status == "fail"
        and result.need_human
    ):
        return result, "none"
    if check_type == "human_only":
        return result, "human_batch"
    return result, "single"


def _eval_veto(  # noqa: ANN001
    c: dict,
    facts,
    on_fail: str = "recommend_reject",
    text: str | None = None,
    gateway=None,
    data_class: str = "synthetic",
    scan_hint: bool = False,
    *,
    chunk_cats: list[str | None] | None = None,
    chunk_cat_confs: list[float] | None = None,
) -> dict:
    check_type = c.get("check_type")
    params = c.get("params") or {}
    if not check_type:
        # 无检查定义 ≠ 永远人工死项：按 human_only 语义走（确定性判不了），
        # auto 模式下 LLM 会接手语义裁决，LLM 也定不了才 need_human
        # （2026-10-26 run 54 实证：c51/c57/c63 无 check → 7/7 恒 need_human）
        check_type = "human_only"
    result = run_check(check_type, params, facts)
    if scan_hint and result.status == "fail" and not result.need_human:
        # 「看不见≠不存在」豁免（6b 对称化）：标书含扫描件/OCR 页时，**检索/存在性
        # 类**硬 fail 可能是文本层不可见造成——不硬否决，升级 LLM/人工裁决。
        # 误否决=0 的机制保证之一（2026-10-26 run 54 c13 类误否决实证）。
        # 2026-10-27 收窄：计数/算术类（count_at_least/math_relation）的输入是
        # 结构化事实（facts 已抽取），不受扫描件文本可见性影响——确定性 fail 保留，
        # 否则 LLM 触发率 60-76% 全是绕路（每格 +20s）且引入模型方差。
        if check_type in ("document_present", "contains_phrase", "human_only"):
            from app.engine.objective import PrimitiveResult

            result = PrimitiveResult(
                "unknown",
                f"{result.message}（标书含扫描件/OCR 页，文本层不可见可能导致误判，不作为否决依据）",
                need_human=True,
            )
    # math_relation 冲突跳过 LLM（阶段 2，2026-10-30）：fail+need_human 来自结构化
    # 数值比对（大小写/分项合计/限价），结论已定——不调 LLM 直接 verdict_for
    # （fail+need_human → conflict_flagged 转人工）；unknown（未抽到报价）保留 LLM。
    # 开关 BID_EVAL_MATH_SKIP_LLM=0 回退逐格 LLM 旧路径。
    _skip_llm_math = (
        config.math_skip_llm_enabled()
        and check_type == "math_relation"
        and result.status == "fail"
        and result.need_human
    )
    if (
        not _skip_llm_math
        and config.auto_mode()
        and gateway is not None
        and (result.status == "unknown" or (result.status == "fail" and result.need_human))
    ):
        # 全自动模式：human_only / 冲突 / 信号命中 → LLM 带原文证据裁决。
        # AI 不否决（2026-10-14 用户指令，恢复"绝不自动否决"原则）：LLM 的 fail
        # 一律降级为"疑问"——machine_status=need_human，由评标委员会认定；
        # 投标人照常参与评分与排名。pass 才是唯一通过终态。
        from app.engine.auto_judge import refine_with_llm

        result = refine_with_llm(
            gateway, c, facts, text or "", result, data_class=data_class,
            chunk_cats=chunk_cats, chunk_cat_confs=chunk_cat_confs,
        )
        if result.status == "pass":
            return {"verdict": "pass", "message": result.message, "evidence": result.evidence_quote}
        return {
            "verdict": "need_human",
            "message": f"AI 存疑（不自动否决，转评标委员会认定）：{result.message}",
            "evidence": result.evidence_quote,
        }
    verdict = verdict_for(c["code"], c["name"], result, on_fail=on_fail)
    return {"verdict": verdict, "message": result.message, "evidence": result.evidence_quote}


def _batch_cell_from_judgement(j) -> dict:  # noqa: ANN001 —— auto_judge.AutoJudgement
    """批量 AutoJudgement → 落库单元格 dict。

    消息口径与单条路径逐字一致：pass → "自动裁决（通过）：…"；fail →
    "AI 存疑（不自动否决，转评标委员会认定）：自动裁决（不通过）：…"——
    单条 _eval_veto 里 f"AI 存疑…：{refine_with_llm 的 message}" 的同款拼接。
    """
    prefix = "通过" if j.verdict == "pass" else "不通过"
    quote = next(
        (str(e.get("quote") or "").strip()
         for e in j.evidence if str(e.get("quote") or "").strip()),
        None,
    )
    if j.verdict == "pass":
        return {
            "verdict": "pass",
            "message": f"自动裁决（{prefix}）：{j.reasoning}",
            "evidence": quote,
        }
    return {
        "verdict": "need_human",
        "message": f"AI 存疑（不自动否决，转评标委员会认定）：自动裁决（{prefix}）：{j.reasoning}",
        "evidence": quote,
    }


def _resolve_veto_results(
    veto_cells: list,
    *,
    facts_by_bidder: dict,
    text_by_bidder: dict,
    scan_hints: dict,
    gateway,
    data_class: str,
    chunk_cats_by_bidder: dict | None = None,
    chunk_cat_confs_by_bidder: dict | None = None,
    ctx=None,
) -> list[dict]:
    """客观审查两段式（阶段 2 否决提速，2026-10-30）。

    Phase0：全格确定性预检（_veto_precheck，无 LLM）——终态格直接 verdict_for；
    Phase1：human_only 格按投标人分组、组内按 config.veto_batch_size() 切块 →
    refine_with_llm_batch 块间并行（样板 = _llm_verify_cases 批间并行；网关信号量
    是真闸门）；其余 unknown/fail+need_human 格维持单格 _eval_veto LLM 路径；
    组批开关关闭（BID_EVAL_VETO_HUMAN_ONLY_BATCH=0）→ human_only 格全部走 single。

    返回与 veto_cells 同序的单元格 dict 列表。不变式 I3：落库顺序 = 调用方按
    veto_cells 原序 zip，本函数内块间/格间的并行完成顺序不影响落库序。
    """
    import threading as _threading

    chunk_cats_by_bidder = chunk_cats_by_bidder or {}
    chunk_cat_confs_by_bidder = chunk_cat_confs_by_bidder or {}
    total = len(veto_cells)
    prog_lock = _threading.Lock()
    done = [0]

    def _advance(n: int, fmt: str = "客观审查 {done}/{total}") -> None:
        with prog_lock:
            done[0] += n
            if ctx is not None:
                ctx.advance(n=n, message=fmt.format(done=done[0], total=total))

    results: dict[int, dict] = {}
    # A.2 资格审查弱化（2026-11-04）：BID_EVAL_VETO_LLM 默认关——human_only/single
    # 两路 LLM 深评全跳过，按 Phase0 确定性结果直出。确定性硬 fail 照出
    # recommend_reject（基准价剔除通道保留）；match 桶（_match_cell）不经此闸。
    llm_ok = config.veto_llm_enabled() and config.auto_mode() and gateway is not None
    _weakened = (
        not config.veto_llm_enabled()
        and config.auto_mode()
        and gateway is not None
    )
    _weaken_note = "（资格审查 LLM 深评已按配置停用（BID_EVAL_VETO_LLM=0），转人工复核）"

    # ---- Phase0：全格确定性预检（无 LLM） ----
    none_idxs: list[int] = []
    batch_idxs: list[int] = []
    single_idxs: list[int] = []
    pre_results: dict[int, object] = {}  # PrimitiveResult（组批 items 的机器预检信息）
    for i, (bid_id, c) in enumerate(veto_cells):
        try:
            result, kind = _veto_precheck(
                c, facts_by_bidder[bid_id],
                text=text_by_bidder.get(bid_id),
                scan_hint=scan_hints.get(bid_id, False),
            )
        except Exception as exc:  # noqa: BLE001 —— 单格失败不让整场失败
            if _is_fatal_llm_error(exc):
                raise
            log.warning("客观审查单元格失败 bid=%s criterion=%s：%s", bid_id, c.get("code"), exc)
            results[i] = {"verdict": "need_human", "message": f"客观审查执行失败：{exc}"}
            none_idxs.append(i)
            continue
        if not llm_ok or kind == "none":
            # 终态（pass/硬 fail，或 math 冲突跳 LLM）/人工模式无网关/弱化态：直接判定映射。
            # 弱化溯源：只给"本来会走 LLM 深评"的格（kind != none）附停用说明；
            # 人工模式/无网关路径消息逐字不变。
            msg = result.message
            if _weakened and kind != "none":
                msg = f"{msg or ''}{_weaken_note}"
            results[i] = {
                "verdict": verdict_for(c["code"], c["name"], result),
                "message": msg,
                "evidence": result.evidence_quote,
            }
            none_idxs.append(i)
        elif kind == "human_batch" and config.veto_human_only_batch_enabled():
            pre_results[i] = result
            batch_idxs.append(i)
        else:
            single_idxs.append(i)
    if none_idxs:
        _advance(len(none_idxs))  # 终态格瞬时收敛，计入进度与取消检查点

    # ---- Phase1a：human_only 组批（同一投标人共享选段/facts，一次 LLM 裁 N 条） ----
    if batch_idxs:
        from app.engine.auto_judge import (
            facts_block_text,
            price_block_line,
            refine_with_llm_batch,
        )

        by_bid: dict[int, list[int]] = {}
        for i in batch_idxs:
            by_bid.setdefault(veto_cells[i][0], []).append(i)
        bsize = config.veto_batch_size()
        blocks: list[tuple[int, list[int]]] = []
        for bid_id, idxs in by_bid.items():  # 插入序 = veto_cells 序（bidder 主序）
            for s0 in range(0, len(idxs), bsize):
                blocks.append((bid_id, idxs[s0 : s0 + bsize]))

        def _run_batch_block(args):  # noqa: ANN001 —— (bid_id, 组内格下标列表)
            bid_id, idxs = args
            facts = facts_by_bidder[bid_id]
            facts_text = facts_block_text(facts)
            price_line = price_block_line(facts)
            items = [
                {
                    "criterion": veto_cells[i][1],
                    "facts_text": facts_text,
                    "price_line": price_line,
                    "machine_message": getattr(pre_results[i], "message", ""),
                }
                for i in idxs
            ]
            try:
                judgements = refine_with_llm_batch(
                    gateway, items, text_by_bidder.get(bid_id) or "",
                    data_class=data_class,
                    chunk_cats=chunk_cats_by_bidder.get(bid_id),
                    chunk_cat_confs=chunk_cat_confs_by_bidder.get(bid_id),
                )
            except Exception as exc:  # noqa: BLE001 —— 单块失败不让整场失败
                if _is_fatal_llm_error(exc):
                    raise  # 配额/预算/涉密 fail-fast 穿透 pool
                log.warning("客观审查批量块失败 bid=%s：%s", bid_id, exc)
                _advance(len(idxs))
                return {
                    i: {"verdict": "need_human", "message": f"客观审查执行失败：{exc}"}
                    for i in idxs
                }
            out: dict[int, dict] = {}
            for i in idxs:
                code = str(veto_cells[i][1].get("code") or "")
                j = judgements.get(code)
                out[i] = (
                    _batch_cell_from_judgement(j)
                    if j is not None
                    else {"verdict": "need_human", "message": "客观审查执行失败：批量裁决结果缺失该条"}
                )
            _advance(len(idxs))
            return out

        workers = min(len(blocks), max(1, config.llm_max_concurrency()))
        if workers <= 1:
            block_outs = [_run_batch_block(b) for b in blocks]
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=workers) as pool:
                block_outs = list(pool.map(_run_batch_block, blocks))
        for bo in block_outs:
            results.update(bo)

    # ---- Phase1b：single 格（含组批开关关闭时的 human_only 全量）——单格 LLM 旧路径 ----
    if single_idxs:

        def _single_cell(args):  # noqa: ANN001 —— veto_cells 下标
            i = args
            bid_id, c = veto_cells[i]
            try:
                r = _eval_veto(  # type: ignore[arg-type]
                    c, facts_by_bidder[bid_id],
                    text=text_by_bidder.get(bid_id), gateway=gateway, data_class=data_class,
                    scan_hint=scan_hints.get(bid_id, False),
                    chunk_cats=chunk_cats_by_bidder.get(bid_id),
                    chunk_cat_confs=chunk_cat_confs_by_bidder.get(bid_id),
                )
            except Exception as exc:  # noqa: BLE001 —— 单格失败不让整场失败
                if _is_fatal_llm_error(exc):
                    raise
                log.warning("客观审查单元格失败 bid=%s criterion=%s：%s", bid_id, c.get("code"), exc)
                r = {"verdict": "need_human", "message": f"客观审查执行失败：{exc}"}
            return i, r

        # 维持现有 _parallel_map 单格并行（含逐格 advance/取消检查）；
        # 消息带「（单格）」与 Phase0/1a 的累计计数区分，避免显示数字回跳。
        single_outs = _parallel_map(
            _single_cell, single_idxs, ctx=ctx, message_fmt="客观审查（单格） {done}/{total}",
        )
        for i, r in single_outs:
            results[i] = r

    return [results[i] for i in range(total)]


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


def _count_zero_warn_entry(result, audit: dict | None) -> tuple[dict, bool]:
    """count 落库警告条目 + unreadable 零分判定（W1-C）。

    返回 (warnings_json 条目, unreadable_zero)：
    - unreadable（机器不可见）→ 文案明示"非确认缺失，建议人工核看"，
      调用方必须配 machine_status="need_human" + review_status="conflict_flagged"；
    - confirmed_absent → 文案明示"确认未提供"（规则硬口径 0，无需转人工）；
    - 条目携带结构化 zero_reason/unresolved 键，报告疑问清单按此分组，
      不再靠匹配中文措辞（与 unresolved 落库同一教训）。
    """
    score = result.score
    zero_reason = (audit or {}).get("zero_reason") if isinstance(audit, dict) else None
    if not zero_reason and score == 0:
        # 兜底：audit 缺席时从 result 侧取（异常路径的 SimpleNamespace 已带字段）
        zero_reason = getattr(result, "zero_reason", None)
    # type_gate（类型门槛剔空）与 unreadable 同级：0 分是类型判断结论而非
    # "未提供材料"，必须转人工核看（7.10 实证：集成/维保类案例被剔后
    # 曾伪装 confirmed_absent/pass）。cases_unverified（核验未做完/批失败）
    # 同级（2026-11-04 诚实化）：机器故障 ≠ 确认缺失。
    unreadable_zero = zero_reason in ("unreadable", "type_gate", "cases_unverified") and score == 0
    if zero_reason == "type_gate" and score == 0:
        msg = f"{result.detail}；零分理由：类型门槛剔除（type_gate），非材料缺失，建议人工核看"
    elif score == 0 and zero_reason == "cases_unverified":
        msg = (f"{result.detail}；零分理由：LLM 案例核验失败（cases_unverified），"
               "机器故障非确认缺失，建议重跑或人工核看")
    elif unreadable_zero:
        msg = f"{result.detail}；零分理由：机器不可见（unreadable），非确认缺失，建议人工核看"
    elif score == 0 and zero_reason == "confirmed_absent":
        msg = f"{result.detail}；零分理由：确认未提供（confirmed_absent）"
    else:
        msg = result.detail
    entry: dict = {"message": msg}
    if result.unresolved:
        entry["unresolved"] = result.unresolved
    if score == 0 and zero_reason:
        entry["zero_reason"] = zero_reason
    return entry, unreadable_zero


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
    # 0 分无证据区分（2026-11-02 三态口径，先算供落库）：confirmed_absent=
    # 材料确认缺失（规则硬口径「不得分」）照 0；其余 0 分且零引文 = 机器没依据
    # （LLM 判 0 却给不出引用/材料未见非确认缺失）→ zero_reason=no_facts +
    # need_human，不落 auto_confirmed（对齐 run_check 的 unknown 哲学，run77
    # c48：材料在 p121-122 却被"未见材料"静默清零，人工给满分）。
    _zr = getattr(suggestion, "zero_reason", None)
    zero_no_basis = (
        suggestion.score == 0 and _zr != "confirmed_absent" and not suggestion.evidence
    )
    if zero_no_basis:
        _zr = "no_facts"
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
        # 聚合算式（可复算不变式 I1）：final_score 可由 inputs 按 rule 重算核对
        _agg_v2 = getattr(suggestion, "aggregation", None)
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
            # 过期字符串 "evidence_cluster_weighted" 已废弃：聚合规则以
            # aggregation_v2["rule"] 为准（W1-A 新格式），此处仅作旧读者兼容镜像
            "aggregation": (
                (str(_agg_v2.get("rule")) if isinstance(_agg_v2, dict) and _agg_v2.get("rule") else None)
                or (
                    f"trimmed_mean_{len(committee)}_drop_high_low"
                    if committee and config.review_mode() != "hierarchical" else None
                )
            ),
            "aggregation_v2": _agg_v2,
            # 校准参数版本（2026-10-27 训练集校准，审计可还原用的哪版尺度）
            "calibration_profile": config.calibration_profile().get("version") or None,
            # 证明材料链核对与复合因素子项得分（评分透明度）
            "artifacts_check": suggestion.artifacts_check,
            "sub_scores": suggestion.sub_scores,
            # 0 分理由（W1-C 对齐 count 路径）：confirmed_absent=材料确认缺失
            # （规则硬口径）/ no_facts=机器没依据（转人工）
            "zero_reason": _zr,
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
            # 全自动模式语义统一（W1-D，2026-10-29）：
            # 2026-10-15 诚道运维方案 0 分实证的教训——机器故障（parse_failed /
            # llm_failed / 分层全簇失败 / insufficient 无分）**不得静默伪装成评审
            # 结论**；表达方式是"最低档兜底 + 显式疑问"：靠 conflict_flagged +
            # zero_reason（count 路径）可审计，警告明示兜底分非评审结论。
            #
            # suggestion.score 非 None（含 provisional / 分层聚合分 / insufficient
            # 带分）→ 直接进总分，**不得被最低档地板分丢弃**（旧行为把 provisional
            # 聚合分扔掉重算地板分——分层评审有效簇的分数被静默清零）。
            # 但 status != ai_suggested 或聚合层标 doubt → need_human +
            # conflict_flagged：分照进（用户要求零未定分），复核态必须可见。
            parse_failed = any(
                isinstance(w, dict) and w.get("message") in ("parse_failed", "llm_failed")
                for w in (row.warnings_json or [])
            )
            doubt_flag = bool(getattr(suggestion, "doubt", False)) or zero_no_basis
            if gate_not_executed:
                # 保留分值（进总分，不影响排名），但标疑问进复核清单：
                # 若整项直接判未定分，8B 偶尔不给逐子项引文就会让整份报告失效
                row.warnings_json = (row.warnings_json or []) + [
                    {"message": "子项引文未提供，证据闸门未执行，建议人工复核"}
                ]
            if suggestion.score is not None:
                # 有分（正常建议 / provisional / 分层聚合 / insufficient 带分）
                row.final_score = suggestion.score
                if suggestion.status == "ai_suggested" and not doubt_flag and not parse_failed:
                    row.machine_status = "pass"
                else:
                    # 分数有效但机器自标存疑（insufficient / need_human / doubt /
                    # 全簇弃权 all-abstain）→ 分进总分，状态强制转人工可见
                    row.machine_status = "need_human"
                    row.warnings_json = (row.warnings_json or []) + [
                        {"message": f"全自动模式：模型建议分（{suggestion.score}）已入总分，"
                                    f"但状态为 {suggestion.status}"
                                    + ("且聚合层标疑问（doubt）" if doubt_flag else "")
                                    + "，建议人工复核"}
                    ]
            else:
                # score is None（parse_failed / llm_failed / 分层全簇失败 /
                # insufficient 无分）→ **最低档兜底**并保留疑问，不再置 final=None。
                # 故障详情留在 warnings / ai_raw_json，供事后审计；机器状态用
                # need_human 区分"兜底"与"正常评审"，但不阻止计分与排名。
                # 兜底分非评审结论——警告必须明示（不得静默伪装）。
                mins = [Decimal(str(lv.get("min")))
                        for lv in (criterion.get("levels") or []) if lv.get("min") is not None]
                fallback = min(mins) if mins else Decimal(0)
                row.final_score = fallback
                row.machine_status = "need_human"
                # R3-5 兜底分清单（2026-11-03）：结构化键供报告单列（下游禁配
                # 中文措辞），message 文案保留给人工阅读
                _fb_key = {
                    "kind": "min_level_floor",
                    "value": str(fallback),
                    "basis": "no_valid_output",
                }
                if parse_failed:
                    row.warnings_json = (row.warnings_json or []) + [
                        {"message": f"全自动模式：机器故障/无有效输出（模型调用失败或输出解析失败），"
                                    f"按最低档兜底（{fallback} 分）——兜底分非评审结论，建议人工复核或重跑",
                         "fallback_score": _fb_key}
                    ]
                else:
                    row.warnings_json = (row.warnings_json or []) + [
                        {"message": f"全自动模式：机器故障/无有效输出（{suggestion.status}），"
                                    f"按最低档兜底（{fallback} 分）——兜底分非评审结论，建议人工复核或重跑",
                         "fallback_score": _fb_key}
                    ]
            row.review_status = (
                "conflict_flagged"
                if (gate_not_executed or parse_failed or doubt_flag
                    or suggestion.status != "ai_suggested" or suggestion.score is None)
                else "auto_confirmed"
            )
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
