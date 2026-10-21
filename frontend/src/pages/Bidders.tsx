import { useEffect, useState } from "react";
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
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from "antd";
import {
  DeleteOutlined,
  InboxOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useBidders, useDocuments } from "../api/hooks";
import { useJobProgress } from "../api/useJobStream";
import { JobProgress } from "../components/JobProgress";
import type { Bidder, DocumentOut } from "../types";

const PRICE_TAG: Record<string, { color: string; text: string }> = {
  extracted: { color: "orange", text: "待确认" },
  confirmed: { color: "green", text: "已确认" },
  conflict: { color: "red", text: "冲突待裁决" },
};

const TERMINAL = ["succeeded", "failed", "cancelled"];

interface PreviewItem {
  code: string;
  name: string;
  verdict: string;
  message: string;
}

interface PreviewState {
  bidder: Bidder;
  items: PreviewItem[];
}

/** 投标文件列：每行独立持有 job 状态。
 *
 * 单值 jobId 放在页面顶层时，并发上传会互相覆盖（只记得最后一个任务），
 * 且进度只显示在页头、不落在对应投标人上——故收敛到行级子组件。
 */
function BidderFileCell({
  bidder,
  pid,
  docs,
}: {
  bidder: Bidder;
  pid: string;
  docs: DocumentOut[];
}) {
  const qc = useQueryClient();
  const [jobId, setJobId] = useState<number | null>(null);
  const job = useJobProgress(jobId);
  const running = !!job && !TERMINAL.includes(job.status);

  const upload = useMutation({
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
    onError: (e: Error) => message.error(e.message || "上传失败"),
  });

  // 重新解析（OCR 引擎修复后重跑扫描件；幂等端点，不重跑评分）
  // 逐文档记录进行中的 id：单值 isPending 会让所有行的图标一起转圈
  const [reparseDocId, setReparseDocId] = useState<number | null>(null);
  const reparse = useMutation({
    mutationFn: (documentId: number) => {
      setReparseDocId(documentId);
      return api.post<{ job_id: number }>(`/documents/${documentId}/reparse`);
    },
    onSuccess: (r) => {
      setJobId(r.job_id);
      message.info("已提交重新解析，扫描件内容将走 OCR");
    },
    onError: (e: Error) => message.error(e.message || "重新解析失败"),
    onSettled: () => setReparseDocId(null),
  });

  // 终态提示 + 刷新（与 ScoreResults 的终态 invalidate 同款）
  useEffect(() => {
    if (!job) return;
    if (job.status === "succeeded") {
      qc.invalidateQueries({ queryKey: ["documents", pid] });
      qc.invalidateQueries({ queryKey: ["bidders", pid] });
      message.success("投标文件解析完成");
    } else if (job.status === "failed") {
      message.error(job.error || "投标文件解析失败");
    }
  }, [job?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Space direction="vertical" size={2} style={{ width: "100%" }}>
      {docs.map((d) => (
        // 列宽 260：文件名 Tag 可收缩省略，图标按钮固定宽度不参与挤压
        //（旧实现用 inline-flex + 文字链接按钮，260 宽装不下 → 按钮被压扁、
        //  hover 时 antd 给链接底色，看起来"字消失"）
        <div
          key={d.id}
          style={{ display: "flex", alignItems: "center", gap: 2, minWidth: 0 }}
        >
          <Tag
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
              flex: "0 1 auto",
              minWidth: 0,
              maxWidth: 208,
              marginRight: 0,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              display: "inline-block",
            }}
          >
            {d.original_filename}（{d.parse_status}，{d.page_count ?? "?"} 页）
          </Tag>
          <Tooltip title="重新解析：OCR 引擎修复后重跑该文档（扫描件证书/截图/合同才会进文本）。不会重跑评分，需新发起评分。">
            <Button
              size="small"
              type="text"
              icon={<ReloadOutlined />}
              aria-label="重新解析"
              style={{ flexShrink: 0 }}
              loading={reparse.isPending && reparseDocId === d.id}
              onClick={() => reparse.mutate(d.id)}
            />
          </Tooltip>
        </div>
      ))}
      {job && (
        <JobProgress
          job={job}
          width={90}
          onCancel={() => api.post(`/jobs/${job.id}/cancel`).catch(() => undefined)}
        />
      )}
      <Upload
        showUploadList={false}
        beforeUpload={(file) => {
          upload.mutate({ bidderId: bidder.id, file });
          return false;
        }}
      >
        <Tooltip title="支持 PDF（含扫描件自动 OCR）。若报价在 Excel 报价表中，请将该表页导出为 PDF 上传，或使用「人工录入」。">
          <Button size="small" icon={<InboxOutlined />} loading={upload.isPending} disabled={running}>
            上传投标文件
          </Button>
        </Tooltip>
      </Upload>
    </Space>
  );
}

