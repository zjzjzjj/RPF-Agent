# -*- coding: utf-8 -*-
"""VL 事实 → MaterialInventory 合并（2026-11-24「VL 报事实、代码算分」）。

模型只报逐案例/逐证书/逐人员结构化事实（vl_fallback.VlFact），算分由确定性
scorer（objective_fastpath.score_objective）执行。合并语义：

- **派生副本**：绝不改共享缓存 inventory（get_inventory 跨格共享）——VL 事实
  是格范围证据，泄漏进其他格就是越权采信；
- 合并键：(归一名称, 金额) 精确，或 金额相等且名称有重叠（_contract_name_overlap）
  → 同一案例；偏保守——宁误合（少计）勿漏合（双计放水）；
- 字段并集：pages/materials 取并；金额/日期文本非空优先、VL 只补空；
  materials_state **只升不降**：VL True 翻案成 True（含文本 False→True，
  VL 图上见到了材料原件）；VL None/false（false 已在 sanitize 归一 None）不动。
"""
from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from app.engine.material_inventory import (
    CertRecord, ContractRecord, MaterialInventory, PersonRecord, certificate_families,
    certificate_numbers, platform_query_details,
)
from app.engine.objective import normalize_cert


def _norm_name(name: str | None) -> str:
    return "".join(str(name or "").split()).lower()


def _amount_val(value) -> Decimal | None:
    try:
        if value in (None, ""):
            return None
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _names_match(left: str, right: str) -> bool:
    if not _norm_name(left) or not _norm_name(right):
        return False
    if _norm_name(left) == _norm_name(right):
        return True
    try:
        from app.engine.material_inventory import _contract_name_overlap

        return bool(_contract_name_overlap(left, right))
    except Exception:  # noqa: BLE001
        return False


def _same_case(rec: ContractRecord, name: str, amount: Decimal | None) -> bool:
    if amount is None or _amount_val(rec.amount_yuan) != amount:
        return False
    return _names_match(rec.name, name)


_ARTIFACT_AMOUNT_MAX = Decimal("1000")  # 显系伪迹的文本金额上限（0.01 元等误抽）
_PERSON_STATE_MAP = (("has_work_proof", "work_proof"),
                     ("has_experience_proof", "experience_proof"),
                     ("has_social_security", "social_security"),
                     ("has_cert_proof", "cert_proof"))


def _artifact_amount(text_amount: Decimal | None, vl_amount: Decimal | None) -> bool:
    """文本金额 <1000 元（小数点/OCR 误抽伪迹）且 VL 图上金额 ≥1000 → 可补正。

    单向：文本 ≥1000 一律文本优先；两值皆伪迹不动。2026-11-25 run160 实证：
    创联第二案例金额误抽 0.01 元被门槛砍掉（人工计满）。
    """
    return (text_amount is not None and text_amount < _ARTIFACT_AMOUNT_MAX
            and vl_amount is not None and vl_amount >= _ARTIFACT_AMOUNT_MAX)


