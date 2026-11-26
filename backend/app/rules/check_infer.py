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
from datetime import date
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
# CMMI/CSMM/能力成熟度是具体证书名（cmmi 专属计数分支），优先于通用"认证"词
# ——"CMMI/CSMM 认证"落到 certificates 会给任何证书计分（2026-11-25 c45 实证）
CMMI_KW = re.compile(r"CMMI|CSMM|能力成熟度", re.I)
_MATURITY_LEVEL_NUMBER = r"[一二三四五六七八九1-9]"
_RE_MATURITY_LEVEL = re.compile(
    rf"(?:CMMI|CSMM|能力成熟度)(?:认证|评估|证书)*(?:等级)?({_MATURITY_LEVEL_NUMBER})级(及以上|或以上|以上)?",
    re.I,
)
_RE_SUBMISSION_DATE = re.compile(
    r"(?:投标(?:文件(?:的)?递交)?|递交)截止(?:时间|日期)[:为]?"
    r"(20\d{2})(?:年|-|/|\.)(\d{1,2})(?:月|-|/|\.)(\d{1,2})(?:日|(?!\d))"
)
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

    count_m = list(_RE_COUNT_TIER.finditer(text))
    # 子句级闭合（同 R0）：多子项清单里的其他计分句不得被本族档位"借用"闭合
    if len(count_m) < 2 or not _tier_scores_closed(text, [m.span() for m in count_m]):
        return None
    matches = [(m.group(1), m.group(2)) for m in count_m]
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
# 中文数字容错（测试10 run113 实证："每缺失一条扣两分，每负偏离一条扣一分"）；
# 词形变体："缺一个扣0.5 分"（承诺响应缺证扣分，run113 c31）
_CN_NUM_MAP = {"一": "1", "两": "2", "二": "2", "三": "3", "四": "4", "五": "5",
               "六": "6", "七": "7", "八": "8", "九": "9", "十": "10", "半": "0.5"}
_NUM_TOKEN = r"(\d+(?:\.\d+)?|一|两|二|三|四|五|六|七|八|九|十|半)"


def _num_value(raw: str) -> str:
    """阿拉伯数字原样返回；中文数字/半 → 对应数值字符串。"""
    return _CN_NUM_MAP.get(raw, raw)


# 「缺(失|少)?」容忍"缺少一项扣0.5分"（利群 p123 检测仪器配置实证：旧
# 缺(?:失)? 匹配不了"缺少"→ 扣分式整条漏判）
_RE_DEV_PER_MISSING = re.compile(
    r"每?\s*缺(?:失|少)?(?:一)?\s*(?:条|个|本|张|项)\s*扣\s*" + _NUM_TOKEN + r"\s*分"
)
_RE_DEV_PER_NEG = re.compile(
    r"每?\s*负偏离\s*(?:一)?\s*(?:条|个|张|项)?\s*扣\s*" + _NUM_TOKEN + r"\s*分"
)
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


# --- 清单式多子项计分（P0-4③：测试10 run113 c30 售后服务「1、…得2分 2、…得2分 3、…各得1分」）---
# 子项编号：全角"（1）"任意位置；半角"1、"只认句首/句末标点之后（防把数量"2、"误作子项）。
_RE_SUB_PAREN = re.compile(r"（\s*(\d{1,2})\s*）")
_RE_SUB_ENUM = re.compile(r"(\d{1,2})\s*[、．.]")
_RE_FIXED_SCORE_ANY = re.compile(r"得\s*\d+(?:\.\d+)?\s*分")
# 「提供A及B的各得N分」：每件各得 N 分（说明书及简章各 1 分）
_RE_EACH_SCORE = re.compile(r"(?:提供|具有|配备)?\s*(.{1,40}?)\s*的?\s*各得\s*(\d+(?:\.\d+)?)\s*分")
# 子项内「得N分」数值：子项上限取其最高档（"不得分"无数字不匹配）
_RE_GET_N = re.compile(r"得\s*(\d+(?:\.\d+)?)\s*分")
_CHECKLIST_NOISE_PREFIX = ("投标产品", "投标人", "投标", "明确", "相关", "相应", "详细", "完整")


