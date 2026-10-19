import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Empty, Space, Table, Tag, Tooltip, Typography } from "antd";
import { cellStyle } from "./cellStyle";
import type { Matrix, MatrixCell } from "../../types";
import { CATEGORY_LABELS, CATEGORY_ORDER, REVIEW_STATUS_LABELS } from "../../types";

interface Props {
  pid: string;
  matrix: Matrix | undefined;
  isLoading: boolean;
}

/** 完整评分矩阵（投标人 × 评分项）。点单元格进证据核验页。 */
export default function MatrixPanel({ pid, matrix, isLoading }: Props) {
  const navigate = useNavigate();

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
                <Tooltip
                  title={
                    cell
                      ? `${s.tip}（${REVIEW_STATUS_LABELS[cell.review_status] ?? cell.review_status}）`
                      : s.tip
                  }
                >
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

  if (isLoading) return <Card loading />;
  if (!matrix) return <Card><Empty description="本次运行尚无评分矩阵" /></Card>;

  return (
    <Card>
      <Table<{ bidder_id: number; name: string; status: string }>
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
            <Tag color="geekblue">已自动裁决</Tag> 全自动模式终态（LLM 带原文证据，留痕可查）
          </span>
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
            <Tag color="red">已否决/不通过</Tag> 裁决终态（自动或人工确认）
          </span>
        </Space>
      </div>
    </Card>
  );
}

export type { MatrixCell };
