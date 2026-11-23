import { Card, Col, Row, Tag, Tooltip, Typography } from "antd";
import type { BidderSummary } from "../../types";

interface Props {
  bidder: BidderSummary;
  managed?: boolean;
  provisional?: boolean;
}

const VERDICT_META: Record<string, { label: string; color: string }> = {
  pass: { label: "通过", color: "#389e0d" },
  recommend_reject: { label: "建议否决", color: "#cf1322" },
  need_human: { label: "待人工", color: "#d46b08" },
  fail: { label: "不通过", color: "#cf1322" },
  concern: { label: "存疑", color: "#d46b08" },
  unverifiable: { label: "现有材料无法证实", color: "#ad8b00" },
  execution_failed: { label: "执行失败", color: "#cf1322" },
};

/** 分项口径标签：固定口径（不随数据变化）——价格/商务为确定性客观分，技术分含 AI 主观评审。 */
const SIDE_META: Record<string, { label: string; color: string; border: string }> = {
  objective: { label: "客观", color: "#0958d9", border: "#adc6ff" },
  subjective: { label: "主观", color: "#531dab", border: "#d3adf7" },
  mixed: { label: "主观+客观", color: "#08979c", border: "#87e8de" },
  verdict: { label: "客观·判定", color: "#0958d9", border: "#adc6ff" },
};

function SideTag({ side }: { side: keyof typeof SIDE_META }) {
  const m = SIDE_META[side];
  return (
    <span
      style={{
        fontSize: 11,
        lineHeight: "16px",
        padding: "0 5px",
        borderRadius: 3,
        color: m.color,
        border: `1px solid ${m.border}`,
        background: "#fff",
      }}
    >
      {m.label}
    </span>
  );
}

