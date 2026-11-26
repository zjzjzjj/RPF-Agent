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
- 业主证明按条款明示的补证条件与人员、合同分别绑定；
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
    documented_project_type_state, normalize_cert, parse_required_artifacts, score_count,
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
    required_evidence = str(scoring.get("required_evidence") or "").strip()
    if required_evidence:
        raw = str(scoring.get("required_artifacts_raw") or "").strip()
        scoring["required_artifacts_raw"] = "\n".join(dict.fromkeys(filter(None, (raw, required_evidence))))
        if not scoring.get("required_artifacts"):
            scoring["required_artifacts"] = required_evidence
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
    verified = getattr(result, "score_verified_only", None)
    unverified = bool(getattr(result, "unverified_counted", 0))
    detail = str(result.detail or "")
    if extra_note:
        detail += extra_note
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict=("pass" if score > 0 else
                 ("concern" if zero_reason not in (None, "confirmed_absent") else "fail")),
        facts={"detail": detail, "unverified_score_included": unverified and (verified is None or verified < score),
               "score_verified_only": str(verified) if verified is not None else None if unverified else str(score)},
        pages=[],
        zero_reason=zero_reason,
        confidence="medium",
        complete=not unresolved and not unverified,
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
    from app.engine.material_inventory import platform_query_families, requires_platform_certificate_query

    facts = inventory.to_bid_facts()
    # cert_backing：记录级（VL 图上见到复印件=翻案 True）+ 文本期逐本定位，
    # 归一键三态只升不降
    backing: dict[str, bool | None] = {}
    for c in inventory.certs:
        key = c.normalized or normalize_cert(c.name)
        if key:
            backing[key] = _tri_max(backing[key], c.backing) if key in backing else c.backing
    for k, st in (inventory.cert_backing or {}).items():
        nk = normalize_cert(str(k))
        if nk:
            backing[nk] = _tri_max(backing[nk], st) if nk in backing else st
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
    missing_queries = False
    if params.get("field") == "certificates" and requires_platform_certificate_query(criterion):
        rule = _rule_text(criterion)
        needs_seal = bool(re.search(r"公章|盖章|签章", rule))
        queried = set(inventory.platform_query_certificates)
        for page in inventory.pages:
            queried.update(platform_query_families(str(page.get("text") or ""), require_seal=needs_seal))
        missing_queries = any(normalize_cert(cert) not in queried for cert in facts.certificates
                              if backing.get(normalize_cert(cert)) is not False)
        facts.certificates = [cert for cert in facts.certificates if normalize_cert(cert) in queried
                              and backing.get(normalize_cert(cert)) is True]
        scoring["cert_section_present"] = bool(queried)
    if params.get("field") == "cmmi" and (params.get("cert_min_level") or params.get("cert_require_current")):
        from app.engine.material_inventory import maturity_certificate_details

        visual_records = [{
            "name": cert.name, "backing": cert.backing, "cert_level": cert.level,
            "valid_from": cert.valid_from, "valid_until": cert.valid_until,
            "appraisal_id": cert.appraisal_id,
            "appraisal_is_lookup": cert.appraisal_is_lookup,
            "document_id": cert.document_id,
            "page_no": page_no,
        } for cert in inventory.certs if normalize_cert(cert.name) == "CMMI"
            for page_no in cert.pages or [None]]
        scoring["certificate_details"] = maturity_certificate_details(inventory.pages, visual_records)
    _synthesize_requirements(scoring)
    try:
        result = score_count(scoring, params, facts)
    except Exception:  # noqa: BLE001 —— managed 档位混杂等：降级非 managed 重试
        try:
            scoring.pop("verified_facts", None)
            result = score_count(scoring, params, facts)
        except Exception:  # noqa: BLE001
            return None
    wrapped = _wrap(criterion, result)
    if scoring.get("certificate_details"):
        wrapped.facts["certificate_details"] = scoring["certificate_details"]
    if missing_queries and section_state is False and wrapped.score == 0:
        # The reviewed proof section explicitly confirms that the required
        # platform screenshots are absent, so zero is a final evidence result.
        wrapped.complete = True
        wrapped.verdict = "fail"
        wrapped.zero_reason = "confirmed_absent"
    elif missing_queries:
        wrapped.complete = False
    if ("截图" in scoring.get("required_artifacts_raw", "") and section_state is None
            and not requires_platform_certificate_query(criterion)):
        wrapped.complete = False
        wrapped.facts.update(unverified_score_included=True, score_verified_only=None)
    return wrapped


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
                      factor_text: str, person=None, *, allow_owner: bool = False,
                      min_social_months: int | None = None, proof: dict | None = None) -> dict:
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
    if person is not None:
        proof = proof or {}
        case["person_name"] = person.name
        case["role"] = "member"
        case["name_reflected"] = True
        case["work_proof"] = proof.get("work_proof")
        if allow_owner and proof.get("owner_proof") is True and (
                rec.materials_state.get("contract") is True or rec.materials.get("contract")):
            case["work_proof"] = True
        if (case["work_proof"] is True and re.search(r"加盖投标人公章|复印件加盖公章", factor_text)
                and rec.materials_state.get("seal") is not True):
            case["work_proof"] = None
        if min_social_months is not None:
            months = person.social_months
            case["social_security"] = (months >= min_social_months if months is not None else None)
        else:
            case["social_security"] = proof.get("social_security", person.social_security)
    elif rec.person_names:
        case["name_reflected"] = True
        case["person_name"] = rec.person_names[0]
        for nm in rec.person_names:
            role = _role_of(nm, inventory)
            if role:
                case["role"] = role
                break
    # 类型闸：managed 强校验要求 bool（L1541 raise）——None→True 保留+疑问注记
    tstate = documented_project_type_state(rec.project_type, rec.name, factor_text)
    case["type_eligible"] = True if tstate is None else bool(tstate)
    # 人员材料三态：VL person_states 起点（overlay 稍后只升不降并入）
    if person is None:
        for vl_key, case_key in _PERSON_STATE_MAP:
            case[case_key] = (rec.person_states or {}).get(vl_key)
    # 项目级三态（_required_flags 点名合同/发票/验收时消费）
    for key in ("contract", "invoice", "acceptance"):
        st = (rec.materials_state or {}).get(key)
        if st is None and (rec.materials or {}).get(key):
            st = True
        case[key] = st
    return case


