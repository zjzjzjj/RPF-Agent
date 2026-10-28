"""规则编译：招标文件条款 → LLM 抽取 → 草稿规则包 + 证据引用 + 六态分诊。

流程（对应方案 ③④ 步）：
1. 取招标文件全文（页序拼接，与 document_pages 逐字一致，保证引文可定位）；
2. 走 LLM 网关抽取（真实模型语义抽取）；
3. 每条规则的 quote 建证据引用并核验（无证据不评分）；
4. 六态分诊：verified / needs_input / ambiguous（档位无分值）/ gap / conflict / invalid；
5. 跑 12 条确定性校验器，findings 落库。
"""
from __future__ import annotations

import logging
import re
import time
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app import config
from app.db.models import (
    Criterion,
    CriterionCheck,
    CriterionLevel,
    Document,
    DocumentPage,
    DocNode,
    EvidenceCitation,
    PackageValidation,
    PriceFormula,
    RulePackage,
)
from app.db.session import session_scope
from app.errors import AppError, NotFound
from app.evidence.verifier import verify_citation
from app.jobs.handlers import register
from app.jobs.queue import JobContext
from app.llm.base import LLMError, LLMResult
from app.llm.gateway import Gateway, get_gateway
from app.parsing.normalize import normalize_no_ws
from app.rules import prompts
from app.rules.model import ExtractedCriterion, RuleExtraction

log = logging.getLogger("bid_eval.rules")

MAX_COMPILE_CHARS = 30_000  # 送进模型的招标文件字符预算（超预算按章节优先级选取，不再盲目截头）

# 中文按 ~1.4 字符/token 估算（Qwen 实测 29794 字符 ≈ 20147 tokens，即 1.48）。
# 取更保守的 1.4：宁可多留输出空间，也不要让输入把上下文吃光。
_CHARS_PER_TOKEN = 1.4
_CONTEXT_SAFETY_TOKENS = 512


def compile_char_budget() -> int:
    """按模型上下文推导可送入的字符预算；未配置上下文时沿用 MAX_COMPILE_CHARS。

    必须同时给输出留够空间：编译产物是一份完整规则 JSON（可达数千 token）。
    只用 provider 层的 max_tokens 钳制不够——输入占满 20K 后仅剩 4K 输出，
    JSON 必然被截断（2026-10-20 实证：本地 Qwen3-8B 24576 上下文）。
    """
    ctx = config.llm_context_tokens()
    if ctx <= 0:
        return MAX_COMPILE_CHARS
    out = config.llm_max_output_tokens()
    usable = ctx - out - _CONTEXT_SAFETY_TOKENS
    if usable <= 0:
        return MAX_COMPILE_CHARS  # 配置异常时不额外收紧，交给 provider 报错
    return min(MAX_COMPILE_CHARS, int(usable * _CHARS_PER_TOKEN))


def split_for_extraction(
    text: str,
    chunk_chars: int | None = None,
    protected_spans: list[tuple[int, int]] | None = None,
) -> list[str]:
    """把选段按行边界切成若干块，每块尽量不超过 chunk_chars 字符。

    分块是长文档可靠抽取的前提：规则 JSON 的输出量随文档线性增长，而模型的
    单次输出上限是固定的。按行切分保证不会在句子中间断开（条款跨块丢失）。

    protected_spans：原文字符区间 [start, end) 列表（如评标细则章节），span 内
    不切开——即使单块因此超出 size（上限 = span 长度）。不传时行为与旧版一致。
    """
    size = chunk_chars if chunk_chars is not None else config.compile_chunk_chars()
    if size <= 0 or len(text) <= size:
        return [text]

    # 预切片为 (是否原子块, 片段)：protected span 整段原子；其余按行（精确保留原文）
    def _line_pieces(region: str) -> list[tuple[bool, str]]:
        if not region:
            return []
        parts = region.split("\n")
        out = [(False, p + "\n") for p in parts[:-1]]
        if parts[-1]:
            out.append((False, parts[-1]))
        return out

    pieces: list[tuple[bool, str]] = []
    spans = sorted((s, e) for s, e in (protected_spans or []) if 0 <= s < e <= len(text))
    cursor = 0
    for s, e in spans:
        if s < cursor:  # 重叠/乱序 span 容错
            continue
        pieces.extend(_line_pieces(text[cursor:s]))
        pieces.append((True, text[s:e]))  # 原子块
        cursor = e
    pieces.extend(_line_pieces(text[cursor:]))

    chunks: list[str] = []
    buf: list[str] = []
    used = 0
    for atomic, piece in pieces:
        if atomic:
            if buf:
                chunks.append("".join(buf))
                buf, used = [], 0
            chunks.append(piece)
            continue
        # 单行本身超长：先冲掉缓冲区，再硬切成整块
        if len(piece) > size:
            if buf:
                chunks.append("".join(buf))
                buf, used = [], 0
            while len(piece) > size:
                chunks.append(piece[:size])
                piece = piece[size:]
            if piece.strip():
                buf.append(piece)
                used = len(piece)
            continue
        if buf and used + len(piece) > size:
            chunks.append("".join(buf))
            buf, used = [], 0
        buf.append(piece)
        used += len(piece)
    if buf:
        chunks.append("".join(buf))
    return [c for c in chunks if c.strip()] or [text]


def dedupe_extracted(items: list["ExtractedCriterion"]) -> list["ExtractedCriterion"]:
    """跨块去重：只丢**真重复**，同名不同分值的子项行全部保留。

    分块边界附近的上下文重叠会让模型在相邻两块里各抽一次同一条规则——
    那是（category, 名称, score_type, 分值）全同的真重复，丢后条。

    但评标细则表的子项行常共享父项名称（"服务保障情况（10）"下的 4 个子项
    都被模型命名为"服务保障情况"），旧版按 (category, 名称) 去重会把
    0-3/0-3/0-2 子项误杀（多媒体维保项目实证：总分 91≠100）。
    同名但分值/评分类型不同的条目现在全部保留；同名且恰有一方分值缺失时
    保留有分值的那条并把缺失方的描述/引文并入。
    """
    exact_seen: set[tuple[str, str, str, str]] = set()
    by_name: dict[tuple[str, str], int] = {}  # (category, name) → out 下标
    out: list["ExtractedCriterion"] = []
    for c in items:
        name_key = (c.category, normalize_no_ws(c.name or ""))
        exact_key = (*name_key, c.score_type, str(c.max_score))
        if exact_key in exact_seen:
            continue
        idx = by_name.get(name_key)
        if idx is not None:
            prev = out[idx]
            same_kind = prev.score_type == c.score_type
            if same_kind and prev.max_score is not None and c.max_score is None:
                # 同名一有一无：保有分值的，把缺失方的描述/引文并入
                if c.description and not prev.description:
                    prev.description = c.description
                if c.quote and not prev.quote:
                    prev.quote = c.quote
                if c.required_evidence and not prev.required_evidence:
                    prev.required_evidence = c.required_evidence
                exact_seen.add(exact_key)
                continue
            if same_kind and prev.max_score is None and c.max_score is not None:
                # 后条有分值：就地提升为权威条目（保序：仍占原位置）
                if prev.description and not c.description:
                    c.description = prev.description
                if prev.quote and not c.quote:
                    c.quote = prev.quote
                if prev.required_evidence and not c.required_evidence:
                    c.required_evidence = prev.required_evidence
                out[idx] = c
                exact_seen.add(exact_key)
                continue
            # 同名不同分值/类型 → 子项行，全部保留
        else:
            by_name[name_key] = len(out)
        exact_seen.add(exact_key)
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# 抽取结果的确定性补全（8B 模型经常漏字段，不能把正确性押在提示词上）
# ---------------------------------------------------------------------------
# 容忍"每低于一个百分点"（多媒体维保项目 p137 实测措辞："每低于一个百分点扣0.4 分"）
_RE_RATE_NUM = r"(?:\d+(?:\.\d+)?|一)"
_RE_RATE_DOWN = re.compile(
    r"每(?:低(?:于)?|下浮)\s*(" + _RE_RATE_NUM + r")\s*个百分点[^。；\n]{0,12}?扣\s*(\d+(?:\.\d+)?)\s*分"
)
_RE_RATE_UP = re.compile(
    r"每(?:高(?:于)?|上浮)\s*(" + _RE_RATE_NUM + r")\s*个百分点[^。；\n]{0,12}?扣\s*(\d+(?:\.\d+)?)\s*分"
)
# 公式式扣分率（语料 3/4 份）："投标报价得分（报价高于基准值）=50-（投标报价-基准值）/基准值*100*0.4"
# → deduct_rate_up=0.4；"（报价低于基准值）=50-（基准值-投标报价）/基准值*100*0.2" → deduct_rate_down=0.2
# 匹配：括注方向 + 公式体尾部的最后一个乘数（扣分率）
_RE_RATE_FORMULA_UP = re.compile(
    r"[（(]报价高于基准值[)）][^=]*=[^（]{0,20}?[（(]投标报价-基准值[)）]\s*/\s*基准值\s*[*×xX]\s*100\s*[*×xX]\s*(\d+(?:\.\d+)?)"
)
_RE_RATE_FORMULA_DOWN = re.compile(
    r"[（(]报价低于基准值[)）][^=]*=[^（]{0,20}?[（(]基准值-投标报价[)）]\s*/\s*基准值\s*[*×xX]\s*100\s*[*×xX]\s*(\d+(?:\.\d+)?)"
)
# "扣除/去掉一个最高价和一个最低价"（含"最高报价/投标报价"变体，语料烟叶/薄片实证）；
# 4-way 形态："去掉一个最高价、一个次高价、一个最低价和一个次低价"
_RE_TRIM_BOTH = re.compile(
    r"(?:扣除|去掉|剔除)一个最高(?:投标)?(?:报)?价[和、及与][^。；]{0,20}?一个最低(?:投标)?(?:报)?价"
)
_RE_TRIM_FOUR = re.compile(r"次高价")
_RE_ARITH_AVG = re.compile(r"算术平均")
_RE_MAX_ITEM_SCORE = re.compile(r"最高(?:得|扣)?\s*(\d+(?:\.\d+)?)\s*分")
_RE_AMOUNT_WAN = re.compile(r"合同金额(?:达到|≥|不低于|不少于)?\s*(\d+(?:\.\d+)?)\s*万元")

