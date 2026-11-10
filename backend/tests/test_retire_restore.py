"""逐条启用/停用：retire / restore 的服务语义、总分联动与覆盖率口径。"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select


def _project_with_template(client) -> tuple[str, int]:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "停用恢复测试"}).json()["id"]
    package_id = client.post(
        f"/api/projects/{pid}/rules/instantiate",
        json={"template_id": "comprehensive-goods"},
    ).json()["package_id"]
    client.post(
        f"/api/rules/packages/{package_id}/signoff-all",
        json={"reason": "测试基线启用"},
    )
    return pid, package_id


def _criteria(client, package_id: int) -> list[dict]:  # noqa: ANN001
    return client.get(f"/api/rules/packages/{package_id}").json()["criteria"]


def _error_codes(client, package_id: int) -> set[str]:  # noqa: ANN001
    vs = client.get(f"/api/rules/packages/{package_id}/validations").json()
    return {v["code"] for v in vs if v["severity"] == "error"}


def test_retire_scoring_item_breaks_total_and_restore_fixes(client) -> None:  # noqa: ANN001
    pid, package_id = _project_with_template(client)
    crit = next(
        c for c in _criteria(client, package_id)
        if not c["is_veto"] and c["max_score"] is not None
    )

    r = client.post(
        f"/api/rules/criteria/{crit['id']}/retire",
        json={"reason": "本项目不启用该判据"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "retired"
    assert "TOTAL_SCORE" in _error_codes(client, package_id)

    r = client.post(
        f"/api/rules/criteria/{crit['id']}/restore",
        json={"reason": "恢复该判据"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] != "retired"
    assert "TOTAL_SCORE" not in _error_codes(client, package_id)


def test_retire_veto_item_freezes_and_snapshot_excludes_it(client) -> None:  # noqa: ANN001
    """否决项无分值：停用不破坏总分，可正常确认冻结，且不进快照。"""
    from app.db.models import RulePackage
    from app.db.session import session_scope

    pid, package_id = _project_with_template(client)
    crit = next(c for c in _criteria(client, package_id) if c["is_veto"])

    r = client.post(
        f"/api/rules/criteria/{crit['id']}/retire",
        json={"reason": "本项目不适用该否决条款"},
    )
    assert r.status_code == 200, r.text

    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/freeze").status_code == 200

    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        codes = {c["code"] for c in pkg.snapshot_json["criteria"]}
    assert crit["code"] not in codes


def test_retire_on_frozen_package_conflicts(client) -> None:  # noqa: ANN001
    pid, package_id = _project_with_template(client)
    crit = next(c for c in _criteria(client, package_id) if c["is_veto"])
    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/freeze").status_code == 200

    r = client.post(
        f"/api/rules/criteria/{crit['id']}/retire",
        json={"reason": "冻结后尝试停用"},
    )
    assert r.status_code == 409, r.text


def test_retired_manual_item_counts_in_coverage(client) -> None:  # noqa: ANN001
    """回归：document/manual 来源的 retired 也必须计入 coverage.retired。

    旧实现只在 law/template 分支判 retired，manual 停用项会被算成 manual。
    """
    pid, package_id = _project_with_template(client)
    r = client.post(
        f"/api/rules/packages/{package_id}/criteria",
        json={
            "category": "technical",
            "name": "人工新增测试项",
            "score_type": "match",
            "max_score": None,
            "reason": "覆盖率口径测试",
        },
    )
    assert r.status_code == 200, r.text
    crit_id = r.json()["id"]

    before = client.get(f"/api/rules/packages/{package_id}/coverage").json()
    assert before["manual"] == 1 and before["retired"] == 0

    assert client.post(
        f"/api/rules/criteria/{crit_id}/retire",
        json={"reason": "停用人工项"},
    ).status_code == 200

    after = client.get(f"/api/rules/packages/{package_id}/coverage").json()
    assert after["retired"] == 1
    assert after["manual"] == 0
