import { Link, useParams } from "react-router-dom";
import { Alert, Card, Col, Descriptions, Row, Steps, Tag, Typography } from "antd";
import { useWorkbench } from "../api/hooks";

const STATE_COLOR: Record<string, string> = {
  done: "green",
  active: "blue",
  blocked: "red",
  pending: "default",
};

export default function ProjectWorkbench() {
  const { pid = "" } = useParams();
  const { data, isLoading } = useWorkbench(pid);

  if (isLoading || !data) return <Card loading />;

  const currentIndex = data.stages.findIndex((s) => s.state === "active" || s.state === "blocked");

  return (
    <div>
      <Typography.Title level={4}>{data.project.name}</Typography.Title>
      <Typography.Text type="secondary">
        项目编号 {data.project.id} ｜ 招标人 {data.project.purchaser || "—"} ｜ 最高限价{" "}
        {data.project.max_price ? `${Number(data.project.max_price).toLocaleString()} 元` : "—"}
      </Typography.Text>

      <Card style={{ marginTop: 16 }}>
        <Steps
          current={currentIndex < 0 ? data.stages.length : currentIndex}
          items={data.stages.map((s) => ({
            title: s.label,
            description: (
              <div>
                <Tag color={STATE_COLOR[s.state]}>
                  {s.state === "done" ? "已完成" : s.state === "active" ? "进行中" : s.state === "blocked" ? "阻塞" : "未开始"}
                </Tag>
                <div style={{ fontSize: 12, color: "#666", marginTop: 4 }}>{s.detail}</div>
              </div>
            ),
          }))}
        />
      </Card>

      {data.blockers.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 16 }}
          message="当前阻塞项"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {data.blockers.map((b, i) => (
                <li key={i}>{b}</li>
              ))}
            </ul>
          }
        />
      )}

      <Row gutter={16} style={{ marginTop: 16 }}>
        {data.stages.map((s) => (
          <Col key={s.key} xs={24} md={8} lg={8} style={{ marginBottom: 16 }}>
            <Card
              size="small"
              title={
                <span>
                  {s.label} <Tag color={STATE_COLOR[s.state]}>{s.state}</Tag>
                </span>
              }
              extra={s.todo_count > 0 ? <Tag color="orange">{s.todo_count} 项待办</Tag> : null}
            >
              <div style={{ minHeight: 44 }}>{s.detail}</div>
              <Link to={`/p/${pid}${s.key === "rules" ? "/rules" : s.key === "bidders" ? "/bidders" : s.key === "review" ? "/review" : s.key === "report" ? "/report" : s.key === "evaluate" ? "/matrix" : ""}`}>
                进入 →
              </Link>
            </Card>
          </Col>
        ))}
      </Row>

      <Card title="合规要点" size="small" style={{ marginTop: 8 }}>
        <Descriptions column={1} size="small">
          <Descriptions.Item label="客观分">
            资格/符合性审查、价格分、计数类评分全部由代码确定性计算，可复算、可审计（《政府采购法实施条例》与财库〔2012〕69 号"客观分必须一致"）。
          </Descriptions.Item>
          <Descriptions.Item label="主观分">
            AI 仅给出建议分与原文依据，最终分值必须由评审人员确认（87 号令"独立评价"）。
          </Descriptions.Item>
          <Descriptions.Item label="否决类">
            系统只做"建议否决 + 依据"，是否否决必须人工确认（《招标投标法实施条例》49 条）。
          </Descriptions.Item>
          <Descriptions.Item label="留痕">
            每一次人工确认、改分、否决都记录操作人、时间与理由，可导出用于答复质疑。
          </Descriptions.Item>
        </Descriptions>
      </Card>
    </div>
  );
}
