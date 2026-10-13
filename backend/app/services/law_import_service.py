"""法律通用规则包导入服务（S-Law，设计稿 §4.4）。

把法条级知识库编译出的通用规则 JSON（87 条 FORM/QUAL/RES/REJ）导入为项目规则包：
- 包级 origin='law'；条目 origin='law'、p_source='P0'、status='verified'、**免签批**
  （权威原文 + 法条知识库核验 = 免签批的依据，不是无依据放行）。
- source_node_id 为 `法条://` → 逐条过法条知识库核验，查无 → 422（不静默降级）；
  其它前缀（如 `招标文件://投标人须知前附表`）→ 置空并记入 dropped_nodes
  （项目尚无招标文件可核验，保留只会产生 CITATION 死引用）。
- 项目无包 → 创建（version=max+1）；已有 draft → 按 code 幂等追加；
  confirmed/frozen → 409（先走 /versions 建新草稿再导）。
- 导入即 run_validations：findings 进响应、不抛错——error 只在 confirm/freeze 闸门阻断。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

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
from app.rules.law_kb import LAW_PREFIX, load_index, node_exists as law_node_exists

CATEGORY_VOCAB = {"qualification", "compliance", "price", "technical", "commercial"}
SCORE_TYPE_VOCAB = {"veto", "formula", "count", "match", "subjective"}


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


# ---------------------------------------------------------------------------
# 写库前校验（结构与词表）→ 失败 422，零写入
# ---------------------------------------------------------------------------
def _validate_doc(package_doc: dict) -> list[dict]:
    if not isinstance(package_doc, dict):
        raise ValidationFailed("规则包 JSON 必须是对象", blockers=[{"reason": "not_an_object"}])
    criteria = package_doc.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValidationFailed("规则包缺少非空 criteria 列表",
                               blockers=[{"reason": "criteria_empty"}])
    blockers: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(criteria):
        if not isinstance(raw, dict):
            blockers.append({"index": i, "reason": "条目不是对象"})
            continue
        code = str(raw.get("code") or "").strip()
        if not code:
            blockers.append({"index": i, "reason": "缺少 code"})
            continue
        if code in seen:
            blockers.append({"code": code, "reason": "输入内 code 重复"})
            continue
        seen.add(code)
        if raw.get("category") not in CATEGORY_VOCAB:
            blockers.append({"code": code, "reason": f"category 不在词表：{raw.get('category')}"})
        if raw.get("score_type") not in SCORE_TYPE_VOCAB:
            blockers.append({"code": code, "reason": f"score_type 不在词表：{raw.get('score_type')}"})
        if not str(raw.get("name") or "").strip():
            blockers.append({"code": code, "reason": "缺少 name"})
    if blockers:
        raise ValidationFailed(f"规则包结构校验失败（{len(blockers)} 处）", blockers=blockers)
    return criteria


# ---------------------------------------------------------------------------
# 映射（纯函数：导入与 dry-run 共用）
# ---------------------------------------------------------------------------
def _map_formula(raw: dict) -> dict | None:
    """平铺公式字段（兼容嵌套 formula 对象）→ PriceFormula 行规格。

    formula_type 缺失 → None（不落行）：score_type=formula 的规则会在 confirm 的
    PRICE_PARAMS_INCOMPLETE 被兜住，比落一行残缺数据更诚实。
    """
    if not str(raw.get("formula_type") or "").strip():
        return None
    nested = raw.get("formula") if isinstance(raw.get("formula"), dict) else {}

    def pick(key: str) -> Any:
        v = raw.get(key)
        return nested.get(key) if v is None else v

    return {
        "formula_type": pick("formula_type"),
        "benchmark_method": pick("benchmark_method"),
        "k": _dec(pick("k")),
        "trim_ratio": _dec(pick("trim_ratio")),
        "deduct_rate": _dec(pick("deduct_rate")),
        "upper_bound": _dec(pick("upper_bound")),
        "lower_bound": _dec(pick("lower_bound")),
        "rounding": int(pick("rounding") or 2),
        "exclude_invalid": bool(nested.get("exclude_invalid", True)),
        "params_json": nested.get("params_json") or None,
    }


def _map_criterion(raw: dict, seq: int) -> dict:
    """单条规则 → 四张表的行规格（含 law 层固定语义）。"""
    score_type = raw["score_type"]
    is_veto = bool(raw.get("is_veto"))
    check_type = str(raw.get("check_type") or "").strip() or None
    if check_type is None:
        # 对齐 compiler 兜底：count 项缺 check_type 时按计数原语，其余转人工
        check_type = "count_at_least" if score_type == "count" else "human_only"
    params = dict(raw.get("params") or {})
    if raw.get("count_per_item") is not None and "per_item" not in params:
        params["per_item"] = str(raw["count_per_item"])  # 对齐 compiler:308-315 折叠
    source_quote = (str(raw.get("source_quote") or raw.get("quote") or "").strip() or None)
    return {
        "code": str(raw["code"]).strip(),
        "category": raw["category"],
        "name": str(raw["name"]).strip(),
        "description": (str(raw.get("description") or "").strip() or None),
        "max_score": _dec(raw.get("max_score")),
        "weight": _dec(raw.get("weight")),
        "score_type": score_type,
        "is_veto": is_veto,
        "veto_consequence": raw.get("veto_consequence") if is_veto else None,
        "source_node_id": (str(raw.get("source_node_id") or "").strip() or None),
        "source_quote": source_quote,
        "confidence": None,
        "sort_order": seq,
        "check_type": check_type,
        "params": params,
        "on_fail": "recommend_reject" if is_veto else "need_human",
        "levels": list(raw.get("levels") or []),
        "formula": _map_formula(raw),
    }


def _resolve_nodes(mapped: list[dict]) -> list[dict]:
    """法条节点核验（查无 → 422 fail-closed）+ 非法条节点置空（dropped_nodes 留痕）。"""
    law_nodes = [m for m in mapped if (m["source_node_id"] or "").startswith(LAW_PREFIX)]
    if law_nodes:
        if not load_index():
            raise ValidationFailed(
                "法条知识库不可读或为空，无法核验法条引用（fail-closed）",
                blockers=[{"reason": "law_kb_unavailable",
                           "hint": "设置 BID_EVAL_LAW_KB_PATH，或用 "
                                   "scripts/import_law_rules.py --law-kb 将知识库 vendoring 到 data/law_kb.json"}],
            )
        missing = [
            {"code": m["code"], "node_id": m["source_node_id"], "reason": "法条在知识库中不存在"}
            for m in law_nodes
            if not law_node_exists(m["source_node_id"])
        ]
        if missing:
            raise ValidationFailed(f"{len(missing)} 条法条引用无法核验", blockers=missing)
    dropped: list[dict] = []
    for m in mapped:
        node = m["source_node_id"]
        if node and not node.startswith(LAW_PREFIX):
            dropped.append({"code": m["code"], "node_id": node})
            m["source_node_id"] = None
    return dropped


def _snapshot_shape(m: dict) -> dict:
    """行规格 → 快照 criterion 形状（供 run_validators 纯内存校验）。"""
    return {
        "code": m["code"],
        "category": m["category"],
        "name": m["name"],
        "description": m["description"],
        "max_score": m["max_score"],
        "weight": m["weight"],
        "score_type": m["score_type"],
        "is_veto": m["is_veto"],
        "veto_consequence": m["veto_consequence"],
        "source_node_id": m["source_node_id"],
        "source_page": None,
        "source_quote": m["source_quote"],
        "status": "verified",
        "origin": "law",
        "p_source": "P0",
        "levels": [
            {
                "label": lv.get("label"),
                "min": _dec(lv.get("min")),
                "max": _dec(lv.get("max")),
                "criteria": lv.get("criteria"),
            }
            for lv in m["levels"]
        ],
        "check_type": m["check_type"],
        "params": m["params"],
        "formula": m["formula"],
    }


def validate_only(package_doc: dict) -> list[dict]:
    """不落库纯校验：与导入共用映射/核验，组快照跑 12 校验器（--dry-run 用）。"""
    from app.rules.validators import run_validators

    criteria = _validate_doc(package_doc)
    mapped = [_map_criterion(raw, i) for i, raw in enumerate(criteria, start=1)]
    _resolve_nodes(mapped)  # 法条核验失败 → 422；非法条节点在内存中置空
    snapshot = {
        "package": {"project_id": "-", "version": 1, "status": "draft", "total_max": 100},
        "criteria": [_snapshot_shape(m) for m in mapped],
    }
    return run_validators(snapshot, node_exists=lambda n: law_node_exists(n))


# ---------------------------------------------------------------------------
# 导入主流程
# ---------------------------------------------------------------------------
def _persist_criterion(s, package_id: int, m: dict, sort_order: int) -> None:  # noqa: ANN001
    """写入一条法律规则及其子结构。法条依据不建 EvidenceCitation（非项目文档）。"""
    crit = Criterion(
        package_id=package_id,
        code=m["code"],
        category=m["category"],
        name=m["name"],
        description=m["description"],
        max_score=m["max_score"],
        weight=m["weight"],
        score_type=m["score_type"],
        is_veto=m["is_veto"],
        veto_consequence=m["veto_consequence"],
        required_evidence=None,
        source_node_id=m["source_node_id"],
        source_page=None,
        source_quote=m["source_quote"],
        origin="law",
        template_code=None,
        template_hash=None,
        library_version=None,
        override_json=None,
        signoff_json=None,       # 免签批（TEMPLATE_FALLBACK_SIGNOFF 不命中 origin='law'）
        p_source="P0",           # P0 = 法律/行政法规/部门规章强制性规定
        status="verified",       # 权威原文 + 法条核验 → 无阻断项，可直接 confirm
        edit_json=None,
        confidence=None,
        sort_order=sort_order,
    )
    s.add(crit)
    s.flush()

    for lv in m["levels"]:
        s.add(
            CriterionLevel(
                criterion_id=crit.id,
                level_label=str(lv.get("label") or ""),
                score_min=_dec(lv.get("min")) or Decimal(0),
                score_max=_dec(lv.get("max")) or Decimal(0),
                description=str(lv.get("criteria") or ""),
                source_quote=lv.get("quote"),
            )
        )

    # 一 criterion 一行 check（load_criteria_for_snapshot:218 的既定假设）
    s.add(
        CriterionCheck(
            criterion_id=crit.id,
            check_type=m["check_type"],
            field=None,
            params_json=m["params"],
            on_fail=m["on_fail"],
            source_quote=m["source_quote"],
        )
    )

    if m["formula"] is not None:
        f = m["formula"]
        s.add(
            PriceFormula(
                package_id=package_id,
                criterion_id=crit.id,
                formula_type=f["formula_type"],
                benchmark_method=f["benchmark_method"],
                k=f["k"],
                trim_ratio=f["trim_ratio"],
                deduct_rate=f["deduct_rate"],
                upper_bound=f["upper_bound"],
                lower_bound=f["lower_bound"],
                rounding=f["rounding"],
                exclude_invalid=f["exclude_invalid"],
                params_json=f["params_json"],
                source_node_id=m["source_node_id"],
                source_quote=m["source_quote"],
            )
        )


def import_law_package(project_id: str, package_doc: dict, actor: str) -> dict:
    """法律通用规则包 → 项目草稿规则包（origin='law'）。"""
    criteria = _validate_doc(package_doc)
    mapped = [_map_criterion(raw, i) for i, raw in enumerate(criteria, start=1)]
    dropped = _resolve_nodes(mapped)

    meta = package_doc.get("package") if isinstance(package_doc.get("package"), dict) else {}
    artifact_sha = hashlib.sha256(
        json.dumps(package_doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    with session_scope() as s:
        project = s.get(Project, project_id)
        if project is None:
            raise NotFound("项目不存在")

        pkg = s.execute(
            select(RulePackage)
            .where(
                RulePackage.project_id == project_id,
                RulePackage.status != "superseded",
                RulePackage.merge_target_id.is_(None),
            )
            .order_by(RulePackage.version.desc())
            .limit(1)
        ).scalar_one_or_none()

        created = False
        if pkg is None:
            version = (
                s.execute(
                    select(func.max(RulePackage.version)).where(
                        RulePackage.project_id == project_id
                    )
                ).scalar_one()
                or 0
            ) + 1
            pkg = RulePackage(
                project_id=project_id,
                version=version,
                status="draft",
                origin="law",
                library_version=str(meta.get("version") or "") or None,
                source_document_id=None,
                compiled_by=actor,
                created_by=actor,
            )
            s.add(pkg)
            s.flush()
            created = True
        elif pkg.status != "draft":
            raise Conflict(
                f"当前规则包状态为 {pkg.status}，无法导入；"
                f"请先 POST /rules/packages/{pkg.id}/versions 创建新草稿版本"
            )

        existing = set(
            s.execute(select(Criterion.code).where(Criterion.package_id == pkg.id)).scalars()
        )
        max_sort = (
            s.execute(
                select(func.max(Criterion.sort_order)).where(Criterion.package_id == pkg.id)
            ).scalar_one()
            or 0
        )

        skipped: list[str] = []
        imported = 0
        for m in mapped:
            if m["code"] in existing:
                skipped.append(m["code"])
                continue
            max_sort += 1
            _persist_criterion(s, pkg.id, m, max_sort)
            imported += 1

        s.add(
            ReviewRecord(
                subject_type="package",
                subject_id=str(pkg.id),
                action="import_law",
                actor=actor,
                reason=(
                    f"导入法律通用规则包：{meta.get('name') or '（未命名）'}"
                    f"（v{meta.get('version') or '?'}）"
                ),
                new_value_json={
                    "artifact_name": meta.get("name"),
                    "artifact_version": meta.get("version"),
                    "artifact_source": meta.get("source"),
                    "artifact_sha256": artifact_sha,
                    "imported": imported,
                    "skipped_codes": skipped,
                    "dropped_nodes": dropped,
                },
            )
        )
        package_id, version_out = pkg.id, pkg.version

    from app.services import audit_service

    audit_service.log(
        project_id, actor, "rules.import_law", "package", str(package_id),
        {"artifact_sha256": artifact_sha, "imported": imported,
         "skipped": len(skipped), "dropped_nodes": len(dropped)},
    )

    # 导入即校验（同 instantiate 先例）：findings 落库、不抛错，error 由 confirm 闸门阻断
    compiler.run_validations(package_id)
    from app.services.rule_service import get_validations

    return {
        "package_id": package_id,
        "version": version_out,
        "created": created,
        "imported": imported,
        "skipped_codes": skipped,
        "dropped_nodes": dropped,
        "validations": get_validations(package_id),
    }
