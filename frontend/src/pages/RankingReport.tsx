import { useParams } from "react-router-dom";
import { Alert, Button, Card, Col, Descriptions, Row, Space, Statistic, Table, Tag, Typography } from "antd";
import { FileTextOutlined, ReloadOutlined } from "@ant-design/icons";
import { useQueryClient } from "@tanstack/react-query";
import { reportUrl } from "../api/client";
import { useRanking, useReport } from "../api/hooks";
import type { OpenByCriterion, OpenQuestion, RankingRow } from "../types";

const SEVERITY_META: Record<OpenQuestion["severity"], { color: string; text: string }> = {
  high: { color: "red", text: "高" },
  medium: { color: "orange", text: "中" },
  low: { color: "green", text: "低" },
};

export default function RankingReport() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const { data, isLoading } = useRanking(pid);
  const { data: report } = useReport(pid);

  const isDraft = report?.status === "draft";
  const highQuestions = report?.open_questions.filter((q) => q.severity === "high") ?? [];
  // medium 不能与 low 混在一起：medium（如"未提取到计数事实"）仍需要人去看，
  // 归进"无实质疑问、可批量确认"会诱导复核人跳过真实问题。
  const mediumQuestions = report?.open_questions.filter((q) => q.severity === "medium") ?? [];
  const lowQuestions = report?.open_questions.filter((q) => q.severity === "low") ?? [];

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          排名与评标报告
        </Typography.Title>
        {report && (
          <Tag color={isDraft ? "red" : "green"} style={{ fontSize: 13, padding: "2px 10px" }}>
            {report.status_label}
          </Tag>
        )}
        <Button
          icon={<ReloadOutlined />}
          onClick={() => {
            qc.invalidateQueries({ queryKey: ["ranking", pid] });
            qc.invalidateQueries({ queryKey: ["report", pid] });
          }}
        >
          重算排名与报告（确定性，秒级）
        </Button>
        <Button
          type="primary"
          icon={<FileTextOutlined />}
          href={reportUrl(pid)}
          target="_blank"
          disabled={!data?.rows.length}
        >
          {isDraft ? "导出评标报告草稿（HTML）" : "导出评标报告（HTML）"}
        </Button>
      </Space>

      {report && isDraft && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message={`本报告为草稿，尚有 ${report.progress.open} 项未经人工处置（其中 ${report.progress.blocking} 项需优先裁决）`}
          description={
            <div>
              草稿中的排名与总分为<b>暂定值</b>：未定分项不计入总分，建议否决者仍参与排名仅供交叉验证。
              逐项复核完成后点「重算排名与报告」即为<b>定稿</b>，两者共用同一规则版本哈希，可逐项对照。
            </div>
          }
        />
      )}
      {report && !isDraft && (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 12 }}
          message={`本报告为定稿：全部 ${report.progress.total} 项评分均已人工处置`}
        />
      )}

      {report && (
        <Row gutter={16} style={{ marginBottom: 12 }}>
          <Col span={6}>
            <Card size="small">
              <Statistic title="评分项总数" value={report.progress.total} />
            </Card>
          </Col>
          <Col span={6}>
            <Card size="small">
              <Statistic title="已处置" value={report.progress.settled} valueStyle={{ color: "#389e0d" }} />
            </Card>
          </Col>
          <Col span={6}>
            <Card size="small">
              <Statistic
                title="待裁决（高）"
                value={report.progress.blocking}
                valueStyle={{ color: report.progress.blocking ? "#cf1322" : undefined }}
              />
            </Card>
          </Col>
          <Col span={6}>
            <Card size="small">
              <Statistic
                title="待核对（中）/ 待浏览（低）"
                value={`${mediumQuestions.length} / ${lowQuestions.length}`}
              />
            </Card>
          </Col>
        </Row>
      )}

      {data?.warnings?.map((w, i) => (
        <Alert key={i} type="warning" showIcon message={w} style={{ marginBottom: 8 }} />
      ))}

      {highQuestions.length > 0 && (
        <Card
          size="small"
          title={<span style={{ color: "#cf1322" }}>需优先裁决事项（{highQuestions.length} 项，涉及 {report?.progress.criteria_open ?? 0} 个评分项）</span>}
          style={{ marginBottom: 12 }}
          extra={<span style={{ color: "#888", fontSize: 12 }}>同一评分项对多家投标人各判一次，建议按评分项批量裁决</span>}
        >
          <Table<OpenByCriterion>
            size="small"
            rowKey={(g) => g.code + g.criterion}
            dataSource={(report?.open_by_criterion ?? []).filter((g) => g.severity === "high")}
            pagination={{ pageSize: 10, size: "small" }}
            scroll={{ x: 900 }}
            columns={[
              {
                title: "严重度",
                dataIndex: "severity",
                width: 80,
                render: (s: OpenQuestion["severity"]) => (
                  <Tag color={SEVERITY_META[s].color}>{SEVERITY_META[s].text}</Tag>
                ),
              },
              { title: "类型", dataIndex: "label", width: 140 },
              {
                title: "评分项",
                width: 220,
                render: (_v, g) => (
                  <span>
                    {g.code} {g.criterion}
                    {g.max_score ? `（满分 ${g.max_score}）` : ""}
                  </span>
                ),
              },
              { title: "类别", dataIndex: "category", width: 100 },
              {
                title: "涉及投标人",
                width: 220,
                render: (_v, g) => (
                  <div>
                    {g.bidders.map((b) => (
                      <div key={b.score_id}>
                        {b.bidder}
                        {b.final_score !== null ? `（当前 ${b.final_score}）` : "（未定分）"}
                      </div>
                    ))}
                  </div>
                ),
              },
              {
                title: "所需处置",
                render: (_v, g) => (
                  <div>
                    <div>{g.action}</div>
                    {g.bidders[0]?.machine_message && (
                      <div style={{ color: "#888", fontSize: 12 }}>
                        机器判定（以首家为例）：{g.bidders[0].machine_message}
                      </div>
                    )}
                  </div>
                ),
              },
            ]}
          />
        </Card>
      )}

      {report && report.open_by_criterion.length > 0 && (
        <Card
          size="small"
          title={`逐条明细（${report.progress.open} 条）`}
          style={{ marginBottom: 12 }}
        >
          <Table<OpenQuestion>
            size="small"
            rowKey={(q) => `${q.kind}-${q.score_id}`}
            dataSource={report.open_questions}
            pagination={{ pageSize: 15, size: "small" }}
            scroll={{ x: 900 }}
            columns={[
              {
                title: "严重度",
                dataIndex: "severity",
                width: 80,
                render: (s: OpenQuestion["severity"]) => (
                  <Tag color={SEVERITY_META[s].color}>{SEVERITY_META[s].text}</Tag>
                ),
              },
              { title: "类型", dataIndex: "label", width: 140 },
              { title: "投标人", dataIndex: "bidder", width: 180 },
              {
                title: "评分项",
                width: 200,
                render: (_v, q) => (
                  <span>
                    {q.code} {q.criterion}
                    {q.max_score ? `（满分 ${q.max_score}）` : ""}
                  </span>
                ),
              },
              {
                title: "当前状态",
                width: 140,
                render: (_v, q) => (
                  <div>
                    <div>{q.review_status}</div>
                    <div style={{ color: "#888", fontSize: 12 }}>
                      {q.final_score !== null ? `当前分 ${q.final_score}` : "未定分"}
                    </div>
                  </div>
                ),
              },
              {
                title: "疑问与所需处置",
                render: (_v, q) => (
                  <div>
                    <div>{q.action}</div>
                    {q.machine_message && (
                      <div style={{ color: "#888", fontSize: 12 }}>机器判定：{q.machine_message}</div>
                    )}
                  </div>
                ),
              },
            ]}
          />
        </Card>
      )}

      <Card loading={isLoading} title={`排名（运行 #${data?.run_id ?? "—"}）`}>
        <Table<RankingRow>
          scroll={{ x: 850 }}
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
              width: 120,
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

      {mediumQuestions.length > 0 && (
        <Card
          size="small"
          title={`需人工核对（${mediumQuestions.length} 项）`}
          style={{ marginBottom: 12 }}
        >
          <Table<OpenQuestion>
            size="small"
            rowKey={(q) => `${q.kind}-${q.score_id}`}
            dataSource={mediumQuestions}
            pagination={{ pageSize: 10, size: "small" }}
            scroll={{ x: 900 }}
            columns={[
              { title: "类型", dataIndex: "label", width: 140 },
              { title: "投标人", dataIndex: "bidder", width: 180 },
              { title: "评分项", width: 200, render: (_v, q) => `${q.code} ${q.criterion}` },
              {
                title: "疑问与所需处置",
                render: (_v, q) => (
                  <div>
                    <div>{q.action}</div>
                    {q.machine_message && (
                      <div style={{ color: "#888", fontSize: 12 }}>机器判定：{q.machine_message}</div>
                    )}
                  </div>
                ),
              },
            ]}
          />
        </Card>
      )}

      {lowQuestions.length > 0 && (
        <Card
          size="small"
          title={`可批量浏览确认（${lowQuestions.length} 项，机器判定通过且计算可复算）`}
          style={{ marginTop: 16 }}
        >
          <Table<OpenQuestion>
            size="small"
            rowKey={(q) => `${q.kind}-${q.score_id}`}
            dataSource={lowQuestions}
            pagination={{ pageSize: 10, size: "small" }}
            scroll={{ x: 800 }}
            columns={[
              { title: "类型", dataIndex: "label", width: 200 },
              { title: "投标人", dataIndex: "bidder", width: 180 },
              { title: "评分项", width: 200, render: (_v, q) => `${q.code} ${q.criterion}` },
              {
                title: "机器判定",
                render: (_v, q) => (
                  <span style={{ color: "#888", fontSize: 12 }}>{q.machine_message || "—"}</span>
                ),
              },
            ]}
          />
        </Card>
      )}

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={24}>
          <Card size="small" title="说明">
            <ul style={{ margin: 0, paddingLeft: 18, color: "#666" }}>
              <li>报告不再等复核完成才可生成：先出草稿、把疑问逐条标出，复核后从同一入口刷新即为定稿。</li>
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