def _merge_contract(contracts: list[ContractRecord], fact: dict,
                    ev_pages: list[int]) -> None:
    name = str(fact.get("name") or "").strip()
    amount = _amount_val(fact.get("amount_yuan"))
    if not name or (fact.get("amount_yuan") is not None and
                    (amount is None or not amount.is_finite() or amount <= 0)):
        return  # sanitize 已拦；防御
    state_map = (("has_contract", "contract"), ("has_invoice", "invoice"),
                 ("has_acceptance", "acceptance"), ("has_seal", "seal"))
    person_names = [str(n).strip() for n in (fact.get("person_names") or [])
                    if str(n).strip()]
    observations = [str(ref.get("observation") or "") for ref in fact.get("evidence") or []]
    observed = " ".join(observations)
    project_type = str(fact.get("project_type") or "").strip() or None
    if project_type and not any(
            re.search(r"(?:项目|系统|服务)(?:的)?(?:类型|类别)|(?:本项目|本合同)(?:明确)?(?:为|属于)", observation)
            and _norm_name(project_type) in _norm_name(observation) for observation in observations):
        project_type = None
    types_from_acceptance = [observation for observation in observations
                             if re.search(r"验收|终验", observation) and re.search(r"(?:项目|系统|服务)(?:类型|类别)", observation)]
    acceptance_type = bool(project_type and any(_norm_name(project_type) in _norm_name(observation)
                                               for observation in types_from_acceptance))
    name_proofs = {}
    for person in person_names:
        if _norm_name(person) not in _norm_name(observed):
            continue
        flags = {key: True for source, key in _PERSON_STATE_MAP if fact.get(source) is True}
        if fact.get("has_owner_proof") is True and any(word in observed for word in ("业主证明", "用户证明", "业主出具")):
            flags["owner_proof"] = True
        if flags:
            name_proofs[_norm_name(person)] = flags
    matches = [rec for rec in contracts if _same_case(rec, name, amount)]
    exact = [rec for rec in contracts if _norm_name(rec.name) == _norm_name(name)]
    signed = str(fact.get("sign_date") or "").strip()
    if signed:
        matches = [rec for rec in matches if not rec.signed_date or rec.signed_date == signed]
        exact = [rec for rec in exact if not rec.signed_date or rec.signed_date == signed]
    if len(matches) > 1:
        return
    target = matches[0] if matches else None
    if target is None and len(exact) == 1 and (amount is None or exact[0].amount_yuan is None):
        target = exact[0]
    if target is None and amount is None:
        return  # A fragment cannot add a contract or select between similarly named records.
    if target is None:
        # 伪迹同案匹配：同名/名称重叠 + 文本伪迹额（或两值皆伪迹）——0.01 记录
        # 与 VL 大额事实并成一条，防双计/漏计；两值皆伪迹只并案不改值
        for rec in contracts:
            rec_amt = _amount_val(rec.amount_yuan)
            if not _names_match(rec.name, name):
                continue
            if _artifact_amount(rec_amt, amount) or (
                    rec_amt is not None and rec_amt < _ARTIFACT_AMOUNT_MAX
                    and amount is not None and amount < _ARTIFACT_AMOUNT_MAX):
                target = rec
                break
    if target is None:
        state = {key: (True if fact.get(src) is True else None)
                 for src, key in state_map}
        materials = {}
        for key in ("contract", "invoice", "acceptance"):
            if state.get(key) is True and ev_pages:
                materials[key] = list(ev_pages)
        pstate = {key: (True if fact.get(src) is True else None)
                  for src, key in _PERSON_STATE_MAP}
        contracts.append(ContractRecord(
            name=name, amount_yuan=amount,
            signed_date=str(fact.get("sign_date") or "").strip() or None,
            pages=list(ev_pages), materials=materials, materials_state=state,
            project_type=project_type,
            person_names=person_names, person_states=pstate, person_proofs=name_proofs,
            source="vl",
        ))
        return
    # 并集：金额/日期文本非空优先、VL 补空（伪迹例外补正）；pages/materials 取并；
    # state 只升不降
    if target.amount_yuan is None:
        target.amount_yuan = amount
    elif _artifact_amount(_amount_val(target.amount_yuan), amount):
        target.amount_yuan = amount          # VL 补正伪迹金额
        target.amount_corrected = True       # 留痕（审计可见）
    if not target.signed_date and fact.get("sign_date"):
        target.signed_date = str(fact["sign_date"]).strip()
    if project_type and (not target.project_type or acceptance_type):
        target.project_type = project_type
    for p in ev_pages:
        if p not in target.pages:
            target.pages.append(p)
    for src, key in state_map:
        if fact.get(src) is True:
            # 只升不降：VL 图上确认有 → 翻成 True（含文本 False→True 翻案）
            target.materials_state[key] = True
            if key in ("contract", "invoice", "acceptance"):
                lst = target.materials.setdefault(key, [])
                for p in ev_pages:
                    if p not in lst:
                        lst.append(p)
    for n in person_names:
        if n not in target.person_names:
            target.person_names.append(n)
    for src, key in _PERSON_STATE_MAP:
        if fact.get(src) is True:
            target.person_states[key] = True   # 只升不降
    for name_key, proof in name_proofs.items():
        target.person_proofs.setdefault(name_key, {}).update(proof)


