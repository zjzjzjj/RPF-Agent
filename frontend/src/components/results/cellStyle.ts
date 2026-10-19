import type { MatrixCell } from "../../types";

/** 矩阵单元格配色与文案的唯一映射（评分结果页的「完整矩阵」标签页使用）。 */
export function cellStyle(cell: MatrixCell | undefined): { bg: string; label: string; tip: string } {
  if (!cell) return { bg: "#f0f0f0", label: "—", tip: "未评分" };
  // 全自动模式终态优先：已自动裁决（LLM 带原文证据或确定性判定），不再有待人工中间态
  if (cell.review_status === "auto_confirmed") {
    if (cell.machine_status === "recommend_reject")
      return { bg: "#fff1f0", label: "已否决", tip: "全自动模式终态：LLM/确定性裁决否决（留痕可查）" };
    if (cell.machine_status === "fail")
      return { bg: "#fff1f0", label: "不通过", tip: "全自动模式终态：裁决不通过（留痕可查）" };
    return { bg: "#e6f4ff", label: cell.final_score ?? "已裁决", tip: "已自动裁决（LLM 带原文证据，留痕可查）" };
  }
  if (cell.score_type === "veto") {
    if (cell.review_status === "human_confirmed" && cell.machine_status === "pass")
      return { bg: "#f6ffed", label: "通过", tip: "已人工确认通过" };
    if (cell.machine_status === "recommend_reject")
      return { bg: "#fff1f0", label: "建议否决", tip: "AI 建议否决，等待人工确认（人工模式）" };
    if (cell.review_status === "conflict_flagged")
      return { bg: "#fff7e6", label: "冲突", tip: "存在冲突，需人工裁决" };
    if (cell.machine_status === "need_human") return { bg: "#fff7e6", label: "待人工", tip: "无法自动判定" };
    return { bg: "#f6ffed", label: "通过", tip: "机器判定通过" };
  }
  if (cell.review_status === "human_confirmed") return { bg: "#f6ffed", label: cell.final_score ?? "✓", tip: "已人工确认" };
  if (cell.review_status === "human_overridden") return { bg: "#e6f4ff", label: cell.final_score ?? "✓", tip: "已人工改分" };
  if (cell.review_status === "conflict_flagged") return { bg: "#fff7e6", label: "冲突", tip: "冲突待裁决" };
  if (cell.review_status === "ai_suggested")
    return { bg: "#f9f0ff", label: "AI建议", tip: "AI 已给出建议分（需人工确认；未解锁前不展示分值）" };
  if (cell.machine_status === "fail") return { bg: "#fff1f0", label: "不通过", tip: "裁决不通过" };
  if (cell.final_score != null) return { bg: "#fafafa", label: cell.final_score, tip: "确定性计算得分（待复核）" };
  return { bg: "#fff7e6", label: "待人工", tip: "无证据或无法自动判定" };
}