def _named_case_proofs(rec, inventory: MaterialInventory, allow_owner: bool) -> dict[str, dict]:
    from app.parsing.normalize import normalize_no_ws

    proofs = {name: dict(flags) for name, flags in rec.person_proofs.items()}
    project = normalize_no_ws(rec.name)
    if not allow_owner or len(project) < 8:
        return proofs
    for page in inventory.pages:
        text = str(page.get("text") or "")
        if not re.search(r"业主证明|用户证明", text[:250]) or not re.search(r"参与|担任|驻场|运维服务人员", text):
            continue
        if re.search(r"须提供|请提供|每[个项人].{0,15}得分", text):
            continue
        normalized = normalize_no_ws(text)
        if project not in normalized:
            continue
        for person in inventory.persons:
            name = normalize_no_ws(person.name).lower()
            if len(name) >= 2 and name in normalized.lower():
                proofs.setdefault(name, {})["owner_proof"] = True
    return proofs


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


def _relax_person_states(cases: list[dict]) -> int:
    """人员材料放宽口径（2026-11-25 用户拍板）：业绩证明可见（work_proof=True，
    其本身已要求人名归属）即计分；经验证明/社保有线索即计入+标疑问——
    None 补 True（线索=归属+业绩证明），**False（可读确认缺）保持阻计**（红线：
    确认缺判 0 不放宽）。返回放宽条数。"""
    relaxed = 0
    for case in cases:
        if case.get("work_proof") is not True:
            continue
        for key in ("experience_proof", "social_security", "name_reflected"):
            if case.get(key) is None:
                case[key] = True
                relaxed += 1
    return relaxed


def _rescore_person_cases(criterion: dict, inventory: MaterialInventory,
                          params: dict) -> FastPathResult | None:
    scope = str(params.get("person_scope") or "")
    factor_text = _rule_text(criterion)
    strict_members = scope == "member" and "驻场" in factor_text
    if strict_members:
        min_months = re.search(r"(?:六|6)\s*个?\s*月", factor_text)
        people = {"".join(p.name.split()).lower(): p for p in inventory.persons
                  if re.search(r"驻场|工程师", p.role) and _role_of(p.name, inventory) != "leader"}
        allow_owner = bool(re.search(r"(?:否则|或|另)[^。；]{0,20}(?:业主|用户)证明", factor_text))
        cases = [_case_from_record(rec, inventory, factor_text, people[name], allow_owner=allow_owner,
                                   min_social_months=6 if min_months else None, proof=proof)
                 for rec in inventory.contracts for name, proof in _named_case_proofs(rec, inventory, allow_owner).items()
                 if name in people]
        cases.sort(key=lambda row: (row.get("type_eligible") is False, row.get("work_proof") is not True,
                                    row.get("social_security") is not True))
        unique = {}
        for case in cases:
            key = (case["person_name"], "".join(case["name"].split()), case.get("amount_yuan"), case.get("sign_date"))
            unique.setdefault(key, case)
        cases = list(unique.values())
        person_max = re.search(r"每(?:人|名[^。；，]{0,10}?)[^。；，]{0,12}?最高(?:得|计)?\s*(\d+(?:\.\d+)?)\s*分",
                               factor_text)
        per_case = _decimal_or_none(params.get("per_item"))
        if person_max and per_case and per_case > 0 and not params.get("tiers"):
            maximum_cases = int(Decimal(person_max[1]) / per_case)
            candidates_by_person = {}
            for case in cases:
                candidates_by_person.setdefault(case["person_name"], []).append(case)
            cases = [case for rows in candidates_by_person.values() for case in rows[:maximum_cases]]
    else:
        cases = [_case_from_record(rec, inventory, factor_text) for rec in inventory.contracts]
    cases = [c for c in cases if c.get("name")]
    if not cases:
        return None
    if strict_members:
        relaxed = 0
    else:
        _apply_overlays(cases, inventory, scope)
        relaxed = _relax_person_states(cases)
        if scope == "leader" and re.search(r"(?:六|6)\s*个?\s*月", factor_text):
            short_paid_leaders = {"".join(person.name.split()) for person in inventory.persons
                                  if person.social_months_from_register and person.social_months is not None
                                  and person.social_months < 6}
            for case in cases:
                name = "".join(str(case.get("person_name") or "").split())
                if name in short_paid_leaders:
                    case["social_security"] = False
    # 金额档预滤（managed 金额档要求每案例有已核验金额，否则 raise）
    tiers = params.get("tiers") or []
    amount_tier = bool(tiers) and any(
        isinstance(t, dict) and t.get("min_amount_yuan") is not None for t in tiers)
    note = ""
    if relaxed:
        note += (f"（{relaxed} 项经验证明/社保证明按放宽口径以线索计入并标疑问"
                 "（业绩证明可见+归属成立），需人工复核）")
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
