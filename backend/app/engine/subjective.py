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
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.llm.base import LLMError
from app.llm.gateway import Gateway
from app.rules import prompts

log = logging.getLogger("bid_eval.subjective")

MAX_BID_CHARS = 20_000  # 送审预算；超长标书按评分项关键词选段（不再头部盲截）


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
) -> SubjectiveSuggestion:
    """对一家投标人的一个主观评分项给出建议分。

    material_pages：该投标人主文件中证明材料相关内容（发票/验收/截图等）的
    页码索引文本，供证明材料链核对参考；无则传 None。
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
            split_text_chunks(bid_text), criterion, MAX_BID_CHARS
        )
        truncated = "\n".join(t for _i, t in picked)
        warnings.append(
            f"投标文件超长（{meta['total_chars']} 字符），已按评分项"
            f"「{criterion.get('name')}」选取 {meta['chars']} 字符参与评审"
            f"（{meta.get('mode', 'keyword')}，保留原文次序）"
        )

    user = prompts.SUBJECTIVE_USER_TEMPLATE.format(
        name=criterion.get("name") or "",
        max_score=max_score,
        levels_text=levels_text(levels) or "（无档位信息，请按判据在 0-满分内给分）",
        required_evidence=(
            criterion.get("required_evidence")
            or criterion.get("source_quote")
            or "投标文件原文"
        ),
        material_pages=material_pages,
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
                          material_pages=material_pages)
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
                                     material_pages=material_pages),
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

    valid = [s for s in suggestions if s.status == "ai_suggested" and s.score is not None]
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
    return SubjectiveSuggestion(
        status="ai_suggested",
        score=final,
        level_label=label,
        reasoning="；".join(s.reasoning for s in middle if s.reasoning)[:1500] or None,
        confidence=sum(s.confidence or 0 for s in middle) / len(middle),
        evidence=evidence,
        warnings=warnings,
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
) -> tuple[SubjectiveSuggestion, list[dict]] | None:
    """分层评审：证据地图定位 K 个不相交证据簇，逐簇深评后聚合。

    返回 None = 证据地图不可用（嵌入服务故障），调用方回退单次评审。
    聚合规则（质量档位型）：取最高分簇为主评；跨簇分差 ≥2 时标"证据分散"疑问。
    评分语义为"内容在某一章成立即成立"，最高簇即代表该章证据。
    """
    from app.engine.selection import build_evidence_map, split_text_chunks

    k = k or config.review_clusters()
    chunks = split_text_chunks(bid_text)
    emap = build_evidence_map(chunks, criterion)
    if emap is None:
        return None
    clusters, _query = emap
    if not clusters:
        return None

    cluster_results: list[dict] = []
    suggestions: list[tuple[dict, SubjectiveSuggestion]] = []
    for cl in clusters[: max(1, k)]:
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
        )
        entry = {
            "judge_no": len(suggestions) + 1,
            "cluster_chars": cl["chars"],
            "score": str(s.score) if s.score is not None else None,
            "level_label": s.level_label,
            "status": s.status,
            "warnings": s.warnings,
            "dropped": False,
        }
        cluster_results.append(entry)
        if s.status == "ai_suggested" and s.score is not None:
            suggestions.append((cl, s))

    if not suggestions:
        return SubjectiveSuggestion(
            status="need_human",
            warnings=[f"分层评审：{len(clusters)} 个证据簇均未给出有效分", "llm_failed"],
        ), cluster_results

    # 主评 = 最高分簇
    best_cl, best = max(
        suggestions, key=lambda pair: pair[1].score  # type: ignore[arg-type,return-value]
    )
    other_scores = [str(s.score) for _cl, s in suggestions if s is not best]
    warnings = list(best.warnings)
    warnings.insert(0, (
        f"分层证据评审：全文档定位到 {min(len(clusters), k)} 个独立证据簇，"
        f"逐簇评分 {'、'.join(other_scores + [str(best.score)])}，取最高簇（{best_cl['chars']} 字符）为主评"
    ))

    merged = SubjectiveSuggestion(
        status="ai_suggested",
        score=best.score,
        level_label=best.level_label,
        reasoning=best.reasoning,
        confidence=best.confidence,
        evidence=best.evidence,
        artifacts_check=best.artifacts_check,
        sub_scores=best.sub_scores,
        warnings=warnings,
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