# --- 分档截尾的通用家数条件（P0-A 泛化：不再硬编码 5/10 边界） -------------------
# 语料实证边界：5/10（多媒体维保/数据库）、10/11（去 2 高 2 低档）、15/30（老代商标）
_RE_TIER_UNIT = r"(?:\s*(?:个|家|户))?"
# 低档："≤5 个" / "不足 6 家" / "等于或不足 15 家" / "不超过 5 个"
_RE_TIER_LOW = re.compile(
    r"(?:[≤<=]\s*(\d+)" + _RE_TIER_UNIT
    + r"|(?:等于或)?不足\s*(\d+)" + _RE_TIER_UNIT
    + r"|不超过\s*(\d+)" + _RE_TIER_UNIT
    + r")"
)
# 中档："5 个＜…≤10 个" / "超过 6 家且不足 11 家"（取上界）/ "9≥报价≥6"（反向，取首数为上界）
_RE_TIER_MID = re.compile(
    r"(\d+)" + _RE_TIER_UNIT + r"\s*[＜<][^。；]{0,40}?[≤<=]\s*(\d+)" + _RE_TIER_UNIT
    + r"|(?:超过|大于)\s*(\d+)" + _RE_TIER_UNIT
    + r"[^。；]{0,25}?(?:且不足|且小于|至|—|–|~|－|-)\s*(\d+)" + _RE_TIER_UNIT
    + r"|(\d+)" + _RE_TIER_UNIT + r"\s*[≥>][=]?[^。；]{0,20}?[≥>][=]?\s*(\d+)" + _RE_TIER_UNIT
)
# 高档（开区间顶）："＞10 个" / "超过 15 家" / "≥30 家"
_RE_TIER_HIGH = re.compile(
    r"[＞>]\s*(\d+)" + _RE_TIER_UNIT
    + r"|(?:超过|大于)\s*(\d+)" + _RE_TIER_UNIT
    + r"|≥\s*(\d+)" + _RE_TIER_UNIT
    + r"|\d+\s*(?:个|家)?\s*以上"
)
# 分档句内"不截尾"显式措辞
_RE_TIER_NO_TRIM = re.compile(r"不(?:再去|去|剔除|扣除|去掉)|则不")
# 非分档语境的家数句（竞争性判断/离群重算 fallback）——不是基准价分档表，须跳过
_RE_TIER_SKIP = re.compile(r"竞争性|评标委员会|继续进行|招标失败|终止|未剔除|不影响竞争")
# "去掉 N 个最高…和 M 个最低"数字式（休闲细支"去掉 2 个最高和 2 个最低"）
_RE_TRIM_N_PAIR = re.compile(
    r"(?:去掉?|剔除|扣除)\s*(\d+)\s*个(?:最高的?|投标报价|报价)[^。；\n]{0,30}?"
    r"(?:去掉?|剔除|扣除)?\s*(\d+)\s*个(?:最低的?|投标报价|报价)"
)
# 家数条件（单档截尾阈值）："＞5 个" / "超过 5 家" / "≥6 家"
_RE_TRIM_GT = re.compile(r"(?:[＞>]|超过|大于|≥)\s*(\d+)\s*(?:个|家|户)")
# 基准价乘系数："算术平均值*0.95" / "平均价×0.95"（K 只认原文明示数值）
_RE_K_FACTOR = re.compile(r"(?:平均值|平均价|评标基准价|基准价)\s*[*×xX]\s*(0\.\d{1,2}|1\.\d{1,2})")
# 次低价基准（"次低价为最佳报价值"/"次低投标报价为最佳报价值"）；
# 必须紧接"为"——"去掉一个次低价后的算术平均值…为评标基准价"是截尾句不是次低价基准
_RE_SECOND_LOWEST = re.compile(r"次低(?:有效)?(?:投标)?(?:报价|价)\s*为(?:最佳报价值|评标基准价)")
# 二次算术平均：有"第一次算术平均"且有"低于…再…平均"
_RE_SECOND_AVG_FIRST = re.compile(r"第[一二]次算术平均")
_RE_SECOND_AVG_BELOW = re.compile(r"低于[^。；\n]{0,20}(?:平均值|平均价)[^。；\n]{0,30}?(?:再|然后)[^。；\n]{0,15}?算术平均")
# 特殊规则（引擎暂不执行，原文留档 + 校验器提示人工复核）
_RE_SPECIAL_70 = re.compile(r"[^。；\n]{0,60}不进入最佳报价计算[^。；\n]{0,20}")
_RE_SPECIAL_OUTLIER = re.compile(r"[^。；\n]{0,80}剔除该报价后重新计算[^。；\n]{0,20}")
_PRICE_HINTS = ("报价", "价格", "评标价", "基准价", "投标价")


def _rate_per_point(match: re.Match[str]) -> Decimal:
    """把"每低 X 个百分点扣 Y 分"换算成每 1 个百分点扣分（"一个"= 1）。"""
    points_raw = match.group(1)
    points = Decimal("1") if points_raw == "一" else Decimal(points_raw)
    deduct = Decimal(match.group(2))
    return (deduct / points).quantize(Decimal("0.0001"))


def _cut_for_tier_segment(seg: str, full_text: str) -> int | None:
    """一个档位句的截尾家数。None=该句没写截尾措辞（回退全文启发式）。"""
    if _RE_TIER_NO_TRIM.search(seg) and not _RE_TRIM_BOTH.search(seg):
        return 0
    m_pair = _RE_TRIM_N_PAIR.search(seg)
    if m_pair:
        return max(int(m_pair.group(1)), int(m_pair.group(2)))
    if _RE_TRIM_FOUR.search(seg):
        return 2
    if _RE_TRIM_BOTH.search(seg):
        return 1
    return None


def _parse_trim_tiers(text: str) -> list[list[Any]] | None:
    """按有效家数分档截尾 → [[bound, cut], ...]（bound 升序，末档 bound=None）。

    泛化（P0-A）：档位边界从原文提取，不再硬编码 5/10——语料实证边界包括
    5/10（多媒体维保/数据库）、10/11（去 2 高 2 低）、15/30（老代商标休闲细支/山外山）。
    结构语义：n ≤ bound（None=+∞）落入该档，取**最小的 bound ≥ n** 档的 cut。
    仅当原文出现 ≥2 个档位条件时返回；单档（"＞5 个扣除最高最低"）返回 None 走旧键。
    """
    t = text or ""
    segments = re.split(r"[。；\n]", t)
    tier_by_bound: dict[int, int] = {}  # 上界 → 截尾家数
    top_cut: int | None = None
    for seg in segments:
        if _RE_TIER_SKIP.search(seg):
            continue  # 竞争性判断/离群重算 fallback 的家数句不是分档表
        m_mid = _RE_TIER_MID.search(seg)
        if m_mid:
            # 正向 "≤N"/"…＜M" 取后界；反向 "M≥X≥N" 取首数（M 为该档上界）
            upper = m_mid.group(2) or m_mid.group(4) or m_mid.group(5)
            cut = _cut_for_tier_segment(seg, t)
            if cut is None:
                # 中档句本身没写截尾措辞 → 按全文措辞回退
                cut = 2 if _RE_TRIM_FOUR.search(t) else (
                    1 if (_RE_TRIM_BOTH.search(t) or _RE_TRIM_N_PAIR.search(t)) else 0
                )
            if upper:
                tier_by_bound[int(upper)] = cut
            continue
        m_low = _RE_TIER_LOW.search(seg)
        if m_low and not _RE_TIER_HIGH.search(seg):
            upper = next(g for g in m_low.groups() if g is not None)
            cut = _cut_for_tier_segment(seg, t)
            if cut is None:
                # 低档句没写截尾措辞 = 该档不截（"≤N 家全平均"语义）
                cut = 0
            tier_by_bound[int(upper)] = cut
            continue
        m_high = _RE_TIER_HIGH.search(seg)
        if m_high and not _RE_TIER_MID.search(seg) and not _RE_TIER_LOW.search(seg):
            cut = _cut_for_tier_segment(seg, t)
            if cut is not None:
                top_cut = cut
    # 高档句没写截尾措辞（写在相邻句）→ 按全文措辞回退：次高次低=2、最高最低=1
    if top_cut is None and tier_by_bound:
        if _RE_TRIM_FOUR.search(t):
            top_cut = 2
        elif _RE_TRIM_BOTH.search(t) or _RE_TRIM_N_PAIR.search(t):
            top_cut = 1
        elif _RE_TIER_NO_TRIM.search(t):
            top_cut = 0
    if len(tier_by_bound) < 2 or top_cut is None:
        return None  # 单档或结构不完整 → 走旧键（trim_count/trim_threshold）
    tiers = [[bound, cut] for bound, cut in sorted(tier_by_bound.items())]
    tiers.append([None, top_cut])
    return tiers


