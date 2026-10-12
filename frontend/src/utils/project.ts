import type { Project } from "../types";

export const PROJECT_STATUS: Record<string, { text: string; color: string }> = {
  draft: { text: "待开始", color: "default" },
  tender_parsed: { text: "招标文件已解析", color: "blue" },
  rules_draft: { text: "规则待确认", color: "orange" },
  rules_frozen: { text: "规则已冻结", color: "cyan" },
  bids_parsed: { text: "投标文件已解析", color: "blue" },
  evaluating: { text: "评分中", color: "processing" },
  reviewing: { text: "人工复核中", color: "gold" },
  completed: { text: "已完成", color: "green" },
  archived: { text: "已归档", color: "default" },
};

export function projectStatus(status: string) {
  return PROJECT_STATUS[status] ?? { text: status, color: "default" };
}

export function isActiveProject(project: Project) {
  return !["completed", "archived"].includes(project.status);
}

export function formatPrice(value: string | null, currency = "CNY") {
  if (value === null || value === "" || !Number.isFinite(Number(value)))
    return "未设置";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(Number(value));
}

export function projectTimestamp(value: string) {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function formatDate(value: string) {
  if (!projectTimestamp(value)) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}
