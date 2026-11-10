# -*- coding: utf-8 -*-
"""OCR 格式标签清理（PaddleOCR-VL 表格输出）。

PaddleOCR-VL 对表格型图像会**自发**输出单元格标签（即使 prompt 只是 "OCR:"）——
服务器 ocr_cache 实证 7.7%（129/1673）输出含 `<fcel>/<lcel>/<nl>/<ucel>`。
这些标签必须在**所有**文本处理前清理，否则：
- 投标评分链路："项目经理：蒋立华"被标签打断 → 引文定位失败 → 0 分；
- 招标编译链路：标签污染选段/LLM 抽取/quote 核验。

两处调用方共享本模块，口径唯一。
"""
from __future__ import annotations

import re

_RE_OCR_TAGS = re.compile(r"</?(?:fcel|lcel|nl|ucel)[^>]*>")


def clean_ocr_tags(text: str) -> str:
    """去掉 OCR 格式标签，保留文本内容。"""
    return _RE_OCR_TAGS.sub(" ", text or "")
