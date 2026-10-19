"""PDF 解析主路径：PyMuPDF（pymupdf）。

2026-10-13 许可决策反转（原 0001 决策弃用 AGPL 的 PyMuPDF，现按项目方向切回，
见 docs/decisions/0001 文末反转记录）；pdfplumber 保留为回退引擎
（BID_EVAL_PDF_ENGINE=pdfplumber 一行切回，MIT 闭包不变、不带 OCR）。

产出（与 pdf_pdfplumber.extract_pdf 同签名同契约）：
- 每页文本（逐行，附行 bbox）、页宽高、文字层标记；
- 表格 bbox（供条款树排除编号扫描）；
- 逐页 OCR 兜底：页字符数过少的页用 pypdfium2 渲染页图 → rapidocr 识别
  （扫描封面 + 数字正文的标准标书不再被整体拒绝）。

与 pdfplumber 行为对齐的两个关键点：
- 行文本 = span **无空格拼接**（中文不能插空格）；
- 行排序 = (round(y0,1), x0)（稳定阅读顺序，否则条款树边界漂移）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from app import config
from app.parsing.base import DocumentRejectedError, ExtractedDocument, ExtractedPage

log = logging.getLogger("bid_eval.parsing.pymupdf")

_MAGIC_WINDOW = 1024  # 魔数探测窗口：容忍真实平台 PDF 的前导垃圾/电子签章头


def _check_file(path: str) -> None:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        raise DocumentRejectedError(config.REJECT_EMPTY_FILE, "文件为空或不存在")
    if p.stat().st_size > config.max_upload_bytes():
        raise DocumentRejectedError(config.REJECT_TOO_LARGE, "文件超过大小上限")
    with open(path, "rb") as fh:
        head = fh.read(_MAGIC_WINDOW)
    if b"%PDF" not in head:
        raise DocumentRejectedError(config.REJECT_NOT_A_PDF, "文件不是 PDF 格式")


def _page_lines(page) -> tuple[list[str], list[tuple[float, float, float, float]]]:  # noqa: ANN001
    """页 → (行文本列表, 行 bbox 列表)。

    两步：① get_text("dict") 取原始行（spans 无空格拼接）；② **按 y 区间重叠合并
    视觉行**——PyMuPDF 把表格一行按单元格拆成多行（"档次"/"分值区间"/"评分标准"
    各一行），而下游（条款树编号扫描、LLM 的评分表抽取与证据定位）依赖
    "优 17-20分 判据…" 这样的整行形态；pdfplumber 的 extract_text_lines 正是按
    视觉行返回的，这里必须对齐，否则评分表抽取整块丢失。
    """
    raw: list[tuple[float, float, float, float, str]] = []
    data = page.get_text("dict")
    for block in data.get("blocks") or []:
        for line in block.get("lines") or []:
            text = "".join(span.get("text", "") for span in line.get("spans") or [])
            if not text.strip():
                continue
            x0, y0, x1, y1 = line.get("bbox") or (0, 0, 0, 0)
            raw.append((float(y0), float(x0), float(x1), float(y1), text))
    raw.sort(key=lambda r: (round(r[0], 1), r[1]))

    rows: list[list] = []  # 每项 [y0, x0, x1, y1, text]
    for y0, x0, x1, y1, text in raw:
        if rows:
            py0, _px0, px1, py1, ptext = rows[-1]
            overlap = min(py1, y1) - max(py0, y0)
            small = min(py1 - py0, y1 - y0)
            if small > 0 and overlap > 0.5 * small:
                # 同一视觉行（y 重叠过半）：合并文本与 bbox
                rows[-1] = [py0, min(_px0, x0), max(px1, x1), max(py1, y1),
                            ptext + " " + text]
                continue
        rows.append([y0, x0, x1, y1, text])

    texts = [r[4] for r in rows]
    boxes = [(r[1], r[0], r[2], r[3]) for r in rows]
    return texts, boxes


def _page_tables(page) -> list[tuple[float, float, float, float]]:  # noqa: ANN001
    tables = page.find_tables()
    out: list[tuple[float, float, float, float]] = []
    for table in tables or []:
        x0, top, x1, bottom = table.bbox
        out.append((float(x0), float(top), float(x1), float(bottom)))
    return out


# ---------------------------------------------------------------------------
# OCR 兜底（扫描页）
# ---------------------------------------------------------------------------
def _ocr_cache_path(png_bytes: bytes, engine: str) -> Path:
    import hashlib

    key = hashlib.sha256(f"{engine}-v1\x00".encode() + png_bytes).hexdigest()[:16]
    return config.data_dir() / "ocr_cache" / key / "result.json"


def _has_large_image(page) -> bool:  # noqa: ANN001
    """页面含大幅图像（单图占比 ≥35%）——「标题+扫描图」混排页的典型形态。

    这类页正文标题让字符数超过 OCR 阈值（30），但扫描图内容（合同/发票/验收
    报告）从未被识别（2026-10-15 实证：龙即 309 页、p133 验收报告 6143 字符
    丢失）。触发条件补充：字数少 + 大图 → 必须走 OCR。
    """
    try:
        rect = page.rect
        page_area = max(1.0, float(rect.width) * float(rect.height))
        for img in page.get_image_info():
            bbox = img.get("bbox")
            if not bbox:
                continue
            w = max(0.0, float(bbox[2]) - float(bbox[0]))
            h = max(0.0, float(bbox[3]) - float(bbox[1]))
            if (w * h) / page_area >= 0.35:
                return True
    except Exception:  # noqa: BLE001 —— 图像探测失败按无图处理
        return False
    return False


def _ocr_page(pix) -> list[tuple[str, tuple[float, float, float, float]]]:  # noqa: ANN001
    """渲染页图 → OCR → [(行文本, PDF 点坐标 bbox)]。调用方保证 ocr 可用。

    引擎选择（BID_EVAL_OCR_ENGINE）：remote_paddle（mwb 自部署 PaddleOCR-VL，
    扫描件识别质量显著更好）优先；调用失败或未配置时回退本地 rapidocr。
    remote 引擎返回整页文本无 bbox，以"单条全页行"融入行模型（0,0)-(页宽,页高)。
    """
    scale = config.OCR_RENDER_SCALE
    png_bytes = pix.tobytes("png")

    engine = config.ocr_engine()
    cache_file = _ocr_cache_path(png_bytes, engine)
    lines: list[tuple[str, tuple[float, float, float, float]]] | None = None
    if cache_file.exists():
        try:
            rows = json.loads(cache_file.read_text(encoding="utf-8"))
            lines = [(r[0], tuple(r[1])) for r in rows]  # type: ignore[misc]
        except Exception:  # noqa: BLE001 —— 缓存坏了当没有
            lines = None

    if lines is None:
        if engine == "remote_paddle":
            lines = _ocr_page_remote(png_bytes, pix)
        else:
            lines = _ocr_page_rapid(png_bytes, scale)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps([[t, list(b)] for t, b in lines], ensure_ascii=False),
            encoding="utf-8",
        )

    return lines


def _ocr_page_remote(png_bytes: bytes, pix) -> list[tuple[str, tuple[float, float, float, float]]]:  # noqa: ANN001
    """PaddleOCR-VL 整页识别；失败回退 rapidocr 并记警告。"""
    from app.parsing import paddle_client

    try:
        text = paddle_client.remote_ocr_page(png_bytes)
    except RuntimeError as exc:
        log.warning("remote_paddle 失败，回退 rapidocr：%s", exc)
        return _ocr_page_rapid(png_bytes, config.OCR_RENDER_SCALE)
    if not text:
        return _ocr_page_rapid(png_bytes, config.OCR_RENDER_SCALE)
    page_w, page_h = pix.width / config.OCR_RENDER_SCALE, pix.height / config.OCR_RENDER_SCALE
    return [(text, (0.0, 0.0, float(page_w), float(page_h)))]


def _ocr_page_rapid(png_bytes: bytes, scale: float) -> list[tuple[str, tuple[float, float, float, float]]]:  # noqa: ANN001
    """本地 rapidocr 引擎（原路径）。"""
    import io

    import numpy as np
    from PIL import Image

    from app.parsing import ocr as ocr_mod

    if ocr_mod.get_ocr() is None:
        raise RuntimeError("OCR 组件未安装")
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    # recognize 返回像素 bbox → ÷ 渲染倍率换算回 PDF 点坐标（同为 top-left 原点）
    return [
        (t, (b[0] / scale, b[1] / scale, b[2] / scale, b[3] / scale))
        for t, b in ocr_mod.recognize(np.array(img)[:, :, ::-1])  # RGB → BGR
    ]


def extract_pdf(path: str) -> ExtractedDocument:
    """解析 PDF：页文本 + 行 bbox + 表格 bbox + 扫描页 OCR 兜底。"""
    import pymupdf as fitz

    _check_file(path)
    warnings: list[str] = []
    degraded = False
    pages: list[ExtractedPage] = []
    table_boxes: dict[int, list[tuple[float, float, float, float]]] = {}
    ocr_used = False

    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "password" in msg or "encrypt" in msg:
            raise DocumentRejectedError(config.REJECT_ENCRYPTED, "PDF 已加密，无法解析") from exc
        raise DocumentRejectedError(config.REJECT_EXTRACTION_FAILED, f"PDF 打开失败：{exc}") from exc

    try:
        if doc.needs_pass and doc.authenticate("") == 0:
            raise DocumentRejectedError(config.REJECT_ENCRYPTED, "PDF 已加密，无法解析")
        if doc.page_count == 0:
            raise DocumentRejectedError(config.REJECT_EMPTY_FILE, "PDF 没有任何页面")

        author = str((doc.metadata or {}).get("author") or "").strip() or None

        ocr_budget = config.ocr_max_pages() if config.ocr_enabled() else 0
        ocr_available = True
        try:
            from app.parsing.ocr import get_ocr

            ocr_available = get_ocr() is not None
        except Exception:  # noqa: BLE001
            ocr_available = False

        total_chars = 0
        no_text_pages = 0
        for i, page in enumerate(doc, start=1):
            width, height = float(page.rect.width), float(page.rect.height)
            try:
                texts, boxes = _page_lines(page)
            except Exception as exc:  # noqa: BLE001 —— 单页失败不让整本文档失败
                warnings.append(f"第 {i} 页文本提取失败：{exc}")
                texts, boxes = [], []
                degraded = True

            page_text = "\n".join(texts)
            chars = sum(len(t) for t in texts)
            total_chars += chars
            source = "text"
            has_layer = True

            need_ocr = chars < config.OCR_PAGE_MIN_CHARS or (
                chars < 200 and _has_large_image(page)
            )
            if need_ocr:
                has_images = bool(page.get_images())
                if chars == 0 and not has_images:
                    # 真空白页（分隔页）：不浪费 OCR，也不算退化
                    pages.append(ExtractedPage(
                        page_no=i, text="", width=width, height=height,
                        has_text_layer=False, source="skipped", line_boxes=[],
                    ))
                    continue
                if ocr_budget > 0 and ocr_available:
                    try:
                        pix = page.get_pixmap(matrix=fitz.Matrix(
                            config.OCR_RENDER_SCALE, config.OCR_RENDER_SCALE))
                        ocr_lines = _ocr_page(pix)
                        texts = [t for t, _b in ocr_lines]
                        boxes = [b for _t, b in ocr_lines]
                        page_text = "\n".join(texts)
                        source = "ocr"
                        has_layer = False
                        ocr_used = True
                        degraded = True
                        ocr_budget -= 1
                        warnings.append(f"第 {i} 页为扫描件，已启用 OCR 识别")
                    except Exception as exc:  # noqa: BLE001 —— OCR 失败按缺字页处理
                        warnings.append(f"第 {i} 页 OCR 识别失败：{exc}")
                        degraded = True
                elif not ocr_available:
                    no_text_pages += 1
                    warnings.append(f"第 {i} 页疑似扫描件（OCR 组件未安装，pip install "
                                    "rapidocr-onnxruntime 后可自动识别）")
                elif ocr_budget <= 0:
                    warnings.append(f"第 {i} 页疑似扫描件，已超出 OCR 页数上限（"
                                    f"{config.ocr_max_pages()}），跳过")
                    degraded = True

            try:
                tables = _page_tables(page)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"第 {i} 页表格探测失败：{exc}")
                tables = []
                degraded = True
            if tables:
                table_boxes[i] = tables

            pages.append(ExtractedPage(
                page_no=i,
                text=page_text,
                width=width,
                height=height,
                has_text_layer=has_layer,
                source=source,
                line_boxes=boxes,
            ))

        if total_chars == 0 and no_text_pages == doc.page_count and doc.page_count > 0:
            raise DocumentRejectedError(
                config.REJECT_NO_TEXT_LAYER,
                "PDF 无文字层（疑似扫描件）。请安装 OCR 组件"
                "（pip install rapidocr-onnxruntime）或提供数字版文件。",
            )
    finally:
        doc.close()

    return ExtractedDocument(
        pages=pages,
        engine="pymupdf+ocr" if ocr_used else "pymupdf",
        warnings=warnings,
        table_boxes=table_boxes,
        degraded=degraded,
        author=author,
    )
