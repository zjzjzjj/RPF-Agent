# -*- coding: utf-8 -*-
"""材料长图拼接（2026-11-23 优化② VL 兜底基建）。

分段策略：≤max_pages_per_segment 页/段，尾段 <min_pages 并入前段；
拼接缓存复用（二次调用不再渲染页）。
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest


@pytest.fixture()
def _render_env(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    from app import config
    from app.evidence import renderer

    monkeypatch.setattr(config, "render_dir", lambda: tmp_path / "render")
    calls: list[int] = []

    def fake_render_page_png(sha256, stored_path, page_no, scale=None, **kw):  # noqa: ANN001, ANN202
        from PIL import Image

        calls.append(page_no)
        png = tmp_path / f"page{page_no}.png"
        if not png.exists():
            Image.new("RGB", (20, 10 + page_no % 3), (page_no * 7 % 255, 128, 64)).save(png)
        return png

    monkeypatch.setattr(renderer, "render_page_png", fake_render_page_png)
    return calls


def test_long_image_segments_35_pages(_render_env) -> None:  # noqa: ANN001
    from app.evidence.renderer import render_long_image

    segs = render_long_image("sha-demo", "x.pdf", list(range(1, 36)),
                             max_pages_per_segment=30, min_pages=5)
    assert [len(s["page_numbers"]) for s in segs] == [30, 5]
    assert segs[0]["page_numbers"][0] == 1 and segs[1]["page_numbers"][-1] == 35
    for s in segs:
        assert Path(s["image_path"]).exists()
        assert s["data_url"].startswith("data:image/png;base64,")
        assert s["image_sha256"]


def test_long_image_small_doc_single_segment(_render_env) -> None:  # noqa: ANN001
    from app.evidence.renderer import render_long_image

    segs = render_long_image("sha-demo", "x.pdf", [3, 1, 2],
                             max_pages_per_segment=30, min_pages=5)
    assert len(segs) == 1
    assert segs[0]["page_numbers"] == [1, 2, 3], "页序按文档序"


def test_long_image_tail_merged_into_previous(_render_env) -> None:  # noqa: ANN001
    from app.evidence.renderer import render_long_image

    # 33 页 → [30, 3]，尾段 3 < 5 → 并入前段（宁可单段 33，不产碎片小图）
    segs = render_long_image("sha-demo", "x.pdf", list(range(1, 34)),
                             max_pages_per_segment=30, min_pages=5)
    assert len(segs) == 1
    assert len(segs[0]["page_numbers"]) == 33


def test_long_image_cache_reuse(_render_env) -> None:  # noqa: ANN001
    from app.evidence.renderer import render_long_image

    segs1 = render_long_image("sha-demo", "x.pdf", list(range(1, 11)),
                              max_pages_per_segment=30, min_pages=5)
    n = len(_render_env)
    segs2 = render_long_image("sha-demo", "x.pdf", list(range(1, 11)),
                              max_pages_per_segment=30, min_pages=5)
    assert len(_render_env) == n, "拼接缓存命中，不应再渲染页"
    assert [s["image_sha256"] for s in segs1] == [s["image_sha256"] for s in segs2]


def test_long_image_empty_pages(_render_env) -> None:  # noqa: ANN001
    from app.evidence.renderer import render_long_image

    assert render_long_image("sha-demo", "x.pdf", []) == []
