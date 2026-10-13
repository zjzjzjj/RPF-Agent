"""逐条覆盖执行（S1）：匹配提案 → 人工确认 → 覆盖 / 追加 / 弃用。

设计稿 §5.3/§5.4：
- override：模板条目字段级替换为文档条目（code 保持模板编号），来源升级为 document；
- drop_template：模板条目人工判定"本项目无此项"→ retired（不进快照）；
- append_document：未匹配编译条目追加进模板包（origin=document）；
- reject = append_document（编译条目照常追加），模板条目保留待签批。

每个决定都必须填理由；执行后重跑校验（TOTAL_SCORE 等闸门照常生效）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select

from app.db.models import (
    Criterion,
    CriterionCheck,
    CriterionLevel,
    EvidenceCitation,
    OverrideRecord,
    PriceFormula,
    ReviewRecord,
    RulePackage,
    utcnow,
)
from app.db.session import session_scope
from app.errors import NotFound, ValidationFailed
from app.rules import compiler


def resolve_proposal(merge_package_id: int, decisions: list[dict], actor: str) -> int:
    """执行合并处置，返回模板包 id。

    decisions 每项：
      {"action": "override", "pair_id": "T-D", "reason": "..."}
      {"action": "drop_template", "template_criterion_id": T, "reason": "..."}
      {"action": "append_document" | "reject", "document_criterion_id": D, "reason": "..."}
    """
    from app.rules.merger import generate_match_proposal

    proposal = generate_match_proposal(merge_package_id)
    pair_by_id = {p["pair_id"]: p for p in proposal["pairs"]}

    with session_scope() as s:
        merge_pkg = _require_draft_pkg(s, merge_package_id)
        target_id = merge_pkg.merge_target_id
        if target_id is None:
            raise ValidationFailed("该规则包不是合并模式的临时编译包")
        target_pkg = _require_draft_pkg(s, target_id)

        for d in decisions:
            action = d.get("action")
            reason = str(d.get("reason") or "").strip()
            if not reason:
                raise ValidationFailed("每个处置决定都必须填写理由")
            if action == "override":
                pair = pair_by_id.get(str(d.get("pair_id") or ""))
                if pair is None:
                    raise ValidationFailed(f"无效的配对 pair_id：{d.get('pair_id')}")
                _apply_override(s, target_id, pair, actor, reason)
            elif action == "drop_template":
                _drop_template(s, target_id, int(d["template_criterion_id"]), actor, reason)
            elif action in ("append_document", "reject"):
                _append_document(s, target_id, int(d["document_criterion_id"]), actor, reason)
            else:
                raise ValidationFailed(f"未知处置动作：{action}")

        # 收尾：合并辅助包标记 superseded；模板包来源升级为 merged，
        # 并把来源文档指向招标文件（覆盖条目引用了该文档的条款节点）
        merge_pkg.status = "superseded"
        target_pkg.origin = "merged"
        if target_pkg.source_document_id is None:
            target_pkg.source_document_id = merge_pkg.source_document_id
        s.add(merge_pkg)
        s.add(target_pkg)

    compiler.run_validations(target_id)
    return target_id


def _require_draft_pkg(s, package_id: int) -> RulePackage:  # noqa: ANN001
    pkg = s.get(RulePackage, package_id)
    if pkg is None:
        raise NotFound("规则包不存在")
    if pkg.status != "draft":
        raise ValidationFailed("只有草稿规则包可以执行覆盖")
    return pkg


def _apply_override(s, target_package_id: int, pair: dict, actor: str, reason: str) -> None:  # noqa: ANN001
    """覆盖：模板条目 T 字段级替换为文档条目 D，来源升级为 document。"""
    t = s.get(Criterion, pair["template"]["id"])
    d = s.get(Criterion, pair["document"]["id"])
    if t is None or d is None or t.package_id != target_package_id:
        raise NotFound("覆盖涉及的评分项不存在")

    old = {
        "name": t.name, "max_score": str(t.max_score) if t.max_score else None,
        "status": t.status, "origin": t.origin,
    }
    # 字段级替换（code 保持模板编号不变 —— 编号稳定锚）
    t.name = d.name
    t.description = d.description
    t.max_score = d.max_score
    t.category = d.category
    t.score_type = d.score_type
    t.is_veto = d.is_veto
    t.veto_consequence = d.veto_consequence
    t.required_evidence = d.required_evidence
    t.source_node_id = d.source_node_id
    t.source_page = d.source_page
    t.source_quote = d.source_quote
    # 来源升级：origin → document，p_source 继承文档来源，status 继承六态分诊
    t.origin = "document"
    t.p_source = d.p_source or "P1"
    t.status = d.status
    t.signoff_json = None  # 已由文件覆盖，不再是模板兜底
    t.override_json = {
        "overridden_template_code": t.template_code,
        "match_method": pair["match_method"],
        "similarity": pair["similarity"],
        "document_node_id": d.source_node_id,
        "document_page": d.source_page,
        "document_quote": d.source_quote,
        "won_source": d.p_source or "P1",
        "lost_source": "P5",
        "by": actor,
        "at": utcnow().isoformat(),
        "reason": reason,
    }
    # 证据引用迁移到覆盖条目：保持 verified 证据链在目标包内可回溯
    cit = s.execute(
        select(EvidenceCitation).where(EvidenceCitation.criterion_id == d.id).limit(1)
    ).scalar_one_or_none()
    if cit is not None:
        cit.criterion_id = t.id
        s.add(cit)
    s.add(t)

    # 档位全删全建（复用 D 的档位）
    s.execute(delete(CriterionLevel).where(CriterionLevel.criterion_id == t.id))
    for lv in s.execute(
        select(CriterionLevel).where(CriterionLevel.criterion_id == d.id)
    ).scalars().all():
        s.add(CriterionLevel(
            criterion_id=t.id, level_label=lv.level_label,
            score_min=lv.score_min, score_max=lv.score_max,
            description=lv.description, source_quote=lv.source_quote,
            source_node_id=lv.source_node_id,
        ))

    # 检查原语全删全建
    s.execute(delete(CriterionCheck).where(CriterionCheck.criterion_id == t.id))
    for ck in s.execute(
        select(CriterionCheck).where(CriterionCheck.criterion_id == d.id)
    ).scalars().all():
        s.add(CriterionCheck(
            criterion_id=t.id, check_type=ck.check_type, field=ck.field,
            params_json=ck.params_json, expected=ck.expected,
            on_fail=ck.on_fail, source_quote=ck.source_quote,
        ))

    # 价格公式替换
    _replace_formula(s, t.id, d.id, target_package_id)

    _record(s, target_package_id, "override", t.id, d.id, pair.get("match_method"),
            pair.get("similarity"), t.p_source, "P5", d.source_quote,
            actor, reason, old)


def _append_document(s, target_package_id: int, d_id: int, actor: str, reason: str) -> None:  # noqa: ANN001
    """追加：未匹配编译条目复制进模板包（origin=document）。"""
    d = s.get(Criterion, d_id)
    if d is None:
        raise NotFound("编译条目不存在")
    existing = set(s.execute(
        select(Criterion.code).where(Criterion.package_id == target_package_id)
    ).scalars().all())
    code = d.code or f"m{d.id}"
    if code in existing:
        n = 2
        while f"{code}~{n}" in existing:
            n += 1
        code = f"{code}~{n}"

    new_c = Criterion(
        package_id=target_package_id,
        code=code,
        category=d.category,
        name=d.name,
        description=d.description,
        max_score=d.max_score,
        weight=d.weight,
        score_type=d.score_type,
        is_veto=d.is_veto,
        veto_consequence=d.veto_consequence,
        required_evidence=d.required_evidence,
        source_node_id=d.source_node_id,
        source_page=d.source_page,
        source_quote=d.source_quote,
        origin="document",
        p_source=d.p_source or "P1",
        status=d.status,
        confidence=d.confidence,
        sort_order=800 + d.sort_order,
    )
    s.add(new_c)
    s.flush()

    for lv in s.execute(
        select(CriterionLevel).where(CriterionLevel.criterion_id == d.id)
    ).scalars().all():
        s.add(CriterionLevel(
            criterion_id=new_c.id, level_label=lv.level_label,
            score_min=lv.score_min, score_max=lv.score_max,
            description=lv.description, source_quote=lv.source_quote,
            source_node_id=lv.source_node_id,
        ))
    for ck in s.execute(
        select(CriterionCheck).where(CriterionCheck.criterion_id == d.id)
    ).scalars().all():
        s.add(CriterionCheck(
            criterion_id=new_c.id, check_type=ck.check_type, field=ck.field,
            params_json=ck.params_json, expected=ck.expected,
            on_fail=ck.on_fail, source_quote=ck.source_quote,
        ))
    _replace_formula(s, new_c.id, d.id, target_package_id)

    _record(s, target_package_id, "append_document", None, d.id, "human", None,
            new_c.p_source, "P5", d.source_quote, actor, reason, None)


def _drop_template(s, target_package_id: int, t_id: int, actor: str, reason: str) -> None:  # noqa: ANN001
    """弃用模板条目：status → retired（不进快照、不参与计分）。"""
    t = s.get(Criterion, t_id)
    if t is None or t.package_id != target_package_id:
        raise NotFound("模板条目不存在")
    if t.origin != "template":
        raise ValidationFailed("只有模板条目（origin=template）可以弃用")
    t.status = "retired"
    s.add(t)
    _record(s, target_package_id, "drop_template", t.id, None, "human", None,
            None, "P5", None, actor, reason, {"status": t.status})


def _replace_formula(s, target_criterion_id: int, source_criterion_id: int, package_id: int) -> None:  # noqa: ANN001
    """把 source 的价格公式参数复制到 target（无则创建）。"""
    src = s.execute(
        select(PriceFormula).where(PriceFormula.criterion_id == source_criterion_id).limit(1)
    ).scalar_one_or_none()
    if src is None:
        return
    dst = s.execute(
        select(PriceFormula).where(PriceFormula.criterion_id == target_criterion_id).limit(1)
    ).scalar_one_or_none()
    if dst is None:
        dst = PriceFormula(package_id=package_id, criterion_id=target_criterion_id)
        s.add(dst)
    for field in ("formula_type", "benchmark_method", "trim_ratio", "k", "deduct_rate",
                  "upper_bound", "lower_bound", "rounding", "exclude_invalid", "tax_basis",
                  "params_json", "source_node_id", "source_quote"):
        setattr(dst, field, getattr(src, field))
    s.add(dst)


def _record(s, package_id: int, action: str, t_id: int | None, d_id: int | None,  # noqa: ANN001
            match_method: str | None, similarity: Decimal | float | None,
            won_source: str | None, lost_source: str | None, quote: str | None,
            actor: str, reason: str, old: dict | None) -> None:
    doc_id = None
    node_id = None
    page = None
    if d_id is not None:
        d = s.get(Criterion, d_id)
        if d is not None:
            row = s.execute(
                select(EvidenceCitation.document_id)
                .where(EvidenceCitation.criterion_id == d.id)
                .limit(1)
            ).scalar_one_or_none()
            doc_id = row
            node_id = d.source_node_id
            page = d.source_page
    s.add(OverrideRecord(
        package_id=package_id,
        merge_package_id=None,
        action=action,
        template_criterion_id=t_id,
        document_criterion_id=d_id,
        match_method=match_method,
        similarity=Decimal(str(similarity)) if similarity is not None else None,
        won_source=won_source,
        lost_source=lost_source,
        document_id=doc_id,
        document_node_id=node_id,
        document_page=page,
        quote=quote,
        confirmed_by=actor,
        reason=reason,
    ))
    subject_id = str(t_id if t_id is not None else d_id)
    s.add(ReviewRecord(
        subject_type="criterion",
        subject_id=subject_id,
        action=f"merge.{action}",
        actor=actor,
        reason=reason,
        old_value_json=old,
        new_value_json={"action": action, "match_method": match_method},
    ))