# -*- coding: utf-8 -*-
"""facts→score_count 重算适配器契约测试（2026-11-25 翻案通道收口）。

红线锁定：owner_proof/业主证明绝不映射 work_proof；cert_section_present 三态
直传（盲态 None 不得压成 False 清零）；type_eligible 恒 bool 不 raise；
确认缺判 0 不放宽（zero_reason 透传）；无法构造 → None（安全态）。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.facts import BidFacts
from app.engine.material_inventory import (
    CertRecord, ContractRecord, MaterialInventory, PersonRecord,
)
from app.engine.vl_rescore import rescore_from_merged_facts


def _inv(contracts=None, certs=None, persons=None, audit=None,
         cert_backing=None, cert_present=False, text=""):
    return MaterialInventory(
        bidder_id=1, pages=[], full_text=text, facts=BidFacts(text=text),
        contracts=contracts or [], certs=certs or [], persons=persons or [],
        cert_section_present=cert_present, cert_backing=cert_backing or {},
        audit=audit or {}, performance_section_found=True,
    )


CRIT_CERT = {
    "score_type": "count", "name": "投标人认证及资质情况—管理体系认证",
    "max_score": "3",
    "params": {"field": "certificates", "per_item": "1", "max_items": "3"},
    "description": "每提供一项有效期内的认证证书得1分，最高3分。",
    "required_artifacts": "证书复印件及查询结果截图",
}

CRIT_CMMI = {
    "score_type": "count", "name": "投标人认证及资质情况—CMMI/CSMM 认证",
    "max_score": "2",
    "params": {"field": "cmmi", "per_item": "2", "max_items": "1"},
    "description": "具有有效期内 CMMI 认证二级及以上得 2 分。",
}

CRIT_PERSON = {
    "code": "c48", "score_type": "count", "name": "项目团队—项目负责人业绩",
    "max_score": "4",
    "description": ("项目负责人具有五年及以上工作经验，且作为项目负责人具有类似系统"
                    "运维项目实施/服务经验，每案例2分，最高4分。【证明材料】须提供"
                    "业绩证明材料、工作经验证明及社保证明，未提供的不得分。"),
    "params": {"person_scope": "leader", "field": "performance", "per_item": "2",
               "max_items": "2"},
}


def _cert(name, backing=True, pages=None):
    from app.engine.objective import normalize_cert

    return CertRecord(name=name, normalized=normalize_cert(name),
                      pages=list(pages or [10]), backing=backing)


# ---- 证书格（c44/c45）-------------------------------------------------------

def test_rescore_cert_counts_with_backing() -> None:
    inv = _inv(certs=[_cert("ISO9001"), _cert("ISO27001"), _cert("ISO20000")],
               audit={"cert_section_state": None})
    res = rescore_from_merged_facts(CRIT_CERT, inv)
    assert res is not None
    assert res.score == Decimal("3.00"), "三本证书复印件（backing=True）照计"
    assert res.zero_reason is None


def test_rescore_cert_blind_section_counts_with_concern() -> None:
    """盲态（截图节不可读 None）→ 照计+疑问，不得压成 False 清零（run155 教训）。"""
    inv = _inv(certs=[_cert("ISO9001")], audit={"cert_section_state": None})
    res = rescore_from_merged_facts(CRIT_CERT, inv)
    assert res.score == Decimal("1.00")
    assert "截图节不可读" in (res.detail or ""), res.detail


def test_rescore_cert_confirmed_no_screenshot_clears_iso() -> None:
    """红线：截图节确认无（False）→ ISO 清零（细则'须提供查询结果截图'）。"""
    inv = _inv(certs=[_cert("ISO9001")], audit={"cert_section_state": False})
    res = rescore_from_merged_facts(CRIT_CERT, inv)
    assert res.score == 0
    assert res.complete
    assert res.facts["score_verified_only"] == "0.00"


def test_rescore_cert_screenshot_true_upgrades() -> None:
    """VL has_query_screenshot=true 升级 cert_section_present=True → 计入无疑。"""
    inv = _inv(certs=[_cert("ISO9001")], audit={"cert_section_state": None},
               cert_present=True)
    res = rescore_from_merged_facts(CRIT_CERT, inv)
    assert res.score == Decimal("1.00")
    assert "截图节不可读" not in (res.detail or "")


def test_rescore_cmmi_from_record_backing() -> None:
    inv = _inv(certs=[_cert("CMMI 三级")], audit={"cert_section_state": None})
    res = rescore_from_merged_facts(CRIT_CMMI, inv)
    assert res is not None
    assert res.score == Decimal("2.00")


def test_rescore_cmmi_binds_visual_maturity_certificate_to_its_published_record() -> None:
    proof = CertRecord(name="CMMI", normalized="CMMI", level="3", backing=True, pages=[67],
                       document_id=1, appraisal_id="64122")
    lookup = CertRecord(name="CMMI", normalized="CMMI", valid_from="2023-03-24", valid_until="2026-03-24",
                        backing=False, pages=[69], document_id=1, appraisal_id="64122", appraisal_is_lookup=True)
    rule = {**CRIT_CMMI, "params": {
        **CRIT_CMMI["params"], "cert_min_level": 2, "cert_require_current": True,
        "cert_reference_date": "2025-11-19",
    }}

    result = rescore_from_merged_facts(rule, _inv(certs=[proof, lookup]))

    assert result is not None and result.complete
    assert result.score == Decimal("2.00")
    assert result.facts["certificate_details"][0]["appraisal_lookup_pages"] == [69]


def test_rescore_cmmi_accepts_source_maturity_level_string() -> None:
    from app.engine.vl_facts import merge_vl_facts
    from app.engine.vl_fallback import VlFact, _sanitize_facts

    rule = {**CRIT_CMMI, "params": {
        **CRIT_CMMI["params"], "cert_min_level": 2, "cert_require_current": True,
        "cert_reference_date": "2025-11-19",
    }}
    source = {"document_id": 70, "page_no": 67}
    fact = VlFact(
        kind="certificate", name="CMMI Maturity Level 3", cert_level="CMMI Maturity Level 3",
        has_cert_proof=True, has_seal=True, valid_until="2026-03-24",
        evidence=[{**source, "observation": "证书复印件，Appraisal Expiration Date 24th Mar 2026，盖红色公章"}],
    )
    sanitized, dropped = _sanitize_facts([fact], [source])

    assert not dropped
    result = rescore_from_merged_facts(rule, merge_vl_facts(_inv(), sanitized))

    assert result is not None and result.complete
    assert result.score == Decimal("2.00")
    assert result.facts["certificate_details"][0]["page_no"] == 67


# ---- person_scope 业绩格（c48/c49）------------------------------------------

def _person_case_inv(**state):
    rec = ContractRecord(
        name="某平台运维服务项目", amount_yuan=Decimal("700000"),
        signed_date="2024-03-01", pages=[1, 2],
        materials={"contract": [1]}, materials_state={"contract": True},
        person_names=["杨祥海"],
        person_states={"work_proof": True, "experience_proof": True,
                       "social_security": True},
        project_type="运维服务", source="vl")
    for k, v in (state or {}).items():
        if k == "person_states":
            rec.person_states.update(v)
        else:
            setattr(rec, k, v)
    persons = [PersonRecord(name="杨祥海", role="项目负责人", pages=[9])]
    return _inv(contracts=[rec], persons=persons)


def test_rescore_person_cases_scores_with_full_chain() -> None:
    res = rescore_from_merged_facts(CRIT_PERSON, _person_case_inv())
    assert res is not None
    assert res.score == Decimal("2.00"), f"全链案例应计 2 分：{res.detail}"
    assert res.zero_reason is None


def test_rescore_person_missing_work_proof_zero() -> None:
    """红线：业绩证明缺（含仅业主证明）→ 案例不计，绝不放水。"""
    inv = _person_case_inv(person_states={"work_proof": None,
                                          "experience_proof": None,
                                          "social_security": None})
    inv.contracts[0].materials["owner_proof"] = [5]
    res = rescore_from_merged_facts(CRIT_PERSON, inv)
    assert res is not None
    assert res.score == 0, f"work_proof 无正向证据不得计分：{res.detail}"


def test_rescore_person_relaxed_partial_chain() -> None:
    """放宽口径（2026-11-25 用户拍板）：业绩证明可见+归属 → 经验证明/社保
    以线索计入并标疑问。"""
    inv = _person_case_inv(person_states={"work_proof": True,
                                          "experience_proof": None,
                                          "social_security": None})
    res = rescore_from_merged_facts(CRIT_PERSON, inv)
    assert res is not None
    assert res.score == Decimal("2.00"), res.detail
    assert "放宽口径" in (res.detail or "")


def test_rescore_person_confirmed_missing_still_blocks() -> None:
    """红线：可读确认缺（False）不放宽——确认缺判 0 不放宽。"""
    inv = _person_case_inv(person_states={"work_proof": True,
                                          "experience_proof": True,
                                          "social_security": False})
    res = rescore_from_merged_facts(CRIT_PERSON, inv)
    assert res is not None
    assert res.score == 0, f"确认缺（False）必须阻计：{res.detail}"


def test_rescore_onsite_member_proof_is_bound_to_each_person_and_capped():
    criterion = {
        "score_type": "count", "name": "驻场工程师业绩", "max_score": "6",
        "description": "驻场工程师每个案例得1.5分，每人最高3分。",
        "required_evidence": "合同中体现人员，否则须提供业主证明，并提供近六个月社会保险缴纳证明。",
        "params": {"field": "performance", "person_scope": "member", "per_item": "1.5", "max_items": 4},
    }
    people = [PersonRecord("张三", role="项目负责人", social_months=12),
              PersonRecord("李四", role="驻场运维工程师", social_months=12),
              PersonRecord("王五", role="驻场运维工程师", social_months=5)]
    proofs = {name: {"owner_proof": True} for name in ("张三", "李四", "王五")}
    records = [ContractRecord(f"某平台独立运维服务项目{i}", Decimal("700000"),
                              materials_state={"contract": True}, person_names=list(proofs), person_proofs=proofs)
               for i in range(3)]
    inventory = _inv(contracts=records + [records[0]], persons=people)
    assert rescore_from_merged_facts(criterion, inventory).score == Decimal("3")
    people[2].social_months = 12
    assert rescore_from_merged_facts(criterion, inventory).score == Decimal("6")
    stamped_rule = {**criterion, "required_evidence": criterion["required_evidence"] + "业绩证明复印件加盖公章。"}
    assert rescore_from_merged_facts(stamped_rule, inventory).score == 0
    for record in records:
        record.materials_state["seal"] = True
    assert rescore_from_merged_facts(stamped_rule, inventory).score == Decimal("6")


def test_social_security_coverage_uses_named_rows_and_respects_latest_gap():
    from app.engine.material_inventory import _overlay_social_security
    from app.engine.vl_facts import merge_vl_facts

    member = PersonRecord("张三", role="驻场运维工程师")
    other = PersonRecord("李四", role="驻场运维工程师")
    reported = {"document_id": 1, "page_no": 1, "text": (
        "社会保险参保证明（单位专用）\n2024年12月 2025年11月，该单位参保人员信息如下\n"
        "1\n张三\n110101198112120001\n202412 202509\n10\n"
        "2\n张三\n110101198112120001\n202511 202511\n1\n"
        "3\n李四\n110101198112120002\n202506 202511\n6\n")}
    _overlay_social_security([member, other], [reported])
    assert member.social_months == 1
    assert other.social_months == 6
    inventory = _inv(persons=[member, other])
    visual = {"kind": "person", "name": "张三", "social_security_months": 12,
              "evidence": [{"document_id": 1, "page_no": 1, "observation": "张三出现在社保清单"}]}
    assert merge_vl_facts(inventory, [visual]).persons[0].social_months == 1


def test_leader_five_paid_months_do_not_meet_six_month_proof_requirement():
    from app.engine.material_inventory import _overlay_social_security

    inventory = _person_case_inv()
    _overlay_social_security(inventory.persons, [{"text": (
        "社会保险参保证明（单位专用） 2025年05月 - 2025年10月，该单位参保人员信息如下\n"
        "1\n杨祥海\n110101198112120001\n202505 - 202509\n5\n")}])
    criterion = {**CRIT_PERSON, "required_evidence": (
        "须提供业绩证明材料、具有五年及以上工作经验证明及近六个月社保缴纳证明。")}
    assert inventory.persons[0].social_months == 5
    assert rescore_from_merged_facts(criterion, inventory).score == 0


def test_cmmi_field_inference_beats_generic_cert_kw() -> None:
    """CMMI/CSMM 是具体证书名（cmmi 专属分支），不得落到 certificates
    （否则任何证书都给分——2026-11-25 c45 实证的放水面）。"""
    from app.engine.objective import _infer_count_field
    from app.rules.check_infer import classify_scoring_form

    crit = {"name": "投标人认证及资质情况—CMMI/CSMM 认证",
            "description": "具有有效期内CMMI认证二级或CSMM认证二级及以上得2分。"}
    assert _infer_count_field(crit) == "cmmi"
    form = classify_scoring_form(crit["name"] + "\n" + crit["description"])
    assert form is not None and form[1].get("field") == "cmmi", form


def test_rescore_person_type_eligible_never_none() -> None:
    """type_eligible 恒 bool（None→True 保留+疑问）——managed raise 不触发。"""
    inv = _person_case_inv(project_type=None)  # 名称含"运维服务"→True
    res = rescore_from_merged_facts(CRIT_PERSON, inv)
    assert res is not None and res.score == Decimal("2.00")
    # 明确非运维 → 剔除
    inv2 = _person_case_inv(project_type=None)
    inv2.contracts[0].name = "某网络系统建设项目"
    res2 = rescore_from_merged_facts(CRIT_PERSON, inv2)
    assert res2 is not None and res2.score == 0


def test_rescore_person_amount_tier_prefilters_no_raise() -> None:
    """managed 金额档：无金额案例预滤（防 raise），有金额照档计。"""
    crit = dict(CRIT_PERSON)
    crit["params"] = dict(CRIT_PERSON["params"], min_amount_yuan="500000",
                          tiers=[{"min_amount_yuan": "500000", "per_item": "2"}])
    inv = _person_case_inv()
    rec2 = ContractRecord(name="某运维项目B", pages=[3],
                          person_names=["杨祥海"],
                          person_states={"work_proof": True, "experience_proof": True,
                                         "social_security": True},
                          materials_state={"contract": True}, source="vl")
    inv.contracts.append(rec2)  # 无金额 → 预滤
    res = rescore_from_merged_facts(crit, inv)
    assert res is not None
    assert res.score > 0, res.detail
    assert "无可核验金额" in (res.detail or "")


def test_rescore_uncovered_shape_returns_none() -> None:
    crit = {"score_type": "count", "name": "承诺响应时间", "max_score": "4",
            "params": {"engine": "value_tier", "field": "response_hours"}}
    assert rescore_from_merged_facts(crit, _inv()) is None
    # 人员级证书格不硬算
    crit2 = {"score_type": "count", "name": "人员证书", "max_score": "2",
             "params": {"person_scope": "member", "field": "certificates"}}
    assert rescore_from_merged_facts(crit2, _inv()) is None


def test_rescore_mixed_tiers_degrades_without_managed() -> None:
    """档位混杂 raise → 降级非 managed 重试（不带 verified_facts）。"""
    crit = dict(CRIT_PERSON)
    crit["params"] = dict(CRIT_PERSON["params"],
                          tiers=[{"min_count": "2", "score": "1"},
                                 {"min_amount_yuan": "500000", "per_item": "1"}])
    res = rescore_from_merged_facts(crit, _person_case_inv())
    assert res is not None, "降级重试仍应给出结论"
