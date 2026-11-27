# -*- coding: utf-8 -*-
"""Deterministic objective scoring from page-bound materials."""
from decimal import Decimal

from app.engine.material_inventory import build_inventory
from app.engine.objective_fastpath import build_objective_plan, score_objective


def _pages():
    pages = []
    for index, (year, page_no) in enumerate(((2022, 47), (2023, 52), (2024, 57)), 1):
        pages.append({
            "document_id": 1, "page_no": page_no, "source": "text",
            "text": (
                f"{index}、{year} 年某研究所维保合同\n（1）合同\n"
                "某会议系统、展厅及大屏维保合同\n"
                "委托方(甲方)：某研究所\n维保方(乙方)：某公司\n"
                f"合同执行期限：{year}年6月1日至{year + 1}年5月31日。\n"
                "合同价款：人民币57万元。\n"
            ),
        })
        pages.append({"document_id": 1, "page_no": page_no + 1, "source": "text",
                      "text": "（2）验收单\n"})
    pages.append({
        "document_id": 1, "page_no": 64, "source": "text",
        "text": (
            "项目团队人员情况表\n姓名 性别 年龄 学历 上岗证书 职称 本项目中的职责 项目经历\n"
            "1 张三 男 54 大专（P68） 二级建造师（P70） 工程师（P69） 总负责人 百余场 P71介绍\n"
            "2 李四 男 59 研究生（P72） 高级工程师（P72） 高级工程师（P72） 项目顾问 百余场 P73介绍\n"
            "3 王五 男 44 大专（P74） 高级音响调音员（P75） 高级电气工程师（P77） 维保负责人 百余场 P78介绍\n"
            "4 赵六 男 41 专科（P80） 工程师（P81） 工程师（P81） 维保驻场 百余场 P82介绍\n"
            "5 孙八 男 28 大专（P87） 助理工程师（P88） 电工证（P89） 维保驻场 几十场 P91介绍\n"
            "6 吴十 男 49 本科（P95） 高处作业（P97） 工程师（P96） 维保驻场 百余场 P98介绍\n"
            "7 徐二 男 44 大专 投影机维修工程师（P105） 投影机维修工程师（P105） 视频设备维修 百余场 P106介绍\n"
        ),
    })
    pages.append({
        "document_id": 1, "page_no": 170, "source": "text",
        "text": "2.11.4 增值服务\n1、系统优化调整配合\n2、音响学会专家固定上门服务\n3、咨询非维保设备服务\n",
    })
    pages.extend([
        {"document_id": 1, "page_no": 38, "source": "text",
         "text": "质量管理体系认证证书\n质量管理体系符合GB/T19001-2016/ISO9001:2015标准\n"},
        {"document_id": 1, "page_no": 39, "source": "text",
         "text": "环境管理体系认证证书\n环境管理体系符合GB/T24001-2016/ISO14001:2015标准\n"},
        {"document_id": 1, "page_no": 40, "source": "text",
         "text": "职业健康安全管理体系认证证书\n职业健康安全管理体系符合GB/T45001-2020/ISO45001:2018标准\n"},
        {"document_id": 1, "page_no": 41, "source": "text",
         "text": "附 全国认证认可信息公共服务平台查询截图\n"},
        {"document_id": 1, "page_no": 42, "source": "text",
         "text": "资质类别及等级：\n电子与智能化工程专业承包贰级\n"},
    ])
    return pages


def _criterion(code, name, score_type, max_score, description):
    return {"code": code, "name": name, "score_type": score_type,
            "max_score": max_score, "description": description, "params": {}}


def test_material_inventory_reads_contracts_personnel_certificates_and_services():
    inventory = build_inventory(7, _pages())
    assert len(inventory.contracts) == 3
    assert {str(c.amount_yuan) for c in inventory.contracts} == {"570000"}
    assert len(inventory.persons) == 7
    assert len(inventory.value_items) == 3
    assert {c.normalized for c in inventory.certs} >= {"ISO9001", "ISO14001", "ISO45001"}
    assert any(c.level == "二级" for c in inventory.certs)


