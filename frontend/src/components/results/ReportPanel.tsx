import { Alert, Card, Col, Row, Space, Statistic, Table, Tag } from "antd";
import type { OpenByCriterion, OpenQuestion, ReportOut } from "../../types";
import { REVIEW_STATUS_LABELS } from "../../types";

const SEVERITY_META: Record<OpenQuestion["severity"], { color: string; text: string }> = {
  high: { color: "red", text: "高" },
  medium: { color: "orange", text: "中" },
  low: { color: "green", text: "低" },
};

interface Props {
  report: ReportOut | undefined;
  /** 后端在无运行/运行失败时 404，此时给出可读提示而不是整页报错 */
  error?: boolean;
}

/** 报告状态、KPI、待处置事项与说明（原「排名与报告」页的报告部分）。 */
export default function ReportPanel({ report, error }: Props) {
  if (error) {
    return (
      <Alert
        type="info"
        showIcon
        message="尚无可用的评标报告"
        description="报告需要一次已完成的评分运行。请先在上方「发起评分运行」，完成后回到本页。"
      />
    );
  }
  if (!report) return <Card loading />;

  const isDraft = report.status === "draft";
  const highQuestions = report.open_questions.filter((q) => q.severity === "high");
  // medium 不能与 low 混在一起：medium（如"未提取到计数事实"）仍需要人去看，
  // 归进"无实质疑问、可批量确认"会诱导复核人跳过真实问题。
  const mediumQuestions = report.open_questions.filter((q) => q.severity === "medium");
  const lowQuestions = report.open_questions.filter((q) => q.severity === "low");

  return (
    <div>
      {isDraft ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message={`本报告为草稿，尚有 ${report.progress.open} 项未定稿处置（其中 ${report.progress.blocking} 项需优先裁决）`}
          description={
            <div>
              草稿中的排名与总分为<b>暂定值</b>：未定分项不计入总分，建议否决者仍参与排名仅供交叉验证。
              逐项处置完成后即为<b>定稿</b>，两者共用同一规则版本哈希，可逐项对照。
            </div>
          }
        />
      ) : (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 12 }}
          message={`本报告为定稿：全部 ${report.progress.total} 项评分均已处置（人工确认或自动裁决，全程留痕）`}
        />
      )}

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

      {highQuestions.length > 0 && (
        <Card
          size="small"
          title={
            <span style={{ color: "#cf1322" }}>
              需优先裁决事项（{highQuestions.length} 项，涉及 {report.progress.criteria_open ?? 0} 个评分项）
            </span>
          }
          style={{ marginBottom: 12 }}
          extra={
            <span style={{ color: "#888", fontSize: 12 }}>
              同一评分项对多家投标人各判一次，逐条处置请点「评分结果」标签页进证据核验
            </span>
          }
        >
          <Table<OpenByCriterion>
            size="small"
            rowKey={(g) => g.code + g.criterion}
            dataSource={report.open_by_criterion.filter((g) => g.severity === "high")}
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

      {report.open_by_criterion.length > 0 && (
        <Card size="small" title={`逐条明细（${report.progress.open} 条）`} style={{ marginBottom: 12 }}>
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
                    <div>{REVIEW_STATUS_LABELS[q.review_status] ?? q.review_status}</div>
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

      {mediumQuestions.length > 0 && (
        <Card size="small" title={`需人工核对（${mediumQuestions.length} 项）`} style={{ marginBottom: 12 }}>
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
          style={{ marginBottom: 12 }}
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

      <Card size="small" title="说明">
        <Space direction="vertical" size={2} style={{ color: "#666", fontSize: 13 }}>
          <div>· 报告先出草稿、把疑问逐条标出；处置完成后从同一入口刷新即为定稿。</div>
          <div>· 总分与并列名次由纯代码计算：取整后比较，依次适用「报价低 → 技术分高 → 商务分高 → 名称序」。</div>
          <div>· 建议否决/已否决的投标人仍参与排名，用于交叉验证；其名次以「否决」标签单独标识。</div>
          <div>· 存在未定分项的投标人总分不完整，报告中如实标注，不得作为定标依据。</div>
          <div>· 报告包含每个评分项的最终分值、复核状态、规则版本哈希与操作留痕，可用于答复质疑。</div>
        </Space>
      </Card>
    </div>
  );
}
