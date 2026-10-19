import { useMemo, useState } from "react";
import { Card, List, Space, Tag, Typography } from "antd";
import PdfPageViewer, { type Highlight } from "./PdfPageViewer";

/** 引文的展示最小契约：证据核验页与评分结果抽屉共用。 */
export interface EvidenceLike {
  id: number;
  document_id?: number | null;
  page_no?: number | null;
  node_id?: string | null;
  quote: string;
  verified: boolean;
  verify_method?: string | null;
  bbox_json?: number[][] | null;
  page_size_json?: number[] | null;
  created_by?: string | null;
  candidates_json?: { page_no: number; quote: string }[] | null;
}

export const METHOD_LABEL: Record<string, string> = {
  exact: "逐字精确命中",
  ws_insensitive: "逐字命中（忽略排版换行）",
  prefix_fallback: "前缀回退（部分命中，待人工核对）",
  sentence_fallback: "句读片段命中（待人工核对）",
};

const CREATOR_LABEL: Record<string, string> = {
  human: "人工补录",
  engine: "引擎生成",
  llm: "AI 引用",
};

interface Props {
  citations: EvidenceLike[];
  emptyText?: string;
  /** 页图容器高度（抽屉里比证据页矮一些） */
  viewerHeight?: number;
  showViewer?: boolean;
}

/**
 * 证据面板：引文列表 + 原件高亮定位。
 *
 * 自持选中态（谁用谁不管）；高亮按「选中黄 / 已核验绿 / 未核验红」着色。
 */
export default function EvidencePanel({
  citations,
  emptyText = "没有证据引用——无证据不评分，该项应由人工补录证据或降级",
  viewerHeight = 520,
  showViewer = true,
}: Props) {
  const [selected, setSelected] = useState<number | null>(null);

  const highlights: Highlight[] = useMemo(() => {
    const out: Highlight[] = [];
    for (const cit of citations) {
      if (!cit.bbox_json || !cit.page_no) continue;
      out.push({
        id: cit.id,
        page: cit.page_no,
        boxes: cit.bbox_json,
        pageSize: cit.page_size_json ?? null,
        color:
          selected === cit.id
            ? "rgba(250, 204, 21, 0.55)"
            : cit.verified
              ? "rgba(134, 239, 172, 0.35)"
              : "rgba(252, 165, 165, 0.35)",
        label: cit.quote.slice(0, 40),
      });
    }
    return out;
  }, [citations, selected]);

  const focusPage =
    citations.find((c) => c.id === selected)?.page_no ??
    citations.find((c) => c.page_no)?.page_no ??
    1;
  const firstDoc = citations.find((c) => c.document_id && c.page_no)?.document_id ?? null;

  return (
    <>
      <Card size="small" title={`证据引文（${citations.length}）`}>
        <List
          size="small"
          dataSource={citations}
          locale={{ emptyText }}
          renderItem={(cit) => (
            <List.Item
              onClick={() => setSelected(cit.id)}
              style={{
                cursor: "pointer",
                background: selected === cit.id ? "#e6f4ff" : undefined,
                borderRadius: 4,
              }}
            >
              <Space direction="vertical" size={2} style={{ width: "100%" }}>
                <Space wrap>
                  {cit.verified ? <Tag color="green">已核验</Tag> : <Tag color="red">未核验</Tag>}
                  <Tag>{METHOD_LABEL[cit.verify_method ?? ""] ?? cit.verify_method ?? "未定位"}</Tag>
                  <Tag>第 {cit.page_no ?? "?"} 页</Tag>
                  {cit.node_id && <Tag color="blue">{cit.node_id}</Tag>}
                  <Tag>{CREATOR_LABEL[cit.created_by ?? ""] ?? cit.created_by}</Tag>
                </Space>
                <Typography.Text style={{ background: "#fffbe6", padding: 4, borderRadius: 4, display: "block" }}>
                  {cit.quote}
                </Typography.Text>
                {cit.candidates_json && cit.candidates_json.length > 0 && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    候选位置：{cit.candidates_json.map((c) => `第${c.page_no}页`).join("、")}
                    （点击后请在右侧核对）
                  </Typography.Text>
                )}
              </Space>
            </List.Item>
          )}
        />
      </Card>

      {showViewer && firstDoc != null && (
        <Card size="small" title="原件定位（高亮为证据所在位置）" style={{ marginTop: 12 }}>
          <PdfPageViewer
            documentId={firstDoc}
            focusPage={focusPage}
            highlights={highlights}
            height={viewerHeight}
          />
        </Card>
      )}
    </>
  );
}