def _person_inventory(monkeypatch):
    from app.engine import material_inventory as mi
    from app.engine.material_inventory import PersonRecord

    monkeypatch.setattr(mi, "_persons_from_tables", lambda pages, path: [
        PersonRecord(name="张三", role="驻场运维工程师", certs=["ITIL Foundation 证书"]),
        PersonRecord(name="李四", role="项目负责人", certs=["PMP 证书"]),
    ])
    pages = [
        {"document_id": 1, "page_no": 3, "source": "ocr_img",
         "text": "This is to certify that Zhang San has achieved the ITIL Foundation Certificate, number 123."},
        {"document_id": 1, "page_no": 5, "source": "ocr_img",
         "text": "This is to certify that Li Si has achieved the PMP Certificate, number 456."},
    ]
    return mi.build_inventory(7, pages)


def test_person_scope_certificates_plan_counts_team_members(monkeypatch):
    inventory = _person_inventory(monkeypatch)
    criterion = {
        "code": "c27", "name": "拟派运维工程师ITIL Foundation认证",
        "score_type": "count", "max_score": "2",
        "description": "拟派运维工程师具有ITIL Foundation 认证得2 分。【证明材料】提供证书复印件，原件备查。",
        "params": {"field": "certificates", "per_item": "2", "min_count": "1",
                   "person_scope": "member", "max_items": "1", "_objective_form": "existence"},
    }
    assert build_objective_plan(criterion).kind == "person_certs"
    result = score_objective(criterion, inventory)
    assert result.score == Decimal("2")
    assert "张三" in result.detail


def test_person_scope_certificates_plan_counts_leader_targets(monkeypatch):
    inventory = _person_inventory(monkeypatch)
    criterion = {
        "code": "c28", "name": "项目经理（项目负责人）认证",
        "score_type": "count", "max_score": "2",
        "description": "项目经理（项目负责人）具有PMP 证书得1 分；"
                       "具有系统集成项目管理工程师认证得1 分，最高得2 分。",
        "params": {"field": "certificates", "per_item": "1", "max_items": "2",
                   "person_scope": "leader", "_objective_form": "existence"},
    }
    result = score_objective(criterion, inventory)
    assert result.score == Decimal("1")
    assert "PMP" in result.detail


def test_person_scope_performance_plan_defers_to_llm():
    """人员级业绩须逐案例核验人员归属，不得退回公司级合同桶（run128 c29）。"""
    criterion = _criterion(
        "c29", "服务团队技术力量", "count", "2",
        "拟派运维工程师具有IT运维管理系统维护项目服务经验得1分。"
        "【证明材料】业绩证明以提供合同的复印件为准。")
    criterion["params"] = {"field": "performance", "per_item": "1",
                           "person_scope": "member"}
    assert build_objective_plan(criterion) is None


def test_objective_fastpath_scores_all_supported_plans():
    inventory = build_inventory(7, _pages())
    criteria = {
        "c39": _criterion(
            "c39", "投标人业绩实力", "count", "6",
            "合同金额50 万及以上的业绩的，每个得2 分；年维护合同金额超100 万，每个得3 分；"
            "最高不超过6 分。【证明材料】：合同复印件及发票或验收材料。"),
        "c52": _criterion(
            "c52", "驻场人员会议及多媒体维护经验", "count", "3",
            "驻场人员具有3 年以上会议及多媒体维护经验的，每人得1 分，最高得3 分"),
        "c53": _criterion(
            "c53", "特种作业操作证", "count", "2",
            "服务团队成员具有特种作业操作证（电工作业和登高作业证），每人得1 分，最高不超2 分"),
        "c54": _criterion(
            "c54", "职称等级", "count", "2",
            "具备高级工程师或以上等级职称的每位得1 分，具有工程师中级职称的每位得0.5 分，最高得2 分"),
        "c55": _criterion(
            "c55", "行业认证维修工程师", "count", "3",
            "大屏、中控、投影机、调音台、音箱、功放、数字音频处理器、灯光系统中，"
            "具有其中一项设备的行业认证维修工程师得1 分，最高不超过3 分"),
        "c57": _criterion(
            "c57", "增值服务", "count", "3",
            "提供针对本项目切实可行并具有价值的其它增值服务；每条得1 分，最高得3 分"),
        "c61": _criterion(
            "c61", "投标人资质实力（1）ISO9001 质量管理、环境管理及职业健康安全管理体系认证、"
                    "信息技术服务认证两项得1 分，三项得2 分", "subjective", "2",
            "投标人具有 ISO9001 质量管理、环境管理及职业健康安全管理体系认证、"
            "信息技术服务认证两项得1 分，三项得2 分。"),
        "c62": _criterion(
            "c62", "投标人资质实力（2）电子与智能化工程专业承包", "subjective", "2",
            "有效期内的电子与智能化工程专业承包贰级得0.5 分，壹级得1 分；"
            "具有音视频集成工程能力证书贰级得0.5 分，壹级得1 分。"),
    }
    expected = {"c39": "6.00", "c52": "3.00", "c53": "2.00", "c54": "2.00",
                "c55": "3.00", "c57": "3.00", "c61": "1.00", "c62": "0.50"}
    for code, criterion in criteria.items():
        assert build_objective_plan(criterion) is not None, code
        result = score_objective(criterion, inventory)
        assert result is not None, code
        assert str(result.score) == expected[code], (code, result.detail)
        assert result.verdict == "pass"