def _parse_benchmark_gate(text: str) -> dict[str, Any] | None:
    """基准价资格门槛结构化（语料 5+1 形态）。

    返回 benchmark_gate={min_ratio, ratio_kind, gate_on, on_fail, half_max_ratio,
    still_reviewable, source_text}；识别不出返回 None。数值只认原文明示。
    """
    t = text or ""
    for seg in re.split(r"[。；\n]", t):
        if not re.search(
            r"不(?:进入|纳入)(?:最佳报价|最佳报价值|评标基准价)|赋(?:值为)?满分|从报价分满分", seg
        ):
            continue
        gate: dict[str, Any] = {"source_text": seg.strip()}
        if re.search(r"样品", seg) and re.search(r"0\s*分|得分为\s*0", seg):
            gate["gate_on"] = "sample_or_composite"
        elif re.search(r"技术资信", seg):
            gate["gate_on"] = "tech_qualification"
        elif re.search(r"商务分\s*\+\s*技术分|商务\s*\+\s*技术", seg):
            gate["gate_on"] = "biz_tech"
        else:
            gate["gate_on"] = "tech"
        m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", seg)
        m_abs = re.search(r"即\s*(\d+(?:\.\d+)?)\s*分", seg) or re.search(
            r"(?:[＜<]|低于|不足)\s*(\d+(?:\.\d+)?)\s*分", seg
        )
        if m_pct:
            gate["min_ratio"] = m_pct.group(1)
            gate["ratio_kind"] = "percent"
        elif m_abs:
            gate["min_ratio"] = m_abs.group(1)
            gate["ratio_kind"] = "absolute"
        else:
            continue  # 门槛句但比例缺失 → 不结构化（校验端 PRICE_GATE_INCOMPLETE 不会见到残缺件）
        m_half = re.search(r"(?:满分报价分的?|赋(?:值为)?满分(?:报价分)?的?)\s*(\d+(?:\.\d+)?)\s*%", seg)
        if re.search(r"赋(?:值为)?满分|从报价分满分", seg):
            gate["on_fail"] = "half_max"
            if m_half:
                gate["half_max_ratio"] = m_half.group(1)
        else:
            gate["on_fail"] = "exclude"
        # 门槛前置条件："投标有效家数超过5家…才不进入最佳报价计算"——
        # 超过 N 家 → min_valid_bidders=N+1（有效家数 ≤ N 时门槛不生效）。
        # 缺此条件会把有条件门槛当无条件执行（2026-10-27 二维码项目实证）。
        m_valid = re.search(
            r"(?:有效(?:投标)?(?:家数|投标人数量|投标人|家))[^。；\n]{0,15}(?:超过|大于|＞|>)\s*(\d+)\s*(?:家|个)?",
            seg,
        )
        if m_valid:
            gate["min_valid_bidders"] = int(m_valid.group(1)) + 1
        if re.search(r"未参与评标基准价计算[^。；\n]{0,30}(?:可以|方可|仍可)进入", t):
            gate["still_reviewable"] = True
        return gate
    # 后备句式（烟丝仪器型）："进入商务报价最佳报价值需满足以下条件：……技术……70%及以上"
    m_enter = re.search(
        r"进入[^。；\n]{0,25}?(?:最佳报价值|基准价)[^。；\n]{0,25}?条件[\s\S]{0,150}?(\d+(?:\.\d+)?)\s*%", t
    )
    if m_enter:
        span = m_enter.group(0)
        gate = {
            "min_ratio": m_enter.group(1),
            "ratio_kind": "percent",
            "on_fail": "exclude",
            "source_text": span.strip(),
        }
        if re.search(r"技术资信", span):
            gate["gate_on"] = "tech_qualification"
        elif re.search(r"商务分\s*\+\s*技术分|商务\s*\+\s*技术", span):
            gate["gate_on"] = "biz_tech"
        else:
            gate["gate_on"] = "tech"
        return gate
    return None


def _parse_outlier_policy(text: str) -> dict[str, Any] | None:
    """离群保护结构化（数值式"＞*2 或 ＜/2"与文字式"两倍/一半"）。

    返回 outlier_policy={max_mult, min_mult, on_hit, source_text}；无原文不产出。
    遍历全部"剔除…重新计算"匹配取首个能抽出阈值的（赋半分句常也带该短语，
    21 文件回放实证：只看首个匹配会被门槛句抢占而漏掉真正的离群句）。
    """
    t = text or ""
    for m_frag in _RE_SPECIAL_OUTLIER.finditer(t):
        seg = m_frag.group(0)
        policy: dict[str, Any] = {"on_hit": "zero_and_recompute", "source_text": seg.strip()}
        m_max = re.search(r"[＞>]\s*[^。；]{0,25}?[*×xX]\s*(\d+(?:\.\d+)?)", seg)
        m_min = re.search(r"[＜<]\s*[^。；]{0,25}?[/／]\s*(\d+(?:\.\d+)?)", seg)
        if m_max:
            policy["max_mult"] = m_max.group(1)
        elif re.search(r"两倍|2\s*倍", seg):
            policy["max_mult"] = "2"
        if m_min:
            try:
                policy["min_mult"] = str((Decimal(1) / Decimal(m_min.group(1))).normalize())
            except Exception:  # noqa: BLE001
                pass
        elif re.search(r"一半", seg):
            policy["min_mult"] = "0.5"
        if "max_mult" in policy or "min_mult" in policy:
            return policy
    return None


def parse_price_formula_text(text: str) -> dict[str, Any]:
    """从一段招标原文解析偏离扣分参数；非偏离扣分型返回空 dict。

    供编译期（enrich_extracted）与草稿自愈（normalize_package）共用。
    P0-A 起输出确定性目录的完整结构化：基准法六变体 / k / trim_tiers /
    benchmark_gate / outlier_policy；数值只认原文明示（红线：不猜默认值）。

    入口做空白归一（21 文件回放实证）：pymupdf 文本行内换行/空格会让
    `[^。；\\n]` 类正则跨行失配——整段去空白后再匹配，与 normalize_no_ws 同哲学。
    """
    text = re.sub(r"[\s　]+", "", text or "")

    # 基准价方法目录（优先级从具体到一般；语料六变体）——与扣分率检测**独立**，
    # 互不为前提（2026-10-28 重构：旧实现在扣分率未命中时早退，导致
    # 只有"基准法 + 公式式扣分率"或"只有基准法"的形态整套丢失——语料 3/4 份
    # 为公式式扣分率，实测全部返回空）。
    out: dict[str, Any] = {}
    if _RE_SECOND_LOWEST.search(text):
        out["benchmark_method"] = "second_lowest"
    elif _RE_SECOND_AVG_FIRST.search(text) and _RE_SECOND_AVG_BELOW.search(text):
        # 复合：≥N 家首均先去极值，再对低于首均者二次算术平均（实验室设备/第二批检测/重招）
        out["benchmark_method"] = "average_trimmed_then_below"
        m_gt = _RE_TRIM_GT.search(text)
        out["trim_count"] = 1
        out["trim_threshold"] = int(m_gt.group(1)) if m_gt else 6
    else:
        tiers = _parse_trim_tiers(text)
        if tiers is not None:
            # 分档截尾：引擎（price.py）按 tiers 精确执行
            out["benchmark_method"] = "average_trimmed"
            out["trim_count"] = 1
            out["trim_threshold"] = int(tiers[0][0])
            out["trim_tiers"] = tiers
        elif _RE_TRIM_BOTH.search(text):
            out["benchmark_method"] = "average_trimmed"
            out["trim_count"] = 1
            m_gt = _RE_TRIM_GT.search(text)
            if m_gt:
                out["trim_threshold"] = int(m_gt.group(1))
            # 2026-10-28：不再默认 trim_threshold=5（硬编码猜测）——未明示家数
            # 阈值时不写入，引擎侧 trim_threshold or 0 → 无条件截尾，与
            # "扣除一个最高价和一个最低价后的算术平均值"原文一致。
        elif _RE_ARITH_AVG.search(text):
            out["benchmark_method"] = "average"

    # 扣分率（多形态独立检测）：句式 "每低(于) X 个百分点扣 Y 分" 或
    # 公式式 "=满分-（投标报价-基准值）/基准值*100*Y"（语料 3/4 份）
    m_down = _RE_RATE_DOWN.search(text)
    m_up = _RE_RATE_UP.search(text)
    if m_down:
        out["deduct_rate_down"] = _rate_per_point(m_down)
    if m_up:
        out["deduct_rate_up"] = _rate_per_point(m_up)
    if "deduct_rate_up" not in out:
        m_fu = _RE_RATE_FORMULA_UP.search(text)
        if m_fu:
            out["deduct_rate_up"] = Decimal(m_fu.group(1))
    if "deduct_rate_down" not in out:
        m_fd = _RE_RATE_FORMULA_DOWN.search(text)
        if m_fd:
            out["deduct_rate_down"] = Decimal(m_fd.group(1))

    if not out:
        return {}  # 基准法与扣分率均未识别——交调用方报 PRICE_FORMULA_UNRECOGNIZED

    # formula_type：有扣分率 → deviation_deduct；只识别出基准法 → benchmark_ratio
    if "deduct_rate_down" in out or "deduct_rate_up" in out:
        out["formula_type"] = "deviation_deduct"
    else:
        out["formula_type"] = "benchmark_ratio"

    k = _extract_k_factor(text)
    if k is not None:
        out["k"] = k
    # 2026-10-28：不再默认 k=1——未明示则不设，由 PRICE_PARAMS_INCOMPLETE 阻断
    # （benchmark_ratio 型缺 k 是明确的阻断信号；deviation_deduct 型引擎侧
    # k 仅用于基准价乘法，缺省时引擎 or 1 维持旧行为）。
    # 特殊规则（商技 70% 门槛/超倍半价剔除重算）：原文留档 + 结构化（P1 执行）
    special = []
    for m in (_RE_SPECIAL_70, _RE_SPECIAL_OUTLIER):
        frag = m.search(text or "")
        if frag:
            special.append(frag.group(0).strip())
    if special:
        out["special_rules"] = special
    gate = _parse_benchmark_gate(text)
    if gate is not None:
        out["benchmark_gate"] = gate
    outlier = _parse_outlier_policy(text)
    if outlier is not None:
        out["outlier_policy"] = outlier
    return out


