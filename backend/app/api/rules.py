"""规则编译 / 审核 / 冻结 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.deps import get_actor
from app.errors import NotFound
from app.schemas.rules import (
    CompileRequest,
    CoverageOut,
    CriterionDetailOut,
    CriterionOut,
    CriterionPatch,
    DiffOut,
    InstantiateBody,
    LawImportBody,
    LawImportOut,
    LevelsBody,
    LevelOut,
    MatchProposalOut,
    NewCriterionBody,
    PackageDetailOut,
    PackageOut,
    PriceFormulaPatch,
    ReasonBody,
    ResolveProposalBody,
    SignoffBody,
    TemplateCriterionOut,
    TemplateDetailOut,
    TemplateSummaryOut,
    ValidationOut,
)
from app.services import (
    audit_service,
    law_import_service,
    library_service,
    merge_service,
    rule_service,
)

router = APIRouter(tags=["rules"])


@router.get("/models")
def list_models() -> dict:
    """可选模型目录（供编译/评分时选择）。"""
    from app import config

    return {
        "default_model": config.llm_model(),
        "api_key_configured": config.llm_api_key_configured(),
        "models": config.model_presets(),
    }


@router.post("/projects/{project_id}/rules/compile")
def compile_rules(project_id: str, body: CompileRequest, actor: str = Depends(get_actor)) -> dict:
    from app.jobs.queue import submit

    merge_target_id = body.merge_target_id
    if body.mode == "merge":
        # S1：merge 模式必须指向一个模板来源草稿包
        if merge_target_id is None:
            tpl_pkg = rule_service.current_template_package(project_id)
            if tpl_pkg is None:
                from app.errors import ValidationFailed

                raise ValidationFailed("项目没有可合并的模板规则包")
            merge_target_id = tpl_pkg.id

    job_id = submit(
        "compile_rules",
        {"project_id": project_id, "document_id": body.document_id,
         "actor": actor, "provider": body.provider, "model": body.model,
         "mode": body.mode, "merge_target_id": merge_target_id},
        project_id=project_id,
    )
    audit_service.log(project_id, actor, "rules.compile", "document", str(body.document_id),
                      {"provider": body.provider, "model": body.model, "mode": body.mode})
    return {"job_id": job_id}


@router.get("/projects/{project_id}/rules", response_model=PackageDetailOut)
def get_current_rules(project_id: str) -> PackageDetailOut:
    pkg = rule_service.current_package(project_id)
    if pkg is None:
        return PackageDetailOut(id=0, project_id=project_id, version=0, status="none",
                                origin="document", template_id=None, template_hash=None,
                                library_version=None, content_hash=None, parent_version_id=None,
                                source_document_id=None, model=None, compiled_by=None,
                                confirmed_by=None, confirmed_at=None, frozen_by=None,
                                frozen_at=None)
    return _package_detail(pkg.id)


def _package_detail(package_id: int) -> PackageDetailOut:
    pkg = rule_service.get_package(package_id)
    detail = PackageDetailOut.model_validate(pkg)
    criteria = []
    for c in rule_service.list_criteria(package_id):
        cd = CriterionDetailOut.model_validate(c)
        cd.levels = [LevelOut.model_validate(lv) for lv in rule_service.criterion_levels(c.id)]
        cd.checks = [
            {"check_type": k.check_type, "field": k.field, "params": k.params_json,
             "on_fail": k.on_fail}
            for k in rule_service.criterion_checks(c.id)
        ]
        criteria.append(cd)
    detail.criteria = criteria
    detail.validations = [
        ValidationOut.model_validate(v).model_dump() for v in rule_service.get_validations(package_id)
    ]
    return detail


@router.get("/rules/packages/{package_id}", response_model=PackageDetailOut)
def get_package(package_id: int) -> PackageDetailOut:
    return _package_detail(package_id)


@router.get("/rules/packages/{package_id}/validations", response_model=list[ValidationOut])
def get_validations(package_id: int) -> list[ValidationOut]:
    return [ValidationOut.model_validate(v) for v in rule_service.get_validations(package_id)]


@router.patch("/rules/criteria/{criterion_id}", response_model=CriterionOut)
def patch_criterion(criterion_id: int, body: CriterionPatch, actor: str = Depends(get_actor)):
    patch = {k: v for k, v in body.model_dump().items() if k != "reason" and v is not None}
    crit = rule_service.edit_criterion(criterion_id, patch, actor, body.reason)
    audit_service.log(None, actor, "rules.criterion.edit", "criterion", str(criterion_id),
                      {"fields": sorted(patch)})
    return CriterionOut.model_validate(crit)


@router.post("/rules/criteria/{criterion_id}/levels", response_model=CriterionOut)
def set_levels(criterion_id: int, body: LevelsBody, actor: str = Depends(get_actor)):
    crit = rule_service.set_levels(criterion_id, body.levels, actor, body.reason)
    return CriterionOut.model_validate(crit)


@router.post("/rules/criteria/{criterion_id}/accept", response_model=CriterionOut)
def accept_criterion(criterion_id: int, body: ReasonBody, actor: str = Depends(get_actor)):
    crit = rule_service.accept_criterion(criterion_id, actor, body.reason)
    audit_service.log(None, actor, "rules.criterion.accept", "criterion", str(criterion_id), {})
    return CriterionOut.model_validate(crit)


@router.post("/rules/packages/{package_id}/criteria", response_model=CriterionOut)
def add_criterion(package_id: int, body: NewCriterionBody, actor: str = Depends(get_actor)):
    crit = rule_service.add_criterion(package_id, body.model_dump(), actor, body.reason)
    return CriterionOut.model_validate(crit)


@router.patch("/rules/packages/{package_id}/price-formula")
def patch_price_formula(package_id: int, body: PriceFormulaPatch, actor: str = Depends(get_actor)):
    """人工补全/修正价格公式参数（抽取缺参时 confirm/freeze 被校验阻断的唯一出口）。"""
    patch = {k: v for k, v in body.model_dump().items() if k != "reason" and v is not None}
    formula = rule_service.edit_price_formula(package_id, patch, actor, body.reason)
    audit_service.log(None, actor, "rules.formula.edit", "formula", str(formula.id),
                      {"fields": sorted(patch)})
    return {
        "id": formula.id,
        "formula_type": formula.formula_type,
        "benchmark_method": formula.benchmark_method,
        "k": str(formula.k) if formula.k is not None else None,
        "upper_bound": str(formula.upper_bound) if formula.upper_bound is not None else None,
        "deduct_rate": str(formula.deduct_rate) if formula.deduct_rate is not None else None,
        "confirmed_by": formula.confirmed_by,
    }


@router.post("/rules/packages/{package_id}/confirm", response_model=PackageOut)
def confirm_package(package_id: int, actor: str = Depends(get_actor)):
    pkg = rule_service.confirm_package(package_id, actor)
    return PackageOut.model_validate(pkg)


@router.post("/rules/packages/{package_id}/freeze", response_model=PackageOut)
def freeze_package(package_id: int, actor: str = Depends(get_actor)):
    pkg = rule_service.freeze_package(package_id, actor)
    return PackageOut.model_validate(pkg)


@router.post("/rules/packages/{package_id}/versions", response_model=PackageOut)
def new_version(package_id: int, body: ReasonBody, actor: str = Depends(get_actor)):
    pkg = rule_service.new_version(package_id, actor, body.reason)
    return PackageOut.model_validate(pkg)


@router.get("/rules/packages/{package_id}/versions", response_model=list[PackageOut])
def list_versions(package_id: int) -> list[PackageOut]:
    pkg = rule_service.get_package(package_id)
    return [PackageOut.model_validate(p) for p in rule_service.list_versions(pkg.project_id)]


@router.get("/rules/packages/{package_id}/diff", response_model=DiffOut)
def diff_versions(package_id: int, against: int = Query(...)) -> DiffOut:
    return DiffOut.model_validate(rule_service.diff_versions(package_id, against))


# ---------------------------------------------------------------------------
# 通用评分规则库（S0）：模板目录 / 实例化 / 签批 / 覆盖率
# ---------------------------------------------------------------------------
def _template_detail(tpl) -> TemplateDetailOut:  # noqa: ANN001
    """把 RuleTemplateFile 转成 API 输出（补 criteria_count / categories_summary）。"""
    cats: dict[str, int] = {}
    for c in tpl.criteria:
        cats[c.category] = cats.get(c.category, 0) + 1
    return TemplateDetailOut(
        template_id=tpl.template_id,
        name=tpl.name,
        method=tpl.method,
        library_version=tpl.library_version,
        criteria_count=len(tpl.criteria),
        categories_summary=cats,
        applicable=tpl.applicable,
        notes=tpl.notes,
        criteria=[TemplateCriterionOut.model_validate(c.model_dump()) for c in tpl.criteria],
    )


def _template_summary(tpl) -> TemplateSummaryOut:  # noqa: ANN001
    detail = _template_detail(tpl)
    return TemplateSummaryOut(
        template_id=detail.template_id,
        name=detail.name,
        method=detail.method,
        library_version=detail.library_version,
        criteria_count=detail.criteria_count,
        categories_summary=detail.categories_summary,
    )


@router.get("/rules/templates", response_model=list[TemplateSummaryOut])
def list_templates() -> list[TemplateSummaryOut]:
    """模板目录（设计稿 §4.2）。"""
    from app.rules.library.loader import load_templates

    return [_template_summary(t) for t in load_templates().values()]


@router.get("/rules/templates/{template_id}", response_model=TemplateDetailOut)
def get_template(template_id: str) -> TemplateDetailOut:
    """完整条目预览（前端选模板时展示）。"""
    from app.rules.library.loader import get_template as load_template

    return _template_detail(load_template(template_id))


@router.post("/projects/{project_id}/rules/instantiate")
def instantiate_rules(project_id: str, body: InstantiateBody, actor: str = Depends(get_actor)) -> dict:
    """把通用模板实例化为草稿规则包（同步执行，纯确定性复制）。"""
    package_id = library_service.instantiate_template(project_id, body.template_id, actor)
    audit_service.log(project_id, actor, "rules.instantiate", "package", str(package_id),
                      {"template_id": body.template_id})
    return {"package_id": package_id}


@router.post("/projects/{project_id}/rules/import-law", response_model=LawImportOut)
def import_law_rules(project_id: str, body: LawImportBody, actor: str = Depends(get_actor)) -> LawImportOut:
    """导入法律通用规则包（S-Law，P0 来源，免签批；同步、纯确定性复制）。

    Body = 制品 JSON 原样（{package, criteria}）。导入即跑校验器，findings 随响应返回；
    error 只在 confirm/freeze 闸门阻断。留痕在服务内（ReviewRecord + audit）。
    """
    result = law_import_service.import_law_package(
        project_id, {"package": body.package, "criteria": body.criteria}, actor
    )
    return LawImportOut(
        package_id=result["package_id"],
        version=result["version"],
        created=result["created"],
        imported=result["imported"],
        skipped_codes=result["skipped_codes"],
        dropped_nodes=result["dropped_nodes"],
        validations=result["validations"],
    )


@router.post("/rules/criteria/{criterion_id}/signoff", response_model=CriterionOut)
def signoff_criterion(criterion_id: int, body: SignoffBody, actor: str = Depends(get_actor)):
    """模板兜底条目签批（逐条责任，不提供"全部签批"）。"""
    crit = rule_service.signoff_criterion(criterion_id, actor, body.reason)
    audit_service.log(None, actor, "rules.criterion.signoff", "criterion", str(criterion_id), {})
    return CriterionOut.model_validate(crit)


@router.get("/rules/packages/{package_id}/coverage", response_model=CoverageOut)
def coverage(package_id: int) -> CoverageOut:
    """覆盖率报告（设计稿 §6.3）。"""
    return CoverageOut.model_validate(rule_service.coverage_report(package_id))


# ---------------------------------------------------------------------------
# 逐条覆盖（S1）：匹配提案 / 处置
# ---------------------------------------------------------------------------
@router.get("/rules/packages/{merge_package_id}/match-proposal", response_model=MatchProposalOut)
def match_proposal(merge_package_id: int) -> MatchProposalOut:
    """匹配提案（只读，设计稿 §5.5）。"""
    from app.errors import ValidationFailed
    from app.rules.merger import generate_match_proposal

    try:
        proposal = generate_match_proposal(merge_package_id)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return MatchProposalOut.model_validate(proposal)


@router.post("/rules/packages/{merge_package_id}/match-proposal/resolve")
def resolve_match_proposal(
    merge_package_id: int, body: ResolveProposalBody, actor: str = Depends(get_actor)
) -> dict:
    """执行匹配处置（人工终审生效，设计稿 §5.5）。返回合并后的模板包 id。"""
    target_id = merge_service.resolve_proposal(
        merge_package_id,
        [d.model_dump() for d in body.decisions],
        actor,
    )
    audit_service.log(None, actor, "rules.merge.resolve", "package", str(merge_package_id),
                      {"decisions": [d["action"] for d in body.model_dump()["decisions"]]})
    return {"target_package_id": target_id}
