"""规则编译：招标文件条款 → LLM 抽取 → 草稿规则包 + 证据引用 + 六态分诊。

流程（对应方案 ③④ 步）：
1. 取招标文件全文（页序拼接，与 document_pages 逐字一致，保证引文可定位）；
2. 走 LLM 网关抽取（真实模型语义抽取）；
3. 每条规则的 quote 建证据引用并核验（无证据不评分）；
4. 六态分诊：verified / needs_input / ambiguous（档位无分值）/ gap / conflict / invalid；
5. 跑 12 条确定性校验器，findings 落库。
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal

from sqlalchemy import select

from app import config
from app.db.models import (
    Criterion,
    CriterionCheck,
    CriterionLevel,
    Document,
    DocumentPage,
    DocNode,
    EvidenceCitation,
    PackageValidation,
    PriceFormula,
    RulePackage,
)
from app.db.session import session_scope
from app.errors import AppError, NotFound
from app.evidence.verifier import verify_citation
from app.jobs.handlers import register
from app.jobs.queue import JobContext
from app.llm.base import LLMError
from app.llm.gateway import Gateway, get_gateway
from app.parsing.normalize import normalize_no_ws
from app.rules import prompts
from app.rules.model import ExtractedCriterion, RuleExtraction

log = logging.getLogger("bid_eval.rules")

MAX_COMPILE_CHARS = 30_000  # 送进模型的招标文件字符预算（超预算按章节优先级选取，不再盲目截头）

# 章节优先级：真实招标文件的"评标办法/评分细则"通常排在最后（本文档在第 89-97 页），
# 盲目截前 N 字符会把它整段丢掉——评分项一条都抽不出来，只剩前附表的资格与否决条款。
# 这里按关键词给页分档，先保证评分办法进模型，再补资格与否决依据。
COMPILE_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "scoring",
        (
            "评标办法", "评标方法", "评分办法", "评分细则", "评分标准", "评分内容",
            "评审因素", "评审办法", "评审细则", "价格分", "报价部分", "最佳报价",
            "商务部分", "技术部分", "商务分", "技术分", "主观分", "客观分",
        ),
    ),
    (
        "eligibility",
        (
            "资格要求", "资格审查", "符合性审查", "否决", "废标", "无效标", "投标人须知",
        ),
    ),
)


def _load_tender_text(document_id: int) -> tuple[str, Document]:
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None:
            raise NotFound("文档不存在")
        if doc.kind not in ("tender", "addendum"):
            raise AppError("只能从招标文件编译规则")
        if doc.parse_status not in ("parsed", "degraded"):
            raise AppError(f"招标文件尚未解析完成（{doc.parse_status}）")
        rows = s.execute(
            select(DocumentPage).where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_no)
        ).scalars().all()
        text = "\n".join(p.text or "" for p in rows)
    return text, doc


def _load_tender_pages(document_id: int) -> list[tuple[int, str]]:
    with session_scope() as s:
        rows = s.execute(
            select(DocumentPage.page_no, DocumentPage.text)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_no)
        ).all()
    return [(int(no), txt or "") for no, txt in rows]


def select_compile_text(pages: list[tuple[int, str]], budget: int = MAX_COMPILE_CHARS) -> tuple[str, dict]:
    """章节感知选取：预算内优先保住评分办法，再补资格与否决依据。

    输出始终按页序拼接，页间以换行相连——与 ``_load_tender_text`` 同源同序，
    因此 LLM 给出的 quote 仍能在 document_pages 上逐字定位核验。

    返回 (text, meta)，meta 记录各档入选页数与是否发生裁剪，便于审计与排查。
    """
    full = "\n".join(t for _no, t in pages)
    if len(full) <= budget:
        return full, {
            "truncated": False,
            "chars": len(full),
            "total_chars": len(full),
            "pages": [no for no, _t in pages],
            "tiers": {},
        }

    by_page = dict(pages)
    chosen: set[int] = set()
    used = 0
    tier_hits: dict[str, int] = {}
    for name, keywords in COMPILE_TIERS:
        # 关键词只出现在章节首页是常态：续页（如"符合性审查"否决情形 (2)-(15)）自身零命中，
        # 若只按本页命中排序，续页会被同档位里低信息量的页挤掉。这里给相邻页半个权重，
        # 让章节成段入选。输出顺序另行按页序重排，不受影响。
        raw = {
            page_no: sum(1 for k in keywords if k in text)
            for page_no, text in pages
            if page_no not in chosen
        }
        order = [p for p, _t in pages]
        pos = {p: i for i, p in enumerate(order)}
        scored: list[tuple[float, int]] = []
        for page_no in order:
            if page_no in chosen:
                continue
            i = pos[page_no]
            neighbor = 0.0
            if i > 0:
                neighbor = max(neighbor, raw.get(order[i - 1], 0))
            if i + 1 < len(order):
                neighbor = max(neighbor, raw.get(order[i + 1], 0))
            score = raw.get(page_no, 0) + 0.5 * neighbor
            if score > 0:
                scored.append((score, page_no))
        scored.sort(key=lambda x: (-x[0], x[1]))
        picked = 0
        for _score, page_no in scored:
            cost = len(by_page[page_no]) + 1
            if used + cost > budget:
                continue
            chosen.add(page_no)
            used += cost
            picked += 1
        tier_hits[name] = picked

    # 预算未用满时按页序回填，尽量让模型看到上下文（不改变已入选页的相对顺序）
    if used < budget:
        for page_no, text in pages:
            if page_no in chosen:
                continue
            cost = len(text) + 1
            if used + cost > budget:
                break
            chosen.add(page_no)
            used += cost
        tier_hits["fill"] = len(chosen) - sum(tier_hits.values())

    selected = [(no, t) for no, t in pages if no in chosen]
    meta = {
        "truncated": True,
        "chars": sum(len(t) + 1 for _no, t in selected),
        "total_chars": len(full),
        "pages": [no for no, _t in selected],
        "tiers": tier_hits,
    }
    return "\n".join(t for _no, t in selected), meta


def _next_version(project_id: str) -> int:
    with session_scope() as s:
        row = s.execute(
            select(RulePackage.version).where(RulePackage.project_id == project_id)
            .order_by(RulePackage.version.desc()).limit(1)
        ).scalar_one_or_none()
    return (row or 0) + 1


def find_node_for_quote(document_id: int, page_no: int | None, quote: str) -> str | None:
    """在给定页（及相邻页）上找"包含该引文的最深节点"。

    两个真实世界的坑（浙江中烟项目实测，26 条里 19 条栽在这上面）：

    ① 软换行：节点文本里是换行，normalize 后成空格，而模型引文跨行逐字复制、不含
       空格。用朴素子串包含会因这一个空格失配——引文核验通过了，节点却定位不到。
    ② 跨页节点只归属一页：如"报价部分（60分）"节点落在 p96，而引文页码是 p97；
       模型引文也常非逐字（尾部有出入），证据层用 prefix_fallback 才通过。

    因此这里与证据核验保持同一口径：空白不敏感 + 相邻页 + 前缀逐级回退。
    """
    q = normalize_no_ws(quote)
    if not q:
        return None
    page = page_no or -1
    pages = [page] + [p for p in (page - 1, page + 1) if p > 0]
    with session_scope() as s:
        rows = s.execute(
            select(DocNode).where(DocNode.document_id == document_id, DocNode.page.in_(pages))
        ).scalars().all()
    needles = [q] + [normalize_no_ws(quote[:n]) for n in config.PREFIX_FALLBACK_LENGTHS]
    for needle in needles:
        if not needle:
            continue
        best: DocNode | None = None
        for n in rows:
            if needle in normalize_no_ws(n.text or ""):
                if best is None or n.depth > best.depth:
                    best = n
        if best is not None:
            return best.node_id
    return None


def _triage(c: ExtractedCriterion, quote_verified: bool, has_node: bool) -> str:
    """六态分诊。"""
    if c.score_type == "subjective" and c.levels and any(
        lv.min is None or lv.max is None for lv in c.levels
    ):
        return "ambiguous"  # 原文没给分值区间——不得由系统猜
    if not quote_verified or (c.quote is None):
        return "needs_input"
    if c.score_type == "subjective" and not c.levels:
        return "needs_input"
    if c.score_type == "formula" and (c.k is None or c.max_score is None):
        return "needs_input"
    if c.score_type == "veto" and not c.check_type:
        return "needs_input"
    if has_node:
        return "verified"
    return "needs_input"


def _persist_criterion(
    s,  # noqa: ANN001
    package_id: int,
    document_id: int,
    c: ExtractedCriterion,
    seq: int,
    used_codes: set[str],
) -> tuple[int, int | None]:
    """写入一条评分项及其子结构；返回 (criterion_id, citation_id)。

    编号唯一性由这里兜底：真实模型可能返回重复或空编号，唯一约束必须先行满足。
    **不在本函数内做核验**——核验要用独立会话读引用，必须等外层事务提交后
    （SQLite 事务隔离下，未提交的行对其他连接不可见）。
    """
    citation_id: int | None = None
    if c.quote:
        cit = EvidenceCitation(
            document_id=document_id,
            quote=c.quote,
            page_no=c.page_hint,
            citation_role="criterion_source",
            created_by="llm",
        )
        s.add(cit)
        s.flush()
        citation_id = cit.id

    # 先按"未核验"落库；_verify_and_backfill 会在核验后重新分诊
    status = _triage(c, quote_verified=False, has_node=False)
    code = c.code.strip() or f"c{seq}"
    if code in used_codes:  # 模型返回重复编号 → 加序号后缀保证唯一
        n = 2
        base = code
        while f"{base}~{n}" in used_codes:
            n += 1
        code = f"{base}~{n}"
    used_codes.add(code)
    crit = Criterion(
        package_id=package_id,
        code=code,
        category=c.category,
        name=c.name,
        description=c.description,
        max_score=c.max_score,
        weight=None,  # 权重永不来自模型
        score_type=c.score_type,
        is_veto=bool(c.is_veto),
        veto_consequence=c.veto_consequence if c.is_veto else None,
        source_node_id=None,  # 核验通过后回填
        source_page=c.page_hint,
        source_quote=c.quote,
        status=status,
        confidence=c.confidence,
        sort_order=seq,
    )
    s.add(crit)
    s.flush()

    # 档位
    for lv in c.levels:
        s.add(
            CriterionLevel(
                criterion_id=crit.id,
                level_label=lv.label,
                score_min=lv.min if lv.min is not None else Decimal(0),
                score_max=lv.max if lv.max is not None else Decimal(0),
                description=lv.criteria,
                source_quote=lv.quote,
            )
        )

    # 客观检查原语（否决项与计数类评分项都需要落 params 供确定性引擎执行）
    if c.check_type or c.params or c.count_per_item is not None:
        from app.rules.freeze import _jsonable

        params = dict(c.params or {})
        # 契约允许模型单独给 count_per_item；确定性引擎只认 params["per_item"]，
        # 这里必须折叠，否则真实模型路径下 count 项全部恒 0 分。
        if c.count_per_item is not None and "per_item" not in params:
            params["per_item"] = c.count_per_item
        s.add(
            CriterionCheck(
                criterion_id=crit.id,
                check_type=c.check_type or ("count_at_least" if c.score_type == "count" else "human_only"),
                field=params.get("field") if isinstance(params.get("field"), str) else None,
                params_json=_jsonable(params),
                on_fail="recommend_reject" if c.is_veto else "need_human",
                source_quote=c.quote,
            )
        )

    # 价格公式
    if c.category == "price":
        s.add(
            PriceFormula(
                package_id=package_id,
                criterion_id=crit.id,
                formula_type=c.formula_type or "benchmark_ratio",
                benchmark_method=c.benchmark_method or "average",
                k=c.k,
                upper_bound=c.upper_bound,
                rounding=c.rounding if c.rounding is not None else 2,
                exclude_invalid=True,
                source_node_id=None,  # 核验通过后回填
                source_quote=c.quote,
            )
        )
    return crit.id, citation_id


def run_validations(package_id: int) -> list[dict]:
    """跑 12 条校验器并把 findings 落库（confirm/freeze 前置）。"""
    from app.rules.validators import run_validators as run_checks
    from app.services.rule_service import load_criteria_for_snapshot

    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is None:
            raise NotFound("规则包不存在")
        criteria = load_criteria_for_snapshot(s, package_id)
    snapshot = {
        "package": {"project_id": pkg.project_id, "version": pkg.version,
                    "status": pkg.status, "total_max": 100},
        "criteria": criteria,
    }
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        source_doc = pkg.source_document_id if pkg else None

    def _node_exists(node_id: str) -> bool:
        # 法律规则的引用指向法条知识库而非项目文档（设计稿 §4.4）：
        # 真实核验"该法规该条是否在知识库里"，查无即 CITATION_UNREACHABLE。
        from app.rules.law_kb import LAW_PREFIX, node_exists as law_node_exists

        if node_id.startswith(LAW_PREFIX):
            return law_node_exists(node_id)
        if source_doc is None:
            return False
        with session_scope() as s:
            return s.execute(
                select(DocNode.id).where(DocNode.document_id == source_doc, DocNode.node_id == node_id)
            ).first() is not None

    findings = run_checks(snapshot, node_exists=_node_exists)
    with session_scope() as s:
        from sqlalchemy import delete

        # 校验结果是"当前草稿"的派生数据：直接清空重写
        s.execute(delete(PackageValidation).where(PackageValidation.package_id == package_id))
        for f in findings:
            s.add(
                PackageValidation(
                    package_id=package_id,
                    code=f.code,
                    severity=f.severity,
                    subject_type=f.subject_type,
                    subject_id=None,
                    message=f.message,
                    hint=f.hint,
                )
            )
    return [f.as_dict() for f in findings]


def compile_rules(
    project_id: str,
    document_id: int,
    actor: str,
    provider: str | None = None,
    model: str | None = None,
    merge_target_id: int | None = None,
    *,
    selection_meta_out: dict | None = None,
) -> int:
    """执行一次编译，返回新草稿规则包 id。

    merge_target_id 非空 = merge 模式（S1）：产物为临时编译包，指向模板包，
    供逐条覆盖匹配（设计稿 §5.1 阶段 A）。
    selection_meta_out 非空时被原地写入选段元数据（truncated/chars/tiers），供
    调用层（compile_rules_job）落 Job.result_json 审计——不改返回值契约。
    """
    from app.llm.registry import get_provider

    _text, doc = _load_tender_text(document_id)  # 校验 kind/parse_status 并取回 doc
    text, sel = select_compile_text(_load_tender_pages(document_id))
    if selection_meta_out is not None:
        selection_meta_out.update(sel)
    if sel["truncated"]:
        log.warning(
            "招标文件超长（%d 字符），按章节优先级选取 %d 字符，入选页 %s，档位命中 %s",
            sel["total_chars"], sel["chars"], sel.get("pages"), sel.get("tiers"),
        )

    gateway = get_gateway() if provider is None and model is None else Gateway(get_provider(provider, model))
    user_prompt = prompts.RULE_USER_TEMPLATE.format(clause_text=text)

    extraction: RuleExtraction | None = None
    result = None
    last_error: Exception | None = None
    for attempt in (1, 2):  # 契约不符允许一次修复重试（真实模型的常态，见 RPF 教训）
        try:
            prompt = user_prompt
            if attempt == 2 and last_error is not None:
                prompt = (
                    f"{user_prompt}\n\n【修复请求】你上一次的输出存在如下问题：\n{last_error}\n"
                    "请修正后重新输出**完整**的 JSON（不要只输出差异部分）。"
                )
            extraction, result = gateway.chat_json(
                prompts.RULE_SYSTEM_PROMPT,
                prompt,
                RuleExtraction,
                data_class=doc.data_class,
                max_tokens=config.llm_max_output_tokens(),
            )
            last_error = None
            break
        except LLMError as exc:
            last_error = exc
            log.warning("规则抽取第 %d 次尝试失败：%s", attempt, exc)
            if attempt == 1:
                time.sleep(config.llm_retry_backoff_s())  # 超时/限流后退避再试
    if extraction is None:
        raise AppError(
            f"规则抽取失败（含修复重试）：{last_error}；"
            "若持续失败，多为模型输出上限不足或模型上下文过小，请更换模型后重试"
        )

    version = _next_version(project_id)
    with session_scope() as s:
        pkg = RulePackage(
            project_id=project_id,
            version=version,
            status="draft",
            source_document_id=document_id,
            merge_target_id=merge_target_id,
            compiled_by=actor,
            model=result.model,
            prompt_version=None,
            created_by=actor,
        )
        s.add(pkg)
        s.flush()
        package_id = pkg.id

        seq = 0
        used_codes: set[str] = set()
        pending: list[tuple[int, int | None, ExtractedCriterion]] = []
        for c in extraction.criteria:
            seq += 1
            crit_id, cit_id = _persist_criterion(s, package_id, document_id, c, seq, used_codes)
            pending.append((crit_id, cit_id, c))

    # 事务已提交：核验引用（独立会话）→ 回填页码/条款节点 → 重新分诊
    _verify_and_backfill(document_id, pending)

    run_validations(package_id)
    return package_id


def _verify_and_backfill(
    document_id: int, pending: list[tuple[int, int | None, ExtractedCriterion]]
) -> None:
    """核验每条引用并把页码、条款节点回填到评分项，按核验结果重新分诊。"""
    from app.db.models import Criterion

    for crit_id, cit_id, c in pending:
        if cit_id is None:
            continue
        cit = verify_citation(cit_id)
        if cit is None:
            continue
        page_no = cit.page_no
        node_id = None
        if cit.verify_method is not None and page_no is not None:
            node_id = find_node_for_quote(document_id, page_no, c.quote or "")
        status = _triage(c, bool(cit.verified), node_id is not None)
        with session_scope() as s:
            crit = s.get(Criterion, crit_id)
            if crit is None:
                continue
            crit.source_page = page_no
            crit.source_node_id = node_id
            crit.status = status
            s.add(crit)


@register("compile_rules")
def compile_rules_job(ctx: JobContext, payload: dict) -> dict:
    project_id = payload["project_id"]
    document_id = int(payload["document_id"])
    actor = payload.get("actor", "system")
    provider = payload.get("provider")
    model = payload.get("model")
    merge_target_id = payload.get("merge_target_id")
    ctx.start(total=4, message="加载招标文件")
    _load_tender_text(document_id)
    ctx.advance(message="LLM 抽取评分规则")
    selection_meta: dict = {}
    package_id = compile_rules(
        project_id, document_id, actor, provider, model,
        merge_target_id=int(merge_target_id) if merge_target_id else None,
        selection_meta_out=selection_meta,
    )
    if selection_meta.get("truncated"):
        ctx.advance(message=(
            f"已按章节优先级选取 {selection_meta['chars']}/{selection_meta['total_chars']} 字符"
            f"（{len(selection_meta.get('pages') or [])} 页；档位命中 {selection_meta.get('tiers') or {}}）"
        ))
    else:
        ctx.advance(message="校验与分诊完成")

    result: dict = {"package_id": package_id}
    if selection_meta.get("truncated"):
        result["selection_meta"] = selection_meta
    if merge_target_id and config.auto_mode():
        # 全自动模式：合并配对自动采纳（覆盖/追加/弃用全留痕），不再进人工抽屉。
        # best-effort：合并处置失败不让编译失败，原因进 result_json 供前端提示。
        ctx.advance(message="自动合并：生成匹配提案并采纳")
        try:
            from app.services import merge_service

            target_id = merge_service.auto_resolve_merge(
                package_id, actor=actor, provider=provider, model=model)
            result.update(auto_resolved=True, target_package_id=target_id)
            # 合并后校验摘要：总分/价格公式可能被覆盖/追加破坏，
            # 自动路径只 merge、不 confirm/freeze，由前端提示人工修正。
            from app.services import rule_service

            findings = rule_service.get_validations(target_id)
            result["post_merge"] = {
                "error_codes": sorted({v.code for v in findings if v.severity == "error"}),
                "warn_codes": sorted({v.code for v in findings if v.severity != "error"}),
                "errors": [
                    {"code": v.code, "message": v.message}
                    for v in findings
                    if v.severity == "error"
                ],
            }
        except Exception as exc:  # noqa: BLE001 —— 人工兜底通道保留（匹配提案抽屉）
            log.warning("自动合并失败（可人工在匹配提案抽屉处置）：%s", exc)
            result.update(auto_resolved=False, auto_resolve_error=str(exc)[:300])

    ctx.advance(message="完成")
    return result
