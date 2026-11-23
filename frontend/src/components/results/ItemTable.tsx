import { Button, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { TableProps } from "antd";
import type { SummaryBucket, SummaryItem } from "../../types";
import { automaticEvidenceCellLabel, evidenceCellLabel } from "../../utils/evidenceText";

interface Props {
  bucket: SummaryBucket;
  onOpenReason: (item: SummaryItem) => void;
  onOpenEvidence: (item: SummaryItem) => void;
  onViewSource: (item: SummaryItem) => void;
  managed?: boolean;
}

const STATE_TAG: Record<string, { color: string; text: string }> = {
  scored: { color: "green", text: "已计分" },
  missing_final: { color: "orange", text: "未定分" },
  veto: { color: "geekblue", text: "否决项" },
  match: { color: "blue", text: "判断项" },
  not_run: { color: "default", text: "未执行" },
};

// 二轴制配色：方法轴 AI评审=紫/规则评审=蓝/混合=青；口径轴 主观=品红/客观=绿/—=灰
const METHOD_COLOR: Record<string, string> = {
  "AI评审": "purple",
  "规则评审": "blue",
  "混合（AI核验事实+规则计数）": "cyan",
};
const BASIS_COLOR: Record<string, string> = {
  "主观": "magenta",
  "客观": "green",
  "—": "default",
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

function scoreCell(item: SummaryItem, managed: boolean) {
  if (managed && item.machine_status === "execution_failed") return <Tag color="red">执行失败</Tag>;
  if (item.state === "veto" || item.state === "match") return verdictTag(item);
  if (item.state === "not_run") return <Typography.Text type="secondary">未执行</Typography.Text>;
  if (item.state === "missing_final") return <Tag color="orange">未定分</Tag>;
  return <b>{item.final}</b>;
}

function reasonSummary(item: SummaryItem, managed: boolean): string {
  if (!managed && item.ai_locked && !item.reason.human_reason && !item.reason.machine_reason) {
    return "AI 理由未解锁（防锚定）";
  }
  const text =
    item.reason.ai_reasoning ?? item.reason.machine_reason ??
    (managed ? item.auto_review?.reasoning ?? item.auto_review?.error : item.reason.human_reason) ?? "";
  const flat = text.replace(/\s+/g, " ").trim();
  if (!flat) return "—";
  return flat.length > 60 ? `${flat.slice(0, 60)}…` : flat;
}

/** 单个分桶的逐项明细表：得分 / 满分 / 失分 / 状态 / 理由 / 证据。 */
export default function ItemTable({ bucket, onOpenReason, onOpenEvidence, onViewSource, managed = false }: Props) {
  const isVerdictBucket = bucket.kind === "verdict";
  const maxScoreColumn: NonNullable<TableProps<SummaryItem>["columns"]>[number] = {
    title: "满分",
    width: 64,
    align: "right" as const,
    render: (_, item) => item.max_score ?? "—",
  };

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
          title: "评审方式",
          width: 168,
          render: (_, item) => (
            <>
              <Tag color={METHOD_COLOR[item.scoring_method] ?? "default"}>{item.scoring_method}</Tag>
              {item.scoring_basis !== "—" && (
                <Tag color={BASIS_COLOR[item.scoring_basis] ?? "default"}>{item.scoring_basis}</Tag>
              )}
            </>
          ),
        },
        // 资格/符合性细项不展示"满分"列（判定项无分值，2026-11-23 用户拍板）
        ...(isVerdictBucket ? [] : [maxScoreColumn]),
        {
          title: "得分",
          width: 96,
          align: "right" as const,
          render: (_, item) => scoreCell(item, managed),
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
            if (managed && item.machine_status === "execution_failed") return <Tag color="red">执行失败</Tag>;
            if (managed && item.review_status === "auto_confirmed") return <Tag color="green">已评审</Tag>;
            if (managed) return <Tag color="blue">待自动完成</Tag>;
            const t = STATE_TAG[item.state] ?? { color: "default", text: item.state };
            return <Tag color={t.color}>{t.text}</Tag>;
          },
        },
        {
          title: "理由",
          width: 280,
          render: (_, item) => {
            const summary = reasonSummary(item, managed);
            const full =
              item.reason.ai_reasoning ?? item.reason.machine_reason ?? item.reason.human_reason ?? "";
            return (
              <Typography.Text
                type={!managed && item.ai_locked ? "warning" : undefined}
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
            if (managed && item.auto_review?.visual_evidence?.length) {
              return <Tag color="green">原页视觉核验</Tag>;
            }
            if (managed && item.auto_review?.basis === "external") {
              return <Tag color="gold">外部证据边界</Tag>;
            }
            if (managed && item.auto_review?.confirmed_absent) {
              return <Tag>已确认缺失</Tag>;
            }
            if (item.evidence.total_count === 0) {
              return (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {managed ? automaticEvidenceCellLabel(item.auto_review) : evidenceCellLabel(item.side, item.verdict_source)}
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
          width: 150,
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
              {/* 价格分（客观-公式）不需要原文定位；其余评分项有引文时显示「查看原文」 */}
              {item.score_type !== "formula" &&
                item.category !== "price" &&
                item.evidence.citations.length > 0 && (
                  <Button size="small" type="link" onClick={() => onViewSource(item)}>
                    查看原文
                  </Button>
                )}
            </Space>
          ),
        },
      ]}
    />
  );
}
