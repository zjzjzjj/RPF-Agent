import { useState } from "react";
import { useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from "antd";
import { InboxOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useBidders, useDocuments, useJob } from "../api/hooks";
import type { Bidder } from "../types";

const PRICE_TAG: Record<string, { color: string; text: string }> = {
  extracted: { color: "orange", text: "待确认" },
  confirmed: { color: "green", text: "已确认" },
  conflict: { color: "red", text: "冲突待裁决" },
};

/** 人工录入报价：受控 Modal（静态 Modal.confirm 的 okButtonProps 一次性求值，
 *  disabled 永远卡在初始态——那是"人工录入不可用"的根因，2026-10-13）。 */
function ManualPriceModal({
  bidder,
  onClose,
}: {
  bidder: Bidder | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [price, setPrice] = useState<number | null>(null);
  const [reason, setReason] = useState("");
  const setPriceManual = useMutation({
    mutationFn: ({ id, price_yuan, reason }: { id: number; price_yuan: number; reason: string }) =>
      api.post(`/bidders/${id}/price/set`, { price_yuan, reason }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["bidders"] });
      message.success("报价已人工录入并确认（留痕）");
      onClose();
    },
    onError: (e: Error) => message.error(e.message),
  });
  if (!bidder) return null;
  const valid = price != null && Number(price) > 0 && !!reason.trim();
  return (
    <Modal
      open
      title={`人工录入报价：${bidder.name}`}
      onCancel={onClose}
      okText="录入并确认（留痕）"
      okButtonProps={{ disabled: !valid, loading: setPriceManual.isPending }}
      onOk={async () =>
        await setPriceManual.mutateAsync({ id: bidder.id, price_yuan: Number(price), reason })
      }
    >
      <InputNumber
        style={{ width: "100%", marginTop: 8 }}
        min={0.01}
        precision={2}
        placeholder="投标报价（元），如 7600000"
        value={price}
        onChange={(v) => setPrice(v ?? null)}
      />
      <Input.TextArea
        placeholder="录入理由（必填；抽取失败或抽错时的兜底，价格分的最敏感输入）"
        rows={2}
        style={{ marginTop: 8 }}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
    </Modal>
  );
}

/** 确认报价：受控 Modal（同族隐患消除；理由可空，空时用默认理由兜底）。 */
function ConfirmPriceModal({
  bidder,
  onClose,
}: {
  bidder: Bidder | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const confirmPrice = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason: string }) =>
      api.post(`/bidders/${id}/price/confirm`, { reason }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["bidders"] });
      message.success("报价已人工确认（价格分输入的强制卡点）");
      onClose();
    },
    onError: (e: Error) => message.error(e.message),
  });
  if (!bidder) return null;
  const conflict = (bidder.price_breakdown_json as { conflict?: boolean } | null)?.conflict;
  return (
    <Modal
      open
      title={`确认 ${bidder.name} 的投标报价`}
      onCancel={onClose}
      okText="确认（留痕）"
      okButtonProps={{ loading: confirmPrice.isPending }}
      onOk={async () =>
        await confirmPrice.mutateAsync({
          id: bidder.id,
          reason: reason.trim() || "与开标一览表核对一致",
        })
      }
    >
      <p>
        报价：<b>{bidder.bid_price ? Number(bidder.bid_price).toLocaleString() : "—"} 元</b>
        （{bidder.bid_price_upper ? `大写：${bidder.bid_price_upper}` : "无大写金额"}）
      </p>
      {conflict && (
        <Alert type="error" showIcon message="大小写金额或分项合计存在冲突，请核对原件后再确认" />
      )}
      <Input.TextArea
        placeholder="确认理由（必填，记入留痕）"
        rows={2}
        style={{ marginTop: 8 }}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
    </Modal>
  );
}

