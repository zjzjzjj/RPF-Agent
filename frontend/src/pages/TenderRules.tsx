import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from "antd";
import { PlayCircleOutlined, CheckOutlined, PlusOutlined, ThunderboltOutlined, ReloadOutlined, UploadOutlined, SwapOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useCoverage, useDocuments, useJob, useMatchProposal, useModels, useRules } from "../api/hooks";
import type { CriterionOut, MatchCriterionBrief, MatchPair, ValidationOut } from "../types";
import { CATEGORY_LABELS } from "../types";

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  verified: { color: "green", text: "已确认" },
  needs_input: { color: "orange", text: "待补全" },
  ambiguous: { color: "gold", text: "歧义（原文未给分值）" },
  gap: { color: "red", text: "缺口" },
  conflict: { color: "red", text: "冲突" },
  invalid: { color: "red", text: "非法" },
  needs_signoff: { color: "orange", text: "兜底待签批" },
  retired: { color: "default", text: "已退休" },
};

export default function TenderRules() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const { data: documents } = useDocuments(pid);
  const { data: rules, isLoading } = useRules(pid);
  const { data: models } = useModels();
  const { data: coverage } = useCoverage(rules?.id ?? null);
  const [jobId, setJobId] = useState<number | null>(null);
  const [selectedModel, setSelectedModel] = useState<string | undefined>(undefined);
  const { data: job } = useJob(jobId);
  const [detail, setDetail] = useState<CriterionOut | null>(null);
  const [signoffTarget, setSignoffTarget] = useState<CriterionOut | null>(null);
  const [mergePackageId, setMergePackageId] = useState<number | null>(null);
  const [proposalOpen, setProposalOpen] = useState(false);
  const mergeCompileRef = useRef(false);
  const isTemplatePackage = rules?.origin === "template";
  const canMerge = rules?.origin === "template" || rules?.origin === "merged";

  const tenderDoc = documents?.find(
    (d) => d.kind === "tender" && (d.parse_status === "parsed" || d.parse_status === "degraded"),
  );
  const tenderFailedDoc = documents?.find((d) => d.kind === "tender" && d.parse_status === "failed");
  const tenderFailed = !!tenderFailedDoc;

  const uploadTender = useMutation({
    mutationFn: (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("kind", "tender");
      return api.upload<{ document_id: number; job_id: number }>(`/projects/${pid}/documents`, formData);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["documents", pid] });
      qc.invalidateQueries({ queryKey: ["workbench", pid] });
      message.success("招标文件已上传，解析中…");
    },
    onError: (e: Error) => message.error(e.message),
  });

  const compile = useMutation({
    mutationFn: () => {
      const isMerge = canMerge && !!tenderDoc;
      mergeCompileRef.current = isMerge;
      return api.post<{ job_id: number }>(`/projects/${pid}/rules/compile`, {
        document_id: tenderDoc?.id,
        model: selectedModel ?? null,
        mode: isMerge ? "merge" : "replace",
        merge_target_id: isMerge ? rules!.id : null,
      });
    },
    onSuccess: (r) => setJobId(r.job_id),
    onError: (e: Error) => message.error(e.message),
  });

  // merge 编译任务成功后：全自动模式已在后端自动采纳配对；人工模式进提案抽屉
  useEffect(() => {
    if (
      job &&
      job.status === "succeeded" &&
      mergeCompileRef.current &&
      job.result_json &&
      typeof job.result_json.package_id === "number"
    ) {
      mergeCompileRef.current = false;
      const r = job.result_json as Record<string, unknown>;
      if (r.target_package_id != null) {
        message.success("已自动合并：模板条目已按招标文件覆盖/追加/弃用（留痕可查）");
        qc.invalidateQueries({ queryKey: ["coverage", r.target_package_id] });
      } else {
        setMergePackageId(job.result_json.package_id);
        message.success("合并编译完成，已生成匹配提案，请逐对审核");
        if (typeof r.auto_resolve_error === "string" && r.auto_resolve_error) {
          message.warning(`自动合并未执行：${r.auto_resolve_error}`);
        }
      }
    }
  }, [job]);

  const acceptAll = useMutation({
    mutationFn: (reason: string) =>
      api.post<{ accepted: number }>(`/rules/packages/${rules!.id}/accept-all`, { reason }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      qc.invalidateQueries({ queryKey: ["coverage", rules?.id ?? null] });
      message.success(`已批量采纳 ${r.accepted} 项并留痕`);
    },
    onError: (e: Error) => message.error(e.message),
  });

  const signoffAll = useMutation({
    mutationFn: (reason: string) =>
      api.post<{ signed_off: number }>(`/rules/packages/${rules!.id}/signoff-all`, { reason }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      qc.invalidateQueries({ queryKey: ["coverage", rules?.id ?? null] });
      message.success(`已批量签批 ${r.signed_off} 项并留痕`);
    },
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

  const signoff = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason: string }) =>
      api.post(`/rules/criteria/${id}/signoff`, { reason }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      qc.invalidateQueries({ queryKey: ["coverage", rules?.id] });
      message.success("已签批并留痕（模板兜底）");
      setSignoffTarget(null);
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (isLoading) return <Spin />;

  const criteria = rules?.criteria ?? [];
  const errors = (rules?.validations ?? []).filter((v) => v.severity === "error");
  const warns = (rules?.validations ?? []).filter((v) => v.severity !== "error");
  const unconfirmed = criteria.filter((c) => c.status !== "verified");
  const verifiedCount = criteria.length - unconfirmed.length;
  const pendingSignoff = criteria.filter(
    (c) => c.origin === "template" && c.status === "needs_signoff",
  ).length;

  return (
    <div>
      <Row justify="space-between" align="middle" gutter={[16, 12]}>
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
              icon={canMerge ? <SwapOutlined /> : <PlayCircleOutlined />}
              disabled={!tenderDoc}
              loading={compile.isPending}
              onClick={() => compile.mutate()}
            >
              {canMerge
                ? "编译并生成覆盖提案（merge）"
                : "编译评分规则（AI 抽取 + 确定性校验）"}
            </Button>
            {canMerge && (
              <Button
                icon={<CheckOutlined />}
                disabled={!mergePackageId}
                onClick={() => setProposalOpen(true)}
              >
                匹配提案审核{mergePackageId ? "（有新提案）" : ""}
              </Button>
            )}
          </Space>
        </Col>
      </Row>

      {isTemplatePackage && (
        <Alert
          style={{ marginTop: 12 }}
          type="success"
          showIcon
          message={`已从通用模板实例化规则草稿（${rules.template_id} v${rules.library_version}）`}
          description={
            <Typography.Text type="secondary">
              当前条目均为模板兜底（P5 来源，招标文件未规定），可签批后确认、冻结。
              上传招标文件后点击「编译并与模板合并」，配对将自动采纳：
              模板条目被招标文件条款逐条覆盖、新增条款追加、未规定条目弃用（全程留痕）。
            </Typography.Text>
          }
        />
      )}
      {!tenderDoc && (
        <Alert
          style={{ marginTop: 12 }}
          type="info"
          showIcon
          message={
            isTemplatePackage
              ? "上传招标文件以优化规则（将与模板自动合并）"
              : "请先上传招标文件，解析完成后才能编译规则"
          }
          description={
            <Space direction="vertical" size="small">
              <Typography.Text type="secondary">
                支持 PDF（含扫描件，自动 OCR）/ Word（.docx），系统将自动解析页文本与条款树。
              </Typography.Text>
              {tenderFailedDoc && (
                <Alert
                  type="error"
                  showIcon
                  message="上次上传的文件解析失败，原因如下（更换文件或点「重新解析」重试）："
                  description={
                    <ul style={{ margin: 0, paddingLeft: 18 }}>
                      {(tenderFailedDoc.warnings_json ?? []).map((w, i) => (
                        <li key={i}>
                          {w.code ? `[${w.code}] ` : ""}
                          {w.message}
                        </li>
                      ))}
                    </ul>
                  }
                />
              )}
              <Space>
                <Upload
                  showUploadList={false}
                  accept=".pdf,.docx"
                  beforeUpload={(file) => {
                    uploadTender.mutate(file);
                    return false; // 阻止 antd 默认上传，统一走 mutation
                  }}
                >
                  <Button type="primary" icon={<UploadOutlined />} loading={uploadTender.isPending}>
                    上传招标文件
                  </Button>
                </Upload>
                {tenderFailedDoc && (
                  <Button
                    icon={<ReloadOutlined />}
                    onClick={async () => {
                      const r = await api.post(`/documents/${tenderFailedDoc.id}/reparse`);
                      qc.invalidateQueries({ queryKey: ["documents", pid] });
                      message.success("已重新解析");
                      void r;
                    }}
                  >
                    重新解析
                  </Button>
                )}
              </Space>
            </Space>
          }
        />
      )}

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
            <Space wrap style={{ width: "100%" }}>
              <Tag color="blue">版本 v{rules.version}</Tag>
              <Tag>{rules.status === "draft" ? "草稿" : rules.status === "confirmed" ? "已确认" : rules.status === "frozen" ? "已冻结" : rules.status}</Tag>
              {rules.origin === "template" && (
                <Tag color="geekblue">
                  模板包 {rules.template_id} v{rules.library_version}
                </Tag>
              )}
              {rules.content_hash && <Tag color="purple">内容哈希 {rules.content_hash}</Tag>}
              <Tag>{criteria.length} 个评分项</Tag>
              {unconfirmed.length > 0 && <Tag color="orange">{unconfirmed.length} 项待确认</Tag>}
              {coverage && (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                  <Progress
                    size="small"
                    style={{ width: 180 }}
                    percent={Math.round(
                      ((coverage.document_backed + coverage.template_fallback_signed) /
                        Math.max(1, coverage.total)) *
                        100,
                    )}
                    format={() => `${coverage.document_backed + coverage.template_fallback_signed}/${coverage.total}`}
                  />
                  <Typography.Text type="secondary">文档覆盖</Typography.Text>
                  {pendingSignoff > 0 && (
                    <Tag color="orange">签批待办 {pendingSignoff}</Tag>
                  )}
                </span>
              )}
              <span style={{ marginLeft: "auto" }}>
                <Space>
                  {rules.status === "draft" && verifiedCount > 0 && (
                    <Popconfirm
                      title="全部采纳？"
                      description={`将批量确认 ${verifiedCount} 项已核验条目，理由统一记入留痕。`}
                      okText="全部采纳"
                      onConfirm={() => acceptAll.mutate("批量采纳：与招标文件原文核对一致")}
                      cancelText="取消"
                    >
                      <Button loading={acceptAll.isPending}>全部采纳</Button>
                    </Popconfirm>
                  )}
                  {rules.status === "draft" && pendingSignoff > 0 && (
                    <Popconfirm
                      title="全部签批？"
                      description={`将批量签批 ${pendingSignoff} 项模板兜底条目（启用须有依据，将留痕）。`}
                      okText="全部签批"
                      onConfirm={() =>
                        signoffAll.mutate("批量签批：招标文件未细化该项，按通用评标惯例启用")
                      }
                      cancelText="取消"
                    >
                      <Button loading={signoffAll.isPending}>全部签批</Button>
                    </Popconfirm>
                  )}
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

          <Card title="评分项（支持全部采纳 / 全部签批，逐条留痕）" style={{ marginTop: 12 }}>
            <Table<CriterionOut>
              scroll={{ x: 1000 }}
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
                  title: "来源",
                  dataIndex: "origin",
                  width: 130,
                  render: (_, row) => {
                    if (row.origin === "template") {
                      return row.signoff_json ? (
                        <Tooltip title={`模板兜底（已签批：${row.signoff_json.by} ${row.signoff_json.at.slice(0, 10)}）`}>
                          <Tag color="orange">模板兜底·已签批</Tag>
                        </Tooltip>
                      ) : (
                        <Tag color="gold">模板兜底（待签批）</Tag>
                      );
                    }
                    if (row.origin === "manual") {
                      return <Tag color="purple">人工</Tag>;
                    }
                    return <Tag color="blue">招标文件</Tag>;
                  },
                },
                {
                  title: "状态",
                  dataIndex: "status",
                  width: 120,
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
                      row.origin === "template" ? (
                        <Tag color="orange">模板兜底</Tag>
                      ) : (
                        <Tag color="red">无引用</Tag>
                      )
                    ),
                },
                {
                  title: "操作",
                  width: 220,
                  render: (_, row) => (
                    <Space>
                      <Button size="small" onClick={() => setDetail(row)}>
                        查看/修正
                      </Button>
                      {row.origin === "template" && row.status === "needs_signoff" && (
                        <Button
                          size="small"
                          type="primary"
                          ghost
                          icon={<CheckOutlined />}
                          onClick={() => setSignoffTarget(row)}
                        >
                          签批
                        </Button>
                      )}
                      {row.origin !== "template" && (
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
                      )}
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
      <SignoffModal
        pid={pid}
        criterion={signoffTarget}
        onClose={() => setSignoffTarget(null)}
      />
      <MatchProposalDrawer
        pid={pid}
        mergePackageId={mergePackageId}
        open={proposalOpen}
        onClose={() => setProposalOpen(false)}
      />
    </div>
  );
}

