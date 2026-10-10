"""规则编译 / 审核 / 冻结 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.deps import get_actor
from app.errors import NotFound
from app.schemas.rules import (
    CompileRequest,
    CriterionDetailOut,
    CriterionOut,
    CriterionPatch,
    DiffOut,
    LevelsBody,
    LevelOut,
    NewCriterionBody,
    PackageDetailOut,
    PackageOut,
    PriceFormulaPatch,
    ReasonBody,
    ValidationOut,
)
from app.services import audit_service, rule_service

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

    job_id = submit(
        "compile_rules",
        {"project_id": project_id, "document_id": body.document_id,
         "actor": actor, "provider": body.provider, "model": body.model},
        project_id=project_id,
    )
    audit_service.log(project_id, actor, "rules.compile", "document", str(body.document_id),
                      {"provider": body.provider, "model": body.model})
    return {"job_id": job_id}


@router.get("/projects/{project_id}/rules", response_model=PackageDetailOut)
def get_current_rules(project_id: str) -> PackageDetailOut:
    pkg = rule_service.current_package(project_id)
    if pkg is None:
        return PackageDetailOut(id=0, project_id=project_id, version=0, status="none",
                                content_hash=None, parent_version_id=None,
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
