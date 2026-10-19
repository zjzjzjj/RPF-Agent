"""PaddleOCR-VL 远程解析客户端（mwb 自部署服务，vLLM OpenAI 兼容协议）。

服务来源：mwb 的 coll-det-gpu-ocr-proxy（GPU 5 号卡，vLLM serve PaddleOCR-VL）。
协议：POST /v1/chat/completions，messages 含 image_url（页面 PNG data URL）+
文本提示，模型整页返回文档文本（无 bbox）。

调用方（pdf_pymupdf._ocr_page）拿整页文本后按"单条全页行"形态融入现有行模型，
坐标退化说明见该处注释。
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

from app import config

log = logging.getLogger("bid_eval.parsing.paddle")

_TIMEOUT = 300  # PaddleOCR-VL 单页推理在 GPU 上数秒~数十秒，留足余量


def _chat_payload(png_bytes: bytes, prompt: str) -> dict:
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return {
        "model": "PaddleOCR-VL-1.6-0.9B",
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
        "max_tokens": 4096,
    }


def remote_ocr_page(png_bytes: bytes, *, prompt: str | None = None) -> str:
    """单页 PNG → PaddleOCR-VL 整页文本。失败抛 RuntimeError（调用方回退 rapidocr）。"""
    url = config.paddle_ocr_url().rstrip("/") + "/v1/chat/completions"
    body = json.dumps(_chat_payload(png_bytes, prompt or "OCR:")).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError(f"PaddleOCR-VL 调用失败：{exc}") from exc
    try:
        text = str(answer["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"PaddleOCR-VL 响应缺少内容：{str(answer)[:200]}") from exc
    return text.strip()
