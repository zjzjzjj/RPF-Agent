import { useParams } from "react-router-dom";
import { Alert, Button, Card, Col, Descriptions, Row, Space, Table, Tag, Typography } from "antd";
import { FileTextOutlined, ReloadOutlined } from "@ant-design/icons";
import { useQueryClient } from "@tanstack/react-query";
import { reportUrl } from "../api/client";
import { useRanking } from "../api/hooks";
import type { RankingRow } from "../types";

export default function RankingReport() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const { data, isLoading } = useRanking(pid);

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          排名与评标报告
        </Typography.Title>
        <Button icon={<ReloadOutlined />} onClick={() => qc.invalidateQueries({ queryKey: ["ranking", pid] })}>
          重算排名（确定性，秒级）
        </Button>
        <Button
          type="primary"
          icon={<FileTextOutlined />}
          href={reportUrl(pid)}
          target="_blank"
          disabled={!data?.rows.length}
        >
          导出评标报告（HTML）
        </Button>
        <Tag>Word/PDF 导出为后续版本</Tag>
      </Space>

      {data?.warnings?.map((w, i) => (
        <Alert key={i} type="warning" showIcon message={w} style={{ marginBottom: 8 }} />
      ))}

      <Card loading={isLoading} title={`排名（运行 #${data?.run_id ?? "—"}）`}>
        <Table<RankingRow>
          rowKey="bidder_id"
          dataSource={data?.rows ?? []}
          pagination={false}
          expandable={{
            expandedRowRender: (row) => (
              <div>
                <Descriptions size="small" column={3}>
                  <Descriptions.Item label="价格分">{row.price_score ?? "—"}</Descriptions.Item>
                  <Descriptions.Item label="技术分合计">{row.tech_total}</Descriptions.Item>
                  <Descriptions.Item label="商务分合计">{row.commercial_total}</Descriptions.Item>
                </Descriptions>
                {row.tie_explanations.length > 0 && (
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 8 }}
                    message="并列说明"
                    description={row.tie_explanations.map((t, i) => <div key={i}>{t}</div>)}
                  />
                )}
                <Table
                  size="small"
                  rowKey={(b) => b.code + b.criterion}
                  dataSource={row.breakdown}
                  pagination={false}
                  columns={[
                    { title: "评分项", dataIndex: "criterion" },
                    { title: "类别", dataIndex: "category", width: 120 },
                    { title: "得分", dataIndex: "final_score", width: 100, render: (v) => v ?? "未定分" },
                    { title: "复核状态", dataIndex: "review_status", width: 140 },
                  ]}
                />
              </div>
            ),
          }}
          columns={[
            {
              title: "名次",
              dataIndex: "rank",
              width: 80,
              render: (rank: number, row) => (
                <Space>
                  <b style={{ fontSize: 16 }}>{rank}</b>
                  {row.veto_flag && <Tag color="red">否决</Tag>}
                </Space>
              ),
            },
            { title: "投标人", dataIndex: "bidder_name" },
            {
              title: "投标报价",
              dataIndex: "bid_price",
              render: (v: string | null) => (v ? `${Number(v).toLocaleString()} 元` : "—"),
            },
            {
              title: "总分",
              dataIndex: "total",
              width: 100,
              render: (v: string, row) =>
                row.missing_final.length ? (
                  <Space>
                    <span>{v}</span>
                    <Tag color="orange">部分未定分</Tag>
                  </Space>
                ) : (
                  v
                ),
            },
            { title: "价格分", dataIndex: "price_score", width: 90, render: (v) => v ?? "—" },
            { title: "技术分", dataIndex: "tech_total", width: 90 },
            { title: "商务分", dataIndex: "commercial_total", width: 90 },
          ]}
        />
      </Card>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={24}>
          <Card size="small" title="说明">
            <ul style={{ margin: 0, paddingLeft: 18, color: "#666" }}>
              <li>总分与并列名次由纯代码计算：取整后比较，依次适用「报价低 → 技术分高 → 商务分高 → 名称序」。</li>
              <li>建议否决/已否决的投标人仍参与排名，用于交叉验证；其名次以「否决」标签单独标识。</li>
              <li>存在未定分项的投标人总分不完整，报告中如实标注，不得作为定标依据。</li>
              <li>报告包含每个评分项的最终分值、复核状态、规则版本哈希与操作留痕，可用于答复质疑。</li>
            </ul>
          </Card>
        </Col>
      </Row>
    </div>
  );
}