def test_response_commitment_is_binary_rule_not_response_hour_tier():
    inventory = build_inventory(7, [{
        "document_id": 1, "page_no": 8, "source": "text",
        "text": "非招标人工作时间应急响应承诺：我方提供值班电话和远程支持方式，响应时间优于招标人要求响应时间一半。",
    }])
    criterion = _criterion(
        "c56", "应急响应时间承诺", "subjective", "2",
        "承诺在非招标人工作时间应急响应优于招标人要求响应时间一半的（要求提供可行的方式能够实现该项服务承诺的响应时间，且得到评标委员的认可）得2分，其它不得分。",
    )
    assert build_objective_plan(criterion) is not None
    result = score_objective(criterion, inventory)
    assert result is not None
    assert result.score == Decimal("2.00")
    assert result.verdict == "pass"


def test_objective_fastpath_reports_zero_with_reason_when_absent():
    inventory = build_inventory(7, [{"page_no": 1, "source": "text", "text": "无关材料"}])
    criterion = _criterion("c57", "增值服务", "count", "3",
                           "提供增值服务；每条得1 分，最高得3 分")
    result = score_objective(criterion, inventory)
    assert result is not None
    assert result.score == Decimal("0.00")
    assert result.zero_reason in ("no_facts", "confirmed_absent")


def test_documented_operations_scope_beats_generic_development_label() -> None:
    from app.engine.objective import documented_project_type_state

    rule = "类似信息系统运维项目实施/服务经验"
    assert documented_project_type_state(
        "软件开发", "数据工厂数据审计功能开发及数据工厂运维服务项目", rule,
    ) is True
    assert documented_project_type_state(
        "软件开发", "中通服创立公司关于承揽2024年AI能力运维系统开发业务外包采购项目", rule,
    ) is True
    assert documented_project_type_state(
        "软件开发", "某软件开发项目", rule,
    ) is False


def test_value_added_service_whitelist_excludes_ordinary_commitments() -> None:
    from app.engine.material_inventory import _value_item_category

    assert _value_item_category("驻场服务及驻场人员餐饮") is None
    assert _value_item_category("运维服 户需求书 服务团队 离") is None
    assert _value_item_category("电话支持及远程服务") is None
    assert _value_item_category("应急服务") is None
    assert _value_item_category("系统性能优化专项服务") is None
    assert _value_item_category("增值服务") is None
    assert _value_item_category("数据安全加固") == "security"
    assert _value_item_category("再增加1名远程支持技术人员") == "extra_staff"


# ==== P0-5（测试10 run113）：三件套材料链（细则"未提供的不得分"制裁句）====

from decimal import Decimal as _D  # noqa: E402

from app.engine.facts import BidFacts  # noqa: E402
from app.engine.material_inventory import ContractRecord, MaterialInventory  # noqa: E402

RULE10 = (
    "投标产品业绩：根据投标人提供自2022年1月1日（合同签订时间为准）至今的同类产品业绩，"
    "要求合同金额100万及以上，每个得1分，最高得2分。"
    "【证明材料】须提供业绩合同、验收证明材料及合同配套的至少一张发票复印件并加盖投标人公章，"
    "以上资料未提供的不得分。"
)


