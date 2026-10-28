"""主观分建议：LLM 逐项对照档位判据给"建议分"，敌意输入校验层兜底。

粒度 = 一家投标人 × 一个评分项 = 一次调用（合并多家会破坏评审独立性并互相锚定）。
输入只含：评分项 + 满分 + 档位判据原文 + 该投标人文本。**不含权重、不含其他投标人。**

校验层处置表（对应方案 6.4）：
- JSON 解析失败       → 降级 need_human（绝不默认 0 分——默认 0 会悄悄改变排名）
- score 越界          → 夹取 + 警告
- score 与档位标签不符 → 以代码按 score 反推档位（信数字不信标签）+ 警告
- 无证据 / insufficient → 降级 need_human，final_score 保持 NULL（无证据不评分）

降级边界：以上仅针对**模型故障**（LLMError）；配置类 AppError（未配置 provider/
密钥缺失/预算超限）不降级，直接让运行显式失败（fail-fast，见 decisions/0005）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.llm.base import LLMError
from app.llm.gateway import Gateway
from app.rules import prompts

log = logging.getLogger("bid_eval.subjective")

MAX_BID_CHARS = 20_000  # 送审预算；超长标书按评分项关键词选段（不再头部盲截）

# 原文子项分值："协同办公（0-2 分）、行政管理（0-2 分）…"
_RE_SUBITEM = re.compile(
    r"([^、，,；;：:\n（）()]{2,24}?)\s*[（(]\s*0\s*[-~至]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]"
)


def parse_subitems(description: str | None) -> list[tuple[str, Decimal]]:
    """从评分项描述中提取"名称（0-X 分）"子项，去重保序。"""
    out: list[tuple[str, Decimal]] = []
    seen: set[str] = set()
    for m in _RE_SUBITEM.finditer(description or ""):
        name = re.sub(r"\s+", "", m.group(1)).strip("。；;")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append((name, Decimal(m.group(2))))
    return out


def _norm_name(text: str) -> str:
    return re.sub(r"[\s（）()：:、，,。.]+", "", text or "")


def _match_subitem(name: str, keys: list[str]) -> str | None:
    n = _norm_name(name)
    if not n:
        return None
    best = None
    for k in keys:
        kn = _norm_name(k)
        if not kn:
            continue
        if kn in n or n in kn:
            if best is None or len(kn) > len(_norm_name(best)):
                best = k
    return best


def aggregate_subitems(
    criterion: dict,
    sub_scores: list[dict],
    model_score: Decimal | None,
    max_score: Decimal,
) -> tuple[Decimal | None, list[dict], list[str]]:
    """有原文子项时，用子项分之和覆盖模型自报总分（缺失子项按 0）。

    返回 (score, normalized_sub_scores, warnings)。没有任何子项能匹配时
    返回 model_score，避免因为名称不一致把整项归零。normalized 条目带 `quote`
    （供证据闸门逐子项核验）。
    """
    parsed = parse_subitems(criterion.get("description"))
    if not parsed or not sub_scores:
        return model_score, sub_scores, []
    keys = [str(s.get("name") or "") for s in sub_scores if isinstance(s, dict)]
    matched = 0
    total = Decimal(0)
    normalized: list[dict] = []
    missing: list[str] = []
    for name, cap in parsed:
        hit = _match_subitem(name, keys)
        if hit is None:
            missing.append(name)
            normalized.append({"name": name, "score": "0", "max": str(cap), "quote": ""})
            continue
        src = next(
            (s for s in sub_scores if isinstance(s, dict) and str(s.get("name") or "") == hit),
            None,
        )
        if src is None:
            missing.append(name)
            normalized.append({"name": name, "score": "0", "max": str(cap), "quote": ""})
            continue
        matched += 1
        raw = _parse_score(src.get("score"))
        value = min(max(raw or Decimal(0), Decimal(0)), cap)
        total += value
        normalized.append({
            "name": name,
            "score": str(value),
            "max": str(cap),
            "quote": str(src.get("quote") or "").strip(),
        })
    if matched == 0:
        return model_score, sub_scores, ["子项名称与原文无法对应，按模型自报总分计"]
    total = min(total, max_score)
    warnings = [f"按原文子项汇总 {total}/{max_score} 分"]
    if missing:
        warnings.append(f"未提供证据的子项按 0 计：{'、'.join(missing)}")
    return total, normalized, warnings


def apply_quote_gate(
    sub_scores: list[dict],
    verify_quote,  # noqa: ANN001
) -> tuple[Decimal | None, list[str], bool]:
    """子项证据闸门：引文能在投标文件中定位 → 分数保留；否则该子项记 0。

    返回 (闸门后总分, warnings, 是否执行)。模型整体没给任何引文时不执行
    （8B 偶发不给引文，直接全零会把整项崩掉），调用方应整项标疑问转人工。
    """
    if verify_quote is None:
        return None, [], False
    has_any_quote = any(
        isinstance(s, dict) and str(s.get("quote") or "").strip() for s in sub_scores
    )
    if not has_any_quote:
        return None, ["子项引文未提供，证据闸门未执行"], False
    warnings: list[str] = []
    total = Decimal(0)
    for item in sub_scores:
        if not isinstance(item, dict):
            continue
        score = _parse_score(item.get("score")) or Decimal(0)
        quote = str(item.get("quote") or "").strip()
        if quote and verify_quote(quote):
            total += score
            continue
        if score > 0:
            reason = "引文未在投标文件中定位到" if quote else "未提供引文"
            warnings.append(f"子项「{item.get('name')}」{reason}，按 0 计")
        item["score"] = "0"
    return total, warnings, True


class SubjectiveOutput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    level_label: str | None = None
    score: str | Decimal | float | int | None = None
    reasoning: str | None = None
    evidence: list[dict] = Field(default_factory=list)
    confidence: float | None = None
    insufficient_evidence: bool = False
    artifacts_check: list[dict] = Field(default_factory=list)  # 证明材料链核对
    sub_scores: list[dict] = Field(default_factory=list)  # 复合因素子项得分


@dataclass
class SubjectiveSuggestion:
    status: str  # ai_suggested | need_human
    score: Decimal | None = None
    level_label: str | None = None
    reasoning: str | None = None
    confidence: float | None = None
    evidence: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts_check: list[dict] = field(default_factory=list)  # 证明材料链核对
    sub_scores: list[dict] = field(default_factory=list)  # 复合因素子项得分
    # 子项证据闸门：True=已执行且通过/扣减；False=模型没给引文未执行（应标疑问）
    quote_gate_executed: bool | None = None
    # 聚合算式（可复算不变式 I1）：{rule, inputs:[{source,score,provisional}], arithmetic}
    # 持久化进 ai_raw_json["aggregation_v2"]，final_score 可由它重算核对
    aggregation: dict | None = None


def _parse_score(value) -> Decimal | None:  # noqa: ANN001
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def level_for_score(levels: list[dict], score: Decimal) -> str | None:
    """按分值反推档位（信数字不信标签）。"""
    for lv in levels:
        lo = Decimal(str(lv.get("min")))
        hi = Decimal(str(lv.get("max")))
        if lo <= score <= hi:
            return lv.get("label")
    return None


def levels_text(levels: list[dict]) -> str:
    lines = []
    for lv in levels:
        lines.append(
            f"{lv.get('label')} | {lv.get('min')} | {lv.get('max')} | {lv.get('criteria') or ''}"
        )
    return "\n".join(lines)


def suggest_score(
    gateway: Gateway,
    criterion: dict,
    bid_text: str,
    data_class: str = "synthetic",
    material_pages: str | None = None,
    verify_quote=None,  # noqa: ANN001  (str) -> bool；子项引文闸门
    *,
    chunk_cats: list[str | None] | None = None,
    chunk_cat_confs: list[float] | None = None,
) -> SubjectiveSuggestion:
    """对一家投标人的一个主观评分项给出建议分。

    material_pages：该投标人主文件中证明材料相关内容（发票/验收/截图等）的
    页码索引文本，供证明材料链核对参考；无则传 None。
    chunk_cats/chunk_cat_confs：与 split_text_chunks(bid_text) 等长的材料类别/
    置信度（scoring_service 侧按同口径切块算好传入）——选段软过滤用，None=无标签。
    """
    material_pages = material_pages or "（无材料页索引）"
    warnings: list[str] = []
    max_score = Decimal(str(criterion.get("max_score") or 0))
    levels = criterion.get("levels") or []

    truncated = bid_text
    if len(truncated) > MAX_BID_CHARS:
        # 章节感知选段：vector 模式按评分项语义检索（bge-m3），keyword 模式按
        # 名称/证据要求派生关键词——替代头部盲截（200 页标书只看前 4% 曾把
        # 尾部"售后服务方案"系统性评成最低档）。
        from app.engine.selection import (
            select_chunks_for_criterion,
            split_text_chunks,
        )

        picked, meta = select_chunks_for_criterion(
            split_text_chunks(bid_text), criterion, MAX_BID_CHARS,
            chunk_cats=chunk_cats, chunk_cat_confs=chunk_cat_confs,
        )
        truncated = "\n".join(t for _i, t in picked)
        _sf = "；类内优先" if meta.get("in_class") else ""
        _fb = "；类内不足已回退全库" if meta.get("soft_fallback") else ""
        warnings.append(
            f"投标文件超长（{meta['total_chars']} 字符），已按评分项"
            f"「{criterion.get('name')}」选取 {meta['chars']} 字符参与评审"
            f"（{meta.get('mode', 'keyword')}{_sf}{_fb}，保留原文次序）"
        )

    # 项目级校准参数（data/calibration/<name>.json，可缺省）：尺度提示注入
    # prompt，version 落 ai_raw_json 供审计还原（2026-10-27 训练集校准）
    from app import config as _config
    _profile = _config.calibration_profile()
    scale_block = (
        f"<scale_profile>\n{_profile['scale_text']}\n</scale_profile>\n"
        if _profile.get("scale_text") else ""
    )
    user = prompts.SUBJECTIVE_USER_TEMPLATE.format(
        name=criterion.get("name") or "",
        max_score=max_score,
        description=(
            criterion.get("description")
            or criterion.get("source_quote")
            or "（原文未给出评分细则）"
        ),
        levels_text=levels_text(levels) or "（无档位信息，请按判据在 0-满分内给分）",
        required_evidence=(
            criterion.get("required_evidence")
            or criterion.get("source_quote")
            or "投标文件原文"
        ),
        material_pages=material_pages,
        scale_profile_block=scale_block,
        bid_text=truncated,
    )
    try:
        out, _result = gateway.chat_json(
            prompts.SUBJECTIVE_SYSTEM_PROMPT,
            user,
            SubjectiveOutput,
            data_class=data_class,
        )
    except LLMError as exc:
        # 余额/配额类失败必须让运行显式失败，不得降级 need_human——
        # 否则余额耗尽后所有评分静默变 0 分（2026-10-14 硅基流动 402 实证：
        # 后两家 40 次调用全灭，技术分整体归零的假结果差点当成结论）。
        # 与 0005 的 fail-fast 原则一致：配置/账户类错误不是模型判断问题。
        msg = str(exc)
        if any(k in msg for k in ("402", "insufficient", "quota", "30001", "arrears")):
            raise
        return SubjectiveSuggestion(
            status="need_human",
            warnings=[f"模型调用失败，降级人工：{exc}", "llm_failed"],
        )

    # ---- 敌意输入校验层 ----
    score = _parse_score(out.score)
    if score is None:
        # 截断/解析失败重试一次（紧凑输出指令），仍失败标 parse_failed——
        # auto 模式下 parse 类失败按疑问处理，不得兜底 0 分进总分
        # （2026-10-15 诚道"运维方案 0 分"实证：JSON 被截断静默兜底）。
        try:
            out, _result = gateway.chat_json(
                prompts.SUBJECTIVE_SYSTEM_PROMPT,
                user + "\n（注意：只输出 JSON，紧凑书写不要换行，reasoning 限 200 字）",
                SubjectiveOutput,
                data_class=data_class,
            )
            score = _parse_score(out.score)
        except LLMError:
            score = None
    if score is None:
        extra = ["parse_failed"]
        if out.insufficient_evidence or not out.evidence:
            return SubjectiveSuggestion(
                status="need_human",
                reasoning=out.reasoning,
                evidence=out.evidence,
                artifacts_check=out.artifacts_check,
                sub_scores=out.sub_scores,
                warnings=warnings + ["模型未给出有效分值，降级人工"] + extra,
            )
        return SubjectiveSuggestion(
            status="need_human",
            reasoning=out.reasoning,
            evidence=out.evidence,
            artifacts_check=out.artifacts_check,
            sub_scores=out.sub_scores,
            warnings=warnings + ["模型给出的分值无法解析，降级人工（不默认 0 分）"] + extra,
        )

    if score < Decimal(0):
        score = Decimal(0)
        warnings.append("分值为负，已夹取至 0")
    if score > max_score:
        score = max_score
        warnings.append(f"分值超满分，已夹取至 {max_score}")

    # 原文有"名称（0-X 分）"子项时，以子项之和为准（缺失子项按 0）。
    # 模型容易只给整体印象分；逐子项汇总能把"漏掉某一章"体现为分数差异。
    score, out.sub_scores, sub_warnings = aggregate_subitems(
        criterion, out.sub_scores, score, max_score
    )
    warnings.extend(sub_warnings)
    if score is None:
        return SubjectiveSuggestion(
            status="need_human",
            reasoning=out.reasoning,
            evidence=out.evidence,
            artifacts_check=out.artifacts_check,
            sub_scores=out.sub_scores,
            warnings=warnings + ["无法确定子项得分，降级人工"],
        )

    # 子项证据闸门（2026-10-21）：模型给的子项分必须能在投标文件里找到逐字依据，
    # 定位不到按 0 计——遏制"整体印象给满分子项"。模型整体没给引文时闸门不执行
    #（quote_gate_executed=False），由调用方标疑问转人工。
    gate_executed: bool | None = None
    if verify_quote is not None and out.sub_scores:
        gated_total, gate_warnings, gate_executed = apply_quote_gate(
            out.sub_scores, verify_quote
        )
        warnings.extend(gate_warnings)
        if gate_executed:
            score = min(gated_total or Decimal(0), max_score)

    # 档位：信数字不信标签
    resolved = level_for_score(levels, score)
    label = out.level_label
    if levels and resolved is not None and label != resolved:
        warnings.append(
            f"档位标签（{label}）与分值不符，已按分值归入「{resolved}」档"
        )
        label = resolved

    if out.insufficient_evidence or not out.evidence:
        return SubjectiveSuggestion(
            status="need_human",
            score=score,
            level_label=label,
            reasoning=out.reasoning,
            confidence=out.confidence,
            evidence=out.evidence,
            artifacts_check=out.artifacts_check,
            sub_scores=out.sub_scores,
            warnings=warnings + ["模型未提供原文证据，无证据不评分"],
            quote_gate_executed=gate_executed,
        )

    return SubjectiveSuggestion(
        status="ai_suggested",
        score=score,
        level_label=label,
        reasoning=out.reasoning,
        confidence=out.confidence,
        evidence=out.evidence,
        artifacts_check=out.artifacts_check,
        sub_scores=out.sub_scores,
        warnings=warnings,
        quote_gate_executed=gate_executed,
    )


# ---------------------------------------------------------------------------
# 评审委员会：多 Agent 独立评分 + 去尾平均（对齐真实评委会规则）
# ---------------------------------------------------------------------------
def _trimmed_mean(values: list[Decimal]) -> Decimal:
    """去 1 个最高 + 1 个最低（并列各弃一），中间取平均，ROUND_HALF_UP 2 位。

    调用方保证 len(values) >= 3：少于 3 无法去尾。
    """
    ordered = sorted(values)
    middle = ordered[1:-1]
    total = sum(middle, Decimal(0))
    return (total / Decimal(len(middle))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def suggest_score_committee(
    gateway: Gateway,
    criterion: dict,
    bid_text: str,
    data_class: str = "synthetic",
    size: int | None = None,
    material_pages: str | None = None,
    verify_quote=None,  # noqa: ANN001  (str) -> bool；子项引文闸门
    *,
    chunk_cats: list[str | None] | None = None,
    chunk_cat_confs: list[float] | None = None,
) -> tuple[SubjectiveSuggestion, list[dict]]:
    """size 个 AI 评委独立评分，去尾平均聚合。返回 (聚合建议, 评委明细)。

    独立性：每位评委一次独立 LLM 调用（同提示词、互不可见），并发执行。
    聚合：有效分（ai_suggested 且分值可解析）>= 3 才聚合；不足 → 整项 need_human
    （宁严勿纵——拿 2 个分去尾等于没去尾，把偶然当结论）。
    评委明细逐人带 dropped 标记（哪两个分被弃），报告与审计直接可用。

    size <= 1：单评委模式（2026-10-14 用户指令，默认）——直接采纳唯一评委的
    建议分，不去尾、无聚合门槛，一次 LLM 调用。
    """
    from concurrent.futures import ThreadPoolExecutor

    size = size if size is not None else config.committee_size()
    if size <= 1:
        s = suggest_score(gateway, criterion, bid_text, data_class=data_class,
                          material_pages=material_pages, verify_quote=verify_quote,
                          chunk_cats=chunk_cats, chunk_cat_confs=chunk_cat_confs)
        committee = [{
            "judge_no": 1,
            "score": str(s.score) if s.score is not None else None,
            "level_label": s.level_label,
            "status": s.status,
            "insufficient": s.status != "ai_suggested",
            "warnings": s.warnings,
            "dropped": False,
        }]
        if s.status != "ai_suggested":
            s.warnings = list(s.warnings) + ["单评委未给出有效分，转人工"]
        return s, committee

    with ThreadPoolExecutor(max_workers=size) as pool:
        suggestions = list(pool.map(
            lambda _i: suggest_score(gateway, criterion, bid_text, data_class=data_class,
                                     material_pages=material_pages,
                                     verify_quote=verify_quote,
                                     chunk_cats=chunk_cats,
                                     chunk_cat_confs=chunk_cat_confs),
            range(size),
        ))

    committee: list[dict] = []
    for i, s in enumerate(suggestions, start=1):
        committee.append({
            "judge_no": i,
            "score": str(s.score) if s.score is not None else None,
            "level_label": s.level_label,
            "status": s.status,
            "insufficient": bool(s.insufficient_evidence if hasattr(s, "insufficient_evidence") else False)
            or any("未提供原文证据" in w or "未给出有效分值" in w or "无法解析" in w for w in s.warnings),
            "warnings": s.warnings,
        })

    # provisional（给了数字但证据不足）也参与聚合（清零机制修复，同 hierarchical）
    valid = [s for s in suggestions if s.score is not None]
    for row, s in zip(committee, suggestions):
        row["provisional"] = s.status != "ai_suggested" and s.score is not None
    warnings: list[str] = []
    if len(valid) < 3:
        return SubjectiveSuggestion(
            status="need_human",
            reasoning="；".join(s.reasoning for s in suggestions if s.reasoning)[:800] or None,
            evidence=next((s.evidence for s in suggestions if s.evidence), []),
            warnings=[f"仅 {len(valid)}/{size} 位 AI 评委给出有效分，不足 3 个，无法去尾平均，转人工"],
        ), committee

    ordered = sorted(valid, key=lambda s: s.score)
    dropped_min, dropped_max = ordered[0], ordered[-1]
    dropped_ids = {id(dropped_min), id(dropped_max)}
    for row, s in zip(committee, suggestions):
        row["dropped"] = id(s) in dropped_ids

    final = _trimmed_mean([s.score for s in valid])
    if len(valid) < size:
        warnings.append(f"{size} 位 AI 评委中 {size - len(valid)} 位未给出有效分，按其余评委去尾平均")
    warnings.append(
        f"{size} 位 AI 评委独立评分，去最高（{dropped_max.score}）最低（{dropped_min.score}）"
        f"后取平均 = {final}"
    )

    # 依据与理由取中间评委（未弃分者）的首位——证据只持久化未弃分评委的，见 scoring_service
    middle = [s for s in valid if id(s) not in dropped_ids]
    lead = middle[0]
    resolved = level_for_score(criterion.get("levels") or [], final) if criterion.get("levels") else None
    label = lead.level_label
    if resolved and label != resolved:
        warnings.append(f"档位标签（{label}）与去尾分值不符，按分值归入「{resolved}」档")
        label = resolved

    evidence: list[dict] = []
    for s in middle:
        if s.evidence:
            evidence.append(s.evidence[0])
    gates = [s.quote_gate_executed for s in suggestions if s.quote_gate_executed is not None]
    # 聚合算式落档（不变式 I1）：trim 后中间均值可由评委分重算
    _vals_sorted = sorted(str(s.score) for s in valid)
    aggregation_v2 = {
        "rule": "trimmed_mean_drop_high_low",
        "inputs": [
            {"source": f"judge{row['judge_no']}", "score": row["score"],
             "provisional": bool(row.get("provisional")), "dropped": bool(row.get("dropped"))}
            for row in committee
        ],
        "arithmetic": (
            "排序[" + ",".join(_vals_sorted) + "]去"
            f"高{dropped_max.score}低{dropped_min.score}→均值 {final}"
        ),
    }
    return SubjectiveSuggestion(
        status="ai_suggested",
        score=final,
        level_label=label,
        reasoning="；".join(s.reasoning for s in middle if s.reasoning)[:1500] or None,
        confidence=sum(s.confidence or 0 for s in middle) / len(middle),
        evidence=evidence,
        warnings=warnings,
        quote_gate_executed=(all(gates) if gates else None),
        aggregation=aggregation_v2,
    ), committee


# ---------------------------------------------------------------------------
# 分层证据评审：证据地图 → 前 K 簇分别深评 → 聚合（解决几百页标书覆盖不足）
# ---------------------------------------------------------------------------
def suggest_score_hierarchical(
    gateway: Gateway,
    criterion: dict,
    bid_text: str,
    data_class: str = "synthetic",
    k: int | None = None,
    material_pages: str | None = None,
    verify_quote=None,  # noqa: ANN001  (str) -> bool；子项引文闸门（对全文核验）
    *,
    chunk_cats: list[str | None] | None = None,
    chunk_cat_confs: list[float] | None = None,
) -> tuple[SubjectiveSuggestion, list[dict]] | None:
    """分层评审：证据地图定位 K 个不相交证据簇，逐簇深评后聚合。

    返回 None = 证据地图不可用（嵌入服务故障），调用方回退单次评审。
    聚合规则：有原文子项时按"子项跨簇平均"；无子项时按簇相关性加权平均；
    跨簇分差 ≥2 时标"证据分散"疑问。
    chunk_cats：证据簇种子打分的类内加成（软过滤口径，不硬排除）。
    """
    from app.engine.selection import build_evidence_map, split_text_chunks

    k = k or config.review_clusters()
    max_score = Decimal(str(criterion.get("max_score") or 0))
    chunks = split_text_chunks(bid_text)
    emap = build_evidence_map(chunks, criterion, chunk_cats=chunk_cats)
    if emap is None:
        return None
    clusters, _query = emap
    if not clusters:
        return None

    cluster_results: list[dict] = []
    suggestions: list[tuple[dict, SubjectiveSuggestion]] = []
    sent_by_cluster: dict[int, int] = {}  # id(cluster dict) → 实际送审字符数
    evaluated = 0

    def _score_cluster(cl):  # noqa: ANN001
        # 单簇截取 ≤18k 字符（按块边界累加）——保证单次调用上下文可控
        parts: list[str] = []
        size = 0
        for i in cl["ids"]:
            body = chunks[i][1]
            if parts and size + len(body) > 18_000:
                break
            parts.append(body)
            size += len(body)
        cluster_text = "\n".join(parts)
        s = suggest_score(
            gateway, criterion, cluster_text, data_class=data_class,
            material_pages=material_pages,
            # 闸门对**全文**核验（子项证据可能落在别的簇里）
            verify_quote=verify_quote,
        )
        return cl, cluster_text, s

    # 簇间并行（2026-10-27 提速）：单簇 30-37s ×3 簇串行是主观 Phase1 的主力；
    # pool.map 保序 → 结果与串行逐字节一致（不变式 I3），总并发仍受网关信号量封顶
    cluster_items = clusters[: max(1, k)]
    _workers = min(len(cluster_items), max(1, config.llm_max_concurrency()))
    if _workers <= 1:
        scored = [_score_cluster(cl) for cl in cluster_items]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=_workers) as pool:
            scored = list(pool.map(_score_cluster, cluster_items))

    for cl, cluster_text, s in scored:
        evaluated += 1
        sent_by_cluster[id(cl)] = len(cluster_text)
        # provisional（证据不足但给了数字）也参与聚合——清零机制修复：
        # 0.5×3 被「均未给出有效分」清零是测试1 最大差距单因（+8.22），
        # 人工评委从不因"证据格式不合机器口径"给 0（2026-10-27）
        provisional = s.status != "ai_suggested" and s.score is not None
        entry = {
            "judge_no": len(suggestions) + 1,
            "cluster_chars": cl["chars"],
            "score": str(s.score) if s.score is not None else None,
            "level_label": s.level_label,
            "status": s.status,
            "warnings": s.warnings,
            "dropped": False,
            "provisional": provisional,
        }
        cluster_results.append(entry)
        if s.score is not None:
            suggestions.append((cl, s))

    if not suggestions:
        return SubjectiveSuggestion(
            status="need_human",
            warnings=[f"分层评审：{evaluated} 个证据簇均未给出有效分", "llm_failed"],
        ), cluster_results

    # 聚合：有原文子项时按"子项跨簇平均"（每个子项取给出该子项得分的簇的平均值，
    # 全部簇都没提到的子项按 0），否则按簇相关性加权平均。
    # 旧实现①取最高分簇、②子项同名取最高——任一章节看起来不错就按该值计分，
    # 系统性把分数推向满分并抹平差异（2026-10-21 实证：T4.2 四家全 8/8）。
    scores = [s.score for _cl, s in suggestions if s.score is not None]
    parsed = parse_subitems(criterion.get("description"))
    merged_subs: list[dict] = []
    merged_score: Decimal | None = None
    parse_keys = [name for name, _cap in parsed]
    sub_map: dict[str, Decimal] = {}
    sub_quotes: dict[str, str] = {}
    if parsed and any(s.sub_scores for _cl, s in suggestions):
        vals: dict[str, list[Decimal]] = {}
        for _cl, s in suggestions:
            for ss in s.sub_scores or []:
                if not isinstance(ss, dict):
                    continue
                hit = _match_subitem(str(ss.get("name") or ""), parse_keys)
                if hit is None:
                    continue
                cap = next(cap for name, cap in parsed if name == hit)
                val = _parse_score(ss.get("score")) or Decimal(0)
                vals.setdefault(hit, []).append(min(max(val, Decimal(0)), cap))
                q = str(ss.get("quote") or "").strip()
                if q and hit not in sub_quotes:
                    sub_quotes[hit] = q
        for name, _cap in parsed:
            got = vals.get(name)
            if got:
                # 跨簇平均：两个簇都看到了同一子项内容时，取它们的均值
                # （ROUND_HALF_UP 2 位），而不是像旧实现那样取最高
                sub_map[name] = (sum(got, Decimal(0)) / Decimal(len(got))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
    if sub_map:
        merged_score = min(sum(sub_map.values(), Decimal(0)), max_score)
        merged_subs = [
            {
                "name": name,
                "score": str(sub_map.get(name, Decimal(0))),
                "max": str(cap),
                "quote": sub_quotes.get(name, ""),
            }
            for name, cap in parsed
        ]
        # 证据/理由取"贡献子项最多"的簇
        best_cl, best = max(
            suggestions,
            key=lambda pair: sum(
                1 for ss in (pair[1].sub_scores or [])
                if isinstance(ss, dict)
                and _match_subitem(str(ss.get("name") or ""), parse_keys) is not None
            ),
        )
        agg_note = f"按子项跨簇平均汇总 {merged_score}/{max_score} 分"
        agg_rule = "subitem_cluster_mean"
        agg_arith = (
            "；".join(f"{n}={sub_map[n]}" for n, _c in parsed) + f" 合计 {merged_score}"
        )
    else:
        # **等权算术平均**（2026-10-27 可复算化）：旧「相关性加权」的权重（证据地图
        # 相似度）不落档、A3/A5 无法复算——审计缺陷实证。等权后 final_score 可由
        # 各簇分直接重算（不变式 I1）。
        merged_score = (
            sum((s.score for _cl, s in suggestions), Decimal(0))
            / Decimal(len(suggestions))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        merged_score = min(merged_score, max_score)
        best_cl, best = max(suggestions, key=lambda pair: float(pair[0].get("score") or 0.0))
        agg_note = f"按证据簇等权平均 {merged_score} 分"
        agg_rule = "cluster_equal_mean"
        agg_arith = (
            "(" + "+".join(str(s.score) for _cl, s in suggestions)
            + f")/{len(suggestions)}={merged_score}"
        )
    aggregation_v2 = {
        "rule": agg_rule,
        "inputs": [
            {"source": f"cluster{entry['judge_no']}", "score": entry["score"],
             "provisional": bool(entry.get("provisional"))}
            for entry in cluster_results
            if entry.get("score") is not None
        ],
        "arithmetic": agg_arith,
    }

    warnings = list(best.warnings)
    warnings.insert(0, (
        f"分层证据评审：全文档定位到 {min(len(clusters), k)} 个独立证据簇，"
        f"逐簇评分 {'、'.join(str(x) for x in scores)}，{agg_note}"
        f"（主依据 实际送审 {sent_by_cluster.get(id(best_cl), best_cl['chars'])}"
        f"/簇共 {best_cl['chars']} 字符）"
    ))
    if scores and max(scores) - min(scores) >= 2:
        warnings.append(
            f"证据分散：各证据簇评分差异 {min(scores)}–{max(scores)}，建议人工复核"
        )

    merged = SubjectiveSuggestion(
        status="ai_suggested",
        score=merged_score,
        level_label=level_for_score(criterion.get("levels") or [], merged_score)
        if criterion.get("levels")
        else best.level_label,
        reasoning=best.reasoning,
        confidence=best.confidence,
        evidence=best.evidence,
        artifacts_check=best.artifacts_check,
        sub_scores=merged_subs or best.sub_scores,
        warnings=warnings,
        quote_gate_executed=None,  # 分层路径的闸门在各簇 suggest_score 内执行
        aggregation=aggregation_v2,
    )
    committee = [
        {
            "judge_no": entry["judge_no"],
            "score": entry["score"],
            "level_label": entry["level_label"],
            "status": entry["status"],
            "cluster_chars": entry.get("cluster_chars"),
            "dropped": False,
        }
        for entry in cluster_results
    ]
    return merged, committee
