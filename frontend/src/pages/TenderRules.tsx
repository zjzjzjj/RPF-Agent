import { useState } from "react";
import { useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { PlayCircleOutlined, CheckOutlined, PlusOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useDocuments, useJob, useModels, useRules } from "../api/hooks";
import type { CriterionOut, ValidationOut } from "../types";
import { CATEGORY_LABELS } from "../types";

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  verified: { color: "green", text: "已确认" },
  needs_input: { color: "orange", text: "待补全" },
  ambiguous: { color: "gold", text: "歧义（原文未给分值）" },
  gap: { color: "red", text: "缺口" },
  conflict: { color: "red", text: "冲突" },
  invalid: { color: "red", text: "非法" },
};

export default function TenderRules() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const { data: documents } = useDocuments(pid);
  const { data: rules, isLoading } = useRules(pid);
  const { data: models } = useModels();
  const [jobId, setJobId] = useState<number | null>(null);
  const [selectedModel, setSelectedModel] = useState<string | undefined>(undefined);
  const { data: job } = useJob(jobId);
  const [detail, setDetail] = useState<CriterionOut | null>(null);

  const tenderDoc = documents?.find((d) => d.kind === "tender" && d.parse_status !== "pending");

  const compile = useMutation({
    mutationFn: () =>
      api.post<{ job_id: number }>(`/projects/${pid}/rules/compile`, {
        document_id: tenderDoc?.id,
        model: selectedModel ?? null,
      }),
    onSuccess: (r) => setJobId(r.job_id),
    onError: (e: Error) => message.error(e.message),
  });

  const accept = useMutation({
    mutationFn: (id: number) =>
      api.post(`/rules/criteria/${id}/accept`, { reason: "与招标文件原文核对一致" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      message.success("已逐条确认并留痕");
    },
    onError: (e: Error) => message.error(e.message),
  });

  const confirm = useMutation({
    mutationFn: () => api.post(`/rules/packages/${rules!.id}/confirm`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      message.success("规则包已确认");
    },
    onError: (e: Error & { detail?: { blockers?: unknown[] } }) => {
      const blockers = e.detail?.blockers as { message?: string }[] | undefined;
      Modal.error({
        title: "存在阻塞项，不能确认",
        content: (
          <ul>
            {(blockers ?? []).map((b, i) => (
              <li key={i}>{b.message ?? JSON.stringify(b)}</li>
            ))}
          </ul>
        ),
      });
    },
  });

  const freeze = useMutation({
    mutationFn: () => api.post(`/rules/packages/${rules!.id}/freeze`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      message.success("规则包已冻结（此后只读，评分只认该版本快照）");
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (isLoading) return <Spin />;

  const criteria = rules?.criteria ?? [];
  const errors = (rules?.validations ?? []).filter((v) => v.severity === "error");
  const warns = (rules?.validations ?? []).filter((v) => v.severity !== "error");
  const unconfirmed = criteria.filter((c) => c.status !== "verified");

  return (
    <div>
      <Row justify="space-between" align="middle">
        <Col>
          <Typography.Title level={4}>招标文件与评分规则</Typography.Title>
        </Col>
        <Col>
          <Space>
            <Select
              style={{ minWidth: 210 }}
              placeholder="选择模型"
              value={selectedModel ?? models?.default_model}
              onChange={setSelectedModel}
              options={(models?.models ?? []).map((m) => ({
                value: m.id,
                label: `${m.label}（${m.family}）`,
              }))}
            />
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              disabled={!tenderDoc}
              loading={compile.isPending}
              onClick={() => compile.mutate()}
            >
              编译评分规则（AI 抽取 + 确定性校验）
            </Button>
          </Space>
        </Col>
      </Row>

      {!tenderDoc && <Alert type="info" showIcon message="请先在项目工作台/投标人页上传招标文件并等待解析完成" />}

      {job && (
        <Alert
          style={{ marginTop: 12 }}
          type={job.status === "failed" ? "error" : job.status === "succeeded" ? "success" : "info"}
          showIcon
          message={`编译任务：${job.status}（${job.done_steps}/${job.total_steps}）${job.message ?? ""}`}
          description={job.error}
        />
      )}

      {rules && rules.id !== 0 && (
        <>
          <Card size="small" style={{ marginTop: 12 }}>
            <Space wrap>
              <Tag color="blue">版本 v{rules.version}</Tag>
              <Tag>{rules.status === "draft" ? "草稿" : rules.status === "confirmed" ? "已确认" : rules.status === "frozen" ? "已冻结" : rules.status}</Tag>
              {rules.content_hash && <Tag color="purple">内容哈希 {rules.content_hash}</Tag>}
              <Tag>{criteria.length} 个评分项</Tag>
              {unconfirmed.length > 0 && <Tag color="orange">{unconfirmed.length} 项待确认</Tag>}
              <span style={{ marginLeft: "auto" }}>
                <Space>
                  <Button
                    disabled={rules.status !== "draft" || unconfirmed.length > 0}
                    onClick={() => confirm.mutate()}
                  >
                    确认规则包
                  </Button>
                  <Button
                    type="primary"
                    disabled={rules.status !== "confirmed"}
                    onClick={() => freeze.mutate()}
                  >
                    冻结（进入只读）
                  </Button>
                </Space>
              </span>
            </Space>
          </Card>

          {(errors.length > 0 || warns.length > 0) && (
            <Card size="small" title="确定性校验结果（12 条校验器）" style={{ marginTop: 12 }}>
              {errors.map((v) => (
                <Alert
                  key={v.code + (v.subject_id ?? "")}
                  type="error"
                  showIcon
                  style={{ marginBottom: 6 }}
                  message={`[${v.code}] ${v.message}`}
                  description={v.hint}
                />
              ))}
              {warns.map((v) => (
                <Alert
                  key={v.code + (v.subject_id ?? "")}
                  type="warning"
                  showIcon
                  style={{ marginBottom: 6 }}
                  message={`[${v.code}] ${v.message}`}
                />
              ))}
            </Card>
          )}

          <Card title="评分项（逐条确认；系统不提供“全部采纳”）" style={{ marginTop: 12 }}>
            <Table<CriterionOut>
              rowKey="id"
              dataSource={criteria}
              pagination={false}
              size="small"
              columns={[
                { title: "编号", dataIndex: "code", width: 90 },
                {
                  title: "类别",
                  dataIndex: "category",
                  width: 110,
                  render: (c: string) => CATEGORY_LABELS[c] ?? c,
                },
                { title: "名称", dataIndex: "name" },
                {
                  title: "满分",
                  dataIndex: "max_score",
                  width: 70,
                  render: (v: string | null) => v ?? "—",
                },
                {
                  title: "类型",
                  dataIndex: "score_type",
                  width: 90,
                  render: (v: string, row) => (row.is_veto ? <Tag color="red">否决项</Tag> : <Tag>{v}</Tag>),
                },
                {
                  title: "状态",
                  dataIndex: "status",
                  width: 130,
                  render: (s: string) => {
                    const t = STATUS_TAG[s] ?? { color: "default", text: s };
                    return <Tag color={t.color}>{t.text}</Tag>;
                  },
                },
                {
                  title: "原文依据",
                  width: 120,
                  render: (_, row) =>
                    row.source_quote ? (
                      <Tooltip title={row.source_quote}>
                        <Typography.Text type="secondary">
                          第 {row.source_page ?? "?"} 页 {row.source_node_id ? "· 已定位条款" : ""}
                        </Typography.Text>
                      </Tooltip>
                    ) : (
                      <Tag color="red">无引用</Tag>
                    ),
                },
                {
                  title: "操作",
                  width: 180,
                  render: (_, row) => (
                    <Space>
                      <Button size="small" onClick={() => setDetail(row)}>
                        查看/修正
                      </Button>
                      <Button
                        size="small"
                        type="primary"
                        ghost
                        icon={<CheckOutlined />}
                        disabled={row.status !== "verified"}
                        onClick={() => accept.mutate(row.id)}
                      >
                        确认
                      </Button>
                    </Space>
                  ),
                },
              ]}
            />
          </Card>
        </>
      )}

      {(!rules || rules.id === 0) && !isLoading && (
        <Empty style={{ marginTop: 60 }} description="尚未编译规则：上传招标文件后点击上方按钮" />
      )}

      <CriterionModal pid={pid} criterion={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

function CriterionModal({
  pid,
  criterion,
  onClose,
}: {
  pid: string;
  criterion: CriterionOut | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [form] = Form.useForm();
  const [levelsForm] = Form.useForm();
  const [reason, setReason] = useState("");

  const patch = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      api.patch(`/rules/criteria/${criterion!.id}`, { ...values, reason }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      message.success("已修正并留痕（保留模型原值）");
      onClose();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const setLevels = useMutation({
    mutationFn: (levels: { label: string; min: number; max: number; criteria: string }[]) =>
      api.post(`/rules/criteria/${criterion!.id}/levels`, { levels, reason }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      message.success("档位已更新并留痕");
      onClose();
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (!criterion) return null;
  const isSubjective = criterion.score_type === "subjective";

  return (
    <Modal
      open
      width={860}
      title={`评分项 ${criterion.code} ${criterion.name}`}
      onCancel={onClose}
      footer={null}
    >
      <Card size="small" title="招标文件原文（逐字引用）" style={{ marginBottom: 12 }}>
        <Typography.Paragraph style={{ background: "#fffbe6", padding: 8, borderRadius: 4 }}>
          {criterion.source_quote || "（无原文引用——该评分项不得作为评分依据，需补全或删除）"}
        </Typography.Paragraph>
        <Space>
          <Tag>页码：{criterion.source_page ?? "未定位"}</Tag>
          <Tag>条款：{criterion.source_node_id ?? "未定位"}</Tag>
          {criterion.confidence != null && <Tag>抽取置信度 {Math.round(criterion.confidence * 100)}%</Tag>}
        </Space>
      </Card>

      <Form
        form={form}
        layout="inline"
        initialValues={{
          name: criterion.name,
          max_score: criterion.max_score ? Number(criterion.max_score) : undefined,
        }}
      >
        <Form.Item name="name" label="名称" rules={[{ required: true }]}>
          <Input style={{ width: 260 }} />
        </Form.Item>
        <Form.Item name="max_score" label="满分">
          <InputNumber min={0} />
        </Form.Item>
      </Form>

      {isSubjective && (
        <Card size="small" title="评分档位（歧义项必须人工补全分值区间）" style={{ marginTop: 12 }}>
          <Form
            form={levelsForm}
            initialValues={{
              levels: criterion.levels.length
                ? criterion.levels.map((l) => ({
                    label: l.level_label,
                    min: Number(l.score_min),
                    max: Number(l.score_max),
                    criteria: l.description,
                  }))
                : [
                    { label: "优", min: 0, max: 0, criteria: "" },
                    { label: "良", min: 0, max: 0, criteria: "" },
                  ],
            }}
          >
            <Form.List name="levels">
              {(fields) => (
                <List
                  size="small"
                  dataSource={fields}
                  renderItem={(field) => (
                    <List.Item>
                      <Space align="start" style={{ width: "100%" }}>
                        <Form.Item name={[field.name, "label"]} noStyle>
                          <Input style={{ width: 60 }} />
                        </Form.Item>
                        <Form.Item name={[field.name, "min"]} noStyle>
                          <InputNumber placeholder="下限" style={{ width: 80 }} />
                        </Form.Item>
                        <Form.Item name={[field.name, "max"]} noStyle>
                          <InputNumber placeholder="上限" style={{ width: 80 }} />
                        </Form.Item>
                        <Form.Item name={[field.name, "criteria"]} noStyle>
                          <Input.TextArea autoSize={{ minRows: 1, maxRows: 3 }} style={{ width: 480 }} />
                        </Form.Item>
                      </Space>
                    </List.Item>
                  )}
                />
              )}
            </Form.List>
          </Form>
        </Card>
      )}

      <Input.TextArea
        style={{ marginTop: 12 }}
        placeholder="修改理由（必填，将记入留痕；评审期不得擅自新增或改变评分标准）"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        rows={2}
      />
      <Space style={{ marginTop: 12 }}>
        <Button
          type="primary"
          disabled={!reason.trim()}
          onClick={() => {
            const values = form.getFieldsValue();
            patch.mutate({ ...values, max_score: values.max_score ?? null });
          }}
        >
          保存修正
        </Button>
        {isSubjective && (
          <Button
            disabled={!reason.trim()}
            onClick={() => setLevels.mutate(levelsForm.getFieldValue("levels"))}
          >
            保存档位
          </Button>
        )}
        <Tooltip title="新增评分项仅限评审准备阶段，且必须说明依据">
          <Button
            icon={<PlusOutlined />}
            disabled
            onClick={() => message.info("MVP 暂未开放：新增评分项接口已预留")}
          >
            新增评分项
          </Button>
        </Tooltip>
      </Space>
    </Modal>
  );
}
