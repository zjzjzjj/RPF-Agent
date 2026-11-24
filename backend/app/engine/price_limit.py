"""价格类否决点：条款范围推断、限价参数归一与原文解析（R45-B/R46-A）。

判定只依据评标细则原文（红线：禁止对着项目答案拟合）：
- ``infer_math_scope`` 从冻结快照保留的 name/description/source_quote 关键词
  推断 math_relation 条款的检查范围 {"limit","arith"}，不改冻结 params、
  不改 quick payload（缓存键安全）；
- ``resolve_limit_keys`` 统一限价键名（规范键 + 别名只读归一），消灭
  ``max_yuan/max``（objective 路径）与 ``max_amount_yuan/yearly_*``
  （auto_review 路径）的双键名分叉；
- ``parse_price_limit_text`` 从细则原文确定性解析限价（万元/分年度/含税
  口径/单价型），覆盖语料通用句式（820.62万含税13%、187.2万元（人民币，
  □不含税 ☑含税，税率6%）、第一年度151.98万…、98230.00元/万套）；
- ``align_tax_basis`` 含税/不含税口径决策表（不对称停比、双盲照比，
  2026-11-24 用户拍板）。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# 限价类语境（条款原文）：刻意覆盖"最高限价/控制价/不得超过…价"等通用句式
_RE_LIMIT_TEXT = re.compile(
    r"最高.{0,6}限价|限价|控制价|不得超过.{0,12}(?:价|报价)|超出.{0,12}限价"
)
# 算术一致类语境：刻意收窄——不收裸"一致""分项"（"响应一致""清单分项报价"
# 会误触发，正是 c27 型误归因的来源）
_RE_ARITH_TEXT = re.compile(
    r"大写|小写|算术|不一致|分项合计|合计.{0,10}(?:与|和).{0,10}总价"
)
# 限价参数键（规范键在前；别名只读归一，不回写冻结 params）
_LIMIT_TOTAL_KEYS = ("max_amount_yuan", "max_yuan", "max")
_LIMIT_YEARLY_KEY = "yearly_max_amount_yuan"


def _decimal_or_none(value) -> Decimal | None:
    """正数金额 → Decimal；其余（0/负/非法/空）→ None（限价必须为正）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).replace(",", "").replace("，", ""))
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 else None


def infer_math_scope(
    criterion: dict | None = None, params: dict | None = None
) -> frozenset[str]:
    """推断 math_relation 条款的检查范围。返回 {"limit","arith"} 子集；空集=unknown。

    推断输入（并集）：
    - 条款原文字段 name/description/source_quote（冻结快照保留的细则原文）；
    - params 中的限价键（显式限价值存在即证明条款意图比限价）；
    - params.relation 自由文本（LLM 可能已填）→ 同一套关键词。
    """
    scope: set[str] = set()
    texts: list[str] = []
    if criterion:
        for key in ("name", "description", "source_quote"):
            val = criterion.get(key)
            if val:
                texts.append(str(val))
    p = params if params is not None else ((criterion or {}).get("params") or {})
    if not isinstance(p, dict):
        p = {}
    for key in _LIMIT_TOTAL_KEYS + (_LIMIT_YEARLY_KEY,):
        if p.get(key) is not None:
            scope.add("limit")
            break
    if p.get("relation"):
        texts.append(str(p["relation"]))
    blob = "\n".join(texts)
    if blob and _RE_LIMIT_TEXT.search(blob):
        scope.add("limit")
    if blob and _RE_ARITH_TEXT.search(blob):
        scope.add("arith")
    return frozenset(scope)


