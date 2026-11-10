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
                "c55": "2.00", "c57": "3.00", "c61": "2.00", "c62": "0.50"}
    for code, criterion in criteria.items():
        assert build_objective_plan(criterion) is not None, code
        result = score_objective(criterion, inventory)
        assert result is not None, code
        assert str(result.score) == expected[code], (code, result.detail)
        assert result.verdict == "pass"


def test_objective_fastpath_reports_zero_with_reason_when_absent():
    inventory = build_inventory(7, [{"page_no": 1, "source": "text", "text": "无关材料"}])
    criterion = _criterion("c57", "增值服务", "count", "3",
                           "提供增值服务；每条得1 分，最高得3 分")
    result = score_objective(criterion, inventory)
    assert result is not None
    assert result.score == Decimal("0.00")
    assert result.zero_reason in ("no_facts", "confirmed_absent")
