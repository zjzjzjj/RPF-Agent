"""价格类否决点：条款范围推断与限价参数归一（R45-B 起）。

判定只依据评标细则原文（红线：禁止对着项目答案拟合）：
- ``infer_math_scope`` 从冻结快照保留的 name/description/source_quote 关键词
  推断 math_relation 条款的检查范围 {"limit","arith"}，不改冻结 params、
  不改 quick payload（缓存键安全）；
- ``resolve_limit_keys`` 统一限价键名（规范键 + 别名只读归一），消灭
  ``max_yuan/max``（objective 路径）与 ``max_amount_yuan/yearly_*``
  （auto_review 路径）的双键名分叉。

R46-A 将扩展：parse_price_limit_text（细则原文确定性解析）、align_tax_basis
（含税/不含税口径决策表）。
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