def _merge_cert(certs: list[CertRecord], fact: dict, ev_pages: list[int]) -> None:
    name = str(fact.get("name") or "").strip()
    if not name:
        return
    norm = normalize_cert(name)
    number = str(fact.get("certificate_number") or "").strip().upper()
    appraisal_id = str(fact.get("appraisal_id") or "").strip() or None
    lookup = bool(fact.get("appraisal_is_lookup"))
    document_ids = {ref.get("document_id") for ref in fact.get("evidence") or []}
    document_id = next(iter(document_ids)) if len(document_ids) == 1 else None
    if norm == "CMMI":
        # Keep visual maturity records independent so one certificate's level
        # cannot inherit the expiry of another certificate from the same family.
        target = next((c for c in certs if _norm_name(c.name) == _norm_name(name)
                       and c.appraisal_is_lookup == lookup
                       and appraisal_id and c.appraisal_id == appraisal_id
                       and c.document_id == document_id
                       and set(c.pages) & set(ev_pages)), None)
    else:
        target = next((c for c in certs if normalize_cert(c.name) == norm
                       and (not number or c.certificate_number == number)), None)
    if target is None:
        certs.append(CertRecord(
            name=name, normalized=norm,
            level=str(fact.get("cert_level") or "").strip() or None,
            valid_until=str(fact.get("valid_until") or "").strip() or None,
            pages=list(ev_pages),
            backing=True if ev_pages and not lookup else None,
            valid_from=str(fact.get("valid_from") or "").strip() or None,
            certificate_number=number or None,
            appraisal_id=appraisal_id,
            appraisal_is_lookup=lookup,
            document_id=document_id,
        ))
        return
    if not target.level and fact.get("cert_level"):
        target.level = str(fact["cert_level"]).strip()
    if not target.valid_until and fact.get("valid_until"):
        target.valid_until = str(fact["valid_until"]).strip()
    if not target.valid_from and fact.get("valid_from"):
        target.valid_from = str(fact["valid_from"]).strip()
    for p in ev_pages:
        if p not in target.pages:
            target.pages.append(p)
    if target.backing is not True and ev_pages and not lookup:
        target.backing = True


_PLATFORM_REFERENCE = re.compile(r"cx\.cnca\.cn|认证认可信息公共服务平台", re.I)
_CERT_COPY = re.compile(r"认证证书|证书(?:原件|复印件|扫描件)")
_CERT_SEAL = re.compile(r"(?:盖有|可见|加盖)[^，。；]{0,45}公章|红(?:色)?公章")
_CERT_EXPIRY = re.compile(r"(?:有效期(?:至|到)|到期日期)\s*[:：]?\s*20\d{2}")
_COPY_NEGATION = re.compile(r"(?:未见|未提供|缺少)(?:[^，。；]{0,12})(?:证书|公章)|未加盖|无公章|须提供|不得分")


