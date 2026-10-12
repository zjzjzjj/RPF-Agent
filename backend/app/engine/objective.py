"""资格/符合性检查原语与三态判定：纯代码，绝不自动否决。

三态语义：
- pass              → 通过
- recommend_reject  → **建议**否决 + 依据（必须人工确认才生效）
- need_human        → 无法自动判定（human_only / 事实缺失），进入复核队列
- conflict_flagged  → 冲突（大小写金额不一致、分项合计≠总价、声称与证书不符），不选边
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.engine.facts import BidFacts
from app.engine.models import PrimitiveResult


def run_check(check_type: str, params: dict, facts: BidFacts) -> PrimitiveResult:
    """单一检查原语。params 来自规则包的 criterion_checks。"""
    if check_type == "human_only":
        return PrimitiveResult("unknown", "该项需人工判定（如密封、签字、盖章）", need_human=True)

    if check_type == "document_present":
        name = str(params.get("document") or "")
        if "保证金" in name:
            if facts.has_guarantee:
                return PrimitiveResult("pass", "已找到投标保证金缴纳凭证")
            return PrimitiveResult(
                "fail", "投标文件中未找到投标保证金缴纳凭证", need_human=False
            )
        if "投标函" in name or "授权" in name:
            # 按正文判断（章节标题在正文里）；文件名不含章节名，不能拿文件名当依据
            if "投标函" in facts.text:
                return PrimitiveResult("pass", "已找到投标函及授权委托书内容")
            return PrimitiveResult("fail", "投标文件中未找到投标函及授权委托书")
        if "资质" in name:
            if facts.has_qualification_cert:
                return PrimitiveResult("pass", "已找到资质证书信息")
            return PrimitiveResult("fail", "投标文件中未找到资质证书")
        # 兜底：按文件名关键词在章节标题/全文中找
        keyword = name[:6]
        if keyword and (keyword in facts.text or any(keyword in d for d in facts.documents)):
            return PrimitiveResult("pass", f"已找到「{name}」相关内容")
        return PrimitiveResult("fail", f"投标文件中未找到「{name}」")

    if check_type == "value_in_range":
        field = params.get("field") or ""
        if field == "registered_capital":
            value = facts.registered_capital
            if value is None:
                return PrimitiveResult("unknown", "未提取到注册资本（需人工核查营业执照）", need_human=True)
            minimum = Decimal(str(params.get("min") or 0))
            if value >= minimum:
                return PrimitiveResult(
                    "pass", f"注册资本 {_money(value)} ≥ 要求 {_money(minimum)}")
            return PrimitiveResult(
                "fail",
                f"注册资本 {_money(value)} 低于要求 {_money(minimum)}",
            )
        return PrimitiveResult("unknown", f"未知数值字段：{field}", need_human=True)

    if check_type == "count_at_least":
        field = params.get("field") or ""
        minimum = int(params.get("min_count") or 0)
        min_amount = Decimal(str(params.get("min_amount_yuan") or 0))
        if field == "performance":
            qualified = [a for a in facts.performance_amounts if a >= min_amount]
            if len(facts.performance_amounts) == 0:
                return PrimitiveResult("unknown", "未提取到业绩清单（需人工核查）", need_human=True)
            if len(qualified) >= minimum:
                return PrimitiveResult(
                    "pass",
                    f"满足金额要求的业绩 {len(qualified)} 个 ≥ 要求 {minimum} 个",
                )
            return PrimitiveResult(
                "fail", f"满足金额要求的业绩 {len(qualified)} 个 < 要求 {minimum} 个"
            )
        if field == "certificates":
            count = len(facts.certificates)
            if count >= minimum:
                return PrimitiveResult("pass", f"证书 {count} 项 ≥ 要求 {minimum} 项")
            return PrimitiveResult("fail", f"证书 {count} 项 < 要求 {minimum} 项")
        return PrimitiveResult("unknown", f"未知计数字段：{field}", need_human=True)

    if check_type == "contains_phrase":
        phrase = str(params.get("phrase") or "")
        if phrase and phrase in facts.text:
            return PrimitiveResult("pass", f"已找到声明「{phrase}」")
        return PrimitiveResult("fail", f"投标文件中未找到声明「{phrase}」")

    if check_type == "math_relation":
        return _check_math(params, facts)

    return PrimitiveResult("unknown", f"未知检查类型：{check_type}", need_human=True)


def _check_math(params: dict, facts: BidFacts) -> PrimitiveResult:
    """报价相关算术检查：限价 / 大小写一致 / 分项合计一致。冲突（不一致）不自动选边。"""
    problems: list[str] = []
    max_price = params.get("max_yuan") or params.get("max")
    if max_price is not None and facts.bid_price is not None:
        if facts.bid_price > Decimal(str(max_price)):
            problems.append(
                f"投标报价 {_money(facts.bid_price)} 超过最高限价 {_money(Decimal(str(max_price)))}"
            )
    if facts.bid_price is not None and facts.bid_price_upper_value is not None:
        if facts.bid_price != facts.bid_price_upper_value:
            problems.append(
                f"大写金额（{facts.bid_price_upper}＝{_money(facts.bid_price_upper_value)}）"
                f"与小写金额（{_money(facts.bid_price)}）不一致"
            )
    if facts.bid_price is not None and facts.price_items_total is not None:
        if facts.bid_price != facts.price_items_total:
            problems.append(
                f"分项报价合计（{_money(facts.price_items_total)}）"
                f"与投标总价（{_money(facts.bid_price)}）不一致"
            )
    if problems:
        return PrimitiveResult("fail", "；".join(problems), need_human=True)  # 冲突交人工
    if facts.bid_price is None:
        return PrimitiveResult("unknown", "未提取到投标报价", need_human=True)
    return PrimitiveResult("pass", "报价算术关系一致且未超限价")


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


# ---------------------------------------------------------------------------
# 判定映射
# ---------------------------------------------------------------------------
def verdict_for(
    criterion_code: str,
    criterion_name: str,
    result: PrimitiveResult,
    on_fail: str = "recommend_reject",
) -> str:
    """PrimitiveResult → 三态。绝不产生"rejected"——否决必须人工确认。"""
    if result.status == "pass":
        return "pass"
    if result.status == "fail":
        if result.need_human:
            return "conflict_flagged"  # 算术/一致性冲突 → 人工裁决
        return "recommend_reject" if on_fail == "recommend_reject" else "need_human"
    return "need_human"


# ---------------------------------------------------------------------------
# 计数类评分（商务分）：确定性
# ---------------------------------------------------------------------------
@dataclass
class CountResult:
    """计数类评分结果。

    ``unresolved`` 是**机器可读**的"算不出来"原因，供下游（报告疑问清单）直接判读：
    靠匹配 detail 里的中文措辞会让两处代码静默漂移——该耦合已经断过一次
    （score_count 说"满足金额要求的业绩 0 个"，而下游在找"未提取到业绩清单"）。
    """

    score: Decimal
    detail: str
    conflict: bool
    # None=正常算出 | "field_missing"=未给出计数口径 | "no_facts"=未抽取到事实
    unresolved: str | None = None


def _infer_count_field(criterion: dict) -> str | None:
    """field 缺失时按评分项名称推断"数什么"。

    抽取契约长期没有 field 字段（提示词也没写），真实模型路径下 count 项因此恒 0 分。

    推断必须保守，只认**明确属于投标人主体**的两类，且只看名称（描述里常出现
    "验收证书""认证"等无关词，用它判断会把业绩项误判成证书项）：
    - 名称须同时含主体词（投标人/公司/企业）与口径词（业绩/案例 → performance，
      认证/资质/证书 → certificates）
    - 人员级（项目负责人、工程师、团队…）一律不推断：facts 模型里没有个人业绩桶，
      猜错会产出静默错误的数字，比可见的 0 分更危险。
    """
    name = str(criterion.get("name") or "")
    if any(k in name for k in ("项目负责人", "工程师", "人员", "团队", "项目经理", "拟派")):
        return None  # 人员级：不猜
    if not any(k in name for k in ("投标人", "公司", "企业", "供应商")):
        return None  # 主体不明确：不猜
    # 业绩优先于证书：业绩项的名称里也可能出现"证书"（如"业绩证明材料"），
    # 先判业绩可避免把业绩误算成证书。
    if any(k in name for k in ("业绩", "案例", "合同")):
        return "performance"
    if any(k in name for k in ("认证", "资质", "证书", "体系")):
        return "certificates"
    return None


def score_count(criterion: dict, params: dict, facts: BidFacts) -> CountResult:
    """count 类评分项。

    证书类：只计证书表内的（有复印件的）；正文声称但无证书的 → 冲突标记交人工。
    """
    per_item = Decimal(str(params.get("per_item") or 0))
    max_items = int(params.get("max_items") or 0)
    max_score = Decimal(str(criterion.get("max_score") or 0))
    field = params.get("field") or ""
    inferred = ""
    unresolved: str | None = None
    if not field:
        guessed = _infer_count_field(criterion)
        if guessed:
            field = guessed
            inferred = "（计数字段由评分项名称推断，请核对）"
        else:
            unresolved = "field_missing"

    conflict = False
    if field == "certificates":
        # 声称了但证书表里没有 → 冲突（可能是引用造假）
        cert_keys = [_cert_key(c) for c in facts.certificates]
        for claim in facts.cert_claims:
            if _cert_key(claim) not in cert_keys:
                conflict = True
        count = len(facts.certificates)
        # 一条都没抽到 ≠ 一条都不合格：前者是抽取能力问题，必须与"确实 0 项"区分开，
        # 否则 0 分会被当成评分结论进入总分。
        if not facts.certificates and not facts.cert_claims:
            detail = "未提取到证书清单（需人工核查）"
            unresolved = "no_facts"
        else:
            detail = f"提供证书 {count} 项"
            if conflict:
                detail += "；正文声称的认证中有未附证书复印件的（待人工核查）"
    elif field == "performance":
        min_amount = Decimal(str(params.get("min_amount_yuan") or 0))
        count = len([a for a in facts.performance_amounts if a >= min_amount])
        if not facts.performance_amounts:
            detail = "未提取到业绩清单（需人工核查）"
            unresolved = "no_facts"
        else:
            detail = f"满足金额要求的业绩 {count} 个"
    else:
        count = 0
        detail = "未知计数字段（评分项未给出 field，且无法从名称安全推断）"
        unresolved = "field_missing"

    effective = min(Decimal(count), Decimal(max_items)) if max_items else Decimal(count)
    score = (effective * per_item).quantize(Decimal("0.01"))
    if max_score and score > max_score:
        score = max_score
    return CountResult(
        score=score, detail=f"{detail}，得 {score} 分{inferred}", conflict=conflict,
        unresolved=unresolved,
    )


def _cert_key(name: str) -> str:
    """证书名归一：'ISO9001质量管理体系认证' 与 'ISO9001...' 视为同一项。"""
    import re

    m = re.match(r"(ISO\s?\d+|CMMI\s?\d?)", name)
    return re.sub(r"\s+", "", m.group(1)) if m else name
