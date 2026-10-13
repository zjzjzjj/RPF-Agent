"""通用评分规则库服务：模板实例化（S0）。

设计稿 §4.2：instantiate 把模板 JSON 复制为草稿规则包（origin='template'），
每条评分项 status='needs_signoff'、p_source='P5'、盖上模板溯源三件套。
此后走与文档编译包完全相同的 confirm → freeze 链路（无演示旁路）。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

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
from app.errors import Conflict, NotFound
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