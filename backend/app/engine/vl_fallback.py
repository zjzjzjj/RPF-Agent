# -*- coding: utf-8 -*-
"""材料审核视觉大模型兜底（2026-11-23 优化②）。

仅对文本链路失败格触发（暂定/no_facts/存在性未核验）：把材料所在页按
"一份材料文档一张长图"竖拼（evidence.renderer.render_long_image），过云端
视觉大模型（BID_EVAL_VISION_MODEL 三件套，走 Gateway 涉密闸门），按冻结细则
处理：

- **计分格**（count/subjective）：按细则定分（0~满分），落 basis=visual_fallback
  的正常结论，标注"视觉AI核验"；分数越界/给不出分 → 视为 VL 失败，维持暂定。
- **否决/资格/符合性格**：**只给线索，绝不产否决结论**（误否决不可接受）——
  VL 返回 fail 一律拒收（契约层+代码层双保险），clues/观察记入 warnings
  （verdict_source="visual"），格维持原暂定/存疑语义。

证据口径=页级视觉核验：observations 落 ai_raw_json.visual_evidence
（页码+观察描述+图哈希），**不产逐字引文、不伪装文本证据**。

预算：每格 ≤2 次、每家 ≤10 次（Budget 线程安全，worker 闭包共享）；
超预算 / 涉密未放行 / 契约校验失败 → 维持原暂定（天然安全态）。
本通道**不调用 OCR**、不受 BID_EVAL_SCORING_OCR 约束（速度红线靠预算，
不复活逐页 OCR 循环）。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from app import config

log = logging.getLogger("bid_eval.engine.vl_fallback")

# 文本链路失败格的 fallback kind（VL 兜底触发集）
TRIGGER_KINDS = frozenset({
    "provisional_zero",
    "min_level_floor",
    "reading_incomplete",       # 第一优先：reason 本就写"须补读原页图像并视觉核验"
    "value_extraction_unresolved",
    "presence_unverified",
    "sample_volume_unverified",
})

VERDICT_SCORE_TYPES = ("veto", "match")
VERDICT_CATEGORIES = ("qualification", "compliance")


def is_trigger_kind(kind: str | None) -> bool:
    return kind in TRIGGER_KINDS


def is_verdict_criterion(criterion: dict) -> bool:
    """否决/资格/符合性格——VL 只给线索，不产否决结论。"""
    return (criterion.get("score_type") in VERDICT_SCORE_TYPES
            or criterion.get("category") in VERDICT_CATEGORIES)


class VlObservation(BaseModel):
    model_config = {"str_strip_whitespace": True}

    document_id: int | None = None
    page_no: int | None = None
    observation: str = Field(min_length=1, max_length=2000)


class VlVerdict(BaseModel):
    """VL 输出契约（pydantic 校验，任何不合法=VL 失败→维持暂定）。"""
    model_config = {"str_strip_whitespace": True}

    verdict: str  # pass|fail|unverifiable
    score: Decimal | None = None
    level: str | None = None
    observations: list[VlObservation] = Field(default_factory=list)
    reasoning: str = ""
    clues: list[str] = Field(default_factory=list)


@dataclass
class VlResult:
    kind: str                      # scored | clue_only | failed
    score: Decimal | None = None
    observations: list[dict] = field(default_factory=list)
    reasoning: str = ""
    clues: list[str] = field(default_factory=list)
    reason: str = ""               # failed 时的原因（供 warnings 留痕）


class Budget:
    """每格/每家 VL 调用预算（线程安全；worker 闭包按家共享）。"""

    def __init__(self, max_per_cell: int | None = None,
                 max_per_bidder: int | None = None) -> None:
        self.max_per_cell = max_per_cell or config.vl_fallback_max_per_cell()
        self.max_per_bidder = max_per_bidder or config.vl_fallback_max_per_bidder()
        self._per_cell: dict[str, int] = {}
        self._per_bidder: dict[int, int] = {}
        self._lock = threading.Lock()

    def consume(self, bidder_id: int, cell_key: str) -> tuple[bool, str]:
        """占用一次调用名额；False 时返回原因（不占名额）。"""
        with self._lock:
            if self._per_cell.get(cell_key, 0) >= self.max_per_cell:
                return False, "cell_budget_exhausted"
            if self._per_bidder.get(bidder_id, 0) >= self.max_per_bidder:
                return False, "bidder_budget_exhausted"
            self._per_cell[cell_key] = self._per_cell.get(cell_key, 0) + 1
            self._per_bidder[bidder_id] = self._per_bidder.get(bidder_id, 0) + 1
            return True, ""


SYSTEM_PROMPT = (
    "你是招投标材料审核的视觉评审助手。给你若干张投标文件材料页图"
    "（每张图是一页材料，图号与页码的对应关系在用户消息中给出），"
    "以及一条评标细则原文。\n"
    "必须遵守：\n"
    "1. 只依据图中实际所见与细则原文判断，不得使用图外知识、不得猜测；\n"
    "2. 输出严格 JSON（不要多余文字），结构：\n"
    '{"verdict":"pass|fail|unverifiable","score":<数字或null>,"level":<档位标签或null>,'
    '"observations":[{"document_id":<编号或null>,"page_no":<页码或null>,'
    '"observation":"客观描述所见"}],"reasoning":"按细则逐条对照的依据",'
    '"clues":["仅否决/资格/符合性格填写线索，如：未见签章"]}\n'
    "3. 计分格（count/subjective）：按细则给 score（0~满分）；图中确实无法判定时 "
    'score=null 且 verdict="unverifiable"；\n'
    "4. 否决/资格/符合性格：**绝对禁止输出 verdict=\"fail\"**——即使图中发现不符"
    "也只在 clues 里写线索，verdict 只能是 pass 或 unverifiable（否决由评标"
    "委员会认定，AI 不否决）；\n"
    "5. observation 必须是页级客观描述（看到了什么材料/文字/印章），不得复述细则。\n"
)


def build_user_prompt(criterion: dict, *, is_verdict: bool,
                      page_images: list[dict] | None = None) -> str:
    max_score = criterion.get("max_score")
    head = "本条为否决/资格/符合性条款：只给观察与线索，禁止给分、禁止判不符合。" \
        if is_verdict else (
            f"本条为计分条款（满分 {max_score}）：按细则在 0~{max_score} 内给分。"
            if max_score is not None else "本条为计分条款：按细则给分。")
    parts = [
        head,
        f"评分项：{criterion.get('name') or ''}",
        f"细则原文：{(criterion.get('description') or criterion.get('quote') or '')[:1500]}",
    ]
    if criterion.get("required_evidence"):
        parts.append(f"证明材料要求：{str(criterion['required_evidence'])[:500]}")
    if page_images:
        mapping = "；".join(
            f"第{i + 1}张图=文档{pi.get('document_id')}第{pi.get('page_no')}页"
            for i, pi in enumerate(page_images))
        parts.append(f"页码对照：{mapping}。observation 的 page_no 请填上述实际页码。")
    return "\n".join(parts)


def _coerce_score(value: Any, max_score: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        score = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if score < 0:
        return None
    if max_score is not None:
        try:
            if score > Decimal(str(max_score)):
                return None
        except (InvalidOperation, ValueError):
            pass
    return score


def rescue(
    criterion: dict,
    page_images: list[dict],
    *,
    is_verdict: bool | None = None,
    data_class: str | None = None,
    gateway: Any | None = None,
) -> VlResult:
    """对一组材料页图发起一次 VL 兜底；失败一律 VlResult(kind="failed")。

    一页一图多图单请求（2026-11-23 run 151 实证：整本长图被拒收 400）；
    gateway 可注入（测试 mock），缺省走 auto_review.get_vision_gateway
    （timeout=config.vl_timeout_s()，涉密闸门在网关内）。
    重试纪律：本层不加重试——provider 内部对瞬时错误（超时/429/5xx）自重试；
    拒收类（LLMError.transient=False，如 HTTP 400）直接判失败不重试。
    """
    if not config.vl_fallback_enabled():
        return VlResult("failed", reason="vl_fallback_disabled")
    if not page_images:
        return VlResult("failed", reason="no_material_pages")
    if is_verdict is None:
        is_verdict = is_verdict_criterion(criterion)
    try:
        if gateway is None:
            from app.engine.auto_review import get_vision_gateway

            gateway = get_vision_gateway(timeout=config.vl_timeout_s())
    except Exception as exc:  # noqa: BLE001 —— 未配置 VL 模型=兜底不可用，维持暂定
        return VlResult("failed", reason=f"vision_gateway_unavailable:{type(exc).__name__}")

    user_prompt = build_user_prompt(criterion, is_verdict=is_verdict,
                                    page_images=page_images)
    images = [pi["data_url"] for pi in page_images]
    try:
        result = gateway.chat(SYSTEM_PROMPT, user_prompt, images=images,
                              data_class=data_class or "synthetic")
        text = getattr(result, "content", None) or str(result)
    except Exception as exc:  # noqa: BLE001 —— 网络/涉密闸门/模型故障=维持暂定
        log.warning("VL 兜底调用失败：%s", exc)
        if getattr(exc, "transient", True) is False:
            # 拒收类（HTTP 400 等）：重试必然同样被拒，直接失败
            return VlResult("failed", reason="vl_call_rejected")
        return VlResult("failed", reason=f"vl_call_failed:{type(exc).__name__}")

    try:
        data = _extract_json(text)
        parsed = VlVerdict.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("VL 输出契约校验失败：%s", exc)
        return VlResult("failed", reason=f"vl_contract_invalid:{type(exc).__name__}")

    observations = [o.model_dump() for o in parsed.observations]
    if not observations:
        return VlResult("failed", reason="vl_no_observations",
                        reasoning=parsed.reasoning, clues=list(parsed.clues))
    if parsed.verdict not in ("pass", "fail", "unverifiable"):
        return VlResult("failed", reason="vl_bad_verdict",
                        observations=observations, reasoning=parsed.reasoning)

    if is_verdict:
        # 双保险之一：否决/资格/符合性格 VL 输出 fail → 拒收（AI 不否决）
        if parsed.verdict == "fail":
            return VlResult("failed", reason="vl_verdict_rejected",
                            observations=observations, reasoning=parsed.reasoning,
                            clues=list(parsed.clues))
        return VlResult("clue_only", observations=observations,
                        reasoning=parsed.reasoning, clues=list(parsed.clues))

    # 计分格：必须给出合法分（越界/缺失=VL 失败，维持暂定）
    score = _coerce_score(parsed.score, criterion.get("max_score"))
    if score is None:
        return VlResult("failed", reason="vl_no_valid_score",
                        observations=observations, reasoning=parsed.reasoning)
    return VlResult("scored", score=score, observations=observations,
                    reasoning=parsed.reasoning)


def _extract_json(text: str) -> dict:
    """从模型输出提取首个 JSON 对象（容忍代码块围栏/前后杂文）。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object in output")
    return json.loads(t[start:end + 1])
