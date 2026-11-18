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


def test_wide_row_table_splits_horizontally(monkeypatch) -> None:  # noqa: ANN001
    """点检宽表：一行几百个 ✓ 列，竖切到底仍截断 → 横切按列读全（run128 p508）。"""

    def fake_post(png_bytes, prompt, max_tokens):  # noqa: ANN001, ANN202
        img = Image.open(io.BytesIO(png_bytes))
        if img.size[0] > 400:
            return _reply("（截断）", truncated=True)
        return _reply(f"[宽{img.size[0]}]")

    monkeypatch.setattr(paddle_client, "_post", fake_post)
    out = paddle_client._ocr_segmented(_png(800, 20), "OCR:")
    # 800×20 → 横切两段各 440 宽（仍截断）→ 再横切各 242 宽 → 读全，共 4 段
    assert out.count("[宽") == 4 and "[宽242]" in out, out


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


def test_loop_detection_fails_fast_on_truncated(monkeypatch) -> None:  # noqa: ANN001
    """VL token 循环铁证（run128 p508 y=330 "0.006 0.006…"）：截断输出结尾
    连续 15+ 次同一含数字单元 → 快速失败转本地，循环文本绝不当读取结果。"""
    import pytest

    looped = "真实开头两行内容\n" + "0.006 " * 200
    monkeypatch.setattr(paddle_client, "_post",
                        lambda *a, **k: _reply(looped, truncated=True))
    with pytest.raises(RuntimeError, match="复读循环"):
        paddle_client.remote_ocr_page(_png(300, 400), prompt="OCR:")


def test_alternating_repetition_not_flagged(monkeypatch) -> None:  # noqa: ANN001
    """交替值重复（p508 y=220 "港水号：A/B 交替"）不是铁证——交递归细分，
    子段正常即读全（宁可漏不误杀）。"""
    alt = ("型号 | 港水号：2401139\n型号 | 港水号：0116-12\n" * 30)

    def fake(png_bytes, prompt, max_tokens):  # noqa: ANN001, ANN202
        img = Image.open(io.BytesIO(png_bytes))
        return _reply(alt, truncated=True) if img.size[1] > 150 else _reply("子段正常读全")

    monkeypatch.setattr(paddle_client, "_post", fake)
    out = paddle_client.remote_ocr_page(_png(300, 400), prompt="OCR:")
    assert out.count("子段正常读全") == 4


def test_header_repetition_at_one_level_resolves_by_splitting(monkeypatch) -> None:  # noqa: ANN001
    """p254 场景：表头重复（含 'PR #' 拉丁）只在粗段疑似循环，子段正常
    → 必须递归读全，不得判死（两击判据的第一击只续切不放弃）。"""
    header_looped = ("机器编号: PR # | 安装位置: 胶印车间 | 机器编号: PR # | "
                     "安装位置: 胶印车间\n") * 12  # 尾部高频重复 → 疑似循环

    def fake(png_bytes, prompt, max_tokens):  # noqa: ANN001, ANN202
        img = Image.open(io.BytesIO(png_bytes))
        if img.size[1] > 150:
            return _reply(header_looped, truncated=True)
        return _reply(f"[{img.size[1]}行]正常读全的表格行内容")

    monkeypatch.setattr(paddle_client, "_post", fake)
    out = paddle_client.remote_ocr_page(_png(300, 400), prompt="OCR:")
    assert "正常读全" in out and out.count("正常读全") == 4, out


def test_glyph_run_repetition_is_not_loop(monkeypatch) -> None:  # noqa: ANN001
    """勾选表「✓ | 」×数百是合法表格内容（p254 误杀实证）：不判循环——
    截断时照常递归细分读全，完整时直接返回。"""
    glyph_run = "设备点检表\n" + "✓ | " * 200
    # 截断 → 递归 → 子段完整 → 读全
    calls = []

    def fake(png_bytes, prompt, max_tokens):  # noqa: ANN001, ANN202
        img = Image.open(io.BytesIO(png_bytes))
        calls.append(img.size[1])
        return _reply("✓ | ✓ | ✓", truncated=img.size[1] > 150)

    monkeypatch.setattr(paddle_client, "_post", fake)
    out = paddle_client.remote_ocr_page(_png(300, 400), prompt="OCR:")
    assert out and "✓" in out
    assert len(calls) > 1, "字形重复不得触发快速失败，应递归细分"
    # 完整输出的重复直接返回
    monkeypatch.setattr(paddle_client, "_post",
                        lambda *a, **k: _reply(glyph_run))
    assert "✓" in paddle_client.remote_ocr_page(_png(300, 400), prompt="OCR:")
