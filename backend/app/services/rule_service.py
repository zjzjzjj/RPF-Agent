"""规则包服务：草稿 → 确认 → 冻结 → 新版本 的状态机。

不变式（confirm/freeze 前置，全部确定性校验）：
- 无 error 级校验发现；
- 每条评分项 status = verified（needs_input/ambiguous/gap 必须先经人工处置）。
冻结后所有写入一律 409；修订 = 复制为新草稿版本（parent_version_id 指向旧版）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Criterion,
    CriterionCheck,
    CriterionLevel,
    EvidenceCitation,
    PackageValidation,
    PriceFormula,
    ReviewRecord,
    RulePackage,
    utcnow,
)
from app.db.session import session_scope
from app.deps import get_actor  # noqa: F401  （API 层使用）
from app.errors import Conflict, FrozenPackageError, NotFound, ValidationFailed
from app.evidence.verifier import verify_citation
from app import config
from app.rules import compiler
from app.rules.freeze import build_snapshot, content_hash


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def get_package(package_id: int) -> RulePackage:
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is None:
            raise NotFound("规则包不存在")
        s.expunge(pkg)
        return pkg


def current_package(project_id: str) -> RulePackage | None:
    """项目当前生效的规则包（排除 superseded 旧版与 merge 辅助临时包）。"""
    with session_scope() as s:
        pkg = s.execute(
            select(RulePackage).where(
                RulePackage.project_id == project_id,
                RulePackage.status != "superseded",
                RulePackage.merge_target_id.is_(None),
            ).order_by(RulePackage.version.desc()).limit(1)
        ).scalar_one_or_none()
        if pkg is not None:
            s.expunge(pkg)
        return pkg


def current_template_package(project_id: str) -> RulePackage | None:
    """项目当前可合并的模板来源草稿包（origin='template' 且非 superseded）。"""
    with session_scope() as s:
        pkg = s.execute(
            select(RulePackage).where(
                RulePackage.project_id == project_id,
                RulePackage.origin == "template",
                RulePackage.status.in_(("draft", "confirmed")),
            ).order_by(RulePackage.version.desc()).limit(1)
        ).scalar_one_or_none()
        if pkg is not None:
            s.expunge(pkg)
        return pkg


def latest_frozen_package(project_id: str) -> RulePackage | None:
    """最新**冻结**版本（发起评分的默认输入）。

    不能拿 current_package：修订草稿会覆盖"最新版本"，而草稿不可用于评分。
    """
    with session_scope() as s:
        pkg = s.execute(
            select(RulePackage).where(
                RulePackage.project_id == project_id, RulePackage.status == "frozen"
            ).order_by(RulePackage.version.desc()).limit(1)
        ).scalar_one_or_none()
        if pkg is not None:
            s.expunge(pkg)
        return pkg


def edit_price_formula(
    package_id: int, patch: dict[str, Any], actor: str, reason: str
) -> PriceFormula:
    """人工补全/修正价格公式参数（抽取缺参时 confirm/freeze 会因校验阻断，这里是唯一出口）。"""
    if not reason.strip():
        raise ValidationFailed("修改价格公式必须填写理由（公式直接决定价格分）")
    allowed = {"formula_type", "benchmark_method", "trim_ratio", "k", "deduct_rate",
               "upper_bound", "lower_bound", "rounding", "exclude_invalid", "tax_basis",
               "params_json"}
    with session_scope() as s:
        pkg = _require_draft(package_id)
        formula = s.execute(
            select(PriceFormula).where(PriceFormula.package_id == package_id).limit(1)
        ).scalar_one_or_none()
        if formula is None:
            formula = PriceFormula(
                package_id=package_id, formula_type="benchmark_ratio", benchmark_method="average"
            )
            s.add(formula)
            s.flush()
        old = {k: str(getattr(formula, k)) for k in allowed}
        for key, value in patch.items():
            if key in allowed:
                setattr(formula, key, value)
        formula.confirmed_by = actor
        s.add(formula)
        s.add(
            ReviewRecord(
                subject_type="formula", subject_id=str(formula.id), action="edit",
                actor=actor, reason=reason,
                old_value_json=old,
                new_value_json={k: str(v) for k, v in patch.items() if k in allowed},
            )
        )
        s.flush()
        s.expunge(formula)
    compiler.run_validations(package_id)
    return formula


def list_criteria(package_id: int) -> list[Criterion]:
    with session_scope() as s:
        rows = s.execute(
            select(Criterion).where(Criterion.package_id == package_id).order_by(Criterion.sort_order)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def get_criterion(criterion_id: int) -> Criterion:
    with session_scope() as s:
        crit = s.get(Criterion, criterion_id)
        if crit is None:
            raise NotFound("评分项不存在")
        s.expunge(crit)
        return crit


def criterion_levels(criterion_id: int) -> list[CriterionLevel]:
    with session_scope() as s:
        rows = s.execute(
            select(CriterionLevel).where(CriterionLevel.criterion_id == criterion_id)
            .order_by(CriterionLevel.id)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def criterion_checks(criterion_id: int) -> list[CriterionCheck]:
    with session_scope() as s:
        rows = s.execute(
            select(CriterionCheck).where(CriterionCheck.criterion_id == criterion_id)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def get_price_formula(package_id: int) -> PriceFormula | None:
    with session_scope() as s:
        row = s.execute(
            select(PriceFormula).where(PriceFormula.package_id == package_id).limit(1)
        ).scalar_one_or_none()
        if row is not None:
            s.expunge(row)
        return row


def get_validations(package_id: int) -> list[PackageValidation]:
    with session_scope() as s:
        rows = s.execute(
            select(PackageValidation).where(PackageValidation.package_id == package_id)
            .order_by(PackageValidation.severity, PackageValidation.id)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def load_criteria_for_snapshot(s: Session, package_id: int) -> list[dict[str, Any]]:  # noqa: ANN001
    """把业务行组装成快照形状（校验器/冻结共用）。

    retired（人工判定"本项目无此项"）不进快照、不参与总分/权重校验（设计稿 §4.1）。
    """
    rows = s.execute(
        select(Criterion)
        .where(Criterion.package_id == package_id, Criterion.status != "retired")
        .order_by(Criterion.sort_order)
    ).scalars().all()
    level_rows = s.execute(
        select(CriterionLevel).order_by(CriterionLevel.id)
    ).scalars().all()
    check_rows = s.execute(select(CriterionCheck)).scalars().all()
    formula_row = s.execute(
        select(PriceFormula).where(PriceFormula.package_id == package_id).limit(1)
    ).scalar_one_or_none()

    levels_by_crit: dict[int, list[dict]] = {}
    for lv in level_rows:
        levels_by_crit.setdefault(lv.criterion_id, []).append(
            {"label": lv.level_label, "min": lv.score_min, "max": lv.score_max,
             "criteria": lv.description}
        )
    checks_by_crit: dict[int, CriterionCheck] = {c.criterion_id: c for c in check_rows}
    formula_by_crit: dict[int | None, dict] = {}
    if formula_row is not None:
        formula_by_crit[formula_row.criterion_id] = {
            "formula_type": formula_row.formula_type,
            "benchmark_method": formula_row.benchmark_method,
            # 快照是评分的唯一输入：公式的**全部**参数都要带上，
            # 漏一个字段就会让偏离扣分型公式退化成"人人满分"。
            "k": formula_row.k,
            "trim_ratio": formula_row.trim_ratio,
            "deduct_rate": formula_row.deduct_rate,
            "upper_bound": formula_row.upper_bound,
            "lower_bound": formula_row.lower_bound,
            "rounding": formula_row.rounding,
            "exclude_invalid": formula_row.exclude_invalid,
            "tax_basis": formula_row.tax_basis,
            "params_json": formula_row.params_json or {},
        }

    out: list[dict[str, Any]] = []
    for c in rows:
        formula = formula_by_crit.get(c.id)
        if formula is None and c.category == "price":
            formula = formula_by_crit.get(None)
        check = checks_by_crit.get(c.id)
        out.append(
            {
                "id": c.id,  # 合并匹配（S1）用；冻结快照不写该字段
                "code": c.code,
                "category": c.category,
                "name": c.name,
                "description": c.description,
                "max_score": c.max_score,
                "weight": c.weight,
                "score_type": c.score_type,
                "is_veto": c.is_veto,
                "veto_consequence": c.veto_consequence,
                "required_evidence": c.required_evidence,
                "source_node_id": c.source_node_id,
                "source_page": c.source_page,
                "source_quote": c.source_quote,
                "status": c.status,
                "sort_order": c.sort_order,
                "origin": c.origin,
                "template_code": c.template_code,
                "template_hash": c.template_hash,
                "library_version": c.library_version,
                "p_source": c.p_source,
                "signoff": c.signoff_json,
                "override": c.override_json,
                "levels": levels_by_crit.get(c.id, []),
                "check_type": check.check_type if check else None,
                "params": check.params_json if check else {},
                "formula": formula,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 草稿编辑（人工卡点 ①）
# ---------------------------------------------------------------------------
def _require_draft(package_id: int) -> RulePackage:
    """只有 draft 可编辑。

    confirmed 状态**不可编辑**：确认是评审准备阶段的责任点，确认后要改必须
    走 new_version（复制为新草稿）——否则"已确认"的语义会被静默破坏，
    且冻结时的校验清单可能与实际内容不符。
    """
    pkg = get_package(package_id)
    if pkg.status == "frozen":
        raise FrozenPackageError()
    if pkg.status == "superseded":
        raise Conflict("该版本已被新版本取代")
    if pkg.status == "confirmed":
        raise Conflict("规则包已确认，不可直接编辑；请先创建新版本（POST /versions）再修改")
    return pkg


def _record_edit(s: Session, crit: Criterion, field: str, old, new, actor: str, reason: str) -> None:  # noqa: ANN001
    edit = dict(crit.edit_json or {})
    edits = edit.get("edits", [])
    edits.append({"field": field, "old": str(old), "new": str(new), "by": actor,
                  "at": utcnow().isoformat(), "reason": reason})
    edit["edits"] = edits
    if "model_value" not in edit and old is not None:
        edit["model_value"] = {field: str(old)}
    crit.edit_json = edit


def edit_criterion(criterion_id: int, patch: dict[str, Any], actor: str, reason: str) -> Criterion:
    """人工修正评分项。保留模型原值，记录谁/何时/为什么。"""
    if not reason.strip():
        raise ValidationFailed("修改评分项必须填写理由")
    allowed = {"name", "description", "max_score", "weight", "category", "score_type",
               "is_veto", "veto_consequence", "required_evidence", "source_quote"}
    with session_scope() as s:
        crit = s.get(Criterion, criterion_id)
        if crit is None:
            raise NotFound("评分项不存在")
        _require_draft(crit.package_id)
        s.add(
            ReviewRecord(
                subject_type="criterion", subject_id=str(criterion_id), action="edit",
                actor=actor, reason=reason,
                old_value_json={k: str(getattr(crit, k)) for k in patch if k in allowed},
                new_value_json={k: str(v) for k, v in patch.items() if k in allowed},
            )
        )
        quote_changed = False
        for key, value in patch.items():
            if key in allowed:
                old = getattr(crit, key)
                if key == "source_quote" and value != old:
                    quote_changed = True
                setattr(crit, key, value)
                _record_edit(s, crit, key, old, value, actor, reason)
        crit.status = "verified"  # 人工处置过即视为已确认（理由已留痕）
        s.add(crit)
        s.flush()
        citation_id = None
        if quote_changed:
            # 引文被人工改正：同步到证据引用并重新核验，否则"无证据不评分"名存实亡
            # ——旧引文（模型抽短/抽错）核验不过，改了正文却不改证据，两者会长期不一致。
            cit = s.execute(
                select(EvidenceCitation).where(EvidenceCitation.criterion_id == criterion_id).limit(1)
            ).scalar_one_or_none()
            if cit is not None:
                cit.quote = crit.source_quote or ""
                cit.verified = False
                cit.verify_method = None
                s.add(cit)
                s.flush()
                citation_id = cit.id
        s.expunge(crit)
        package_id = crit.package_id
    if citation_id is not None:
        cit = verify_citation(citation_id)
        if cit is not None:
            with session_scope() as s:
                crit = s.get(Criterion, criterion_id)
                if crit is not None:
                    crit.source_page = cit.page_no
                    crit.source_node_id = compiler.find_node_for_quote(
                        cit.document_id or 0, cit.page_no, cit.quote or ""
                    )
                    s.add(crit)
    compiler.run_validations(package_id)
    return crit


def set_levels(criterion_id: int, levels: list[dict], actor: str, reason: str) -> Criterion:
    """人工设置/修正档位（歧义项的处置入口）。levels: [{label,min,max,criteria}]"""
    if not reason.strip():
        raise ValidationFailed("设置档位必须填写理由")
    with session_scope() as s:
        crit = s.get(Criterion, criterion_id)
        if crit is None:
            raise NotFound("评分项不存在")
        _require_draft(crit.package_id)
        s.add(
            ReviewRecord(
                subject_type="criterion", subject_id=str(criterion_id), action="set_levels",
                actor=actor, reason=reason,
                old_value_json={"levels": [str(l) for l in criterion_levels(criterion_id)]},
                new_value_json={"levels": [str(l) for l in levels]},
            )
        )
        from sqlalchemy import delete

        s.execute(delete(CriterionLevel).where(CriterionLevel.criterion_id == criterion_id))
        for lv in levels:
            s.add(
                CriterionLevel(
                    criterion_id=criterion_id,
                    level_label=lv["label"],
                    score_min=lv["min"],
                    score_max=lv["max"],
                    description=lv.get("criteria", ""),
                    source_quote=lv.get("quote"),
                )
            )
        if crit.status in ("ambiguous", "needs_input", "gap"):
            crit.status = "verified"
        s.add(crit)
        s.flush()
        s.expunge(crit)
        package_id = crit.package_id
    compiler.run_validations(package_id)
    return crit


def accept_all_criteria(package_id: int, actor: str, reason: str) -> dict:
    """批量采纳：对全部 status=='verified' 条目逐条写 accept 留痕（单事务，全成全败）。

    与逐条 accept_criterion 同语义（verified 条目的复核留痕，不改状态），
    仅省去逐次请求；2026-10-13 应用户要求移除"故意不提供全部采纳"限制。
    """
    if not reason.strip():
        raise ValidationFailed("批量采纳必须填写理由")
    _require_draft(package_id)
    accepted = 0
    with session_scope() as s:
        for crit in list_criteria(package_id):
            if crit.status != "verified":
                continue
            accepted += 1
            s.add(
                ReviewRecord(
                    subject_type="criterion", subject_id=str(crit.id), action="accept",
                    actor=actor, reason=reason,
                )
            )
    from app.services import audit_service

    audit_service.log(package_id, actor, "rules.accept_all", "package",
                      str(package_id), {"accepted": accepted, "reason": reason})
    return {"accepted": accepted}


def signoff_all_criteria(package_id: int, actor: str, reason: str) -> dict:
    """批量签批：对全部 origin=='template' 且 status=='needs_signoff' 条目执行签批。"""
    if not reason.strip():
        raise ValidationFailed("批量签批必须填写理由")
    _require_draft(package_id)
    signed = 0
    with session_scope() as s:
        for crit in list_criteria(package_id):
            if crit.origin != "template" or crit.status != "needs_signoff":
                continue
            crit.status = "verified"
            crit.signoff_json = {
                "actor": actor,
                "reason": reason,
                "time": utcnow().isoformat(),
            }
            s.add(crit)
            signed += 1
            s.add(
                ReviewRecord(
                    subject_type="criterion", subject_id=str(crit.id), action="signoff",
                    actor=actor, reason=reason,
                )
            )
    from app.services import audit_service

    audit_service.log(package_id, actor, "rules.signoff_all", "package",
                      str(package_id), {"signed_off": signed, "reason": reason})
    return {"signed_off": signed}


def accept_criterion(criterion_id: int, actor: str, reason: str) -> Criterion:
    """逐条确认（批量采纳见 accept_all_criteria）。"""
    if not reason.strip():
        raise ValidationFailed("确认必须填写理由")
    with session_scope() as s:
        crit = s.get(Criterion, criterion_id)
        if crit is None:
            raise NotFound("评分项不存在")
        _require_draft(crit.package_id)
        if crit.status != "verified":
            raise ValidationFailed(f"评分项状态为 {crit.status}，不能确认；请先补全或修正")
        crit.status = "verified"
        s.add(crit)
        s.add(
            ReviewRecord(
                subject_type="criterion", subject_id=str(criterion_id), action="accept",
                actor=actor, reason=reason,
            )
        )
        s.flush()
        s.expunge(crit)
        package_id = crit.package_id
    return crit


def add_criterion(package_id: int, body: dict, actor: str, reason: str) -> Criterion:
    """人工新增评分项（编号 x1、x2…）。"""
    if not reason.strip():
        raise ValidationFailed("新增评分项必须填写理由（评审期不得擅自新增标准，须有依据）")
    with session_scope() as s:
        pkg = _require_draft(package_id)
        existing = s.execute(
            select(Criterion.code).where(Criterion.package_id == package_id)
        ).scalars().all()
        n = 1
        while f"x{n}" in existing:
            n += 1
        crit = Criterion(
            package_id=package_id,
            code=f"x{n}",
            category=body["category"],
            name=body["name"],
            description=body.get("description"),
            max_score=body.get("max_score"),
            weight=body.get("weight"),
            score_type=body["score_type"],
            is_veto=bool(body.get("is_veto")),
            veto_consequence=body.get("veto_consequence"),
            source_quote=body.get("source_quote"),
            source_node_id=body.get("source_node_id"),
            status="verified",
            sort_order=900 + n,
        )
        s.add(crit)
        s.flush()
        s.add(
            ReviewRecord(
                subject_type="criterion", subject_id=str(crit.id), action="add",
                actor=actor, reason=reason,
            )
        )
        s.expunge(crit)
        version = pkg.version
    compiler.run_validations(package_id)
    _touch_package_updated(package_id)
    return crit


def signoff_criterion(criterion_id: int, actor: str, reason: str) -> Criterion:
    """模板兜底条目签批（设计稿 §6.2）。

    仅对 origin='template' 且 status='needs_signoff' 生效；签批后 status → verified，
    origin 仍是 template，冻结包与报告中永远标注"模板兜底（已签批）"。
    """
    if not reason.strip():
        raise ValidationFailed("签批必须填写理由（模板兜底是 P5 来源，启用必须有依据）")
    with session_scope() as s:
        crit = s.get(Criterion, criterion_id)
        if crit is None:
            raise NotFound("评分项不存在")
        _require_draft(crit.package_id)
        if crit.origin != "template":
            raise ValidationFailed("只有模板条目（origin=template）需要签批")
        if crit.status != "needs_signoff":
            raise ValidationFailed(f"评分项状态为 {crit.status}，不能签批")
        crit.signoff_json = {"by": actor, "at": utcnow().isoformat(), "reason": reason}
        crit.status = "verified"
        s.add(crit)
        s.add(
            ReviewRecord(
                subject_type="criterion", subject_id=str(criterion_id), action="signoff",
                actor=actor, reason=reason,
                old_value_json={"status": "needs_signoff"},
                new_value_json={"status": "verified", "signoff": crit.signoff_json},
            )
        )
        s.flush()
        s.expunge(crit)
        package_id = crit.package_id
    compiler.run_validations(package_id)
    return crit


def coverage_report(package_id: int) -> dict:
    """覆盖率报告（设计稿 §6.3）：文档覆盖 / 模板兜底已签批 / 待签批 / 已退休 / 人工。"""
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is None:
            raise NotFound("规则包不存在")
        rows = s.execute(
            select(Criterion).where(Criterion.package_id == package_id).order_by(Criterion.sort_order)
        ).scalars().all()

    def _bucket(c: Criterion) -> str:  # noqa: ANN001
        if c.origin == "law":
            # 法律层（P0）：权威原文 + 法条核验，免签批，自成一桶
            if c.status == "retired":
                return "retired"
            return "law"
        if c.origin == "template":
            if c.status == "retired":
                return "retired"
            if c.signoff_json is not None:
                return "template_fallback_signed"
            return "template_fallback_pending"
        if c.origin == "manual":
            return "manual"
        return "document_backed"

    counts = {"document_backed": 0, "template_fallback_signed": 0,
              "template_fallback_pending": 0, "retired": 0, "manual": 0, "law": 0}
    by_category: dict[str, dict] = {}
    essential_uncovered: list[str] = []
    for c in rows:
        cat = by_category.setdefault(c.category, {
            "total": 0, "document_backed": 0, "template_fallback_signed": 0,
            "template_fallback_pending": 0, "retired": 0, "manual": 0, "law": 0})
        bucket = _bucket(c)
        counts[bucket] += 1
        cat["total"] += 1
        cat[bucket] += 1
        if bucket == "template_fallback_pending" and (
            c.is_veto or c.category in ("qualification", "compliance", "price")
        ):
            essential_uncovered.append(c.template_code or c.code)

    price_rows = [c for c in rows if c.category == "price"]
    price_covered = bool(price_rows) and all(
        c.origin != "template" or c.signoff_json is not None for c in price_rows
    )
    veto_rows = [c for c in rows if c.is_veto]
    veto_covered = sum(
        1 for c in veto_rows if c.origin != "template" or c.signoff_json is not None
    )
    return {
        "total": len(rows),
        **counts,
        "by_category": by_category,
        "essential_uncovered": essential_uncovered,
        "price_covered": price_covered,
        "veto_covered_ratio": round(veto_covered / len(veto_rows), 4) if veto_rows else 1.0,
    }


def _touch_package_updated(package_id: int) -> None:
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is not None:
            pkg.updated_at = utcnow()


# ---------------------------------------------------------------------------
# 确认与冻结（人工卡点 ②；全自动模式下自动收敛阻断项）
# ---------------------------------------------------------------------------
def _auto_settle_blocking(package_id: int, actor: str) -> None:
    """全自动模式：把阻断 confirm/freeze 的非 verified 条目自动收敛并留痕。

    - needs_signoff（模板兜底）→ 补 signoff_json（等价人工签批的自动形态）；
    - 其余（needs_input/ambiguous/conflict…）→ 置 verified + edit_json 记录原状态。
    逐条写 ReviewRecord（actor="auto"），事后审计可完整还原"哪些项被自动采纳"。
    """
    with session_scope() as s:
        crits = s.execute(
            select(Criterion).where(Criterion.package_id == package_id)
        ).scalars().all()
        for c in crits:
            if c.status in ("verified", "retired"):
                continue
            old = c.status
            c.status = "verified"
            if c.origin == "template" and old == "needs_signoff":
                c.signoff_json = {
                    "actor": actor,
                    "reason": "全自动模式：模板兜底条目自动签批",
                    "time": utcnow().isoformat(),
                }
            c.edit_json = {
                "auto_mode": True,
                "from_status": old,
                "reason": "全自动模式：阻断确认/冻结的条目自动采纳并留痕",
            }
            s.add(c)
            s.add(
                ReviewRecord(
                    subject_type="criterion", subject_id=str(c.id), action="edit",
                    actor=actor,
                    reason=f"全自动模式自动采纳（原状态 {old}）",
                    old_value_json={"status": old},
                    new_value_json={"status": "verified"},
                )
            )


def _blocking_items(package_id: int) -> list[str]:
    crits = list_criteria(package_id)
    # retired（本项目无此项）不阻断：它已从快照/总分口径中排除
    return [
        f"{c.code} {c.name}（{c.status}）"
        for c in crits
        if c.status != "verified" and c.status != "retired"
    ]


def confirm_package(package_id: int, actor: str) -> RulePackage:
    pkg = _require_draft(package_id)
    if config.auto_mode():
        _auto_settle_blocking(package_id, actor)
    findings = compiler.run_validations(package_id)
    errors = [f for f in findings if f["severity"] == "error"]
    if errors:
        raise ValidationFailed("规则包存在校验错误，不能确认", blockers=errors)
    blocking = _blocking_items(package_id)
    if blocking:
        raise ValidationFailed("仍有未确认的评分项", blockers=[
            {"code": "ITEMS_UNCONFIRMED", "message": "、".join(blocking[:10])}])
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        pkg.status = "confirmed"
        pkg.confirmed_by = actor
        pkg.confirmed_at = utcnow()
        s.add(
            ReviewRecord(
                subject_type="package", subject_id=str(package_id), action="confirm",
                actor=actor, reason="确认规则草稿",
            )
        )
        s.flush()
        s.expunge(pkg)
        project_id = pkg.project_id
    from app.services import audit_service

    audit_service.log(project_id, actor, "rules.confirm", "package", str(package_id), {})
    return get_package(package_id)


def freeze_package(package_id: int, actor: str) -> RulePackage:
    """冻结：确认态 → 重跑校验（防"确认后被绕过"）→ 计算快照与哈希 → 之后只读。"""
    pkg = get_package(package_id)
    if pkg.status != "confirmed":
        raise ValidationFailed("只有已确认的规则包可以冻结")
    if config.auto_mode():
        _auto_settle_blocking(package_id, actor)

    # 冻结是最后一道闸门：即使确认时校验通过，这里也必须重跑并清零 error
    findings = compiler.run_validations(package_id)
    errors = [f for f in findings if f["severity"] == "error"]
    if errors:
        raise ValidationFailed("冻结前校验未通过（确认后被修改或数据异常）", blockers=errors)
    blocking = _blocking_items(package_id)
    if blocking:
        raise ValidationFailed("仍有未确认的评分项", blockers=[
            {"code": "ITEMS_UNCONFIRMED", "message": "、".join(blocking[:10])}])

    with session_scope() as s:
        criteria = load_criteria_for_snapshot(s, package_id)
        pkg_row = s.get(RulePackage, package_id)
        snapshot = build_snapshot(
            {"project_id": pkg_row.project_id, "version": pkg_row.version,
             "status": "frozen", "total_max": 100,
             "origin": pkg_row.origin,
             "template_id": pkg_row.template_id,
             "template_hash": pkg_row.template_hash,
             "library_version": pkg_row.library_version},
            criteria,
        )
        pkg_row.snapshot_json = snapshot
        pkg_row.content_hash = content_hash(snapshot)
        pkg_row.status = "frozen"
        pkg_row.frozen_by = actor
        pkg_row.frozen_at = utcnow()
        s.add(
            ReviewRecord(
                subject_type="package", subject_id=str(package_id), action="freeze",
                actor=actor, reason="冻结规则包（此后只读）",
                new_value_json={"content_hash": pkg_row.content_hash},
            )
        )
        s.flush()
        s.expunge(pkg_row)
        project_id = pkg_row.project_id
        frozen_hash = pkg_row.content_hash

    # 同项目旧冻结版本标记为 superseded
    with session_scope() as s:
        for other in s.execute(
            select(RulePackage).where(
                RulePackage.project_id == project_id,
                RulePackage.status == "frozen",
                RulePackage.id != package_id,
            )
        ).scalars():
            other.status = "superseded"
            s.add(other)

    from app.services import audit_service

    audit_service.log(project_id, actor, "rules.freeze", "package", str(package_id),
                      {"content_hash": frozen_hash})
    return get_package(package_id)


def new_version(package_id: int, actor: str, reason: str) -> RulePackage:
    """复制为新草稿（冻结后的修订入口）。"""
    if not reason.strip():
        raise ValidationFailed("创建新版本必须填写理由")
    pkg = get_package(package_id)
    with session_scope() as s:
        version = s.execute(
            select(RulePackage.version).where(RulePackage.project_id == pkg.project_id)
            .order_by(RulePackage.version.desc()).limit(1)
        ).scalar_one_or_none() or 0
        new_pkg = RulePackage(
            project_id=pkg.project_id,
            version=version + 1,
            status="draft",
            parent_version_id=package_id,
            source_document_id=pkg.source_document_id,
            compiled_by=actor,
            model=pkg.model,
            created_by=actor,
        )
        s.add(new_pkg)
        s.flush()
        new_id = new_pkg.id
        for c in s.execute(
            select(Criterion).where(Criterion.package_id == package_id)
        ).scalars().all():
            new_cid = _copy_criterion(s, c, new_id)
            for lv in s.execute(
                select(CriterionLevel).where(CriterionLevel.criterion_id == c.id)
            ).scalars().all():
                s.add(
                    CriterionLevel(
                        criterion_id=new_cid, level_label=lv.level_label,
                        score_min=lv.score_min, score_max=lv.score_max,
                        description=lv.description, source_quote=lv.source_quote,
                        source_node_id=lv.source_node_id,
                    )
                )
            for ck in s.execute(
                select(CriterionCheck).where(CriterionCheck.criterion_id == c.id)
            ).scalars().all():
                s.add(
                    CriterionCheck(
                        criterion_id=new_cid, check_type=ck.check_type, field=ck.field,
                        params_json=ck.params_json, expected=ck.expected,
                        on_fail=ck.on_fail, source_quote=ck.source_quote,
                    )
                )
            formula = s.execute(
                select(PriceFormula).where(
                    PriceFormula.package_id == package_id, PriceFormula.criterion_id == c.id
                )
            ).scalar_one_or_none()
            if formula is not None:
                s.add(
                    PriceFormula(
                        package_id=new_id, criterion_id=new_cid,
                        formula_type=formula.formula_type,
                        benchmark_method=formula.benchmark_method,
                        trim_ratio=formula.trim_ratio, k=formula.k,
                        deduct_rate=formula.deduct_rate,
                        upper_bound=formula.upper_bound, lower_bound=formula.lower_bound,
                        rounding=formula.rounding, exclude_invalid=formula.exclude_invalid,
                        tax_basis=formula.tax_basis, params_json=formula.params_json,
                        source_node_id=formula.source_node_id,
                        source_quote=formula.source_quote,
                        confirmed_by=formula.confirmed_by,
                    )
                )
        s.add(
            ReviewRecord(
                subject_type="package", subject_id=str(new_id), action="new_version",
                actor=actor, reason=reason,
                old_value_json={"parent": package_id},
            )
        )
        s.expunge(new_pkg)
        project_id = new_pkg.project_id
    from app.services import audit_service

    audit_service.log(project_id, actor, "rules.new_version", "package", str(new_id),
                      {"parent": package_id})
    return new_pkg


_CRIT_FIELDS = (
    "code", "category", "name", "description", "max_score", "weight", "score_type",
    "is_veto", "veto_consequence", "required_evidence", "source_node_id", "source_page",
    "source_quote", "status", "origin", "template_code", "template_hash", "library_version",
    "override_json", "signoff_json", "p_source", "edit_json", "confidence", "sort_order",
)


def _copy_criterion(s: Session, source: Criterion, new_package_id: int) -> int:  # noqa: ANN001
    new_c = Criterion(package_id=new_package_id, **{f: getattr(source, f) for f in _CRIT_FIELDS})
    s.add(new_c)
    s.flush()
    return new_c.id


def list_versions(project_id: str) -> list[RulePackage]:
    with session_scope() as s:
        rows = s.execute(
            select(RulePackage).where(RulePackage.project_id == project_id)
            .order_by(RulePackage.version)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def diff_versions(package_id: int, against_id: int) -> dict:
    """两个版本的评分项差异（按 code 对齐）。"""
    a = {c.code: c for c in list_criteria(package_id)}
    b = {c.code: c for c in list_criteria(against_id)}

    def _brief(c: Criterion) -> dict:
        return {"name": c.name, "max_score": str(c.max_score) if c.max_score else None,
                "score_type": c.score_type, "status": c.status}

    changed = []
    for code in sorted(set(a) & set(b)):
        if _brief(a[code]) != _brief(b[code]):
            changed.append({"code": code, "from": _brief(b[code]), "to": _brief(a[code])})
    return {
        "added": [{"code": c, **_brief(a[c])} for c in sorted(set(a) - set(b))],
        "removed": [{"code": c, **_brief(b[c])} for c in sorted(set(b) - set(a))],
        "changed": changed,
    }


def freeze_info(pkg: RulePackage) -> dict:
    return {
        "package_id": pkg.id,
        "version": pkg.version,
        "status": pkg.status,
        "content_hash": pkg.content_hash,
        "frozen_at": pkg.frozen_at.isoformat() if pkg.frozen_at else None,
        "frozen_by": pkg.frozen_by,
    }


def now_iso() -> str:
    return datetime.now().isoformat()
