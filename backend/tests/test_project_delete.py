"""项目删除：依赖序清理（文档/规则/投标人/评分全部连带），审计保留。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .conftest import upload_pdf, wait_job


def _db_counts(db_path: Path) -> dict[str, int]:
    con = sqlite3.connect(str(db_path))
    try:
        tables = [
            "projects", "documents", "document_pages", "doc_nodes", "bidders",
            "rule_packages", "criteria", "criterion_levels", "criterion_checks",
            "override_records", "evaluation_runs", "score_records",
            "evidence_citations",
        ]
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        con.close()


def test_delete_project_cascades_everything(client, app_env, samples_dir) -> None:  # noqa: ANN001
    pid = client.post("/api/projects",
                      json={"name": "待删除项目", "max_price": "8000000"}).json()["id"]

    # 铺全链数据：模板实例化（产生 rule_packages/criteria）+ 招标文件（documents/pages/nodes）
    r = client.post(f"/api/projects/{pid}/rules/instantiate",
                    json={"template_id": "comprehensive-goods"})
    assert r.status_code == 200, r.text
    tender_pdf = next((samples_dir / "tender").glob("*招标文件.pdf"))
    out = upload_pdf(client, pid, tender_pdf, "tender")
    assert wait_job(client, out["job_id"])["status"] == "succeeded"
    b = client.post(f"/api/projects/{pid}/bidders", json={"name": "测试投标人"}).json()

    before = _db_counts(app_env / "test.db")
    assert before["projects"] == 1 and before["criteria"] > 0 and before["documents"] == 1

    # ---- 删除 ----
    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 200, r.text
    counts = r.json()
    assert counts["projects"] == 1
    assert counts["documents"] == 1
    assert counts["rule_packages"] == 1
    assert counts["criteria"] == before["criteria"]

    # ---- 全空 + 无孤儿 ----
    after = _db_counts(app_env / "test.db")
    assert all(v == 0 for v in after.values()), after

    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert client.delete(f"/api/projects/{pid}").status_code == 404  # 幂等语义：再删 404

    # ---- 审计保留（删除动作本身留痕）----
    con = sqlite3.connect(str(app_env / "test.db"))
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = 'project.delete'"
        ).fetchone()[0]
    finally:
        con.close()
    assert n >= 1


def test_delete_project_with_documents_only(client, app_env, samples_dir) -> None:  # noqa: ANN001
    """只有文档没有规则包的项目也能删（覆盖无 rule_packages 的分支）。"""
    pid = client.post("/api/projects", json={"name": "仅文档项目"}).json()["id"]
    tender_pdf = next((samples_dir / "tender").glob("*招标文件.pdf"))
    out = upload_pdf(client, pid, tender_pdf, "tender")
    assert wait_job(client, out["job_id"])["status"] == "succeeded"

    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 200, r.text
    assert r.json()["documents"] == 1
    after = _db_counts(app_env / "test.db")
    assert after["projects"] == 0 and after["documents"] == 0
