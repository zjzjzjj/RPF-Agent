"""classify_material job 集成测试：上传→解析→自动分类→索引端点。

仿 test_price_extract_job 的形态。样例投标文件 7 页结构清晰：
p1 封面 / p2 投标函 / p3 开标一览表 / p4 资格证明文件 / p5-6 技术方案类 / p7 售后服务。
"""
from __future__ import annotations

from .conftest import upload_pdf, wait_job


def test_classify_job_runs_after_parse(client, app_env, samples_dir) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "材料分类测试"}).json()["id"]
    b = client.post(f"/api/projects/{pid}/bidders", json={"name": "甲公司"}).json()
    bid_pdf = samples_dir / "bids" / "bing_恒通软件科技有限公司_投标文件.pdf"
    out = upload_pdf(client, pid, bid_pdf, "bid", bidder_id=b["id"])
    parse_job = wait_job(client, out["job_id"])
    assert parse_job["status"] == "succeeded", parse_job.get("error")
    # 解析成功应自动排队分类任务
    assert parse_job["result_json"].get("classify_job_id"), parse_job["result_json"]
    classify_job = wait_job(client, parse_job["result_json"]["classify_job_id"], timeout=60)
    assert classify_job["status"] == "succeeded", classify_job.get("error")
    assert classify_job["kind"] == "classify_material"
    assert classify_job["result_json"]["pages"] == 7

    r = client.get(f"/api/documents/{out['document_id']}/material-index")
    assert r.status_code == 200, r.text
    idx = r.json()
    assert idx["pages_total"] == 7
    assert idx["pages_classified"] == 7
    assert idx["stale_pages"] == []
    cats = idx["by_category"]

    def _expand(ranges: list[str]) -> set[int]:
        out: set[int] = set()
        for r in ranges:
            body = r.lstrip("p")
            if "-" in body:
                a, b = body.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            else:
                out.add(int(body))
        return out

    assert 3 in _expand(cats["price"]), cats
    assert 4 in _expand(cats["qualification"]), cats
    assert _expand(cats["technical"] + cats["commercial"]) & {5, 6}, cats

    # 投标人级索引
    rb = client.get(f"/api/bidders/{b['id']}/material-index")
    assert rb.status_code == 200
    assert len(rb.json()) == 1


def test_classify_rerun_idempotent(client, app_env, samples_dir) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "分类重跑幂等"}).json()["id"]
    b = client.post(f"/api/projects/{pid}/bidders", json={"name": "乙公司"}).json()
    bid_pdf = samples_dir / "bids" / "jia_华信科技股份有限公司_投标文件.pdf"
    out = upload_pdf(client, pid, bid_pdf, "bid", bidder_id=b["id"])
    parse_job = wait_job(client, out["job_id"])
    wait_job(client, parse_job["result_json"]["classify_job_id"], timeout=60)

    doc_id = out["document_id"]
    first = client.get(f"/api/documents/{doc_id}/material-index").json()
    n_first = first["pages_classified"]

    # 手动重跑
    r = client.post(f"/api/documents/{doc_id}/classify")
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["job_id"], timeout=60)
    assert job["status"] == "succeeded", job.get("error")

    second = client.get(f"/api/documents/{doc_id}/material-index").json()
    assert second["pages_classified"] == n_first  # 行数不翻倍
    assert second["by_category"] == first["by_category"]


def test_classify_not_scheduled_for_tender(client, app_env, samples_dir) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "招标侧不分类"}).json()["id"]
    tender_pdf = next((samples_dir / "tender").glob("*招标文件.pdf"))
    out = upload_pdf(client, pid, tender_pdf, "tender")
    parse_job = wait_job(client, out["job_id"], timeout=120)
    assert parse_job["status"] == "succeeded", parse_job.get("error")
    assert "classify_job_id" not in (parse_job["result_json"] or {})


def test_classify_404(client, app_env) -> None:  # noqa: ANN001
    assert client.post("/api/documents/999999/classify").status_code == 404
    assert client.get("/api/documents/999999/material-index").status_code == 404


def test_price_domain_hard_filter(client, app_env, samples_dir) -> None:  # noqa: ANN001
    """报价抽取的检索域：price 类页非空 → 只在类内找；空 → 回退全量（安全阀）。"""
    from app.services.bidder_service import _price_domain_text

    pid = client.post("/api/projects", json={"name": "报价检索域"}).json()["id"]
    b = client.post(f"/api/projects/{pid}/bidders", json={"name": "丙公司"}).json()
    bid_pdf = samples_dir / "bids" / "bing_恒通软件科技有限公司_投标文件.pdf"
    out = upload_pdf(client, pid, bid_pdf, "bid", bidder_id=b["id"])
    parse_job = wait_job(client, out["job_id"])
    wait_job(client, parse_job["result_json"]["classify_job_id"], timeout=60)

    full = "FULL_TEXT_MARKER 恒通 投标总价"
    dom = _price_domain_text(b["id"], full, [out["document_id"]])
    # 该样例 p2-3 是 price 类 → 检索域非全量文本且不含无关页内容
    assert dom != full
    assert "开标一览表" in dom or "投标" in dom

    # 空集安全阀：不存在的文档 → 回退全量
    assert _price_domain_text(b["id"], full, []) == full
    assert _price_domain_text(b["id"], full, [999999]) == full