export default function Bidders() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const { data: bidders } = useBidders(pid);
  const { data: documents } = useDocuments(pid);
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const [jobId, setJobId] = useState<number | null>(null);
  const { data: job } = useJob(jobId);
  const [preview, setPreview] = useState<{ bidder: Bidder; items: { code: string; name: string; verdict: string; message: string }[] } | null>(null);
  const [manualTarget, setManualTarget] = useState<Bidder | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<Bidder | null>(null);

  const create = useMutation({
    mutationFn: (values: { name: string; unified_code?: string }) =>
      api.post<Bidder>(`/projects/${pid}/bidders`, values),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["bidders", pid] });
      setOpen(false);
      form.resetFields();
    },
  });

  const extractPrice = useMutation({
    mutationFn: (id: number) => api.get<{ price_status: string }>(`/bidders/${id}/price`),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["bidders", pid] });
      message.success(`报价已抽取（${r.price_status === "conflict" ? "存在冲突，需人工裁决" : "待确认"}）`);
    },
    onError: (e: Error) => message.error(e.message),
  });

  const uploadDoc = useMutation({
    mutationFn: async ({ bidderId, file }: { bidderId: number; file: File }) => {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("role", "main");
      return api.upload<{ job_id: number }>(`/bidders/${bidderId}/documents`, formData);
    },
    onSuccess: (r) => {
      setJobId(r.job_id);
      qc.invalidateQueries({ queryKey: ["bidders", pid] });
      qc.invalidateQueries({ queryKey: ["documents", pid] });
    },
  });

  const qualificationPreview = useMutation({
    mutationFn: (bidder: Bidder) =>
      api
        .get<{ items: { code: string; name: string; verdict: string; message: string }[] }>(
          `/bidders/${bidder.id}/qualification-preview`
        )
        .then((r) => ({ bidder, items: r.items })),
    onSuccess: (r) => setPreview(r),
    onError: (e: Error) => message.error(e.message),
  });

  const docsByBidder = (bidderId: number) =>
    (documents ?? []).filter((d) => d.bidder_id === bidderId);

  return (
    <div>
      <Space style={{ marginBottom: 12 }} align="center">
        <Typography.Title level={4} style={{ margin: 0 }}>
          投标人管理
        </Typography.Title>
        <Button icon={<PlusOutlined />} type="primary" onClick={() => setOpen(true)}>
          添加投标人
        </Button>
        {job && (
          <Tag color={job.status === "succeeded" ? "green" : job.status === "failed" ? "red" : "blue"}>
            解析任务 {job.status} {job.message}
          </Tag>
        )}
      </Space>

      <Table<Bidder>
        scroll={{ x: 1160 }}
        tableLayout="fixed"
        rowKey="id"
        dataSource={bidders ?? []}
        pagination={false}
        columns={[
          // 必须给宽：否则文件名 Tag（nowrap）会把自动列宽撑开，这一列被压成竖排
          { title: "投标人", dataIndex: "name", width: 220, ellipsis: true },
          {
            title: "统一社会信用代码",
            dataIndex: "unified_code",
            width: 200,
            render: (v: string | null) => v ?? "—",
          },
          {
            title: "投标文件",
            width: 260,
            render: (_, row) => (
              <Space direction="vertical" size={2}>
                {docsByBidder(row.id).map((d) => (
                  <Tag
                    key={d.id}
                    color={
                      d.parse_status === "parsed"
                        ? "green"
                        : d.parse_status === "failed"
                          ? "red"
                          : d.parse_status === "degraded"
                            ? "orange"
                            : "blue"
                    }
                    title={`${d.original_filename}（${d.parse_status}，${d.page_count ?? "?"} 页）`}
                    style={{
                      maxWidth: 236,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      display: "inline-block",
                      verticalAlign: "middle",
                    }}
                  >
                    {d.original_filename}（{d.parse_status}，{d.page_count ?? "?"} 页）
                  </Tag>
                ))}
                <Upload
                  showUploadList={false}
                  beforeUpload={(file) => {
                    uploadDoc.mutate({ bidderId: row.id, file });
                    return false;
                  }}
                >
                  <Tooltip title="支持 PDF（含扫描件自动 OCR）。若报价在 Excel 报价表中，请将该表页导出为 PDF 上传，或使用「人工录入」。">
                    <Button size="small" icon={<InboxOutlined />}>
                      上传投标文件
                    </Button>
                  </Tooltip>
                </Upload>
              </Space>
            ),
          },
          {
            title: "投标报价",
            width: 220,
            render: (_, row) => (
              <Space direction="vertical" size={2}>
                <span>
                  {row.bid_price ? `${Number(row.bid_price).toLocaleString()} 元` : "未抽取"}
                  {row.price_status && (
                    <Tag style={{ marginLeft: 6 }} color={PRICE_TAG[row.price_status]?.color}>
                      {PRICE_TAG[row.price_status]?.text ?? row.price_status}
                    </Tag>
                  )}
                </span>
                {!row.bid_price && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    请确认文件含开标一览表，或直接人工录入
                  </Typography.Text>
                )}
                {row.bid_price_upper && (
                  <Typography.Text type="secondary">大写：{row.bid_price_upper}</Typography.Text>
                )}
              </Space>
            ),
          },
          {
            title: "操作",
            width: 260,
            render: (_, row) => (
              <Space wrap>
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  onClick={() => extractPrice.mutate(row.id)}
                >
                  抽取报价
                </Button>
                <Button
                  size="small"
                  type="primary"
                  ghost
                  disabled={!row.bid_price || row.price_status === "confirmed"}
                  onClick={() => setConfirmTarget(row)}
                >
                  确认报价
                </Button>
                <Button size="small" onClick={() => setManualTarget(row)}>
                  人工录入
                </Button>
                <Button size="small" onClick={() => qualificationPreview.mutate(row)}>
                  资格预判
                </Button>
              </Space>
            ),
          },
        ]}
      />

      <Modal title="新建投标人" open={open} onCancel={() => setOpen(false)} onOk={() => form.submit()}>
        <Form form={form} layout="vertical" onFinish={(v) => create.mutate(v)}>
          <Form.Item name="name" label="投标人名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="unified_code" label="统一社会信用代码">
            <Input />
          </Form.Item>
        </Form>
      </Modal>

      <ManualPriceModal bidder={manualTarget} onClose={() => setManualTarget(null)} />
      <ConfirmPriceModal bidder={confirmTarget} onClose={() => setConfirmTarget(null)} />

      <Modal
        open={!!preview}
        title={`资格/符合性预判：${preview?.bidder.name ?? ""}`}
        onCancel={() => setPreview(null)}
        footer={null}
        width={720}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message="预判只读且不生效；正式结论以评分运行的记录为准（裁决留痕可溯）。"
        />
        <List
          size="small"
          dataSource={preview?.items ?? []}
          renderItem={(item) => (
            <List.Item>
              <Space>
                <Tag
                  color={
                    item.verdict === "pass"
                      ? "green"
                      : item.verdict === "recommend_reject" || item.verdict === "fail"
                        ? "red"
                        : "orange"
                  }
                >
                  {item.verdict === "pass"
                    ? "通过"
                    : item.verdict === "recommend_reject"
                      ? "建议否决"
                      : item.verdict === "fail"
                        ? "不通过"
                        : item.verdict === "conflict_flagged"
                          ? "冲突"
                          : "待人工"}
                </Tag>
                <span>
                  {item.code} {item.name}
                </span>
                <Typography.Text type="secondary">{item.message}</Typography.Text>
              </Space>
            </List.Item>
          )}
        />
      </Modal>
    </div>
  );
}
