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
def _ocr_cache_path(png_bytes: bytes, engine: str, variant: str) -> Path:
    """OCR 结果缓存路径。

    键含 engine + 版本号 + 路径变体（page=整页渲染 / img=嵌入图原图）+ 提示词：
    旧实现只有 engine-v1，改渲染倍率/换 OCR 路径时旧结果会被复用（"字节变了
    所以键变了"是隐式推理，不可靠）。v2 显式失效历史缓存。
    """
    key = hashlib.sha256(
        f"{engine}-v2-{variant}-{config.paddle_ocr_prompt()}\x00".encode() + png_bytes
    ).hexdigest()[:16]
    return config.data_dir() / "ocr_cache" / key / "result.json"


def _cached_lines(
    png_bytes: bytes, engine: str, variant: str
) -> list[tuple[str, tuple[float, float, float, float]]] | None:
    path = _ocr_cache_path(png_bytes, engine, variant)
    if not path.exists():
        return None
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [(r[0], tuple(r[1])) for r in rows]  # type: ignore[misc]
    except Exception:  # noqa: BLE001 —— 缓存坏了当没有
        return None


def _store_lines(
    png_bytes: bytes,
    engine: str,
    variant: str,
    lines: list[tuple[str, tuple[float, float, float, float]]],
) -> None:
    path = _ocr_cache_path(png_bytes, engine, variant)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([[t, list(b)] for t, b in lines], ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 —— 缓存写失败不影响解析
        log.warning("OCR 缓存写入失败：%s", exc)


def _has_large_image(page) -> bool:  # noqa: ANN001
    """页面含大幅图像（单图占比 ≥ OCR_IMAGE_MIN_RATIO）——「标题+扫描图」混排页的典型形态。

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
            if (w * h) / page_area >= config.OCR_IMAGE_MIN_RATIO:
                return True
    except Exception:  # noqa: BLE001 —— 图像探测失败按无图处理
        return False
    return False


def _to_png(data: bytes) -> bytes | None:
    """嵌入图流 → PNG 字节（PIL 重编码）。

    嵌入流常是 JPEG/CMYK/带 SMask，直接 base64 加 `data:image/png` 前缀会被
    服务端拒绝或解码出错；重编码后缓存键也与原始 ext 无关、稳定。
    打不开（JPX 等）返回 None，由调用方退回整页渲染。
    """
    import io

    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _page_image_blocks(
    doc, page, min_ratio: float | None = None  # noqa: ANN001
) -> list[tuple[tuple[float, float, float, float], bytes]]:
    """页内大幅嵌入图 → [((x0,y0,x1,y1), png_bytes)]（阅读序）。

    2026-10-21 服务器实证：整页渲染图送 PaddleOCR-VL **只能读回文字层**
    （诚道 p46 只返回"第 45 页"），而**嵌入图原图**能完整读出内容
    （同页 366 字："国家市场监督管理总局 全国认证认可信息公共服务平台…
    证书编号：02623Q30431R5M-HZ…杭州诚道科技股份有限公司"）。故扫描件优先原图。
    """
    try:
        rotation = abs(float(page.rotation or 0))
    except Exception:  # noqa: BLE001
        rotation = 0.0
    if rotation > 0.01:
        return []  # 旋转页 placement bbox 与渲染坐标不一致，退回整页渲染
    try:
        rect = page.rect
        page_area = max(1.0, float(rect.width) * float(rect.height))
        infos = page.get_image_info(xrefs=True)
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[tuple[float, float, float, float], bytes]] = []
    for info in infos or []:
        try:
            xref = int(info.get("xref") or 0)
        except (TypeError, ValueError):
            continue
        if xref <= 0:
            continue  # 内联图无 xref，extract_image 取不到
        bbox = info.get("bbox")
        if not bbox:
            continue
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        w, h = max(0.0, x1 - x0), max(0.0, y1 - y0)
        if (w * h) / page_area < (
            config.OCR_IMAGE_MIN_RATIO if min_ratio is None else float(min_ratio)
        ):
            continue  # logo/印章/水印
        try:
            raw = doc.extract_image(xref)
        except Exception:  # noqa: BLE001
            continue
        if min(int(raw.get("width") or 0), int(raw.get("height") or 0)) < config.OCR_IMAGE_MIN_SIDE:
            continue
        png = _to_png(raw.get("image") or b"")
        if png is None:
            continue
        out.append(((x0, y0, x1, y1), png))
    out.sort(key=lambda item: (round(item[0][1], 1), item[0][0]))
    return out


def _remote_text(png_bytes: bytes, state: dict) -> str:
    """远程 OCR 调用（带熔断）。不可用或失败抛 RuntimeError。"""
    if not state.get("remote_ok"):
        raise RuntimeError("远程 OCR 不可用")
    from app.parsing import paddle_client

    try:
        text = paddle_client.remote_ocr_page(png_bytes)
    except RuntimeError:
        state["remote_failures"] = int(state.get("remote_failures", 0)) + 1
        if state["remote_failures"] >= config.ocr_max_failures():
            state["remote_ok"] = False
            state["tripped"] = True
        raise
    state["remote_failures"] = 0
    return text


def _rapid_lines(
    png_bytes: bytes, rect: tuple[float, float, float, float]
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """本地 rapidocr 识别并把像素 bbox 换算到给定 rect（PDF 点坐标）。"""
    import io

    import numpy as np
    from PIL import Image

    from app.parsing import ocr as ocr_mod

    if ocr_mod.get_ocr() is None:
        raise RuntimeError("OCR 组件未安装")
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    px_w, px_h = img.size
    sx = max(1e-6, (rect[2] - rect[0]) / max(1, px_w))
    sy = max(1e-6, (rect[3] - rect[1]) / max(1, px_h))
    return [
        (
            t,
            (rect[0] + b[0] * sx, rect[1] + b[1] * sy, rect[0] + b[2] * sx, rect[1] + b[3] * sy),
        )
        for t, b in ocr_mod.recognize(np.array(img)[:, :, ::-1])  # RGB → BGR
    ]


def _lines_in_rect(
    text: str, rect: tuple[float, float, float, float]
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """整块文本按行拆开，每行都赋该块的 rect。

    远程引擎不返回 bbox。赋"该图/该页的 rect"而不是整页 rect，比旧实现
    （整页 blob 单行）更精确：`_in_table` 表格排除能生效，且**行数与
    text.split("\\n") 一一对应**（旧实现违反 base.ExtractedPage 契约，
    导致后续行 bbox=None 并可能凭空造出条款节点）。
    """
    return [(line, rect) for line in text.split("\n") if line.strip()]


def _ocr_image_lines(
    png_bytes: bytes, rect: tuple[float, float, float, float], engine: str, state: dict
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """单张嵌入图 → OCR 行（带缓存）。"""
    cached = _cached_lines(png_bytes, engine, "img")
    if cached is not None:
        return cached
    lines: list[tuple[str, tuple[float, float, float, float]]] | None = None
    if engine == "remote_paddle":
        try:
            text = _remote_text(png_bytes, state)
            lines = _lines_in_rect(text, rect)
        except RuntimeError as exc:
            log.warning("嵌入图远程 OCR 失败，回退 rapidocr：%s", exc)
    if lines is None:
        lines = _rapid_lines(png_bytes, rect)
    _store_lines(png_bytes, engine, "img", lines)
    return lines


def _ocr_render_lines(
    png_bytes: bytes, page, engine: str, state: dict  # noqa: ANN001
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """整页渲染图 → OCR 行（带缓存）。"""
    cached = _cached_lines(png_bytes, engine, "page")
    if cached is not None:
        return cached
    rect = (0.0, 0.0, float(page.rect.width), float(page.rect.height))
    lines: list[tuple[str, tuple[float, float, float, float]]] | None = None
    if engine == "remote_paddle":
        try:
            text = _remote_text(png_bytes, state)
            lines = _lines_in_rect(text, rect)
        except RuntimeError as exc:
            log.warning("整页远程 OCR 失败，回退 rapidocr：%s", exc)
    if lines is None:
        lines = _rapid_lines(png_bytes, rect)
    _store_lines(png_bytes, engine, "page", lines)
    return lines


def _ocr_page(
    doc, page, engine: str, state: dict  # noqa: ANN001
) -> tuple[list[tuple[str, tuple[float, float, float, float]]], str, bool]:
    """页 → (OCR 行, 路径 img|page, 是否低产出)。

    优先对页内嵌入图**原图**逐张 OCR（质量与速度都优于整页渲染），
    无可用图时退回整页渲染。两者都失败时抛 RuntimeError（调用方记警告）。
    """
    import pymupdf as fitz

    last_exc: Exception | None = None
    blocks = _page_image_blocks(doc, page)
    if blocks:
        lines: list[tuple[str, tuple[float, float, float, float]]] = []
        for rect, png in blocks:
            try:
                lines.extend(_ocr_image_lines(png, rect, engine, state))
            except RuntimeError as exc:
                last_exc = exc
        if lines:
            return lines, "img", False
    pix = page.get_pixmap(
        matrix=fitz.Matrix(config.OCR_RENDER_SCALE, config.OCR_RENDER_SCALE)
    )
    png_bytes = pix.tobytes("png")
    try:
        lines = _ocr_render_lines(png_bytes, page, engine, state)
    except RuntimeError:
        if last_exc is not None:
            raise last_exc
        raise
    low = len("".join(t for t, _b in lines).strip()) < config.OCR_RENDER_MIN_CHARS
    return lines, "page", low


def ocr_pages_text(
    path: str, page_nos: list[int], min_image_ratio: float = 0.10
) -> dict[int, str]:
    """对指定页做定向 OCR（材料可见性救援），返回 page_no → 文本；**不写库**。

    与正常解析不同：**绕过 need_ocr 阈值**。材料页常是"标题 43 字 + 占比 0.19–0.23
    的发票/合同扫描图"——字符数 >30、图片占比 <0.35，正常解析不触发 OCR，文本里
    永远没有发票内容（2026-10-21 实证：doc2 发票页 132/141/… 只有
    "8.x.2 发票 - 124 -"）。这里优先 OCR **嵌入图原图**（阈值放宽到 min_image_ratio），
    无合适图再整页渲染。
    """
    import pymupdf as fitz

    _check_file(path)
    from app.parsing.ocr import ocr_backend_available

    available, engine = ocr_backend_available()
    if not available:
        raise RuntimeError("OCR 后端不可用")
    state: dict = {"remote_ok": engine == "remote_paddle"}
    out: dict[int, str] = {}
    doc = fitz.open(path)
    try:
        for pno in page_nos:
            if pno < 1 or pno > doc.page_count:
                continue
            page = doc[pno - 1]
            lines: list[tuple[str, tuple[float, float, float, float]]] = []
            try:
                blocks = _page_image_blocks(doc, page, min_ratio=min_image_ratio)
                for rect, png in blocks:
                    try:
                        lines.extend(_ocr_image_lines(png, rect, engine, state))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("第 %d 页嵌入图 OCR 失败：%s", pno, exc)
                if not lines:
                    pix = page.get_pixmap(
                        matrix=fitz.Matrix(config.OCR_RENDER_SCALE, config.OCR_RENDER_SCALE)
                    )
                    lines = _ocr_render_lines(pix.tobytes("png"), page, engine, state)
            except Exception as exc:  # noqa: BLE001
                log.warning("第 %d 页 OCR 救援失败：%s", pno, exc)
                continue
            text = "\n".join(t for t, _b in lines if str(t or "").strip()).strip()
            if text:
                out[pno] = text
    finally:
        doc.close()
    return out


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
        ocr_available = False
        ocr_engine = config.ocr_engine()
        if ocr_budget > 0:
            # 2026-10-21 修复：必须按配置解析可用性（远程引擎探针），不能只看
            # 本地 rapidocr——旧实现在未装 rapidocr 的镜像里让 4 份标书零 OCR。
            from app.parsing.ocr import ocr_backend_available

            try:
                ocr_available, ocr_engine = ocr_backend_available()
            except Exception:  # noqa: BLE001
                ocr_available = False
        ocr_state: dict = {"remote_ok": ocr_engine == "remote_paddle"}
        if ocr_available:
            # 实际使用的引擎必须落进 warnings：这是排查"OCR 静默没跑"的唯一抓手
            warnings.append(
                f"OCR 引擎：{ocr_engine}"
                + (f"（{config.paddle_ocr_url()}）" if ocr_engine == "remote_paddle" else "")
            )
        ocr_pages = 0
        ocr_img_pages = 0
        ocr_skipped_pages = 0

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
            has_layer = bool(texts)

            need_ocr = chars < config.OCR_PAGE_MIN_CHARS or (
                chars < config.OCR_MIXED_PAGE_MAX_CHARS and _has_large_image(page)
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
                        ocr_lines, path_kind, low_yield = _ocr_page(
                            doc, page, ocr_engine, ocr_state
                        )
                        # 与文字层合并（阅读序）：文字层常含标题/页码，OCR 补扫描内容
                        merged = [(round(b[1], 1), b[0], t, b) for t, b in zip(texts, boxes)]
                        merged += [
                            (round(b[1], 1), b[0], t, b) for t, b in ocr_lines if t.strip()
                        ]
                        merged.sort(key=lambda r: (r[0], r[1]))
                        texts = [m[2] for m in merged]
                        boxes = [m[3] for m in merged]
                        page_text = "\n".join(texts)
                        source = "ocr_img" if path_kind == "img" else "ocr"
                        ocr_used = True
                        degraded = True
                        ocr_budget -= 1
                        ocr_pages += 1
                        if path_kind == "img":
                            ocr_img_pages += 1
                        if low_yield:
                            # 防"OCR 跑了但只读回页码"这类静默垃圾（旧实现无感知）
                            warnings.append(
                                f"第 {i} 页整页 OCR 产出过少（"
                                f"{len(page_text.strip())} 字），页内有大图，内容可能未识别"
                            )
                    except Exception as exc:  # noqa: BLE001 —— OCR 失败按缺字页处理
                        warnings.append(f"第 {i} 页 OCR 识别失败：{exc}")
                        degraded = True
                        if ocr_state.get("tripped"):
                            warnings.append(
                                f"远程 OCR 连续失败 {config.ocr_max_failures()} 次，"
                                "本文档余下页跳过 OCR"
                            )
                            ocr_available = False
                elif not ocr_available:
                    no_text_pages += 1
                    warnings.append(
                        f"第 {i} 页疑似扫描件（OCR 不可用：引擎 {ocr_engine}，"
                        "远程探针未通过且本地未安装 rapidocr-onnxruntime）"
                    )
                elif ocr_budget <= 0:
                    ocr_skipped_pages += 1
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

        if ocr_pages or ocr_skipped_pages:
            warnings.append(
                f"OCR 汇总：识别 {ocr_pages} 页（嵌入图原图 {ocr_img_pages} 页）"
                + (f"，超限跳过 {ocr_skipped_pages} 页" if ocr_skipped_pages else "")
            )

        if total_chars == 0 and no_text_pages == doc.page_count and doc.page_count > 0:
            raise DocumentRejectedError(
                config.REJECT_NO_TEXT_LAYER,
                "PDF 无文字层（疑似扫描件）。请配置 OCR 引擎"
                "（BID_EVAL_OCR_ENGINE=remote_paddle + 可达的 PADDLE_OCR_URL，"
                "或本地 pip install rapidocr-onnxruntime）或提供数字版文件。",
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
