"""报价抽取的后台任务（extract_price job）。

原 `POST /bidders/{id}/price/extract` 端点已随投标人管理下线；job 处理器保留
（运维/脚本复用），本文件直提任务验证：返回 job、任务成功、报价落库、
进度字段有值、投标人不存在时任务失败、无文档不崩。
"""
from __future__ import annotations

from app.jobs import queue

from .conftest import add_bidder, upload_pdf, wait_job


def _submit(bidder_id: int, project_id: str) -> int:
    return queue.submit("extract_price", {"bidder_id": bidder_id}, project_id=project_id)


def test_extract_price_job_succeeds(client, app_env, samples_dir) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "报价后台抽取测试"}).json()["id"]
    b = add_bidder(pid, "甲公司")
    bid_pdf = next((samples_dir / "bids").glob("*.pdf"))
    out = upload_pdf(client, pid, bid_pdf, "bid", bidder_id=b["id"])
    assert wait_job(client, out["job_id"])["status"] == "succeeded"

    job_id = _submit(b["id"], pid)
    job = wait_job(client, job_id, timeout=120)
    assert job["status"] == "succeeded", job.get("error")
    assert job["kind"] == "extract_price"
    assert job["total_steps"] > 0
    assert job["done_steps"] > 0
    assert job["result_json"]["bidder_id"] == b["id"]

    # 报价已落库（该样例投标文件含开标一览表）
    bidder = client.get(f"/api/bidders/{b['id']}").json()
    assert bidder["price_status"] in ("extracted", "conflict")


def test_extract_price_job_unknown_bidder_fails(client, app_env) -> None:  # noqa: ANN001
    job = wait_job(client, _submit(999999, "PRJ-NOPE"), timeout=60)
    assert job["status"] == "failed"
    assert "投标人不存在" in (job["error"] or "")


def test_extract_price_job_no_documents(client, app_env) -> None:  # noqa: ANN001
    """无已解析文档也能正常结束（不崩），只是抽不到报价。"""
    pid = client.post("/api/projects", json={"name": "无文档报价抽取"}).json()["id"]
    b = add_bidder(pid, "乙公司")

    job = wait_job(client, _submit(b["id"], pid), timeout=60)
    assert job["status"] == "succeeded", job.get("error")
    assert client.get(f"/api/bidders/{b['id']}").json()["bid_price"] is None
