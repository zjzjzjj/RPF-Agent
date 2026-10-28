# -*- coding: utf-8 -*-
"""OCR 格式标签清理（共享模块）——招标编译与投标评分链路同口径。"""
from __future__ import annotations

from app.parsing.ocr_tags import clean_ocr_tags


def test_clean_ocr_tags_strips_cell_markers():
    raw = "1 2<fcel>许灿晖<fcel>46.42<lcel>项目经理：<ucel>蒋立华<nl>第二行"
    out = clean_ocr_tags(raw)
    assert "<fcel>" not in out and "<lcel>" not in out
    assert "<nl>" not in out and "<ucel>" not in out
    assert "许灿晖" in out and "蒋立华" in out
    # 标签换为空格，不粘连
    assert "1 2 许灿晖 46.42" in out


def test_clean_ocr_tags_noop_on_plain_text():
    plain = "投标人业绩：每提供 1 个类似项目业绩得 2 分。"
    assert clean_ocr_tags(plain) == plain
    assert clean_ocr_tags("") == ""
    assert clean_ocr_tags(None) == ""  # type: ignore[arg-type]