def _extract_k_factor(text: str) -> Decimal | None:
    """基准价乘系数（"算术平均值*0.95"）。只认原文明示；无匹配返回 None。"""
    m = _RE_K_FACTOR.search(text or "")
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except Exception:  # noqa: BLE001
        return None


def parse_count_params_text(
    text: str,
    max_score: Decimal | None,
    per_item: Decimal | None,
    name: str = "",
) -> dict[str, str]:
    """从评分项原文补 count 参数：最高项数/金额门槛/人员范围。"""
    params: dict[str, str] = {}
    if per_item is not None and per_item > 0 and max_score is not None:
        derived = int(Decimal(str(max_score)) / per_item)
        if derived > 0:
            params["max_items"] = str(derived)
    m = _RE_AMOUNT_WAN.search(text or "")
    if m:
        params["min_amount_yuan"] = str(int(Decimal(m.group(1)) * Decimal(10000)))
    # 人员范围只看评分项名称（描述里常出现"项目负责人/团队"只是证明材料要求）
    if "团队" in name or "成员" in name:
        params["person_scope"] = "member"
    elif "负责人" in name:
        params["person_scope"] = "leader"
    return params


def _price_window(c: "ExtractedCriterion", tender_text: str) -> str:
    """价格项的正则作用域：先看自身描述/quote，再看 quote 之后的窗口。

    不能扫全文——正文里的工期/违约条款也会出现"每低/每高 1 个百分点扣…"。
    """
    own = f"{c.description or ''}\n{c.quote or ''}"
    if _RE_RATE_DOWN.search(own) or _RE_RATE_UP.search(own):
        return own
    quote = (c.quote or "").strip()
    if quote:
        # 前 20 字里通常没有换行，直接定位；找不到再用去空白匹配
        pos = tender_text.find(quote[:20])
        if pos < 0:
            q_norm = normalize_no_ws(quote)
            t_norm = normalize_no_ws(tender_text)
            npos = t_norm.find(q_norm[:20])
            if npos >= 0:
                # 归一化索引无法直接回映原文，退化为从价格关键词附近找
                for hint in _PRICE_HINTS:
                    pos = tender_text.find(hint)
                    if pos >= 0:
                        break
        if pos >= 0:
            return tender_text[pos : pos + 4000]
    return own


# 报价修正/澄清规则：是"怎么算"，不是"什么情形否决"。
# 2026-10-21 实证：模型把「报价错误修正原则」抽成 score_type=veto，校验器要求
# 否决后果（VETO_CONSEQUENCE_MISSING），而该条根本没有否决语义，用户无法定义。
_RE_CORRECTION_RULE = re.compile(
    r"修正原则|以大写金额为准|大写金额.{0,6}为准|以单价金额|单价金额.{0,6}为准|算术错误|澄清确认"
)
_RE_VETO_WORD = re.compile(r"否决|无效投标|作废标|取消.{0,8}资格|不予受理|不得参加|拒绝其投标")


def is_correction_rule(text: str) -> bool:
    return bool(_RE_CORRECTION_RULE.search(text or ""))


def has_veto_word(text: str) -> bool:
    return bool(_RE_VETO_WORD.search(text or ""))


def normalize_veto_semantics(c: "ExtractedCriterion") -> None:
    """否决项语义纠正（就地修改）。

    - 报价修正/澄清类规则被误标成 veto → 降级为 match（判断项，不计分、不否决）；
    - 真正的否决项漏填后果 → 补 disqualify（原文写"否决/无效"的默认后果）。
    """
    if c.score_type != "veto":
        return
    text = f"{c.name or ''}\n{c.description or ''}\n{c.quote or ''}"
    if is_correction_rule(text) and not has_veto_word(text):
        c.score_type = "match"
        c.is_veto = False
        c.max_score = None
        c.veto_consequence = None
        c.check_type = c.check_type or "human_only"
        return
    if not c.veto_consequence:
        c.veto_consequence = "disqualify"


def enrich_extracted(
    criteria: list["ExtractedCriterion"], tender_text: str
) -> list["ExtractedCriterion"]:
    """用确定性规则补齐 LLM 漏掉的公式/计数参数（就地修改并返回）。"""
    for c in criteria:
        normalize_veto_semantics(c)
        # P0-C：主/客观 hint（仅技术/商务计分类）。评分表"主/客观分"明文列值
        # 由模型照抄进 subjectivity_hint；缺席时用量化措辞兜底（"每…得 N 分"类）。
        # 只产 hint 不改 score_type——hint=objective 且 score_type=match 的冲突
        # 由校验器 AMBIGUOUS_SUBJECTIVITY 告警，交人工（歧义不猜测）。
        if c.category in ("technical", "commercial"):
            hint = c.subjectivity_hint if c.subjectivity_hint in (
                "objective", "subjective") else None
            if hint is None:
                text_c = f"{c.name or ''}\n{c.description or ''}\n{c.quote or ''}"
                if re.search(
                    r"客观分|每(?:个|项|人|条|家|份|证|次)?[^。；，,]{0,12}?得\s*\d+(?:\.\d+)?\s*分"
                    r"|最高(?:得|不超过)?\s*\d+(?:\.\d+)?\s*分",
                    text_c,
                ):
                    hint = "objective"
            if hint == "objective":
                params = dict(c.params or {})
                params.setdefault("_subjectivity_hint", hint)
                c.params = params
        # P1-C.6 样品分总分口径（代际反转实证：老代"四部分总和"样品入总分、
        # 新代"三部分总和"样品单列）——原文口径挂到样品项 params.in_total，
        # ranking 据此决定是否计入总分；无样品项/无口径句 → 不动（默认计入=旧行为）
        if "样品" in (c.name or ""):
            m_scope = re.search(
                r"综合得(?:分为|是)?以上\s*([三四])\s*部分得分的总和", tender_text or ""
            )
            if m_scope:
                params = dict(c.params or {})
                params["in_total"] = m_scope.group(1) == "四"
                c.params = params
        if c.category == "price":
            window = _price_window(c, tender_text)
            parsed = parse_price_formula_text(window)
            if parsed:
                # 2026-10-28：不再硬编码 formula_type/k——解析器已给出真实
                # formula_type（deviation_deduct 或 benchmark_ratio）与真实 k；
                # 未明示则不设默认，交 PRICE_PARAMS_INCOMPLETE 阻断（不猜默认值）。
                if parsed.get("formula_type"):
                    c.formula_type = parsed["formula_type"]
                if parsed.get("k") is not None:
                    c.k = parsed["k"]
                if parsed.get("deduct_rate_down") is not None:
                    c.deduct_rate_down = parsed["deduct_rate_down"]
                if parsed.get("deduct_rate_up") is not None:
                    c.deduct_rate_up = parsed["deduct_rate_up"]
                # 分档截尾/门槛/离群：引擎暂按近似执行，原文与结构化参数进 params 留档
                params = dict(c.params or {})
                for key in ("trim_tiers", "special_rules", "benchmark_gate", "outlier_policy"):
                    if parsed.get(key) is not None:
                        params.setdefault(key, parsed[key])
                # 基准法：确定性目录优先（G20 归一）；与模型自报标签冲突时留痕
                if parsed.get("benchmark_method"):
                    if (
                        c.benchmark_method
                        and c.benchmark_method != parsed["benchmark_method"]
                        and "_label_mismatch" not in params
                    ):
                        params["_label_mismatch"] = {
                            "model": c.benchmark_method,
                            "deterministic": parsed["benchmark_method"],
                        }
                    c.benchmark_method = parsed["benchmark_method"]
                    if parsed.get("trim_count") is not None:
                        c.trim_count = parsed["trim_count"]
                    if parsed.get("trim_threshold") is not None:
                        c.trim_threshold = parsed["trim_threshold"]
                elif _RE_TRIM_BOTH.search(window):
                    c.benchmark_method = "average_trimmed"
                    c.trim_count = 1
                    # 2026-10-28：不再硬编码 trim_threshold=5（语料分档阈值依原文而定），
                    # 只在原文明示家数阈值时才写入，否则交阻断。
                    m_gt_fb = _RE_TRIM_GT.search(re.sub(r"[\s　]+", "", window))
                    if m_gt_fb:
                        c.trim_threshold = int(m_gt_fb.group(1))
                elif _RE_ARITH_AVG.search(window) and not c.benchmark_method:
                    c.benchmark_method = "average"
                if params:
                    c.params = params

        if c.score_type == "count":
            params = dict(c.params or {})
            per_item = c.count_per_item
            if per_item is None and params.get("per_item") is not None:
                try:
                    per_item = Decimal(str(params["per_item"]))
                except Exception:  # noqa: BLE001
                    per_item = None
            if per_item is not None and c.count_per_item is None:
                c.count_per_item = per_item
            derived = parse_count_params_text(
                f"{c.description or ''}\n{c.quote or ''}",
                c.max_score,
                per_item,
                c.name,
            )
            for key, value in derived.items():
                params.setdefault(key, value)
            c.params = params
    return criteria