def _cert_number_key(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _cert_number_matches(expected: str, observed: str) -> bool:
    left = _cert_number_key(expected)
    right = _cert_number_key(observed)
    if not left or not right:
        return False
    return left == right or (len(left) >= 6 and right.endswith(left)) or (
        len(right) >= 6 and left.endswith(right)
    )


def _merge_certificate_observations(certs: list[CertRecord], fact: dict) -> list[tuple]:
    """Read ISO copy and platform observations independently of optional model flags."""
    normalized = normalize_cert(str(fact.get("name") or ""))
    refs = [ref for ref in fact.get("evidence") or [] if ref.get("page_no") is not None]
    if not normalized.startswith("ISO"):
        if fact.get("has_query_screenshot") is not True or fact.get("has_cert_proof") is True:
            _merge_cert(certs, fact, sorted({int(ref["page_no"]) for ref in refs}))
        return []

    expected_number = str(fact.get("certificate_number") or "").strip().upper()
    copies = []
    queries = []
    has_platform_ref = False
    for ref in refs:
        observation = str(ref.get("observation") or "")
        if _PLATFORM_REFERENCE.search(observation):
            has_platform_ref = True
            families, numbers = platform_query_details(observation, require_seal=True)
            if families or numbers:
                queries.append((normalized, families, numbers, ref.get("document_id")))
            continue
        numbers = certificate_numbers(observation)
        if (ref.get("document_id") and len(numbers) == 1
                and (not expected_number
                     or any(_cert_number_matches(expected_number, number) for number in numbers))
                and certificate_families(observation) == {normalized}
                and _CERT_COPY.search(observation)
                and (_CERT_SEAL.search(observation) or fact.get("has_seal") is True)
                and _CERT_EXPIRY.search(observation) and not _COPY_NEGATION.search(observation)):
            copies.append(({**fact, "certificate_number": next(iter(numbers)), "evidence": [ref]},
                           int(ref["page_no"])))
    if copies:
        for copy, page in copies:
            _merge_cert(certs, copy, [page])
    elif (not has_platform_ref and fact.get("has_query_screenshot") is not True
          and (not expected_number or fact.get("has_cert_proof") is True)):
        _merge_cert(certs, fact, sorted({int(ref["page_no"]) for ref in refs}))
    return queries


def _merge_person(persons: list[PersonRecord], fact: dict, ev_pages: list[int]) -> None:
    name = str(fact.get("name") or "").strip()
    if not name:
        return
    observed = " ".join(str(ref.get("observation") or "") for ref in fact.get("evidence") or [])
    months = fact.get("social_security_months")
    if (not isinstance(months, int) or months < 0 or _norm_name(name) not in _norm_name(observed)
            or not any(word in observed for word in ("社保", "缴费", "参保", "社会保险"))):
        months = None
    role = str(fact.get("person_role") or "").strip()
    if not role or _norm_name(name) not in _norm_name(observed) or _norm_name(role) not in _norm_name(observed):
        role = ""

    def _years(raw) -> Decimal | None:
        try:
            if raw in (None, ""):
                return None
            return Decimal(str(raw).replace("年", "").strip())
        except (InvalidOperation, ValueError):
            return None

    target = next((p for p in persons if _norm_name(p.name) == _norm_name(name)), None)
    if target is None:
        persons.append(PersonRecord(
            name=name, role=role, title=str(fact.get("person_title") or "").strip(),
            years=_years(fact.get("person_years")),
            experience_pages=list(ev_pages), pages=list(ev_pages), social_months=months,
        ))
        return
    if not target.title and fact.get("person_title"):
        target.title = str(fact["person_title"]).strip()
    if not target.role and role:
        target.role = role
    if target.years is None:
        target.years = _years(fact.get("person_years"))
    if (months is not None and not target.social_months_from_register
            and (target.social_months is None or months > target.social_months)):
        target.social_months = months
    for p in ev_pages:
        if p not in target.pages:
            target.pages.append(p)
        if p not in target.experience_pages:
            target.experience_pages.append(p)


def merge_vl_facts(inventory: MaterialInventory,
                   vl_facts: list[dict]) -> MaterialInventory:
    """VL 事实并入派生副本（不触碰共享缓存）后返回新 inventory。"""
    contracts = [replace(
        r,
        pages=list(r.pages or []),
        materials={k: list(v or []) for k, v in (r.materials or {}).items()},
        materials_state=dict(r.materials_state or {}),
        person_names=list(r.person_names or []),
        person_states=dict(r.person_states or {}),
        person_proofs={name: dict(proof) for name, proof in r.person_proofs.items()},
    ) for r in inventory.contracts]
    certs = [replace(c, pages=list(c.pages or [])) for c in inventory.certs]
    persons = [replace(
        p, pages=list(p.pages or []), certs=list(p.certs or []),
        experience_pages=list(p.experience_pages or []),
    ) for p in inventory.persons]
    cert_section_present = inventory.cert_section_present
    # Main contract pages establish identity and total before detached proof
    # fragments are applied. This also handles batches arriving out of order.
    query_families = set(inventory.platform_query_certificates)
    observations_of_queries = []
    ordered_facts = sorted(vl_facts or [], key=lambda f: f.get("kind") == "contract" and f.get("amount_yuan") is None)
    for fact in ordered_facts:
        ev_pages = sorted({int(e["page_no"]) for e in (fact.get("evidence") or [])
                           if e.get("page_no") is not None})
        kind = str(fact.get("kind") or "")
        if kind == "contract":
            _merge_contract(contracts, fact, ev_pages)
        elif kind == "certificate":
            observations_of_queries.extend(_merge_certificate_observations(certs, fact))
        elif kind == "person":
            _merge_person(persons, fact, ev_pages)
    for name, families, numbers, document_id in observations_of_queries:
        numbered = {normalize_cert(cert.name) for cert in certs
                    if cert.backing is True and _cert_number_key(cert.certificate_number) in {_cert_number_key(n) for n in numbers}
                    and cert.document_id == document_id}
        # An explicit certificate-number match carries identity across pages.
        # A conflicting known number must never fall back to a broad family match.
        known_numbers = {_cert_number_key(cert.certificate_number) for cert in certs if normalize_cert(cert.name) == name
                         and cert.backing is True and cert.certificate_number}
        same_document = document_id is None or all(
            cert.document_id == document_id for cert in certs
            if normalize_cert(cert.name) == name and cert.backing is True
        )
        number_compatible = not numbers or not known_numbers or not (
            {_cert_number_key(n) for n in numbers}.isdisjoint(known_numbers)
        )
        if len(numbered) == 1 and (not families or numbered <= families):
            query_families.update(numbered)
        elif name in families and len(families) == 1 and same_document and number_compatible:
            query_families.add(name)
    cert_section_present = cert_section_present or bool(query_families)
    return replace(inventory, contracts=contracts, certs=certs, persons=persons,
                   cert_section_present=cert_section_present, platform_query_certificates=query_families)
