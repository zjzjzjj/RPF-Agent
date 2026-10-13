"""投标事实抽取：从投标文件文本中确定性地提取评分所需事实。

MVP 用确定性正则（对结构化程度较高的标书命中率足够，且全程可复算）；
真实场景的长尾格式后续接 LLM 两段式抽取——抽取接口不变，只换实现。

设计约束：这里抽出的只是"事实"（报价是多少、业绩有几个），**判定**由
objective/primitives 做；事实与判定分离才能保证"客观分可复算一致"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

_UPPER_DIGITS = {"零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9}
_RUN_UPPER = re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元整角分]{4,}")
# 行首：前导分隔符* + 可选编号 + 后随分隔符*。
# 兼容 "1 某项目" / "1. 某项目" / "1、某项目" / "（1）某项目" / "| 1 | 某项目"。
_SEP = r"(?:[|｜]|\s)"
_ROW_PREFIX = r"[|｜、.．\s）)]*(?:[（(]?\d+[）)]?)?[|｜、.．\s）)]*"
_RE_PRICE_TOTAL_ROW = re.compile(
    r"投标总价\s*[：:（(]*(?:大写|小写)?[）)]?\s*[|｜]?\s*([\d,，]+(?:\.\d+)?)"
)
_RE_PRICE_SYMBOL = re.compile(r"[¥￥]\s*([\d,，]+(?:\.\d+)?)")
_RE_PRICE_TOTAL_CN = re.compile(r"投标总价人民币([零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元整]+)")
_RE_ITEMS_TOTAL_ROW = re.compile(r"^\s*[|｜]?\s*合计\s*[|｜]?\s*([\d,，]+(?:\.\d+)?)", re.M)
_RE_ITEM_ROW = re.compile(
    r"^\s*" + _ROW_PREFIX + r"\s*(.{2,60}?)\s*" + _SEP + r"\s*([\d,，]+(?:\.\d+)?)\s*"
    + _SEP + r"\s*(\d{4})\s*年",
    re.M,
)
_RE_CAPITAL = re.compile(r"注册资本[：:]*人民币?([\d,，]+(?:\.\d+)?)\s*万元")
_RE_CERT_ROW = re.compile(
    r"^\s*" + _ROW_PREFIX + r"\s*((?:ISO\s?\d+|CMMI\s?\d?)[^\n|｜]{0,60})", re.M
)
# 证书行里的证书编号/有效期（用于把名称与编号切开）
_RE_CERT_TAIL = re.compile(r"\s+[A-Z][A-Za-z]{1,8}[-–]\d|\s+\d{4}\s*年")
_RE_CERT_CLAIM = re.compile(r"已通过\s*((?:ISO\s?\d+|CMMI\s?\d?)[^，。；；()（）]{0,30})")
_RE_QUAL_CERT = re.compile(r"信息系统集成及服务资质")
# 保证金：否定语境不算已缴纳（"未缴纳/无需缴纳"不得因含"缴纳"而命中）
_RE_GUARANTEE = re.compile(r"(?<![未无需不予])缴纳\s*投标\s*保证金")
_RE_WAN_UNIT = re.compile(r"万元")


def parse_upper_amount(text: str) -> Decimal | None:
    """大写金额 → Decimal。

    "柒佰陆拾万元整" → 7,600,000；"柒佰零伍万元整" → 7,050,000；
    "壹万贰仟叁佰肆拾伍元陆角柒分" → 12,345.67（角/分按小数处理，不丢精度）。
    """
    m = _RUN_UPPER.search(text)
    if not m:
        return None
    run = m.group(0)
    # 角/分单独处理（1 角 = 0.1 元，1 分 = 0.01 元），整数部分在 元/圆 处截断
    fraction = Decimal(0)
    fm = re.search(r"[圆元]?(?:([零壹贰叁肆伍陆柒捌玖])角)?(?:([零壹贰叁肆伍陆柒捌玖])分)?[整]?$", run)
    if fm and (fm.group(1) or fm.group(2)):
        if fm.group(1):
            fraction += Decimal(_UPPER_DIGITS.get(fm.group(1), 0)) / 10
        if fm.group(2):
            fraction += Decimal(_UPPER_DIGITS.get(fm.group(2), 0)) / 100
    integer_part = re.split(r"[圆元]", run)[0]
    run = integer_part
    total = 0
    section = 0
    number = 0
    saw_digit = False
    for ch in run:
        if ch in _UPPER_DIGITS:
            number = _UPPER_DIGITS[ch]
            saw_digit = True
        elif ch == "拾":
            section += (number or 1) * 10
            number = 0
        elif ch == "佰":
            section += (number or 1) * 100
            number = 0
        elif ch == "仟":
            section += (number or 1) * 1000
            number = 0
        elif ch == "万":
            section = (section + number) * 10_000
            total += section
            section = 0
            number = 0
        elif ch == "亿":
            total = (total + section + number) * 100_000_000
            section = 0
            number = 0
    value = total + section + number
    if not saw_digit:
        return None
    return Decimal(value) + fraction


def _money(s: str) -> Decimal:
    return Decimal(s.replace(",", "").replace("，", ""))


def _contextual_price(text: str) -> Decimal | None:
    """¥ 回退必须带语境：优先取"投标总价/投标报价/报价"附近的金额。

    裸取全文第一个 ¥ 会把保证金（¥50,000）当成投标报价——那种错误
    会直接污染价格分这个最敏感的确定性输入。兜底顺序：
    ① 报价语境附近 → ② 非保证金/押金语境且非零的金额 → ③ 放弃（宁缺勿错，
    人工可在报价确认卡点手工录入）。
    """
    first_non_deposit: Decimal | None = None
    for m in _RE_PRICE_SYMBOL.finditer(text):
        value = _money(m.group(1))
        context = text[max(0, m.start() - 40) : m.start()]
        if any(k in context for k in ("投标总价", "投标报价", "总报价", "报价")):
            return value
        if first_non_deposit is None and value != 0 and not any(
            k in context for k in ("保证金", "押金", "税", "利息")
        ):
            first_non_deposit = value
    # 宁缺勿错：没有报价语境、且候选都被排除时返回 None（人工在确认卡点录入）
    return first_non_deposit


# ---------------------------------------------------------------------------
# 第 5 级兜底：开标一览表窗口扫描（真实标书的表头跨行断裂形态，2026-10-13）
# 实测《北京先进数通投标文件.pdf》p303：表头断成 3 行、数据行断成 2 行，
# 四级标签-金额邻近正则全灭。mwb 借鉴（xlsx_parser PRICE_KEYWORDS +
# signal_runners._AMOUNT_RE 的 ≥10000 元阈值）：
# "开标一览表"章节锚点 + 2000 字符窗口 + 语境词预筛 + 税率显式配对判含税价。
# ---------------------------------------------------------------------------
_RE_OPEN_BID_HEADING = re.compile(r"开标一览表|开标一览|报价一览表")
_PRICE_SECTION_WINDOW = 2000
_RE_MONEY_LIKE = re.compile(
    r"(?<![\d.a-zA-Z])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{5,}(?:\.\d+)?)(?![\d.]*%[^\d]|[\d.]*%\s)"
)
_PRICE_CONTEXT_WORDS = ("投标价", "投标报价", "报价", "总价", "含增值税", "含税")
_RE_TAX_RATE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%")
_RE_DATE_CONTEXT = re.compile(r"\d{4}\s*年|\d{1,2}\s*月|\d{4}[-/]\d")


def _money_candidates(window: str) -> list[Decimal]:
    """窗口内金额形态候选（≥10000；排除税率/日期/小额序号）。"""
    out: list[Decimal] = []
    for m in _RE_MONEY_LIKE.finditer(window):
        raw = m.group(1).replace(",", "")
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        if value < 10_000:
            continue
        # 日期连写（20240601 形态或紧邻年月日）
        ctx = window[max(0, m.start() - 6): m.end() + 6]
        if _RE_DATE_CONTEXT.search(ctx):
            continue
        out.append(value)
    return out


def _tax_paired_price(window: str, candidates: list[Decimal]) -> Decimal | None:
    """显式配对："(b - a) / a ≈ 税率%" → 返回含税价 b。p303 实测
    2545211.58 / 2401143 - 1 = 6.03%（6% 税率，0.5% 容差内命中）。"""
    head = window[:400]
    has_excl = ("不含增值税" in head or "不含税" in head)
    has_incl = ("含增值税" in head or "含税" in head)
    if not (has_excl and has_incl):
        return None
    rates = [Decimal(m.group(1)) for m in _RE_TAX_RATE.finditer(head)
             if Decimal(m.group(1)) < 50]
    if not rates:
        return None
    for b in candidates:
        for a in candidates:
            if a >= b:
                continue
            try:
                implied = (b - a) / a * 100
            except InvalidOperation:
                continue
            if any(abs(implied - r) <= Decimal("0.5") for r in rates):
                return b
    return None


def _price_from_open_bid_section(text: str) -> Decimal | None:
    """开标一览表章节锚点 + 2000 字符窗口扫描。多锚点择优
    （得分 = 语境词命中 + 显式配对×2；并列取更靠后锚点——真章节在目录之后）。"""
    best: tuple[int, Decimal] | None = None
    for m in _RE_OPEN_BID_HEADING.finditer(text):
        window = text[m.start(): m.start() + _PRICE_SECTION_WINDOW]
        if not any(w in window for w in _PRICE_CONTEXT_WORDS):
            continue  # 目录假锚点
        # 跨行断裂的表头拼接——行间必须插分隔符：行尾数字与下一行行首数字
        # 无缝相连会拼出假金额（实测 2000000.00\n10000 → 12000000.00）
        window_joined = " ".join(window.split("\n"))
        candidates = _money_candidates(window_joined) or _money_candidates(window)
        if not candidates:
            continue
        paired = _tax_paired_price(window_joined, candidates)
        score = sum(1 for w in _PRICE_CONTEXT_WORDS if w in window)
        chosen: Decimal
        if paired is not None:
            score += 2
            chosen = paired
        else:
            # 保守回退取最大（增值税恒正 → 含税 > 不含税，与限价对比口径一致）；
            # 候选恰为整万且窗口含"保证金" → 软降权（保证金列与真报价并列时弃它）
            def _rank(v: Decimal) -> tuple:
                is_round_wan = v % 10_000 == 0
                return (1 if (is_round_wan and "保证金" in window) else 0, v)

            chosen = max(candidates, key=_rank)
        if best is None or score >= best[0]:
            best = (score, chosen)
    return best[1] if best else None


# 报价标签后跟金额的窗口长度。真实开标一览表把金额放在表头**下一行**
# （"投标总价（含税） 税率\n1 壹佰柒拾肆万玖仟元整 6% …"），同行正则够不着，
# 必须向后看一小段；窗口取窄值以免越过表格读到别的金额（如投标保证金）。
_PRICE_LABEL_WINDOW = 160
# "小写：￥1,526,400.00 元" —— 真实标书常在标签与数字之间夹货币符号，必须容忍
_RE_PRICE_LOWER_LABEL = re.compile(r"小写[：:\s]*[¥￥]?\s*([\d,，]+(?:\.\d+)?)")
# "投标价（含增值税,人民币元） 1749000.00" —— 分项报价表的含税合计行。
# (?!\s*不) 必需："不含增值税"含"含增值税"子串，少了它会把不含税价当成投标报价。
_RE_PRICE_INCL_VAT = re.compile(
    r"投标价\s*[（(](?!\s*不)[^）)\n]{0,30}含增值税[^）)\n]{0,30}[）)]\s*[|｜]?\s*([\d,，]+(?:\.\d+)?)"
)
_RE_TOTAL_LABEL = re.compile(r"投标总价")


def _price_near_total_label(text: str) -> Decimal | None:
    """在"投标总价"标签之后的窗口里找金额（先小写、再大写）。

    真实标书里报价有三种写法，同行正则都够不着，这里统一兜住：
    ① 独立开标一览表：``大写:壹佰柒拾玖万陆仟柒佰元`` / ``小写：1796700``
    ② 正文表格：``投标总价（含税） 税率`` 换行后 ``1 壹佰柒拾肆万玖仟元整 6%``
    ③ 分项报价表：``投标价（含增值税,人民币元） 1749000.00``

    大写只认 4 字以上的连续大写金额。取数只看表头**之后的前几行**并取该行首个大写：
    "投标保证金"是表头列名，出现在报价之前，按邻近字样排除会把真报价误杀、
    反而取到后面那列的保证金金额。
    """
    for m in _RE_TOTAL_LABEL.finditer(text):
        tail = text[m.end() : m.end() + _PRICE_LABEL_WINDOW]
        if lm := _RE_PRICE_LOWER_LABEL.search(tail):
            return _money(lm.group(1))
        for line in tail.split("\n")[:3]:
            if lm := _RE_PRICE_LOWER_LABEL.search(line):
                return _money(lm.group(1))
            if um := _RUN_UPPER.search(line):
                if value := parse_upper_amount(um.group(0)):
                    return value
    return None


@dataclass
class BidFacts:
    text: str
    bid_price: Decimal | None = None
    bid_price_upper: str | None = None
    bid_price_upper_value: Decimal | None = None
    price_items_total: Decimal | None = None
    registered_capital: Decimal | None = None
    performance_amounts: list[Decimal] = field(default_factory=list)
    certificates: list[str] = field(default_factory=list)  # 证书表内的（有复印件）
    cert_claims: list[str] = field(default_factory=list)  # 正文声称的（可能无证书）
    has_qualification_cert: bool = False
    has_guarantee: bool = False
    credit_statement: bool = False
    placeholders: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)  # 章节标题
    # 跨投标人串通信号（engine/collusion.py 预计算后注入；单家抽取无法产出）。
    # 结构：{"signals": [...], "detail": str, "available": bool}；未注入 = 空 dict → need_human 兜底
    cross_bidder: dict = field(default_factory=dict)


def extract_bid_facts(text: str, documents: list[str] | None = None) -> BidFacts:
    facts = BidFacts(text=text, documents=documents or [])

    # 报价：多级策略（宁缺勿错，都取不到就留 None，由人工在报价确认卡点录入）：
    # ① "投标总价：金额" 同行 → ② 标签后窗口内的小写/大写金额（真实标书的换行表格）
    # → ③ "投标价（含增值税…）" 合计行 → ④ 带语境的 ¥。
    if m := _RE_PRICE_TOTAL_ROW.search(text):
        facts.bid_price = _money(m.group(1))
    elif (near := _price_near_total_label(text)) is not None:
        facts.bid_price = near
    elif m := _RE_PRICE_INCL_VAT.search(text):
        facts.bid_price = _money(m.group(1))
    elif (ob := _price_from_open_bid_section(text)) is not None:
        # ⑤ 结构锚点兜底（强于④的全局弱启发）：开标一览表跨行断裂表单
        facts.bid_price = ob
    else:
        facts.bid_price = _contextual_price(text)
    # 大写金额
    if m := _RE_PRICE_TOTAL_CN.search(text):
        facts.bid_price_upper = m.group(1)
        facts.bid_price_upper_value = parse_upper_amount(m.group(1))
    # 分项合计
    if m := _RE_ITEMS_TOTAL_ROW.search(text):
        facts.price_items_total = _money(m.group(1))

    # 注册资本
    if m := _RE_CAPITAL.search(text):
        facts.registered_capital = _money(m.group(1)) * Decimal(10_000)

    # 业绩（表格行：编号 名称 金额 年份；管道或空格分隔）
    # 表头若声明"万元"单位，金额要做 ×10000 换算，否则出现 10000 倍低估
    lines = text.split("\n")
    for m in _RE_ITEM_ROW.finditer(text):
        name, amount, year = m.group(1).strip(" |｜"), m.group(2), m.group(3)
        if "项目名称" in name or "合同金额" in name:
            continue
        value = _money(amount)
        line_no = text.count("\n", 0, m.start())
        for back in range(line_no - 1, max(-1, line_no - 12), -1):
            header = lines[back] if 0 <= back < len(lines) else ""
            if "合同金额" in header or "金额" in header:
                if _RE_WAN_UNIT.search(header):
                    value *= Decimal(10_000)
                break
        facts.performance_amounts.append(value)

    # 体系认证：表内（有证书）与正文声称（可能无证书）分开记
    for m in _RE_CERT_ROW.finditer(text):
        raw = m.group(1)
        name = _RE_CERT_TAIL.split(raw)[0]
        name = re.sub(r"\s+", "", name)
        if name and name not in facts.certificates:
            facts.certificates.append(name)
    for m in _RE_CERT_CLAIM.finditer(text):
        name = re.sub(r"\s+", "", m.group(1))
        if name and name not in facts.cert_claims:
            facts.cert_claims.append(name)

    facts.has_qualification_cert = bool(_RE_QUAL_CERT.search(text))
    facts.has_guarantee = bool(_RE_GUARANTEE.search(text))
    facts.credit_statement = bool(re.search(r"未被列入失信被执行人名单", text))

    for pattern, label in ((r"[X×]{2}项目", "XX项目占位符"), (r"待定|待补充", "待定/待补充")):
        for m in re.finditer(pattern, text):
            fragment = text[max(0, m.start() - 10) : m.end() + 10].replace("\n", " ")
            facts.placeholders.append(f"{label}：…{fragment}…")

    return facts
