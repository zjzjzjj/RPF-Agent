"""投标人删除：依赖序清理（文档/页/节点/证据），评分记录存在时拒绝，审计保留。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .conftest import upload_pdf, wait_job


def _counts(db_path: Path, bidder_id: int) -> dict[str, int]:
    """直查 sqlite：该投标人关联的各表行数（另含 bidders 总数）。"""
    con = sqlite3.connect(str(db_path))
    try:
        q = {
            "bidders_total": "SELECT COUNT(*) FROM bidders",
            "bidders": f"SELECT COUNT(*) FROM bidders WHERE id = {bidder_id}",
            "documents": f"SELECT COUNT(*) FROM documents WHERE bidder_id = {bidder_id}",
            "bidder_documents": (
                f"SELECT COUNT(*) FROM bidder_documents WHERE bidder_id = {bidder_id}"
            ),
            "pages": (
                "SELECT COUNT(*) FROM document_pages WHERE document_id IN "
                f"(SELECT id FROM documents WHERE bidder_id = {bidder_id})"
            ),
            "nodes": (
                "SELECT COUNT(*) FROM doc_nodes WHERE document_id IN "
                f"(SELECT id FROM documents WHERE bidder_id = {bidder_id})"
            ),
            "citations": (
                "SELECT COUNT(*) FROM evidence_citations WHERE document_id IN "
                f"(SELECT id FROM documents WHERE bidder_id = {bidder_id})"
            ),
        }
        return {k: con.execute(v).fetchone()[0] for k, v in q.items()}
    finally:
        con.close()


def test_delete_bidder_cascades_documents(client, app_env, samples_dir) -> None:  # noqa: ANN001
    """删除投标人连带其投标文件/页/节点；另一家不受影响；审计留痕。"""
    pid = client.post("/api/projects", json={"name": "投标人删除测试"}).json()["id"]
    bid_pdf = next((samples_dir / "bids").glob("*.pdf"))

    victim = client.post(f"/api/projects/{pid}/bidders", json={"name": "待删投标人"}).json()
    keeper = client.post(f"/api/projects/{pid}/bidders", json={"name": "保留投标人"}).json()
    for b in (victim, keeper):
        out = upload_pdf(client, pid, bid_pdf, "bid", bidder_id=b["id"])
        assert wait_job(client, out["job_id"])["status"] == "succeeded"

    before = _counts(app_env / "test.db", victim["id"])
    assert before["bidders"] == 1 and before["documents"] == 1
    assert before["pages"] > 0 and before["nodes"] > 0

    r = client.delete(f"/api/bidders/{victim['id']}")
    assert r.status_code == 200, r.text
    counts = r.json()
    assert counts["deleted"] == victim["id"]
    assert counts["documents"] == 1
    assert counts["bidders"] == 1

    after = _counts(app_env / "test.db", victim["id"])
    assert after["bidders"] == 0
    assert after["documents"] == 0 and after["pages"] == 0 and after["nodes"] == 0
    assert after["bidder_documents"] == 0 and after["citations"] == 0
    # 另一家毫发无损
    assert after["bidders_total"] == 1
    assert client.get(f"/api/bidders/{keeper['id']}").status_code == 200

    # 幂等语义：再删 404
    assert client.delete(f"/api/bidders/{victim['id']}").status_code == 404

    # 审计保留
    con = sqlite3.connect(str(app_env / "test.db"))
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = 'bidder.delete'"
        ).fetchone()[0]
    finally:
        con.close()
    assert n >= 1


def test_delete_bidder_removes_files(client, app_env, samples_dir) -> None:  # noqa: ANN001
    """物理投标文件随删除一并清理。"""
    pid = client.post("/api/projects", json={"name": "文件清理测试"}).json()["id"]
    bid_pdf = next((samples_dir / "bids").glob("*.pdf"))
    b = client.post(f"/api/projects/{pid}/bidders", json={"name": "待删投标人"}).json()
    out = upload_pdf(client, pid, bid_pdf, "bid", bidder_id=b["id"])
    assert wait_job(client, out["job_id"])["status"] == "succeeded"

    con = sqlite3.connect(str(app_env / "test.db"))
    try:
        stored = con.execute(
            f"SELECT stored_path FROM documents WHERE bidder_id = {b['id']}"
        ).fetchone()[0]
    finally:
        con.close()
    assert Path(stored).exists()

    assert client.delete(f"/api/bidders/{b['id']}").status_code == 200
    assert not Path(stored).exists()


def test_delete_bidder_404(client, app_env) -> None:  # noqa: ANN001
    assert client.delete("/api/bidders/999999").status_code == 404


def test_delete_bidder_blocked_when_scored(client, app_env) -> None:  # noqa: ANN001
    """已产生评分记录的投标人拒绝删除（409），避免历史评分口径不一致。"""
    pid = client.post("/api/projects", json={"name": "已评分投标人删除测试"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/rules/instantiate",
                    json={"template_id": "comprehensive-goods"})
    assert r.status_code == 200, r.text
    package_id = r.json()["package_id"]
    pkg = client.get(f"/api/rules/packages/{package_id}").json()
    for c in pkg["criteria"]:
        assert client.post(f"/api/rules/criteria/{c['id']}/signoff",
                           json={"reason": "测试基线启用"}).status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/freeze").status_code == 200

    b = client.post(f"/api/projects/{pid}/bidders", json={"name": "甲公司"}).json()
    assert client.post(f"/api/bidders/{b['id']}/price/set",
                       json={"price_yuan": "1000000", "reason": "测试录入报价"}).status_code == 200

    resp = client.post(f"/api/projects/{pid}/runs",
                       json={"package_id": package_id, "bidder_ids": [b["id"]],
                             "stages": ["objective", "price"]})
    assert resp.status_code == 200, resp.text
    job = wait_job(client, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")

    con = sqlite3.connect(str(app_env / "test.db"))
    try:
        n = con.execute(
            f"SELECT COUNT(*) FROM score_records WHERE bidder_id = {b['id']}"
        ).fetchone()[0]
    finally:
        con.close()
    assert n > 0, "前置条件：应有评分记录"

    r = client.delete(f"/api/bidders/{b['id']}")
    assert r.status_code == 409, r.text
    assert "评分记录" in r.json()["message"]
    # 未被误删
    assert client.get(f"/api/bidders/{b['id']}").status_code == 200
