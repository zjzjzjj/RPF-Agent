import { Alert, Button, Descriptions, Drawer, Empty, Space, Table, Tag, Typography } from "antd";
import { useNavigate } from "react-router-dom";
import EvidencePanel, { type EvidenceLike } from "../EvidencePanel";
import type { SummaryItem } from "../../types";

interface Props {
  pid: string;
  item: SummaryItem | null;
  open: boolean;
  onClose: () => void;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <Typography.Text strong style={{ display: "block", marginBottom: 6 }}>
        {title}
      </Typography.Text>
      {children}
    </div>
  );
}

function Quote({ text }: { text: string }) {
  return (
    <Typography.Paragraph
      style={{ background: "#fafafa", padding: 8, borderRadius: 4, whiteSpace: "pre-wrap", marginBottom: 0 }}
    >
      {text}
    </Typography.Paragraph>
  );
}

/** 逐项理由抽屉：AI / 机器 / 人工三段理由 + 评委、子项、材料核验、证据引文。只读。 */
export default function ItemReasonDrawer({ pid, item, open, onClose }: Props) {
  const navigate = useNavigate();
  const citations: EvidenceLike[] = (item?.evidence.citations ?? []).map((c) => ({
    id: c.id,
    document_id: c.document_id,
    page_no: c.page_no,
    node_id: c.node_id,
    quote: c.quote ?? "",
    verified: c.verified,
    verify_method: c.verify_method,
    bbox_json: c.bbox_json,
    page_size_json: c.page_size_json,
    created_by: c.created_by,
  }));

  const reason = item?.reason;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={720}
      title={item ? `${item.code} ${item.name}` : "评分理由"}
      destroyOnHidden
      footer={
        item?.score_record_id != null ? (
          <Button
            type="primary"
            onClick={() => navigate(`/p/${pid}/evidence/${item.score_record_id}`)}
          >
            去证据核验页处置 →
          </Button>
        ) : null
      }
    >
      {!item ? (
        <Empty />
      ) : (
        <>
          <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}>
            <Descriptions.Item label="得分">
              {item.state === "scored" ? <b>{item.final}</b> : (item.verdict ?? "—")}
            </Descriptions.Item>
            <Descriptions.Item label="满分">{item.max_score ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="失分">
              {item.deduction.kind === "points" ? (
                <span style={{ color: "#cf1322" }}>{item.deduction.points}</span>
              ) : (
                "—"
              )}
            </Descriptions.Item>
            <Descriptions.Item label="复核状态">{item.review_status}</Descriptions.Item>
            <Descriptions.Item label="证据">
              {item.evidence.total_count === 0
                ? "无引用"
                : `${item.evidence.verified_count}/${item.evidence.total_count} 已核验`}
            </Descriptions.Item>
            <Descriptions.Item label="失分算式">
              {item.deduction.formula ?? "—"}
            </Descriptions.Item>
          </Descriptions>

          <Section title="AI 评审理由">
            {item.ai_locked ? (
              <Alert
                type="warning"
                showIcon
                message="AI 理由未解锁（防锚定）"
                description="该项尚未定分。请先到证据核验页给出独立分数并解锁，再回来看 AI 建议与理由——避免先看建议影响评审独立性。"
              />
            ) : reason?.ai_reasoning ? (
              <>
                <Quote text={reason.ai_reasoning} />
                {reason.truncated && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    已截断（原文 {reason.ai_reasoning_chars} 字），完整内容见证据核验页
                  </Typography.Text>
                )}
              </>
            ) : (
              <Typography.Text type="secondary">—（该项无 AI 评审理由）</Typography.Text>
            )}
          </Section>

          {reason?.machine_reason && (
            <Section title="机器判定理由">
              <Quote text={reason.machine_reason} />
              {reason.unresolved && (
                <Tag color="orange" style={{ marginTop: 6 }}>
                  未决标记：{reason.unresolved}
                </Tag>
              )}
            </Section>
          )}

          {reason?.human_reason && (
            <Section title="人工复核理由">
              <Quote text={reason.human_reason} />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                动作 {reason.human_action} · 操作人 {reason.human_actor ?? "—"}
                {reason.human_at ? ` · ${reason.human_at}` : ""}
              </Typography.Text>
            </Section>
          )}

          {item.committee.length > 0 && (
            <Section title={`评委明细（${item.committee.length} 位）`}>
              <Table
                size="small"
                pagination={false}
                rowKey={(m) => String(m.judge_no)}
                dataSource={item.committee}
                columns={[
                  { title: "评委/证据簇", dataIndex: "judge_no", width: 110 },
                  { title: "分值", dataIndex: "score", width: 80 },
                  { title: "档位", dataIndex: "level_label", width: 90 },
                  {
                    title: "处置",
                    render: (_, m) => (
                      <Space size={4}>
                        {m.dropped && <Tag color="default">已弃（去尾）</Tag>}
                        {m.insufficient && <Tag color="orange">证据不足</Tag>}
                        {m.cluster_chars != null && (
                          <Tag color="blue">{m.cluster_chars} 字符证据簇</Tag>
                        )}
                      </Space>
                    ),
                  },
                ]}
              />
            </Section>
          )}

          {item.sub_scores.length > 0 && (
            <Section title="子项得分">
              <Table
                size="small"
                pagination={false}
                rowKey={(s) => s.name}
                dataSource={item.sub_scores}
                columns={[
                  { title: "子项", dataIndex: "name" },
                  { title: "得分", dataIndex: "score", width: 90 },
                  { title: "满分", dataIndex: "max", width: 90 },
                ]}
              />
            </Section>
          )}

          {item.artifacts_check.length > 0 && (
            <Section title="材料核验">
              <Table
                size="small"
                pagination={false}
                rowKey={(a) => a.artifact}
                dataSource={item.artifacts_check}
                columns={[
                  { title: "证明材料", dataIndex: "artifact", width: 160 },
                  {
                    title: "是否提供",
                    dataIndex: "found",
                    width: 100,
                    render: (v: boolean) =>
                      v ? <Tag color="green">已提供</Tag> : <Tag color="red">缺失</Tag>,
                  },
                  { title: "引文", dataIndex: "quote", ellipsis: true },
                ]}
              />
            </Section>
          )}

          <Section title="招标文件依据">
            {item.source.source_quote ? (
              <>
                <Quote text={item.source.source_quote} />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  第 {item.source.source_page ?? "?"} 页
                  {item.source.source_node_id ? ` · ${item.source.source_node_id}` : ""}
                </Typography.Text>
              </>
            ) : (
              <Typography.Text type="secondary">—</Typography.Text>
            )}
            {item.source.levels.length > 0 && (
              <div style={{ marginTop: 8 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  档位判据
                </Typography.Text>
                {item.source.levels.map((lv) => (
                  <div key={lv.label} style={{ fontSize: 12, marginTop: 4 }}>
                    <Tag>{lv.label}</Tag>
                    {lv.min}-{lv.max} 分 {lv.criteria}
                  </div>
                ))}
              </div>
            )}
          </Section>

          <Section title="证据引文">
            <EvidencePanel citations={citations} viewerHeight={420} />
          </Section>
        </>
      )}
    </Drawer>
  );
}
