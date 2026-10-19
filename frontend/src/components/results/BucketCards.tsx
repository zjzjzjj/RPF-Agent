import { Card, Col, Row, Tooltip, Typography } from "antd";
import type { BidderSummary } from "../../types";

interface Props {
  bidder: BidderSummary;
}

const VERDICT_META: Record<string, { label: string; color: string }> = {
  pass: { label: "通过", color: "#389e0d" },
  recommend_reject: { label: "建议否决", color: "#cf1322" },
  need_human: { label: "待人工", color: "#d46b08" },
  fail: { label: "不通过", color: "#cf1322" },
};

/** 四个分桶的汇总卡：总分 / 价格分 / 技术分 / 商务分（资格桶显示判定计数）。 */
export default function BucketCards({ bidder }: Props) {
  const summary = bidder.summary;
  if (!summary) return null;

  const tiles = [
    { key: "total", label: "总分", score: summary.total, max: null as string | null, extra: null },
    {
      key: "price",
      label: "价格分",
      score: summary.price_score,
      max: bidder.buckets.find((b) => b.key === "price")?.max ?? null,
      extra: null,
    },
    {
      key: "technical",
      label: "技术分",
      score: summary.tech_score,
      max: bidder.buckets.find((b) => b.key === "technical")?.max ?? null,
      extra: null,
    },
    {
      key: "commercial",
      label: "商务分",
      score: summary.commercial_score,
      max: bidder.buckets.find((b) => b.key === "commercial")?.max ?? null,
      extra: null,
    },
  ];

  const verdictBucket = bidder.buckets.find((b) => b.key === "qualification_compliance");

  return (
    <Row gutter={12}>
      {tiles.map((t) => (
        <Col xs={12} md={6} key={t.key}>
          <Card size="small" styles={{ body: { padding: "10px 12px" } }}>
            <div style={{ fontSize: 12, color: "#888" }}>{t.label}</div>
            <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
              <span
                style={{ fontSize: t.key === "total" ? 26 : 22, fontWeight: 700, color: "#29745a" }}
              >
                {t.score ?? "—"}
              </span>
              {t.max && (
                <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                  / {t.max}
                </Typography.Text>
              )}
            </div>
          </Card>
        </Col>
      ))}
      {verdictBucket && verdictBucket.verdicts && (
        <Col xs={24}>
          <Card size="small" style={{ marginTop: 0 }} styles={{ body: { padding: "8px 12px" } }}>
            <Typography.Text type="secondary" style={{ fontSize: 12, marginRight: 8 }}>
              资格与符合性（{verdictBucket.item_count} 项，不计分）
            </Typography.Text>
            {Object.entries(verdictBucket.verdicts).map(([k, v]) =>
              v > 0 ? (
                <Tooltip key={k} title={VERDICT_META[k]?.label ?? k}>
                  <Typography.Text
                    style={{ color: VERDICT_META[k]?.color, fontSize: 13, marginRight: 12 }}
                  >
                    {VERDICT_META[k]?.label ?? k} {v}
                  </Typography.Text>
                </Tooltip>
              ) : null,
            )}
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
