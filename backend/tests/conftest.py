"""测试夹具：每个测试用独立的临时数据目录与 SQLite 库，全程离线。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    """样例目录；缺失时自动生成（确定性、可重复）。"""
    d = REPO_ROOT / "data" / "samples"
    tender = list((d / "tender").glob("*招标文件.pdf")) if (d / "tender").exists() else []
    bids = list((d / "bids").glob("*投标文件.pdf")) if (d / "bids").exists() else []
    if not tender or len(bids) < 5:
        subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "generate_sample_tender.py")],
                       check=True, capture_output=True)
        subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "generate_sample_bids.py")],
                       check=True, capture_output=True)
    return d


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """隔离环境：临时 data 目录 + 全新引擎。

    存量测试默认走**人工回退模式**（BID_EVAL_AUTO_MODE=0）——它们守护的正是
    三态/复核队列那套回退路径的语义。全自动语义由显式开启 auto 的用例覆盖
    （test_auto_pipeline.py 等，设 BID_EVAL_AUTO_MODE=1）。
    """
    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BID_EVAL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BID_EVAL_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("BID_EVAL_RENDER_DIR", str(tmp_path / "renders"))
    monkeypatch.setenv("BID_EVAL_LLM_PROVIDER", "mock")
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "0")

    from app.db import session as db_session

    db_session.reset_engine()
    db_session.init_db()
    yield tmp_path
    db_session.reset_engine()


@pytest.fixture()
def client(app_env):  # noqa: ANN001
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def wait_job(client, job_id: int, timeout: float = 120.0) -> dict:  # noqa: ANN001
    """轮询任务直至终态。"""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        job = resp.json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.2)
    raise TimeoutError(f"任务 {job_id} 超时未完成")


def upload_pdf(client, project_id: str, path: Path, kind: str, bidder_id: int | None = None) -> dict:  # noqa: ANN001
    data = {"kind": kind}
    if bidder_id is not None:
        data["bidder_id"] = str(bidder_id)
    with open(path, "rb") as fh:
        resp = client.post(
            f"/api/projects/{project_id}/documents",
            files={"file": (path.name, fh, "application/pdf")},
            data=data,
        )
    assert resp.status_code == 200, resp.text
    return resp.json()
