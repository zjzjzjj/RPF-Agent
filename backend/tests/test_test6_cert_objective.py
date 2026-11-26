from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.engine.material_inventory import build_inventory
from app.engine.vl_rescore import rescore_from_merged_facts


FIXTURE = Path(__file__).parent / "fixtures" / "test6_objective_page_cases.json"
MANUAL = Path(__file__).parent / "fixtures" / "test6_manual_scores.json"

C44 = {
    "code": "c44",
    "name": "投标人认证及资质情况—管理体系认证",
    "score_type": "count",
    "max_score": "3",
    "description": (
        "投标人具有有效期内的ISO9001质量管理体系认证、ISO27001信息安全管理体系认证、"
        "ISO20000信息技术服务管理体系认证证书的，每项得1分，最高3分。"
        "【证明材料】提供有效证书复印件并加盖公章，其中管理体系认证还需提供"
        "全国认证认可信息公共服务平台（cx.cnca.cn）查询结果截图。"
    ),
    "params": {"field": "certificates", "per_item": "1", "max_items": "3"},
}

C45 = {
    "code": "c45",
    "name": "投标人认证及资质情况—CMMI/CSMM认证",
    "score_type": "count",
    "max_score": "2",
    "description": "具有有效期内CMMI认证二级或CSMM认证二级及以上得2分。",
    "params": {
        "field": "cmmi", "per_item": "2", "max_items": "1",
        "cert_min_level": "2", "cert_require_current": True,
        "cert_reference_date": "2025-11-19",
    },
}


def _cases() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_test6_c44_c45_original_page_regressions() -> None:
    for case in _cases():
        inventory = build_inventory(case["bidder_id"], case["pages"])
        criterion = C44 if case["criterion"] == "c44" else C45
        result = rescore_from_merged_facts(criterion, inventory)
        assert result is not None, case["case"]
        assert result.score == Decimal(case["expected"]), (case["case"], result.detail)
        assert result.complete is case["complete"], (case["case"], result.detail)
        if Decimal(case["expected"]) == 0:
            assert result.zero_reason == "confirmed_absent", (case["case"], result.detail)


def test_test6_page_regressions_match_frozen_objective_vector() -> None:
    manual = json.loads(MANUAL.read_text(encoding="utf-8"))
    order = [item["id"] for item in manual["bidder_order"]]
    for case in _cases():
        index = order.index(case["bidder_id"])
        expected = manual["expected_objective"][case["criterion"]][index]
        assert case["expected"] == expected, (case["case"], case["expected"], expected)
