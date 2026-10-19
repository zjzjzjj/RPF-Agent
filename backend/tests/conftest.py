"""测试夹具：每个测试用独立的临时数据目录与 SQLite 库，全程离线。

密闭性（重要）：仓库 .env 在导入 app.config 时以 setdefault 注入环境变量
（含 BID_EVAL_LLM_PROVIDER/KEY）。因此 conftest 顶部**先导入 app.config**
触发 .env 加载，再在 app_env 里 delenv 全部 LLM 变量——测试进程内 provider
恒为未配置，任何意外的 LLM 调用都会立即抛 LLM_NOT_CONFIGURED（中文可操作
报错），绝不会静默摸到真实 API。顶部的 config 导入是承重的，不可删。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app import config as _config  # noqa: F401  承重：先触发 .env 注入，app_env 的 delenv 才有效

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
    """隔离环境：临时 data 目录 + 全新引擎 + 零 LLM 配置（全程离线）。

    存量测试默认走**人工回退模式**（BID_EVAL_AUTO_MODE=0）——它们守护的正是
    三态/复核队列那套回退路径的语义。全自动语义由显式开启 auto 的用例覆盖
    （test_auto_pipeline.py 等，设 BID_EVAL_AUTO_MODE=1）。
    """
    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BID_EVAL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BID_EVAL_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("BID_EVAL_RENDER_DIR", str(tmp_path / "renders"))
    # 清掉所有 LLM 配置（含 .env 注入的）：未配置即不可用，意外调用立即报错
    monkeypatch.delenv("BID_EVAL_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("BID_EVAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BID_EVAL_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "0")

    from app.db import session as db_session
    from app.llm.gateway import reset_gateway

    reset_gateway()  # 清掉进程级单例（上一测试可能留下的网关与缓存）
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
