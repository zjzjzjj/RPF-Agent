import { useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Empty,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  FileTextOutlined,
  FileWordOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  StopOutlined,
  TrophyOutlined,
} from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, reportDocxUrl, reportFormalUrl, reportUrl } from "../api/client";
import { useJobProgress } from "../api/useJobStream";
import {
  useBidders,
  useMatrix,
  useModels,
  useReport,
  useRules,
  useScoreSummary,
  useSelectedRun,
} from "../api/hooks";
import { ExecutionProgressDetails, JobProgress } from "../components/JobProgress";
import BucketCards from "../components/results/BucketCards";
import BidderRankList from "../components/results/BidderRankList";
import ItemReasonDrawer from "../components/results/ItemReasonDrawer";
import ItemTable from "../components/results/ItemTable";
import MatrixPanel from "../components/results/MatrixPanel";
import ReportPanel from "../components/results/ReportPanel";
import type { BidderSummary, SummaryBucket, SummaryItem } from "../types";

type TabKey = "summary" | "matrix" | "report";

const BUCKET_ORDER = ["price", "technical", "commercial", "qualification_compliance"];
const BUCKET_LABEL: Record<string, string> = {
  price: "价格分",
  technical: "技术分",
  commercial: "商务分",
  qualification_compliance: "资格与符合性审查",
};