def material_block_applies(block: str, criterion_name: str) -> bool:
    """【证明材料】块是否适用于某个计数项（按主体词匹配，避免串到别的评分项）。"""
    name = criterion_name or ""
    if any(k in block for k in ("负责人", "团队", "成员", "人员")):
        return any(k in name for k in ("负责人", "团队", "成员", "人员"))
    if any(k in block for k in ("业绩", "案例", "合同")):
        return any(k in name for k in ("业绩", "案例", "合同"))
    return True


def fold_material_blocks(
    criteria: list["ExtractedCriterion"],
) -> list["ExtractedCriterion"]:
    """把单独抽出来的【证明材料】块折叠进同页 count 项，避免重复计分。

    真实标书里【证明材料】通常是评分项单元格的续行，模型会把它当成一条
    score_type=veto 的独立条目（浙江中烟项目实证：c30 导致总分 106）。
    """
    out: list["ExtractedCriterion"] = []
    for c in criteria:
        text = f"{c.description or ''}\n{c.quote or ''}".strip()
        is_material_block = text.startswith("【证明材料】")
        if is_material_block and c.score_type in ("veto", "match") and not c.is_veto:
            targets = [
                t
                for t in out
                if t.score_type == "count"
                and material_block_applies(text, t.name)
                and (
                    c.page_hint is None
                    or t.page_hint is None
                    or abs(int(t.page_hint) - int(c.page_hint)) <= 1
                )
            ]
            for t in targets:
                existing = (t.required_evidence or "").strip()
                if text not in existing:
                    t.required_evidence = (
                        f"{existing}\n{text}".strip() if existing else text
                    )
            continue  # 不再作为独立评分项落库
        out.append(c)
    return out


def _triage(c: ExtractedCriterion, quote_verified: bool, has_node: bool) -> str:
    """六态分诊。"""
    if c.score_type == "subjective" and c.levels and any(
        lv.min is None or lv.max is None for lv in c.levels
    ):
        return "ambiguous"  # 原文没给分值区间——不得由系统猜
    if not quote_verified or (c.quote is None):
        return "needs_input"
    # 主观项无档位不再阻断：引擎支持按 description 在 0-满分内评分，
    # 校验器 LEVELS_MISSING 也只是 warn（ADR 0004 的口径）。
    if c.score_type == "formula":
        if c.max_score is None:
            return "needs_input"
        if c.formula_type == "deviation_deduct":
            has_rate = bool(
                c.deduct_rate
                or c.deduct_rate_down
                or c.deduct_rate_up
                or (c.params or {}).get("deduct_rate_down")
                or (c.params or {}).get("deduct_rate_up")
            )
            if not has_rate:
                return "needs_input"
        elif c.k is None:
            return "needs_input"
    if c.score_type == "veto" and not c.check_type:
        return "needs_input"
    if has_node:
        return "verified"
    return "needs_input"


def _extract_chunk(
    gateway: "Gateway", chunk: str, *, data_class: str, label: str = ""
) -> tuple["RuleExtraction", "LLMResult"]:
    """单块抽取（契约不符允许一次修复重试）。"""
    user_prompt = prompts.RULE_USER_TEMPLATE.format(clause_text=chunk)
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            prompt = user_prompt
            if attempt == 2 and last_error is not None:
                prompt = (
                    f"{user_prompt}\n\n【修复请求】你上一次的输出存在如下问题：\n{last_error}\n"
                    "请修正后重新输出**完整**的 JSON（不要只输出差异部分）。"
                )
            return gateway.chat_json(
                prompts.RULE_SYSTEM_PROMPT,
                prompt,
                RuleExtraction,
                data_class=data_class,
                max_tokens=config.llm_max_output_tokens(),
            )
        except LLMError as exc:
            last_error = exc
            log.warning(
                "规则抽取%s第 %d 次尝试失败：%s",
                f"[分块 {label}]" if label else "", attempt, exc,
            )
            if attempt == 1:
                time.sleep(config.llm_retry_backoff_s())
    raise AppError(
        f"规则抽取失败（含修复重试）{f'（分块 {label}）' if label else ''}：{last_error}；"
        "JSON 非法转义/尾逗号已尝试自动修复仍失败，常见原因是模型输出超长被截断"
        "或输出里出现未转义的双引号——可换用上下文更大的模型、"
        "提高模型 max-model-len，或缩短招标文件后再试"
    )

# 章节优先级：真实招标文件的"评标办法/评分细则"通常排在最后（本文档在第 89-97 页），
# 盲目截前 N 字符会把它整段丢掉——评分项一条都抽不出来，只剩前附表的资格与否决条款。
# 这里按关键词给页分档，先保证评分办法进模型，再补资格与否决依据。
COMPILE_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "scoring",
        (
            "评标办法", "评标方法", "评分办法", "评分细则", "评分标准", "评分内容",
            "评审因素", "评审办法", "评审细则", "价格分", "报价部分", "最佳报价",
            "商务部分", "技术部分", "商务分", "技术分", "主观分", "客观分",
        ),
    ),
    (
        "eligibility",
        (
            "资格要求", "资格审查", "符合性审查", "否决", "废标", "无效标", "投标人须知",
        ),
    ),
)


def _load_tender_text(document_id: int) -> tuple[str, Document]:
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None:
            raise NotFound("文档不存在")
        if doc.kind not in ("tender", "addendum"):
            raise AppError("只能从招标文件编译规则")
        if doc.parse_status not in ("parsed", "degraded"):
            raise AppError(f"招标文件尚未解析完成（{doc.parse_status}）")
        rows = s.execute(
            select(DocumentPage).where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_no)
        ).scalars().all()
        text = "\n".join(p.text or "" for p in rows)
    return text, doc


def _load_tender_pages(document_id: int) -> list[tuple[int, str]]:
    from app.parsing.ocr_tags import clean_ocr_tags

    with session_scope() as s:
        rows = s.execute(
            select(DocumentPage.page_no, DocumentPage.text)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_no)
        ).all()
    # OCR 标签清理（PaddleOCR-VL 对表格页自发输出 <fcel>/<nl> 等，污染选段/抽取）
    return [(int(no), clean_ocr_tags(txt or "")) for no, txt in rows]


