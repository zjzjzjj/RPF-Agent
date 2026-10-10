"""中文条款编号识别。

优先级（深度即层级）：第X章 > 第X节/第X部分 > 第X条 > 一、 > （一） > 数字多级(2.3.1) > 1、 > （1） > ①

关键防护（来自蓝本的教训，重新实现）：
- **小数误判**："2.5 万元"不是编号——判据是 ① 数字后不能紧跟计量单位；② 多级数字的父编号必须已出现过。
- **编号重启**：出现"第X章"（或"附件"等）时重置计数上下文——附件常独立编号。
- 表格区域内的文本由调用方排除（不进入本模块）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 层级深度常量（越小越顶层）。中文编号习惯：一、 > （一） > 1. > （1） > ①
DEPTH_CHAPTER = 1
DEPTH_SECTION = 2
DEPTH_ARTICLE = 3
DEPTH_LEVEL_A = 4  # 一、
DEPTH_LEVEL_B = 5  # （一）
DEPTH_NUM_1 = 6  # 1、（比 （一） 深一层——"（三）商务分"下的"1. 类似业绩"才挂得对）
DEPTH_NUM_N = 7  # 1.1 / 1.1.1（按段数继续加深）
DEPTH_LEVEL_DIGIT_BRACKET = 8  # （1）
DEPTH_CIRCLED = 9  # ①

_CN_NUM = "一二三四五六七八九十百零〇两"

_RE_CHAPTER = re.compile(rf"^第\s*([{_CN_NUM}\d]+)\s*章[\s、.．:：]*(.*)$")
_RE_SECTION = re.compile(rf"^第\s*([{_CN_NUM}\d]+)\s*(?:节|部分)[\s、.．:：]*(.*)$")
_RE_ARTICLE = re.compile(rf"^第\s*([{_CN_NUM}\d]+)\s*条[\s、.．:：]*(.*)$")
_RE_LEVEL_A = re.compile(rf"^([{_CN_NUM}]+)\s*、\s*(.*)$")
_RE_LEVEL_B = re.compile(rf"^[（(]([{_CN_NUM}]+)[）)]\s*(.*)$")
_RE_NUM_N = re.compile(r"^(\d+(?:\.\d+)+)\s*[\s、.．:：]*(.*)$")
_RE_NUM_1 = re.compile(r"^(\d+)\s*[、.．]\s*(.*)$")
_RE_DIGIT_BRACKET = re.compile(r"^[（(](\d+)[）)]\s*(.*)$")
_RE_CIRCLED = re.compile(r"^([①-⑳])\s*(.*)$")

# 数字后紧跟这些字符说明是计量而不是编号
_UNIT_AFTER = frozenset("万元米个年月天日吨件台人分秒%％倍次项种条页位台套度级")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(text: str) -> int | None:
    """中文数字 → 整数。支持 一 / 十二 / 二十三 / 一百零三 / 一百一十 / 200 等。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            section += (number or 1) * 100
            number = 0
        else:
            return None
    return total + section + number


@dataclass
class Marker:
    kind: str  # chapter|section|article|level_a|level_b|num_n|num_1|digit_bracket|circled
    raw: str  # 原样编号文本（含"第""章"等），用于 label 与显示
    value: str  # 标准化数值文本："3"、"2.3.1"、"（一）"→"1"
    depth: int
    rest: str  # 编号之后的正文（同行剩余部分）
    restart: bool = False  # 该标记触发编号上下文重启（第X章 / 附件）


class NumberingContext:
    """逐行扫描的编号上下文：维护"已见过的编号"，供小数误判防护与重启判断。"""

    def __init__(self) -> None:
        self.seen_prefixes: set[str] = set()  # "2"、"2.3" 这样的已见多级前缀
        self.seen_chapters: set[int] = set()
        self.attachment_mode = False  # 进入附件后独立编号

    def reset(self) -> None:
        self.seen_prefixes.clear()

    def note_seen(self, value: str) -> None:
        """登记一个已出现的编号：它自身与它的各级前缀都算"已见"。

        "2.3" 已出现 ⇒ "2" 与 "2.3" 都已见 ⇒ "2.3.1" 是合法编号；
        而从未出现过的 "9.8" 不会被误认（父编号不存在）。
        """
        parts = value.split(".")
        for i in range(1, len(parts) + 1):
            self.seen_prefixes.add(".".join(parts[:i]))