def _contract_inv(records):
    return MaterialInventory(
        bidder_id=1, pages=[], full_text="", facts=BidFacts(text=""),
        contracts=records, performance_section_found=True,
    )


def _perf_rule(description, **param_changes):
    params = {"field": "performance", "per_item": "1",
              "min_amount_yuan": "1000000", **param_changes}
    return {"code": "c25", "name": "投标产品业绩", "score_type": "count",
            "max_score": "2", "description": description, "params": params}


def test_hard_sanction_rejects_case_missing_acceptance_or_invoice():
    """细则三件套制裁句：**可读确认缺**验收/发票的案例不计分（表格行不豁免）。

    三态（2026-11-24）：materials_state=False 表示分类已跑且确认无材料；
    确认缺判 0 是红线，不放宽。
    """
    record = ContractRecord(name="某研究所维保", amount_yuan=_D("1200000"),
                            pages=[1], materials={"contract": [1]}, source="table",
                            materials_state={"contract": True, "invoice": False,
                                             "acceptance": False})
    result = score_objective(_perf_rule(RULE10), _contract_inv([record]))
    assert result is not None
    assert result.score == Decimal("0.00")
    assert "未提供的不得分" in result.detail
    assert "缺" in result.detail


def test_hard_sanction_unverified_materials_strict_drops():
    """三态：材料状态未知（分类未跑/未绑上）→ 严口径不计入（run158 放水修正）。

    run158 实证："未核实计入"=无据给分（c43 两家人工 0→AI 4）。案例级严口径：
    含 None 的案例剔除（verified-only 计分）+ zero_reason=material_unverified
    + 疑问标记（绝不静默 0），交 VL 翻案/人工复核。
    """
    record = ContractRecord(name="某研究所维保", amount_yuan=_D("1200000"),
                            pages=[1], materials={"contract": [1]}, source="table")
    result = score_objective(_perf_rule(RULE10), _contract_inv([record]))
    assert result is not None
    assert result.score == Decimal("0.00"), "未核实不得计入（run158 放水红线）"
    assert result.zero_reason == "material_unverified"
    assert result.verdict == "concern"
    assert any(w.get("code") == "material_unverified" for w in result.warnings)
    assert "严口径不计入" in result.detail
    assert (result.facts or {}).get("unverified_records"), "留 VL 翻案页源"


def test_hard_sanction_unverified_legacy_count_rollback(monkeypatch):  # noqa: ANN001
    """回滚通道：BID_EVAL_CASE_UNVERIFIED_COUNT=1 → 旧"未核实计入+疑问"语义。"""
    monkeypatch.setenv("BID_EVAL_CASE_UNVERIFIED_COUNT", "1")
    record = ContractRecord(name="某研究所维保", amount_yuan=_D("1200000"),
                            pages=[1], materials={"contract": [1]}, source="table")
    result = score_objective(_perf_rule(RULE10), _contract_inv([record]))
    assert result is not None
    assert result.score == Decimal("1.00"), "旧口径：未核实材料计入"
    assert result.verdict == "concern"
    assert any(w.get("code") == "material_unverified" for w in result.warnings)
    assert "未核实计入" in result.detail


def test_hard_sanction_partial_credit_keeps_verified_cases():
    """部分计分保留：全 True 案例照计，含 None 案例剔除（创联 c43 形态）。"""
    ok = ContractRecord(name="某研究所维保", amount_yuan=_D("1200000"), pages=[1, 2, 3],
                        materials={"contract": [1], "acceptance": [2], "invoice": [3]},
                        source="table")
    bad = ContractRecord(name="某局运维", amount_yuan=_D("1500000"), pages=[4],
                         materials={"contract": [4]}, source="table")
    result = score_objective(_perf_rule(RULE10), _contract_inv([ok, bad]))
    assert result is not None
    assert result.score == Decimal("1.00"), "verified 案例保留，unverified 案例剔除"
    assert result.zero_reason is None
    assert any(w.get("code") == "material_unverified" for w in result.warnings)
    assert [r["name"] for r in ((result.facts or {}).get("unverified_records") or [])] \
        == ["某局运维"]


