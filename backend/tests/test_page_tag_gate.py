# -*- coding: utf-8 -*-
"""评分读页的标签噪声闸门（2026-11-18 用户指令：解析期标签评分复用）。

只在标签**明确命中**结构性噪声（目录/空白/分隔页）时跳过 OCR 补读——
未打标/拿不准的照读（正向判定，不猜）；封面类保留（公章/投标人标识，
签章与身份核验要用）。
"""
from __future__ import annotations

from app.services.automatic_scoring_service import _is_noise_page


def test_noise_tags_skip() -> None:
    assert _is_noise_page({"material_tags": ["toc"]})
    assert _is_noise_page({"material_tags": ["发票", "目录"]})
    assert _is_noise_page({"material_tags": ["空白页"]})


def test_relevant_or_unknown_pages_not_skipped() -> None:
    assert not _is_noise_page({"material_tags": ["发票"]})
    assert not _is_noise_page({"material_tags": ["使用记录"]})  # 设备记录=保养程序证据
    assert not _is_noise_page({"material_tags": []})
    assert not _is_noise_page({"material_tags": ["文件封面"]})  # 封面保留（公章/身份）
    assert not _is_noise_page({})
