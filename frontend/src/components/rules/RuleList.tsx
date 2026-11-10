import { useMemo, useState } from "react";
import { Button, Card, Empty, List, Select, Space, Switch, Tag, Tooltip, Typography } from "antd";
import type { CriterionOut } from "../../types";
import { CATEGORY_LABELS, CATEGORY_ORDER } from "../../types";

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  verified: { color: "green", text: "已确认" },
  needs_input: { color: "orange", text: "待补全" },
  ambiguous: { color: "gold", text: "歧义（原文未给分值）" },
  gap: { color: "red", text: "缺口" },
  conflict: { color: "red", text: "冲突" },
  invalid: { color: "red", text: "非法" },
  needs_signoff: { color: "orange", text: "兜底待签批" },
  retired: { color: "default", text: "已停用" },
};

const ORIGIN_TAG: Record<string, { color: string; text: string }> = {
  document: { color: "blue", text: "招标文件" },
  template: { color: "orange", text: "模板兜底" },
  law: { color: "purple", text: "法规" },
  manual: { color: "cyan", text: "人工" },
  merged: { color: "geekblue", text: "合并" },
};

/** 计数类客观项的计分规则摘要（从 checks[0].params 生成，如「每项 3 分，最多 5 项（金额≥80 万）」）。 */
function countRuleText(c: CriterionOut): string | null {
  const params = c.checks?.[0]?.params as
    | { per_item?: number | string; max_items?: number | string; min_amount_yuan?: number | string }
    | undefined;
  if (!params || params.per_item == null) return null;
  const parts = [`每项 ${params.per_item} 分`];
  if (params.max_items != null) parts.push(`最多 ${params.max_items} 项`);
  if (params.min_amount_yuan != null) {
    parts.push(`金额≥${Number(params.min_amount_yuan) / 10000} 万元`);
  }
  return parts.join("，");
}

/** 评分细则展开区：档位判据 / 计分规则 / 证明材料——此前只在编辑弹窗里可见，
 *  列表页只显示一行 description，用户「看不到细则」的根因。 */