def select_compile_text(pages: list[tuple[int, str]], budget: int = MAX_COMPILE_CHARS) -> tuple[str, dict]:
    """章节感知选取：预算内优先保住评分办法，再补资格与否决依据。

    输出始终按页序拼接，页间以换行相连——与 ``_load_tender_text`` 同源同序，
    因此 LLM 给出的 quote 仍能在 document_pages 上逐字定位核验。

    返回 (text, meta)，meta 记录各档入选页数与是否发生裁剪，便于审计与排查。
    """
    full = "\n".join(t for _no, t in pages)
    if len(full) <= budget:
        return full, {
            "truncated": False,
            "chars": len(full),
            "total_chars": len(full),
            "pages": [no for no, _t in pages],
            "tiers": {},
        }

    by_page = dict(pages)
    chosen: set[int] = set()
    used = 0
    tier_hits: dict[str, int] = {}
    for name, keywords in COMPILE_TIERS:
        # 关键词只出现在章节首页是常态：续页（如"符合性审查"否决情形 (2)-(15)）自身零命中，
        # 若只按本页命中排序，续页会被同档位里低信息量的页挤掉。这里给相邻页半个权重，
        # 让章节成段入选。输出顺序另行按页序重排，不受影响。
        raw = {
            page_no: sum(1 for k in keywords if k in text)
            for page_no, text in pages
            if page_no not in chosen
        }
        order = [p for p, _t in pages]
        pos = {p: i for i, p in enumerate(order)}
        scored: list[tuple[float, int]] = []
        for page_no in order:
            if page_no in chosen:
                continue
            i = pos[page_no]
            neighbor = 0.0
            if i > 0:
                neighbor = max(neighbor, raw.get(order[i - 1], 0))
            if i + 1 < len(order):
                neighbor = max(neighbor, raw.get(order[i + 1], 0))
            score = raw.get(page_no, 0) + 0.5 * neighbor
            if score > 0:
                scored.append((score, page_no))
        scored.sort(key=lambda x: (-x[0], x[1]))
        picked = 0
        for _score, page_no in scored:
            cost = len(by_page[page_no]) + 1
            if used + cost > budget:
                continue
            chosen.add(page_no)
            used += cost
            picked += 1
        tier_hits[name] = picked

    # 预算未用满时按页序回填，尽量让模型看到上下文（不改变已入选页的相对顺序）
    if used < budget:
        for page_no, text in pages:
            if page_no in chosen:
                continue
            cost = len(text) + 1
            if used + cost > budget:
                break
            chosen.add(page_no)
            used += cost
        tier_hits["fill"] = len(chosen) - sum(tier_hits.values())

    selected = [(no, t) for no, t in pages if no in chosen]
    meta = {
        "truncated": True,
        "chars": sum(len(t) + 1 for _no, t in selected),
        "total_chars": len(full),
        "pages": [no for no, _t in selected],
        "tiers": tier_hits,
    }
    return "\n".join(t for _no, t in selected), meta


def _next_version(project_id: str) -> int:
    with session_scope() as s:
        row = s.execute(
            select(RulePackage.version).where(RulePackage.project_id == project_id)
            .order_by(RulePackage.version.desc()).limit(1)
        ).scalar_one_or_none()
    return (row or 0) + 1


def find_node_for_quote(document_id: int, page_no: int | None, quote: str) -> str | None:
    """在给定页（及相邻页）上找"包含该引文的最深节点"。

    两个真实世界的坑（浙江中烟项目实测，26 条里 19 条栽在这上面）：

    ① 软换行：节点文本里是换行，normalize 后成空格，而模型引文跨行逐字复制、不含
       空格。用朴素子串包含会因这一个空格失配——引文核验通过了，节点却定位不到。
    ② 跨页节点只归属一页：如"报价部分（60分）"节点落在 p96，而引文页码是 p97；
       模型引文也常非逐字（尾部有出入），证据层用 prefix_fallback 才通过。

    因此这里与证据核验保持同一口径：空白不敏感 + 相邻页 + 前缀逐级回退。
    """
    q = normalize_no_ws(quote)
    if not q:
        return None
    page = page_no or -1
    pages = [page] + [p for p in (page - 1, page + 1) if p > 0]
    with session_scope() as s:
        rows = s.execute(
            select(DocNode).where(DocNode.document_id == document_id, DocNode.page.in_(pages))
        ).scalars().all()
    needles = [q] + [normalize_no_ws(quote[:n]) for n in config.PREFIX_FALLBACK_LENGTHS]
    for needle in needles:
        if not needle:
            continue
        best: DocNode | None = None
        for n in rows:
            if needle in normalize_no_ws(n.text or ""):
                if best is None or n.depth > best.depth:
                    best = n
        if best is not None:
            return best.node_id
    return None


def _persist_criterion(
    s,  # noqa: ANN001
    package_id: int,
    document_id: int,
    c: ExtractedCriterion,
    seq: int,
    used_codes: set[str],
) -> tuple[int, int | None]:
    """写入一条评分项及其子结构；返回 (criterion_id, citation_id)。

    编号唯一性由这里兜底：真实模型可能返回重复或空编号，唯一约束必须先行满足。
    **不在本函数内做核验**——核验要用独立会话读引用，必须等外层事务提交后
    （SQLite 事务隔离下，未提交的行对其他连接不可见）。
    """
    citation_id: int | None = None
    if c.quote:
        cit = EvidenceCitation(
            document_id=document_id,
            quote=c.quote,
            page_no=c.page_hint,
            citation_role="criterion_source",
            created_by="llm",
        )
        s.add(cit)
        s.flush()
        citation_id = cit.id

    # 先按"未核验"落库；_verify_and_backfill 会在核验后重新分诊
    status = _triage(c, quote_verified=False, has_node=False)
    code = c.code.strip() or f"c{seq}"
    if code in used_codes:  # 模型返回重复编号 → 加序号后缀保证唯一
        n = 2
        base = code
        while f"{base}~{n}" in used_codes:
            n += 1
        code = f"{base}~{n}"
    used_codes.add(code)
    is_veto = c.score_type == "veto"
    crit = Criterion(
        package_id=package_id,
        code=code,
        category=c.category,
        name=c.name,
        description=c.description,
        # 否决项不得同时作为评分因素给分（法规红线）——忽略模型给的分值
        max_score=None if is_veto else c.max_score,
        weight=None,  # 权重永不来自模型
        score_type=c.score_type,
        is_veto=is_veto,
        veto_consequence=c.veto_consequence if is_veto else None,
        required_evidence=c.required_evidence,
        source_node_id=None,  # 核验通过后回填
        source_page=c.page_hint,
        source_quote=c.quote,
        status=status,
        confidence=c.confidence,
        sort_order=seq,
    )
    s.add(crit)
    s.flush()

    # 档位
    for lv in c.levels:
        s.add(
            CriterionLevel(
                criterion_id=crit.id,
                level_label=lv.label,
                score_min=lv.min if lv.min is not None else Decimal(0),
                score_max=lv.max if lv.max is not None else Decimal(0),
                description=lv.criteria,
                source_quote=lv.quote,
            )
        )

    # 客观检查原语（否决/匹配/计数类需要 params 供确定性引擎执行）。
    # 价格项的参数（trim_count 等）走 PriceFormula.params_json，不能误建 human_only check。
    # veto/match/count 一律建 check：缺 check 会让评分端落入「缺少可执行的检查
    # 定义」死项或「未知计数字段」恒 0（2026-10-26 run 54 c51/c57/c63、c64 实证）。
    wants_check = bool(c.check_type) or c.score_type in ("veto", "match", "count")
    if wants_check:
        from app.rules.check_infer import infer_check, infer_count_params
        from app.rules.freeze import _jsonable

        params = dict(c.params or {})
        # 契约允许模型单独给 count_per_item；确定性引擎只认 params["per_item"]，
        # 这里必须折叠，否则真实模型路径下 count 项全部恒 0 分。
        if c.count_per_item is not None and "per_item" not in params:
            params["per_item"] = c.count_per_item
        check_type = c.check_type or ""
        if c.score_type == "count":
            # 计分参数从评标细则原文补缺口（per_item/max_items/field/person_scope）
            params = infer_count_params(
                {"name": c.name, "description": c.description,
                 "source_quote": c.quote, "required_evidence": c.required_evidence},
                params,
            )
            check_type = check_type or "count_at_least"
        if not check_type:
            gen = infer_check(
                {"name": c.name, "description": c.description, "source_quote": c.quote}
            )
            if gen is not None:
                check_type, gen_params = gen
                params = {**gen_params, **params}  # 模型显式参数优先
            else:
                check_type = "human_only"
        s.add(
            CriterionCheck(
                criterion_id=crit.id,
                check_type=check_type,
                field=params.get("field") if isinstance(params.get("field"), str) else None,
                params_json=_jsonable(params),
                on_fail="recommend_reject" if is_veto else "need_human",
                source_quote=c.quote,
            )
        )

    # 价格公式：扣分率与基准法参数全部落盘（快照是评分唯一输入，漏一个字段
    # 就会让偏离扣分退化成"人人满分"）
    if c.category == "price":
        from app.rules.freeze import _jsonable

        ext: dict[str, Any] = {}
        if c.deduct_rate is not None:
            ext["deduct_rate"] = str(c.deduct_rate)
        if c.deduct_rate_down is not None:
            ext["deduct_rate_down"] = str(c.deduct_rate_down)
        if c.deduct_rate_up is not None:
            ext["deduct_rate_up"] = str(c.deduct_rate_up)
        if c.trim_count is not None:
            ext["trim_count"] = str(c.trim_count)
        if c.trim_threshold is not None:
            ext["trim_threshold"] = str(c.trim_threshold)
        # 分档截尾/特殊规则/门槛/离群：引擎暂按近似执行（trim_count/trim_threshold），
        # 完整原文与结构化参数进 params_json 留档，供 PRICE_ENGINE_APPROX 提示与后续引擎扩展
        for key in ("trim_tiers", "special_rules", "benchmark_gate", "outlier_policy", "_label_mismatch"):
            if (c.params or {}).get(key) is not None:
                ext[key] = c.params[key]
        s.add(
            PriceFormula(
                package_id=package_id,
                criterion_id=crit.id,
                # 2026-10-28：未识别则落空串（NOT NULL 约束下用 "" 代替默认值），
                # 让 validator 的 PRICE_FORMULA_UNRECOGNIZED 能看到——旧
                # `or "benchmark_ratio"` 会把未识别态改写回默认，阻断形同虚设。
                formula_type=c.formula_type or "",
                benchmark_method=c.benchmark_method or "",
                k=c.k,
                trim_ratio=c.trim_ratio,
                deduct_rate=c.deduct_rate,
                upper_bound=c.upper_bound,
                lower_bound=c.lower_bound,
                rounding=c.rounding if c.rounding is not None else 2,
                exclude_invalid=True,
                params_json=_jsonable(ext) if ext else None,
                source_node_id=None,  # 核验通过后回填
                source_quote=c.quote,
            )
        )
    return crit.id, citation_id


