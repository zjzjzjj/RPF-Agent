import { useState } from "react";
import { Alert, Card, List, Select, Space, Tag, Typography } from "antd";
import type { AutoReviewMetadata } from "../types";
import PdfPageViewer from "./PdfPageViewer";

const BASIS_LABEL: Record<string, string> = {
  evidence: "原文证据核验",
  visual: "原页视觉核验",
  visual_fallback: "原页视觉补核",
  fallback: "评审补充记录",
  external: "外部事实证据边界",
  deterministic: "程序计算",
  absence: "完整材料检索确认缺失",
  lightweight: "资格轻量预检（确定性预检+快审）",
};

export default function AutoReviewDetails({ metadata }: { metadata?: AutoReviewMetadata | null }) {
  const [selectedVisual, setSelectedVisual] = useState(0);
  if (!metadata) return null;
  const visuals = metadata.visual_evidence ?? [];
  const visual = visuals[selectedVisual] ?? visuals[0];
  const rejectQuotes = metadata.lightweight?.reject_quotes ?? [];
  const pending = metadata.material_review_pending;
  const pendingPages = pending?.candidate_pages ?? [];

  return (
    <Card size="small" title="自动评审记录" style={{ marginBottom: 12 }}>
      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        <Space wrap>
          <Tag color={pending ? "orange" : metadata.state === "completed" ? "green" : "red"}>
            {pending ? "材料待核验" : metadata.state === "completed" ? "已自动完成" : "执行失败，可恢复"}
          </Tag>
          {metadata.basis && <Tag>{BASIS_LABEL[metadata.basis] ?? metadata.basis}</Tag>}
          {metadata.attempts != null && <Typography.Text type="secondary">共评审 {metadata.attempts} 轮</Typography.Text>}
        </Space>
        {metadata.error && <Alert type="error" showIcon message="技术故障" description={metadata.error} />}
        {pending && (
          <Alert
            type="warning"
            showIcon
            message={pending.reason || "可继续核验关联证明材料"}
            description={
              <Space direction="vertical" size={4}>
                {pending.required_materials?.length ? <span>待核对 {pending.required_materials.join("、")}</span> : null}
                {pending.value != null && <span>已核实小计 {pending.value} 分</span>}
                {pendingPages.length > 0 && (
                  <span>候选原页 {pendingPages.map((page) => `文件 #${page.document_id} 第 ${page.page_no} 页`).join("、")}</span>
                )}
              </Space>
            }
          />
        )}
        {metadata.confirmed_absent && metadata.coverage_complete && (
          <Alert type="info" showIcon message="已完成全部关联材料检索，确认所需证明缺失，按细则形成最终结论。" />
        )}
        {typeof metadata.calculation === "string" && metadata.calculation.trim() && (
          <div>
            <Typography.Text strong>计分复算</Typography.Text>
            <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>
              {metadata.calculation}
            </Typography.Paragraph>
          </div>
        )}
        {rejectQuotes.length > 0 && (
          <div>
            <Typography.Text strong>反面引文（逐字核验）</Typography.Text>
            <List
              size="small"
              dataSource={rejectQuotes}
              renderItem={(q, index) => (
                <List.Item>
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    <Typography.Text>
                      {index + 1}. {q.quote}
                    </Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      文件 #{q.document_id} · 第 {q.page_no} 页
                    </Typography.Text>
                  </Space>
                </List.Item>
              )}
            />
          </div>
        )}
        {(metadata.attempt_history?.length ?? 0) > 0 && (
          <List
            size="small"
            header="初评与补评过程"
            dataSource={metadata.attempt_history}
            renderItem={(attempt) => (
              <List.Item>
                <Space direction="vertical" size={2}>
                  <Space>
                    <Typography.Text>{attempt.round === 0 ? "初评" : `第 ${attempt.round} 轮补评`}</Typography.Text>
                    <Tag color={attempt.state === "completed" ? "green" : "orange"}>
                      {attempt.state === "completed" ? "已完成" : "本轮未完成"}
                    </Tag>
                  </Space>
                  {attempt.error && <Typography.Text type="secondary">{attempt.error}</Typography.Text>}
                </Space>
              </List.Item>
            )}
          />
        )}
        {visual && (
          <>
            <Typography.Text strong>视觉核验依据（{visuals.length} 页）</Typography.Text>
            <Select
              style={{ width: "100%" }}
              value={visuals.indexOf(visual)}
              onChange={setSelectedVisual}
              options={visuals.map((entry, index) => ({
                value: index,
                label: `文件 #${entry.document_id} · 第 ${entry.page_no} 页`,
              }))}
            />
            <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>
              {visual.observation}
            </Typography.Paragraph>
            {visual.image_sha256 && (
              <Typography.Paragraph type="secondary" copyable={{ text: visual.image_sha256 }} ellipsis style={{ marginBottom: 0 }}>
                页图哈希：{visual.image_sha256}
              </Typography.Paragraph>
            )}
            <PdfPageViewer
              key={`${visual.document_id}-${visual.page_no}`}
              documentId={visual.document_id}
              focusPage={visual.page_no}
              highlights={[]}
              height={420}
            />
          </>
        )}
      </Space>
    </Card>
  );
}
