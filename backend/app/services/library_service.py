"""通用评分规则库服务：模板实例化（S0）。

设计稿 §4.2：instantiate 把模板 JSON 复制为草稿规则包（origin='template'），
每条评分项 status='needs_signoff'、p_source='P5'、盖上模板溯源三件套。
此后走与文档编译包完全相同的 confirm → freeze 链路（无演示旁路）。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app import config
from app.db.models import (
    Criterion,
    CriterionCheck,
    CriterionLevel,
    PriceFormula,
    Project,
    ReviewRecord,
    RulePackage,
)
from app.db.session import session_scope
from app.errors import Conflict, NotFound, ValidationFailed
from app.rules import compiler
from app.rules.library.loader import get_template, template_content_hash


def instantiate_template(project_id: str, template_id: str, actor: str) -> int:
    """把模板实例化为项目草稿规则包；返回 package_id。

    幂等限制（设计稿 §十一·开放问题 5）：仅限尚无规则包的新项目。
    """
    tpl = get_template(template_id)
    template_hash = template_content_hash(tpl)

    with session_scope() as s:
        project = s.get(Project, project_id)
        if project is None:
            raise NotFound("项目不存在")
        existing = s.execute(
            select(RulePackage.id).where(RulePackage.project_id == project_id)
        ).first()
        if existing is not None:
            raise Conflict("项目已有规则包；S0 不支持更换基线模板，请使用 new_version 修订")

        pkg = RulePackage(
            project_id=project_id,
            version=1,
            status="draft",
            origin="template",
            template_id=tpl.template_id,
            template_hash=template_hash,
            library_version=tpl.library_version,
            source_document_id=None,
            compiled_by=actor,
            created_by=actor,
        )
        s.add(pkg)
        s.flush()
        package_id = pkg.id

        for seq, c in enumerate(tpl.criteria, start=1):
            _persist_template_criterion(s, package_id, template_hash, tpl, c, seq)

        project.method = tpl.method
        s.add(project)
        s.add(
            ReviewRecord(
                subject_type="package", subject_id=str(package_id), action="instantiate",
                actor=actor, reason=f"实例化通用规则库模板：{tpl.name}（{tpl.library_version}）",
                new_value_json={"template_id": tpl.template_id, "template_hash": template_hash},
            )
        )

    from app.services import audit_service

    audit_service.log(project_id, actor, "rules.instantiate", "package", str(package_id),
                      {"template_id": tpl.template_id, "template_hash": template_hash})

    # 实例化后立刻跑校验：会有一批 TEMPLATE_FALLBACK_SIGNOFF error——这是特性，
    # 明示"模板条目尚无招标文件依据、未签批不得冻结"。
    compiler.run_validations(package_id)
    return package_id


def use_default_template(
    project_id: str, template_id: str, actor: str, reason: str | None = None
) -> dict:
    """无招标文件的一键启用：实例化（或复用草稿模板包）→ 全部签批 → 确认 → 冻结。

    - 无规则包：实例化 templates/{template_id}；
    - 已有 draft 且 origin in (template, merged)：直接复用（建项目时已选模板的场景，
      此时再实例化会 409）；
    - 已 frozen：幂等返回 already_frozen；
    - 其它来源（document/law）或 confirmed：409，提示先解冻创建新版本。
    """
    from app.services import rule_service

    reason = (reason or "").strip()
    if not reason:
        if not config.auto_mode():
            raise ValidationFailed("人工模式下启用默认规则必须填写理由")
        reason = f"启用内置默认规则模板（{template_id}）"
    pkg = rule_service.current_package(project_id)
    created = False
    if pkg is not None:
        if pkg.status == "frozen":
            return {
                "package_id": pkg.id,
                "ok": True,
                "frozen": True,
                "already_frozen": True,
                "created": False,
                "signed_off": 0,
                "blockers": [],
                "validations": _validation_dicts(pkg.id),
            }
        if not (pkg.status == "draft" and pkg.origin in ("template", "merged")):
            raise Conflict(
                "项目已有非模板规则包；如需更换默认规则，请先确认并冻结，再「解冻并创建新版本」"
            )
        package_id = pkg.id
    else:
        package_id = instantiate_template(project_id, template_id, actor)
        created = True

    signed = rule_service.signoff_all_criteria(package_id, actor, reason)
    signed_off = int((signed or {}).get("signed_off") or 0)
    try:
        rule_service.confirm_package(package_id, actor)
        rule_service.freeze_package(package_id, actor)
    except ValidationFailed as exc:
        blockers = (exc.detail or {}).get("blockers", []) if isinstance(exc.detail, dict) else []
        return {
            "package_id": package_id,
            "ok": False,
            "frozen": False,
            "already_frozen": False,
            "created": created,
            "signed_off": signed_off,
            "blockers": blockers,
            "validations": _validation_dicts(package_id),
            "message": exc.message,
        }
    return {
        "package_id": package_id,
        "ok": True,
        "frozen": True,
        "already_frozen": False,
        "created": created,
        "signed_off": signed_off,
        "blockers": [],
        "validations": _validation_dicts(package_id),
    }


def _validation_dicts(package_id: int) -> list[dict]:
    """把包级校验结果转成可直接序列化的 dict 列表。"""
    from app.services import rule_service

    return [
        {
            "code": v.code,
            "severity": v.severity,
            "message": v.message,
            "hint": v.hint,
        }
        for v in rule_service.get_validations(package_id)
    ]


def _persist_template_criterion(s, package_id: int, template_hash: str, tpl, c, seq: int) -> None:  # noqa: ANN001
    """写入一条模板评分项及其子结构。模板无原文：不建证据引用。"""
    crit = Criterion(
        package_id=package_id,
        code=c.template_code,          # 编号稳定锚：code = template_code（设计稿结论四）
        template_code=c.template_code,
        category=c.category,
        name=c.name,
        description=c.description,
        max_score=c.max_score,
        weight=None,                   # 权重永不进模板
        score_type=c.score_type,
        is_veto=bool(c.is_veto),
        veto_consequence=c.veto_consequence if c.is_veto else None,
        required_evidence=None,
        source_node_id=None,           # 模板无原文
        source_page=None,
        source_quote=None,             # 模板无原文（QUOTE_REQUIRED 按 origin 豁免）
        origin="template",
        template_hash=template_hash,
        library_version=tpl.library_version,
        p_source="P5",                 # 模板=示范文本来源（P5，不进执行链路，须签批）
        status="needs_signoff",        # 模板兜底待签批（设计稿 §4.1）
        confidence=None,
        sort_order=seq,
    )
    s.add(crit)
    s.flush()

    for lv in c.levels:
        s.add(
            CriterionLevel(
                criterion_id=crit.id,
                level_label=lv.label,
                score_min=lv.min if lv.min is not None else Decimal(0),
                score_max=lv.max if lv.max is not None else Decimal(0),
                description=lv.criteria,
                source_quote=None,
            )
        )

    for ck in c.checks:
        s.add(
            CriterionCheck(
                criterion_id=crit.id,
                check_type=ck.check_type,
                field=ck.field,
                params_json=ck.params,
                on_fail=ck.on_fail,
                source_quote=None,
            )
        )

    if c.formula is not None:
        s.add(
            PriceFormula(
                package_id=package_id,
                criterion_id=crit.id,
                formula_type=c.formula.formula_type,
                benchmark_method=c.formula.benchmark_method,
                k=c.formula.k,
                upper_bound=c.formula.upper_bound,
                lower_bound=c.formula.lower_bound,
                rounding=c.formula.rounding if c.formula.rounding is not None else 2,
                trim_ratio=c.formula.trim_ratio,
                deduct_rate=c.formula.deduct_rate,
                exclude_invalid=True,
                source_node_id=None,
                source_quote=None,
            )
        )