def run_validations(package_id: int) -> list[dict]:
    """跑 12 条校验器并把 findings 落库（confirm/freeze 前置）。"""
    from app.rules.validators import run_validators as run_checks
    from app.services.rule_service import load_criteria_for_snapshot

    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is None:
            raise NotFound("规则包不存在")
        criteria = load_criteria_for_snapshot(s, package_id)
    snapshot = {
        "package": {"project_id": pkg.project_id, "version": pkg.version,
                    "status": pkg.status, "total_max": 100},
        "criteria": criteria,
    }
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        source_doc = pkg.source_document_id if pkg else None

    def _node_exists(node_id: str) -> bool:
        # 法律规则的引用指向法条知识库而非项目文档（设计稿 §4.4）：
        # 真实核验"该法规该条是否在知识库里"，查无即 CITATION_UNREACHABLE。
        from app.rules.law_kb import LAW_PREFIX, node_exists as law_node_exists

        if node_id.startswith(LAW_PREFIX):
            return law_node_exists(node_id)
        if source_doc is None:
            return False
        with session_scope() as s:
            return s.execute(
                select(DocNode.id).where(DocNode.document_id == source_doc, DocNode.node_id == node_id)
            ).first() is not None

    findings = run_checks(snapshot, node_exists=_node_exists)
    # Finding.subject_id 是 criterion code（字符串）；落库列是 Integer，
    # 解析成数据库 id，前端才能把 issue 定位到具体规则。
    code_to_id = {c["code"]: c.get("id") for c in criteria}
    with session_scope() as s:
        from sqlalchemy import delete

        # 校验结果是"当前草稿"的派生数据：直接清空重写
        s.execute(delete(PackageValidation).where(PackageValidation.package_id == package_id))
        for f in findings:
            subject_id = None
            if f.subject_type in ("criterion", "formula") and f.subject_id:
                resolved = code_to_id.get(str(f.subject_id))
                subject_id = int(resolved) if isinstance(resolved, int) else None
            s.add(
                PackageValidation(
                    package_id=package_id,
                    code=f.code,
                    severity=f.severity,
                    subject_type=f.subject_type,
                    subject_id=subject_id,
                    message=f.message,
                    hint=f.hint,
                )
            )
    return [f.as_dict() for f in findings]


def _protected_scoring_span(text: str) -> tuple[int, int] | None:
    """评标细则章节在选段文本中的字符区间（分块时不切开）。

    取最后一个章节标题（评标细则/评分细则/评标办法/评分办法）到文本尾——
    评标办法通常在文档尾部，标题之前是目录等干扰项；报价段/证明材料都在
    该区间内，整段原子化即可保证细则表不被 chunk 边界切开。
    """
    title = None
    for m in re.finditer(r"评标细则|评分细则|评标办法|评分办法", text):
        title = m.start()
    if title is None:
        return None
    return (title, len(text))


def compile_rules(
    project_id: str,
    document_id: int,
    actor: str,
    provider: str | None = None,
    model: str | None = None,
    merge_target_id: int | None = None,
    *,
    selection_meta_out: dict | None = None,
    ctx: JobContext | None = None,
) -> int:
    """执行一次编译，返回新草稿规则包 id。

    merge_target_id 非空 = merge 模式（S1）：产物为临时编译包，指向模板包，
    供逐条覆盖匹配（设计稿 §5.1 阶段 A）。
    selection_meta_out 非空时被原地写入选段元数据（truncated/chars/tiers），供
    调用层（compile_rules_job）落 Job.result_json 审计——不改返回值契约。
    """
    from app.llm.registry import get_provider

    _text, doc = _load_tender_text(document_id)  # 校验 kind/parse_status 并取回 doc
    tender_pages = _load_tender_pages(document_id)
    text, sel = select_compile_text(tender_pages, budget=compile_char_budget())
    if selection_meta_out is not None:
        selection_meta_out.update(sel)
    if sel["truncated"]:
        log.warning(
            "招标文件超长（%d 字符），按章节优先级选取 %d 字符，入选页 %s，档位命中 %s",
            sel["total_chars"], sel["chars"], sel.get("pages"), sel.get("tiers"),
        )

    gateway = get_gateway() if provider is None and model is None else Gateway(get_provider(provider, model))

    # 分块抽取：长招标文件的规则集合可能远超单次输出上限（99 页文档实测 12000
    # tokens 仍被截断）。按字符预算切块分别抽取，再合并去重——每块输入小，
    # 输出空间充足，模型不会中途被截断。
    chunks = split_for_extraction(text, protected_spans=[s] if (s := _protected_scoring_span(text)) else None)
    criteria_acc: list[ExtractedCriterion] = []
    result_model: str | None = None
    for idx, chunk in enumerate(chunks, start=1):
        if ctx is not None and len(chunks) > 1:
            ctx.advance(message=f"规则抽取 {idx}/{len(chunks)}")
        extraction, result = _extract_chunk(
            gateway, chunk, data_class=doc.data_class,
            label=f"{idx}/{len(chunks)}" if len(chunks) > 1 else "",
        )
        criteria_acc.extend(extraction.criteria)
        result_model = result.model
    criteria_acc = dedupe_extracted(criteria_acc)
    # 确定性补全：先把【证明材料】块折叠进 count 项，再补公式/计数参数
    criteria_acc = fold_material_blocks(criteria_acc)
    # 类别确定性校正（落库前）：category=="price" 是价格公式落盘的门槛，
    # LLM 把商务/价格项标错类别会让对应链路整体失效（2026-10-21 实证）。
    from app.rules.category_map import apply_category_to_extracted

    apply_category_to_extracted(criteria_acc, document_id)
    criteria_acc = enrich_extracted(criteria_acc, text)

    # 评标细则骨架对账：确定性表格台账核对 LLM 抽取（补缺口行/校正分值）。
    # 骼架解析失败自动回退纯抽取（与旧行为一致），report 进 selection_meta 审计。
    from app.rules.reconcile import reconcile_with_skeleton

    criteria_acc, reconcile_report = reconcile_with_skeleton(
        criteria_acc, document_id=document_id, pages=tender_pages)
    if selection_meta_out is not None and not reconcile_report.get("skipped"):
        selection_meta_out["skeleton_reconcile"] = reconcile_report
        if reconcile_report.get("generated") or reconcile_report.get("price_generated"):
            log.info(
                "评标细则骨架对账：补齐 %d 行%s，分值校正 %d 处，合计 %s",
                len(reconcile_report.get("generated", [])),
                "（含表外报价段）" if reconcile_report.get("price_generated") else "",
                len(reconcile_report.get("score_fixed", [])),
                reconcile_report.get("final_sum"),
            )

    version = _next_version(project_id)
    with session_scope() as s:
        pkg = RulePackage(
            project_id=project_id,
            version=version,
            status="draft",
            source_document_id=document_id,
            merge_target_id=merge_target_id,
            compiled_by=actor,
            model=result_model,
            prompt_version=config.llm_prompt_version(),
            created_by=actor,
        )
        s.add(pkg)
        s.flush()
        package_id = pkg.id

        seq = 0
        used_codes: set[str] = set()
        pending: list[tuple[int, int | None, ExtractedCriterion]] = []
        for c in criteria_acc:
            seq += 1
            crit_id, cit_id = _persist_criterion(s, package_id, document_id, c, seq, used_codes)
            pending.append((crit_id, cit_id, c))

    # 事务已提交：核验引用（独立会话）→ 回填页码/条款节点 → 重新分诊
    _verify_and_backfill(document_id, pending)
    # 节点已确定：按节点标题链再校正一次类别（权威页码，留痕），
    # 并把表格里被模型漏掉的子项/档位续行补进 description
    from app.rules.category_map import apply_category_from_nodes

    apply_category_from_nodes(package_id)
    enrich_descriptions_from_nodes(package_id)

    run_validations(package_id)
    return package_id