def resolve_limit_keys(params: dict | None) -> tuple:
    """归一限价参数（规范键优先，别名只读）。

    返回 ``(total, yearly, includes_tax, tax_rate, kind)``：
    - total: 总价限价（元）；yearly: 分年度限价列表（元）；两者可同时存在
      （resolve 层可用 sum(yearly) 补 total 缺口，调用方决定）；
    - includes_tax: True/False/None（None=原文未声明口径）；
    - tax_rate: 税率百分数（如 6 表示 6%）；kind: "unit_price"（元/万套类
      单价型限价，不产出总价比对）或 None。
    """
    p = params if isinstance(params, dict) else {}
    total: Decimal | None = None
    for key in _LIMIT_TOTAL_KEYS:
        total = _decimal_or_none(p.get(key))
        if total is not None:
            break
    yearly: list[Decimal] | None = None
    raw_yearly = p.get(_LIMIT_YEARLY_KEY)
    if isinstance(raw_yearly, (list, tuple)):
        vals = [_decimal_or_none(v) for v in raw_yearly]
        if vals and all(v is not None for v in vals):
            yearly = vals  # type: ignore[assignment]
    includes_tax = p.get("price_includes_tax")
    if isinstance(includes_tax, str):
        includes_tax = includes_tax.strip().lower() in ("true", "1", "yes", "含税")
    elif not isinstance(includes_tax, bool):
        includes_tax = None
    tax_rate = _decimal_or_none(p.get("tax_rate"))
    kind_raw = p.get("price_limit_kind")
    kind = str(kind_raw).strip() if kind_raw else None
    return total, yearly, includes_tax, tax_rate, kind


# ---------------------------------------------------------------------------
# R46-A：细则原文确定性解析
# ---------------------------------------------------------------------------
# 限价语境词（触发解析；通用句式，非项目拟合）
_RE_LIMIT_WORD = re.compile(r"最高.{0,6}限价|投标限价|限价|控制价|最高投标报价")
# 金额+单位（千分位容忍；万元 → ×10000）
_RE_AMOUNT = re.compile(r"(\d+(?:[,，]\d{3})*(?:\.\d+)?)\s*(万元|元)")
# 分年度限价："第一年度151.98万元"（"年度"与数字必须紧邻，防把
# "第一年度报价151.98万"这类非限价数字抓进来——中间夹"报价/限价"词即不匹配）
_RE_YEARLY = re.compile(
    r"第[一二三四五六七八九十\d]{1,3}年度\s*(\d+(?:[,，]\d{3})*(?:\.\d+)?)\s*万元"
)
# 单价型限价："98230.00元/万套"（有"元/"即单价型，不产出总价比对）
_RE_UNIT_PRICE = re.compile(
    r"(\d+(?:[,，]\d{3})*(?:\.\d+)?)\s*元\s*/\s*[^，。；;\n\d]{1,12}"
)
_RE_RATE = re.compile(r"税率?\s*[：:]?\s*(\d{1,2}(?:\.\d+)?)\s*%")
_RE_RATE_AFTER_TAX = re.compile(r"(?:含税|不含税)\s*[，,]?\s*(\d{1,2}(?:\.\d+)?)\s*%")


def _to_yuan(num: str, unit: str) -> Decimal | None:
    v = _decimal_or_none(num)
    if v is None:
        return None
    if unit == "万元":
        v *= Decimal(10_000)
    return v


