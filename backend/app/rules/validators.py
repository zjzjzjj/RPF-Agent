"""规则包确定性校验器（12 条，纯代码，禁止调用 LLM）。

全部只吃 snapshot dict（纯数据、可单测）；error 级未清零 → confirm/freeze 一律 422。
这条链是合规底线：49 条（只能用招标文件载明的标准）+ 财库 69 号（客观分必须一致）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

ZERO = Decimal("0")


@dataclass
class Finding:
    code: str
    severity: str  # error|warn
    message: str
    subject_type: str | None = None  # criterion|formula|package
    subject_id: str | None = None  # criterion code
    hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "message": self.message,
            "hint": self.hint,
        }


def run_validators(
    snapshot: dict,
    node_exists: Callable[[str], bool] | None = None,
    *,
    total_max: Decimal | str | int | None = None,
    price_weight_range: tuple[Decimal | str | int, Decimal | str | int] | None = None,
) -> list[Finding]:
    """按顺序跑全部校验器，返回 findings（error 在前）。

    total_max / price_weight_range 为可选覆盖（语料库等非完整规则包场景用）；
    缺省时保持原行为：total_max 取 snapshot.package.total_max 或 100，
    price_weight_range 取 (10, 60)。
    """
    findings: list[Finding] = []
    criteria = snapshot.get("criteria", [])
    by_code = {c["code"]: c for c in criteria}

    for c in criteria:
        code = c["code"]

        # ① 档位区间闭合：连续覆盖 [0, max] 且不重叠（主观分）
        if c["score_type"] == "subjective":
            findings += _check_level_closure(c)

        # ③ 引用可达：source_node_id 必须存在且 quote 非空
        if c.get("source_node_id"):
            if node_exists is not None and not node_exists(c["source_node_id"]):
                # 法条:// 引用走法条知识库核验（区别于项目文档 doc_nodes，误导排查要分开说）
                where = (
                    "在法条知识库中不存在"
                    if str(c["source_node_id"]).startswith("法条://")
                    else "在文档中不存在"
                )
                findings.append(Finding("CITATION_UNREACHABLE", "error",
                                        f"评分项「{c['name']}」的条款引用 {c['source_node_id']} {where}",
                                        "criterion", code))
        # 模板条目（P5）没有原文，QUOTE_REQUIRED 按 origin 分支豁免（设计稿 §6.1）；
        # 其"依据缺失"由 TEMPLATE_FALLBACK_SIGNOFF 承担（error，阻断冻结）。
        if c.get("origin") != "template" and not (c.get("source_quote") or "").strip():
            findings.append(Finding("QUOTE_REQUIRED", "error",
                                    f"评分项「{c['name']}」缺少招标文件原文引用（无证据的标准不得作为评分依据）",
                                    "criterion", code))

        # 模板兜底签批（设计稿 §6.1）：模板条目未经签批不得执行
        if c.get("origin") == "template" and c.get("status") != "retired" and not c.get("signoff"):
            findings.append(Finding("TEMPLATE_FALLBACK_SIGNOFF", "error",
                                    f"评分项「{c['name']}」为模板兜底（P5），招标文件未规定；未签批不得冻结",
                                    "criterion", code,
                                    "请在签批队列中逐条确认启用理由"))
        # 合规红线（S0 为 warn，S2 转 error）：价格/否决/资格类模板兜底即使签批也需单列
        if c.get("origin") == "template" and c.get("status") != "retired":
            if c.get("is_veto") or c.get("category") in ("qualification", "compliance", "price"):
                findings.append(Finding("TEMPLATE_FALLBACK_ESSENTIAL", "warn",
                                        f"评分项「{c['name']}」为模板兜底且属资格/符合性/价格/否决类，"
                                        "签批理由应在报告中单列",
                                        "criterion", code))

        # ⑤ 价格公式变量完备（按公式类型判定：ratio 要 k，deviation 要扣分率）
        if c["score_type"] == "formula":
            f = c.get("formula") or {}
            ext = f.get("params_json") or {}
            formula_type = f.get("formula_type") or ""
            missing: list[str] = []
            if not formula_type:
                # 2026-10-28：识别不出公式形态（解析器对价格句式无任何已知形态命中）
                # ≠ 参数缺失——用独立 error 码，且 _auto_settle_blocking 不自动收敛，
                # 直到人工补全公式。这是"静默算错"的兜底。
                findings.append(Finding(
                    "PRICE_FORMULA_UNRECOGNIZED", "error",
                    f"评分项「{c['name']}」的价格公式无法识别（招标文件中未匹配到任何已知公式形态）",
                    "formula", code, "请人工确认并补全该价格公式"))
            if not f.get("benchmark_method"):
                missing.append("benchmark_method")
            if c.get("max_score") is None:
                missing.append("max_score")
            if formula_type == "deviation_deduct":
                if not (
                    f.get("deduct_rate")
                    or ext.get("deduct_rate")
                    or ext.get("deduct_rate_down")
                    or ext.get("deduct_rate_up")
                ):
                    missing.append("deduct_rate_down/up")
            elif formula_type in ("benchmark_ratio", "lowest_ratio"):
                if not f.get("k"):
                    missing.append("k")
            if missing:
                findings.append(Finding("PRICE_PARAMS_INCOMPLETE", "error",
                                        f"评分项「{c['name']}」的价格公式缺少参数：{('、'.join(missing))}",
                                        "formula", code, "请人工补全公式参数后确认"))
            upper = f.get("upper_bound")
            if (
                upper is not None
                and c.get("max_score") is not None
                and Decimal(str(upper)) > Decimal(str(c["max_score"]))
            ):
                findings.append(Finding("PRICE_UPPER_BOUND", "error",
                                        f"评分项「{c['name']}」的价格分上限 {upper} 大于满分 {c['max_score']}",
                                        "formula", code, "价格分不得超过该项满分"))
            if formula_type == "benchmark_ratio" and f.get("benchmark_method") == "average":
                findings.append(Finding("PRICE_FULL_MARK_RISK", "warn",
                                        f"评分项「{c['name']}」使用 benchmark_ratio + average："
                                        "所有低于平均基准价的报价都会被上限截成满分",
                                        "formula", code,
                                        "如招标文件是偏离扣分型，请改用 deviation_deduct 并填写扣分率"))
            # 分档截尾/门槛/离群：引擎侧执行状态（P1-A/B/C 落地后按字段消警）
            approx = []
            if ext.get("trim_tiers"):
                approx.append("按有效家数分档截尾")
            if ext.get("benchmark_gate"):
                approx.append("基准价资格门槛")
            if ext.get("outlier_policy"):
                approx.append("离群剔除重算")
            if ext.get("special_rules") and not (
                ext.get("benchmark_gate") or ext.get("outlier_policy")
            ):
                approx.append("基准价剔除/重算特殊规则")
            if ext.get("lot_weights"):
                approx.append("多产品加权（lot_weights，按单包总价近似）")
            if approx:
                findings.append(Finding("PRICE_ENGINE_APPROX", "warn",
                                        f"评分项「{c['name']}」的{'、'.join(approx)}"
                                        "由引擎按近似口径执行（原文已留档）",
                                        "formula", code,
                                        "请人工复核价格分口径是否与评标细则一致"))

            # 分档截尾结构完整性（P0-D）：[[bound|None, cut], ...] 末档 None、cut 单调不减
            tiers = ext.get("trim_tiers")
            if tiers is not None:
                tier_bad = not (
                    isinstance(tiers, list)
                    and tiers
                    and all(
                        isinstance(row, (list, tuple))
                        and len(row) == 2
                        and (row[0] is None or (isinstance(row[0], (int, float))))
                        and isinstance(row[1], int)
                        and row[1] >= 0
                        for row in tiers
                    )
                    and tiers[-1][0] is None
                    and all(tiers[i][1] <= tiers[i + 1][1] for i in range(len(tiers) - 1))
                )
                if tier_bad:
                    findings.append(Finding("PRICE_TRIM_TIERS_MALFORMED", "error",
                                            f"评分项「{c['name']}」的 trim_tiers 结构非法："
                                            "须为 [[bound|None, cut]...]，末档 bound=None 且 cut 单调不减",
                                            "formula", code, "请人工修正分档截尾参数"))

            # 基准价资格门槛结构完整性（P0-D）
            gate = ext.get("benchmark_gate")
            if gate is not None:
                gate_bad: list[str] = []
                if not isinstance(gate, dict):
                    gate_bad.append("须为对象")
                else:
                    kind = gate.get("ratio_kind")
                    if kind not in ("percent", "absolute"):
                        gate_bad.append("ratio_kind")
                    try:
                        ratio = Decimal(str(gate.get("min_ratio")))
                        if ratio <= 0 or (kind == "percent" and ratio > 100):
                            gate_bad.append("min_ratio")
                    except Exception:  # noqa: BLE001
                        gate_bad.append("min_ratio")
                    if gate.get("on_fail") not in ("exclude", "half_max"):
                        gate_bad.append("on_fail")
                    if gate.get("on_fail") == "half_max" and not gate.get("half_max_ratio"):
                        gate_bad.append("half_max_ratio")
                    if gate.get("gate_on") not in (
                        "tech", "tech_qualification", "biz_tech", "sample_or_composite",
                    ):
                        gate_bad.append("gate_on")
                if gate_bad:
                    findings.append(Finding("PRICE_GATE_INCOMPLETE", "error",
                                            f"评分项「{c['name']}」的 benchmark_gate 参数不完备："
                                            f"{'、'.join(gate_bad)}",
                                            "formula", code, "请按评标细则原文补全门槛参数"))

            # 离群保护结构完整性（P0-D）
            outlier = ext.get("outlier_policy")
            if outlier is not None:
                out_bad: list[str] = []
                if not isinstance(outlier, dict):
                    out_bad.append("须为对象")
                else:
                    if outlier.get("on_hit") != "zero_and_recompute":
                        out_bad.append("on_hit")
                    if "max_mult" not in outlier and "min_mult" not in outlier:
                        out_bad.append("max_mult/min_mult 至少其一")
                if out_bad:
                    findings.append(Finding("PRICE_OUTLIER_POLICY_INCOMPLETE", "warn",
                                            f"评分项「{c['name']}」的 outlier_policy 参数不完备："
                                            f"{'、'.join(out_bad)}（引擎将不执行离群保护）",
                                            "formula", code, "请按评标细则原文补全离群参数"))

            # 基准法标签归一留痕（G20）：确定性目录与模型自报不一致 → 以目录为准并提示
            mismatch = ext.get("_label_mismatch")
            if isinstance(mismatch, dict) and mismatch.get("deterministic"):
                findings.append(Finding("PRICE_METHOD_LABEL_MISMATCH", "warn",
                                        f"评分项「{c['name']}」的基准法标签不一致："
                                        f"模型「{mismatch.get('model')}」→ 确定性目录"
                                        f"「{mismatch.get('deterministic')}」（已按目录归一）",
                                        "formula", code,
                                        "请人工确认归一结果与评标细则一致"))

        # P0-C：主/客观冲突告警——hint 来自评分表明文列或量化措辞，score_type 却是
        # match（骨架缺席时 LLM 回退路径的历史错分形态：12 分客观被误分成 match）
        if (
            (c.get("params") or {}).get("_subjectivity_hint") == "objective"
            and c.get("score_type") == "match"
            and not c.get("is_veto")
        ):
            findings.append(Finding("AMBIGUOUS_SUBJECTIVITY", "warn",
                                    f"评分项「{c['name']}」的细则含客观计分措辞/明文标注，"
                                    "但评分类型是 match（满足即得分）——请人工确认是否应为 count/formula",
                                    "criterion", code,
                                    "核对评标细则原文的主/客观分列与计分方式"))

        # ⑦ 否决项必须有后果
        if c.get("is_veto") and not c.get("veto_consequence"):
            findings.append(Finding("VETO_CONSEQUENCE_MISSING", "error",
                                    f"否决项「{c['name']}」未定义否决后果", "criterion", code))

        # ⑧ 否决项不得作为评分因素给分（法规红线）
        if c.get("is_veto") and c.get("max_score") not in (None, ZERO):
            findings.append(Finding("VETO_SCORED", "error",
                                    f"否决项「{c['name']}」不得同时作为评分因素给分", "criterion", code))

        # ⑪ 占位符残留
        for field in ("name", "description"):
            text = c.get(field) or ""
            if _has_placeholder(text):
                findings.append(Finding("PLACEHOLDER_LEFT", "error",
                                        f"评分项「{c['name']}」的{field}含占位符（XX/待定/____）",
                                        "criterion", code, "请替换为确认后的文字"))

    # ② 权重合计（若设置了权重）
    weights = [c.get("weight") for c in criteria if c.get("weight") is not None]
    if weights and len(weights) == len(criteria):
        total = sum((Decimal(str(w)) for w in weights), ZERO)
        if total != Decimal("100"):
            findings.append(Finding("WEIGHT_SUM", "error",
                                    f"权重合计 {total} ≠ 100", "package"))

    # ⑥ 价格权重区间（可配置，warn）
    price_max = [c.get("max_score") for c in criteria if c["category"] == "price"]
    if price_max and price_max[0] is not None:
        v = Decimal(str(price_max[0]))
        lo, hi = price_weight_range if price_weight_range is not None else ("10", "60")
        if not (Decimal(str(lo)) <= v <= Decimal(str(hi))):
            findings.append(Finding("PRICE_WEIGHT_RANGE", "warn",
                                    f"价格分权重 {v} 分超出常规区间（{lo}–{hi}）", "package"))

    # ⑨ 总分 = 各计分项满分之和（veto/match 不计分；计分项缺满分是错误，
    # 不能像旧实现那样因为一个 None 就整段跳过校验）
    scored = [
        c for c in criteria
        if c.get("score_type") in ("formula", "count", "subjective")
    ]
    missing_max = [c["code"] for c in scored if c.get("max_score") is None]
    if missing_max:
        findings.append(Finding("MAX_SCORE_MISSING", "error",
                                f"计分项缺少满分：{'、'.join(missing_max)}", "package"))
    maxes = [c.get("max_score") for c in scored]
    if all(m is not None for m in maxes) and maxes:
        total = sum((Decimal(str(m)) for m in maxes), ZERO)
        target = total_max if total_max is not None else snapshot.get("package", {}).get("total_max", 100)
        target = Decimal(str(target))
        if total != target:
            findings.append(Finding("TOTAL_SCORE", "error",
                                    f"各评分项满分之和 {total} ≠ {target}", "package"))

    # ⑩ code 唯一
    codes = [c["code"] for c in criteria]
    dupes = {c for c in codes if codes.count(c) > 1}
    if dupes:
        findings.append(Finding("CODE_DUPLICATE", "error",
                                f"评分项编号重复：{sorted(dupes)}", "package"))

    # ⑫ 档位单调（优 ≥ 良 ≥ 一般 ≥ 差 的区间不逆序）
    for c in criteria:
        if c["score_type"] == "subjective":
            findings += _check_level_monotonic(c)

    # ④ 已并入 ①（闭合检查包含连续性）；这里保留 code 稳定性说明
    errors = [f for f in findings if f.severity == "error"]
    return sorted(errors, key=lambda f: f.code) + sorted(
        (f for f in findings if f.severity != "error"), key=lambda f: f.code
    )


def has_blocking_errors(findings: list[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)


def _check_level_closure(c: dict) -> list[Finding]:
    levels = c.get("levels") or []
    if not levels:
        # 真实标书常把主观分拆在子项里（如"协同办公（0-2分）、行政管理（0-2分）…"），
        # 原文没有档位表。引擎本就支持无档位评分（按判据在 0-满分内给分，
        # 见 subjective.py 的 levels_text 兜底），故有满分时只 warn 不阻断；
        # 连满分都没有则完全不可评，保持 error。
        if c.get("max_score") is not None:
            return [Finding("LEVELS_MISSING", "warn",
                            f"主观评分项「{c['name']}」没有任何档位（原文未给分值档位，将按 0-满分综合评定）",
                            "criterion", c["code"],
                            "如原文有档位表请人工补录；无档位时主观分按 0-满分综合评定")]
        return [Finding("LEVELS_MISSING", "error",
                        f"主观评分项「{c['name']}」没有任何档位", "criterion", c["code"])]
    for lv in levels:
        if lv.get("min") is None or lv.get("max") is None:
            return [Finding("LEVEL_RANGE_MISSING", "error",
                            f"「{c['name']}」档位「{lv.get('label')}」缺少分值区间（原文未给的需人工补）",
                            "criterion", c["code"], "歧义项：不得由系统猜测分值")]
    intervals = sorted(
        ((Decimal(str(lv["min"])), Decimal(str(lv["max"])), lv.get("label")) for lv in levels),
        key=lambda t: t[0],
    )
    problems: list[Finding] = []
    # 下界必须为 0：档位区间要连续覆盖 [0, max]，从正数起步意味着最低档之下是空白区
    lowest_lo = intervals[0][0] if intervals else None
    if lowest_lo is not None and lowest_lo != ZERO:
        problems.append(Finding(
            "LEVEL_BOTTOM_MISMATCH", "error",
            f"「{c['name']}」最低档下界 {lowest_lo} ≠ 0（分值区间未覆盖从 0 起的完整范围）",
            "criterion", c["code"],
        ))
    for lo, hi, label in intervals:
        if lo > hi:
            problems.append(Finding("LEVEL_RANGE_INVALID", "error",
                                    f"「{c['name']}」档位「{label}」区间下界大于上界", "criterion", c["code"]))
    for (lo1, hi1, l1), (lo2, hi2, l2) in zip(intervals, intervals[1:]):
        if hi1 > lo2:
            # 区间内部相交 → 同一分值可落进两个档位，真歧义
            problems.append(Finding("LEVEL_OVERLAP", "error",
                                    f"「{c['name']}」档位「{l1}」与「{l2}」区间重叠", "criterion", c["code"]))
        elif hi1 == lo2:
            # 端点共享（原文常见写法："得0-2分/得2-3分"）——评分时按较低档确定性归属
            #（level_for_score 取首个命中档），不阻断，提示留痕即可
            problems.append(Finding("LEVEL_OVERLAP", "warn",
                                    f"「{c['name']}」档位「{l1}」与「{l2}」共享边界值 {hi1}"
                                    "（原文如此，评分时该分值归入较低档）",
                                    "criterion", c["code"]))
        elif hi1 + 1 < lo2:
            # 整数分值档位之间允许正好相邻（如 16 与 17），中间留空则是空洞
            problems.append(Finding("LEVEL_GAP", "error",
                                    f"「{c['name']}」档位「{l1}」与「{l2}」之间有分值空洞", "criterion", c["code"]))
    max_score = c.get("max_score")
    if max_score is not None and intervals:
        top_hi = max(Decimal(str(hi)) for _, hi, _ in intervals)
        if top_hi != Decimal(str(max_score)):
            problems.append(Finding("LEVEL_TOP_MISMATCH", "error",
                                    f"「{c['name']}」最高档上界 {top_hi} ≠ 满分 {max_score}", "criterion", c["code"]))
    return problems


_ORDER = {"优": 0, "良": 1, "中": 2, "一般": 3, "及格": 4, "差": 5}


def _check_level_monotonic(c: dict) -> list[Finding]:
    levels = [lv for lv in (c.get("levels") or []) if lv.get("min") is not None]
    if len(levels) < 2:
        return []
    ordered = sorted(levels, key=lambda lv: _ORDER.get(lv["label"], 99))
    findings: list[Finding] = []
    for lv1, lv2 in zip(ordered, ordered[1:]):
        hi1 = Decimal(str(lv1["max"]))
        lo2 = Decimal(str(lv2["min"]))
        if hi1 < lo2 - 1:  # 允许整数相邻
            findings.append(Finding(
                "LEVEL_NOT_MONOTONIC", "error",
                f"「{c['name']}」档位逆序：{lv1['label']}({lv1['min']}-{lv1['max']}) 与 {lv2['label']}({lv2['min']}-{lv2['max']})",
                "criterion", c["code"],
            ))
    return findings


def _has_placeholder(text: str) -> bool:
    if not text:
        return False
    for token in ("XX", "xx", "××", "待定", "____", "TBD", "待补充"):
        if token in text:
            return True
    return False
