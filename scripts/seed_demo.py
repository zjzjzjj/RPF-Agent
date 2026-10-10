"""在**真实数据目录**里铺一套演示数据（与 run_e2e 相同的链路，但落库到 data/bid_eval.db）。

用法：python scripts/seed_demo.py
之后 uvicorn + 前端打开即可看到完整演示项目。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

BIDDER_KEYS = ["jia", "yi", "bing", "ding", "wu"]


def _wait(client, job_id: int, timeout: float = 300.0) -> dict:  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.2)
    raise TimeoutError(f"任务 {job_id} 超时")


def main() -> None:
    from fastapi.testclient import TestClient

    from app.db import session as db
    from app.main import create_app

    db.reset_engine()
    db.init_db()

    samples = REPO / "data" / "samples"
    tender_pdf = next((samples / "tender").glob("*招标文件.pdf"), None)
    if tender_pdf is None:
        import subprocess

        subprocess.run([sys.executable, str(REPO / "scripts" / "generate_sample_tender.py")], check=True)
        subprocess.run([sys.executable, str(REPO / "scripts" / "generate_sample_bids.py")], check=True)
        tender_pdf = next((samples / "tender").glob("*招标文件.pdf"))

    def upload(client, pid, path, kind, bidder_id=None):  # noqa: ANN001
        data = {"kind": kind}
        if bidder_id is not None:
            data["bidder_id"] = str(bidder_id)
        resp = client.post(
            f"/api/projects/{pid}/documents",
            files={"file": (path.name, path.read_bytes(), "application/pdf")},
            data=data,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    with TestClient(create_app()) as client:
        # 若已有演示项目则跳过，避免重复铺数
        existing = client.get("/api/projects").json()
        if any(p["name"] == "某市智慧政务云平台建设项目（演示）" for p in existing):
            print("演示项目已存在，跳过。")
            return

        pid = client.post("/api/projects", json={
            "name": "某市智慧政务云平台建设项目（演示）",
            "project_code": "ZJZB-2026-0117",
            "purchaser": "某市大数据发展管理局",
            "max_price": "8000000",
        }).json()["id"]
        print(f"项目：{pid}")

        out = upload(client, pid, tender_pdf, "tender")
        job = _wait(client, out["job_id"])
        assert job["status"] == "succeeded", job.get("error")

        resp = client.post(f"/api/projects/{pid}/rules/compile",
                           json={"document_id": out["document_id"], "provider": "mock"})
        job = _wait(client, resp.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        package_id = job["result_json"]["package_id"]

        pkg = client.get(f"/api/rules/packages/{package_id}").json()
        for c in pkg["criteria"]:
            r = client.post(f"/api/rules/criteria/{c['id']}/accept", json={"reason": "与招标文件原文核对一致"})
            assert r.status_code == 200, r.text
        client.post(f"/api/rules/packages/{package_id}/confirm")
        client.post(f"/api/rules/packages/{package_id}/freeze")
        print(f"规则包 v1 已冻结：{len(pkg['criteria'])} 个评分项")

        bid_pdfs = sorted((samples / "bids").glob("*_投标文件.pdf"))
        bidder_ids = []
        for key in BIDDER_KEYS:
            pdf = next(p for p in bid_pdfs if p.name.startswith(f"{key}_"))
            b = client.post(f"/api/projects/{pid}/bidders",
                            json={"name": pdf.stem.split("_")[1]}).json()
            bo = upload(client, pid, pdf, "bid", bidder_id=b["id"])
            jb = _wait(client, bo["job_id"])
            assert jb["status"] == "succeeded", jb.get("error")
            client.get(f"/api/bidders/{b['id']}/price")
            client.post(f"/api/bidders/{b['id']}/price/confirm", json={"reason": "与开标一览表核对一致"})
            bidder_ids.append(b["id"])
        print(f"5 家投标人已就绪：{bidder_ids}")

        resp = client.post(f"/api/projects/{pid}/runs", json={
            "package_id": package_id,
            "bidder_ids": bidder_ids,
            "stages": ["objective", "price", "subjective"],
        })
        job = _wait(client, resp.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        print(f"评分运行完成：run #{resp.json()['run_id']}")
        print("演示数据就绪。")

    print(f"数据库：{REPO / 'data' / 'bid_eval.db'}")


if __name__ == "__main__":
    main()
