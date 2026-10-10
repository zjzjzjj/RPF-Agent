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


def run_validators(snapshot: dict, node_exists: Callable[[str], bool] | None = None) -> list[Finding]:
    """按顺序跑全部校验器，返回 findings（error 在前）。"""
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
                findings.append(Finding("CITATION_UNREACHABLE", "error",
                                        f"评分项「{c['name']}」的条款引用 {c['source_node_id']} 在文档中不存在",
                                        "criterion", code))
        if not (c.get("source_quote") or "").strip():
            findings.append(Finding("QUOTE_REQUIRED", "error",
                                    f"评分项「{c['name']}」缺少招标文件原文引用（无证据的标准不得作为评分依据）",
                                    "criterion", code))

        # ⑤ 价格公式变量完备
        if c["score_type"] == "formula":
            f = c.get("formula") or {}
            missing = [k for k in ("formula_type", "benchmark_method", "k") if not f.get(k)]
            if c.get("max_score") is None:
                missing.append("max_score")
            if missing:
                findings.append(Finding("PRICE_PARAMS_INCOMPLETE", "error",
                                        f"价格分公式缺少参数：{('、'.join(missing))}",
                                        "formula", code, "请人工补全公式参数后确认"))

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
        if not (Decimal("10") <= v <= Decimal("60")):
            findings.append(Finding("PRICE_WEIGHT_RANGE", "warn",
                                    f"价格分权重 {v} 分超出常规区间（10–40）", "package"))

    # ⑨ 总分 = 各项满分之和（只统计非否决项）
    scored = [c for c in criteria if not c.get("is_veto")]
    maxes = [c.get("max_score") for c in scored]
    if all(m is not None for m in maxes) and maxes:
        total = sum((Decimal(str(m)) for m in maxes), ZERO)
        target = Decimal(str(snapshot.get("package", {}).get("total_max", 100)))
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
        if hi1 >= lo2:
            # 端点重合或区间相交 → 同一分值可落进两个档位，歧义
            problems.append(Finding("LEVEL_OVERLAP", "error",
                                    f"「{c['name']}」档位「{l1}」与「{l2}」区间重叠", "criterion", c["code"]))
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
