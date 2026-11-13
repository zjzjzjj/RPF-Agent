"""导入 API 层：错误码、202 形态、重导 409。"""
from __future__ import annotations

import io
import zipfile

from app.db.models import EvaluationRun, RulePackage
from app.db.session import session_scope


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


OK_ENTRIES = {
    "招标/项目(招标文件).pdf": "%PDF-1.4 x",
    "投标/公司甲/开标一览表.pdf": "%PDF-1.4 y",
}


def _post(client, content: bytes, name: str | None = "导入API测试"):  # noqa: ANN001
    data = {} if name is None else {"name": name}
    return client.post(
        "/api/projects/import-bundle",
        files={"file": ("项目包.zip", content, "application/zip")},
        data=data,
    )


def test_create_returns_202(client, app_env) -> None:  # noqa: ANN001
    r = _post(client, _zip_bytes(OK_ENTRIES))
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["project_id"] and out["job_id"]


def test_not_a_zip(client, app_env) -> None:  # noqa: ANN001
    r = _post(client, b"this is not a zip")
    assert r.status_code == 422
    assert r.json()["code"] == "ZIP_INVALID"


def test_precheck_failure_leaves_no_staging_or_project(client, app_env) -> None:  # noqa: ANN001
    """预检 4xx 不留脏数据：暂存 zip 被清、没有产生项目行。"""
    from app import config

    before = client.get("/api/projects").json()
    r = _post(client, _zip_bytes({"招标/项目(招标文件).pdf": "x"}))  # 缺投标
    assert r.status_code == 422
    staging = config.import_dir()
    assert not staging.exists() or not any(staging.iterdir())
    assert client.get("/api/projects").json() == before


def test_missing_bid_dir(client, app_env) -> None:  # noqa: ANN001
    r = _post(client, _zip_bytes({"招标/项目(招标文件).pdf": "x"}))
    assert r.status_code == 422
    assert r.json()["code"] == "ZIP_MISSING_BID_DIR"


def test_missing_tender_dir(client, app_env) -> None:  # noqa: ANN001
    r = _post(client, _zip_bytes({"投标/公司甲/开标一览表.pdf": "x"}))
    assert r.status_code == 422
    assert r.json()["code"] == "ZIP_MISSING_TENDER_DIR"


def test_empty_tender(client, app_env) -> None:  # noqa: ANN001
    r = _post(client, _zip_bytes({"招标/说明.txt": "x", "投标/公司甲/开标一览表.pdf": "x"}))
    assert r.status_code == 422
    assert r.json()["code"] == "ZIP_EMPTY_TENDER"


def test_no_bidders(client, app_env) -> None:  # noqa: ANN001
    r = _post(client, _zip_bytes({"招标/项目(招标文件).pdf": "x", "投标/说明.txt": "x"}))
    assert r.status_code == 422
    assert r.json()["code"] == "ZIP_NO_BIDDERS"


def test_duplicate_bidder(client, app_env) -> None:  # noqa: ANN001
    r = _post(
        client,
        _zip_bytes(
            {
                "招标/项目(招标文件).pdf": "x",
                "投标/公司甲/a.pdf": "x",
                "投标/公司甲 /b.pdf": "x",
            }
        ),
    )
    assert r.status_code == 422
    assert r.json()["code"] == "ZIP_DUPLICATE_BIDDER"


def test_zip_too_large(client, app_env, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_IMPORT_MAX_ZIP_MB", "0")
    r = _post(client, _zip_bytes(OK_ENTRIES))
    assert r.status_code == 413
    assert r.json()["code"] == "ZIP_TOO_LARGE"


def test_reimport_conflicts_with_runs(client, app_env) -> None:  # noqa: ANN001
    pid = client.post("/api/projects", json={"name": "重导409"}).json()["id"]
    with session_scope() as s:
        pkg = RulePackage(project_id=pid, version=1, status="frozen")
        s.add(pkg)
        s.flush()
        s.add(EvaluationRun(project_id=pid, package_id=pkg.id, package_version=1))
    r = client.post(
        f"/api/projects/{pid}/import-bundle",
        files={"file": ("项目包.zip", _zip_bytes(OK_ENTRIES), "application/zip")},
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "CONFLICT"


def test_reimport_unknown_project(client, app_env) -> None:  # noqa: ANN001
    r = client.post(
        "/api/projects/PRJ-NOPE/import-bundle",
        files={"file": ("项目包.zip", _zip_bytes(OK_ENTRIES), "application/zip")},
    )
    assert r.status_code == 404
