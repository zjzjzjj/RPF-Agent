import { Card, Empty, Space, Tag, Tooltip, Typography } from "antd";
import type { BidderSummary } from "../../types";

interface Props {
  bidders: BidderSummary[];
  selectedId: number | null;
  onSelect: (bidderId: number) => void;
}

const RANK_COLOR = ["#d4a017", "#8c8c8c", "#a97142"];

function Metric({ label, value }: { label: string; value: string | null }) {
  return (
    <div style={{ flex: 1, textAlign: "center" }}>
      <div style={{ fontSize: 11, color: "#888" }}>{label}</div>
      <div style={{ fontSize: 13, fontWeight: 600 }}>{value ?? "—"}</div>
    </div>
  );
}

/** 左栏：排名列表。点选一家，右侧显示该家的分桶与逐项明细。 */
export default function BidderRankList({ bidders, selectedId, onSelect }: Props) {
  if (bidders.length === 0) {
    return (
      <Card size="small">
        <Empty description="尚无评分结果" />
      </Card>
    );
  }
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      {bidders.map((b) => {
        const selected = b.bidder_id === selectedId;
        const excluded = b.state === "excluded";
        return (
          <Card
            key={b.bidder_id}
            size="small"
            onClick={() => onSelect(b.bidder_id)}
            style={{
              cursor: "pointer",
              borderColor: selected ? "#29745a" : undefined,
              borderWidth: selected ? 2 : 1,
              opacity: excluded ? 0.6 : 1,
            }}
            styles={{ body: { padding: 12 } }}
          >
            <Space align="start" style={{ width: "100%", justifyContent: "space-between" }}>
              <Space size={8} align="center">
                <div
                  style={{
                    width: 26,
                    height: 26,
                    borderRadius: "50%",
                    lineHeight: "26px",
                    textAlign: "center",
                    fontWeight: 700,
                    color: "#fff",
                    background: excluded
                      ? "#bfbfbf"
                      : (b.rank != null && RANK_COLOR[b.rank - 1]) || "#29745a",
                  }}
                >
                  {excluded ? "—" : (b.rank ?? "?")}
                </div>
                <div>
                  <Typography.Text strong style={{ fontSize: 14 }}>
                    {b.name}
                  </Typography.Text>
                  <div style={{ fontSize: 12, color: "#888" }}>
                    报价 {b.bid_price ? Number(b.bid_price).toLocaleString() : "未录入"} 元
                  </div>
                </div>
              </Space>
              <div style={{ textAlign: "right" }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: excluded ? "#999" : "#29745a" }}>
                  {b.summary?.total ?? "—"}
                </div>
                <div style={{ fontSize: 11, color: "#888" }}>总分</div>
              </div>
            </Space>

            {excluded ? (
              <Tooltip title={b.excluded_reason ?? ""}>
                <Tag color="default" style={{ marginTop: 8 }}>
                  已剔除：{b.excluded_reason}
                </Tag>
              </Tooltip>
            ) : (
              <Space size={4} wrap style={{ marginTop: 8 }}>
                <Tag color={b.veto_flag ? "red" : "green"}>
                  {b.veto_flag ? "存在否决建议" : "无否决建议"}
                </Tag>
                {(b.summary?.missing_count ?? 0) > 0 && (
                  <Tooltip title={`未定分项不计入总分：${(b.summary?.missing_criteria ?? []).join("、")}`}>
                    <Tag color="orange">{b.summary?.missing_count} 项未定分</Tag>
                  </Tooltip>
                )}
                {b.summary && !b.summary.consistent && (
                  <Tooltip title="总分与该投标人三桶之和不相等，差额来自其它计分项（详见右侧提示）">
                    <Tag color="gold">含其它计分项</Tag>
                  </Tooltip>
                )}
                {b.tie_break_level != null && <Tag color="blue">并列裁决</Tag>}
              </Space>
            )}

            {!excluded && b.summary && (
              <div style={{ display: "flex", gap: 4, marginTop: 10 }}>
                <Metric label="价格分" value={b.summary.price_score} />
                <Metric label="技术分" value={b.summary.tech_score} />
                <Metric label="商务分" value={b.summary.commercial_score} />
              </div>
            )}
          </Card>
        );
      })}
    </Space>
  );
}
