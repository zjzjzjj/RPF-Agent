"""页面渲染：pypdfium2 渲染 PNG 并磁盘缓存。

为什么服务端渲染而不是前端 PDF.js：
- 中文 PDF 在浏览器端有 CMap/字体坑；扫描件更是无文字层；
- 高亮**不烧录**进 PNG——前端在页图上按归一化 bbox 百分比叠加高亮层，
  切换证据零延迟（同一页只渲染一次）。
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path

from app import config
from app.errors import AppError


RENDER_VERSION = "isolated-pdf-v2"


def _check_geometry(meta: dict, *, page_no: int, page_count, width, height, rotation) -> None:
    if page_count is not None and int(page_count) != int(meta["page_count"]):
        raise ValueError("原 PDF 页数与冻结解析记录不一致")
    for name, expected in (("width", width), ("height", height)):
        if expected is None:
            continue
        expected = float(expected)
        if not math.isfinite(expected) or expected < 0:
            raise ValueError(f"第 {page_no} 页冻结解析尺寸无效")
        if expected > 0 and abs(float(meta[name]) - expected) > 1:
            raise ValueError(f"第 {page_no} 页原页尺寸与冻结解析记录不一致")
    if rotation is not None and int(rotation) % 360 != int(meta["rotation"]) % 360:
        raise ValueError(f"第 {page_no} 页原页旋转与冻结解析记录不一致")


def _valid_png(content: bytes, meta: dict, scale: float) -> bool:
    from PIL import Image

    try:
        if hashlib.sha256(content).hexdigest() != meta.get("image_sha256"):
            return False
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            return (image.format == "PNG" and abs(image.width - float(meta["width"]) * scale) <= 2
                    and abs(image.height - float(meta["height"]) * scale) <= 2)
    except Exception:
        return False


def render_page_png(sha256: str, stored_path: str, page_no: int, scale: float | None = None, *,
                    expected_page_count: int | None = None, expected_width: float | None = None,
                    expected_height: float | None = None, expected_rotation: int | None = None) -> Path:
    """Render the original in an isolated process; publish only complete PNGs."""
    from app.parsing.pdf_runtime import artifact_lock, atomic_json, atomic_write, run_pdf_task
    from app.jobs.queue import JobCancelled

    scale = config.render_scale() if scale is None else float(scale)
    if not math.isfinite(scale) or not 0 < scale <= 8 or page_no < 1:
        raise AppError("页面渲染参数无效", detail={"page_no": page_no, "stage": "render", "error_code": "PDF_RENDER_INPUT"})
    key = hashlib.sha256(json.dumps([RENDER_VERSION, sha256, page_no, scale]).encode()).hexdigest()
    cache = config.render_dir() / "v2" / key / "page.png"
    metadata = cache.with_suffix(".json")
    expected = {"page_no": page_no, "page_count": expected_page_count, "width": expected_width,
                "height": expected_height, "rotation": expected_rotation}
    try:
        with artifact_lock(cache.with_suffix(".lock")):
            if cache.exists() and metadata.exists():
                try:
                    meta = json.loads(metadata.read_text(encoding="utf-8"))
                    content = cache.read_bytes()
                except (OSError, ValueError):
                    meta, content = {}, b""
                if not isinstance(meta, dict):
                    meta = {}
                if (meta.get("version") == RENDER_VERSION and meta.get("file_sha256") == sha256
                        and _valid_png(content, meta, scale)):
                    _check_geometry(meta, **expected)
                    return cache
            result = run_pdf_task("render", stored_path, page_no, scale)
            content = result.pop("png")
            meta = {**result, "version": RENDER_VERSION, "file_sha256": sha256,
                    "image_sha256": hashlib.sha256(content).hexdigest()}
            _check_geometry(meta, **expected)
            if not _valid_png(content, meta, scale):
                raise ValueError("原页渲染图像不完整或尺寸不匹配")
            atomic_write(cache, content)
            atomic_json(metadata, meta)
            return cache
    except (AppError, JobCancelled):
        raise
    except Exception as exc:
        raise AppError(f"页面渲染失败：第 {page_no} 页：{exc}", detail={
            "page_no": page_no, "stage": "render", "error_code": "PDF_RENDER_FAILED",
        }) from exc
