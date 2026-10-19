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
import { CheckOutlined, PlusOutlined, ThunderboltOutlined, ReloadOutlined, UploadOutlined, SwapOutlined, ImportOutlined, SettingOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import {
  useCoverage,
  useDocuments,
  useJob,
  useLawPacks,
  useMatchProposal,
  useMergeRecords,
  useModels,
  useRestoreCriterion,
  useRetireCriterion,
  useRules,
  useTemplates,
  useUseDefaultTemplate,
} from "../api/hooks";
import AutoMatchBanner from "../components/rules/AutoMatchBanner";
import RuleList from "../components/rules/RuleList";
import TemplatePicker from "../components/rules/TemplatePicker";
import type { CriterionOut, MatchCriterionBrief, MatchPair, ValidationOut } from "../types";
import { CATEGORY_LABELS } from "../types";

export default function TenderRules() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const { data: documents } = useDocuments(pid);
  const { data: rules, isLoading } = useRules(pid);
  const { data: models } = useModels();
  const { data: coverage } = useCoverage(rules?.id ?? null);
  const { data: lawPacks } = useLawPacks();
  const { data: templates, isLoading: templatesLoading } = useTemplates();
  const [jobId, setJobId] = useState<number | null>(null);
  const [parseJobId, setParseJobId] = useState<number | null>(null);
  const [matchJobId, setMatchJobId] = useState<number | null>(null);
  const [flow, setFlow] = useState<"idle" | "parsing" | "matching">("idle");
  const [selectedModel, setSelectedModel] = useState<string | undefined>(undefined);
  const { data: job } = useJob(jobId);
  const { data: parseJob } = useJob(parseJobId);
  const { data: matchJob } = useJob(matchJobId);
  const [detail, setDetail] = useState<CriterionOut | null>(null);
  const [signoffTarget, setSignoffTarget] = useState<CriterionOut | null>(null);
  const [toggleTarget, setToggleTarget] = useState<CriterionOut | null>(null);
  const [toggleReason, setToggleReason] = useState("");
  const [mergePackageId, setMergePackageId] = useState<number | null>(null);
  const [proposalOpen, setProposalOpen] = useState(false);
  const [unfreezeOpen, setUnfreezeOpen] = useState(false);
  const [unfreezeReason, setUnfreezeReason] = useState("");
  const [lawOpen, setLawOpen] = useState(false);
  const [lawPackId, setLawPackId] = useState("general-law");
  const [addOpen, setAddOpen] = useState(false);
  const [defaultOpen, setDefaultOpen] = useState(false);
  const [addForm] = Form.useForm();
  const mergeCompileRef = useRef(false);
  const isTemplatePackage = rules?.origin === "template";
  // merge 只能写进草稿包；冻结/已确认时按钮必须禁用（否则后端 resolve 会失败）
  const canMerge =
    (rules?.origin === "template" || rules?.origin === "merged") && rules?.status === "draft";
  // 法规包只能导入到草稿包（confirmed/frozen 会 409；无包时后端会新建草稿）
  const canImportLaw = !rules || rules.status === "none" || rules.status === "draft";
  const canAddCriterion = !!rules && rules.id > 0 && rules.status === "draft";
  // 默认规则入口：无包、或已有草稿模板/合并包
  const canUseDefault =
    !rules ||
    rules.id === 0 ||
    rules.status === "none" ||
    (rules.status === "draft" && (rules.origin === "template" || rules.origin === "merged"));
  const hasNonTemplatePackage =
    !!rules && rules.id > 0 && rules.origin !== "template" && rules.origin !== "merged";

  const useDefault = useUseDefaultTemplate(pid);
  const retireCriterion = useRetireCriterion(pid);
  const restoreCriterion = useRestoreCriterion(pid);
  const { data: mergeRecords } = useMergeRecords(rules?.id ?? null);

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
    onSuccess: (r) => {
      setParseJobId(r.job_id);
      setFlow("parsing");
      qc.invalidateQueries({ queryKey: ["documents", pid] });
      qc.invalidateQueries({ queryKey: ["workbench", pid] });
      message.success("招标文件已上传，解析完成后将自动匹配规则…");
    },
    onError: (e: Error) => message.error(e.message),
  });

  const reparseDoc = useMutation({
    mutationFn: (documentId: number) => api.post(`/documents/${documentId}/reparse`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["documents", pid] });
      qc.invalidateQueries({ queryKey: ["workbench", pid] });
      message.success("已重新提交解析");
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

  // 编译任务终态：无论 replace 还是 merge 都要刷新规则表
  // （此前只在自动合并分支刷新 coverage，replace 后页面仍显示旧包）
  useEffect(() => {
    if (!job) return;
    if (job.status === "failed" || job.status === "cancelled") {
      mergeCompileRef.current = false;
      return;
    }
    if (job.status !== "succeeded") return;
    qc.invalidateQueries({ queryKey: ["rules", pid] });
    qc.invalidateQueries({ queryKey: ["coverage"] });
    qc.invalidateQueries({ queryKey: ["workbench", pid] });
    qc.invalidateQueries({ queryKey: ["mergeRecords"] });
    if (!mergeCompileRef.current) return;
    mergeCompileRef.current = false;
    const r = (job.result_json ?? {}) as Record<string, unknown>;
    if (typeof r.target_package_id === "number") {
      const post = r.post_merge as { errors?: { code: string }[] } | undefined;
      message.success("已自动合并：模板条目已按招标文件覆盖/追加/弃用（留痕可查）");
      if (post?.errors?.length) {
        message.warning(
          `合并后规则不完整：${post.errors.map((e) => e.code).join("、")}，请修正后再确认/冻结`,
        );
      }
    } else if (typeof r.package_id === "number") {
      setMergePackageId(r.package_id as number);
      message.success("合并编译完成，已生成匹配提案，请逐对审核");
      if (typeof r.auto_resolve_error === "string" && r.auto_resolve_error) {
        message.warning(`自动合并未执行：${r.auto_resolve_error}`);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.status, job?.id, pid]);

  // 解析任务终态：成功时跟随后端排队的自动匹配任务
  useEffect(() => {
    if (!parseJob) return;
    if (parseJob.status === "succeeded") {
      qc.invalidateQueries({ queryKey: ["documents", pid] });
      qc.invalidateQueries({ queryKey: ["workbench", pid] });
      const r = (parseJob.result_json ?? {}) as Record<string, unknown>;
      if (r.auto_match === "scheduled" && typeof r.job_id === "number") {
        setMatchJobId(r.job_id as number);
        setFlow("matching");
      } else {
        setFlow("idle");
      }
    } else if (parseJob.status === "failed" || parseJob.status === "cancelled") {
      setFlow("idle");
      message.error(parseJob.error || "招标文件解析失败");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parseJob?.status, parseJob?.id, pid]);

  // 自动匹配任务终态
  useEffect(() => {
    if (!matchJob) return;
    if (matchJob.status === "succeeded") {
      setFlow("idle");
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      qc.invalidateQueries({ queryKey: ["coverage"] });
      qc.invalidateQueries({ queryKey: ["workbench", pid] });
      qc.invalidateQueries({ queryKey: ["mergeRecords"] });
      const r = (matchJob.result_json ?? {}) as Record<string, unknown>;
      const post = r.post_merge as { errors?: unknown[] } | undefined;
      if (post?.errors?.length) {
        message.warning("自动匹配完成，但规则总分/价格公式不完整，请修正后再确认冻结");
      } else if (r.auto_resolved === true) {
        message.success("自动匹配完成：规则已按招标文件更新（变更清单见上方）");
      } else if (typeof r.auto_resolve_error === "string") {
        message.warning(`自动匹配未完成：${r.auto_resolve_error}`);
      }
    } else if (matchJob.status === "failed" || matchJob.status === "cancelled") {
      setFlow("idle");
      message.error(matchJob.error || "自动匹配失败");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matchJob?.status, matchJob?.id, pid]);

  const handleUseDefault = (templateId: string, reason: string) => {
    useDefault.mutate(
      { template_id: templateId, reason: reason.trim() || null },
      {
        onSuccess: (r) => {
          if (r.ok) {
            setDefaultOpen(false);
            message.success(
              r.already_frozen
                ? "默认规则已处于冻结状态，可直接发起评分"
                : `已启用默认规则并冻结（${r.signed_off} 条签批）`,
            );
          } else {
            const codes = r.blockers.map((b) => b.code).filter(Boolean).join("、");
            message.error(`启用未完成：${codes || r.message || "校验未通过"}；请修正后重试`);
          }
        },
        onError: (e: Error) => message.error(e.message),
      },
    );
  };

  const submitToggle = () => {
    if (!toggleTarget) return;
    const target = toggleTarget;
    const isRestore = target.status === "retired";
    if (!toggleReason.trim()) {
      message.warning("请填写理由");
      return;
    }
    const mutation = isRestore ? restoreCriterion : retireCriterion;
    mutation.mutate(
      { id: target.id, reason: toggleReason.trim() },
      {
        onSuccess: () => {
          setToggleTarget(null);
          setToggleReason("");
          message.success(isRestore ? "已启用该评分项" : "已停用该评分项");
        },
        onError: (e: Error) => message.error(e.message),
      },
    );
  };

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

  // 解冻并创建新版本：冻结包复制为可编辑草稿（原包保持只读留痕，历史评分不受影响）
  const newVersion = useMutation({
    mutationFn: (reason: string) =>
      api.post<{ id: number; version: number }>(`/rules/packages/${rules!.id}/versions`, { reason }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      qc.invalidateQueries({ queryKey: ["coverage", r.id] });
      setUnfreezeOpen(false);
      setUnfreezeReason("");
      message.success(`已创建 v${r.version} 草稿（原冻结包保持只读留痕）`);
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

  // 导入仓库内固化的法规规则包（87 条通则，法条逐条核验 fail-closed）
  const importLaw = useMutation({
    mutationFn: (packId: string) =>
      api.post<{ package_id: number; imported: number; created: boolean; skipped_codes: string[] }>(
        `/projects/${pid}/rules/import-law-pack`,
        { pack_id: packId },
      ),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      qc.invalidateQueries({ queryKey: ["coverage", rules?.id] });
      qc.invalidateQueries({ queryKey: ["workbench", pid] });
      setLawOpen(false);
      message.success(
        `法规规则包已导入：新增 ${r.imported} 条${r.skipped_codes.length ? `，跳过 ${r.skipped_codes.length} 条（已存在）` : ""}（${r.created ? "新建草稿包" : "追加进现有草稿"}）`,
      );
    },
    onError: (e: Error) => message.error(e.message),
  });

  // 人工新增评分项（仅草稿包；理由必填，编号自动为 x1、x2…）
  const addCriterion = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      api.post<CriterionOut>(`/rules/packages/${rules!.id}/criteria`, values),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rules", pid] });
      qc.invalidateQueries({ queryKey: ["coverage", rules?.id] });
      setAddOpen(false);
      addForm.resetFields();
      message.success("已新增评分项并留痕（编号 x 系列，主观分项可在表格里继续补档位）");
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (isLoading) return <Spin />;

  const criteria = rules?.criteria ?? [];
  const errors = (rules?.validations ?? []).filter((v) => v.severity === "error");
  const warns = (rules?.validations ?? []).filter((v) => v.severity !== "error");
  const unconfirmed = criteria.filter(
    (c) => c.status !== "verified" && c.status !== "retired",
  );
  const verifiedCount = criteria.length - unconfirmed.length;
  const pendingSignoff = criteria.filter(
    (c) => c.origin === "template" && c.status === "needs_signoff",
  ).length;

  return (
    <div>
      <Row justify="space-between" align="middle" gutter={[16, 12]}>
        <Col>
          <Typography.Title level={4}>规则管理</Typography.Title>
        </Col>
        <Col>
          <Space wrap>
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
            <Tooltip
              title={
                canUseDefault
                  ? "无招标文件时，用内置模板一键启用并冻结（实例化 → 签批 → 确认 → 冻结）"
                  : "当前规则包不能更换默认规则；如需更换请先冻结，再「解冻并创建新版本」"
              }
            >
              <Button
                type="primary"
                icon={<SettingOutlined />}
                disabled={!canUseDefault}
                onClick={() => setDefaultOpen(true)}
              >
                使用默认规则
              </Button>
            </Tooltip>
            <Button
              icon={<ImportOutlined />}
              disabled={!canImportLaw}
              onClick={() => setLawOpen(true)}
            >
              导入法规包
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
            <Button
              icon={<ReloadOutlined />}
              onClick={() => {
                qc.invalidateQueries({ queryKey: ["rules", pid] });
                qc.invalidateQueries({ queryKey: ["coverage"] });
                qc.invalidateQueries({ queryKey: ["mergeRecords"] });
              }}
            >
              刷新
            </Button>
          </Space>
        </Col>
      </Row>

      <div style={{ marginTop: 12 }}>
        <AutoMatchBanner
          tenderDoc={tenderDoc}
          tenderFailedDoc={tenderFailedDoc}
          flow={flow}
          parseJob={parseJob}
          matchJob={matchJob}
          uploading={uploadTender.isPending}
          canRematch={
            !!tenderDoc &&
            (canMerge || !rules || rules.id === 0 || rules.status === "none")
          }
          rematchPending={compile.isPending}
          onUpload={(file) => uploadTender.mutate(file)}
          onReparse={(id) => reparseDoc.mutate(id)}
          onRematch={() => compile.mutate()}
          records={mergeRecords ?? []}
          onRefreshRecords={() => qc.invalidateQueries({ queryKey: ["mergeRecords"] })}
        />
      </div>

      {isTemplatePackage && (
        <Alert
          style={{ marginTop: 12 }}
          type="success"
          showIcon
          message={`已从通用模板实例化规则草稿（${rules.template_id} v${rules.library_version}）`}
          description={
            <Typography.Text type="secondary">
              当前条目均为模板兜底（P5 来源，招标文件未规定），可签批后确认、冻结。
              上传招标文件后系统会自动匹配并更新规则：
              模板条目被招标文件条款逐条覆盖、新增条款追加、未规定条目弃用（全程留痕）。
            </Typography.Text>
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
                  {rules.status === "frozen" && (
                    <Button icon={<SwapOutlined />} loading={newVersion.isPending}
                      onClick={() => setUnfreezeOpen(true)}>
                      解冻并创建新版本
                    </Button>
                  )}
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

          <div style={{ marginTop: 12 }}>
            <Space style={{ marginBottom: 8 }}>
              <Tooltip title="仅草稿包可新增；必须说明依据（评审期不得擅自新增标准）">
                <Button
                  icon={<PlusOutlined />}
                  disabled={!canAddCriterion}
                  onClick={() => setAddOpen(true)}
                >
                  新增评分项
                </Button>
              </Tooltip>
              {!canAddCriterion && rules && rules.status !== "none" && (
                <Typography.Text type="secondary">
                  当前规则包状态为「{rules.status}」，仅草稿可新增评分项
                </Typography.Text>
              )}
            </Space>
            {/* 解冻弹层 */}
            <Modal
              open={unfreezeOpen}
              title={`解冻并创建新版本（当前 v${rules.version}）`}
              onCancel={() => setUnfreezeOpen(false)}
              okText="创建新草稿"
              okButtonProps={{ disabled: !unfreezeReason.trim(), loading: newVersion.isPending }}
              onOk={() => newVersion.mutate(unfreezeReason.trim())}
            >
              <Typography.Text type="secondary">
                将把当前冻结包复制一份可编辑草稿（条目、签批与覆盖记录全部沿用）；
                原冻结包保持只读留痕，历史评分结果不受影响。创建新版本必须填写理由。
              </Typography.Text>
              <Input.TextArea
                rows={2}
                style={{ marginTop: 12 }}
                placeholder="解冻/修订理由（必填，记入留痕）"
                value={unfreezeReason}
                onChange={(e) => setUnfreezeReason(e.target.value)}
              />
            </Modal>
            <RuleList
              criteria={criteria}
              packageStatus={rules.status}
              onEdit={(c) => setDetail(c)}
              onSignoff={(c) => setSignoffTarget(c)}
              onAccept={(c) => accept.mutate(c.id)}
              onToggle={(c) => {
                setToggleTarget(c);
                setToggleReason("");
              }}
              pendingId={
                retireCriterion.isPending || restoreCriterion.isPending
                  ? (toggleTarget?.id ?? null)
                  : null
              }
            />
          </div>
        </>
      )}

      {(!rules || rules.id === 0) && !isLoading && (
        <Empty
          style={{ marginTop: 40 }}
          description="尚无评分规则：上传招标文件自动匹配，或点「使用默认规则」选择内置模板直接评分"
        />
      )}

      <TemplatePicker
        open={defaultOpen}
        loading={templatesLoading}
        submitting={useDefault.isPending}
        hasNonTemplatePackage={hasNonTemplatePackage}
        templates={templates ?? []}
        onCancel={() => setDefaultOpen(false)}
        onPick={handleUseDefault}
      />

      <Modal
        open={toggleTarget != null}
        title={toggleTarget?.status === "retired" ? "启用评分项" : "停用评分项"}
        onCancel={() => setToggleTarget(null)}
        okText={toggleTarget?.status === "retired" ? "启用" : "停用"}
        okButtonProps={{
          disabled: !toggleReason.trim(),
          loading: retireCriterion.isPending || restoreCriterion.isPending,
        }}
        onOk={submitToggle}
      >
        <Typography.Paragraph>
          {toggleTarget?.code} {toggleTarget?.name}
        </Typography.Paragraph>
        {toggleTarget && !toggleTarget.is_veto && toggleTarget.max_score != null && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 8 }}
            message={
              toggleTarget.status === "retired"
                ? `启用后总分将增加 ${toggleTarget.max_score} 分`
                : `停用后该评分项退出计分，总分将减少 ${toggleTarget.max_score} 分`
            }
            description="总分变化后需通过「总分=100」校验才能确认/冻结；可继续启用其它项或调整分值。"
          />
        )}
        <Input.TextArea
          rows={2}
          value={toggleReason}
          onChange={(e) => setToggleReason(e.target.value)}
          placeholder="理由（必填，记入留痕）"
        />
      </Modal>

      <CriterionModal
        key={detail?.id ?? "none"}
        pid={pid}
        criterion={detail}
        onClose={() => setDetail(null)}
      />
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

      {/* 导入法规包（仓库内固化包，87 条通则） */}
      <Modal
        open={lawOpen}
        title="导入法规规则包"
        onCancel={() => setLawOpen(false)}
        okText="导入"
        okButtonProps={{ loading: importLaw.isPending }}
        onOk={() => importLaw.mutate(lawPackId)}
      >
        <Typography.Paragraph type="secondary" style={{ marginTop: 0 }}>
          仓库内固化的法规通用评审规则（形式 / 资格 / 响应性 / 否决四组），
          依据《招标投标法》《实施条例》、30 号令、87 号令等编制，法条引用逐条核验
          （核验不过直接拒绝导入）。导入后为 <Tag color="purple">法规来源</Tag>
          <Tag color="blue">P0</Tag>，免签批；error 级校验只在确认/冻结时阻断。
        </Typography.Paragraph>
        <Select
          style={{ width: "100%" }}
          value={lawPackId}
          onChange={setLawPackId}
          options={(lawPacks ?? []).map((p) => ({
            value: p.id,
            label: `${p.name}（${p.rule_count} 条 · v${p.version}）`,
          }))}
        />
        {!canImportLaw && (
          <Alert
            style={{ marginTop: 12 }}
            type="warning"
            showIcon
            message="当前规则包已确认或冻结，不能直接导入"
            description="请先「解冻并创建新版本」得到可编辑草稿，再导入法规包。"
          />
        )}
      </Modal>

      {/* 新增评分项（仅草稿包） */}
      <Modal
        open={addOpen}
        title="新增评分项"
        onCancel={() => setAddOpen(false)}
        okText="新增并留痕"
        okButtonProps={{ loading: addCriterion.isPending }}
        onOk={() => addForm.submit()}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="仅限评审准备阶段，且必须说明依据"
          description="新增条目编号自动为 x1、x2…，来源标为「人工」；主观分项保存后可在评分项表格里补录档位。"
        />
        <Form
          form={addForm}
          layout="vertical"
          onFinish={(values) =>
            addCriterion.mutate({
              ...values,
              max_score: values.max_score ?? null,
              is_veto: values.is_veto ?? false,
            })
          }
        >
          <Form.Item name="category" label="类别" rules={[{ required: true }]}>
            <Select
              options={Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label }))}
            />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]}>
            <Input placeholder="如：项目实施方案完整性" />
          </Form.Item>
          <Form.Item name="score_type" label="类型" rules={[{ required: true }]}>
            <Select
              options={[
                { value: "subjective", label: "主观分（需档位判据）" },
                { value: "count", label: "计数分（业绩/证书）" },
                { value: "match", label: "判断项（满足即得，不打分）" },
                { value: "veto", label: "否决项（资格/符合性）" },
                { value: "formula", label: "价格分公式" },
              ]}
            />
          </Form.Item>
          <Form.Item name="max_score" label="满分">
            <InputNumber min={0} step={0.5} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="description" label="判据描述">
            <Input.TextArea rows={2} placeholder="原文要求或判据说明" />
          </Form.Item>
          <Form.Item name="is_veto" label="否决项" valuePropName="checked">
            <Checkbox>不符合即建议否决（须评标委员会认定）</Checkbox>
          </Form.Item>
          <Form.Item name="source_quote" label="原文依据（逐字）">
            <Input.TextArea rows={2} placeholder="招标文件原文；留空则该条目需人工核验来源" />
          </Form.Item>
          <Form.Item
            name="reason"
            label="新增理由（必填，记入留痕）"
            rules={[{ required: true, message: "请填写新增依据" }]}
          >
            <Input.TextArea rows={2} placeholder="评审期不得擅自新增标准，须有依据" />
          </Form.Item>
        </Form>
      </Modal>
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
