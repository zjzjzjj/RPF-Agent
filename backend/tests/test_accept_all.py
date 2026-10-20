"""全部采纳 / 全部签批（人工回退模式）。

实例化 → 全部签批 → 全部采纳 → confirm/freeze 走通；
非草稿包 409；空理由 422；ReviewRecord 逐条留痕。
"""
from __future__ import annotations

import sqlite3

from .conftest import samples_dir  # noqa: F401
from .test_template_instantiate import _instantiate


def _audit_count(db_path: Path, action: str) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = ?", (action,)
        ).fetchone()[0]
    finally:
        con.close()


def test_signoff_all_then_accept_all_then_freeze(client, app_env) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "批量采纳演示"}).json()["id"]
    package_id = _instantiate(client, pid)

    # 空理由 → 422
    r = client.post(f"/api/rules/packages/{package_id}/signoff-all", json={"reason": "  "})
    assert r.status_code == 422, r.text

    # 全部签批：needs_signoff → verified，signoff_json 齐全
    r = client.post(f"/api/rules/packages/{package_id}/signoff-all",
                    json={"reason": "批量签批：按通用评标惯例启用模板判据"})
    assert r.status_code == 200, r.text
    signed = r.json()["signed_off"]
    assert signed > 0

    pkg = client.get(f"/api/rules/packages/{package_id}").json()
    assert all(c["status"] == "verified" for c in pkg["criteria"])
    assert all(c["signoff_json"] for c in pkg["criteria"] if c["origin"] == "template")

    # 全部采纳：verified 条目逐条 accept 留痕
    r = client.post(f"/api/rules/packages/{package_id}/accept-all",
                    json={"reason": "批量采纳：与招标文件原文核对一致"})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == len(pkg["criteria"])

    # 空理由 → 422
    r = client.post(f"/api/rules/packages/{package_id}/accept-all", json={"reason": ""})
    assert r.status_code == 422, r.text

    # 确认 → 冻结走通
    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    fr = client.post(f"/api/rules/packages/{package_id}/freeze")
    assert fr.status_code == 200, fr.text

    # 非草稿 → 409
    r = client.post(f"/api/rules/packages/{package_id}/accept-all",
                    json={"reason": "再试一次"})
    assert r.status_code == 409, r.text

    # 留痕：audit + review_records
    assert _audit_count(app_env / "test.db", "rules.signoff_all") >= 1
    assert _audit_count(app_env / "test.db", "rules.accept_all") >= 1
    con = sqlite3.connect(str(app_env / "test.db"))
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM review_records WHERE action = 'accept' AND actor != 'auto'"
        ).fetchone()[0]
    finally:
        con.close()
    assert n == len(pkg["criteria"])


def test_accept_all_on_law_package(client, monkeypatch, app_env) -> None:  # noqa: ANN001
    """法规包（origin=law，免签批，全部 verified）也必须能全部采纳并冻结。

    回归护栏：前端「全部采纳」按钮以 verifiedCount>0 为显示条件，
    若法规条目不可批量采纳，用户会看到按钮却点不动（2026-10-20 用户实测反馈）。
    """
    from pathlib import Path

    from app.rules import law_kb as lk
    from app.rules.library.law_pack_loader import reset_law_pack_cache

    repo_kb = Path(__file__).resolve().parent.parent.parent / "data" / "law_kb.json"
    monkeypatch.setenv("BID_EVAL_LAW_KB_PATH", str(repo_kb))
    lk.reset_cache()
    reset_law_pack_cache()
    try:
        pid = client.post("/api/projects", json={"name": "法规包批量采纳"}).json()["id"]
        r = client.post(f"/api/projects/{pid}/rules/import-law-pack",
                        json={"pack_id": "general-law"})
        assert r.status_code == 200, r.text
        assert r.json()["imported"] == 87
        package_id = r.json()["package_id"]

        detail = client.get(f"/api/projects/{pid}/rules").json()
        assert len(detail["criteria"]) == 87
        # 法规条目：免签批（signoff_json 为空）、全部 verified → 前端 verifiedCount = 87
        assert all(c["status"] == "verified" for c in detail["criteria"])
        assert all(c["origin"] == "law" for c in detail["criteria"])
        assert all(c["signoff_json"] is None for c in detail["criteria"])

        # 全部采纳：法规条目逐条写入 accept 留痕
        r = client.post(f"/api/rules/packages/{package_id}/accept-all",
                        json={"reason": "批量采纳：法条逐条核验通过"})
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] == 87

        # 采纳后仍可确认 → 冻结（免签批语义不受影响）
        assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
        fr = client.post(f"/api/rules/packages/{package_id}/freeze")
        assert fr.status_code == 200, fr.text
    finally:
        lk.reset_cache()
        reset_law_pack_cache()