export default function ScoreResults() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as TabKey) ?? "summary";

  const { data: rules } = useRules(pid);
  const { data: bidders } = useBidders(pid);
  const { data: models } = useModels();
  const { runs, runId, setRunId, run } = useSelectedRun(pid);
  // 单 worker 串行：已有 queued/running 的运行时再点「发起」只会堆一条永远排队的运行
  // （2026-10-20 实证：用户连点产生 run 29/30 双双 queued，取消还不生效）
  const hasActiveRun = runs.some((r) => r.status === "queued" || r.status === "running");
  // 当前选中的运行是否仍在执行：取消按钮必须独立于"本会话是否持有 jobId"——
  // 旧实现只有刚发起评分的那一页才有 JobProgress 的取消入口，刷新/换页后
  // 运行卡在 running 就再也点不到取消（2026-10-21 实证）。
  const currentRun = runs.find((r) => r.id === runId) ?? null;
  const currentRunActive =
    currentRun?.status === "queued" || currentRun?.status === "running";

  // live 轮询只在选中的运行进行中开启：结果页必须渐进刷新，否则横幅冻结在
  // queued、评分格全「未执行」（2026-10-26 run 54 实证）；运行一结束即停。
  const { data: summary, isLoading: summaryLoading } = useScoreSummary(runId, pid, { live: currentRunActive });
  const { data: matrix, isLoading: matrixLoading } = useMatrix(runId, { live: currentRunActive });
  const { data: report, isError: reportError } = useReport(pid, runId);
  const automation = currentRun?.automation ?? summary?.run.automation;
  const managed = automation != null;
  const partialResult = currentRun?.status === "partial";
  const continueErrors = automation?.failure_policy === "continue";
  const formalReady = currentRun != null && (!managed || (
    currentRun.status === "succeeded" && automation.expected > 0 &&
    automation.completed === automation.expected && automation.remaining === 0 && automation.failed === 0
  ));

  const [jobId, setJobId] = useState<number | null>(null);
  const job = useJobProgress(jobId);
  const [selectedModel, setSelectedModel] = useState<string | undefined>(undefined);
  const [drawerItem, setDrawerItem] = useState<SummaryItem | null>(null);

  const rawBidder = params.get("bidder");
  const selectedBidderId = rawBidder ? Number(rawBidder) : null;

  const setTab = (key: string) => {
    const next = new URLSearchParams(params);
    if (key === "summary") next.delete("tab");
    else next.set("tab", key);
    setParams(next, { replace: true });
  };

  const selectBidder = (bidderId: number) => {
    const next = new URLSearchParams(params);
    next.set("bidder", String(bidderId));
    setParams(next, { replace: true });
  };

  // 被剔除的投标人也留在列表里（剔除本身是留痕事实，不能静默消失）
  const listedBidders = summary?.bidders ?? [];
  const selected: BidderSummary | null =
    listedBidders.find((b) => b.bidder_id === selectedBidderId) ??
    listedBidders.find((b) => b.rank === 1) ??
    listedBidders[0] ??
    null;

  const invalidateResults = () => {
    qc.invalidateQueries({ queryKey: ["scoreSummary"] });
    qc.invalidateQueries({ queryKey: ["matrix"] });
    qc.invalidateQueries({ queryKey: ["ranking", pid] });
    qc.invalidateQueries({ queryKey: ["report", pid] });
    qc.invalidateQueries({ queryKey: ["workbench", pid] });
    qc.invalidateQueries({ queryKey: ["projects"] });
  };

  const startRun = useMutation({
    mutationFn: () =>
      api.post<{ run_id: number; job_id: number }>(`/projects/${pid}/runs`, {
        package_id: rules?.id,
        bidder_ids: (bidders ?? []).map((b) => b.id),
        stages: ["objective", "price", "subjective"],
        model: selectedModel ?? null,
        failure_policy: "continue",
      }),
    onSuccess: (r) => {
      setJobId(r.job_id);
      setRunId(r.run_id);
      qc.invalidateQueries({ queryKey: ["runs", pid] });
      message.success("评分运行已发起");
    },
    onError: (e: Error) => message.error(e.message),
  });

  const resumeRun = useMutation({
    mutationFn: async (id: number) => {
      await api.patch(`/runs/${id}/failure-policy`, { failure_policy: "continue" });
      return api.post<{ run_id: number; job_id: number }>(`/runs/${id}/resume`);
    },
    onSuccess: (r) => {
      setJobId(r.job_id);
      setRunId(r.run_id);
      qc.invalidateQueries({ queryKey: ["runs", pid] });
      invalidateResults();
      message.success("已继续评审，已记录故障直接跳过，其余项目继续评分");
    },
    onError: (e: Error) => message.error(e.message),
  });

  // 取消当前运行（与是否在本页发起无关）：后端 POST /runs/{id}/cancel 会找到
  // 对应 evaluate 任务并置 cancel_requested，任务在下一个检查点退出。
  const cancelRun = useMutation({
    mutationFn: (id: number) => api.post<{ ok: boolean }>(`/runs/${id}/cancel`),
    onSuccess: (r) => {
      if (r.ok) {
        message.info("已请求取消：当前这次模型调用结束后生效，未完成项会标记为未定分");
      } else {
        message.warning("该运行已不在执行中（可能刚结束），请刷新");
      }
      qc.invalidateQueries({ queryKey: ["runs", pid] });
      invalidateResults();
    },
    onError: (e: Error) => message.error(e.message || "取消失败"),
  });

  // 任务进入终态后自动刷新结果（此前必须手点「刷新」）
  const prevJobStatus = useRef<string | null>(null);
  useEffect(() => {
    const status = job?.status ?? null;
    if (prevJobStatus.current && prevJobStatus.current !== status) {
      if (status === "succeeded" || status === "failed" || status === "cancelled") {
        invalidateResults();
        qc.invalidateQueries({ queryKey: ["runs", pid] });
      }
    }
    prevJobStatus.current = status;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.status]);

  // 跨会话/跨标签页的运行终态刷新：jobId 是本页 useState，刷新页面即丢，
  // 上面的 job effect 永远不会触发——以 useRuns 1.5s 轮询的 run.status 为准，
  // 非终态翻到终态时拉一次终版（与 live 轮询衔接，覆盖收尾瞬间）。
  const prevRunStatus = useRef<string | null>(null);
  useEffect(() => {
    const status = currentRun?.status ?? null;
    if (prevRunStatus.current && prevRunStatus.current !== status) {
      const wasActive =
        prevRunStatus.current === "queued" || prevRunStatus.current === "running";
      const nowTerminal =
        status === "succeeded" || status === "partial" || status === "failed" || status === "cancelled";
      if (wasActive && nowTerminal) {
        invalidateResults();
      }
    }
    prevRunStatus.current = status;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentRun?.status]);

  const openEvidence = (item: SummaryItem) => {
    if (item.score_record_id != null) {
      window.open(`/p/${pid}/evidence/${item.score_record_id}`, "_blank");
    }
  };

  const bucketsOf = (bidder: BidderSummary): { bucket: SummaryBucket }[] =>
    BUCKET_ORDER.map((key) => ({
      bucket:
        bidder.buckets.find((b) => b.key === key) ??
        ({ key, label: BUCKET_LABEL[key] ?? key, kind: "score", max: null, score: null,
           all_items_score: null, item_count: 0, scored_count: 0, missing_count: 0,
           subjective_count: 0, objective_count: 0,
           verdicts: null, items: [] } as SummaryBucket),
    }));

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          评分结果
        </Typography.Title>
        <Select
          style={{ minWidth: 260 }}
          placeholder="选择评分运行"
          value={runId ?? undefined}
          onChange={(v) => setRunId(v)}
          options={runs.map((r) => ({
            value: r.id,
            label: `运行 #${r.id}（${r.status}）`,
          }))}
        />
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          disabled={rules?.status !== "frozen" || !bidders?.length || hasActiveRun}
          loading={startRun.isPending}
          onClick={() => startRun.mutate()}
        >
          {hasActiveRun ? "已有评分在执行（单任务排队）" : "发起评分运行"}
        </Button>
        <Select
          style={{ minWidth: 190 }}
          placeholder="评审模型"
          value={selectedModel ?? models?.default_model}
          onChange={setSelectedModel}
          options={(models?.models ?? []).map((m) => ({ value: m.id, label: `${m.label}（${m.family}）` }))}
        />
        <Button icon={<ReloadOutlined />} onClick={invalidateResults}>
          刷新
        </Button>
        {automation?.resumable && runId != null && (
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            loading={resumeRun.isPending}
            disabled={hasActiveRun}
            onClick={() => resumeRun.mutate(runId)}
          >
            跳过故障并继续评审
          </Button>
        )}
        {currentRunActive && runId != null && (
          <Popconfirm
            title="取消这次评分运行？"
            description="已写入的评分保留；未完成项标记为未定分。取消后即可重新发起。"
            okText="取消运行"
            cancelText="继续等待"
            okButtonProps={{ danger: true }}
            onConfirm={() => cancelRun.mutate(runId)}
          >
            <Button danger icon={<StopOutlined />} loading={cancelRun.isPending}>
              取消运行
            </Button>
          </Popconfirm>
        )}
        {report && (
          <Tag color={report.status === "final" ? "green" : "orange"} style={{ fontSize: 13, padding: "2px 10px" }}>
            {report.status_label}
          </Tag>
        )}
        {job && (
          <JobProgress
            job={job}
            width={130}
            onCancel={() => api.post(`/jobs/${job.id}/cancel`).catch(() => undefined)}
          />
        )}
      </Space>

      <Space style={{ marginBottom: 12 }} wrap>
        {managed && <Button icon={<FileTextOutlined />} href={`/api/runs/${runId}/results.csv`} disabled={!runId}>
          导出评分结果与故障清单（CSV）
        </Button>}
        <Button
          icon={<FileTextOutlined />}
          href={reportUrl(pid, runId)}
          target="_blank"
          disabled={!runId}
        >
          导出结果汇总（HTML）
        </Button>
        <Button
          icon={<TrophyOutlined />}
          href={reportFormalUrl(pid, runId)}
          target="_blank"
          disabled={!runId || !formalReady}
        >
          正式评审报告（HTML）
        </Button>
        <Button icon={<FileWordOutlined />} href={reportDocxUrl(pid, runId)} disabled={!runId || !formalReady}>
          正式评审报告（Word）
        </Button>
      </Space>

      {automation && (
        <Card size="small" style={{ marginBottom: 12 }} title="全自动评审进度">
          <Progress
            percent={automation.expected ? Math.round((continueErrors ? automation.processed ?? automation.completed : automation.completed) / automation.expected * 1000) / 10 : 0}
            status={currentRun?.status === "failed" ? "exception" : partialResult || automation.execution_finished ? "normal" : formalReady ? "success" : "active"}
          />
          <Typography.Text>
            已评分 {automation.completed} / {automation.expected} 项 · {continueErrors ? `待处理 ${automation.pending ?? automation.remaining}` : `剩余 ${automation.remaining}`} 项
            {automation.failed > 0 ? ` · ${continueErrors ? "故障已跳过" : "技术故障"} ${automation.failed} 项` : ""}
          </Typography.Text>
          <div style={{ marginTop: 8 }}>
            <ExecutionProgressDetails progress={currentRun?.execution_progress ?? automation.execution_progress} />
          </div>
          {currentRunActive && job?.message && (
            <div style={{ marginTop: 8 }}>
              <Typography.Text type="secondary">{job.message}</Typography.Text>
            </div>
          )}
          {automation.last_error && (
            <Alert
              style={{ marginTop: 8 }}
              type={continueErrors ? "warning" : "error"}
              showIcon
              message={partialResult ? "评审已结束，可导出评分结果及跳过项清单" : continueErrors ? "故障已记录，继续评审其余项目" : "自动评审未完成，已保留成功结果"}
              description={`${automation.last_error}。${continueErrors ? "汇总仅合计已评分项，跳过项分值留空，具体原因可在明细或导出清单中查看。" : "故障修复后可恢复评审。"}`}
            />
          )}
        </Card>
      )}

      {rules?.status !== "frozen" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="规则包尚未冻结：请先在「规则库与招标文件」页完成确认与冻结，才能发起评分"
        />
      )}
      {summary && !summary.run.ready && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={
            currentRunActive
              ? // 状态取 useRuns 1.5s 轮询的活值：summary.incomplete_reason 是
                // 服务端取数瞬间拼死的文案，运行中会冻结在旧状态（run 54 里
                // 曾一路显示 queued）。
                `评分运行 #${summary.run.id} 状态为 ${currentRun?.status ?? summary.run.status}，正在评审中，结果持续更新`
              : summary.run.incomplete_reason ?? "评分运行尚未完成，结果可能不完整"
          }
        />
      )}
      {/* incomplete_reason 同时被服务端塞进 warnings——不滤掉就双条同文黄幅。
          提醒默认折叠（2026-11-11 用户要求：十几条暂定分黄幅平铺太占屏）。 */}
      {(() => {
        const warns = (summary?.warnings ?? []).filter(
          (w) => w !== summary?.run.incomplete_reason,
        );
        return warns.length ? (
          <Collapse
            style={{ marginBottom: 12 }}
            items={[
              {
                key: "warnings",
                label: `提醒（${warns.length} 条，需人工复核等）`,
                children: warns.map((w, i) => (
                  <Alert
                    key={i}
                    type="warning"
                    showIcon
                    message={w}
                    style={{ marginBottom: i === warns.length - 1 ? 0 : 8 }}
                  />
                )),
              },
            ]}
          />
        ) : null;
      })()}

      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: "summary",
            label: "评分结果",
            children: summaryLoading ? (
              <Card loading />
            ) : !summary || summary.bidders.length === 0 ? (
              <Card>
                <Empty description="尚无评分结果——请先发起评分运行" />
              </Card>
            ) : (
              <Row gutter={16}>
                <Col xs={24} lg={8}>
                  <div style={{ position: "sticky", top: 16 }}>
                    <BidderRankList
                      bidders={summary.bidders}
                      selectedId={selected?.bidder_id ?? null}
                      onSelect={selectBidder}
                      managed={managed}
                      provisional={managed && !formalReady}
                    />
                  </div>
                </Col>
                <Col xs={24} lg={16}>
                  {!selected ? (
                    <Empty description="请选择一家投标人" />
                  ) : selected.state === "excluded" ? (
                    <Card size="small">
                      <Typography.Title level={5}>{selected.name}</Typography.Title>
                      <Alert
                        type="info"
                        showIcon
                        message="本投标人未参与本次评分"
                        description={selected.excluded_reason ?? "—"}
                      />
                    </Card>
                  ) : (
                    <Space direction="vertical" size={12} style={{ width: "100%" }}>
                      <Card size="small" styles={{ body: { padding: "12px 16px" } }}>
                        <Space wrap size={12}>
                          <Typography.Title level={5} style={{ margin: 0 }}>
                            {selected.rank != null ? `第 ${selected.rank} 名 · ` : ""}
                            {selected.name}
                          </Typography.Title>
                          <Tag color={selected.veto_flag ? "red" : "green"}>
                            {managed ? selected.veto_flag ? "存在不符合项，继续参评" : "AI 自动评审" : selected.veto_flag ? "存在否决建议" : "无否决建议"}
                          </Tag>
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            报价 {selected.bid_price ? Number(selected.bid_price).toLocaleString() : "未录入"} 元
                            {selected.bid_price_is_run_snapshot ? "（本次运行快照）" : "（当前值）"}
                          </Typography.Text>
                          {selected.tie_explanations.map((t, i) => (
                            <Tag key={i} color="blue">
                              {t}
                            </Tag>
                          ))}
                        </Space>
                      </Card>

                      <BucketCards bidder={selected} managed={managed} provisional={managed && !formalReady} />

                      {bucketsOf(selected).map(({ bucket }) => (
                        <Card
                          key={bucket.key}
                          size="small"
                          title={
                            <Space>
                              <span>{BUCKET_LABEL[bucket.key] ?? bucket.label}</span>
                              {bucket.kind === "score" ? (
                                <Typography.Text type="secondary">
                                  {bucket.score ?? "—"}
                                  {bucket.max ? ` / ${bucket.max}` : ""}
                                  {`　${bucket.item_count} 项`}
                                  {bucket.subjective_count > 0 || bucket.objective_count > 0
                                    ? `（主观 ${bucket.subjective_count} · 客观 ${bucket.objective_count}）`
                                    : ""}
                                  {bucket.missing_count > 0 ? `（${bucket.missing_count} 项未定分）` : ""}
                                </Typography.Text>
                              ) : (
                                <Typography.Text type="secondary">
                                  {bucket.item_count} 项，不计分
                                </Typography.Text>
                              )}
                            </Space>
                          }
                        >
                          {bucket.items.length === 0 ? (
                            <Empty description="本次运行未涉及该类评分项" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                          ) : (
                            <ItemTable
                              bucket={bucket}
                              onOpenReason={(item) => setDrawerItem(item)}
                              onOpenEvidence={openEvidence}
                              managed={managed}
                            />
                          )}
                        </Card>
                      ))}

                      {(selected.summary?.missing_count ?? 0) > 0 && (
                        <Alert
                          type="warning"
                          showIcon
                          message={managed ? `${selected.summary?.missing_count} 项尚未自动完成，当前仅展示已完成项合计，尚无最终排名` : `${selected.summary?.missing_count} 项未定分，不计入总分（与排名口径一致）`}
                          description={(selected.summary?.missing_criteria ?? []).join("、")}
                        />
                      )}

                      <Tooltip title={managed ? "评审与证据核验由系统自动完成，可查看每项依据和原页" : "逐项处置（确认 / 改分 / 标记冲突）请在证据核验页完成，本页只读"}>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {managed ? "评审与核验由系统自动完成：点「理由」查看依据，点「核验」查看原页与证据。" : "本页只读：点任一项的「理由」查看完整依据，「核验」进证据页处置。"}
                        </Typography.Text>
                      </Tooltip>
                    </Space>
                  )}
                </Col>
              </Row>
            ),
          },
          {
            key: "matrix",
            label: "完整矩阵",
            children: <MatrixPanel pid={pid} matrix={matrix} isLoading={matrixLoading} managed={managed} />,
          },
          {
            key: "report",
            label: "报告与导出",
            children: <ReportPanel report={report} error={reportError} managed={managed} />,
          },
        ]}
      />

      <ItemReasonDrawer
        pid={pid}
        item={drawerItem}
        open={drawerItem != null}
        onClose={() => setDrawerItem(null)}
        managed={managed}
      />
    </div>
  );
}
