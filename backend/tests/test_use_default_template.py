"""一键启用默认规则（无招标文件直接评分）：实例化 → 全部签批 → 确认 → 冻结。"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select


def _make_project(client) -> str:  # noqa: ANN001
    return client.post("/api/projects", json={"name": "默认规则项目"}).json()["id"]


def _use_default(client, pid: str, reason: str | None = "无招标文件，启用默认规则"):
    body = {"template_id": "comprehensive-goods"}
    if reason is not None:
        body["reason"] = reason
    return client.post(f"/api/projects/{pid}/rules/use-default-template", json=body)


def test_use_default_template_manual_mode(client) -> None:  # noqa: ANN001
    """人工模式（conftest 默认 auto=0）：带理由 → 实例化+签批+确认+冻结 一次完成。"""
    pid = _make_project(client)
    r = _use_default(client, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["frozen"] is True
    assert body["created"] is True
    assert body["already_frozen"] is False
    assert body["signed_off"] > 0

    pkg = client.get(f"/api/projects/{pid}/rules").json()
    assert pkg["id"] == body["package_id"]
    assert pkg["status"] == "frozen"
    assert pkg["origin"] == "template"
    assert pkg["content_hash"]


def test_use_default_template_requires_reason_in_manual_mode(client) -> None:  # noqa: ANN001
    pid = _make_project(client)
    r = _use_default(client, pid, reason=None)
    assert r.status_code == 422, r.text


def test_use_default_template_is_idempotent_when_frozen(client) -> None:  # noqa: ANN001
    pid = _make_project(client)
    first = _use_default(client, pid).json()
    second = _use_default(client, pid).json()
    assert second["ok"] is True
    assert second["already_frozen"] is True
    assert second["package_id"] == first["package_id"]


def test_use_default_template_reuses_draft_template_package(client) -> None:  # noqa: ANN001
    """建项目时已选模板（draft）→ 直接复用并启用，不再实例化（否则会 409）。"""
    pid = _make_project(client)
    package_id = client.post(
        f"/api/projects/{pid}/rules/instantiate",
        json={"template_id": "comprehensive-goods"},
    ).json()["package_id"]

    r = _use_default(client, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["package_id"] == package_id
    assert body["created"] is False
    assert body["frozen"] is True


def test_use_default_template_rejects_non_template_package(client) -> None:  # noqa: ANN001
    """已先编译出 document 包时，默认规则入口必须拒绝（README 的顺序约束）。"""
    from app.db.models import RulePackage
    from app.db.session import session_scope

    pid = _make_project(client)
    with session_scope() as s:
        s.add(RulePackage(project_id=pid, version=1, status="draft", origin="document"))

    r = _use_default(client, pid)
    assert r.status_code == 409, r.text


def test_use_default_template_reports_blockers_with_package_id(client) -> None:  # noqa: ANN001
    """总分被破坏时返回 ok=false + blockers，且保留 package_id 供前端修正。"""
    from app.db.models import Criterion
    from app.db.session import session_scope

    pid = _make_project(client)
    package_id = client.post(
        f"/api/projects/{pid}/rules/instantiate",
        json={"template_id": "comprehensive-goods"},
    ).json()["package_id"]

    with session_scope() as s:
        crit = s.execute(
            select(Criterion).where(
                Criterion.package_id == package_id,
                Criterion.is_veto.is_(False),
                Criterion.max_score.is_not(None),
            )
        ).scalars().first()
        assert crit is not None
        crit.max_score = Decimal("1")

    r = _use_default(client, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["frozen"] is False
    assert body["package_id"] == package_id
    codes = {b.get("code") for b in body["blockers"]}
    assert "TOTAL_SCORE" in codes, body["blockers"]
