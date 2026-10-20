"""报价抽取的后台任务路径（POST /bidders/{id}/price/extract）。

同步 legacy 路径（GET /bidders/{id}/price）由其他 e2e 测试覆盖；本文件只验新入口：
返回 job_id、任务成功、报价落库、进度字段有值、不存在时 404。
"""
from __future__ import annotations

from .conftest import upload_pdf, wait_job


def test_extract_price_job_succeeds(client, app_env, samples_dir) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "报价后台抽取测试"}).json()["id"]
    b = client.post(f"/api/projects/{pid}/bidders", json={"name": "甲公司"}).json()
    bid_pdf = next((samples_dir / "bids").glob("*.pdf"))
    out = upload_pdf(client, pid, bid_pdf, "bid", bidder_id=b["id"])
    assert wait_job(client, out["job_id"])["status"] == "succeeded"

    r = client.post(f"/api/bidders/{b['id']}/price/extract")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bidder_id"] == b["id"]
    job_id = body["job_id"]

    job = wait_job(client, job_id, timeout=120)
    assert job["status"] == "succeeded", job.get("error")
    assert job["kind"] == "extract_price"
    assert job["total_steps"] > 0
    assert job["done_steps"] > 0
    assert job["result_json"]["bidder_id"] == b["id"]

    # 报价已落库（该样例投标文件含开标一览表）
    bidder = client.get(f"/api/bidders/{b['id']}").json()
    assert bidder["price_status"] in ("extracted", "conflict")


def test_extract_price_job_404(client, app_env) -> None:  # noqa: ANN001
    assert client.post("/api/bidders/999999/price/extract").status_code == 404


def test_extract_price_job_no_documents(client, app_env) -> None:  # noqa: ANN001
    """无已解析文档也能正常结束（不崩），只是抽不到报价。"""
    pid = client.post("/api/projects", json={"name": "无文档报价抽取"}).json()["id"]
    b = client.post(f"/api/projects/{pid}/bidders", json={"name": "乙公司"}).json()

    r = client.post(f"/api/bidders/{b['id']}/price/extract")
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["job_id"], timeout=60)
    assert job["status"] == "succeeded", job.get("error")
    assert client.get(f"/api/bidders/{b['id']}").json()["bid_price"] is None
