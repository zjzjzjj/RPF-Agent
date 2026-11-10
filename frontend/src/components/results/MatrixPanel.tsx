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
  managed?: boolean;
}

/** 完整评分矩阵（投标人 × 评分项）。点单元格进证据核验页。 */
export default function MatrixPanel({ pid, matrix, isLoading, managed = false }: Props) {
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
              const s = cellStyle(cell, managed);
              return (
                <Tooltip
                  title={
                    cell
                      ? managed ? s.tip : `${s.tip}（${REVIEW_STATUS_LABELS[cell.review_status] ?? cell.review_status}）`
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
                    {cell?.evidence_status === "unverified" && (
                      <div style={{ fontSize: 10, color: "#d46b08" }}>证据待核</div>
                    )}
                    {cell?.evidence_status === "none" && cell.score_type === "subjective" && (
                      <div style={{ fontSize: 10, color: "#cf1322" }}>无证据</div>
                    )}
                  </div>
                </Tooltip>
              );
            },
          }))
      ),
    ];
  }, [matrix, pid, navigate, managed]);

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
          {!managed && <span>
            <Tag color="green">已确认</Tag> 人工确认的分数
          </span>}
          {!managed && <span>
            <Tag color="purple">AI建议</Tag> 待人工确认（未解锁不显示分值）
          </span>}
          {!managed && <span>
            <Tag color="orange">待人工/冲突</Tag> 需人工裁决
          </span>}
          {managed && <span><Tag color="blue">自动评审中</Tag> 自动补检与复评</span>}
          {managed && <span><Tag color="red">执行失败</Tag> 保留进度，恢复后续评</span>}
          {managed && <span><Tag color="gold">无法证实</Tag> 已评审的外部事实风险结论</span>}
          <span>
            <Tag color="red">{managed ? "不通过" : "已否决/不通过"}</Tag> {managed ? "最终结论，投标人仍参与计分和排名" : "裁决终态（自动或人工确认）"}
          </span>
          <span>
            <Tag color="green">✓ 有据</Tag>
            <Tag color="orange">证据待核</Tag>
            <Tag color="red">无证据</Tag>
            {managed ? "格内证据角注：证据由系统自动核验；确定性项的计算过程可复算" : "格内证据角注：红＝主观项缺引文需人工处置；确定性项（判定/计算）无引文属设计"}
          </span>
        </Space>
      </div>
    </Card>
  );
}

export type { MatrixCell };
