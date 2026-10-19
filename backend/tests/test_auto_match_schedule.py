"""上传招标文件后自动匹配的调度护栏（不执行真实编译，不依赖 LLM）。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import config


def _make_project(client) -> str:  # noqa: ANN001
    return client.post("/api/projects", json={"name": "自动匹配调度测试"}).json()["id"]


def _make_tender_doc(pid: str, parse_status: str = "parsed") -> int:
    from app.db.models import Document
    from app.db.session import session_scope

    with session_scope() as s:
        doc = Document(
            project_id=pid,
            kind="tender",
            original_filename="tender.pdf",
            stored_path="",
            sha256="x" * 64,
            parse_status=parse_status,
            data_class="synthetic",
        )
        s.add(doc)
        s.flush()
        return doc.id


def _jobs():
    from app.db.models import Job
    from app.db.session import session_scope

    with session_scope() as s:
        return list(s.execute(select(Job)).scalars())


def test_skips_when_llm_not_configured(client, monkeypatch) -> None:  # noqa: ANN001
    """离线/未配模型：解析成功也不排编译任务（pytest 全绿的关键护栏）。"""
    from app.services.auto_merge_service import maybe_schedule_auto_match

    pid = _make_project(client)
    doc_id = _make_tender_doc(pid)
    monkeypatch.setattr(config, "llm_api_key_configured", lambda: False)

    out = maybe_schedule_auto_match(pid, doc_id)
    assert out["auto_match"] == "skipped"
    assert out["skip_reason"] == "llm_not_configured"
    assert _jobs() == []


def test_skips_when_switch_off(client, monkeypatch) -> None:  # noqa: ANN001
    from app.services.auto_merge_service import maybe_schedule_auto_match

    pid = _make_project(client)
    doc_id = _make_tender_doc(pid)
    monkeypatch.setattr(config, "llm_api_key_configured", lambda: True)
    monkeypatch.setattr(config, "auto_match_enabled", lambda: False)

    out = maybe_schedule_auto_match(pid, doc_id)
    assert out["skip_reason"] == "auto_match_disabled"
    assert _jobs() == []


def test_schedules_merge_for_draft_template_package(client, monkeypatch) -> None:  # noqa: ANN001
    from app.services.auto_merge_service import maybe_schedule_auto_match

    pid = _make_project(client)
    package_id = client.post(
        f"/api/projects/{pid}/rules/instantiate",
        json={"template_id": "comprehensive-goods"},
    ).json()["package_id"]
    doc_id = _make_tender_doc(pid)
    monkeypatch.setattr(config, "llm_api_key_configured", lambda: True)

    out = maybe_schedule_auto_match(pid, doc_id)
    assert out["auto_match"] == "scheduled"
    assert out["mode"] == "merge"
    assert out["merge_target_id"] == package_id

    jobs = _jobs()
    assert len(jobs) == 1
    assert jobs[0].kind == "compile_rules"
    assert jobs[0].payload_json["mode"] == "merge"
    assert jobs[0].payload_json["merge_target_id"] == package_id


def test_schedules_replace_when_project_has_no_package(client, monkeypatch) -> None:  # noqa: ANN001
    from app.services.auto_merge_service import maybe_schedule_auto_match

    pid = _make_project(client)
    doc_id = _make_tender_doc(pid)
    monkeypatch.setattr(config, "llm_api_key_configured", lambda: True)

    out = maybe_schedule_auto_match(pid, doc_id)
    assert out["auto_match"] == "scheduled"
    assert out["mode"] == "replace"
    assert out["merge_target_id"] is None


def test_skips_when_package_not_draft(client, monkeypatch) -> None:  # noqa: ANN001
    from app.services.auto_merge_service import maybe_schedule_auto_match

    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/rules/use-default-template",
        json={"template_id": "comprehensive-goods", "reason": "先冻结一个默认包"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["frozen"] is True
    doc_id = _make_tender_doc(pid)
    monkeypatch.setattr(config, "llm_api_key_configured", lambda: True)

    out = maybe_schedule_auto_match(pid, doc_id)
    assert out["auto_match"] == "skipped"
    assert out["skip_reason"] == "package_not_draft"


def test_skips_already_in_flight(client, monkeypatch) -> None:  # noqa: ANN001
    from app.services.auto_merge_service import maybe_schedule_auto_match

    pid = _make_project(client)
    package_id = client.post(
        f"/api/projects/{pid}/rules/instantiate",
        json={"template_id": "comprehensive-goods"},
    ).json()["package_id"]
    doc_id = _make_tender_doc(pid)
    monkeypatch.setattr(config, "llm_api_key_configured", lambda: True)

    first = maybe_schedule_auto_match(pid, doc_id)
    assert first["auto_match"] == "scheduled"
    second = maybe_schedule_auto_match(pid, doc_id)
    assert second["auto_match"] == "skipped"
    assert second["skip_reason"] == "already_in_flight"
    assert len(_jobs()) == 1