def test_hard_sanction_counts_case_with_full_three_piece_chain():
    """三件套齐全（合同+验收+发票≥1）+签订时间在窗内的案例照计（三闸全过）。"""
    record = ContractRecord(
        name="某研究所维保", amount_yuan=_D("1200000"), signed_date="2024-01-25",
        pages=[1, 2, 3],
        materials={"contract": [1], "acceptance": [2], "invoice": [3]}, source="table")
    result = score_objective(_perf_rule(RULE10), _contract_inv([record]))
    assert result is not None
    assert result.score == Decimal("1.00")
    assert result.verdict == "pass"


def test_hard_sanction_accepts_text_contract_block_as_contract_copy():
    """正文合同块（source=text）本身就是提供的合同复印件；仍需验收+发票。"""
    record = ContractRecord(
        name="某研究所维保", amount_yuan=_D("1200000"), pages=[1, 2, 3],
        materials={"acceptance": [2], "invoice": [3]}, source="text")
    result = score_objective(_perf_rule(RULE10), _contract_inv([record]))
    assert result is not None
    assert result.score == Decimal("1.00")


def test_or_material_group_needs_only_one():
    """"发票或验收材料"+制裁句：OR 组任一即可（不误杀）。"""
    rule_text = ("同类产品业绩，合同金额100万及以上每个得1分，最高得2分。"
                 "【证明材料】须提供合同复印件及发票或验收材料，未提供的不得分。")
    record = ContractRecord(name="某维保", amount_yuan=_D("1200000"), pages=[1, 2],
                            materials={"contract": [1], "acceptance": [2]}, source="table")
    result = score_objective(_perf_rule(rule_text), _contract_inv([record]))
    assert result is not None
    assert result.score == Decimal("1.00")


def test_no_sanction_keeps_legacy_lenient_material_gate():
    """无制裁句的细则不设硬闸（不猜）：表格行主张照计（legacy 契约不动）。"""
    rule_text = ("合同金额50万及以上的业绩的，每个得2分，最高不超过6分。"
                 "【证明材料】：合同复印件及发票或验收材料。")
    record = ContractRecord(name="某维保", amount_yuan=_D("570000"),
                            pages=[1], materials={"contract": [1]}, source="table")
    result = score_objective(_perf_rule(rule_text, min_amount_yuan="500000",
                                        per_item="2"), _contract_inv([record]))
    assert result is not None
    assert result.score == Decimal("2.00")


# --- 测试2 run166 P0-4/P0-5/P1-4 回归锚（2026-11-27） --------------------------

def _persons_inv(persons):
    from app.engine.facts import BidFacts as _BF
    return MaterialInventory(
        bidder_id=1, pages=[], full_text="", facts=_BF(text=""),
        contracts=[], persons=persons, personnel_table_found=True)


def test_p04_person_experience_not_routed_to_company_contracts():
    """P0-4：c51「项目经理类似项目经验」命中人员主体词族 → 不得走公司合同金额桶。

    run166 c51 五家全 0 根因：'类似项目'命中 contracts 计划，无金额档→rate=0。
    """
    from app.engine.material_inventory import PersonRecord as _PR  # noqa: F401
    criterion = {
        "code": "c51", "name": "项目经理类似项目经验", "score_type": "subjective",
        "max_score": "3",
        "description": "项目经理具有五年及以上类似会议系统运维项目的实施/服务经验得3分；"
                       "四年及以上得2分，三年及以上得1分。",
    }
    plan = build_objective_plan(criterion)
    assert plan is None or plan.kind != "contracts", \
        "人员主体项不得走公司合同金额桶（c51 系统性 0 分根因）"


