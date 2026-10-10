"""M1 验收：真实样例 PDF 的上传 → 解析 → 落库 → 条款树查询（全离线）。"""
from __future__ import annotations

import pytest

from .conftest import upload_pdf, wait_job


@pytest.fixture()
def project(client) -> str:
    resp = client.post("/api/projects", json={"name": "样例评标项目", "max_price": "8000000"})
    assert resp.status_code == 200
    return resp.json()["id"]


def test_tender_upload_and_parse(client, project, samples_dir):
    tender_pdf = next((samples_dir / "tender").glob("*招标文件.pdf"))
    out = upload_pdf(client, project, tender_pdf, "tender")
    job = wait_job(client, out["job_id"])
    assert job["status"] == "succeeded", job.get("error")

    doc = client.get(f"/api/documents/{out['document_id']}").json()
    assert doc["parse_status"] == "parsed"
    assert doc["page_count"] >= 5
    assert doc["parse_engine"] == "pdfplumber"

    # 条款树：根节点下应能找到"评标办法"章节
    nodes = client.get(f"/api/documents/{out['document_id']}/nodes").json()
    assert len(nodes) == 1 and nodes[0]["kind"] == "document"
    root_id = nodes[0]["node_id"]
    chapters = client.get(f"/api/documents/{out['document_id']}/nodes",
                          params={"parent_id": root_id}).json()
    titles = [c["title"] for c in chapters]
    assert any("评标办法" in (t or "") for t in titles), titles
    assert any("投标人须知" in (t or "") for t in titles), titles

    # 页文本可查（价格公式在第 4 页）
    pages = client.get(f"/api/documents/{out['document_id']}/pages", params={"from_page": 4, "to_page": 4}).json()
    assert len(pages) == 1
    assert "0.95" in pages[0]["text"]
    assert pages[0]["width"] > 0 and pages[0]["height"] > 0


def test_bid_upload_and_parse(client, project, samples_dir):
    bid_pdf = next((samples_dir / "bids").glob("jia_*投标文件.pdf"))
    out = upload_pdf(client, project, bid_pdf, "bid")
    job = wait_job(client, out["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    doc = client.get(f"/api/documents/{out['document_id']}").json()
    assert doc["parse_status"] == "parsed"

    # 投标文件的关键节点：投标函、开标一览表
    nodes = client.get(f"/api/documents/{out['document_id']}/nodes",
                       params={"parent_id": f"bid{out['document_id']}:root"}).json()
    titles = [n["title"] for n in nodes]
    assert any("投标函" in (t or "") for t in titles), titles


def test_page_image_rendered(client, project, samples_dir):
    tender_pdf = next((samples_dir / "tender").glob("*招标文件.pdf"))
    out = upload_pdf(client, project, tender_pdf, "tender")
    wait_job(client, out["job_id"])
    resp = client.get(f"/api/documents/{out['document_id']}/pages/4/image", params={"scale": 1.5})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_non_pdf_rejected(client, project, tmp_path):
    bad = tmp_path / "not_a_pdf.pdf"
    bad.write_text("这不是 PDF", encoding="utf-8")
    out = upload_pdf(client, project, bad, "tender")
    job = wait_job(client, out["job_id"])
    # 拒绝是正常结果：任务成功结束，文档标记 failed 并带稳定拒绝码
    assert job["status"] == "succeeded"
    doc = client.get(f"/api/documents/{out['document_id']}").json()
    assert doc["parse_status"] == "failed"
    assert doc["warnings_json"][0]["code"] == "NOT_A_PDF"
