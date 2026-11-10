/** 运行选择的共用逻辑：自动运行展示最新进度，历史运行优先最近成功。 */

export interface RunBrief {
  id: number;
  status: string;
  automation?: unknown;
}

/**
 * 默认运行：最新一条是自动运行时直接展示，便于看见故障并续评。
 * 历史运行沿用最近的 succeeded，没有则退回最新一次。
 *
 * 历史运行的默认选择保持不变；自动运行显示已保存的部分结果与续评入口。
 */
export function pickDefaultRun<T extends RunBrief>(runs?: T[] | null): T | null {
  if (!runs || runs.length === 0) return null;
  if (runs[0].automation != null) return runs[0];
  return runs.find((r) => r.status === "succeeded") ?? runs[0];
}
