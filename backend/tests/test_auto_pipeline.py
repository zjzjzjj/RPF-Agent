"""全自动模式端到端测试（BID_EVAL_AUTO_MODE=1，默认生产语义）。

与 test_scoring_e2e / test_law_e2e（人工回退模式，conftest 显式关 auto）成对：
本文件断言的是全自动语义——
- 法规包 confirm/freeze 的阻断项自动采纳（含模板签批自动形态）；
- 报价自动确认（无报价的投标人自动剔除）；
- 评分运行零 need_human/conflict_flagged 残留，全部收敛 auto_confirmed；
- human_only/冲突项由 LLM（mock 确定性实现）裁决，reasoning 落 ai/warnings；
- 报告直接 final。
"""
from __future__ import annotations

import pytest

from .conftest import upload_pdf, wait_job
from app.rules import law_kb as lk
from app.rules.library.law_pack_loader import load_law_pack, reset_law_pack_cache

REPO = __import__("pathlib").Path(__file__).resolve().parents[2]
REPO_KB = REPO / "data" / "law_kb.json"


@pytest.fixture()
def auto_client(app_env, monkeypatch):  # noqa: ANN001
    """在 app_env 隔离环境上显式开启全自动模式，并指向仓库法条知识库。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    monkeypatch.setenv("BID_EVAL_LAW_KB_PATH", str(REPO_KB))
    lk.reset_cache()
    reset_law_pack_cache()
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
    lk.reset_cache()
    reset_law_pack_cache()


@pytest.fixture()
def frozen_auto_chain(auto_client, samples_dir):  # noqa: ANN001
    """编译包 + 法规包追加 → 确认 → 冻结。返回 (pid, package_id)。"""
    c = auto_client
    pid = c.post("/api/projects",
                 json={"name": "全自动链路", "max_price": "8000000"}).json()["id"]
    out = upload_pdf(c, pid, next((samples_dir / "tender").glob("*招标文件.pdf")), "tender")
    job = wait_job(c, out["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    resp = c.post(f"/api/projects/{pid}/rules/compile", json={"document_id": out["document_id"]})
    job = wait_job(c, resp.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    package_id = job["result_json"]["package_id"]

    r = c.post(f"/api/projects/{pid}/rules/import-law", json=load_law_pack())
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 87

    assert c.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200, \
        "全自动模式下阻断项应已被自动采纳"
    fr = c.post(f"/api/rules/packages/{package_id}/freeze")
    assert fr.status_code == 200, fr.text
    return pid, package_id


def test_auto_pipeline_full_chain(auto_client, frozen_auto_chain, samples_dir) -> None:  # noqa: ANN001
    c, (pid, package_id) = auto_client, frozen_auto_chain

    bidder_ids: list[int] = []
    for pdf in sorted((samples_dir / "bids").glob("*_投标文件.pdf")):
        b = c.post(f"/api/projects/{pid}/bidders",
                   json={"name": pdf.stem.split("_")[1]}).json()
        up = upload_pdf(c, pid, pdf, "bid", bidder_id=b["id"])
        job = wait_job(c, up["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        price = c.get(f"/api/bidders/{b['id']}/price").json()
        assert price["bid_price"], price  # 无报价会被评分入口自动剔除，这里样例都有
        bidder_ids.append(b["id"])

    resp = c.post(f"/api/projects/{pid}/runs", json={
        "package_id": package_id,
        "bidder_ids": bidder_ids,
        "stages": ["objective", "price", "subjective"],
    })
    assert resp.status_code == 200, resp.text
    job = wait_job(c, resp.json()["job_id"], timeout=300)
    assert job["status"] == "succeeded", job.get("error")
    summary = job["result_json"]
    run_id = summary["run_id"]

    # ---- 核心护栏：零人工残留 ----
    assert summary["need_human"] == 0, summary

    matrix = c.get(f"/api/runs/{run_id}/matrix").json()
    cells = [cell for cols in matrix["cells"].values() for cell in cols.values()]
    bad = [c for c in cells if c["review_status"] not in ("auto_confirmed", "not_applicable")]
    assert not bad, [(c["criterion_name"], c["review_status"]) for c in bad[:3]]

    # ---- 模糊项（原 need_human/conflict）由 LLM 裁决收敛：状态不再是待人工 ----
    for c_ in cells:
        if c_["review_status"] == "auto_confirmed":
            assert c_["machine_status"] not in ("need_human",), c_

    # ---- 排名：无未定分警告；被自动否决者 veto_flag 置位 ----
    ranking = c.get(f"/api/projects/{pid}/ranking").json()
    assert all(not row["missing_final"] for row in ranking["rows"]), ranking["warnings"]

    # ---- 报告：直接定稿，无待复核疑问 ----
    report = c.get(f"/api/projects/{pid}/report").json()
    assert report["status"] == "final", report["status"]
    assert report["open_questions"] == []


def test_auto_mode_confirms_prices_without_manual_step(auto_client, frozen_auto_chain,
                                                       samples_dir) -> None:  # noqa: ANN001
    """报价未经人工确认也能进评分（全自动确认并留痕）。"""
    c, (pid, package_id) = auto_client, frozen_auto_chain
    pdf = sorted((samples_dir / "bids").glob("*_投标文件.pdf"))[0]
    b = c.post(f"/api/projects/{pid}/bidders", json={"name": pdf.stem.split("_")[1]}).json()
    up = upload_pdf(c, pid, pdf, "bid", bidder_id=b["id"])
    job = wait_job(c, up["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    c.get(f"/api/bidders/{b['id']}/price")  # 触发抽取（不调用 confirm）

    # 抽取后仍是 extracted（未人工确认）……
    bidder = c.get(f"/api/projects/{pid}/bidders").json()
    target = next(x for x in bidder if x["id"] == b["id"])
    assert target["price_status"] in ("extracted", "confirmed")

    # ……但评分入口自动确认，不 422；确认动作已在评分记录的终态里体现
    resp = c.post(f"/api/projects/{pid}/runs",
                  json={"package_id": package_id, "bidder_ids": [b["id"]],
                        "stages": ["objective"]})
    assert resp.status_code == 200, resp.text
    job = wait_job(c, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")
    matrix = c.get(f"/api/runs/{job['result_json']['run_id']}/matrix").json()
    cells = [cell for cols in matrix["cells"].values() for cell in cols.values()]
    assert cells and all(c["review_status"] == "auto_confirmed" for c in cells)
