/**
 * 证据展示口径的单一来源（2026-11-05）。
 *
 * 背景：详情抽屉曾对所有 0 引文条目统一显示「无证据不评分，该项应由人工补录证据
 * 或降级」——但判定类条目（veto/match 的确定性判定、公式/计数确定性计算）设计上
 * 就不产生引文（过程即证据），并非"缺证据"；"人工补录证据"UI 也不存在。文案按
 * 条目性质分层，四个组件（ItemReasonDrawer/ItemTable/EvidencePanel/EvidenceWorkbench）
 * 共用本文件，防止逐处漂移。
 */
import type { SummaryItem } from "../types";

export type Side = SummaryItem["side"];
export type VerdictSource = "llm" | "machine" | null | undefined;

/** warnings_json 条目 → 判定来源（结构化 verdict_source 键；缺键按 machine 兜底，
 *  与后端 score_summary_service._verdict_source 同口径）。供 ScoreOut（证据核验页）用。 */
export function verdictSourceFromWarnings(
  warnings: unknown,
): "llm" | "machine" {
  if (Array.isArray(warnings)) {
    for (const w of warnings) {
      if (w && typeof w === "object" && "verdict_source" in w) {
        const v = (w as { verdict_source?: unknown }).verdict_source;
        if (v === "llm" || v === "machine") return v;
      }
    }
  }
  return "machine";
}

/** score_type → side（ScoreOut 没有 side 字段时的同口径推导；
 *  与后端 score_summary_service._score_side 一致：未知按客观）。 */
export function sideFromScoreType(scoreType: string | null | undefined): Side {
  if (scoreType === "subjective") return "subjective";
  if (scoreType === "veto" || scoreType === "match") return "verdict";
  return "objective";
}

/** 证据引文空态文案（EvidencePanel 的 emptyText）。 */
export function evidenceEmptyText(
  side: Side,
  verdictSource: VerdictSource,
  state?: string,
): string {
  if (state === "not_run") return "本次运行未执行该项";
  if (side === "subjective")
    return "没有证据引用——无证据不评分，该项应由人工在证据核验页改分或降级处置";
  if (verdictSource === "llm")
    return "AI 裁决未附逐字引文——判定理由见上方“机器判定理由”，可到证据核验页核对原件后处置";
  if (side === "verdict")
    return "确定性判定项：判定过程即证据（结构化事实比对/原文检索），设计上不产生引文";
  return "确定性计算项：计算过程即证据（价格公式可复算/计数有原语与参数），设计上不产生引文";
}

/** 「证据」列 0 引文时的标签（ItemReasonDrawer / ItemTable 逐字一致）。 */
export function evidenceCellLabel(
  side: Side,
  verdictSource: VerdictSource,
): string {
  if (side === "subjective") return "无引用";
  if (verdictSource === "llm") return "无引用（AI 裁决未附引文）";
  if (side === "verdict") return "无引用（确定性判定）";
  return "无引用（确定性计算）";
}
