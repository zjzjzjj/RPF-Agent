"""上传限额预检（upload_guard）：Content-Length 超限在收 body 前 413。"""
from __future__ import annotations

from app.api.upload_guard import oversized_upload_response

LIMIT = 100 * 1024 * 1024  # 100MB


def test_rejects_oversized_import_bundle_create() -> None:
    r = oversized_upload_response("POST", "/api/projects/import-bundle", LIMIT + 8 * 1024 * 1024 + 1, LIMIT)
    assert r is not None
    assert r.status_code == 413
    assert r.body  # 信封 JSON 落在 body


def test_rejects_oversized_import_bundle_reimport() -> None:
    r = oversized_upload_response(
        "POST", "/api/projects/PRJ-1/import-bundle", 3 * LIMIT, LIMIT
    )
    assert r is not None
    assert r.status_code == 413


def test_slack_absorbs_multipart_overhead() -> None:
    """限额内的 zip + multipart 开销（≤8MB）不被误杀。"""
    assert oversized_upload_response(
        "POST", "/api/projects/import-bundle", LIMIT + 4 * 1024 * 1024, LIMIT
    ) is None
    assert oversized_upload_response(
        "POST", "/api/projects/import-bundle", LIMIT, LIMIT
    ) is None


def test_other_paths_and_methods_pass() -> None:
    assert oversized_upload_response("POST", "/api/projects/PRJ-1/documents", 3 * LIMIT, LIMIT) is None
    assert oversized_upload_response("GET", "/api/projects/import-bundle", 3 * LIMIT, LIMIT) is None


def test_missing_content_length_passes() -> None:
    """chunked 无 Content-Length：不拦，交给流式落盘精确计数兜底。"""
    assert oversized_upload_response("POST", "/api/projects/import-bundle", None, LIMIT) is None
