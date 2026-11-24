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

from dataclasses import replace
from decimal import Decimal, InvalidOperation

from app.engine.material_inventory import (
    CertRecord, ContractRecord, MaterialInventory, PersonRecord,
)


def _norm_name(name: str | None) -> str:
    return "".join(str(name or "").split()).lower()


def _amount_val(value) -> Decimal | None:
    try:
        if value in (None, ""):
            return None
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _same_case(rec: ContractRecord, name: str, amount: Decimal | None) -> bool:
    if amount is None or _amount_val(rec.amount_yuan) != amount:
        return False
    if not _norm_name(rec.name) or not _norm_name(name):
        return False
    if _norm_name(rec.name) == _norm_name(name):
        return True
    try:
        from app.engine.material_inventory import _contract_name_overlap

        return bool(_contract_name_overlap(rec.name, name))
    except Exception:  # noqa: BLE001
        return False


def _merge_contract(contracts: list[ContractRecord], fact: dict,
                    ev_pages: list[int]) -> None:
    name = str(fact.get("name") or "").strip()
    amount = _amount_val(fact.get("amount_yuan"))
    if not name or amount is None:
        return  # sanitize 已拦；防御
    state_map = (("has_contract", "contract"), ("has_invoice", "invoice"),
                 ("has_acceptance", "acceptance"), ("has_seal", "seal"))
    target = None
    for rec in contracts:
        if _same_case(rec, name, amount):
            target = rec
            break
    if target is None:
        state = {key: (True if fact.get(src) is True else None)
                 for src, key in state_map}
        materials = {}
        for key in ("contract", "invoice", "acceptance"):
            if state.get(key) is True and ev_pages:
                materials[key] = list(ev_pages)
        contracts.append(ContractRecord(
            name=name, amount_yuan=amount,
            signed_date=str(fact.get("sign_date") or "").strip() or None,
            pages=list(ev_pages), materials=materials, materials_state=state,
            project_type=str(fact.get("project_type") or "").strip() or None,
            source="vl",
        ))
        return
    # 并集：金额/日期文本非空优先、VL 补空；pages/materials 取并；state 只升不降
    if target.amount_yuan is None:
        target.amount_yuan = amount
    if not target.signed_date and fact.get("sign_date"):
        target.signed_date = str(fact["sign_date"]).strip()
    if not target.project_type and fact.get("project_type"):
        target.project_type = str(fact["project_type"]).strip()
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


def _merge_cert(certs: list[CertRecord], fact: dict, ev_pages: list[int]) -> None:
    name = str(fact.get("name") or "").strip()
    if not name:
        return
    from app.engine.objective import normalize_cert

    norm = normalize_cert(name)
    target = next((c for c in certs if (c.normalized or "") == norm
                   or normalize_cert(c.name) == norm), None)
    if target is None:
        certs.append(CertRecord(
            name=name, normalized=norm,
            level=str(fact.get("cert_level") or "").strip() or None,
            valid_until=str(fact.get("valid_until") or "").strip() or None,
            pages=list(ev_pages),
            backing=True if ev_pages else None,
        ))
        return
    if not target.level and fact.get("cert_level"):
        target.level = str(fact["cert_level"]).strip()
    if not target.valid_until and fact.get("valid_until"):
        target.valid_until = str(fact["valid_until"]).strip()
    for p in ev_pages:
        if p not in target.pages:
            target.pages.append(p)
    if target.backing is not True and ev_pages:
        target.backing = True


def _merge_person(persons: list[PersonRecord], fact: dict, ev_pages: list[int]) -> None:
    name = str(fact.get("name") or "").strip()
    if not name:
        return

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
            name=name, title=str(fact.get("person_title") or "").strip(),
            years=_years(fact.get("person_years")),
            experience_pages=list(ev_pages), pages=list(ev_pages),
        ))
        return
    if not target.title and fact.get("person_title"):
        target.title = str(fact["person_title"]).strip()
    if target.years is None:
        target.years = _years(fact.get("person_years"))
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
    ) for r in inventory.contracts]
    certs = [replace(c, pages=list(c.pages or [])) for c in inventory.certs]
    persons = [replace(
        p, pages=list(p.pages or []), certs=list(p.certs or []),
        experience_pages=list(p.experience_pages or []),
    ) for p in inventory.persons]
    cert_section_present = inventory.cert_section_present
    for fact in vl_facts or []:
        ev_pages = sorted({int(e["page_no"]) for e in (fact.get("evidence") or [])
                           if e.get("page_no") is not None})
        kind = str(fact.get("kind") or "")
        if kind == "contract":
            _merge_contract(contracts, fact, ev_pages)
        elif kind == "certificate":
            _merge_cert(certs, fact, ev_pages)
        elif kind == "person":
            _merge_person(persons, fact, ev_pages)
        if fact.get("has_query_screenshot") is True:
            cert_section_present = True  # 只升不降：图上确认有查询截图
    return replace(inventory, contracts=contracts, certs=certs, persons=persons,
                   cert_section_present=cert_section_present)
