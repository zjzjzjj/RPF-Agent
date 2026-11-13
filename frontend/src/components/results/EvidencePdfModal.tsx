import { Modal } from "antd";
import EvidencePanel, { type EvidenceLike } from "../EvidencePanel";

/**
 * 「查看原文」弹窗：直达 PDF 原页并高亮定位到引文位置。
 * 复用 EvidencePanel（含 PdfPageViewer + bbox 高亮）。
 */
export default function EvidencePdfModal({
  open,
  onClose,
  citations,
  title,
}: {
  open: boolean;
  onClose: () => void;
  citations: EvidenceLike[];
  title?: string;
}) {
  return (
    <Modal
      title={title ?? "投标书原文"}
      open={open}
      onCancel={onClose}
      footer={null}
      width={960}
      styles={{ body: { maxHeight: "75vh", overflow: "auto" } }}
    >
      <EvidencePanel
        citations={citations}
        viewerHeight={520}
        emptyText="暂无证据引文（该评分项的依据记录在评审理由中）"
      />
    </Modal>
  );
}
