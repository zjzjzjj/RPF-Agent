"""文本归一化——证据定位与证据核验**共用同一实现**。

如果核验用一套规则、定位用另一套，就会出现"核验通过但定位不到"的漂移。
因此这里提供：
- `normalize(text)`：全角→半角、中文引号→英文、空白折叠、去零宽字符。
- `normalize_with_map(text)`：同上，并给出 归一化下标 → 原文下标 的映射，
  定位算法靠它把命中位置映射回原文坐标，再映射到 bbox。
"""
from __future__ import annotations

_ZERO_WIDTH = frozenset("​‌‍⁠﻿")

_QUOTE_MAP = {"“": '"', "”": '"', "‘": "'", "’": "'"}
# 乘号变体：模型转写时 × 常写成 x，核验时视为同一字符（"40×基准价" vs "40x基准价"）
_MULTIPLY_MAP = {"×": "x", "✕": "x", "⨯": "x", "╳": "x"}
_FULLWIDTH_START = 0xFF01
_FULLWIDTH_END = 0xFF5E
_FULLWIDTH_OFFSET = 0xFEE0


def _map_char(ch: str) -> str:
    if ch in _ZERO_WIDTH:
        return ""
    if ch in _QUOTE_MAP:
        return _QUOTE_MAP[ch]
    if ch in _MULTIPLY_MAP:
        return _MULTIPLY_MAP[ch]
    o = ord(ch)
    if _FULLWIDTH_START <= o <= _FULLWIDTH_END:
        return chr(o - _FULLWIDTH_OFFSET)
    if ch == "　":  # 全角空格
        return " "
    if ch == "〜" or ch == "～":  # 波浪线变体
        return "~"
    if ch.isspace():
        return " "
    return ch


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """返回 (归一化文本, 映射表)。映射表第 i 项 = 归一化文本第 i 个字符的原文下标。

    空白折叠：连续空白（含换行）折叠为一个空格，映射取该段第一个字符的位置。
    """
    out: list[str] = []
    index_map: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        mapped = _map_char(ch)
        if mapped == "":
            continue
        if mapped == " ":
            if prev_space:
                continue
            prev_space = True
        else:
            prev_space = False
        out.append(mapped)
        index_map.append(i)
    # 去掉首尾空白（避免匹配时把边界空白计入）
    start = 0
    while start < len(out) and out[start] == " ":
        start += 1
    end = len(out)
    while end > start and out[end - 1] == " ":
        end -= 1
    return "".join(out[start:end]), index_map[start:end]


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


def normalize_no_ws_with_map(text: str) -> tuple[str, list[int]]:
    """在 normalize 基础上**去掉全部空白**（含折叠出的单空格）。

    中文文档里软换行会变成空格，而模型引文通常跨行逐字复制（不含空格）。
    这一级用于"空白不敏感"的证据核对：中文无词间空格，去掉空白不改变语义，
    但能消除排版换行造成的假不匹配。
    """
    norm, index_map = normalize_with_map(text)
    chars: list[str] = []
    idx: list[int] = []
    for ch, original_index in zip(norm, index_map):
        if ch == " ":
            continue
        chars.append(ch)
        idx.append(original_index)
    return "".join(chars), idx


def normalize_for_match(text: str) -> str:
    """核验用的宽松形态：再折叠大小写（英文），中文不受影响。"""
    return normalize(text).casefold()
