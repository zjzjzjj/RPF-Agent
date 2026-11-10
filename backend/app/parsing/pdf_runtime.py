"""Bounded, spawn-isolated PDF work and atomic derived-artifact utilities.

Only local PDF/image operations belong in this pool. OCR HTTP requests, model
calls and retry sleeps execute in the caller, never in a PDF worker.
"""
from __future__ import annotations

import atexit
import errno
import io
import json
import math
import multiprocessing
import os
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, TimeoutError
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from time import monotonic as _metric_clock

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_worker_timings: ContextVar[dict | None] = ContextVar("pdf_worker_timings", default=None)


class OCRNoTextError(RuntimeError):
    """Recognition completed without text; image facts still require vision."""

    def __init__(self, message: str = "OCR 未产生有效文本", *, context: dict | None = None,
                 engine_attempts: list[dict] | None = None):
        super().__init__(message)
        self.ocr_context = dict(context or {})
        self.engine_attempts = list(engine_attempts or [])


class OCRProcessingError(RuntimeError):
    """All applicable OCR engines were tried; the terminal failure is technical."""

    ocr_attempts_exhausted = True

    def __init__(self, message: str = "OCR 处理引擎已全部失败", *, context: dict | None = None,
                 engine_attempts: list[dict] | None = None):
        super().__init__(message)
        self.ocr_context = dict(context or {})
        self.engine_attempts = list(engine_attempts or [])


def _shutdown_pool() -> None:
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown_pool)


def run_pdf_task(operation: str, *args, **kwargs):
    """Run a serializable local operation in one of two isolated processes."""
    global _pool
    with _pool_lock:
        if _pool is None:
            workers = max(1, min(2, int(os.environ.get("BID_EVAL_PDF_WORKERS", "2"))))
            _pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
        pool = _pool
    from app.llm.request_control import check_cancelled, emit_pdf_task_metrics

    check_cancelled()
    try:
        future = pool.submit(_dispatch, operation, args, kwargs, _metric_clock())
        while True:
            try:
                result = future.result(timeout=0.25)
                break
            except TimeoutError:
                check_cancelled()
    except BrokenProcessPool:
        # A crashed native worker must not poison every later resume attempt.
        with _pool_lock:
            if _pool is pool:
                _pool = None
        pool.shutdown(wait=False, cancel_futures=True)
        raise RuntimeError("PDF 隔离进程意外退出，可在下一次读取时重建") from None
    if result.get("timing") is not None:
        emit_pdf_task_metrics(operation, result["timing"], error_type=None if result["ok"] else "PDFTaskError")
    if not result["ok"]:
        if result.get("error_type") == "OCRNoTextError":
            raise OCRNoTextError(result["error"])
        raise RuntimeError(result["error"])
    return result["value"]


def _dispatch(operation, args, kwargs, submitted_at=None):
    started = _metric_clock()
    timings = {}
    token = _worker_timings.set(timings)
    try:
        operations = {
            "probe": _probe_pdf,
            "render": _render_pdf,
            "ocr_images": _prepare_ocr_images,
            "layout": _extract_layout,
            "rapid_text": _rapid_text,
            "rapid_lines": _rapid_lines,
        }
        result = {"ok": True, "value": operations[operation](*args, **kwargs)}
    except Exception as exc:  # Keep exception transport independent of custom constructors.
        result = {"ok": False, "error": str(exc),
                  "error_type": "OCRNoTextError" if isinstance(exc, OCRNoTextError) else "RuntimeError"}
    finally:
        elapsed = max(0, round((_metric_clock() - started) * 1000))
        _worker_timings.reset(token)
    rendered = elapsed if operation == "render" else min(elapsed, timings.get("render_ms", 0))
    result["timing"] = {"pdf_worker_ms": elapsed,
        "pdf_queue_wait_ms": max(0, round((started - submitted_at) * 1000)) if submitted_at is not None else 0,
        "render_ms": rendered,
        "pdf_prepare_ms": elapsed - rendered if operation == "ocr_images" else 0,
        "ocr_local_ms": elapsed if operation in {"rapid_text", "rapid_lines"} else 0}
    return result


@contextmanager
def artifact_lock(path: Path):
    """Stable cross-process lock; closing releases it, lock files are never removed."""
    from app.services.run_execution_lock import _acquire
    from app.llm.request_control import check_cancelled

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    os.set_inheritable(descriptor, False)
    try:
        while True:
            check_cancelled()
            try:
                _acquire(descriptor)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}:
                    raise
                time.sleep(0.025)
        yield
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _geometry(document, page_no: int) -> dict:
    if document.needs_pass:
        raise RuntimeError("原 PDF 仍被加密，不能读取原页")
    if page_no < 1 or page_no > len(document):
        raise RuntimeError(f"页码越界：{page_no}（共 {len(document)} 页）")
    page = document[page_no - 1]
    rect = page.rect
    values = (float(rect.width), float(rect.height))
    if not all(math.isfinite(v) and v > 0 for v in values):
        raise RuntimeError(f"第 {page_no} 页原页尺寸无效")
    return {"page_count": len(document), "width": values[0], "height": values[1],
            "rotation": int(page.rotation) % 360}


