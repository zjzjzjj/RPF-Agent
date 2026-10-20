"""工作台三阶段（规则库与招标文件 / 投标人 / 评分结果）。

- 无招标文件但默认规则已冻结 → rules 阶段直接 done（招标文件可选，optional=True）；
- 无招标文件且无规则包 → rules 阶段 pending；
- 招标文件解析失败优先于一切规则状态：即使包已冻结也显示 blocked（失败必须可见）。
"""
from __future__ import annotations

from .conftest import upload_pdf, wait_job


def _stages(client, pid: str) -> dict[str, dict]:  # noqa: ANN001
    data = client.get(f"/api/projects/{pid}/workbench").json()
    return {s["key"]: s for s in data["stages"]}


def test_no_tender_is_optional_when_default_rules_frozen(client) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "无招标文件默认规则"}).json()["id"]
    r = client.post(
        f"/api/projects/{pid}/rules/use-default-template",
        json={"template_id": "comprehensive-goods", "reason": "无招标文件，启用默认规则"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["frozen"] is True

    data = client.get(f"/api/projects/{pid}/workbench").json()
    assert [s["key"] for s in data["stages"]] == ["rules", "bidders", "results"]
    stages = {s["key"]: s for s in data["stages"]}
    assert stages["rules"]["state"] == "done"
    assert stages["rules"]["optional"] is True
    assert "未上传" in stages["rules"]["detail"]
    assert stages["bidders"]["state"] == "pending"
    assert stages["results"]["state"] == "pending"


def test_rules_pending_without_tender_and_package(client) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "空项目"}).json()["id"]
    stages = _stages(client, pid)
    assert len(stages) == 3
    assert stages["rules"]["state"] == "pending"
    assert stages["rules"]["optional"] is False
    assert "未上传" in stages["rules"]["detail"]


def test_failed_tender_parse_beats_frozen_package(client, tmp_path) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "解析失败优先"}).json()["id"]
    r = client.post(
        f"/api/projects/{pid}/rules/use-default-template",
        json={"template_id": "comprehensive-goods", "reason": "先启用默认规则"},
    )
    assert r.status_code == 200 and r.json()["frozen"] is True

    # 损坏"PDF"：解析任务正常结束，文档标记 failed（NOT_A_PDF）
    bad = tmp_path / "broken.pdf"
    bad.write_text("这不是 PDF", encoding="utf-8")
    out = upload_pdf(client, pid, bad, "tender")
    wait_job(client, out["job_id"])

    data = client.get(f"/api/projects/{pid}/workbench").json()
    stages = {s["key"]: s for s in data["stages"]}
    assert stages["rules"]["state"] == "blocked"
    assert stages["rules"]["todo_count"] == 1
    assert "解析被拒绝" in stages["rules"]["detail"]
    assert any("解析被拒绝" in b for b in data["blockers"])
