/** 任务进度统一展示：进度条 + 状态标签 + 阶段文案 + 可选取消。
 *
 * 数据源是 JobOut 的 done_steps/total_steps/message（后端 JobContext 已产出）。
 * 用于上传解析、报价抽取、评分运行、规则编译等全部长耗时任务。
 */
import { Button, Collapse, Progress, Space, Tag, Typography } from "antd";
import type { ExecutionProgress, JobOut } from "../types";

const RUNNING = ["queued", "running"];

export function JobProgress({
  job,
  onCancel,
  width = 120,
  executionProgress,
}: {
  job: JobOut | null;
  onCancel?: () => void;
  width?: number;
  executionProgress?: ExecutionProgress | null;
}) {
  if (!job) return null;
  const total = job.total_steps ?? 0;
  const done = job.done_steps ?? 0;
  const queued = job.status === "queued";
  const percent = queued ? 0 : total > 0 ? Math.min(100, Math.round((done / total) * 1000) / 10) : 0;
  const status =
    job.status === "failed" ? "exception" : job.status === "succeeded" ? "success" : "active";
  const color =
    job.status === "failed" ? "red" : job.status === "succeeded" ? "green" : "blue";
  const errorSummary = job.error?.split(/\r?\n/, 1)[0];
  const hasErrorDetails = Boolean(job.error && job.error !== errorSummary);

  return (
    <Space size={6} wrap>
      <Progress percent={percent} size="small" status={status} style={{ width }} />
      <Tag color={color} style={{ marginInlineEnd: 0 }}>
        {job.status}
      </Tag>
      {queued && (
        <Typography.Text type="warning" style={{ fontSize: 12 }}>
          排队等待中
          {job.queue_position != null && job.queue_position > 0
            ? `（前面还有 ${job.queue_position} 个任务，单 worker 串行执行）`
            : "（即将开始）"}
        </Typography.Text>
      )}
      {job.message && RUNNING.includes(job.status) && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {job.message}
        </Typography.Text>
      )}
      {executionProgress && <ExecutionProgressDetails progress={executionProgress} />}
      {job.error && (
        <div style={{ width: "100%", maxWidth: "100%" }}>
          <Typography.Text type="danger" style={{ fontSize: 12, overflowWrap: "anywhere" }}>
            {errorSummary}
          </Typography.Text>
          {hasErrorDetails && (
            <Collapse
              ghost
              size="small"
              items={[{
                key: "diagnostics",
                label: "查看技术详情",
                children: (
                  <pre style={{ margin: 0, maxHeight: 240, overflow: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 12 }}>
                    {job.error}
                  </pre>
                ),
              }]}
            />
          )}
        </div>
      )}
      {onCancel && RUNNING.includes(job.status) && (
        <Button size="small" type="link" onClick={onCancel}>
          取消
        </Button>
      )}
    </Space>
  );
}

export function ExecutionProgressDetails({ progress }: { progress: ExecutionProgress | null | undefined }) {
  if (!progress) return null;
  const cacheHits = (progress.metrics.cache_hits ?? 0) + (progress.metrics.reused_batches ?? 0);
  const terminal = ["completed", "partial", "failed", "cancelled"].includes(progress.phase);
  const phaseLabel: Record<string, string> = {
    preparing: "准备自动评审", collecting: "读取材料", synthesizing: "综合评分",
    reviewing: "评审执行中",
    completed: "自动评审完成", partial: "评审已结束（含跳过项）", failed: "自动评审故障", cancelled: "自动评审已取消",
  };
  return (
    <Space direction="vertical" size={2}>
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
      {terminal && progress.batches_total > 0 ? `${phaseLabel[progress.phase]} · ` : ""}
      {progress.batches_total > 0
        ? `材料采集 ${progress.batches_completed}/${progress.batches_total} 批`
        : phaseLabel[progress.phase] ?? "自动评审"}
      {(progress.read_pages_total ?? 0) > 0
        ? ` · 已读 ${progress.read_pages_completed ?? 0}/${progress.read_pages_total} 页`
        : progress.read_units_total > 0 ? ` · 读取 ${progress.read_units_completed}/${progress.read_units_total} 个材料单元` : ""}
      {!terminal && progress.batches_running > 0 ? ` · 正在处理 ${progress.batches_running} 批` : ""}
      {!terminal && (progress.batches_pending ?? 0) > 0 ? ` · 待处理 ${progress.batches_pending} 批` : ""}
      {!terminal && (progress.requests_queued ?? 0) > 0 ? ` · 模型排队 ${progress.requests_queued} 次` : ""}
      {!terminal && (progress.requests_inflight ?? 0) > 0 ? ` · 模型请求中 ${progress.requests_inflight} 次` : ""}
      {progress.phase === "synthesizing" ? " · 正在综合评分" : ""}
      {cacheHits > 0 ? ` · 已复用 ${cacheHits} 次` : ""}
      {progress.batches_failed > 0 ? ` · 采集故障 ${progress.batches_failed} 批` : ""}
      {progress.last_progress_at ? ` · 最近完成 ${new Date(progress.last_progress_at).toLocaleTimeString()}` : ""}
    </Typography.Text>
    {(progress.failures ?? []).slice(0, 3).map((failure) => (
      <Typography.Text key={`${failure.collection_id}:${failure.batch_key}`} type="danger" style={{ fontSize: 12 }}>
        {failure.batch_index != null ? `第 ${failure.batch_index + 1} 批` : "材料采集"}
        {failure.documents.map((document) => ` · ${document.filename ?? `文件 ${document.document_id}`} 第 ${document.page_numbers.join("、")} 页`).join("")}
        {`：${failure.message}`}
      </Typography.Text>
    ))}
    </Space>
  );
}

export default JobProgress;