def _probe_pdf(path: str) -> dict:
    import pymupdf

    pages = []
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise RuntimeError("原 PDF 仍被加密，无法核验嵌入图像，不能视为已完整读取")
        for number, page in enumerate(document, 1):
            try:
                geometry = _geometry(document, number)
                visible = page.rect * page.derotation_matrix
                area = float(visible.get_area())
                if not math.isfinite(area) or area <= 0:
                    raise ValueError("原页可见区域无效")
                images = page.get_image_info()
                if not isinstance(images, list):
                    raise ValueError("原页图像信息不是有效列表")
                largest = 0.0
                for image in images:
                    bbox = image.get("bbox")
                    if not bbox or len(bbox) != 4 or not all(math.isfinite(float(v)) for v in bbox):
                        raise ValueError("原页图像位置无法可靠确定")
                    bounds = pymupdf.Rect(bbox)
                    if bounds.is_infinite:
                        raise ValueError("原页图像位置超出有效范围")
                    largest = max(largest, float((bounds & visible).get_area()) / area)
                pages.append({**geometry, "page_no": number, "embedded_image_ratio": round(largest, 6)})
            except Exception as exc:
                raise RuntimeError(f"第 {number} 页原文件图像结构核验失败：{exc}") from exc
        return {"page_count": len(document), "pages": pages}


def _render_pdf(path: str, page_no: int, scale: float) -> dict:
    import pymupdf

    # The independently readable original is also the reference for a fallback.
    with pymupdf.open(path) as reference:
        geometry = _geometry(reference, page_no)
        try:
            import pypdfium2 as pdfium

            with pdfium.PdfDocument(path) as document:
                if len(document) != geometry["page_count"]:
                    raise RuntimeError("PDF 引擎页数不一致")
                with document[page_no - 1] as page:
                    width, height = page.get_size()
                    if (abs(width - geometry["width"]) > 1 or abs(height - geometry["height"]) > 1
                            or int(page.get_rotation()) % 360 != geometry["rotation"]):
                        raise RuntimeError("PDF 引擎原页几何或旋转不一致")
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        output = io.BytesIO()
                        image.save(output, format="PNG")
                        return {**geometry, "engine": "pdfium", "png": output.getvalue()}
                    finally:
                        bitmap.close()
        except Exception as pdfium_error:
            # Never normalize/rewrite the source PDF or invent a page mapping.
            page = reference[page_no - 1]
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            return {**geometry, "engine": "pymupdf", "png": pixmap.tobytes("png"),
                    "fallback_reason": type(pdfium_error).__name__}


def _prepare_ocr_images(path: str, page_no: int, min_ratio: float, scale: float,
                        *, force_full_page: bool = False) -> dict:
    import pymupdf
    from app.parsing.pdf_pymupdf import _page_image_blocks

    with pymupdf.open(path) as document:
        geometry = _geometry(document, page_no)
        page = document[page_no - 1]
        blocks = [] if force_full_page else _page_image_blocks(document, page, min_ratio=min_ratio)
        if blocks:
            # The extraction helper can skip unsupported/inline image streams.
            # A subset must not discharge an unreadability warning for the page.
            visible = page.rect * page.derotation_matrix
            area = float(visible.get_area())
            expected = 0
            for info in page.get_image_info(xrefs=True):
                bbox = info.get("bbox")
                if not bbox or len(bbox) != 4 or not all(math.isfinite(float(v)) for v in bbox):
                    raise RuntimeError(f"第 {page_no} 页目标图像位置无效")
                image_area = float((pymupdf.Rect(bbox) & visible).get_area())
                if image_area / area + 1e-9 >= min_ratio:
                    expected += 1
            if len(blocks) != expected:
                blocks = []
        if blocks:
            return {**geometry, "kind": "img", "images": [
                {"rect": rect, "png": png} for rect, png in blocks]}
        started = _metric_clock()
        try:
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            return {**geometry, "kind": "page", "images": [
                {"rect": (0.0, 0.0, geometry["width"], geometry["height"]), "png": pixmap.tobytes("png")} ]}
        finally:
            timings = _worker_timings.get()
            if timings is not None:
                timings["render_ms"] = max(0, round((_metric_clock() - started) * 1000))


def _extract_layout(path: str) -> dict:
    import pymupdf
    from app.parsing.pdf_pymupdf import _has_large_image, _page_lines, _page_tables

    rows = []
    with pymupdf.open(path) as document:
        if document.needs_pass and document.authenticate("") == 0:
            raise RuntimeError("PDF 已加密，无法解析")
        for number, page in enumerate(document, 1):
            geometry = _geometry(document, number)
            warnings = []
            try:
                texts, boxes = _page_lines(page)
            except Exception as exc:
                warnings.append(f"第 {number} 页文本提取失败：{exc}")
                texts, boxes = [], []
            try:
                tables = _page_tables(page)
            except Exception as exc:
                warnings.append(f"第 {number} 页表格探测失败：{exc}")
                tables = []
            rows.append({**geometry, "page_no": number, "texts": texts, "boxes": boxes,
                         "tables": tables, "has_images": bool(page.get_images()),
                         "has_large_image": _has_large_image(page), "warnings": warnings})
        return {"page_count": len(document), "pages": rows}


def _rapid_text(png_bytes: bytes) -> str:
    return "\n".join(text for text, _box in _rapid_lines(png_bytes))


def _rapid_lines(png_bytes: bytes) -> list:
    import numpy as np
    from PIL import Image
    from app.parsing import ocr

    if ocr.get_ocr() is None:
        raise RuntimeError("OCR 组件未安装")
    with Image.open(io.BytesIO(png_bytes)) as image:
        array = np.array(image.convert("RGB"))[:, :, ::-1]
    lines = [(str(text), tuple(float(value) for value in box))
             for text, box in ocr.recognize(array) if str(text).strip()]
    if not lines:
        raise OCRNoTextError()
    return lines
