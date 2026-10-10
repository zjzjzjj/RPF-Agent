"""M0 验收：建表、健康检查、项目 CRUD、任务队列基本语义。"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect

from app.db.session import get_engine

EXPECTED_TABLES = {
    "projects",
    "documents",
    "document_pages",
    "doc_nodes",
    "rule_packages",
    "criteria",
    "criterion_levels",
    "criterion_checks",
    "price_formulas",
    "package_validations",
    "bidders",
    "bidder_documents",
    "evaluation_runs",
    "score_records",
    "evidence_citations",
    "review_records",
    "audit_logs",
    "jobs",
}


def test_all_tables_created(app_env):
    names = set(inspect(get_engine()).get_table_names())
    missing = EXPECTED_TABLES - names
    assert not missing, f"缺少表：{missing}"


def test_foreign_keys_enforced(app_env):
    """PRAGMA foreign_keys=ON 必须真实生效。"""
    from sqlalchemy import text

    with get_engine().connect() as conn:
        value = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert value == 1


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["llm_provider"] == "mock"


def test_project_crud(client):
    resp = client.post(
        "/api/projects",
        json={"name": "某市政务云平台建设项目", "purchaser": "某市大数据局", "max_price": "8000000.00"},
        headers={"X-Actor": "zhang-reviewer"},
    )
    assert resp.status_code == 200
    project = resp.json()
    assert project["id"].startswith("PRJ-")
    assert project["status"] == "draft"

    pid = project["id"]
    resp = client.get(f"/api/projects/{pid}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "某市政务云平台建设项目"

    resp = client.patch(f"/api/projects/{pid}", json={"status": "tender_parsed"})
    assert resp.json()["status"] == "tender_parsed"

    resp = client.get("/api/projects")
    assert len(resp.json()) == 1

    # 审计留痕已写入
    resp = client.get(f"/api/projects/{pid}/workbench")
    assert resp.status_code == 200
    stages = {s["key"]: s for s in resp.json()["stages"]}
    assert stages["tender"]["state"] == "pending"


def test_project_not_found(client):
    resp = client.get("/api/projects/PRJ-19700101-XXXX")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


@pytest.mark.anyio
async def test_job_queue_runs_handler(app_env):
    """任务队列：提交 → worker 领取 → 处理器执行 → 结果落库。"""
    from app.db.models import Job
    from app.db.session import session_scope
    from app.jobs import handlers, queue

    @handlers.register("test_noop")
    def _noop(ctx, payload):  # noqa: ANN001
        ctx.start(total=2, message="步骤一")
        ctx.advance(message="步骤二")
        ctx.advance(message="步骤三")
        return {"echo": payload.get("x")}

    job_id = queue.submit("test_noop", {"x": 42}, project_id=None)
    await queue.start_worker()
    try:
        import asyncio

        for _ in range(50):
            await asyncio.sleep(0.1)
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job is not None and job.status in ("succeeded", "failed"):
                    break
        with session_scope() as s:
            job = s.get(Job, job_id)
            assert job.status == "succeeded", job.error
            assert job.result_json == {"echo": 42}
            assert job.done_steps == 2
    finally:
        await queue.stop_worker()
