/** 任务进度统一展示：进度条 + 状态标签 + 阶段文案 + 可选取消。
 *
 * 数据源是 JobOut 的 done_steps/total_steps/message（后端 JobContext 已产出）。
 * 用于上传解析、报价抽取、评分运行、规则编译等全部长耗时任务。
 */
import { Button, Progress, Space, Tag, Typography } from "antd";
import type { JobOut } from "../types";

const RUNNING = ["queued", "running"];

export function JobProgress({
  job,
  onCancel,
  width = 120,
}: {
  job: JobOut | null;
  onCancel?: () => void;
  width?: number;
}) {
  if (!job) return null;
  const total = job.total_steps ?? 0;
  const done = job.done_steps ?? 0;
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const status =
    job.status === "failed" ? "exception" : job.status === "succeeded" ? "success" : "active";
  const color =
    job.status === "failed" ? "red" : job.status === "succeeded" ? "green" : "blue";

  return (
    <Space size={6} wrap>
      <Progress percent={percent} size="small" status={status} style={{ width }} />
      <Tag color={color} style={{ marginInlineEnd: 0 }}>
        {job.status}
      </Tag>
      {job.message && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {job.message}
        </Typography.Text>
      )}
      {job.error && (
        <Typography.Text type="danger" style={{ fontSize: 12 }}>
          {job.error}
        </Typography.Text>
      )}
      {onCancel && RUNNING.includes(job.status) && (
        <Button size="small" type="link" onClick={onCancel}>
          取消
        </Button>
      )}
    </Space>
  );
}

export default JobProgress;
