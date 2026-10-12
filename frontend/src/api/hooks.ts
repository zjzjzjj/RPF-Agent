import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  Bidder,
  Citation,
  DocumentOut,
  JobOut,
  Matrix,
  ModelsOut,
  PackageDetail,
  Project,
  Ranking,
  ReportOut,
  ReviewQueueItem,
  ScoreOut,
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

export function useRanking(pid: string) {
  return useQuery({
    queryKey: ["ranking", pid],
    queryFn: () => api.get<Ranking>(`/projects/${pid}/ranking`),
    enabled: !!pid,
  });
}

/** 评标报告（草稿/定稿）。有评分运行即可取——草稿把疑问逐条标出。 */
export function useReport(pid: string) {
  return useQuery({
    queryKey: ["report", pid],
    queryFn: () => api.get<ReportOut>(`/projects/${pid}/report`),
    enabled: !!pid,
  });
}

/** 变更操作的公共封装：成功后失效相关查询。 */
export function useInvalidate() {
  const qc = useQueryClient();
  return (keys: string[]) => keys.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
}

export { useMutation, useQueryClient };
