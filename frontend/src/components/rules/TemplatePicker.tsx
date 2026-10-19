import { useState } from "react";
import { Alert, Button, Card, Empty, Input, Modal, Space, Spin, Tag, Tooltip, Typography } from "antd";
import type { TemplateSummary } from "../../types";

const APPLICABLE: Record<string, string> = {
  goods: "货物类",
  services: "服务类",
  works: "工程类",
};

interface Props {
  open: boolean;
  loading: boolean;
  submitting: boolean;
  hasNonTemplatePackage: boolean;
  templates: TemplateSummary[];
  onCancel: () => void;
  onPick: (templateId: string, reason: string) => void;
}

/** 无招标文件时的默认规则选择：4 套内置模板 → 一键启用并冻结。 */
export default function TemplatePicker({
  open,
  loading,
  submitting,
  hasNonTemplatePackage,
  templates,
  onCancel,
  onPick,
}: Props) {
  const [reason, setReason] = useState("");

  return (
    <Modal
      open={open}
      onCancel={onCancel}
      footer={null}
      width={860}
      title="使用默认规则（无需招标文件）"
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="选择一套内置评标办法：系统将实例化 → 全部签批 → 确认 → 冻结，完成后即可直接发起评分。"
        description="模板条目为通用判据（P5 来源），启用动作会逐条留痕；之后仍可在规则列表里逐条停用/修正。"
      />
      {hasNonTemplatePackage && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="当前项目已有非模板规则包"
          description="如需更换默认规则，请先确认并冻结，再「解冻并创建新版本」。"
        />
      )}

      {loading ? (
        <Spin />
      ) : templates.length === 0 ? (
        <Empty description="没有可用的模板" />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(380px, 1fr))", gap: 12 }}>
          {templates.map((t) => {
            const procurement = (t.applicable?.procurement ?? "—") as string;
            const cats = t.categories_summary ?? {};
            return (
              <Card key={t.template_id} size="small" styles={{ body: { padding: 14 } }}>
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  <Space wrap>
                    <Typography.Text strong>{t.name}</Typography.Text>
                    <Tag color="blue">{APPLICABLE[procurement] ?? procurement}</Tag>
                    <Tag>v{t.library_version}</Tag>
                  </Space>
                  <Space size={4} wrap>
                    <Tag>{t.criteria_count} 条</Tag>
                    <Tag color="green">价格 {cats.price ?? 0}</Tag>
                    <Tag color="geekblue">技术 {cats.technical ?? 0}</Tag>
                    <Tag color="purple">商务 {cats.commercial ?? 0}</Tag>
                    <Tag>资格/符合 {((cats.qualification ?? 0) + (cats.compliance ?? 0)) || 0}</Tag>
                  </Space>
                  {t.notes && (
                    <Tooltip title={t.notes}>
                      <Typography.Paragraph
                        type="secondary"
                        style={{ fontSize: 12, marginBottom: 0 }}
                        ellipsis={{ rows: 2 }}
                      >
                        {t.notes}
                      </Typography.Paragraph>
                    </Tooltip>
                  )}
                  <Button
                    type="primary"
                    size="small"
                    block
                    loading={submitting}
                    disabled={hasNonTemplatePackage}
                    onClick={() => onPick(t.template_id, reason)}
                  >
                    启用并冻结
                  </Button>
                </Space>
              </Card>
            );
          })}
        </div>
      )}

      <Input.TextArea
        style={{ marginTop: 12 }}
        rows={2}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="启用理由（人工模式必填，将逐条记入签批留痕）"
      />
    </Modal>
  );
}