def test_p05_hard_sanction_accepts_no_recognition_clause():
    """P0-5：c39 制裁句'否则不予认可'（非'不得分'）→ 材料闸门激活，缺件表行判 0。

    run166 集服 c39 材料全缺仍计 6 分根因：旧正则只认'不得分'→AND 核验整段不执行。
    """
    rule_text = (
        "投标人业绩实力：承担类似会议、大屏系统维护合同金额50万及以上的业绩的，每个得2分，"
        "最高不超过6分。【证明材料】：投标人提供2022年1月1日至今项目业绩，以提供合同的复印件"
        "及合同内任意一张发票，业主证明或验收材料作为证明材料，否则不予认可。"
    )
    record = ContractRecord(name="某会议维保", amount_yuan=_D("600000"),
                            pages=[1], materials={"contract": [1]}, source="table",
                            materials_state={"contract": False, "invoice": False,
                                             "acceptance": False, "_case_block_complete": True})
    result = score_objective(
        _perf_rule(rule_text, min_amount_yuan="500000", per_item="2", max_score="6"),
        _contract_inv([record]))
    assert result is not None
    assert result.score == Decimal("0.00"), "'否则不予认可'制裁句须激活材料闸门"


def test_p05_table_row_not_exempt_when_materials_confirmed_missing():
    """P0-5：无制裁句但 materials_state 确认缺件（三件全 False+案例块完整）→ 表行不豁免。"""
    rule_text = ("合同金额50万及以上的业绩的，每个得2分，最高不超过6分。"
                 "【证明材料】：合同复印件及发票、验收材料。")
    record = ContractRecord(name="某维保", amount_yuan=_D("600000"),
                            pages=[1], materials={"contract": [1]}, source="table",
                            materials_state={"contract": False, "invoice": False,
                                             "acceptance": False, "_case_block_complete": True})
    result = score_objective(
        _perf_rule(rule_text, min_amount_yuan="500000", per_item="2", max_score="6"),
        _contract_inv([record]))
    assert result is not None
    assert result.score == Decimal("0.00"), "确认缺件的表行不得无条件豁免"


def test_p14_special_certs_requires_electric_and_climbing_pair():
    """P1-4：c53'电工作业和登高作业证，具备齐全的每人得1分'→ 只数两证齐全者。

    run166 c53 双向错根因之一：旧 any() 数'持任一证的人'（科通 3 人各持单证计 2）。
    """
    from app.engine.material_inventory import PersonRecord as _PR
    persons = [
        _PR(name="吴绍松", certs=["电工证"]),          # 仅电工 → 不计
        _PR(name="宋继勇", certs=["高处作业"]),        # 仅登高 → 不计
        _PR(name="杨育贵", certs=["电工证", "登高证"]),  # 两证齐全 → 计 1
    ]
    criterion = {
        "code": "c53", "name": "特种作业操作证", "score_type": "count",
        "max_score": "2",
        "description": "服务团队成员具有特种作业操作证（作业类别：电工作业和登高作业证），"
                       "具备齐全的每人得1分，最高不超2分。",
    }
    result = score_objective(criterion, _persons_inv(persons))
    assert result is not None
    assert result.score == Decimal("1.00"), "只数电工+登高两证齐全者"
    assert "杨育贵" in result.detail


def test_leader_case_count_plan_defers_to_llm():
    """P1（2026-11-28）c48 路由修复：负责人**案例计数**项（"每提供一个案例得
    2分"）不得进年限档引擎——空档表曾把案例计分锁成 confirmed_absent 0
    （run195 c48 海晟 人工 4→AI 0）；真年限档规则仍走 leader_experience。"""
    case_count_rule = {
        "code": "c48", "name": "项目团队—项目负责人业绩",
        "score_type": "count", "max_score": "4",
        "description": "项目负责人具有五年及以上工作经验且作为项目负责人具有类似系统运维项目的"
                       "实施/服务经验的，每提供一个案例得2分，最高得4分。"
                       "【证明材料】业绩证明复印件并加盖投标人公章。",
        "params": {"field": "performance", "person_scope": "leader",
                   "per_item": "2", "max_items": 2},
    }
    assert build_objective_plan(case_count_rule) is None
    year_tier_rule = {
        "code": "cX", "name": "项目负责人经验年限",
        "score_type": "count", "max_score": "4",
        "description": "项目负责人具有类似项目经验 5 年以上得 4 分。",
        "params": {"field": "performance", "person_scope": "leader",
                   "per_item": "2", "max_items": 2},
    }
    assert build_objective_plan(year_tier_rule).kind == "leader_experience"
