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


def test_stream_over_limit_cleans_staging(client, app_env, monkeypatch) -> None:  # noqa: ANN001
    """流式落盘中途超限：413 且暂存目录清干净（不留半截 zip）。"""
    from app import config

    monkeypatch.setenv("BID_EVAL_IMPORT_MAX_ZIP_MB", "1")  # 1MB
    r = _post(client, b"x" * (3 * 1024 * 1024))  # 3MB，越过流式精确限额
    assert r.status_code == 413
    assert r.json()["code"] == "ZIP_TOO_LARGE"
    staging = config.import_dir()
    assert not staging.exists() or not any(staging.iterdir())


def test_content_length_precheck_before_body(client, app_env, monkeypatch) -> None:  # noqa: ANN001
    """upload_guard 中间件：Content-Length 超限直接 413（multipart 开销余量置 0）。

    message 必须含"本次约"、detail 必须含 size_mb——两者都是中间件信封专属
    （_save_zip 的同码 413：message 只有"上限 X MB"、detail 只有 limit_mb）。
    若中间件被禁用，响应来自流式兜底，本测试必须变红。
    （2026-11-17 变异测试实证：只断言 code/detail.limit_mb 时禁用中间件的变异体存活。）
    """
    monkeypatch.setenv("BID_EVAL_IMPORT_MAX_ZIP_MB", "0")
    monkeypatch.setattr("app.api.upload_guard.SLACK_BYTES", 0)
    r = _post(client, _zip_bytes(OK_ENTRIES))
    assert r.status_code == 413
    assert r.json()["code"] == "ZIP_TOO_LARGE"
    assert "limit_mb" in r.json()["detail"]
    assert "size_mb" in r.json()["detail"]  # 中间件信封专属；流式兜底不产出
    assert "本次约" in r.json()["message"]


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
