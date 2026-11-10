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


def ocr_backend_available() -> tuple[bool, str]:
    """解析层可用的 OCR 后端 → (可用?, 引擎名)。

    2026-10-21 故障修复：旧实现只看本地 rapidocr（`get_ocr() is not None`），
    完全忽略 `BID_EVAL_OCR_ENGINE=remote_paddle`——镜像未装 rapidocr 时
    4 份标书整体零 OCR，扫描件（证书/查询截图/合同发票验收）全部丢失，
    却只在 warnings 里写一句"OCR 组件未安装"。这里改为按配置解析：
    远程引擎探针通过即视为可用，探针不过再退回本地 rapidocr。
    """
    from app import config

    engine = config.ocr_engine()
    if engine == "remote_paddle":
        from app.parsing import paddle_client

        if paddle_client.probe():
            return True, "remote_paddle"
    if get_ocr() is not None:
        return True, "rapidocr"
    return False, engine
