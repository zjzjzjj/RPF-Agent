# -*- coding: utf-8 -*-
"""评分项检查定义/计分参数的确定性推断（规则文本驱动，零 LLM）。

真实抽取模型经常不给 check_type / params.field / per_item——后果是评分时
「缺少可执行的检查定义」（match 死项）或「未知计数字段」（count 恒 0）。
本模块从**该项目评标细则原文**（name/description/source_quote/required_evidence）
确定性推断缺口，原则：

- 只认规则文本明示的语言（"每提供 1 项得 2 分"→per_item=2；"特种作业操作证"
  →证件类），不做跨项目经验假设；
- 推断不出的返回 None/空，调用方落 human_only 或留缺口告警——不猜数值。

compiler 落库与 rule_service.normalize_package 自愈共用本模块，口径唯一。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from app.parsing.normalize import normalize_no_ws

# 计分语言："每（个/项/人/条/家/份/证）…得 N 分"
_RE_PER_ITEM = re.compile(r"每(?:个|项|人|条|家|份|证|次)?[^。；，,]{0,12}?得\s*(\d+(?:\.\d+)?)\s*分")
# 封顶："最高（不超过/得）M 分"
_RE_CAP = re.compile(r"最高(?:不超过|得|为)?\s*(\d+(?:\.\d+)?)\s*分")
# 数量上限："最多 N 个/项/人"
_RE_MAX_ITEMS = re.compile(r"最多\s*(\d+)\s*(?:个|项|人|条|家|份)")

# 计数对象关键词（与 reconcile._score_type_for_row 同口径）
CERT_KW = re.compile(r"证书|认证|资质|职称|操作证|上岗证|执业证|许可证|登记证")
PERF_KW = re.compile(r"业绩|案例|合同|项目经验|类似项目|服务经验|实施经验")
# 人员级范围
_LEADER_KW = re.compile(r"项目负责人|项目经理|负责人|拟派负责人|项目总监|技术负责人")
_MEMBER_KW = re.compile(r"团队成员|成员|团队|驻场人员|人员名单|拟派人员|技术人员")

# 声明/承诺类短语提取：取名字里含否定/承诺标记的核心段
# （裸"应"会误配"应急响应"，只认"应当/应按/应提供/应满足"）
_RE_COMMIT_MARK = re.compile(r"承诺|保证|不得|不允许|禁止|严禁|杜绝|须|应当|应按|应提供|应满足")


def _rule_text(c: dict[str, Any]) -> str:
    return "\n".join(
        str(c.get(k) or "") for k in ("name", "description", "source_quote", "required_evidence")
    )


def infer_count_params(c: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    """count 项 params 补缺口：per_item / max_items / min_amount / field / person_scope。

    已给出的值一律不覆盖（模型/人工显式值优先）；只填缺失项。
    max_items/min_amount_yuan/person_scope 复用 compiler.parse_count_params_text
    的口径（normalize_package 自愈同源），本函数只补它没有的 per_item/field。
    """
    params = dict(existing or {})
    text = _rule_text(c)
    name = str(c.get("name") or "")

    # P1-E：新计分原语（偏离逐条扣分/线性步进/值档位/属性分档）优先识别；
    # 识别出 engine 后其余口径参数照常补（互不冲突，评分端按 engine 分发）
    if not params.get("engine"):
        engine_params = infer_engine_params(text)
        if engine_params:
            params = {**engine_params, **params}

    if not params.get("per_item"):
        m = _RE_PER_ITEM.search(text)
        if m:
            params["per_item"] = m.group(1)

    from app.rules.compiler import parse_count_params_text

    per = None
    if params.get("per_item") is not None:
        try:
            per = Decimal(str(params["per_item"]))
        except Exception:  # noqa: BLE001
            per = None
    derived = parse_count_params_text(text, c.get("max_score"), per, name)
    for key, value in derived.items():
        params.setdefault(key, value)
    if not params.get("max_items") and params.get("per_item"):
        cap = _RE_CAP.search(text)
        if cap:
            try:
                per_d = Decimal(str(params["per_item"]))
            except Exception:  # noqa: BLE001
                per_d = Decimal(0)
            if per_d > 0:
                params["max_items"] = str(int(Decimal(cap.group(1)) / per_d))

    if not params.get("person_scope"):
        if _LEADER_KW.search(name):
            params["person_scope"] = "leader"
        elif _MEMBER_KW.search(name):
            params["person_scope"] = "member"

    if not params.get("field"):
        # 业绩优先于证书（业绩项名称里也可能出现"证书"字样）
        if PERF_KW.search(text):
            params["field"] = "performance"
        elif CERT_KW.search(text):
            params["field"] = "certificates"
        elif _RE_PER_ITEM.search(text):
            # 通用按件计数（"每提供一项增值服务得 1 分"类）：计数对象是条目本身，
            # 走逐条 LLM 核验计数。不给 field 会落「未知计数字段」恒 0
            # （2026-10-26 run 54 c64 实证）。
            params["field"] = "items"
    return params


def _infer_count_tier(text: str) -> dict[str, Any] | None:
    """分档计数（P3）识别："ISO体系认证两项得1分，三项得2分"。

    返回 {engine: count_tier, field, tiers: [{min_count, score}]}；不足 2 档不认。
    只认"（中文数字|阿拉伯数字）项/个…得N分"的明示档位，不猜阈值。
    """
    from app.engine.objective import _CN_NUM, _RE_COUNT_TIER

    matches = _RE_COUNT_TIER.findall(text)
    if len(matches) < 2:
        return None
    tiers: list[dict[str, Any]] = []
    seen_min: set[int] = set()
    for raw_n, raw_score in matches:
        min_c = _CN_NUM.get(raw_n)
        if min_c is None:
            try:
                min_c = int(raw_n)
            except ValueError:
                continue
        if min_c in seen_min:
            continue
        seen_min.add(min_c)
        tiers.append({"min_count": min_c, "score": raw_score})
    if len(tiers) < 2:
        return None
    tiers.sort(key=lambda t: int(t["min_count"]))
    # 计数对象：证书/资质类 vs 业绩类
    field = "certificates" if CERT_KW.search(text) else (
        "performance" if PERF_KW.search(text) else "certificates"
    )
    return {"engine": "count_tier", "field": field, "tiers": tiers}


def infer_check(c: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """veto/match 项检查类型确定性生成。生成不了返回 None（→ human_only）。

    规则（按序）：
    1. 材料/证书存在性（名称含 证书/资质/执照/许可证…）→ document_present，
       document 取名称（_document_phrases 会再拆词组）；
    2. 声明/承诺类（名称含 承诺/不得/不允许/禁止…）→ contains_phrase，
       phrase 取名称核心段（剥组名前缀"服务团队技术力量—"等）。
    """
    name = str(c.get("name") or "")
    # 剥组名前缀（"服务团队技术力量—承诺额外增加驻场人员"→"承诺额外增加驻场人员"）
    core = re.split(r"[—\-－—:：]", name)[-1].strip() or name
    # 证件类只认**材料名词形态**（证书/执照/许可证…）：裸"资质"会把
    # 「资质等级达到一级以上」「资质有效期」这类**等级/有效性要求**误生成
    # document_present——文本提及即 pass = 假通过（审查实锤）。等级/有效期
    # 交 human_only → LLM 语义裁决。
    if re.search(r"落款|签署", core) and re.search(r"日期|时间|截止", core):
        # 落款/签署日期类（如"授权书落款日期有效"）→ 确定性日期核查
        return "date_valid", {"document": core}
    if re.search(r"有效期|等级|一级|二级|三级|四级|达到|吊销|暂停|撤销|动态考核|复审|换证", core):
        return None  # 等级/有效性/状态要求，存在性检查判不了
    if re.search(r"证书|执照|许可证|登记证|认证书|合格证|资质证书|资质副本|资质证件", core):
        return "document_present", {"document": core}
    if _RE_COMMIT_MARK.search(core) and len(normalize_no_ws(core)) >= 4:
        return "contains_phrase", {"phrase": core}
    return None


# --- P1-E：新计分原语的引擎参数推断（只认规则文本明示，推断不出不设 engine） ---

# 偏离表逐条扣分："每缺失一条扣 2 分" / "每负偏离一条扣 1 分"
_RE_DEV_PER_MISSING = re.compile(r"每缺失一条扣\s*(\d+(?:\.\d+)?)\s*分")
_RE_DEV_PER_NEG = re.compile(r"每负偏离一条扣\s*(\d+(?:\.\d+)?)\s*分")
# 响应时间档："2 小时内…得 3 分"（≥2 档才认）
# 断行容错（2026-11-04）：OCR 断行常见"小时\n内"，\s* 覆盖不放宽档数守卫
_RE_HOURS_TIER = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:个)?\s*小时\s*(?:内|以内)[^。；]{0,20}?得\s*(\d+(?:\.\d+)?)\s*分"
)
# 线性步进："每额外增加 12 个月质保期加 1 分" / "每增加 12 个月…得 1 分"
_RE_LINEAR_STEP = re.compile(
    r"每(?:额外)?增(?:加)?\s*(\d+)\s*(?:个月|月|年)[^。；]{0,16}?(?:加|得)\s*(\d+(?:\.\d+)?)\s*分"
)
# 基线："满足 1 年质保期以外" / "质保期不少于 1 年" / "1 年免费保修基础上"
_RE_LINEAR_BASE = re.compile(
    r"(?:满足|在|自)\s*(\d+(?:\.\d+)?)\s*(?:年|个月|月)[^。；]{0,10}(?:质保|保修)"
    r"|质保期?(?:要求)?(?:不少于|不低于|达到)\s*(\d+(?:\.\d+)?)\s*(?:年|个月|月)"
)
_RE_CAP_SCORE = re.compile(r"最高(?:不超过|得)?\s*(\d+(?:\.\d+)?)\s*分")
# 属性分档（年限）："具有 5 年及以上工作经验得 3 分"（≥2 档才认；断行容错同上）
_RE_YEARS_TIER = re.compile(
    r"(\d+)\s*年\s*(?:及\s*)?以上[^。；]{0,20}?得\s*(\d+(?:\.\d+)?)\s*分"
)
# 属性分档（职称）："高级工程师得 1 分" / "中级工程师得 0.5 分"（≥2 档才认）
_RE_TITLE_TIER = re.compile(
    r"((?:高级|中级|初级)[^。；，,]{0,6}(?:工程师|技师|建筑师|会计师))[^。；]{0,12}?得\s*(\d+(?:\.\d+)?)\s*分"
)


def infer_engine_params(text: str) -> dict:
    """新计分原语（P1-E）的 params 推断；识别不出返回 {}（走原有 count 口径）。

    优先级：偏离逐条扣分 > 线性步进（质保）> 值档位（响应时间）> 属性分档（年限/职称）。
    全部只认明示句式与明示数值——不猜档位、不猜阈值（红线）。
    """
    if _RE_DEV_PER_MISSING.search(text) or _RE_DEV_PER_NEG.search(text):
        out: dict = {"engine": "deviation_deduct_table"}
        m = _RE_DEV_PER_MISSING.search(text)
        if m:
            out["per_missing"] = m.group(1)
        m = _RE_DEV_PER_NEG.search(text)
        if m:
            out["per_negative"] = m.group(1)
        return out

    # 分档计数（P3）："ISO体系认证两项得1分，三项得2分"——计数命中最高档，
    # 非 per_item 线性（run 59 T5.2 Part1 缺 per_item → 7 格全 0 的根因）。
    tier = _infer_count_tier(text)
    if tier is not None:
        return tier

    m = _RE_LINEAR_STEP.search(text)
    if m:
        step_n = int(m.group(1))
        step_raw = m.group(0)
        step_months = step_n * 12 if "年" in step_raw.split("，")[0] else step_n
        mb = _RE_LINEAR_BASE.search(text)
        if mb:
            base_n = mb.group(1) or mb.group(2)
            base_raw = mb.group(0)
            base_months = (
                Decimal(str(base_n)) * 12 if "年" in base_raw else Decimal(str(base_n))
            )
            out = {
                "engine": "value_linear",
                "field": "warranty_months",
                "base": str(base_months),
                "step": str(step_months),
                "per": m.group(2),
            }
            mc = _RE_CAP_SCORE.search(text)
            if mc:
                out["cap"] = mc.group(1)
            return out

    hours = [
        (Decimal(hg), Decimal(sg))
        for hg, sg in _RE_HOURS_TIER.findall(text)
    ]
    if len(hours) >= 2:
        # lower_better：档位编码为 [0..h]→score（文本给的是"≤h 得 s 分"语义）
        tiers = [{"max_value": str(h), "score": str(s)} for h, s in hours]
        # 同值多档去重（保分数更高者）
        dedup: dict[str, dict] = {}
        for t in tiers:
            prev = dedup.get(t["max_value"])
            if prev is None or Decimal(t["score"]) > Decimal(prev["score"]):
                dedup[t["max_value"]] = t
        return {
            "engine": "value_tier",
            "field": "response_hours",
            "direction": "lower_better",
            "tiers": [dedup[k] for k in sorted(dedup, key=lambda x: Decimal(x))],
        }

    years = [(int(y), Decimal(s)) for y, s in _RE_YEARS_TIER.findall(text)]
    if len(years) >= 2:
        tiers = [{"min": str(y), "score": str(s)} for y, s in years]
        return {"engine": "attribute_tier", "attr": "years", "tiers": tiers}

    titles = [(lv, Decimal(s)) for lv, s in _RE_TITLE_TIER.findall(text)]
    if len(titles) >= 2:
        tiers = [{"level": lv, "score": str(s)} for lv, s in titles]
        return {"engine": "attribute_tier", "attr": "title", "tiers": tiers}

    return {}


# --- R0：客观分路由（2026-11-03 run78 取证） -------------------------------
# score_type=subjective 但细则是明示「X得N分」档表的格，评分时路由到客观引擎。
# 服务承诺「4小时内到达得4分,5小时得3分…」被编译成 subjective 后丢确定性引擎
# （证据簇均分只得 2/4），是 run78 中科「该给没给」的主因之一。

# 通用「得N分」计分提及（闭合性检查用；"不得分"无数字不匹配）
_RE_GET_SCORE = re.compile(r"得\s*(\d+(?:\.\d+)?)\s*分")
# 响应时间档（宽松版：不强制「内/以内」——"5小时得3分"类省略式同样明示）
_RE_HOURS_TIER_LOOSE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:个)?\s*小时\s*(?:内|以内)?[^。；，,]{0,16}?得\s*(\d+(?:\.\d+)?)\s*分"
)
# 证书等级档："四级得1分，三级及以上得2分"（≥2 档才认；"三级及\n以上"断行容错）
_RE_CERT_LEVEL_TIER = re.compile(
    r"([一二三四五六七八九十\d]+级(?:及\s*以上)?)\s*[^。；，,]{0,10}?得\s*(\d+(?:\.\d+)?)\s*分"
)
# 中文等级序（"及以上"=语言学语义，非项目拟合）
_CERT_LEVEL_ORDER = {"一级": 1, "二级": 2, "三级": 3, "四级": 4, "五级": 5,
                     "六级": 6, "七级": 7, "八级": 8, "九级": 9, "十级": 10}
# 分档计数（带量词后名词捕获，判计数源用）："2个及以上…经验，得1分" / "提供两项得1分"
# 得(得)* 容忍双得断行（与 objective._RE_COUNT_TIER 同口径）
_RE_COUNT_TIER_NOUN = re.compile(
    r"(一|二|两|三|四|五|六|七|八|九|十|\d+)\s*(?:项|个|本|张|类)"
    r"([^得。；]{0,14})得(?:\s*得)*\s*(\d+(?:\.\d+)?)\s*分"
)
_EXP_KW = re.compile(r"经验|业绩|案例")


def _cert_level_expand(token: str) -> list[str]:
    """「三级及以上」→ ["一级","二级","三级"]（优于/等于该级的显式列表）。

    断行容错（2026-11-04）：OCR"三级\\n及以上"先去空白再匹配，防止降级成单档。
    """
    t = re.sub(r"\s+", "", token or "")
    m = re.fullmatch(r"([一二三四五六七八九十\d]+级)(及以上)?", t)
    if not m:
        return [t] if t else []
    base = m.group(1)
    if not m.group(2):
        return [base]
    rank = _CERT_LEVEL_ORDER.get(base)
    if rank is None:
        return [base]
    return [k for k, v in sorted(_CERT_LEVEL_ORDER.items(), key=lambda kv: kv[1])
            if v <= rank]


def _tier_scores_closed(text: str, parsed_scores: set[str]) -> bool:
    """闭合性：细则里所有「得N分」计分提及都被已解析档位覆盖。

    文本另有解析不出的计分提及（散文式"酌情得N分"混写）→ 不闭合 → 不路由
    （歧义不猜）。档表是打分规则的全部 → 闭合 → 客观形态。
    """
    mentions = {s for s in _RE_GET_SCORE.findall(text)}
    return bool(mentions) and mentions <= parsed_scores


def infer_objective_route(criterion: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """主观格评分时的客观路由推断（R0）。返回 (engine_kind, params) 或 None。

    engine_kind: "value_tier" | "attribute_tier" | "count_tier" | "count"。
    守卫（防把主观散文客观化）：档表 ≥2 明示档，且
    （`_subjectivity_hint=="objective"` 或文本含「客观分」 或 档表闭合）。
    纯确定性零 LLM；只读 criteria 字典不回写，兼容冻结包。推断不出 → None
    （回退 subjective，歧义不猜）。
    """
    text = _rule_text(criterion)
    name = str(criterion.get("name") or "")
    if not text.strip():
        return None
    gated_hint = (criterion.get("params") or {}).get("_subjectivity_hint") == "objective"
    gated_mark = "客观分" in text

    def _accept(parsed_scores: set[str]) -> bool:
        return gated_hint or gated_mark or _tier_scores_closed(text, parsed_scores)

    # ① 数值承诺档（响应时间）："4小时内到达得4分,5小时得3分…"
    hours: list[tuple[str, str]] = _RE_HOURS_TIER_LOOSE.findall(text)
    dedup: dict[str, str] = {}
    for h, s in hours:
        if h not in dedup or Decimal(s) > Decimal(dedup[h]):
            dedup[h] = s
    if len(dedup) >= 2 and _accept(set(dedup.values())):
        tiers = [{"max_value": k, "score": v}
                 for k, v in sorted(dedup.items(), key=lambda kv: Decimal(kv[0]))]
        return "value_tier", {
            "field": "response_hours", "direction": "lower_better", "tiers": tiers,
        }

    # ② 证书等级档："四级得1分，三级及以上得2分"
    levels: list[tuple[str, str]] = _RE_CERT_LEVEL_TIER.findall(text)
    lv_dedup: dict[tuple[str, ...], str] = {}
    for token, s in levels:
        key = tuple(_cert_level_expand(token))
        if not key:
            continue
        if key not in lv_dedup or Decimal(s) > Decimal(lv_dedup[key]):
            lv_dedup[key] = s
    if len(lv_dedup) >= 2 and _accept(set(lv_dedup.values())):
        tiers = [{"match_any": list(k), "score": v} for k, v in lv_dedup.items()]
        return "attribute_tier", {"attr": "cert_level", "tiers": tiers}

    # ③ 分档计数："2个及以上…经验，得1分；4个…得2分" / "提供两项得1分，四项得2分"
    from app.engine.objective import _CN_NUM

    cnt: dict[int, str] = {}
    cnt_noun = ""
    for raw_n, noun, s in _RE_COUNT_TIER_NOUN.findall(text):
        n = _CN_NUM.get(raw_n)
        if n is None:
            try:
                n = int(raw_n)
            except ValueError:
                continue
        if n not in cnt or Decimal(s) > Decimal(cnt[n]):
            cnt[n] = s
        # 捕获名词断行归一（"经\n验"→"经验"），防 _EXP_KW 漏判计数源
        noun_norm = re.sub(r"\s+", "", noun or "")
        if _EXP_KW.search(noun_norm):
            cnt_noun = noun_norm
    if len(cnt) >= 2 and _accept(set(cnt.values())):
        tiers = [{"min_count": k, "score": v} for k, v in sorted(cnt.items())]
        if _EXP_KW.search(cnt_noun):
            # 计数源=人员/公司经验（verified_cases 逐案例核验路径）
            out: dict[str, Any] = {"field": "performance", "tiers": tiers}
            if _LEADER_KW.search(name):
                out["person_scope"] = "leader"
            elif _MEMBER_KW.search(name):
                out["person_scope"] = "member"
            return "count", out
        return "count_tier", {"field": "certificates", "tiers": tiers}

    # ④ per_item 线性："每1人得1分，最高不超过3分"
    m = _RE_PER_ITEM.search(text)
    if m:
        out = {"per_item": m.group(1)}
        cap = _RE_CAP.search(text)
        if cap:
            try:
                per_d = Decimal(m.group(1))
            except Exception:  # noqa: BLE001
                per_d = Decimal(0)
            if per_d > 0:
                out["max_items"] = str(int(Decimal(cap.group(1)) / per_d))
        if _accept({m.group(1)}):
            if _LEADER_KW.search(name):
                out["person_scope"] = "leader"
            elif _MEMBER_KW.search(name):
                out["person_scope"] = "member"
            return "count", out

    return None