/** 操作列：每行独立持有报价抽取的 job 状态（原同步 GET 改为后台任务）。 */
function BidderActionCell({
  bidder,
  pid,
  onManual,
  onConfirm,
  onPreview,
}: {
  bidder: Bidder;
  pid: string;
  onManual: (b: Bidder) => void;
  onConfirm: (b: Bidder) => void;
  onPreview: (p: PreviewState) => void;
}) {
  const qc = useQueryClient();
  const [jobId, setJobId] = useState<number | null>(null);
  const job = useJobProgress(jobId);
  const running = !!job && !TERMINAL.includes(job.status);

  const extract = useMutation({
    mutationFn: (id: number) =>
      api.post<{ bidder_id: number; job_id: number }>(`/bidders/${id}/price/extract`),
    onSuccess: (r) => setJobId(r.job_id),
    onError: (e: Error) => message.error(e.message || "抽取报价失败"),
  });

  const preview = useMutation({
    mutationFn: (b: Bidder) =>
      api
        .get<{ items: PreviewItem[] }>(`/bidders/${b.id}/qualification-preview`)
        .then((r) => ({ bidder: b, items: r.items })),
    onSuccess: (r) => onPreview(r),
    onError: (e: Error) => message.error(e.message),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.del<{ deleted: number }>(`/bidders/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["bidders", pid] });
      qc.invalidateQueries({ queryKey: ["documents", pid] });
      qc.invalidateQueries({ queryKey: ["workbench", pid] });
      message.success("投标人已删除");
    },
    onError: (e: Error) => message.error(e.message || "删除投标人失败"),
  });

  useEffect(() => {
    if (!job) return;
    if (job.status === "succeeded") {
      qc.invalidateQueries({ queryKey: ["bidders", pid] });
      const st = (job.result_json as { price_status?: string } | null)?.price_status;
      message.success(
        `报价已抽取（${st === "conflict" ? "存在冲突，需人工裁决" : "待确认"}）`
      );
    } else if (job.status === "failed") {
      message.error(job.error || "抽取报价失败");
    }
  }, [job?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Space direction="vertical" size={2} style={{ width: "100%" }}>
      <Space wrap>
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={extract.isPending}
          disabled={running}
          onClick={() => extract.mutate(bidder.id)}
        >
          抽取报价
        </Button>
        <Button
          size="small"
          type="primary"
          ghost
          disabled={!bidder.bid_price || bidder.price_status === "confirmed"}
          onClick={() => onConfirm(bidder)}
        >
          确认报价
        </Button>
        <Button size="small" onClick={() => onManual(bidder)}>
          人工录入
        </Button>
        <Button
          size="small"
          loading={preview.isPending}
          onClick={() => preview.mutate(bidder)}
        >
          资格预判
        </Button>
        <Popconfirm
          title="删除该投标人？"
          description="其投标文件与解析数据将一并删除且不可恢复。已产生评分记录的不能删除。"
          okText="删除"
          okButtonProps={{ danger: true, loading: del.isPending }}
          cancelText="取消"
          onConfirm={() => del.mutate(bidder.id)}
        >
          <Button
            danger
            type="text"
            size="small"
            icon={<DeleteOutlined />}
            aria-label={`删除投标人 ${bidder.name}`}
          />
        </Popconfirm>
      </Space>
      {job && (
        <JobProgress
          job={job}
          width={90}
          onCancel={() => api.post(`/jobs/${job.id}/cancel`).catch(() => undefined)}
        />
      )}
    </Space>
  );
}

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
  const [preview, setPreview] = useState<PreviewState | null>(null);
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
              <BidderFileCell bidder={row} pid={pid} docs={docsByBidder(row.id)} />
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
              <BidderActionCell
                bidder={row}
                pid={pid}
                onManual={setManualTarget}
                onConfirm={setConfirmTarget}
                onPreview={setPreview}
              />
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
