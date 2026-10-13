"""全自动合并：模板 → 上传招标文件 → merge 编译 → 配对自动采纳（auto 模式）。

- 编译任务内嵌自动 resolve：auto_resolved=True、模板包 origin 升级 merged、
  OverrideRecord 留痕、编译不因合并失败而失败；
- 人工回退模式（conftest 默认）不走自动 resolve，提案进抽屉（既有 test_merge 覆盖）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from .conftest import samples_dir, upload_pdf, wait_job
from .test_auto_pipeline import auto_client  # noqa: F401  （fixture 复用）
from app.rules import law_kb as lk
from app.rules.library.law_pack_loader import load_law_pack, reset_law_pack_cache

REPO = Path(__file__).resolve().parents[2]
REPO_KB = REPO / "data" / "law_kb.json"


@pytest.fixture()
def auto_env(app_env, monkeypatch):  # noqa: ANN001
    """app_env 之上显式开启全自动模式并指向仓库知识库。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    monkeypatch.setenv("BID_EVAL_LAW_KB_PATH", str(REPO_KB))
    lk.reset_cache()
    reset_law_pack_cache()
    yield app_env
    lk.reset_cache()
    reset_law_pack_cache()


def test_template_upload_auto_merge(auto_env, auto_client, samples_dir) -> None:  # noqa: ANN001
    c = auto_client
    pid = c.post("/api/projects", json={"name": "模板自动合并"}).json()["id"]
    template_pkg = _instantiate(c, pid)

    # 上传招标文件（模板包存在时上传入口已放开）
    tender = next((samples_dir / "tender").glob("*招标文件.pdf"))
    out = upload_pdf(c, pid, tender, "tender")
    assert wait_job(c, out["job_id"])["status"] == "succeeded"

    # merge 编译 → 后端自动采纳配对
    resp = c.post(f"/api/projects/{pid}/rules/compile", json={
        "document_id": out["document_id"],
        "mode": "merge",
        "merge_target_id": template_pkg,
    })
    assert resp.status_code == 200, resp.text
    job = wait_job(c, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")
    result = job["result_json"]
    assert result["auto_resolved"] is True, result
    target_id = result["target_package_id"]

    # 模板包 origin 升级 merged；被覆盖条目 origin=document
    pkg = c.get(f"/api/rules/packages/{target_id}").json()
    assert pkg["origin"] == "merged"
    overridden = [x for x in pkg["criteria"] if x["origin"] == "document"]
    assert overridden, "应存在被文档条款覆盖的条目"

    # 留痕：OverrideRecord > 0
    con = sqlite3.connect(str(auto_env / "test.db"))
    try:
        n = con.execute("SELECT COUNT(*) FROM override_records").fetchone()[0]
    finally:
        con.close()
    assert n > 0


def _instantiate(c, pid: str) -> int:  # noqa: ANN001
    r = c.post(f"/api/projects/{pid}/rules/instantiate",
               json={"template_id": "comprehensive-goods"})
    assert r.status_code == 200, r.text
    return r.json()["package_id"]


def test_auto_resolve_failure_is_best_effort(auto_env, auto_client, samples_dir) -> None:  # noqa: ANN001
    """模板包已 confirmed 时自动 resolve 失败 → 编译仍成功，原因进 result_json。"""
    c = auto_client
    pid = c.post("/api/projects", json={"name": "合并失败路径"}).json()["id"]
    template_pkg = _instantiate(c, pid)
    # 先人工确认模板包（制造 _require_draft_pkg 的 422 条件）
    assert c.post(f"/api/rules/packages/{template_pkg}/confirm").status_code == 200

    tender = next((samples_dir / "tender").glob("*招标文件.pdf"))
    out = upload_pdf(c, pid, tender, "tender")
    assert wait_job(c, out["job_id"])["status"] == "succeeded"

    resp = c.post(f"/api/projects/{pid}/rules/compile", json={
        "document_id": out["document_id"],
        "mode": "merge",
        "merge_target_id": template_pkg,
    })
    assert resp.status_code == 200, resp.text
    job = wait_job(c, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")  # 编译本身不失败
    result = job["result_json"]
    assert result["auto_resolved"] is False
    assert result.get("auto_resolve_error")
