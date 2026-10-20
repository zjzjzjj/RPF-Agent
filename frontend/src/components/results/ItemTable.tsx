import { Button, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { SummaryBucket, SummaryItem } from "../../types";

interface Props {
  bucket: SummaryBucket;
  onOpenReason: (item: SummaryItem) => void;
  onOpenEvidence: (item: SummaryItem) => void;
}

const STATE_TAG: Record<string, { color: string; text: string }> = {
  scored: { color: "green", text: "已计分" },
  missing_final: { color: "orange", text: "未定分" },
  veto: { color: "geekblue", text: "否决项" },
  match: { color: "blue", text: "判断项" },
  not_run: { color: "default", text: "未执行" },
};

// 主观/客观口径配色：主观=紫（AI 评委），客观=蓝（确定性计算），判定项=青（只出结论）
const METHOD_TAG: Record<string, { color: string }> = {
  subjective: { color: "purple" },
  objective: { color: "blue" },
  verdict: { color: "cyan" },
};

function verdictTag(item: SummaryItem) {
  const t = item.verdict ?? "—";
  const color =
    item.machine_status === "recommend_reject" || item.machine_status === "fail"
      ? "red"
      : item.machine_status === "pass"
        ? "green"
        : "orange";
  return <Tag color={color}>{t}</Tag>;
}

function scoreCell(item: SummaryItem) {
  if (item.state === "veto" || item.state === "match") return verdictTag(item);
  if (item.state === "not_run") return <Typography.Text type="secondary">未执行</Typography.Text>;
  if (item.state === "missing_final") return <Tag color="orange">未定分</Tag>;
  return <b>{item.final}</b>;
}

function reasonSummary(item: SummaryItem): string {
  if (item.ai_locked && !item.reason.human_reason && !item.reason.machine_reason) {
    return "AI 理由未解锁（防锚定）";
  }
  const text =
    item.reason.ai_reasoning ?? item.reason.machine_reason ?? item.reason.human_reason ?? "";
  const flat = text.replace(/\s+/g, " ").trim();
  if (!flat) return "—";
  return flat.length > 60 ? `${flat.slice(0, 60)}…` : flat;
}

/** 单个分桶的逐项明细表：得分 / 满分 / 失分 / 状态 / 理由 / 证据。 */
export default function ItemTable({ bucket, onOpenReason, onOpenEvidence }: Props) {
  const isVerdictBucket = bucket.kind === "verdict";

  return (
    <Table<SummaryItem>
      rowKey="criterion_id"
      size="small"
      pagination={false}
      dataSource={bucket.items}
      // 列宽合计约 1194：容器更窄时表格内部横向滚动，
      // 否则「理由」这种弹性列会被挤成 1 字符宽，文字逐字竖排。
      scroll={{ x: 1194 }}
      rowClassName={(item) => (item.state === "not_run" ? "row-not-run" : "")}
      onRow={(item) => ({
        style:
          item.state === "missing_final"
            ? { borderLeft: "3px solid #fa8c16" }
            : item.state === "not_run"
              ? { opacity: 0.55 }
              : undefined,
      })}
      columns={[
        { title: "编号", dataIndex: "code", width: 76, fixed: "left" as const },
        { title: "评分项", dataIndex: "name", width: 190, ellipsis: true },
        {
          title: "评分方式",
          width: 104,
          render: (_, item) => {
            const m = METHOD_TAG[item.side] ?? { color: "default" };
            return <Tag color={m.color}>{item.scoring_method}</Tag>;
          },
        },
        {
          title: "满分",
          width: 64,
          align: "right" as const,
          render: (_, item) => (isVerdictBucket ? "—" : (item.max_score ?? "—")),
        },
        {
          title: "得分",
          width: 96,
          align: "right" as const,
          render: (_, item) => scoreCell(item),
        },
        {
          title: "失分",
          width: 72,
          align: "right" as const,
          render: (_, item) => {
            if (isVerdictBucket || item.deduction.kind !== "points") return "—";
            const p = Number(item.deduction.points);
            if (!Number.isFinite(p) || p === 0) return <Typography.Text type="secondary">0</Typography.Text>;
            return (
              <Tooltip title={item.deduction.formula}>
                <span style={{ color: "#cf1322", fontWeight: 600 }}>
                  {p > 0 ? `-${item.deduction.points}` : `+${Math.abs(p)}`}
                </span>
              </Tooltip>
            );
          },
        },
        {
          title: "状态",
          width: 92,
          render: (_, item) => {
            const t = STATE_TAG[item.state] ?? { color: "default", text: item.state };
            return <Tag color={t.color}>{t.text}</Tag>;
          },
        },
        {
          title: "理由",
          width: 280,
          render: (_, item) => {
            const summary = reasonSummary(item);
            const full =
              item.reason.ai_reasoning ?? item.reason.machine_reason ?? item.reason.human_reason ?? "";
            return (
              <Typography.Text
                type={item.ai_locked ? "warning" : undefined}
                title={full || summary}
                style={{
                  display: "block",
                  maxWidth: 260,
                  overflow: "hidden",
                  whiteSpace: "nowrap",
                  textOverflow: "ellipsis",
                  fontSize: 12,
                }}
              >
                {summary}
              </Typography.Text>
            );
          },
        },
        {
          title: "证据",
          width: 110,
          render: (_, item) => {
            if (item.evidence.total_count === 0) {
              return (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {item.score_type === "subjective" ? "无引用" : "确定性项"}
                </Typography.Text>
              );
            }
            return (
              <Tag color={item.evidence.status === "verified" ? "green" : "orange"}>
                {item.evidence.verified_count}/{item.evidence.total_count} 已核验
              </Tag>
            );
          },
        },
        {
          title: "操作",
          width: 110,
          fixed: "right" as const,
          render: (_, item) => (
            <Space size={0}>
              <Button size="small" type="link" onClick={() => onOpenReason(item)}>
                理由
              </Button>
              {item.score_record_id != null && (
                <Button size="small" type="link" onClick={() => onOpenEvidence(item)}>
                  核验
                </Button>
              )}
            </Space>
          ),
        },
      ]}
    />
  );
}
