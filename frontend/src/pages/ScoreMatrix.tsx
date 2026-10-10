import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Alert, Button, Card, Empty, Select, Space, Table, Tag, Tooltip, Typography, message } from "antd";
import { PlayCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useBidders, useJob, useMatrix, useModels, useRules, useRuns } from "../api/hooks";
import type { MatrixCell } from "../types";
import { CATEGORY_LABELS, REVIEW_STATUS_LABELS } from "../types";

const CATEGORY_ORDER = ["qualification", "compliance", "price", "technical", "commercial"];

function cellStyle(cell: MatrixCell | undefined): { bg: string; label: string; tip: string } {
  if (!cell) return { bg: "#f0f0f0", label: "—", tip: "未评分" };
  if (cell.score_type === "veto") {
    if (cell.review_status === "human_confirmed" && cell.machine_status === "pass")
      return { bg: "#f6ffed", label: "通过", tip: "已人工确认通过" };
    if (cell.machine_status === "recommend_reject")
      return { bg: "#fff1f0", label: "建议否决", tip: "AI 建议否决，等待人工确认（绝不自动否决）" };
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
  if (cell.final_score != null) return { bg: "#fafafa", label: cell.final_score, tip: "确定性计算得分（待复核）" };
  return { bg: "#fff7e6", label: "待人工", tip: "无证据或无法自动判定" };
}

export default function ScoreMatrix() {
  const { pid = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { data: runs } = useRuns(pid);
  const { data: bidders } = useBidders(pid);
  const { data: rules } = useRules(pid);
  const [jobId, setJobId] = useState<number | null>(null);
  const { data: job } = useJob(jobId);

  const latestRun = runs?.[0];
  const { data: matrix, isLoading } = useMatrix(latestRun?.id ?? null);
  const { data: models } = useModels();
  const [selectedModel, setSelectedModel] = useState<string | undefined>(undefined);

  const startRun = useMutation({
    mutationFn: () =>
      api.post<{ run_id: number; job_id: number }>(`/projects/${pid}/runs`, {
        package_id: rules?.id,
        bidder_ids: (bidders ?? []).map((b) => b.id),
        stages: ["objective", "price", "subjective"],
        model: selectedModel ?? null,
      }),
    onSuccess: (r) => {
      setJobId(r.job_id);
      qc.invalidateQueries({ queryKey: ["runs", pid] });
      message.success("评分运行已发起");
    },
    onError: (e: Error) => message.error(e.message),
  });

  const columns = useMemo(() => {
    if (!matrix) return [];
    const grouped = CATEGORY_ORDER.filter((c) => matrix.criteria.some((cr) => cr.category === c));
    return [
      {
        title: "投标人",
        dataIndex: "name",
        fixed: "left" as const,
        width: 180,
        render: (name: string, row: { bidder_id: number }) => (
          <Space direction="vertical" size={0}>
            <b>{name}</b>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              #{row.bidder_id}
            </Typography.Text>
          </Space>
        ),
      },
      ...grouped.flatMap((cat) =>
        matrix.criteria
          .filter((c) => c.category === cat)
          .map((c, idx) => ({
            title:
              idx === 0 ? (
                <div>
                  <div>{CATEGORY_LABELS[cat] ?? cat}</div>
                  <div style={{ fontSize: 11, fontWeight: 400 }}>{c.name}</div>
                </div>
              ) : (
                <span style={{ fontSize: 11, fontWeight: 400 }}>{c.name}</span>
              ),
            key: String(c.criterion_id),
            width: cat === "qualification" || cat === "compliance" ? 92 : 100,
            render: (_: unknown, row: { bidder_id: number }) => {
              const cell = matrix.cells[String(row.bidder_id)]?.[String(c.criterion_id)];
              const s = cellStyle(cell);
              return (
                <Tooltip title={cell ? `${s.tip}（${REVIEW_STATUS_LABELS[cell.review_status] ?? cell.review_status}）` : s.tip}>
                  <div
                    onClick={() => cell && navigate(`/p/${pid}/evidence/${cell.score_record_id}`)}
                    style={{
                      background: s.bg,
                      border: "1px solid #e5e5e5",
                      borderRadius: 4,
                      padding: "6px 4px",
                      textAlign: "center",
                      cursor: cell ? "pointer" : "default",
                      fontSize: 13,
                    }}
                  >
                    {s.label}
                    {cell?.evidence_status === "verified" && (
                      <div style={{ fontSize: 10, color: "#389e0d" }}>✓ 有据</div>
                    )}
                    {cell && cell.evidence_status !== "verified" && cell.score_type !== "veto" && (
                      <div style={{ fontSize: 10, color: "#d46b08" }}>
                        {cell.evidence_status === "unverified" ? "证据待核" : ""}
                      </div>
                    )}
                  </div>
                </Tooltip>
              );
            },
          }))
      ),
    ];
  }, [matrix, pid, navigate]);

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          评分矩阵
        </Typography.Title>
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          disabled={rules?.status !== "frozen" || !bidders?.length}
          loading={startRun.isPending}
          onClick={() => startRun.mutate()}
        >
          发起评分运行
        </Button>
        <Select
          style={{ minWidth: 200 }}
          placeholder="主观分模型"
          value={selectedModel ?? models?.default_model}
          onChange={setSelectedModel}
          options={(models?.models ?? []).map((m) => ({
            value: m.id,
            label: `${m.label}（${m.family}）`,
          }))}
        />
        <Button icon={<ReloadOutlined />} onClick={() => qc.invalidateQueries({ queryKey: ["matrix"] })}>
          刷新
        </Button>
        {latestRun && <Tag color="blue">运行 #{latestRun.id}（{latestRun.status}）</Tag>}
        {job && (
          <Tag color={job.status === "failed" ? "red" : job.status === "succeeded" ? "green" : "blue"}>
            {job.status} {job.message}
          </Tag>
        )}
      </Space>

      {rules?.status !== "frozen" && (
        <Alert type="warning" showIcon message="规则包尚未冻结：请先在“招标文件与规则”页完成确认与冻结" />
      )}

      <Card>
        {isLoading ? (
          <Card loading />
        ) : !matrix ? (
          <Empty description="尚无评分运行结果" />
        ) : (
          <>
            <Table
              rowKey="bidder_id"
              dataSource={matrix.bidders}
              columns={columns as never}
              pagination={false}
              size="small"
              scroll={{ x: "max-content" }}
            />
            <div style={{ marginTop: 12 }}>
              <Space size="large" wrap>
                <span>
                  <Tag color="green">已确认</Tag> 人工确认的分数
                </span>
                <span>
                  <Tag color="purple">AI建议</Tag> 待人工确认（未解锁不显示分值）
                </span>
                <span>
                  <Tag color="orange">待人工/冲突</Tag> 需人工裁决
                </span>
                <span>
                  <Tag color="red">建议否决</Tag> 绝不自动否决，须人工确认
                </span>
              </Space>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
