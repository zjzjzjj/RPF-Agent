"""规则编译：招标文件条款 → LLM 抽取 → 草稿规则包 + 证据引用 + 六态分诊。

流程（对应方案 ③④ 步）：
1. 取招标文件全文（页序拼接，与 document_pages 逐字一致，保证引文可定位）；
2. 走 LLM 网关抽取（Mock=确定性正则 / 真实模型=语义抽取）；
3. 每条规则的 quote 建证据引用并核验（无证据不评分）；
4. 六态分诊：verified / needs_input / ambiguous（档位无分值）/ gap / conflict / invalid；
5. 跑 12 条确定性校验器，findings 落库。
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select

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
from app.parsing.normalize import normalize
from app.rules import prompts
from app.rules.model import ExtractedCriterion, RuleExtraction

log = logging.getLogger("bid_eval.rules")

MAX_COMPILE_CHARS = 30_000  # 超长文档的 MVP 截断（真实长文档按章节分段是后续优化）


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


def _next_version(project_id: str) -> int:
    with session_scope() as s:
        row = s.execute(
            select(RulePackage.version).where(RulePackage.project_id == project_id)
            .order_by(RulePackage.version.desc()).limit(1)
        ).scalar_one_or_none()
    return (row or 0) + 1


def find_node_for_quote(document_id: int, page_no: int | None, quote: str) -> str | None:
    """在给定页上找"包含该引文的最深节点"。"""
    q = normalize(quote)
    if not q:
        return None
    with session_scope() as s:
        rows = s.execute(
            select(DocNode).where(DocNode.document_id == document_id, DocNode.page == (page_no or -1))
        ).scalars().all()
    best: DocNode | None = None
    for n in rows:
        if q in normalize(n.text or ""):
            if best is None or n.depth > best.depth:
                best = n
    return best.node_id if best is not None else None


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
    crit = Criterion(        package_id=package_id,
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
) -> int:
    """执行一次编译，返回新草稿规则包 id。"""
    from app.llm.registry import get_provider

    text, doc = _load_tender_text(document_id)
    if len(text) > MAX_COMPILE_CHARS:
        text = text[:MAX_COMPILE_CHARS]
        log.warning("招标文件超长，编译截断至 %d 字符", MAX_COMPILE_CHARS)

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
            )
            last_error = None
            break
        except LLMError as exc:
            last_error = exc
            log.warning("规则抽取第 %d 次尝试失败：%s", attempt, exc)
    if extraction is None:
        raise AppError(f"规则抽取失败（含修复重试）：{last_error}")

    version = _next_version(project_id)
    with session_scope() as s:
        pkg = RulePackage(
            project_id=project_id,
            version=version,
            status="draft",
            source_document_id=document_id,
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
    ctx.start(total=3, message="加载招标文件")
    _load_tender_text(document_id)
    ctx.advance(message="LLM 抽取评分规则")
    package_id = compile_rules(project_id, document_id, actor, provider, model)
    ctx.advance(message="校验与分诊完成")
    ctx.advance(message="完成")
    return {"package_id": package_id}