def _verify_and_backfill(
    document_id: int, pending: list[tuple[int, int | None, ExtractedCriterion]]
) -> None:
    """核验每条引用并把页码、条款节点回填到评分项，按核验结果重新分诊。"""
    from app.db.models import Criterion

    for crit_id, cit_id, c in pending:
        if cit_id is None:
            continue
        cit = verify_citation(cit_id)
        if cit is None:
            continue
        page_no = cit.page_no
        node_id = None
        if cit.verify_method is not None and page_no is not None:
            node_id = find_node_for_quote(document_id, page_no, c.quote or "")
        status = _triage(c, bool(cit.verified), node_id is not None)
        with session_scope() as s:
            crit = s.get(Criterion, crit_id)
            if crit is None:
                continue
            crit.source_page = page_no
            crit.source_node_id = node_id
            crit.status = status
            s.add(crit)


def _has_scoring_detail(text: str) -> bool:
    """描述里是否已经有可执行的子项/档位明细。"""
    return bool(
        re.search(r"[（(]\s*0\s*[-~至]\s*\d", text or "")
        or re.search(r"得\s*\d+(?:\.\d+)?\s*[-~至]\s*\d+(?:\.\d+)?\s*分", text or "")
    )


def _normalized_span(haystack: str, needle: str) -> tuple[int, int] | None:
    """忽略空白/排版归一化定位 needle，返回其在 haystack 原文中的 [start, end)。"""
    from app.parsing.normalize import normalize_no_ws_with_map

    needle_n = normalize_no_ws(needle)
    if not needle_n:
        return None
    norm, idx = normalize_no_ws_with_map(haystack)
    pos = norm.find(needle_n)
    if pos < 0:
        return None
    return idx[pos], idx[pos + len(needle_n) - 1] + 1


def _quote_span(haystack: str, quote: str) -> tuple[int, int, bool] | None:
    """定位 quote；表格列文字插入把原句打断时退回前缀匹配。

    返回 (start, end, exact)。exact=False 表示只匹配到前缀，end 不是 quote 的真实结尾。
    """
    full = _normalized_span(haystack, quote)
    if full is not None:
        return full[0], full[1], True
    q = (quote or "").strip()
    # 真实 PDF 表格会把"3 运维方案 0-8 主观分"插进句子中间，整句无法连续匹配；
    # 逐步缩短前缀，取第一个能定位到开头的长度。
    for size in (60, 40, 30, 20, 12):
        if len(q) <= size:
            continue
        part = _normalized_span(haystack, q[:size])
        if part is not None:
            return part[0], part[1], False
    return None


def enrich_descriptions_from_nodes(package_id: int) -> int:
    """把 source node 里紧跟 quote 的评分细则续行补进主观项 description。

    真实招标文件的表格在 PDF 里被拆成多行：模型只抄了首句，子项分值
    （"协同办公（0-2 分）…"）或档位文字（"得 3-4 分…"）留在同一节点后半段。
    不补进描述，主观评分就只能按名字整体印象打分（评分趋同的直接原因）。
    """
    from app.db.models import Criterion

    changed = 0
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is None or pkg.source_document_id is None:
            return 0
        doc_id = pkg.source_document_id
        crits = s.execute(
            select(Criterion).where(
                Criterion.package_id == package_id,
                Criterion.status != "retired",
                Criterion.score_type == "subjective",
            )
        ).scalars().all()
        groups: dict[str, list[Criterion]] = {}
        for c in crits:
            if c.source_node_id and (c.source_quote or "").strip():
                groups.setdefault(c.source_node_id, []).append(c)
        for node_id, group in groups.items():
            node = s.execute(
                select(DocNode).where(
                    DocNode.document_id == doc_id, DocNode.node_id == node_id
                )
            ).scalar_one_or_none()
            if node is None or not (node.text or "").strip():
                continue
            group.sort(key=lambda x: (x.sort_order or 0, x.id))
            spans = [
                (c, _quote_span(node.text, c.source_quote or "")) for c in group
            ]
            # 必须按 quote 在节点中的实际位置排序：合并包的 sort_order 来自模板
            # （T4.1/T4.2/T4.3 在前，c23… 追加项在后），与原文表格顺序不一致。
            spans = [x for x in spans if x[1] is not None]
            spans.sort(key=lambda x: x[1][0])  # type: ignore[index]
            for i, (crit, span) in enumerate(spans):
                if span is None:
                    continue
                desc = crit.description or ""
                # 已带可执行细则且长度正常 → 不重复追加。
                # 超过 800 字符的"细则"通常是上一版把整个节点都塞进了描述
                # （表格列打断导致 next_start 找不到），需要按 quote 重建。
                if _has_scoring_detail(desc) and len(desc) <= 800:
                    continue
                start, quote_end, exact = span
                next_start = (
                    spans[i + 1][1][0]  # type: ignore[index]
                    if i + 1 < len(spans)
                    else len(node.text)
                )
                segment = node.text[start:next_start].strip()
                if not segment or not _has_scoring_detail(segment):
                    continue
                # 整句能连续定位：去掉 quote 本身只留续行；
                # 表格列打断只能前缀定位：保留整格文本（含被打断的原句），
                # 否则会把 T4.2 的"运维服务方[3 运维方案 0-8 主观分]案"截断。
                tail = (
                    node.text[quote_end:next_start].strip()
                    if exact
                    else segment
                )
                if not tail:
                    continue
                base = (crit.source_quote or "").strip()
                new_desc = (
                    f"{base}\n{tail}".strip()
                    if base and tail not in base
                    else (desc + "\n" + tail).strip()
                )
                if new_desc == desc:
                    continue
                if tail in desc and len(desc) <= 800:
                    continue
                crit.description = new_desc[:4000]
                s.add(crit)
                changed += 1
    return changed


@register("compile_rules")
def compile_rules_job(ctx: JobContext, payload: dict) -> dict:
    project_id = payload["project_id"]
    document_id = int(payload["document_id"])
    actor = payload.get("actor", "system")
    provider = payload.get("provider")
    model = payload.get("model")
    merge_target_id = payload.get("merge_target_id")
    ctx.start(total=4, message="加载招标文件")
    _load_tender_text(document_id)
    ctx.advance(message="LLM 抽取评分规则")
    selection_meta: dict = {}
    package_id = compile_rules(
        project_id, document_id, actor, provider, model,
        merge_target_id=int(merge_target_id) if merge_target_id else None,
        selection_meta_out=selection_meta,
        ctx=ctx,
    )
    if selection_meta.get("truncated"):
        ctx.advance(message=(
            f"已按章节优先级选取 {selection_meta['chars']}/{selection_meta['total_chars']} 字符"
            f"（{len(selection_meta.get('pages') or [])} 页；档位命中 {selection_meta.get('tiers') or {}}）"
        ))
    else:
        ctx.advance(message="校验与分诊完成")

    result: dict = {"package_id": package_id}
    if selection_meta.get("truncated"):
        result["selection_meta"] = selection_meta
    # 校验摘要（2026-10-27 错配护栏）：编译结果即时带出 error/warn（Σ 满分核对、
    # 价格参数缺参等），前端编译完成即可见红字——不让"Σ≠100/无法执行"静默上线
    try:
        from app.services import rule_service as _rs

        findings = _rs.get_validations(package_id)
        result["validations"] = {
            "error_codes": sorted({v.code for v in findings if v.severity == "error"}),
            "warn_codes": sorted({v.code for v in findings if v.severity != "error"}),
        }
    except Exception:  # noqa: BLE001 —— 校验摘要失败不阻断编译结果
        pass
    if merge_target_id and config.auto_mode():
        # 全自动模式：合并配对自动采纳（覆盖/追加/弃用全留痕），不再进人工抽屉。
        # best-effort：合并处置失败不让编译失败，原因进 result_json 供前端提示。
        ctx.advance(message="自动合并：生成匹配提案并采纳")
        try:
            from app.services import merge_service

            target_id = merge_service.auto_resolve_merge(
                package_id, actor=actor, provider=provider, model=model)
            result.update(auto_resolved=True, target_package_id=target_id)
            # 合并后校验摘要：总分/价格公式可能被覆盖/追加破坏，
            # 自动路径只 merge、不 confirm/freeze，由前端提示人工修正。
            from app.services import rule_service

            findings = rule_service.get_validations(target_id)
            result["post_merge"] = {
                "error_codes": sorted({v.code for v in findings if v.severity == "error"}),
                "warn_codes": sorted({v.code for v in findings if v.severity != "error"}),
                "errors": [
                    {"code": v.code, "message": v.message}
                    for v in findings
                    if v.severity == "error"
                ],
            }
        except Exception as exc:  # noqa: BLE001 —— 人工兜底通道保留（匹配提案抽屉）
            log.warning("自动合并失败（可人工在匹配提案抽屉处置）：%s", exc)
            result.update(auto_resolved=False, auto_resolve_error=str(exc)[:300])

    ctx.advance(message="完成")
    return result
