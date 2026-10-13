"""OCR 隔离层：rapidocr-onnxruntime 的惰性加载与统一调用。

- 依赖未安装 → get_ocr() 返回 None（调用方据此报"OCR 组件未安装"，不崩溃）；
- 模型随 wheel 内置，离线可用；进程级单例（模型加载约数百 MB，只做一次）；
- recognize 返回 [(文本, 轴对齐 bbox 像素坐标)]，坐标换算由调用方负责。
"""
from __future__ import annotations

import logging

log = logging.getLogger("bid_eval.parsing.ocr")

_ocr = None
_loaded = False


def get_ocr():
    """惰性获取 RapidOCR 实例；未安装返回 None。"""
    global _ocr, _loaded
    if _loaded:
        return _ocr
    try:
        from rapidocr_onnxruntime import RapidOCR

        _ocr = RapidOCR()
        log.info("RapidOCR 已加载（rapidocr-onnxruntime）")
    except Exception as exc:  # noqa: BLE001 —— ImportError 及模型初始化失败统一降级
        log.warning("OCR 组件不可用：%s", exc)
        _ocr = None
    _loaded = True
    return _ocr


def recognize(img) -> list[tuple[str, tuple[float, float, float, float]]]:  # noqa: ANN001
    """图片（numpy 数组，BGR）→ [(文本, 轴对齐 bbox)]。无 OCR/无结果返回空列表。"""
    ocr = get_ocr()
    if ocr is None:
        return []
    result, _elapse = ocr(img)
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    for row in result or []:
        text = str(row[1] or "").strip()
        if not text:
            continue
        xs = [p[0] for p in row[0]]
        ys = [p[1] for p in row[0]]
        out.append((text, (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))))
    return out
