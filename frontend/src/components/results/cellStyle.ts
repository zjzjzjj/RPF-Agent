import type { MatrixCell } from "../../types";

/** 矩阵单元格配色与文案的唯一映射（评分结果页的「完整矩阵」标签页使用）。 */
export function cellStyle(cell: MatrixCell | undefined, managed = false): { bg: string; label: string; tip: string } {
  if (!cell) return { bg: "#f0f0f0", label: "—", tip: "未评分" };
  if (managed && cell.auto_review_basis === "lightweight") {
    const tip = "资格轻量预检 + 每家一次快审，不构成完整资格审查；不阻断计分、排名与价格基准价";
    if (cell.machine_status === "fail")
      return { bg: "#fff1f0", label: "预检不符合", tip };
    if (cell.machine_status === "concern")
      return { bg: "#fff7e6", label: "预检存疑", tip };
    if (cell.machine_status === "not_deeply_reviewed")
      return { bg: "#fafafa", label: "未深评", tip };
    return { bg: "#f6ffed", label: "预检通过", tip };
  }
  if (managed && cell.auto_review_basis === "fallback") {
    if (cell.score_type === "match" || cell.machine_status === "not_deeply_reviewed")
      return { bg: "#fafafa", label: "不适用", tip: "汇总或判断条目，分值已在对应计分项计算，不重复计分" };
    return {
      bg: "#fff7e6",
      label: cell.final_score ?? "暂定",
      tip: "模型或证据路径未完成，按暂定分兜底；已在报告中列入待复核清单",
    };
  }
  if (managed && cell.machine_status === "execution_failed")
    return { bg: "#fff1f0", label: "执行失败", tip: "自动评审遇到技术故障，已保留进度，可恢复后续评" };
  // 全自动模式终态优先：已自动裁决（LLM 带原文证据或确定性判定），不再有待人工中间态
  if (cell.review_status === "auto_confirmed") {
    if (cell.machine_status === "unverifiable")
      return { bg: "#fffbe6", label: "无法证实", tip: "已完成评审：缺少外部事实记录，现有材料无法证实；不影响参评" };
    if (cell.machine_status === "recommend_reject")
      return { bg: "#fff1f0", label: "已否决", tip: "全自动模式终态：LLM/确定性裁决否决（留痕可查）" };
    if (cell.machine_status === "fail")
      return { bg: "#fff1f0", label: "不通过", tip: "全自动模式终态：裁决不通过（留痕可查）" };
    return { bg: "#e6f4ff", label: cell.final_score ?? "已裁决", tip: "已自动裁决（LLM 带原文证据，留痕可查）" };
  }
  if (managed) {
    if (cell.review_status === "not_applicable")
      return { bg: "#fafafa", label: "不适用", tip: "系统已确认本条款不适用" };
    return { bg: "#e6f4ff", label: "自动评审中", tip: "正在自动补检证据或复评，尚未形成最终结论" };
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
