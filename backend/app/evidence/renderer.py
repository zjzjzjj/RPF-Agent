"""页面渲染：pypdfium2 渲染 PNG 并磁盘缓存。

为什么服务端渲染而不是前端 PDF.js：
- 中文 PDF 在浏览器端有 CMap/字体坑；扫描件更是无文字层；
- 高亮**不烧录**进 PNG——前端在页图上按归一化 bbox 百分比叠加高亮层，
  切换证据零延迟（同一页只渲染一次）。
"""
from __future__ import annotations

import base64
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


# ---------------------------------------------------------------------------
# 材料长图（2026-11-23 优化②：一份材料文档一张长图，竖拼过视觉大模型）
# ---------------------------------------------------------------------------
LONGSHOT_VERSION = "longshot-v1"


def render_long_image(sha256: str, stored_path: str, page_numbers: list[int], *,
                      max_pages_per_segment: int | None = None,
                      min_pages: int | None = None,
                      scale: float | None = None) -> list[dict]:
    """一份材料文档一张长图：逐页 render_page_png → 竖拼 PNG，超长分段。

    分段策略：≤max_pages_per_segment（默认 30）页一段；尾段 <min_pages（默认 5）
    并入前段（宁可单段略超上限，不产碎片小图）。拼接缓存落
    render_dir()/longshots/<key>/seg{n}.png，key 含（版本、文件哈希、页集、
    scale、分段参数）；页渲染复用 render_page_png 自身缓存。
    返回 [{image_path, image_sha256, data_url, page_numbers}]，data_url 为
    data:image/png;base64（openai_compat 多模态 image_url 直用）。
    """
    import base64 as _b64

    from PIL import Image

    from app.parsing.pdf_runtime import artifact_lock, atomic_write

    max_pages = max_pages_per_segment or config.vl_fallback_max_pages_per_segment()
    min_pages = min_pages or config.vl_fallback_min_pages()
    pages = sorted({int(p) for p in page_numbers if int(p) >= 1})
    if not pages:
        return []
    key = hashlib.sha256(json.dumps(
        [LONGSHOT_VERSION, sha256, pages, scale, max_pages, min_pages]
    ).encode()).hexdigest()
    out_dir = config.render_dir() / "longshots" / key
    chunks = [pages[i:i + max_pages] for i in range(0, len(pages), max_pages)]
    if len(chunks) > 1 and len(chunks[-1]) < min_pages:
        chunks[-2].extend(chunks.pop())
    out_dir.mkdir(parents=True, exist_ok=True)
    segments: list[dict] = []
    for n, chunk in enumerate(chunks):
        cache = out_dir / f"seg{n}.png"
        with artifact_lock(cache.with_suffix(".lock")):
            if cache.exists():
                content = cache.read_bytes()
            else:
                imgs = [Image.open(render_page_png(sha256, stored_path, page, scale)).convert("RGB")
                        for page in chunk]
                width = max(im.width for im in imgs)
                height = sum(im.height for im in imgs)
                canvas = Image.new("RGB", (width, height), "white")
                y = 0
                for im in imgs:
                    canvas.paste(im, (0, y))
                    y += im.height
                buf = io.BytesIO()
                canvas.save(buf, format="PNG")
                content = buf.getvalue()
                atomic_write(cache, content)
        segments.append({
            "image_path": str(cache),
            "image_sha256": hashlib.sha256(content).hexdigest(),
            "data_url": "data:image/png;base64," + _b64.b64encode(content).decode("ascii"),
            "page_numbers": list(chunk),
        })
    return segments
