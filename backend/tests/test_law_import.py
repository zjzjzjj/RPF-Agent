"""法律通用规则包导入（S-Law）：导入 → 确认 → 冻结 全生命周期测试。

覆盖（对应计划 §八·1）：
- 导入建包字段语义（origin='law'、p_source='P0'、verified、免签批、on_fail、dropped_nodes）；
- 幂等重导 / 追加进已有草稿 / confirmed 后 409；
- 坏结构与不可核验法条 → 422 且零写入；
- 无需任何 signoff 即可 confirm/freeze；快照带 law 溯源字段；
- 知识库缺失 → confirm 422（fail-closed）；
- coverage 新增 law 桶；validate_only 不落库。
"""
from __future__ import annotations

import json

import pytest

LAW51 = (
    "有下列情形之一的，评标委员会应当否决其投标：（一）投标文件未经投标单位盖章和单位负责人签字；"
    "（二）投标联合体没有提交共同投标协议；（三）投标人不符合国家或者招标文件规定的资格条件。"
)
LAW40 = (
    "有下列情形之一的，视为投标人相互串通投标：（一）不同投标人的投标文件由同一单位或者个人编制；"
    "（二）不同投标人委托同一单位或者个人办理投标事宜。"
)
LAW26 = "投标人应当具备承担招标项目的能力；国家有关规定对投标人资格条件有规定的，投标人应当具备规定的资格条件。"

NODE_51 = "法条://中华人民共和国招标投标法实施条例（2019年修订）/第五十一条"
NODE_40 = "法条://中华人民共和国招标投标法实施条例（2019年修订）/第四十条"
NODE_26 = "法条://中华人民共和国招标投标法（2017年修正本）/第二十六条"
NODE_BID_DOC = "招标文件://投标人须知前附表"  # 非法条前缀 → 导入时置空并留痕


def _doc() -> dict:
    """6 条合成包：4 veto（human_only/document_present/collusion_check/count_at_least）
    + 2 match（其一带招标文件:// 节点用于 dropped_nodes 断言）。"""
    return {
        "package": {"name": "法律规则测试包", "version": "0.9.0", "source": "LAW",
                    "total_max": 100, "status": "draft"},
        "criteria": [
            {"code": "REJ-001", "category": "compliance", "name": "投标文件未经投标单位盖章或单位负责人签字",
             "score_type": "veto", "is_veto": True, "veto_consequence": "disqualify",
             "description": "投标文件须经投标单位盖章和单位负责人签字，否则否决投标。",
             "max_score": None, "weight": None, "levels": [], "check_type": "human_only", "params": {},
             "source_quote": LAW51, "source_node_id": NODE_51},
            {"code": "REJ-002", "category": "compliance", "name": "投标联合体没有提交共同投标协议",
             "score_type": "veto", "is_veto": True, "veto_consequence": "disqualify",
             "description": "联合体投标须提交共同投标协议，否则否决投标。",
             "max_score": None, "weight": None, "levels": [],
             "check_type": "document_present", "params": {"document": "联合体共同投标协议"},
             "source_quote": LAW51, "source_node_id": NODE_51},
            {"code": "REJ-009", "category": "compliance", "name": "视为投标人相互串通投标",
             "score_type": "veto", "is_veto": True, "veto_consequence": "disqualify",
             "description": "条例第四十条六种客观情形命中任一即视为串通投标。",
             "max_score": None, "weight": None, "levels": [], "check_type": "collusion_check", "params": {},
             "source_quote": LAW40, "source_node_id": NODE_40},
            {"code": "QUAL-009", "category": "qualification", "name": "类似业绩证明材料完整性",
             "score_type": "veto", "is_veto": True, "veto_consequence": "disqualify",
             "description": "业绩须提供合同关键页及中标通知书或验收证明。",
             "max_score": None, "weight": None, "levels": [],
             "check_type": "count_at_least", "params": {"field": "performance", "min_count": 1},
             "source_quote": LAW26, "source_node_id": NODE_26},
            {"code": "MATCH-001", "category": "compliance", "name": "投标文件装订与骑缝章",
             "score_type": "match", "is_veto": False, "veto_consequence": None,
             "description": "未按要求加盖骑缝章的，招标文件明确约定时才否决，否则记录后进入评审。",
             "max_score": None, "weight": None, "levels": [], "check_type": "human_only", "params": {},
             "source_quote": "招标文件「投标文件编制要求」章节载明", "source_node_id": NODE_BID_DOC},
            {"code": "MATCH-002", "category": "compliance", "name": "形式评审与澄清边界",
             "score_type": "match", "is_veto": False, "veto_consequence": None,
             "description": "非实质瑕疵可书面澄清补正，但不得改变投标文件的实质性内容。",
             "max_score": None, "weight": None, "levels": [], "check_type": "human_only", "params": {},
             "source_quote": LAW51, "source_node_id": NODE_51},
        ],
    }


