import { useEffect, useRef, useState } from "react";
import { Button, Space, Tag, Typography } from "antd";
import { LeftOutlined, RightOutlined } from "@ant-design/icons";
import { pageImageUrl } from "../api/client";

export interface Highlight {
  id: string | number;
  page: number;
  /** PDF 坐标（points，top-left 原点） */
  boxes: number[][];
  /** 页面尺寸 [width, height]（points） */
  pageSize: number[] | null;
  color?: string;
  label?: string;
}

interface Props {
  documentId: number;
  focusPage: number;
  pageCount?: number | null;
  highlights: Highlight[];
  scale?: number;
  height?: number;
}

/**
 * 页面查看器：服务端渲染页图 + 按归一化 bbox 百分比叠加高亮层。
 * 高亮不烧录进 PNG —— 切换证据零延迟；滚动只发生在容器内（不滚动整个页面）。
 */
export default function PdfPageViewer({
  documentId,
  focusPage,
  pageCount,
  highlights,
  scale = 2,
  height = 620,
}: Props) {
  const [page, setPage] = useState(focusPage);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const boxRefs = useRef<Record<string, HTMLDivElement | null>>({});

  useEffect(() => {
    setPage(focusPage);
  }, [focusPage, documentId]);

  const pageHighlights = highlights.filter((h) => h.page === page);
  const pageSize = pageHighlights.find((h) => h.pageSize)?.pageSize ?? null;
  // 归一化分母：优先用 PDF 页尺寸（points）；缺失时退回图片像素/scale
  const denom = pageSize
    ? { w: pageSize[0], h: pageSize[1] }
    : natural
      ? { w: natural.w / scale, h: natural.h / scale }
      : null;

  // 首个高亮框滚入容器视野（只在容器内滚动）
  useEffect(() => {
    if (!denom || pageHighlights.length === 0) return;
    const first = boxRefs.current[`${pageHighlights[0].id}`];
    const container = containerRef.current;
    if (first && container) {
      const top = first.offsetTop - container.clientHeight / 3;
      container.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
    }
  }, [page, denom, pageHighlights.length]);

  const total = pageCount ?? undefined;

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Button size="small" icon={<LeftOutlined />} disabled={page <= 1} onClick={() => setPage(page - 1)}>
          上一页
        </Button>
        <Tag>
          第 {page} 页{total ? ` / 共 ${total} 页` : ""}
        </Tag>
        <Button
          size="small"
          disabled={total != null && page >= total}
          onClick={() => setPage(page + 1)}
        >
          下一页 <RightOutlined />
        </Button>
        {pageHighlights.length > 0 && (
          <Typography.Text type="secondary">本页 {pageHighlights.length} 处高亮</Typography.Text>
        )}
      </Space>
      <div
        ref={containerRef}
        style={{
          height,
          overflow: "auto",
          border: "1px solid #ddd",
          borderRadius: 6,
          background: "#f0f0f0",
          position: "relative",
        }}
      >
        <div style={{ position: "relative", display: "inline-block", minWidth: "100%" }}>
          <img
            src={pageImageUrl(documentId, page, scale)}
            alt={`第 ${page} 页`}
            style={{ display: "block", width: "100%" }}
            onLoad={(e) => {
              const img = e.currentTarget;
              setNatural({ w: img.naturalWidth, h: img.naturalHeight });
            }}
          />
          {denom &&
            pageHighlights.map((h) =>
              h.boxes.map((box, i) => {
                const [x0, y0, x1, y1] = box;
                const key = `${h.id}-${i}`;
                return (
                  <div
                    key={key}
                    ref={(el) => {
                      if (i === 0) boxRefs.current[`${h.id}`] = el;
                    }}
                    title={h.label}
                    style={{
                      position: "absolute",
                      left: `${(x0 / denom.w) * 100}%`,
                      top: `${(y0 / denom.h) * 100}%`,
                      width: `${((x1 - x0) / denom.w) * 100}%`,
                      height: `${((y1 - y0) / denom.h) * 100}%`,
                      background: h.color ?? "rgba(250, 204, 21, 0.38)",
                      border: "1px solid rgba(217, 119, 6, 0.9)",
                      borderRadius: 2,
                      pointerEvents: "none",
                    }}
                  />
                );
              })
            )}
        </div>
      </div>
    </div>
  );
}
