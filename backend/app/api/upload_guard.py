"""上传限额预检：Content-Length 超限在收 body 之前就拒绝。

背景：项目包（zip）上传曾整包接收后才检查大小——3.5GB 的真实标书包
让用户对着无进度的页面等十几分钟，最后只换来一个 413 toast。
本模块在 HTTP 中间件层看一眼 Content-Length，超限立即回错误信封，
浏览器几秒内收到响应；multipart 开销（boundary/表单字段）留 SLACK_BYTES 余量，
精确限额仍由 imports._save_zip 流式落盘时兜底。

Content-Length 缺失（chunked）时不拦——交给流式落盘的精确计数。
"""
from __future__ import annotations

import re

from fastapi.responses import JSONResponse

# multipart 包裹开销（boundary、name/project_code 等小字段）的余量；
# 模块级变量（非默认参数）以便测试 monkeypatch。
SLACK_BYTES = 8 * 1024 * 1024

# POST /api/projects/import-bundle 与 POST /api/projects/{pid}/import-bundle
_RE_IMPORT_BUNDLE = re.compile(r"^/api/projects/(?:[^/]+/)?import-bundle$")


def oversized_upload_response(
    method: str,
    path: str,
    content_length: int | None,
    limit_bytes: int,
) -> JSONResponse | None:
    """超限返回 413 错误信封，否则 None（放行）。"""
    if method != "POST" or not _RE_IMPORT_BUNDLE.match(path):
        return None
    if content_length is None:
        return None
    if content_length <= limit_bytes + SLACK_BYTES:
        return None
    limit_mb = limit_bytes // (1024 * 1024)
    size_mb = content_length / (1024 * 1024)
    return JSONResponse(
        status_code=413,
        content={
            "code": "ZIP_TOO_LARGE",
            "message": f"项目包超出大小限制（上限 {limit_mb} MB，本次约 {size_mb:.0f} MB）",
            "detail": {"limit_mb": limit_mb, "size_mb": round(size_mb)},
        },
    )
