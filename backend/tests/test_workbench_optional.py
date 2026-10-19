"""工作台阶段：无招标文件但已有规则包时，招标文件是可选阶段。"""
from __future__ import annotations


def _stages(client, pid: str) -> dict[str, dict]:  # noqa: ANN001
    data = client.get(f"/api/projects/{pid}/workbench").json()
    return {s["key"]: s for s in data["stages"]}


def test_tender_is_optional_when_default_rules_frozen(client) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "无招标文件默认规则"}).json()["id"]
    r = client.post(
        f"/api/projects/{pid}/rules/use-default-template",
        json={"template_id": "comprehensive-goods", "reason": "无招标文件，启用默认规则"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["frozen"] is True

    stages = _stages(client, pid)
    assert stages["rules"]["state"] == "done"
    assert stages["tender"]["state"] == "pending"
    assert stages["tender"]["optional"] is True
    assert "未上传" in stages["tender"]["detail"]


def test_tender_is_required_without_rules(client) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "空项目"}).json()["id"]
    stages = _stages(client, pid)
    assert stages["tender"]["state"] == "pending"
    assert stages["tender"]["optional"] is False
