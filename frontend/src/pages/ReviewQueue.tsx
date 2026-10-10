import { useNavigate, useParams } from "react-router-dom";
import { Alert, Button, Card, Empty, List, Space, Tag, Typography } from "antd";
import { useReviewQueue } from "../api/hooks";
import { REVIEW_STATUS_LABELS } from "../types";

const PRIORITY: Record<number, { color: string; text: string }> = {
  0: { color: "red", text: "建议否决" },
  1: { color: "orange", text: "冲突" },
  2: { color: "gold", text: "证据待核" },
  3: { color: "blue", text: "常规复核" },
};

export default function ReviewQueue() {
  const { pid = "" } = useParams();
  const navigate = useNavigate();
  const { data, isLoading } = useReviewQueue(pid);

  return (
    <div>
      <Typography.Title level={4}>复核队列</Typography.Title>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="按优先级排序：建议否决 > 冲突 > 未核验证据 > 常规。逐项复核，系统不提供批量确认。"
      />
      <Card loading={isLoading}>
        {!data?.items.length ? (
          <Empty description="没有待复核项" />
        ) : (
          <List
            dataSource={data.items}
            renderItem={(item, idx) => (
              <List.Item
                actions={[
                  <Button
                    key="go"
                    type="primary"
                    size="small"
                    onClick={() => navigate(`/p/${pid}/evidence/${item.score_record_id}`)}
                  >
                    复核
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Space wrap>
                      <Tag color={PRIORITY[item.priority]?.color}>{PRIORITY[item.priority]?.text}</Tag>
                      <span>
                        {idx + 1}. {item.bidder_name} · {item.code} {item.criterion}
                      </span>
                    </Space>
                  }
                  description={
                    <Space wrap>
                      <Tag>{REVIEW_STATUS_LABELS[item.review_status] ?? item.review_status}</Tag>
                      {item.machine_status && <Tag>机器：{item.machine_status}</Tag>}
                      <Tag>证据：{item.evidence_status}</Tag>
                      {item.ai_confidence != null && (
                        <Tag>AI 置信度 {Math.round(item.ai_confidence * 100)}%</Tag>
                      )}
                    </Space>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Card>
    </div>
  );
}