@pytest.fixture()
def law_kb(tmp_path):
    """写入小法条知识库到 data_dir/law_kb.json（app_env 已把 data_dir 指向 tmp_path）。"""
    import app.rules.law_kb as lk

    kb = [
        {"lawName": "中华人民共和国招标投标法实施条例（2019年修订）",
         "articles": [{"articleNo": "第五十一条", "text": LAW51},
                      {"articleNo": "第四十条", "text": LAW40}]},
        {"lawName": "中华人民共和国招标投标法（2017年修正本）",
         "articles": [{"articleNo": "第二十六条", "text": LAW26}]},
    ]
    path = tmp_path / "law_kb.json"
    path.write_text(json.dumps(kb, ensure_ascii=False), encoding="utf-8")
    lk.reset_cache()
    yield path
    lk.reset_cache()


def _import(client, pid: str, doc: dict | None = None):  # noqa: ANN001
    return client.post(f"/api/projects/{pid}/rules/import-law",
                       json=doc if doc is not None else _doc())


def test_import_creates_law_package(client, law_kb) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "法律导入项目"}).json()["id"]
    r = _import(client, pid)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] is True
    assert out["imported"] == 6
    assert out["skipped_codes"] == []
    # 非法条节点被置空并留痕
    assert out["dropped_nodes"] == [{"code": "MATCH-001", "node_id": NODE_BID_DOC}]
    assert all(v["severity"] != "error" for v in out["validations"]), out["validations"]

    pkg = client.get(f"/api/rules/packages/{out['package_id']}").json()
    assert pkg["origin"] == "law"
    assert pkg["status"] == "draft"
    assert pkg["library_version"] == "0.9.0"
    assert len(pkg["criteria"]) == 6
    for c in pkg["criteria"]:
        assert c["origin"] == "law"
        assert c["p_source"] == "P0"
        assert c["status"] == "verified"
        assert c["signoff_json"] is None
    by_code = {c["code"]: c for c in pkg["criteria"]}
    assert by_code["MATCH-001"]["source_node_id"] is None  # 招标文件:// 已置空
    assert by_code["REJ-001"]["source_node_id"] == NODE_51  # 法条:// 保留
    # check 行：is_veto → recommend_reject；match → need_human
    checks = {c["code"]: c["checks"][0] for c in pkg["criteria"]}
    assert checks["REJ-002"]["check_type"] == "document_present"
    assert checks["REJ-002"]["on_fail"] == "recommend_reject"
    assert checks["REJ-009"]["check_type"] == "collusion_check"
    assert checks["MATCH-001"]["on_fail"] == "need_human"


def test_import_is_idempotent(client, law_kb) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "幂等测试"}).json()["id"]
    first = _import(client, pid).json()
    second = _import(client, pid)
    assert second.status_code == 200, second.text
    out = second.json()
    assert out["created"] is False
    assert out["imported"] == 0
    assert set(out["skipped_codes"]) == {c["code"] for c in _doc()["criteria"]}
    pkg = client.get(f"/api/rules/packages/{out['package_id']}").json()
    assert len(pkg["criteria"]) == len(first["skipped_codes"]) + first["imported"]


def test_append_into_existing_draft(client, law_kb) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "模板+法律混合"}).json()["id"]
    inst = client.post(f"/api/projects/{pid}/rules/instantiate",
                       json={"template_id": "comprehensive-goods"})
    assert inst.status_code == 200, inst.text
    out = _import(client, pid).json()
    assert out["created"] is False
    assert out["imported"] == 6  # 幂等追加，不覆盖模板条目
    pkg = client.get(f"/api/rules/packages/{out['package_id']}").json()
    assert len(pkg["criteria"]) == 14 + 6
    origins = {c["origin"] for c in pkg["criteria"]}
    assert origins == {"template", "law"}
    # 模板条目未签批的 error 照常返回（导入只报告，不阻断）
    assert any(v["code"] == "TEMPLATE_FALLBACK_SIGNOFF" for v in out["validations"])


