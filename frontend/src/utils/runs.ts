/** 运行选择的共用逻辑：默认取最近一次「成功」的运行。 */

export interface RunBrief {
  id: number;
  status: string;
}

/**
 * 默认运行：最近的 succeeded，没有则退回最新一次。
 *
 * 「失败的运行不顶掉成功运行的数据」——失败时矩阵/结果为空白，
 * 曾是"评分跑不动"的主要误判来源。
 */
export function pickDefaultRun<T extends RunBrief>(runs?: T[] | null): T | null {
  if (!runs || runs.length === 0) return null;
  return runs.find((r) => r.status === "succeeded") ?? runs[0];
}
