import { useState } from "react";
import { Alert, Button, Card, Empty, Input, Modal, Space, Spin, Tag, Tooltip, Typography } from "antd";
import { useTemplate } from "../../api/hooks";
import type { TemplateCriterion, TemplateSummary } from "../../types";

const APPLICABLE: Record<string, string> = {
  goods: "货物类",
  services: "服务类",
  works: "工程类",
};

/** 单条模板评分项的细则（分档判据 / 计分规则 / 证明材料）。 */
function TemplateCriterionDetail({ c }: { c: TemplateCriterion }) {
  const params = c.checks?.[0]?.params as
    | { per_item?: number | string; max_items?: number | string; min_amount_yuan?: number | string }
    | undefined;
  const countParts: string[] = [];
  if (params?.per_item != null) {
    countParts.push(`每项 ${params.per_item} 分`);
    if (params.max_items != null) countParts.push(`最多 ${params.max_items} 项`);
    if (params.min_amount_yuan != null) {
      countParts.push(`金额≥${Number(params.min_amount_yuan) / 10000} 万元`);
    }
  }

  return (
    <div
      style={{
        padding: "8px 10px",
        borderBottom: "1px solid #f0f0f0",
        fontSize: 12,
      }}
    >
      <Space size={6} wrap>
        <Typography.Text strong>{c.template_code} {c.name}</Typography.Text>
        {c.is_veto ? <Tag color="red">否决项</Tag> : <Tag color="green">{c.max_score ?? "—"} 分</Tag>}
        <Tag>{c.category}</Tag>
      </Space>
      {c.description && (
        <div style={{ color: "#666", marginTop: 4 }}>{c.description}</div>
      )}
      {countParts.length > 0 && (
        <div style={{ marginTop: 4 }}>
          <b>计分规则：</b>
          {countParts.join("，")}
        </div>
      )}
      {c.levels.length > 0 && (
        <div style={{ marginTop: 4 }}>
          <b>档位判据：</b>
          {c.levels.map((lv) => (
            <div key={lv.label} style={{ marginTop: 2, color: "#555" }}>
              {lv.label}（{lv.min}–{lv.max} 分）：{lv.criteria}
            </div>
          ))}
        </div>
      )}
      {c.required_evidence && (
        <div style={{ marginTop: 4 }}>
          <b>证明材料：</b>
          <span style={{ color: "#555" }}>{c.required_evidence}</span>
        </div>
      )}
    </div>
  );
}

/** 启用前预览某套模板的完整评分细则。 */
function TemplateDetailModal({
  templateId,
  name,
  onClose,
}: {
  templateId: string | null;
  name: string;
  onClose: () => void;
}) {
  const { data, isLoading } = useTemplate(templateId);
  return (
    <Modal
      open={!!templateId}
      onCancel={onClose}
      footer={null}
      width={900}
      title={`评分细则：${name}`}
    >
      {isLoading ? (
        <Spin />
      ) : (
        <>
          {data?.notes && (
            <Alert type="info" showIcon style={{ marginBottom: 10 }} message={data.notes} />
          )}
          <div style={{ maxHeight: 520, overflowY: "auto", border: "1px solid #f0f0f0", borderRadius: 4 }}>
            {(data?.criteria ?? []).map((c) => (
              <TemplateCriterionDetail key={c.template_code} c={c} />
            ))}
          </div>
        </>
      )}
    </Modal>
  );
}

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
  const [detail, setDetail] = useState<{ id: string; name: string } | null>(null);

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
                  <Space size={6} style={{ width: "100%" }}>
                    <Button
                      size="small"
                      block
                      onClick={() => setDetail({ id: t.template_id, name: t.name })}
                    >
                      查看评分细则
                    </Button>
                  </Space>
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

      <TemplateDetailModal
        templateId={detail?.id ?? null}
        name={detail?.name ?? ""}
        onClose={() => setDetail(null)}
      />
    </Modal>
  );
}
