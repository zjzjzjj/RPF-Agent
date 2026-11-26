from decimal import Decimal
import json
from pathlib import Path

from app.engine.facts import BidFacts
from app.engine.material_inventory import (
    ContractRecord, MaterialInventory, PersonRecord, _attach_person_case_proofs,
    _person_case_records_from_pages, build_inventory,
)
from app.engine.vl_rescore import rescore_from_merged_facts

FIXTURE = Path(__file__).parent / "fixtures" / "test6_manual_scores.json"


def _rule(code, name, maximum, description, params):
    return {"code": code, "name": name, "score_type": "count", "max_score": str(maximum),
            "description": description, "required_evidence": description, "params": params}


C48 = _rule(
    "c48", "项目团队—项目负责人业绩", 4,
    "项目负责人具有五年及以上工作经验且作为项目负责人具有类似系统运维项目的实施/服务经验的，每提供一个案例得2分，最高得4分。"
    "【证明材料】须提供业绩证明文件复印件并加盖投标人公章，项目负责人须提供工作经验证明；业绩证明反映姓名，否则提供业主证明；提供近六个月社保。",
    {"field": "performance", "person_scope": "leader", "per_item": "2", "max_items": 2},
)
C49 = _rule(
    "c49", "项目团队—驻场运维工程师业绩", 6,
    "两名项目驻场运维工程师（若项目负责人为驻场运维工程师之一，不重复计分），每位工程师每个案例得1.5分，每人最高3分，本项最高6分。"
    "【证明材料】须提供业绩证明文件复印件并加盖投标人公章，反映姓名，否则业主证明，提供近六个月社保。",
    {"field": "performance", "person_scope": "member", "per_item": "1.5", "max_items": 4},
)


def _inventory(records, persons, pages):
    return MaterialInventory(bidder_id=1, pages=pages, full_text="", facts=BidFacts(text=""),
                             contracts=records, persons=persons, personnel_table_found=True,
                             personnel_detail_found=True)


def test_owner_proof_only_cannot_score_leader_case():
    pages = [
        {"document_id": 1, "page_no": 1, "text": "项目负责人情况表 姓名 王伟 业绩及承担的主要工作情况"},
        {"document_id": 1, "page_no": 2, "text": "业主证明 某运维项目 王伟 投标人：某公司（盖单位公章）"},
    ]
    record = ContractRecord(name="某运维项目运维服务", amount_yuan=Decimal("800000"),
                            pages=[1, 2], materials_state={"seal": True}, source="text")
    person = PersonRecord(name="王伟", role="项目负责人", social_security=True)
    inventory = _inventory([record], [person], pages)
    _attach_person_case_proofs(inventory.contracts, inventory.persons, pages)
    result = rescore_from_merged_facts(C48, inventory)
    assert result is not None and result.score == 0


def test_person_case_matrix_binds_real_case_pages_without_leader_duplication():
    pages = [
        {"document_id": 1, "page_no": 82, "text": "项目负责人情况表 姓名 郑得龙 业绩及承担 2025年宁波卷烟厂智慧动力管控一体化集成系统维保服务"},
        {"document_id": 1, "page_no": 84, "text": "2-10-1 郑得龙"},
        {"document_id": 1, "page_no": 86, "text": "2-10-1-1 项目经理业绩 中标通知书 2025年宁波卷烟厂智慧动力管控一体化集成系统维保服务 郑得龙 投标人：某公司（盖单位公章）"},
        {"document_id": 1, "page_no": 91, "text": "2-10-3 张涛波"},
        {"document_id": 1, "page_no": 93, "text": "2-10-3-1 实施经验 四川中烟工业有限责任公司2025-2027年物流综管平台项目系统运维技术服务 合同复印件 技术服务费2045800 张涛波 投标人：某公司（盖单位公章）"},
        {"document_id": 1, "page_no": 94, "text": "2-10-4 仇萍"},
        {"document_id": 1, "page_no": 96, "text": "2-10-4-1 实施经验 2024-2025年度辅助决策平台及仪器数据采集系统运维服务 合同复印件 仇萍 投标人：某公司（盖单位公章）"},
    ]
    records = [
        ContractRecord(name="2025年宁波卷烟厂智慧动力管控一体化集成系统维保服务", amount_yuan=Decimal("398560"), source="table"),
        ContractRecord(name="2024-2025年度辅助决策平台及仪器数据采集系统运维服务", amount_yuan=Decimal("1003466"), source="table"),
        ContractRecord(name="四川中烟工业有限责任公司2025-2027年物流综管平台项目系统运维技术服务", amount_yuan=Decimal("2045800"), source="table"),
    ]
    persons = [PersonRecord("郑得龙", role="项目负责人", social_security=True),
               PersonRecord("张涛波", role="驻场运维工程师", social_security=True),
               PersonRecord("仇萍", role="驻场运维工程师", social_security=True)]
    inventory = _inventory(records, persons, pages)
    _attach_person_case_proofs(inventory.contracts, inventory.persons, pages)
    assert rescore_from_merged_facts(C48, inventory).score == Decimal("2")
    assert rescore_from_merged_facts(C49, inventory).score == Decimal("3")


def test_person_declaration_without_project_title_never_creates_case():
    pages = [{"document_id": 1, "page_no": 1,
              "text": "项目团队人员情况表 张三 驻场运维工程师 2个类似项目实施经验"}]
    records = _person_case_records_from_pages(pages, [PersonRecord("张三", role="驻场运维工程师")])
    assert records == []


def test_expected_person_targets_match_manual_fixture():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))["expected_objective"]
    assert expected["c48"] == ["0", "4", "0", "2", "4"]
    assert expected["c49"] == ["0", "6", "0", "3", "6"]