function SignoffModal({
  pid,
  criterion,
  onClose,
}: {
  pid: string;
  criterion: CriterionOut | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [reason, setReason] = useState("");

  if (!criterion) return null;
  return (
    <Modal
      open
      title={`签批模板兜底：${criterion.code} ${criterion.name}`}
      onCancel={onClose}
      onOk={() => {
        const r = reason.trim();
        if (!r) {
          message.warning("请填写签批理由");
          return;
        }
        api
          .post(`/rules/criteria/${criterion.id}/signoff`, { reason: r })
          .then(() => {
            qc.invalidateQueries({ queryKey: ["rules", pid] });
            qc.invalidateQueries({ queryKey: ["coverage", criterion.package_id] });
            message.success("已签批并留痕（模板兜底）");
            onClose();
          })
          .catch((e: Error) => message.error(e.message));
      }}
      okText="签批并留痕"
      cancelText="取消"
      okButtonProps={{ disabled: !reason.trim() }}
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message="模板兜底（P5 来源）"
        description="该条目来自通用模板，招标文件未明文规定。签批表示你确认在本项目启用此通用判据；签批人与理由将永久留痕，并在冻结包与评标报告中标注「模板兜底（已签批）」。"
      />
      <Typography.Paragraph type="secondary">
        模板：{criterion.template_code} · {criterion.library_version ?? ""}
      </Typography.Paragraph>
      <Input.TextArea
        placeholder="签批理由（必填；例如：招标文件未细化该项，按通用评标惯例启用）"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        rows={3}
      />
    </Modal>
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

function MatchProposalDrawer({
  pid,
  mergePackageId,
  open,
  onClose,
}: {
  pid: string;
  mergePackageId: number | null;
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { data: proposal, isLoading } = useMatchProposal(open ? mergePackageId : null);
  const [checkedPairs, setCheckedPairs] = useState<Set<string>>(new Set());
  const [dropIds, setDropIds] = useState<Set<number>>(new Set());
  const [appendIds, setAppendIds] = useState<Set<number>>(new Set());
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // 提案加载后：全部配对预勾选（仍需逐对过目，点击提交才生效）
  useEffect(() => {
    if (proposal) {
      setCheckedPairs(new Set(proposal.pairs.map((p) => p.pair_id)));
    }
  }, [proposal]);

  const toggleDrop = (id: number) => {
    setDropIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const toggleAppend = (id: number) => {
    setAppendIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const decisionCount = checkedPairs.size + dropIds.size + appendIds.size;

  const submit = async () => {
    if (!proposal || !mergePackageId) return;
    const r = reason.trim();
    if (!r) {
      message.warning("请填写处置理由（将写入每条留痕）");
      return;
    }
    const decisions: Record<string, unknown>[] = [];
    for (const p of proposal.pairs) {
      if (checkedPairs.has(p.pair_id)) {
        decisions.push({ action: "override", pair_id: p.pair_id, reason: r });
      }
    }
    for (const id of dropIds) {
      decisions.push({ action: "drop_template", template_criterion_id: id, reason: r });
    }
    for (const id of appendIds) {
      decisions.push({ action: "append_document", document_criterion_id: id, reason: r });
    }
    if (!decisions.length) {
      message.warning("请至少选择一个处置动作");
      return;
    }
    setSubmitting(true);
    try {
      const res = await api.post<{ target_package_id: number }>(
        `/rules/packages/${mergePackageId}/match-proposal/resolve`,
        { decisions },
      );
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      qc.invalidateQueries({ queryKey: ["coverage", res.target_package_id] });
      message.success("匹配处置已生效并留痕");
      onClose();
    } catch (e) {
      message.error((e as Error).message || "处置失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Drawer
      title="匹配提案审核（模板条目 ↔ 招标文件条目）"
      width={960}
      open={open}
      onClose={onClose}
      destroyOnClose
    >
      {isLoading || !proposal ? (
        <Spin />
      ) : (
        <>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={`共 ${proposal.pairs.length} 对匹配建议 · 模板未覆盖 ${proposal.unmatched_template.length} · 编译条目未匹配 ${proposal.unmatched_document.length}`}
            description="确认覆盖后，模板条目升级为招标文件依据（P5→P1–P4）；未覆盖的模板兜底仍需逐条签批。任何覆盖在点击「提交处置」前不生效。"
          />

          {proposal.pairs.length > 0 && (
            <Card size="small" title="匹配建议（勾选 = 确认覆盖）" style={{ marginBottom: 12 }}>
              <Table<MatchPair>
                rowKey="pair_id"
                size="small"
                pagination={false}
                dataSource={proposal.pairs}
                rowSelection={{
                  selectedRowKeys: [...checkedPairs],
                  onChange: (keys) => setCheckedPairs(new Set(keys as string[])),
                }}
                columns={[
                  { title: "模板", dataIndex: ["template", "code"], width: 70 },
                  { title: "模板名称", dataIndex: ["template", "name"], width: 170 },
                  { title: "", width: 24, render: () => "⇄" },
                  { title: "文件", dataIndex: ["document", "code"], width: 70 },
                  { title: "文件名称（原文依据）", dataIndex: ["document", "name"], width: 220 },
                  {
                    title: "相似度",
                    width: 80,
                    render: (_, p) => (
                      <Tag color={p.similarity >= 0.7 ? "green" : "gold"}>
                        {Math.round(p.similarity * 100)}%
                      </Tag>
                    ),
                  },
                  {
                    title: "方式",
                    width: 90,
                    render: (_, p) => <Tag>{p.match_method}</Tag>,
                  },
                ]}
              />
            </Card>
          )}

          {proposal.unmatched_template.length > 0 && (
            <Card size="small" title="未覆盖模板条目（勾选 = 弃用：本项目无此项，不进快照）" style={{ marginBottom: 12 }}>
              <List
                size="small"
                dataSource={proposal.unmatched_template}
                renderItem={(t) => (
                  <List.Item
                    actions={[
                      <Checkbox key="drop" checked={dropIds.has(t.id)} onChange={() => toggleDrop(t.id)}>
                        弃用
                      </Checkbox>,
                    ]}
                  >
                    <List.Item.Meta
                      title={`${t.code} ${t.name}`}
                      description={t.category}
                    />
                  </List.Item>
                )}
              />
            </Card>
          )}

          {proposal.unmatched_document.length > 0 && (
            <Card size="small" title="未匹配编译条目（勾选 = 追加为文件依据条目）" style={{ marginBottom: 12 }}>
              <List
                size="small"
                dataSource={proposal.unmatched_document}
                renderItem={(d) => (
                  <List.Item
                    actions={[
                      <Checkbox key="append" checked={appendIds.has(d.id)} onChange={() => toggleAppend(d.id)}>
                        追加
                      </Checkbox>,
                    ]}
                  >
                    <List.Item.Meta
                      title={`${d.code} ${d.name}`}
                      description={
                        d.source_quote
                          ? `第 ${d.source_page ?? "?"} 页 · ${d.source_quote}`
                          : d.category
                      }
                    />
                  </List.Item>
                )}
              />
            </Card>
          )}

          <Input.TextArea
            placeholder="处置理由（必填；将写入每条覆盖/追加/弃用的留痕）"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={2}
            style={{ marginBottom: 12 }}
          />
          <Button
            type="primary"
            block
            loading={submitting}
            disabled={decisionCount === 0 || !reason.trim()}
            onClick={submit}
          >
            提交处置（{decisionCount} 项）
          </Button>
        </>
      )}
    </Drawer>
  );
}
