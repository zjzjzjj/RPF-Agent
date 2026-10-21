"""PaddleOCR-VL 远程解析客户端（mwb 自部署服务，vLLM OpenAI 兼容协议）。

服务来源：mwb 的 coll-det-gpu-ocr-proxy（vLLM serve PaddleOCR-VL）。
协议：POST /v1/chat/completions，messages 含 image_url（页面 PNG data URL）+
文本提示，模型整页返回文档文本（无 bbox）。

调用方（pdf_pymupdf）拿整页文本后按"单图一块行"形态融入现有行模型，
坐标退化说明见该处注释。

2026-10-21 修复：可用性判定必须覆盖远程引擎（旧实现只看本地 rapidocr，
镜像未装 rapidocr 时 4 份标书整体零 OCR，扫描件全部丢失）。
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request

from app import config

log = logging.getLogger("bid_eval.parsing.paddle")

_PROBE_OK: bool | None = None  # 成功永久缓存
_PROBE_FAILED_AT: float | None = None  # 失败 30s TTL（防一次抖动让整批文档零 OCR）
_PROBE_FAIL_TTL_S = 30.0


def _chat_payload(png_bytes: bytes, prompt: str) -> dict:
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return {
        "model": config.paddle_ocr_model(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": config.paddle_ocr_max_tokens(),
    }


def probe(*, timeout: float | None = None) -> bool:
    """服务可用性探针：GET /v1/models（毫秒级，不触发推理）。

    成功结果进程级永久缓存；失败结果缓存 30s——既避免每次解析都探一次，
    也避免把一次瞬时失败固化成本进程"永久不可用"。
    """
    global _PROBE_OK, _PROBE_FAILED_AT
    if _PROBE_OK:
        return True
    now = time.monotonic()
    if _PROBE_FAILED_AT is not None and now - _PROBE_FAILED_AT < _PROBE_FAIL_TTL_S:
        return False
    url = config.paddle_ocr_url().rstrip("/") + "/v1/models"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(
            request, timeout=timeout or config.paddle_ocr_probe_timeout_s()
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ok = bool(payload.get("data"))
    except Exception as exc:  # noqa: BLE001 —— 探针失败一律按不可用处理
        log.warning("PaddleOCR-VL 探针失败（%s）：%s", url, exc)
        ok = False
    if ok:
        _PROBE_OK = True
        _PROBE_FAILED_AT = None
    else:
        _PROBE_FAILED_AT = now
    return ok


def reset_probe_cache() -> None:
    """测试/排障用：清空探针缓存。"""
    global _PROBE_OK, _PROBE_FAILED_AT
    _PROBE_OK = None
    _PROBE_FAILED_AT = None


def remote_ocr_page(
    png_bytes: bytes, *, prompt: str | None = None, allow_truncated: bool = False
) -> str:
    """单页 PNG → PaddleOCR-VL 整页文本。失败抛 RuntimeError（调用方回退 rapidocr）。

    `allow_truncated=True`（材料页救援用）：输出被 max_tokens 截断时返回**已识别
    部分**（发票号/甲乙方等材料信号通常在前半段），而不是丢弃。正常解析仍保持
    严格（截断=不可信，抛错由调用方记警告）。
    """
    try:
        answer = _post(png_bytes, prompt, config.paddle_ocr_max_tokens())
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise RuntimeError(f"PaddleOCR-VL 调用失败：{exc}") from exc
        # 输出预算超过服务端上下文（max_model_len）时 vLLM 直接 400——降半重试一次
        retry_budget = max(512, config.paddle_ocr_max_tokens() // 2)
        if retry_budget >= config.paddle_ocr_max_tokens():
            raise RuntimeError(f"PaddleOCR-VL 调用失败：{exc}") from exc
        try:
            answer = _post(png_bytes, prompt, retry_budget)
        except (OSError, ValueError, urllib.error.URLError) as exc2:
            raise RuntimeError(f"PaddleOCR-VL 调用失败：{exc2}") from exc2
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError(f"PaddleOCR-VL 调用失败：{exc}") from exc
    try:
        choice = answer["choices"][0]
        text = str(choice["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"PaddleOCR-VL 响应缺少内容：{str(answer)[:200]}") from exc
    if choice.get("finish_reason") == "length":
        # 旧实现忽略 finish_reason：密集扫描页被静默截断，内容丢失却无人知晓。
        # 救援模式接受部分输出（材料信号通常在前半段），正常解析仍抛错。
        if not allow_truncated:
            raise RuntimeError("PaddleOCR-VL 输出被 max_tokens 截断")
    return text.strip()


def _post(png_bytes: bytes, prompt: str | None, max_tokens: int) -> dict:
    payload = _chat_payload(png_bytes, prompt or config.paddle_ocr_prompt())
    payload["max_tokens"] = max_tokens
    url = config.paddle_ocr_url().rstrip("/") + "/v1/chat/completions"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=config.paddle_ocr_timeout_s()) as response:
        return json.loads(response.read().decode("utf-8"))