def _marker_rest_ok(rest: str) -> bool:
    """数字编号的正文不能以计量单位开头（防"2.5 万元"误判）。"""
    return not (rest and rest[0] in _UNIT_AFTER)


def detect_marker(line: str, ctx: NumberingContext) -> Marker | None:
    """检测行首编号。line 应已 strip。返回 None 表示该行不是条款标题。"""
    if not line:
        return None

    if m := _RE_CHAPTER.match(line):
        value = str(cn_to_int(m.group(1)) or 0)
        return Marker("chapter", m.group(0)[: m.end(1) + 1], value, DEPTH_CHAPTER, m.group(2), restart=True)

    if m := _RE_SECTION.match(line):
        value = str(cn_to_int(m.group(1)) or 0)
        return Marker("section", f"第{m.group(1)}节", value, DEPTH_SECTION, m.group(2))

    if m := _RE_ARTICLE.match(line):
        value = str(cn_to_int(m.group(1)) or 0)
        return Marker("article", f"第{m.group(1)}条", value, DEPTH_ARTICLE, m.group(2))

    if m := _RE_LEVEL_A.match(line):
        value = str(cn_to_int(m.group(1)) or 0)
        return Marker("level_a", m.group(1), value, DEPTH_LEVEL_A, m.group(2))

    if m := _RE_LEVEL_B.match(line):
        value = str(cn_to_int(m.group(1)) or 0)
        return Marker("level_b", f"（{m.group(1)}）", value, DEPTH_LEVEL_B, m.group(2))

    if m := _RE_NUM_N.match(line):
        value = m.group(1)
        rest = m.group(2)
        if not _marker_rest_ok(rest):
            return None
        parent = value.rsplit(".", 1)[0]
        if parent not in ctx.seen_prefixes and not ctx.attachment_mode:
            # 父编号必须已出现，否则可能是"2.5 万元"这类小数
            return None
        depth = DEPTH_NUM_N + value.count(".")
        return Marker("num_n", value, value, depth, rest)

    if m := _RE_NUM_1.match(line):
        value = m.group(1)
        rest = m.group(2)
        if not _marker_rest_ok(rest):
            return None
        # "1.5" 之类会被 _RE_NUM_N 处理；此处是 "1、" 或 "1."
        return Marker("num_1", m.group(0)[: m.end(1) + 1], value, DEPTH_NUM_1, rest)

    if m := _RE_DIGIT_BRACKET.match(line):
        return Marker("digit_bracket", f"（{m.group(1)}）", m.group(1), DEPTH_LEVEL_DIGIT_BRACKET, m.group(2))

    if m := _RE_CIRCLED.match(line):
        return Marker("circled", m.group(1), m.group(1), DEPTH_CIRCLED, m.group(2))

    return None


# 目录页判据（chapter_tree 使用）
_RE_LEADER = re.compile(r"[.·…]{3,}\s*\d{1,4}\s*$")  # "……12" / "....12"
_RE_TRAILING_PAGE = re.compile(r"\s\d{1,4}\s*$")  # 行尾裸页码


def looks_like_toc(lines: list[str]) -> bool:
    """目录页判据：编号标记密集 + 每标记正文极短 + 多数行以点线/页码结尾。

    只看"标记密集"会把正常的条款列表页（如资格要求 4 条 + 编制要求 3 条）误判为目录——
    实测教训。真正的目录条目以页码结尾，这是内容页不具备的特征。
    """
    ctx = NumberingContext()
    marker_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        m = detect_marker(stripped, ctx) or _lenient_marker(stripped)
        if m is not None:
            marker_lines.append(stripped)
    if len(marker_lines) < 8:
        return False
    leader_count = sum(
        1 for s in marker_lines if _RE_LEADER.search(s) or _RE_TRAILING_PAGE.search(s)
    )
    return leader_count >= len(marker_lines) * 0.5


def _lenient_marker(line: str) -> Marker | None:
    """目录页专用：跳过父编号存在性检查。"""
    if m := _RE_NUM_N.match(line):
        return Marker("num_n", m.group(1), m.group(1), DEPTH_NUM_N + m.group(1).count("."), m.group(2))
    return None
