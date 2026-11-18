# -*- coding: utf-8 -*-
"""PaddleOCR-VL 分段识别递归化（2026-11-18 run128 实证）。

密集文字页（6000+ 字）单层对半仍被 max_tokens 截断 → 硬失败回退本地 CPU OCR
（149 页把 675 格评分拖成 30+ 小时）。递归细分（深度≤3，最多八分）让密集页
走完远程快通道；严格完整性不变——任何最终分段仍截断/空内容都明确失败，
部分文本绝不冒充完整读取。
"""
from __future__ import annotations

import io

from PIL import Image

from app.parsing import paddle_client


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def _reply(text: str, truncated: bool = False) -> dict:
    return {"choices": [{"message": {"content": text},
                         "finish_reason": "length" if truncated else "stop"}]}


def test_dense_page_recurses_until_segments_fit(monkeypatch) -> None:  # noqa: ANN001
    """整页截断（调用方已确认）→ 对半仍截断 → 四分后读全 → 拼接完整。"""
    calls: list[int] = []

    def fake_post(png_bytes, prompt, max_tokens):  # noqa: ANN001, ANN202
        img = Image.open(io.BytesIO(png_bytes))
        calls.append(img.size[1])
        if img.size[1] > 150:
            return _reply("（截断）", truncated=True)
        return _reply(f"[{img.size[1]}行]")

    monkeypatch.setattr(paddle_client, "_post", fake_post)
    out = paddle_client._ocr_segmented(_png(300, 400), "OCR:")
    # h=400 → 两半各 220（5% 重叠）→ 仍截断 → 四分各 121 → 读全
    assert out == "[121行]\n[121行]\n[121行]\n[121行]", out
    assert max(calls) <= 220, "首轮应从对半开始（整页截断已由调用方确认，不重发整页）"


def test_depth_limit_still_fails_loudly(monkeypatch) -> None:  # noqa: ANN001
    """任何深度都截断 → 明确失败（绝不输出部分文本冒充完整读取）。"""
    monkeypatch.setattr(paddle_client, "_post",
                        lambda *a, **k: _reply("（截断）", truncated=True))
    import pytest

    with pytest.raises(RuntimeError, match="仍被 max_tokens 截断"):
        paddle_client._ocr_segmented(_png(300, 400), "OCR:")


def test_empty_segment_fails_loudly(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(paddle_client, "_post", lambda *a, **k: _reply("   "))
    import pytest

    with pytest.raises(RuntimeError, match="未产生内容"):
        paddle_client._ocr_segmented(_png(300, 400), "OCR:")
