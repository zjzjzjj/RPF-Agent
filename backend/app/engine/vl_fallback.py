# -*- coding: utf-8 -*-
"""材料审核视觉大模型兜底（2026-11-23 优化②）。

仅对文本链路失败格触发（暂定/no_facts/存在性未核验）：把材料所在页按
"一份材料文档一张长图"竖拼（evidence.renderer.render_long_image），过云端
视觉大模型（BID_EVAL_VISION_MODEL 三件套，走 Gateway 涉密闸门），按冻结细则
处理：

- **计分格**（count/subjective）：只报逐案例结构化事实（facts，2026-11-24
  "VL 报事实、代码算分"），定分由确定性代码按细则硬条件算；事实全被拒收/
  无法判定 → 视为 VL 失败，维持严口径分+疑问。
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
    "material_unverified",      # 严口径未核验格（2026-11-24 run158 修正）：VL 翻案
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


class VlFact(BaseModel):
    """逐案例/逐证书/逐人员结构化事实（计分格专用；只报事实，不报分、不判合格）。

    null 语义：null=图上未见或看不清（不猜、不补全）；true=图上确认存在（唯一
    可采信值）；false=模型声称确认无——**消费侧归一为 None 只作疑问线索**
    （VL 只看≤8 张选送页，"未见≠全书没有"，防 VL 误杀）。2026-11-24 run159
    实证：模型直接给分时"自述未见发票/验收仍计分、56 万当≥60万"——定分权收归
    确定性代码，模型只报所见。
    """
    model_config = {"str_strip_whitespace": True}

    kind: str                      # contract | certificate | person
    name: str | None = None        # 案例/证书/人员名称（看不清=null；无名称案例不可计分）
    # —— 业绩/合同 ——
    amount_yuan: str | None = None     # 元、纯数字字符串；看不清=null（绝不猜）
    sign_date: str | None = None       # YYYY-MM-DD / YYYY-MM / YYYY；看不清=null
    project_type: str | None = None    # 图上所见类型原词（"运维""系统集成"…），只转述不归类
    has_contract: bool | None = None
    has_invoice: bool | None = None
    has_acceptance: bool | None = None
    has_seal: bool | None = None       # 合同/发票等材料上的盖章有无
    # —— 人员归属与人员材料（contract 事实：材料上出现的评分人员姓名）——
    person_names: list[str] = Field(default_factory=list)   # 图上所见评分人员姓名，空=未见
    has_work_proof: bool | None = None       # 业绩证明材料上有其姓名（业主证明/用户证明/社保页不算业绩证明）
    has_experience_proof: bool | None = None # 个人工作经验证明上有其姓名
    has_social_security: bool | None = None  # 社保/参保证明上有其姓名
    has_cert_proof: bool | None = None       # 细则点名的人员证书/培训证书
    # —— 证书 ——
    cert_level: str | None = None
    valid_until: str | None = None
    has_query_screenshot: bool | None = None   # 查询结果截图有无（细则常点名要件）
    # —— 人员 ——
    person_title: str | None = None
    person_years: str | None = None
    # —— 证据（每条事实必须给页级依据）——
    evidence: list[VlObservation] = Field(default_factory=list)


class VlVerdict(BaseModel):
    """VL 输出契约（pydantic 校验，任何不合法=VL 失败→维持暂定）。

    2026-11-24：计分格输出面收窄为 facts（结构化事实）——score/level 删除，
    模型定分权拔除（"VL 报事实、代码算分"，用户拍板）。判定格语义不变
    （verdict/clues，只给线索绝不产否决）。
    """
    model_config = {"str_strip_whitespace": True}

    verdict: str  # pass|fail|unverifiable（判定格语义；计分格仅作弃权标记）
    facts: list[VlFact] = Field(default_factory=list)
    observations: list[VlObservation] = Field(default_factory=list)
    reasoning: str = ""
    clues: list[str] = Field(default_factory=list)


@dataclass
class VlResult:
    kind: str                      # facts | clue_only | failed
    facts: list[dict] = field(default_factory=list)
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
    "2. 输出严格 JSON：**单个 JSON 对象**（不要 markdown 代码块、不要多余文字），结构：\n"
    '{"verdict":"pass|unverifiable",'
    '"facts":[{"kind":"contract|certificate|person","name":"名称或null",'
    '"amount_yuan":"金额（元，纯数字）或null","sign_date":"签订日期或null",'
    '"project_type":"图上所见项目类型原词或null",'
    '"has_contract":true|null|false,"has_invoice":true|null|false,'
    '"has_acceptance":true|null|false,"has_seal":true|null|false,'
    '"person_names":["图上所见评分人员姓名，未见=空数组"],'
    '"has_work_proof":true|null|false,"has_experience_proof":true|null|false,'
    '"has_social_security":true|null|false,"has_cert_proof":true|null|false,'
    '"cert_level":"等级或null","valid_until":"有效期至或null",'
    '"has_query_screenshot":true|null|false,'
    '"person_title":"职称或null","person_years":"年限或null",'
    '"evidence":[{"document_id":<编号或null>,"page_no":<页码或null>,'
    '"observation":"该事实的页级依据"}]}],'
    '"observations":[{"document_id":<编号或null>,"page_no":<页码或null>,'
    '"observation":"客观描述所见"}],"reasoning":"依据说明",'
    '"clues":["仅否决/资格/符合性格填写线索，如：未见签章"]}\n'
    "3. 计分格（count/subjective）：**只输出 facts（逐案例/逐证书/逐人员结构化事实），"
    "禁止给分、禁止判合格不合格**。每条事实：图上确实看清才填值，看不清或未见一律 null，"
    "禁止用细则或常识补全；金额填元的纯数字字符串；has_contract/has_invoice/"
    "has_acceptance/has_seal/has_query_screenshot 填 true/null/false（true=确认有、"
    "false=确认无、null=看不清或未见）；evidence 必须给 document_id/page_no 与客观观察。"
    "图中确实无法判定任何事实时 facts=[] 且 verdict=\"unverifiable\"；\n"
    "4. 否决/资格/符合性格：**绝对禁止输出 verdict=\"fail\"**——即使图中发现不符"
    "也只在 clues 里写线索，verdict 只能是 pass 或 unverifiable（否决由评标"
    "委员会认定，AI 不否决）；facts 对判定格不需要；\n"
    "5. observation 必须是页级客观描述（看到了什么材料/文字/印章），不得复述细则。\n"
    "6. 业绩汇总表/一览表是投标人的主张不是证明材料——合同/发票/验收必须见其原件"
    "（复印件/扫描件）才算 true。\n"
    "7. 人员归属与人员材料：合同/案例事实若图上确实看清材料上出现评分人员"
    "（项目负责人/团队成员）姓名，填 person_names；has_work_proof=该人员姓名出现在"
    "**业绩证明材料（合同/中标通知书/发票/验收材料）**上——业主证明/用户证明/"
    "身份证明/社保页**不算**业绩证明材料；has_experience_proof=个人工作经验证明"
    "（年限/经历证明）上有其姓名；has_social_security=社保/参保证明上有其姓名；"
    "has_cert_proof=细则点名的人员证书/培训证书。三态含义同 has_contract"
    "（true=确认、null=未见或看不清、false=确认无）。\n"
)


def build_user_prompt(criterion: dict, *, is_verdict: bool,
                      page_images: list[dict] | None = None) -> str:
    max_score = criterion.get("max_score")
    head = "本条为否决/资格/符合性条款：只给观察与线索，禁止给分、禁止判不符合。" \
        if is_verdict else (
            "本条为计分条款：只报事实不下分（逐案例/逐证书/逐人员填 facts）。"
        )
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
        parts.append(f"页码对照：{mapping}。observation/evidence 的 page_no 请填上述实际页码。")
    return "\n".join(parts)


def _sanitize_facts(
    raw_facts: list[VlFact], page_images: list[dict],
) -> tuple[list[dict], int]:
    """自洽性拒收——**拒案例不拒整格**（2026-11-24）。

    ①evidence 页码不在本次送审页集→剔越界页，evidence 清空→丢案；
    ②观察含"未见X/无X/未提供X"而 has_x=true→has_x 回退 null（模型自相矛盾）；
    ③contract 无金额/金额非法/无名称→丢案（翻案必须有金额，防"金额没看清照计"）；
    ④false 归一为 None（消费侧不判缺——VL 只看≤8 页，未见≠全书没有，防误杀）。
    单案例矛盾只污染该案例；拒整格=一个坏案例废掉全部翻案机会。丢弃留痕可审计。
    返回 (清洗后 facts dict 列表, 拒收条数)。
    """
    allowed_pairs = {(p.get("document_id"), p.get("page_no")) for p in page_images}
    allowed_nos = {p.get("page_no") for p in page_images}
    out: list[dict] = []
    dropped = 0
    for f in raw_facts:
        data = f.model_dump()
        # ④ false → None（归一未核实，只作线索）
        for key in ("has_contract", "has_invoice", "has_acceptance", "has_seal",
                    "has_query_screenshot", "has_work_proof", "has_experience_proof",
                    "has_social_security", "has_cert_proof"):
            if data.get(key) is False:
                data[key] = None
        # ① 证据页越界剔除
        evidence = []
        for e in data.get("evidence") or []:
            pair = (e.get("document_id"), e.get("page_no"))
            pg = e.get("page_no")
            if pair in allowed_pairs or (pg is not None and pg in allowed_nos):
                evidence.append(e)
        data["evidence"] = evidence
        if not evidence:
            # ① 证据清空→丢案（每条事实必须给页级依据）
            dropped += 1
            continue
        # ② 矛盾回退：观察说"未见/无 X"却 has_x=true
        hay = " ".join(str(e.get("observation") or "") for e in evidence)
        for word, key in (("发票", "has_invoice"), ("验收", "has_acceptance"),
                          ("合同", "has_contract"), ("盖章", "has_seal"),
                          ("公章", "has_seal"), ("签章", "has_seal"),
                          ("截图", "has_query_screenshot"),
                          ("业绩证明", "has_work_proof"),
                          ("工作经验证明", "has_experience_proof"),
                          ("经验证明", "has_experience_proof"),
                          ("社保证明", "has_social_security"),
                          ("社保", "has_social_security")):
            if data.get(key) is True and any(w in hay for w in (
                    f"未见{word}", f"无{word}", f"没有{word}", f"未提供{word}",
                    f"未附{word}", f"{word}缺失", f"{word}未见")):
                data[key] = None
        # ②' 归属自洽：has_work_proof=true 但 person_names 空 → 回退 null
        # （不知道为谁开的证明不能当归属证据）
        if data.get("has_work_proof") is True and not (data.get("person_names") or []):
            data["has_work_proof"] = None
        # ③ contract 金额/名称硬要求
        if data.get("kind") == "contract":
            try:
                val = (Decimal(str(data.get("amount_yuan")).replace(",", "").strip())
                       if data.get("amount_yuan") else None)
            except (InvalidOperation, ValueError):
                val = None
            if val is None or val <= 0 or not str(data.get("name") or "").strip():
                dropped += 1
                continue
            data["amount_yuan"] = str(val)
        out.append(data)
    return out, dropped


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

    def _chat() -> str:
        result = gateway.chat(SYSTEM_PROMPT, user_prompt, images=images,
                              data_class=data_class or "synthetic")
        return getattr(result, "content", None) or str(result)

    try:
        text = _chat()
    except Exception as exc:  # noqa: BLE001 —— 网络/涉密闸门/模型故障=维持暂定
        log.warning("VL 兜底调用失败：%s", exc)
        if getattr(exc, "transient", True) is False:
            # 拒收类（HTTP 400 等）：重试必然同样被拒，直接失败
            return VlResult("failed", reason="vl_call_rejected")
        return VlResult("failed", reason=f"vl_call_failed:{type(exc).__name__}")

    # 契约解析加固（2026-11-24 run155 c45 JSONDecodeError 白白放弃）：解析/
    # 校验失败重试一次完整调用（同预算名额内，不额外扣预算）；仍失败才放弃。
    parsed = None
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            parsed = VlVerdict.model_validate(_extract_json(text))
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 2:
                break
            log.warning("VL 输出契约校验失败（重试 1 次）：%s", exc)
            try:
                text = _chat()
            except Exception as exc2:  # noqa: BLE001
                log.warning("VL 兜底重试调用失败：%s", exc2)
                if getattr(exc2, "transient", True) is False:
                    return VlResult("failed", reason="vl_call_rejected")
                return VlResult("failed", reason=f"vl_call_failed:{type(exc2).__name__}")
    if parsed is None:
        log.warning("VL 输出契约校验失败：%s", last_exc)
        return VlResult("failed", reason=f"vl_contract_invalid:{type(last_exc).__name__}")

    observations = [o.model_dump() for o in parsed.observations]
    if parsed.verdict not in ("pass", "fail", "unverifiable"):
        return VlResult("failed", reason="vl_bad_verdict",
                        observations=observations, reasoning=parsed.reasoning)

    if is_verdict:
        if not observations:
            return VlResult("failed", reason="vl_no_observations",
                            reasoning=parsed.reasoning, clues=list(parsed.clues))
        # 双保险之一：否决/资格/符合性格 VL 输出 fail → 拒收（AI 不否决）
        if parsed.verdict == "fail":
            return VlResult("failed", reason="vl_verdict_rejected",
                            observations=observations, reasoning=parsed.reasoning,
                            clues=list(parsed.clues))
        return VlResult("clue_only", observations=observations,
                        reasoning=parsed.reasoning, clues=list(parsed.clues))

    # 计分格：只收结构化事实（VL 报事实、代码算分——2026-11-24 run159 实证）
    if not config.vl_facts_enabled():
        return VlResult("failed", reason="vl_facts_disabled",
                        observations=observations, reasoning=parsed.reasoning)
    facts, dropped = _sanitize_facts(parsed.facts, page_images)
    if dropped:
        log.warning("VL facts 拒收 %d 条案例（自洽性/越界/金额非法）", dropped)
    if not facts:
        return VlResult("failed", reason="vl_no_facts",
                        observations=observations, reasoning=parsed.reasoning)
    return VlResult("facts", facts=facts, observations=observations,
                    reasoning=parsed.reasoning, clues=list(parsed.clues))


def _extract_json(text: str) -> dict:
    """从模型输出提取 JSON 对象。

    2026-11-24 加固：复用 gateway.extract_json 的分级容错解析（围栏/前后杂文/
    尾逗号/非法转义/缺失逗号全修，平衡括号扫描取首个对象；截断才失败）——
    run155 c45 一次 JSONDecodeError 让整格白放弃是病灶。
    """
    from app.llm.gateway import extract_json as _robust_extract

    return _robust_extract(text)