function RuleDetail({ criterion: c }: { criterion: CriterionOut }) {
  const [open, setOpen] = useState(false);
  const countText = countRuleText(c);
  const hasDetail = c.levels.length > 0 || !!c.required_evidence || !!countText;
  if (!hasDetail) return null;

  const summary = [
    c.levels.length > 0 ? `${c.levels.length} 档判据` : null,
    countText ? "计分规则" : null,
    c.required_evidence ? "证明材料" : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div style={{ marginTop: 4 }}>
      <Typography.Link style={{ fontSize: 12 }} onClick={() => setOpen((v) => !v)}>
        {open ? "收起评分细则" : `查看评分细则（${summary}）`}
      </Typography.Link>
      {open && (
        <div
          style={{
            marginTop: 6,
            padding: "6px 10px",
            background: "#fafafa",
            border: "1px solid #f0f0f0",
            borderRadius: 4,
            fontSize: 12,
          }}
        >
          {countText && (
            <div style={{ marginBottom: 4 }}>
              <b>计分规则：</b>
              {countText}
            </div>
          )}
          {c.levels.length > 0 && (
            <div style={{ marginBottom: 4 }}>
              <b>档位判据：</b>
              {c.levels.map((lv) => (
                <div key={lv.id ?? lv.level_label} style={{ marginTop: 2, color: "#555" }}>
                  {lv.level_label}（{lv.score_min}–{lv.score_max} 分）：{lv.description || "—"}
                </div>
              ))}
            </div>
          )}
          {c.required_evidence && (
            <div>
              <b>证明材料：</b>
              <span style={{ color: "#555" }}>{c.required_evidence}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface Props {
  criteria: CriterionOut[];
  packageStatus: string;
  onEdit: (c: CriterionOut) => void;
  onSignoff: (c: CriterionOut) => void;
  onAccept: (c: CriterionOut) => void;
  onToggle: (c: CriterionOut) => void;
  pendingId?: number | null;
}

/** 规则列表（列表形态）：编号 / 名称 / 类别 / 分值 / 来源 / 状态 / 启用开关 / 操作。 */
export default function RuleList({
  criteria,
  packageStatus,
  onEdit,
  onSignoff,
  onAccept,
  onToggle,
  pendingId,
}: Props) {
  const [category, setCategory] = useState<string>("all");
  const draft = packageStatus === "draft";

  const filtered = useMemo(
    () => (category === "all" ? criteria : criteria.filter((c) => c.category === category)),
    [criteria, category],
  );
  const activeCount = criteria.filter((c) => c.status !== "retired").length;

  if (criteria.length === 0) {
    return (
      <Card title="全部规则">
        <Empty description="尚未生成规则：上传招标文件自动匹配，或使用默认规则" />
      </Card>
    );
  }

  return (
    <Card
      title={
        <Space>
          <span>全部规则</span>
          <Tag>{activeCount} 条启用</Tag>
          {criteria.length > activeCount && <Tag color="default">{criteria.length - activeCount} 条停用</Tag>}
        </Space>
      }
      extra={
        <Select
          size="small"
          style={{ minWidth: 150 }}
          value={category}
          onChange={setCategory}
          options={[
            { value: "all", label: "全部类别" },
            ...CATEGORY_ORDER.filter((c) => criteria.some((x) => x.category === c)).map((c) => ({
              value: c,
              label: CATEGORY_LABELS[c] ?? c,
            })),
          ]}
        />
      }
    >
      <List
        itemLayout="horizontal"
        dataSource={filtered}
        pagination={filtered.length > 20 ? { pageSize: 20, size: "small" } : false}
        renderItem={(c, idx) => {
          const status = STATUS_TAG[c.status] ?? { color: "default", text: c.status };
          const origin = ORIGIN_TAG[c.origin] ?? { color: "default", text: c.origin };
          const retired = c.status === "retired";
          return (
            <List.Item
              style={{ opacity: retired ? 0.55 : 1, alignItems: "flex-start" }}
              actions={[
                <Button key="edit" size="small" type="link" onClick={() => onEdit(c)}>
                  查看/修正
                </Button>,
                ...(c.origin === "template" && c.status === "needs_signoff"
                  ? [
                      <Button key="signoff" size="small" type="link" onClick={() => onSignoff(c)}>
                        签批
                      </Button>,
                    ]
                  : []),
                ...(c.origin !== "template" && c.status !== "verified" && !retired
                  ? [
                      <Button key="accept" size="small" type="link" onClick={() => onAccept(c)}>
                        确认
                      </Button>,
                    ]
                  : []),
              ]}
            >
              <div style={{ display: "flex", gap: 14, width: "100%", alignItems: "flex-start" }}>
                <Typography.Text type="secondary" style={{ fontVariantNumeric: "tabular-nums", width: 22 }}>
                  {String(idx + 1).padStart(2, "0")}
                </Typography.Text>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Space size={6} wrap>
                    <Typography.Text strong>{c.name}</Typography.Text>
                    <Tag>{CATEGORY_LABELS[c.category] ?? c.category}</Tag>
                    {c.is_veto ? (
                      <Tag color="red">否决项</Tag>
                    ) : (
                      <Tag color="green">{c.max_score ?? "—"} 分</Tag>
                    )}
                    <Tag color={origin.color}>{origin.text}</Tag>
                    <Tag color={status.color}>{status.text}</Tag>
                    {c.origin === "template" && c.signoff_json && (
                      <Tooltip
                        title={`已签批：${c.signoff_json.by ?? "—"} ${String(c.signoff_json.at ?? "").slice(0, 10)}`}
                      >
                        <Tag color="gold">已签批</Tag>
                      </Tooltip>
                    )}
                  </Space>
                  <div style={{ color: "#888", fontSize: 12, marginTop: 4 }}>
                    {c.description || "—"}
                  </div>
                  <RuleDetail criterion={c} />
                  {(c.source_page || c.source_node_id) && (
                    <div style={{ color: "#aaa", fontSize: 11, marginTop: 2 }}>
                      依据：第 {c.source_page ?? "?"} 页
                      {c.source_node_id ? ` · ${c.source_node_id}` : ""}
                    </div>
                  )}
                </div>
                <Tooltip
                  title={
                    draft
                      ? retired
                        ? "启用该评分项（恢复停用前状态）"
                        : "停用该评分项（退出计分与冻结快照）"
                      : "仅草稿可启用/停用；请先解冻创建新版本"
                  }
                >
                  <Switch
                    size="small"
                    checked={!retired}
                    disabled={!draft || pendingId === c.id}
                    onChange={() => onToggle(c)}
                  />
                </Tooltip>
              </div>
            </List.Item>
          );
        }}
      />
    </Card>
  );
}