def test_confirmed_package_conflict_409(client, law_kb) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "确认后禁止导入"}).json()["id"]
    pkg_id = _import(client, pid).json()["package_id"]
    r = client.post(f"/api/rules/packages/{pkg_id}/confirm")
    assert r.status_code == 200, r.text  # 6 条全 verified，免签批直接过闸门
    r = _import(client, pid)
    assert r.status_code == 409, r.text
    assert "versions" in r.json()["message"]


def test_bad_structure_422_zero_writes(client, law_kb) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "坏结构"}).json()["id"]
    bad = _doc()
    bad["criteria"][0]["score_type"] = "not_a_type"
    r = _import(client, pid, bad)
    assert r.status_code == 422, r.text
    blockers = r.json()["detail"]["blockers"]
    assert any(b.get("code") == "REJ-001" and "score_type" in b.get("reason", "")
               for b in blockers)
    assert client.get(f"/api/projects/{pid}/rules").json()["id"] == 0  # 零写入


def test_unverifiable_law_node_422(client, law_kb) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "法条不存在"}).json()["id"]
    bad = _doc()
    bad["criteria"][0]["source_node_id"] = "法条://中华人民共和国招标投标法实施条例（2019年修订）/第九百九十九条"
    r = _import(client, pid, bad)
    assert r.status_code == 422, r.text
    blockers = r.json()["detail"]["blockers"]
    assert any(b["code"] == "REJ-001" and b["reason"] == "法条在知识库中不存在" for b in blockers)
    assert client.get(f"/api/projects/{pid}/rules").json()["id"] == 0  # 零写入


def test_confirm_freeze_without_signoff(client, law_kb) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "免签批冻结"}).json()["id"]
    pkg_id = _import(client, pid).json()["package_id"]
    # 法律包免签批、无阻断项 → confirm 无需任何 accept/signoff 动作
    r = client.post(f"/api/rules/packages/{pkg_id}/confirm")
    assert r.status_code == 200, r.text
    r = client.post(f"/api/rules/packages/{pkg_id}/freeze")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "frozen"
    assert r.json()["content_hash"]

    from app.db.models import RulePackage
    from app.db.session import session_scope

    with session_scope() as s:
        snap = s.get(RulePackage, pkg_id).snapshot_json
    assert snap["package"]["origin"] == "law"
    assert len(snap["criteria"]) == 6
    for c in snap["criteria"]:
        assert c["origin"] == "law"
        assert c["p_source"] == "P0"
        assert c["signoff"] is None


def test_citation_requires_law_kb_fail_closed(client, law_kb) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "知识库缺失"}).json()["id"]
    pkg_id = _import(client, pid).json()["package_id"]
    law_kb.unlink()  # 导入后知识库被移除 → confirm 必须被 CITATION 拦住（fail-closed）
    r = client.post(f"/api/rules/packages/{pkg_id}/confirm")
    assert r.status_code == 422, r.text
    blockers = r.json()["detail"]["blockers"]
    assert any(b["code"] == "CITATION_UNREACHABLE" for b in blockers), blockers


def test_coverage_law_bucket(client, law_kb) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "覆盖率"}).json()["id"]
    pkg_id = _import(client, pid).json()["package_id"]
    cov = client.get(f"/api/rules/packages/{pkg_id}/coverage").json()
    assert cov["law"] == 6
    assert cov["template_fallback_pending"] == 0
    assert cov["document_backed"] == 0
    assert cov["total"] == 6
    assert cov["veto_covered_ratio"] == 1.0
    assert cov["by_category"]["compliance"]["law"] == 5
    assert cov["by_category"]["qualification"]["law"] == 1


def test_validate_only_dry_run(client, law_kb) -> None:  # noqa: ANN001
    from app.errors import ValidationFailed
    from app.services import law_import_service

    # 好包 → 0 findings
    findings = law_import_service.validate_only(_doc())
    assert findings == [], findings

    # 坏包 → 422（词表）
    bad = _doc()
    bad["criteria"][3]["category"] = "no_such_category"
    with pytest.raises(ValidationFailed):
        law_import_service.validate_only(bad)

    # dry-run 全程零落库
    pid = client.post("/api/projects", json={"name": "dry-run"}).json()["id"]
    assert client.get(f"/api/projects/{pid}/rules").json()["id"] == 0


def test_law_kb_node_exists_unit() -> None:  # noqa: ANN001
    import app.rules.law_kb as lk

    from app.services import law_import_service  # noqa: F401  确保包可导入

    lk.reset_cache()
    try:
        assert lk.node_exists("") is False
        assert lk.node_exists("招标文件://投标人须知前附表") is False  # 非法条前缀
        assert lk.node_exists("法条://只有一段") is False  # 缺条号
    finally:
        lk.reset_cache()
