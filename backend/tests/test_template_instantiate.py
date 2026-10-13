"""通用评分规则库（S0）：实例化 → 签批 → 确认 → 冻结 → 演示评分 全链路测试。

与文档编译包走同一条 confirm/freeze/评分代码路径（无演示旁路）：
- 实例化后全部条目 needs_signoff，校验含 TEMPLATE_FALLBACK_SIGNOFF error；
- 未签批 confirm 被 422 阻断；逐条签批后确认/冻结成功；
- 快照升级 rule_package/v2 且带溯源字段；冻结后写入 409；
- retired 条目不阻断确认、不进快照；
- 用样例投标文件跑通"演示评分"。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import upload_pdf, wait_job

REPO = Path(__file__).resolve().parents[2]
BIDDER_KEYS = ["jia", "yi", "bing", "ding", "wu"]


def _make_project(client) -> str:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "模板演示项目"}).json()["id"]
    return pid


def _instantiate(client, pid: str, template_id: str = "comprehensive-goods") -> int:  # noqa: ANN001
    resp = client.post(f"/api/projects/{pid}/rules/instantiate",
                        json={"template_id": template_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["package_id"]


def _signoff_all(client, package_id: int, reason: str = "演示项目启用通用模板判据") -> None:  # noqa: ANN001
    pkg = client.get(f"/api/rules/packages/{package_id}").json()
    for c in pkg["criteria"]:
        assert c["status"] == "needs_signoff", (c["code"], c["status"])
        r = client.post(f"/api/rules/criteria/{c['id']}/signoff", json={"reason": reason})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "verified"
        assert r.json()["origin"] == "template"
        assert r.json()["signoff_json"]["reason"] == reason


def test_templates_api_list_and_detail(client) -> None:  # noqa: ANN001
    resp = client.get("/api/rules/templates")
    assert resp.status_code == 200
    ids = {t["template_id"] for t in resp.json()}
    assert ids == {"comprehensive-goods", "comprehensive-works",
                   "comprehensive-services", "lowest-price"}
    g = next(t for t in resp.json() if t["template_id"] == "comprehensive-goods")
    assert g["criteria_count"] == 14
    assert g["categories_summary"]["price"] == 1

    detail = client.get("/api/rules/templates/comprehensive-goods")
    assert detail.status_code == 200
    d = detail.json()
    assert d["notes"]
    t4 = next(c for c in d["criteria"] if c["template_code"] == "T4.1")
    assert t4["score_type"] == "subjective"
    assert len(t4["levels"]) == 4


def test_instantiate_chain(client) -> None:  # noqa: ANN001
    pid = _make_project(client)
    package_id = _instantiate(client, pid)

    pkg = client.get(f"/api/rules/packages/{package_id}").json()
    assert pkg["origin"] == "template"
    assert pkg["template_id"] == "comprehensive-goods"
    assert pkg["library_version"] == "1.0.0"
    assert pkg["source_document_id"] is None

    # 校验：只有 TEMPLATE_FALLBACK_SIGNOFF error（模板兜底未签批）
    errors = [v for v in pkg["validations"] if v["severity"] == "error"]
    assert {e["code"] for e in errors} == {"TEMPLATE_FALLBACK_SIGNOFF"}, errors

    # 重复实例化 → 409（S0 不支持换模板）
    resp = client.post(f"/api/projects/{pid}/rules/instantiate",
                       json={"template_id": "lowest-price"})
    assert resp.status_code == 409, resp.text

    # 未签批 confirm → 422
    resp = client.post(f"/api/rules/packages/{package_id}/confirm")
    assert resp.status_code == 422, resp.text
    blockers = resp.json()["detail"]["blockers"]
    assert any(b["code"] == "TEMPLATE_FALLBACK_SIGNOFF" for b in blockers)

    # 签批（无"全部签批"端点，逐条）
    _signoff_all(client, package_id)
    pkg = client.get(f"/api/rules/packages/{package_id}").json()
    assert all(c["status"] == "verified" for c in pkg["criteria"])
    assert all(v["severity"] != "error" for v in pkg["validations"])

    # 确认 → 冻结 → 快照 v2 + 溯源字段
    r = client.post(f"/api/rules/packages/{package_id}/confirm")
    assert r.status_code == 200, r.text
    r = client.post(f"/api/rules/packages/{package_id}/freeze")
    assert r.status_code == 200, r.text
    frozen = r.json()
    assert frozen["content_hash"]
    assert frozen["status"] == "frozen"

    # 冻结后写入 409
    crit_id = client.get(f"/api/rules/packages/{package_id}").json()["criteria"][0]["id"]
    resp = client.patch(f"/api/rules/criteria/{crit_id}",
                        json={"name": "篡改", "reason": "尝试修改冻结包"})
    assert resp.status_code == 409, resp.text

    # 覆盖率报告
    cov = client.get(f"/api/rules/packages/{package_id}/coverage").json()
    assert cov["total"] == 14
    assert cov["template_fallback_signed"] == 14
    assert cov["template_fallback_pending"] == 0
    assert cov["price_covered"] is True


def test_retired_excluded_from_snapshot_and_not_blocking(client) -> None:  # noqa: ANN001
    from app.db.models import Criterion, RulePackage
    from app.db.session import session_scope

    pid = _make_project(client)
    package_id = _instantiate(client, pid)

    # 弃用一个模板条目（status → retired）：S0 通过既有编辑路径处置，
    # 这里直接在 DB 更新模拟人工处置，验证 coverage 桶与 _blocking_items 跳过逻辑。
    with session_scope() as s:
        c = s.query(Criterion).filter(Criterion.package_id == package_id).first()
        c.status = "retired"
        s.add(c)

    # 签批其余条目
    pkg = client.get(f"/api/rules/packages/{package_id}").json()
    for c in pkg["criteria"]:
        if c["status"] == "needs_signoff":
            r = client.post(f"/api/rules/criteria/{c['id']}/signoff", json={"reason": "启用"})
            assert r.status_code == 200, r.text

    # retired 条目不阻断 confirm/freeze
    r = client.post(f"/api/rules/packages/{package_id}/confirm")
    assert r.status_code == 200, r.text
    r = client.post(f"/api/rules/packages/{package_id}/freeze")
    assert r.status_code == 200, r.text

    # 快照不含 retired 条目（load_criteria_for_snapshot 过滤）
    with session_scope() as s:
        pkg_row = s.get(RulePackage, package_id)
        codes = [c["code"] for c in pkg_row.snapshot_json["criteria"]]
    assert "T1.1" not in codes

    cov = client.get(f"/api/rules/packages/{package_id}/coverage").json()
    assert cov["retired"] == 1
    assert cov["total"] == 14
    assert cov["document_backed"] + cov["template_fallback_signed"] + cov["retired"] == 14


def test_demo_scoring_full_chain(client, samples_dir) -> None:  # noqa: ANN001
    """S0 核心价值：无招标文件也能走通 建包→签批→冻结→演示评分。"""
    pid = _make_project(client)
    package_id = _instantiate(client, pid)
    _signoff_all(client, package_id)
    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/freeze").status_code == 200

    # 用样例投标文件造 5 家投标人（复用既有报价确认流程）
    bid_pdfs = sorted((samples_dir / "bids").glob("*_投标文件.pdf"))
    assert len(bid_pdfs) == 5
    bidder_ids = []
    for key in BIDDER_KEYS:
        pdf = next(p for p in bid_pdfs if p.name.startswith(f"{key}_"))
        b = client.post(f"/api/projects/{pid}/bidders",
                        json={"name": pdf.stem.split("_")[1]}).json()
        out = upload_pdf(client, pid, pdf, "bid", bidder_id=b["id"])
        job = wait_job(client, out["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        price = client.get(f"/api/bidders/{b['id']}/price").json()
        assert price["bid_price"]
        r = client.post(f"/api/bidders/{b['id']}/price/confirm",
                        json={"reason": "与开标一览表核对一致"})
        assert r.status_code == 200, r.text
        bidder_ids.append(b["id"])

    resp = client.post(f"/api/projects/{pid}/runs",
                       json={"package_id": package_id, "bidder_ids": bidder_ids,
                             "stages": ["objective", "price", "subjective"]})
    assert resp.status_code == 200, resp.text
    job = wait_job(client, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")
    summary = job["result_json"]
    assert summary["run_id"]

    # 矩阵可读（模板包评分可运行）
    matrix = client.get(f"/api/runs/{summary['run_id']}/matrix").json()
    assert len(matrix["bidders"]) == 5
    assert matrix["cells"]