def _yuan_str(v: Decimal) -> str:
    """元金额 → 干净字符串（去无意义小数零，format 避免科学计数法）。"""
    s = format(v, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _fill_tax_basis(text: str, out: dict) -> None:
    """从原文提取含税口径与税率（勾选框形态优先）。"""
    if re.search(r"[☑✓√]\s*不含税", text):
        out["price_includes_tax"] = False
    elif re.search(r"[☑✓√]\s*含税", text):
        out["price_includes_tax"] = True
    else:
        pos_excl = text.find("不含税")
        pos_incl = text.find("含税")
        # "含税"是"不含税"的子串：跳过落在"不含税"内部的命中
        while pos_incl != -1 and pos_excl != -1 and pos_incl == pos_excl + 1:
            pos_incl = text.find("含税", pos_incl + 2)
        if pos_excl == -1 and pos_incl != -1:
            out["price_includes_tax"] = True
        elif pos_excl != -1 and pos_incl == -1:
            out["price_includes_tax"] = False
        elif pos_excl != -1 and pos_incl != -1:
            out["price_includes_tax"] = pos_incl < pos_excl
    rm = _RE_RATE.search(text) or _RE_RATE_AFTER_TAX.search(text)
    if rm:
        try:
            rate = Decimal(rm.group(1))
        except (InvalidOperation, ValueError):
            rate = None
        if rate is not None and rate < 50:
            out["tax_rate"] = rm.group(1)


def parse_price_limit_text(text: str) -> tuple[dict | None, str | None]:
    """从细则原文确定性解析限价。返回 (规范键 dict | None, warning | None)。

    覆盖形态（语料通用句式）：
    - ``最高限价为：187.2万元（人民币，□不含税 ☑含税，税率6%）`` → total+口径；
    - ``820.62万元(含税13%)`` / ``411.50万元(不含税)`` → 万元换算+口径；
    - ``第一年度151.98万…第二年度168.32万…第三年度184.66万(含税6%)`` → yearly；
    - ``98230.00元/万套`` → kind=unit_price（不产出总价）；
    - 多个不等总价限价 → (None, warning)（歧义不猜）。
    无金额或无限价语境 → (None, None)。
    """
    if not text or not _RE_LIMIT_WORD.search(text):
        return None, None
    if rm := _RE_UNIT_PRICE.search(text):
        # 单价型：正则命中且确在限价语境（上面已确认 _RE_LIMIT_WORD 存在）
        del rm
        return {"price_limit_kind": "unit_price"}, None
    if yearly := _RE_YEARLY.findall(text):
        vals: list[str] = []
        for num in yearly:
            v = _to_yuan(num, "万元")
            if v is None:
                continue
            vals.append(_yuan_str(v))
        if vals:
            out: dict = {"yearly_max_amount_yuan": vals}
            _fill_tax_basis(text, out)
            return out, None
    amounts: list[Decimal] = []
    for m in _RE_LIMIT_WORD.finditer(text):
        seg = text[m.end() : m.end() + 60]
        am = _RE_AMOUNT.search(seg)
        if am:
            v = _to_yuan(am.group(1), am.group(2))
            if v is not None:
                amounts.append(v)
    if amounts:
        if len(set(amounts)) > 1:
            return None, "细则文本中存在多个不等的限价金额，未自动采用（需人工确认）"
        out = {"max_amount_yuan": _yuan_str(amounts[0])}
        _fill_tax_basis(text, out)
        return out, None
    return None, None


def align_tax_basis(
    bid: Decimal | None,
    bid_incl: bool | None,
    limit: Decimal | None,
    limit_incl: bool | None,
    rate: Decimal | None,
) -> tuple[Decimal | None, bool]:
    """含税/不含税口径决策表（2026-11-24 用户拍板：不对称停比、双盲照比）。

    返回 ``(对齐到限价口径的报价, 是否可比)``：
    - 双方口径都未知 → 直接比较（普通标书常态，保持现状覆盖面）；
    - 一边已知一边未知 → 不可比（单边不对称=误否决高危，如不含税限价×含税报价）；
    - 口径不同且税率可得 → 换算到限价口径后比较；税率不可得 → 不可比。
    """
    if bid is None or limit is None:
        return None, False
    if bid_incl is None and limit_incl is None:
        return bid, True
    if bid_incl is None or limit_incl is None:
        return None, False
    if bid_incl == limit_incl:
        return bid, True
    if rate is None or rate <= 0 or rate >= 50:
        return None, False
    factor = Decimal(1) + rate / Decimal(100)
    if limit_incl and not bid_incl:
        aligned = bid * factor  # 报价不含税 → 乘到含税口径
    else:
        aligned = bid / factor  # 报价含税 → 除到不含税口径
    return aligned.quantize(Decimal("0.01")), True
