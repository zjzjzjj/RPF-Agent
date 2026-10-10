"""主观分建议：LLM 逐项对照档位判据给"建议分"，敌意输入校验层兜底。

粒度 = 一家投标人 × 一个评分项 = 一次调用（合并多家会破坏评审独立性并互相锚定）。
输入只含：评分项 + 满分 + 档位判据原文 + 该投标人文本。**不含权重、不含其他投标人。**

校验层处置表（对应方案 6.4）：
- JSON 解析失败       → 降级 need_human（绝不默认 0 分——默认 0 会悄悄改变排名）
- score 越界          → 夹取 + 警告
- score 与档位标签不符 → 以代码按 score 反推档位（信数字不信标签）+ 警告
- 无证据 / insufficient → 降级 need_human，final_score 保持 NULL（无证据不评分）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.llm.base import LLMError
from app.llm.gateway import Gateway
from app.rules import prompts

log = logging.getLogger("bid_eval.subjective")

MAX_BID_CHARS = 20_000  # MVP 截断；真实长标书按章节检索是后续优化


class SubjectiveOutput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    level_label: str | None = None
    score: str | Decimal | float | int | None = None
    reasoning: str | None = None
    evidence: list[dict] = Field(default_factory=list)
    confidence: float | None = None
    insufficient_evidence: bool = False


@dataclass
class SubjectiveSuggestion:
    status: str  # ai_suggested | need_human
    score: Decimal | None = None
    level_label: str | None = None
    reasoning: str | None = None
    confidence: float | None = None
    evidence: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
) -> SubjectiveSuggestion:
    """对一家投标人的一个主观评分项给出建议分。"""
    warnings: list[str] = []
    max_score = Decimal(str(criterion.get("max_score") or 0))
    levels = criterion.get("levels") or []

    truncated = bid_text
    if len(truncated) > MAX_BID_CHARS:
        truncated = truncated[:MAX_BID_CHARS]
        warnings.append(f"投标文件超长，截断至前 {MAX_BID_CHARS} 字符参与评审")

    user = prompts.SUBJECTIVE_USER_TEMPLATE.format(
        name=criterion.get("name") or "",
        max_score=max_score,
        levels_text=levels_text(levels) or "（无档位信息，请按判据在 0-满分内给分）",
        required_evidence=criterion.get("required_evidence") or "投标文件原文",
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
        return SubjectiveSuggestion(
            status="need_human", warnings=[f"模型调用失败，降级人工：{exc}"]
        )

    # ---- 敌意输入校验层 ----
    score = _parse_score(out.score)
    if score is None:
        if out.insufficient_evidence or not out.evidence:
            return SubjectiveSuggestion(
                status="need_human",
                reasoning=out.reasoning,
                evidence=out.evidence,
                warnings=warnings + ["模型未给出有效分值，降级人工"],
            )
        return SubjectiveSuggestion(
            status="need_human",
            reasoning=out.reasoning,
            evidence=out.evidence,
            warnings=warnings + ["模型给出的分值无法解析，降级人工（不默认 0 分）"],
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
            warnings=warnings + ["模型未提供原文证据，无证据不评分"],
        )

    return SubjectiveSuggestion(
        status="ai_suggested",
        score=score,
        level_label=label,
        reasoning=out.reasoning,
        confidence=out.confidence,
        evidence=out.evidence,
        warnings=warnings,
    )
