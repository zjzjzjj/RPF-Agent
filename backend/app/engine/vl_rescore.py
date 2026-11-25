# -*- coding: utf-8 -*-
"""VL 事实 → score_count 重算适配器（2026-11-25 翻案通道收口）。

run160 实证：build_objective_plan 对 person_scope 业绩格（objective_fastpath
L145-149 显式 return None）与 count 型证书格（L171 兜底 None）无确定性算分
路径，VL 报的事实无人消费（no_deterministic_rescore）→ 误杀扩大。本模块从
merge_vl_facts 的派生 inventory 构造 score_count 输入重算——**只在
_maybe_vl_rescue 翻案通道生效**，主路径（build_objective_plan/score_objective/
fastpath_cell 定分语义、score_count 冻结分支）零改动。

红线：
- 判定只依据细则原文（required_artifacts 缺省时按 _verified_count 同口径合成）；
- owner_proof/业主证明**绝不映射 work_proof**（overlay 输入侧剔除业主/用户证明页）；
- cert_section_present 三态直传（audit["cert_section_state"]，绝不传压扁 bool）；
- 确认缺判 0 不放宽（zero_reason 照 score_count 原语义透传）；
- 无法构造/重算失败 → 返 None，调用方维持 no_deterministic_rescore 放弃语义。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from app.engine.material_inventory import MaterialInventory
from app.engine.objective import (
    normalize_cert, parse_required_artifacts, score_count, type_gate_state,
)
from app.engine.objective_fastpath import FastPathResult, _cap, _rule_text

_PERSON_STATE_MAP = (("work_proof", "work_proof"),
                     ("experience_proof", "experience_proof"),
                     ("social_security", "social_security"),
                     ("cert_proof", "cert_proof"))


def _tri_max(a: bool | None, b: bool | None) -> bool | None:
    """三态取强（只升不降）：True > None > False。"""
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def _infer_field(criterion: dict) -> str:
    params = dict(criterion.get("params") or {})
    field = str(params.get("field") or "")
    if field:
        return field
    try:
        from app.engine.objective import _infer_count_field

        return str(_infer_count_field(criterion) or "")
    except Exception:  # noqa: BLE001
        return ""


def _synthesize_requirements(scoring: dict) -> None:
    """required_artifacts 缺省时从规则文本合成（照 _verified_count 同口径）——
    截图硬闸/人员材料闸都读它，缺了会静默失效。"""
    if str(scoring.get("required_artifacts") or ""):
        return
    rule_text = (str(scoring.get("description") or "")
                 + "\n" + str(scoring.get("source_quote") or ""))
    material_names = parse_required_artifacts(rule_text)
    if "【证明材料】" in rule_text or re.search(
            r"提供.{0,30}(?:合同|发票|验收|业主证明|社保)", rule_text):
        requirements = rule_text
    elif material_names:
        requirements = "、".join(material_names)
    else:
        return
    scoring["required_artifacts"] = requirements
    scoring["required_artifacts_raw"] = requirements


def _wrap(criterion: dict, result, extra_note: str = "") -> FastPathResult:
    score = _cap(Decimal(str(result.score or 0)), criterion)
    zero_reason = getattr(result, "zero_reason", None)
    unresolved = getattr(result, "unresolved", None)
    detail = str(result.detail or "")
    if extra_note:
        detail += extra_note
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict=("pass" if score > 0 else
                 ("concern" if zero_reason not in (None, "confirmed_absent") else "fail")),
        facts={"detail": detail},
        pages=[],
        zero_reason=zero_reason,
        confidence="medium",
        complete=not bool(unresolved),
    )


def rescore_from_merged_facts(
    criterion: dict, inventory: MaterialInventory,
) -> FastPathResult | None:
    """build_objective_plan 不适用的格（证书计数/人员业绩）从合并后 inventory
    构造 score_count 输入重算。返回 None = 无法构造/重算失败（安全态）。"""
    params = dict(criterion.get("params") or {})
    if params.get("person_scope"):
        text = _rule_text(criterion) + str(criterion.get("name") or "")
        if any(k in text for k in ("业绩", "合同金额", "类似项目")):
            return _rescore_person_cases(criterion, inventory, params)
        return None  # 人员级证书格等：维持放弃（不硬算）
    if _infer_field(criterion) in ("certificates", "cmmi"):
        return _rescore_cert(criterion, inventory, params)
    return None


# ---------------------------------------------------------------------------
# 证书格（c44/c45：field=certificates / cmmi）

def _rescore_cert(criterion: dict, inventory: MaterialInventory,
                  params: dict) -> FastPathResult | None:
    facts = inventory.to_bid_facts()
    # cert_backing：记录级（VL 图上见到复印件=翻案 True）+ 文本期逐本定位，
    # 归一键三态只升不降
    backing: dict[str, bool | None] = {}
    for c in inventory.certs:
        key = c.normalized or normalize_cert(c.name)
        if key:
            backing[key] = _tri_max(backing.get(key), c.backing)
    for k, st in (inventory.cert_backing or {}).items():
        nk = normalize_cert(str(k))
        if nk:
            backing[nk] = _tri_max(backing.get(nk), st)
    # cert_section_present 三态直传：build_inventory 的 bool 是压扁值（None→False），
    # 用 audit 原三态 + VL has_query_screenshot 升级——截图三态语义原样
    # （False 清零/None 照计+疑/VL true 升级），不私自裁决争议格
    section_state: bool | None = (
        True if inventory.cert_section_present is True
        else inventory.audit.get("cert_section_state"))
    scoring = {
        **criterion,
        "cert_backing": backing,
        "cert_section_present": section_state,
        "verified_facts": {"vl_rescore": True},
    }
    _synthesize_requirements(scoring)
    try:
        result = score_count(scoring, params, facts)
    except Exception:  # noqa: BLE001 —— managed 档位混杂等：降级非 managed 重试
        try:
            scoring.pop("verified_facts", None)
            result = score_count(scoring, params, facts)
        except Exception:  # noqa: BLE001
            return None
    return _wrap(criterion, result)


# ---------------------------------------------------------------------------
# person_scope 业绩格（c48/c49：项目负责人/驻场工程师业绩）

def _role_of(name: str, inventory: MaterialInventory) -> str | None:
    """人员归属→role：人员表行匹配（role 词经 _normalize_role）；无归属→None
    （交给 score_count 既有"退全部+疑问"/scope 过滤语义）。"""
    norm = "".join(str(name or "").split())
    if not norm:
        return None
    try:
        from app.engine.objective import _normalize_role
    except Exception:  # noqa: BLE001
        return None
    for p in inventory.persons or []:
        pname = "".join(str(p.name or "").split())
        if pname and (norm == pname or norm in pname or pname in norm):
            role = _normalize_role({"role": str(p.role or "")})
            if role:
                return role
    return None


def _case_from_record(rec, inventory: MaterialInventory,
                      factor_text: str) -> dict:
    case: dict[str, Any] = {
        "name": rec.name,
        "sanitized": "ok",           # 构造件默认 ok（AS 路只读值，不走 _sanitize_case）
        "case_source": "vl_rescore",
        "quote": rec.name,
    }
    if rec.amount_yuan is not None:
        case["amount_yuan"] = str(rec.amount_yuan)
    if rec.signed_date:
        case["sign_date"] = rec.signed_date
    if rec.person_names:
        case["name_reflected"] = True
        case["person_name"] = rec.person_names[0]
        for nm in rec.person_names:
            role = _role_of(nm, inventory)
            if role:
                case["role"] = role
                break
    # 类型闸：managed 强校验要求 bool（L1541 raise）——None→True 保留+疑问注记
    tstate = type_gate_state(f"{rec.name or ''} {rec.project_type or ''}", factor_text)
    case["type_eligible"] = True if tstate is None else bool(tstate)
    # 人员材料三态：VL person_states 起点（overlay 稍后只升不降并入）
    for vl_key, case_key in _PERSON_STATE_MAP:
        case[case_key] = (rec.person_states or {}).get(vl_key)
    # 项目级三态（_required_flags 点名合同/发票/验收时消费）
    for key in ("contract", "invoice", "acceptance"):
        st = (rec.materials_state or {}).get(key)
        if st is None and (rec.materials or {}).get(key):
            st = True
        case[key] = st
    return case


def _reconcile_person_states(cases: list[dict], prior: list[dict]) -> None:
    """overlay 后与 VL 先验合并：VL True 一律翻 True（图上见到扫描件材料）；
    否则 overlay True→True、False→False、其余 None。"""
    for case, pre in zip(cases, prior):
        for _vl_key, case_key in _PERSON_STATE_MAP:
            vl = pre.get(case_key)
            ov = case.get(case_key)
            if vl is True or ov is True:
                case[case_key] = True
            elif ov is False:
                case[case_key] = False
            else:
                case[case_key] = None


def _apply_overlays(cases: list[dict], inventory: MaterialInventory,
                    scope: str) -> None:
    """确定性 overlay 补材料三态（scoring_service 同口径，只升不降）。

    红线：owner_proof/业主证明页不参与 work_proof 判定（输入侧剔除）——
    业主证明≠业绩证明（run160 人工口径：浩鲸/小河 c48"只提供业主证明"=0）。
    """
    try:
        from app.services.scoring_service import (
            _overlay_experience_proof, _overlay_leader_table_proof,
            _overlay_social_security, _overlay_work_proof,
        )
    except Exception:  # noqa: BLE001
        return
    pages = [(int(p.get("page_no") or 0), str(p.get("text") or ""),
              str(p.get("source") or "text")) for p in (inventory.pages or [])]
    if not pages:
        return
    excluded: set[int] = set()
    for rec in inventory.contracts or []:
        for p in (rec.materials or {}).get("owner_proof") or []:
            if p is not None:
                excluded.add(int(p))
    for p in inventory.pages or []:
        head = str(p.get("text") or "")[:120]
        if "业主证明" in head or "用户证明" in head:
            excluded.add(int(p.get("page_no") or 0))
    wp_pages = [t for t in pages if t[0] not in excluded]
    prior = [{k: c.get(k) for _vk, k in _PERSON_STATE_MAP} for c in cases]
    _overlay_work_proof(cases, wp_pages)
    _overlay_social_security(cases, pages)
    if scope == "leader":
        _overlay_experience_proof(cases, pages)
        _overlay_leader_table_proof(cases, pages)
    _reconcile_person_states(cases, prior)


def _rescore_person_cases(criterion: dict, inventory: MaterialInventory,
                          params: dict) -> FastPathResult | None:
    scope = str(params.get("person_scope") or "")
    factor_text = (str(criterion.get("name") or "")
                   + str(criterion.get("description") or ""))
    cases = [_case_from_record(rec, inventory, factor_text)
             for rec in inventory.contracts or []]
    cases = [c for c in cases if c.get("name")]
    if not cases:
        return None
    _apply_overlays(cases, inventory, scope)
    # 金额档预滤（managed 金额档要求每案例有已核验金额，否则 raise）
    tiers = params.get("tiers") or []
    amount_tier = bool(tiers) and any(
        isinstance(t, dict) and t.get("min_amount_yuan") is not None for t in tiers)
    note = ""
    if amount_tier or _decimal_or_none(params.get("min_amount_yuan")):
        kept = [c for c in cases if c.get("amount_yuan") not in (None, "")]
        if len(kept) != len(cases):
            note = f"（{len(cases) - len(kept)} 个案例无可核验金额，按金额门槛剔除）"
        cases = kept
        if not cases:
            return None
    scoring = {
        **criterion,
        "verified_cases": cases,
        "verified_facts": {"vl_rescore": True},
    }
    _synthesize_requirements(scoring)
    try:
        result = score_count(scoring, params, inventory.to_bid_facts())
    except Exception:  # noqa: BLE001 —— managed 档位混杂/类型 raise：降级重试
        try:
            scoring.pop("verified_facts", None)
            result = score_count(scoring, params, inventory.to_bid_facts())
        except Exception:  # noqa: BLE001
            return None
    return _wrap(criterion, result, extra_note=note)


def _decimal_or_none(value) -> Decimal | None:
    try:
        if value in (None, ""):
            return None
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
