"""解冻并创建新版本（new_version 收紧 + origin 溯源复制）。"""
from __future__ import annotations

from .conftest import samples_dir  # noqa: F401
from .test_template_instantiate import _instantiate


def test_frozen_package_creates_new_draft(client, app_env, samples_dir) -> None:  # noqa: ANN001
    pdi = client.post("/api/projects", json={"name": "解冻演示"}).json()["id"]
    package_id = _instantiate(client, pdi)
    assert client.post(f"/api/rules/packages/{package_id}/signoff-all",
                       json={"reason": "批量签批"}).status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    frozen = client.post(f"/api/rules/packages/{package_id}/freeze").json()

    # 冻结态直接编辑 → 409（既有护栏，确认仍守）
    crit = client.get(f"/api/rules/packages/{package_id}").json()["criteria"][0]
    r = client.patch(f"/api/rules/criteria/{crit['id']}",
                     json={"name": "篡改", "reason": "试试"})
    assert r.status_code == 409

    # 前置校验（draft/confirmed/superseded 不能派生）见 test_new_version_rejects_draft_and_confirmed

    # ---- 解冻并创建新版本（frozen → 新草稿，合法）----
    nv = client.post(f"/api/rules/packages/{package_id}/versions",
                     json={"reason": "解冻修改评分细则"}).json()
    assert nv["status"] == "draft"
    assert nv["version"] == frozen["version"] + 1
    assert nv["parent_version_id"] == package_id
    # 条目数量一致（全字段复制）
    nv_full = client.get(f"/api/rules/packages/{nv['id']}").json()
    frozen_full = client.get(f"/api/rules/packages/{package_id}").json()
    assert len(nv_full["criteria"]) == len(frozen_full["criteria"])

    # 旧包保持 frozen（评分流水自持）；新草稿可编辑
    old = client.get(f"/api/rules/packages/{package_id}").json()
    assert old["status"] == "frozen"
    crit2 = nv_full["criteria"][0]
    r = client.patch(f"/api/rules/criteria/{crit2['id']}",
                     json={"name": "修改后", "reason": "解冻后的合法编辑"})
    assert r.status_code == 200, r.text

    # 新草稿冻结 → 旧包变 superseded，current 切新包
    assert client.post(f"/api/rules/packages/{nv['id']}/confirm").status_code == 200
    assert client.post(f"/api/rules/packages/{nv['id']}/freeze").status_code == 200
    old = client.get(f"/api/rules/packages/{package_id}").json()
    assert old["status"] == "superseded"


def test_new_version_rejects_draft_and_confirmed(client, app_env) -> None:  # noqa: ANN001
    pdi = client.post("/api/projects", json={"name": "前置校验"}).json()["id"]
    package_id = _instantiate(client, pdi)

    # draft → 409（草稿请直接编辑）
    r = client.post(f"/api/rules/packages/{package_id}/versions", json={"reason": "试试"})
    assert r.status_code == 409, r.text
    assert "直接编辑" in r.json()["message"]

    # confirmed → 409（请先冻结）
    assert client.post(f"/api/rules/packages/{package_id}/signoff-all",
                       json={"reason": "签批"}).status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    r = client.post(f"/api/rules/packages/{package_id}/versions", json={"reason": "试试"})
    assert r.status_code == 409, r.text
    assert "冻结" in r.json()["message"]

    # 空理由 → 422
    assert client.post(f"/api/rules/packages/{package_id}/freeze").status_code == 200
    r = client.post(f"/api/rules/packages/{package_id}/versions", json={"reason": "  "})
    assert r.status_code == 422, r.text


def test_new_version_preserves_template_origin(client, app_env) -> None:  # noqa: ANN001
    """origin 溯源修复钉死：模板包派生的新版本 origin 仍是 template（旧 bug：退化 document）。"""
    pdi = client.post("/api/projects", json={"name": "模板解冻"}).json()["id"]
    package_id = _instantiate(client, pdi)  # origin == "template"
    assert client.post(f"/api/rules/packages/{package_id}/signoff-all",
                       json={"reason": "签批"}).status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/freeze").status_code == 200

    nv = client.post(f"/api/rules/packages/{package_id}/versions",
                     json={"reason": "解冻"}).json()
    assert nv["origin"] == "template", nv["origin"]
    assert nv["template_id"] is not None
    assert nv["library_version"] is not None
