import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { pickDefaultRun } from "../utils/runs";
import type {
  Bidder,
  Citation,
  Coverage,
  DocumentOut,
  JobOut,
  LawPackSummary,
  MatchProposal,
  Matrix,
  ModelsOut,
  PackageDetail,
  Project,
  Ranking,
  ReportOut,
  ReviewQueueItem,
  ScoreOut,
  ScoreSummary,
  TemplateDetail,
  TemplateSummary,
  Workbench,
} from "../types";

export function useModels() {
  return useQuery({
    queryKey: ["models"],
    queryFn: () => api.get<ModelsOut>("/models"),
    staleTime: 3600_000,
  });
}

export function useProjects() {
  return useQuery({ queryKey: ["projects"], queryFn: () => api.get<Project[]>("/projects") });
}

export function useProject(pid: string) {
  return useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.get<Project>(`/projects/${pid}`),
    enabled: !!pid,
  });
}

export function useWorkbench(pid: string) {
  return useQuery({
    queryKey: ["workbench", pid],
    queryFn: () => api.get<Workbench>(`/projects/${pid}/workbench`),
    enabled: !!pid,
  });
}

export function useJob(jobId: number | null) {
  return useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.get<JobOut>(`/jobs/${jobId}`),
    enabled: jobId != null,
    refetchInterval: (query) => {
      const data = query.state.data as JobOut | undefined;
      return data && ["succeeded", "failed", "cancelled"].includes(data.status) ? false : 800;
    },
  });
}

export function useDocuments(pid: string) {
  return useQuery({
    queryKey: ["documents", pid],
    queryFn: () => api.get<DocumentOut[]>(`/projects/${pid}/documents`),
    enabled: !!pid,
    refetchInterval: 2000,
  });
}

export function useRules(pid: string) {
  return useQuery({
    queryKey: ["rules", pid],
    queryFn: () => api.get<PackageDetail>(`/projects/${pid}/rules`),
    enabled: !!pid,
  });
}

export function useBidders(pid: string) {
  return useQuery({
    queryKey: ["bidders", pid],
    queryFn: () => api.get<Bidder[]>(`/projects/${pid}/bidders`),
    enabled: !!pid,
    refetchInterval: 3000,
  });
}

export function useRuns(pid: string) {
  return useQuery({
    queryKey: ["runs", pid],
    queryFn: () => api.get<{ id: number; status: string; package_version: number }[]>(`/projects/${pid}/runs`),
    enabled: !!pid,
    refetchInterval: 1500,
  });
}

/**
 * 统一的「当前运行」选择：`?run=` 持久化，缺省取最近一次成功运行。
 *
 * 评分结果页的矩阵 / 聚合 / 排名 / 报告必须共用同一个 runId——否则会出现
 * 「矩阵是上一次成功的运行、排名是最新那次失败的运行」这类串台。
 */
export function useSelectedRun(pid: string) {
  const { data: runs } = useRuns(pid);
  const [params, setParams] = useSearchParams();
  const raw = params.get("run");
  const parsed = raw != null && raw !== "" ? Number(raw) : null;
  const fallback = pickDefaultRun(runs);
  const runId = parsed != null && Number.isFinite(parsed) ? parsed : (fallback?.id ?? null);
  const setRunId = (id: number | null) => {
    const next = new URLSearchParams(params);
    if (id == null) next.delete("run");
    else next.set("run", String(id));
    setParams(next, { replace: true });
  };
  return {
    runs: runs ?? [],
    runId,
    setRunId,
    run: (runs ?? []).find((r) => r.id === runId) ?? null,
  };
}

/** 评分结果聚合（排名 + 四分桶 + 逐项加减分明细与理由）。 */
export function useScoreSummary(runId: number | null, pid: string) {
  return useQuery({
    queryKey: ["scoreSummary", runId],
    queryFn: () =>
      api.get<ScoreSummary>(`/runs/${runId}/score-summary?project_id=${encodeURIComponent(pid)}`),
    enabled: runId != null && !!pid,
  });
}

export function useMatrix(runId: number | null) {
  return useQuery({
    queryKey: ["matrix", runId],
    queryFn: () => api.get<Matrix>(`/runs/${runId}/matrix`),
    enabled: runId != null,
  });
}

export function useScore(scoreId: number | null) {
  return useQuery({
    queryKey: ["score", scoreId],
    queryFn: () => api.get<ScoreOut>(`/scores/${scoreId}`),
    enabled: scoreId != null,
  });
}

export function useScoreEvidence(scoreId: number | null) {
  return useQuery({
    queryKey: ["evidence", scoreId],
    queryFn: () => api.get<Citation[]>(`/scores/${scoreId}/evidence`),
    enabled: scoreId != null,
  });
}

export function useReviewQueue(pid: string) {
  return useQuery({
    queryKey: ["reviewQueue", pid],
    queryFn: () => api.get<{ run_id: number | null; items: ReviewQueueItem[] }>(`/projects/${pid}/review-queue`),
    enabled: !!pid,
  });
}

export function useRanking(pid: string, runId?: number | null) {
  return useQuery({
    queryKey: ["ranking", pid, runId ?? null],
    queryFn: () =>
      api.get<Ranking>(`/projects/${pid}/ranking${runId != null ? `?run_id=${runId}` : ""}`),
    enabled: !!pid,
  });
}

/** 评标报告（草稿/定稿）。有评分运行即可取——草稿把疑问逐条标出。 */
export function useReport(pid: string, runId?: number | null) {
  return useQuery({
    queryKey: ["report", pid, runId ?? null],
    queryFn: () =>
      api.get<ReportOut>(`/projects/${pid}/report${runId != null ? `?run_id=${runId}` : ""}`),
    enabled: !!pid,
    retry: false, // 无运行时后端 404，不适合重试
  });
}

/** 通用评分规则库模板目录（S0） */
export function useTemplates() {
  return useQuery({
    queryKey: ["templates"],
    queryFn: () => api.get<TemplateSummary[]>("/rules/templates"),
    staleTime: 3600_000,
  });
}

export function useTemplate(templateId: string | null) {
  return useQuery({
    queryKey: ["template", templateId],
    queryFn: () => api.get<TemplateDetail>(`/rules/templates/${templateId}`),
    enabled: !!templateId,
    staleTime: 3600_000,
  });
}

/** 仓库内固化的法规规则包目录（导入选择用） */
export function useLawPacks() {
  return useQuery({
    queryKey: ["lawPacks"],
    queryFn: () => api.get<LawPackSummary[]>("/rules/law-packs"),
    staleTime: 3600_000,
  });
}

/** 覆盖率报告（S0） */
export function useCoverage(packageId: number | null) {
  return useQuery({
    queryKey: ["coverage", packageId],
    queryFn: () => api.get<Coverage>(`/rules/packages/${packageId}/coverage`),
    enabled: packageId != null && packageId > 0,
  });
}

/** 匹配提案（S1） */
export function useMatchProposal(mergePackageId: number | null) {
  return useQuery({
    queryKey: ["matchProposal", mergePackageId],
    queryFn: () => api.get<MatchProposal>(`/rules/packages/${mergePackageId}/match-proposal`),
    enabled: mergePackageId != null && mergePackageId > 0,
  });
}

/** 变更操作的公共封装：成功后失效相关查询。 */
export function useInvalidate() {
  const qc = useQueryClient();
  return (keys: string[]) => keys.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
}

export { useMutation, useQueryClient };