def _checklist_markers(text: str) -> list[tuple[int, int, int]]:
    marks: list[tuple[int, int, int]] = [
        (int(m.group(1)), m.start(), m.end()) for m in _RE_SUB_PAREN.finditer(text)
    ]
    for m in _RE_SUB_ENUM.finditer(text):
        before = text[: m.start()].rstrip()
        if before and before[-1] not in "：:。；;\n分）)":
            continue
        marks.append((int(m.group(1)), m.start(), m.end()))
    return sorted(marks, key=lambda t: t[1])


def _checklist_segments(text: str) -> list[str] | None:
    """按 1,2,3… 连续编号切出子项正文；每个子项须含明示「得N分」，否则不是清单形态。"""
    seq: list[tuple[int, int, int]] = []
    expect = 1
    for num, start, end in _checklist_markers(text):
        if num == expect:
            seq.append((num, start, end))
            expect += 1
    if len(seq) < 2:
        return None
    segs: list[str] = []
    for i, (_num, _start, end) in enumerate(seq):
        stop = seq[i + 1][1] if i + 1 < len(seq) else len(text)
        segs.append(text[end:stop])
    if not all(_RE_FIXED_SCORE_ANY.search(seg) for seg in segs):
        return None
    return segs


def _clean_checklist_keywords(raw: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[、，,及和与或/]", raw):
        p = re.sub(r"[（(][^）)]*[）)]", "", part).strip()
        for pre in _CHECKLIST_NOISE_PREFIX:
            while p.startswith(pre) and len(p) > len(pre):
                p = p[len(pre):]
        if len(p) >= 2 and p not in out:
            out.append(p)
    return out


def _checklist_name(seg: str, idx: int) -> str:
    """子项名：子项首个分句（去空白、剥尾部"的/各"，≤24 字）；无可用名时以序号兜底。"""
    head = re.split(r"[，,。；;：:]|得\s*\d", seg, maxsplit=1)[0]
    head = re.sub(r"\s+", "", head)
    head = re.sub(r"的各$|各$|的$", "", head).strip()
    return head[:24] or f"第{idx}项"


def checklist_subitems(text: str) -> list[tuple[str, Decimal]] | None:
    """清单式多子项（编号子项各自计分）→ [(子项名, 分值上限)]，供主观分子项独立核验（P0-4③）。

    子项上限：子项内「得N分」取最高档；「A及B的各得N分」按并列项个数相乘。
    编号不连续、子项无明示分值、或子项名重复 → None（歧义不猜，退回整项判读）。
    """
    segs = _checklist_segments(text or "")
    if segs is None:
        return None
    items: list[tuple[str, Decimal]] = []
    names: set[str] = set()
    for idx, seg in enumerate(segs, start=1):
        each = _RE_EACH_SCORE.search(seg)
        if each:
            n_items = len(_clean_checklist_keywords(each.group(1))) or 1
            cap = Decimal(each.group(2)) * n_items
        else:
            scores = [Decimal(s) for s in _RE_GET_N.findall(seg)]
            if not scores:
                return None
            cap = max(scores)
        name = _checklist_name(seg, idx)
        if name in names:
            return None
        names.add(name)
        items.append((name, cap))
    return items


def infer_engine_params(text: str) -> dict:
    """新计分原语（P1-E）的 params 推断；识别不出返回 {}（走原有 count 口径）。

    优先级：偏离逐条扣分 > 线性步进（质保）> 值档位（响应时间）> 属性分档（年限/职称）。
    全部只认明示句式与明示数值——不猜档位、不猜阈值（红线）。
    """
    if _RE_DEV_PER_MISSING.search(text) or _RE_DEV_PER_NEG.search(text):
        out: dict = {"engine": "deviation_deduct_table"}
        m = _RE_DEV_PER_MISSING.search(text)
        if m:
            out["per_missing"] = _num_value(m.group(1))
        m = _RE_DEV_PER_NEG.search(text)
        if m:
            out["per_negative"] = _num_value(m.group(1))
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

    hours_m = list(_RE_HOURS_TIER.finditer(text))
    hours = [(Decimal(hm.group(1)), Decimal(hm.group(2))) for hm in hours_m]
    if len(hours) >= 2 and _tier_scores_closed(text, [hm.span() for hm in hours_m]):
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

    years_m = list(_RE_YEARS_TIER.finditer(text))
    years = [(int(ym.group(1)), Decimal(ym.group(2))) for ym in years_m]
    if len(years) >= 2 and _tier_scores_closed(text, [ym.span() for ym in years_m]):
        tiers = [{"min": str(y), "score": str(s)} for y, s in years]
        return {"engine": "attribute_tier", "attr": "years", "tiers": tiers}

    titles_m = list(_RE_TITLE_TIER.finditer(text))
    titles = [(tm.group(1), Decimal(tm.group(2))) for tm in titles_m]
    if len(titles) >= 2 and _tier_scores_closed(text, [tm.span() for tm in titles_m]):
        tiers = [{"level": lv, "score": str(s)} for lv, s in titles]
        return {"engine": "attribute_tier", "attr": "title", "tiers": tiers}

    return {}


# --- R0：客观分路由（2026-11-03 run78 取证） -------------------------------
# score_type=subjective 但细则是明示「X得N分」档表的格，评分时路由到客观引擎。
# 服务承诺「4小时内到达得4分,5小时得3分…」被编译成 subjective 后丢确定性引擎
# （证据簇均分只得 2/4），是 run78 中科「该给没给」的主因之一。

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
    r"(一|二|两|三|四|五|六|七|八|九|十|\d+)\s*(?:项|个|本|张|类|条|台|套|人|份|种)"
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


# 计分提及：「得N分」，排除「最高得N分」封顶句与「最低得N分」保底句（二者都不是档位分值）
_RE_SCORE_CLAUSE = re.compile(r"(?<![高低])得\s*(\d+(?:\.\d+)?)\s*分")


def _tier_scores_closed(text: str, tier_spans: list[tuple[int, int]]) -> bool:
    """子句级闭合：细则里每一处「得N分」计分提及都必须落在已解析的档位子句之内。

    旧判据只比对分值集合（提及分值 ⊆ 档位分值），多子项清单（售后服务 1/2/3 子项）的
    「得2分」「得1分」会被同值的响应时限档"借用"闭合，把整条细则误路由成单一档位
    （测试10 run113 c30 实证）。子句级判据只看位置：另一子项的计分句、散文式"酌情得N分"
    都会让闭合失败 → 不路由，交主观整体判读（歧义不猜）。客观分标注/主客观 hint 不再绕过
    本判据——标注不能让未被档位覆盖的计分子句消失。
    """
    if not tier_spans:
        return False
    mentions = [m.span() for m in _RE_SCORE_CLAUSE.finditer(text)]
    return bool(mentions) and all(
        any(ts <= ms and me <= te for ts, te in tier_spans) for ms, me in mentions
    )


def infer_objective_route(criterion: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """主观格评分时的客观路由推断（R0）。返回 (engine_kind, params) 或 None。

    engine_kind: "value_tier" | "attribute_tier" | "count_tier" | "count"。
    守卫（防把主观散文客观化）：档表 ≥2 明示档，且档表子句闭合（每处「得N分」都落在档位子句内）。
    纯确定性零 LLM；只读 criteria 字典不回写，兼容冻结包。推断不出 → None
    （回退 subjective，歧义不猜）。
    """
    text = _rule_text(criterion)
    name = str(criterion.get("name") or "")
    if not text.strip():
        return None
    def _accept(tier_spans: list[tuple[int, int]]) -> bool:
        return _tier_scores_closed(text, tier_spans)

    # ① 数值承诺档（响应时间）："4小时内到达得4分,5小时得3分…"
    hours_m = list(_RE_HOURS_TIER_LOOSE.finditer(text))
    dedup: dict[str, str] = {}
    for hm in hours_m:
        h, s = hm.group(1), hm.group(2)
        if h not in dedup or Decimal(s) > Decimal(dedup[h]):
            dedup[h] = s
    if len(dedup) >= 2 and _accept([hm.span() for hm in hours_m]):
        tiers = [{"max_value": k, "score": v}
                 for k, v in sorted(dedup.items(), key=lambda kv: Decimal(kv[0]))]
        return "value_tier", {
            "field": "response_hours", "direction": "lower_better", "tiers": tiers,
        }

    # ② 证书等级档："四级得1分，三级及以上得2分"
    level_m = list(_RE_CERT_LEVEL_TIER.finditer(text))
    lv_dedup: dict[tuple[str, ...], str] = {}
    for lm in level_m:
        key = tuple(_cert_level_expand(lm.group(1)))
        if not key:
            continue
        if key not in lv_dedup or Decimal(lm.group(2)) > Decimal(lv_dedup[key]):
            lv_dedup[key] = lm.group(2)
    if len(lv_dedup) >= 2 and _accept([lm.span() for lm in level_m]):
        tiers = [{"match_any": list(k), "score": v} for k, v in lv_dedup.items()]
        return "attribute_tier", {"attr": "cert_level", "tiers": tiers}

    # ③ 分档计数："2个及以上…经验，得1分；4个…得2分" / "提供两项得1分，四项得2分"
    from app.engine.objective import _CN_NUM

    count_m = list(_RE_COUNT_TIER_NOUN.finditer(text))
    cnt: dict[int, str] = {}
    cnt_noun = ""
    for cm in count_m:
        raw_n, noun, s = cm.group(1), cm.group(2), cm.group(3)
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
    if len(cnt) >= 2 and _accept([cm.span() for cm in count_m]):
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
    per_m = list(_RE_PER_ITEM.finditer(text))
    if per_m:
        m = per_m[0]
        out = {"per_item": m.group(1)}
        cap = _RE_CAP.search(text)
        if cap:
            try:
                per_d = Decimal(m.group(1))
            except Exception:  # noqa: BLE001
                per_d = Decimal(0)
            if per_d > 0:
                out["max_items"] = str(int(Decimal(cap.group(1)) / per_d))
        if _accept([pm.span() for pm in per_m]):
            if _LEADER_KW.search(name):
                out["person_scope"] = "leader"
            elif _MEMBER_KW.search(name):
                out["person_scope"] = "member"
            return "count", out

    return None


# ---------------------------------------------------------------------------
# 评分形态共享决策树（reconcile 与 normalize_package 共用，口径唯一）
# ---------------------------------------------------------------------------
# 只认细则明示形态（不猜）：全有全无 / 分档计数 / 扣分式 / per_item 线性 / 二值存在。
# 利群商标条盒 7 条 match 误标实证：多档"一条…得2分、两条…得3分"（无「每」）、
# 扣分式"缺少一项扣0.5分"（旧正则匹配不了"缺少"）、二值"提供…得1分"（无证书词）
# 三形态全走旧兜底 → 撞上 match（不计分）→ Σ 恒缺 15。

_RE_ALL_OR_NOTHING_FAIL = re.compile(
    r"任(?:何|意)?\s*(?:一)?\s*(?:项|个|条|处|样品)[^。；]{0,24}?"
    r"(?:不能|不符合|未)(?:满足|符合)[^。；]{0,30}?得\s*0\s*分"
)
_RE_ALL_OR_NOTHING_PASS = re.compile(
    r"全部(?:符合|满足)[^。；]{0,24}?(?:得\s*(?:满分|\d+(?:\.\d+)?\s*分))"
)
# 二值存在性："提供检测人员配置方案的得1分，未提供的得0分"（无「每」的单件得分）
_RE_EXIST_SCORE = re.compile(
    r"(?:提供|具有|配备|提交|出具)[^。；]{0,40}?(?:得|计)\s*(\d+(?:\.\d+)?)\s*分"
)


def classify_scoring_form(text: str) -> tuple[str, dict[str, Any]] | None:
    """细则文本 → (form, params)；明示形态都判不出 → None（调用方维持现状，不猜）。

    form ∈ {all_or_nothing, tier_count, deduct, per_item, existence}：
    - all_or_nothing（"任一不能满足得0分、全部符合得满分"）：文本无计数对象
      （样品符合性是实体判定）——调用方应落 subjective + _subjectivity_hint=objective
      留痕，不得硬映射 count（count 事实层无案例会悬置不进总分，更糟）；
    - 其余形态 → count 族，params 供确定性引擎执行（tiers 走 score_count 案例档表
      （objective.score_count 的 _use_tiers）/ deviation_deduct_table 扣分封顶 /
      per_item 线性）。
    """
    text = text or ""
    # ① 全有全无（双侧判据：0 分侧与满分侧都在才算明示）
    if _RE_ALL_OR_NOTHING_FAIL.search(text) and _RE_ALL_OR_NOTHING_PASS.search(text):
        return "all_or_nothing", {}
    # ② 扣分式："缺少一项扣0.5分（扣完为止）"——封顶由引擎 deduct=min(deduct,max) 天然满足
    m = _RE_DEV_PER_MISSING.search(text)
    if m:
        return "deduct", {"engine": "deviation_deduct_table",
                          "per_missing": _num_value(m.group(1))}
    # ③ 分档计数（≥2 档明示 + 子句闭合守卫，不猜阈值）
    from app.engine.objective import _CN_NUM, _RE_COUNT_TIER

    count_m = list(_RE_COUNT_TIER.finditer(text))
    if len(count_m) >= 2 and _tier_scores_closed(text, [m.span() for m in count_m]):
        tiers: list[dict[str, Any]] = []
        seen_min: set[int] = set()
        for raw_n, raw_score in ((mm.group(1), mm.group(2)) for mm in count_m):
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
        if len(tiers) >= 2:
            tiers.sort(key=lambda t: int(t["min_count"]))
            if CERT_KW.search(text):
                # 证书档计数：count_tier 引擎按证书清单计数
                return "tier_count", {"engine": "count_tier",
                                      "field": "certificates", "tiers": tiers}
            if PERF_KW.search(text):
                # 业绩档计数：score_count 案例档表（tiers 形状、不带 engine）
                return "tier_count", {"field": "performance", "tiers": tiers}
            # 其他计数对象（设备/生产线/条目）：LLM 逐条核验 + 案例档表
            return "tier_count", {"field": "items", "tiers": tiers}
    # ④ per_item 线性："每提供1个案例得1分，最高2分"
    m = _RE_PER_ITEM.search(text)
    if m:
        field = ("cmmi" if CMMI_KW.search(text)
                 else "certificates" if CERT_KW.search(text)
                 else "performance" if PERF_KW.search(text) else "items")
        params: dict[str, Any] = {"field": field, "per_item": m.group(1)}
        cap = _RE_CAP.search(text)
        if cap:
            try:
                per = Decimal(m.group(1))
            except Exception:  # noqa: BLE001
                per = Decimal(0)
            if per > 0:
                params["max_items"] = str(int(Decimal(cap.group(1)) / per))
        return "per_item", params
    # ⑤ 二值存在性："提供…得N分，未提供得0分"——单件存在即得 N 分（per_item=N，max_items=1）
    m = _RE_EXIST_SCORE.search(text)
    if m:
        field = ("cmmi" if CMMI_KW.search(text)
                 else "certificates" if CERT_KW.search(text) else "items")
        return "existence", {"field": field, "per_item": m.group(1), "max_items": "1"}
    return None


def extract_tender_submission_date(pages: list[tuple[int, str]]) -> str | None:
    """Return the unambiguous date explicitly labeled as the tender submission deadline."""
    dates = set()
    for _, page in pages:
        for match in _RE_SUBMISSION_DATE.finditer(normalize_no_ws(page)):
            try:
                dates.add(date(*(int(value) for value in match.groups())).isoformat())
            except ValueError:
                return None
    return next(iter(dates)) if len(dates) == 1 else None


def maturity_certificate_requirements(text: str, submission_date: str | None = None) -> dict[str, Any]:
    """Read explicitly stated maturity level and validity requirements from one clause."""
    from app.engine.objective import _CN_NUM

    text = normalize_no_ws(text)
    matches = list(_RE_MATURITY_LEVEL.finditer(text))
    levels = {_CN_NUM.get(match[1], match[1]) for match in matches}
    if len(levels) != 1 or not matches[-1][2]:
        return {}
    result: dict[str, Any] = {"cert_min_level": int(levels.pop())}
    if re.search(r"有效期(?:之)?内|有效的(?:CMMI|CSMM|能力成熟度)", text, re.I):
        result["cert_require_current"] = True
        if submission_date:
            result["cert_reference_date"] = submission_date
    return result


def explicit_count_allocation(text: str) -> tuple[Decimal, dict[str, Any]] | None:
    """Extract the ceiling of one self-contained count clause from its source text."""
    form = classify_scoring_form(text)
    if form is None or len(list(_RE_SCORE_CLAUSE.finditer(text))) != 1:
        return None
    kind, params = form
    caps = list(_RE_CAP.finditer(text))
    if kind == "per_item" and len(caps) == 1:
        ceiling = Decimal(caps[0].group(1))
        per_item = Decimal(params["per_item"])
        if per_item <= 0 or ceiling < per_item or ceiling % per_item:
            return None
    elif kind == "existence" and not caps:
        ceiling = Decimal(params["per_item"])
    else:
        return None
    return (ceiling, {**params, "_objective_form": kind}) if ceiling > 0 else None


# ---------------------------------------------------------------------------
# 权重表数据行判定（compiler 抽取期剪枝与 normalize_package 退役共用）
# ---------------------------------------------------------------------------
# 利群 p125「标段|产品名称|所占样品分 → 1|利群(休闲细支)商标条盒|10分」实证：
# 权重声明行被 LLM 误抽成评分项（quote=null、与样品分重复计 10 分）。
# 判据全部来自文本形态，零项目专名；调用方另要求 source_quote 为空（有原文
# 引用的真评分项不动）。

_RE_WEIGHT_CTX = re.compile(r"产品名称|所占.{0,6}分|分值计算权重|评审权重|标段")
_RE_SCORING_LANG = re.compile(r"得\s*\d|扣\s*\d|评分|评审细则|评标细则|符合|满足")
_RE_BARE_VALUE = re.compile(r"[\d.]+\s*(?:分|%)\s*$")


def is_weight_table_item(name: str, text: str, quote: str | None,
                         pages_by_no: dict[int, str] | None) -> bool:
    """权重表数据行 → True。全部满足才判（宁放过不误杀）：
    1. source_quote 为空（有原文引用的真评分项不动）；
    2. name/text 无任何计分/评审语言，且 text 为空或仅形如「N分/N%」；
    3. name 在招标文件页文本中命中权重表上下文：邻近 80 字符内出现
       产品名称/所占分值/分值计算权重/评审权重/标段，或同行呈
       「数字 | name | 数字分|%」表格行形态且上下文带权重列头词。
    """
    if (quote or "").strip():
        return False
    name = (name or "").strip()
    if not name:
        return False
    text = (text or "").strip()
    if _RE_SCORING_LANG.search(name) or _RE_SCORING_LANG.search(text):
        return False
    if text and not _RE_BARE_VALUE.search(text):
        return False
    for ptext in (pages_by_no or {}).values():
        if not ptext or name not in ptext:
            continue
        for m in re.finditer(re.escape(name), ptext):
            window = ptext[max(0, m.start() - 80): m.end() + 80]
            if _RE_WEIGHT_CTX.search(window):
                return True
            line_start = ptext.rfind("\n", 0, m.start()) + 1
            line_end = ptext.find("\n", m.end())
            line = ptext[line_start: line_end if line_end != -1 else len(ptext)]
            compact = re.sub(r"\s+", "", line)
            if re.search(
                r"\d+[|｜]%s[|｜][\d.]+(?:分|%%)" % re.escape(name), compact
            ) and _RE_WEIGHT_CTX.search(compact):
                return True
    return False