/** 四个分桶的汇总卡：总分 / 价格分 / 技术分 / 商务分（资格桶显示判定计数）。 */
export default function BucketCards({ bidder, managed = false, provisional = false }: Props) {
  const summary = bidder.summary;
  if (!summary) return null;

  const tiles = [
    {
      key: "total",
      label: provisional ? "已完成项合计" : "总分",
      score: summary.total,
      max: null as string | null,
      extra: null,
      side: "mixed" as const,
    },
    {
      key: "price",
      label: "价格分",
      score: summary.price_score,
      max: bidder.buckets.find((b) => b.key === "price")?.max ?? null,
      extra: null,
      side: "objective" as const,
    },
    {
      key: "technical",
      label: "技术分",
      score: summary.tech_score,
      max: bidder.buckets.find((b) => b.key === "technical")?.max ?? null,
      extra: null,
      side: "mixed" as const,
    },
    {
      key: "commercial",
      label: "商务分",
      score: summary.commercial_score,
      max: bidder.buckets.find((b) => b.key === "commercial")?.max ?? null,
      extra: null,
      side: "objective" as const,
    },
  ];

  const verdictBucket = bidder.buckets.find((b) => b.key === "qualification_compliance");
  // 轻量 fail（建议否决/二值化"存疑"）与深评 fail（不通过）语义不同，后端合并进同一个
  // verdicts["fail"] 键——这里按条目拆回两个计数，避免混跑时互相冒充。
  const verdictItems = verdictBucket?.items ?? [];
  const lightFailItems = verdictItems.filter((it) => it.machine_status === "fail" && it.lightweight);
  const lightFailCount = lightFailItems.length;
  const deepFailCount = verdictItems.filter((it) => it.machine_status === "fail" && !it.lightweight).length;
  // 二值化 v3（决策 0017）：轻量 fail 显示"存疑"（不写"不通过/建议否决"）；v1/v2 旧显示不变
  const lightFailLabel = lightFailItems.some((it) => (it.lightweight_schema_version ?? 0) >= 3)
    ? "存疑"
    : "建议否决";
  const verdictLabel = (k: string) => {
    if (managed && k === "need_human") return "待自动完成"; // deep/fallback 行特例
    return VERDICT_META[k]?.label ?? k;
  };

  return (
    <Row gutter={12}>
      {tiles.map((t) => {
        // 桶内全部条目都"未定分"时，桶合计的 0 是聚合初值而不是评审结论——
        // 显示 0 会让"机器没算出"与"真的是 0 分"混同（2026-10-21 商务桶事故）。
        const bucket = bidder.buckets.find((x) => x.key === t.key);
        const missingOnly =
          !!bucket && bucket.scored_count === 0 && bucket.missing_count > 0;
        return (
          <Col xs={12} md={6} key={t.key}>
            <Card size="small" styles={{ body: { padding: "10px 12px" } }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 12, color: "#888" }}>{t.label}</span>
                <SideTag side={t.side} />
              </div>
              <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
                <span
                  style={{
                    fontSize: t.key === "total" ? 26 : 22,
                    fontWeight: 700,
                    color: missingOnly ? "#b26a00" : "#29745a",
                  }}
                >
                  {missingOnly ? "未定分" : (t.score ?? "—")}
                </span>
                {missingOnly ? (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {bucket?.missing_count} 项{managed ? "待自动完成" : "待人工"}
                  </Typography.Text>
                ) : (
                  t.max && (
                    <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                      / {t.max}
                    </Typography.Text>
                  )
                )}
              </div>
            </Card>
          </Col>
        );
      })}
      {verdictBucket && verdictBucket.verdicts && (
        <Col xs={24}>
          <Card size="small" style={{ marginTop: 0 }} styles={{ body: { padding: "8px 12px" } }}>
            <Typography.Text type="secondary" style={{ fontSize: 12, marginRight: 8 }}>
              资格与符合性（{verdictBucket.item_count} 项，不计分）
            </Typography.Text>
            {bidder.qualification_suggestion && (
              <Tag
                color={
                  bidder.qualification_suggestion.code === "reject"
                    ? "red"
                    : bidder.qualification_suggestion.code === "concern"
                      ? "orange"
                      : "green"
                }
                style={{ marginRight: 8 }}
              >
                总评价：{bidder.qualification_suggestion.label}
                {bidder.qualification_suggestion.code === "pass" &&
                (bidder.qualification_suggestion.manual_check_count ?? 0) > 0
                  ? `（${bidder.qualification_suggestion.manual_check_count} 项待人工核对）`
                  : ""}
              </Tag>
            )}
            <SideTag side="verdict" />
            {Object.entries(verdictBucket.verdicts)
              .flatMap(([k, v]) => {
                if (k === "fail") {
                  // 合并计数还原：轻量 fail=存疑/建议否决、深评 fail=不通过
                  return [
                    ...(lightFailCount > 0
                      ? [{ key: "light_fail", v: lightFailCount, label: lightFailLabel, color: "#cf1322" }]
                      : []),
                    ...(deepFailCount > 0
                      ? [{ key: "fail", v: deepFailCount, label: "不通过", color: "#cf1322" }]
                      : []),
                  ];
                }
                return v > 0 ? [{ key: k, v, label: verdictLabel(k), color: VERDICT_META[k]?.color }] : [];
              })
              .map(({ key, v, label, color }) => (
                <Tooltip key={key} title={label}>
                  <Typography.Text style={{ color, fontSize: 13, marginRight: 12 }}>
                    {label} {v}
                  </Typography.Text>
                </Tooltip>
              ))}
          </Card>
        </Col>
      )}
      {!summary.consistent && (
        <Col xs={24}>
          <Typography.Text type="warning" style={{ fontSize: 12 }}>
            总分 {summary.total} 与该投标人三桶之和 {summary.bucket_sum} 不一致，
            差额 {summary.other_score} 来自其它计分项（如多个价格项或资格/符合性项带分值）。
          </Typography.Text>
        </Col>
      )}
    </Row>
  );
}
