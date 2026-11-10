import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Input,
  InputNumber,
  List,
  Modal,
  Row,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from "antd";
import { EyeInvisibleOutlined, EyeOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useRuns, useScore, useScoreEvidence } from "../api/hooks";
import EvidencePanel from "../components/EvidencePanel";
import AutoReviewDetails from "../components/AutoReviewDetails";
import { REVIEW_STATUS_LABELS } from "../types";
import { automaticEvidenceEmptyText, evidenceEmptyText, sideFromScoreType, verdictSourceFromWarnings } from "../utils/evidenceText";

export default function EvidenceWorkbench() {
  const { pid = "", sid = "" } = useParams();
  const scoreId = Number(sid);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { data: score, isLoading } = useScore(scoreId);
  const { data: citations } = useScoreEvidence(scoreId);
  const { data: runs, isLoading: runsLoading } = useRuns(pid);
  const currentRun = runs?.find((r) => r.id === score?.run_id);
  const managed = currentRun?.automation != null;
  const [humanScore, setHumanScore] = useState<number | null>(null);
  const [reason, setReason] = useState("");
  const [blindDialog, setBlindDialog] = useState(false);
  const [blindReason, setBlindReason] = useState("");

  const revealed = managed || !!score?.ai_revealed_at;

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["score", scoreId] });
    qc.invalidateQueries({ queryKey: ["matrix"] });
    qc.invalidateQueries({ queryKey: ["reviewQueue", pid] });
    // 评分结果页的三份数据都要跟着复核动作更新
    qc.invalidateQueries({ queryKey: ["scoreSummary"] });
    qc.invalidateQueries({ queryKey: ["ranking", pid] });
    qc.invalidateQueries({ queryKey: ["report", pid] });
  };

  const reveal = useMutation({
    mutationFn: (payload: { blind_skip: boolean; reason: string }) =>
      api.post(`/scores/${scoreId}/reveal`, payload),
    onSuccess: () => {
      invalidate();
      setBlindDialog(false);
      message.success("已解锁 AI 建议（解锁动作已留痕）");
    },
    onError: (e: Error) => message.error(e.message),
  });

  const review = useMutation({
    mutationFn: (payload: { action: string; score?: number | null; reason: string }) =>
      api.post(`/scores/${scoreId}/review`, payload),
    onSuccess: () => {
      invalidate();
      setReason("");
      message.success("复核动作已记录（含操作人、时间与理由）");
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (isLoading || runsLoading || !score) return <Spin />;
  if (!currentRun) {
    return <Alert type="warning" showIcon message="暂时无法确认本项所属运行" description="运行信息加载完成后才能查看评审操作，请刷新后重试。" />;
  }

  const isVeto = score.score_type === "veto";
  const isMatch = score.score_type === "match";
  const isSubjective = score.score_type === "subjective";
  const verdictSource = verdictSourceFromWarnings(score.warnings_json);

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Button onClick={() => navigate(`/p/${pid}/results`)}>← 返回评分结果</Button>
        <Typography.Title level={5} style={{ margin: 0 }}>
          {score.bidder_name} · {score.criterion_code} {score.criterion_name}
        </Typography.Title>
        <Tag color={isVeto ? "red" : isMatch ? "geekblue" : isSubjective ? "purple" : "blue"}>
          {isVeto ? managed ? "符合性结论" : "否决项" : isMatch ? "判断项" : isSubjective ? managed ? "AI 自动评分" : "主观分（AI 仅建议）" : "确定性计算"}
        </Tag>
        <Tag>{managed && score.machine_status === "execution_failed" ? "执行失败，可恢复" : managed && !["auto_confirmed", "not_applicable"].includes(score.review_status) ? "待自动完成" : REVIEW_STATUS_LABELS[score.review_status] ?? score.review_status}</Tag>
        {score.evidence_status === "verified" && <Tag color="green">证据已核验</Tag>}
        {score.evidence_status === "unverified" && <Tag color="orange">{managed ? "证据自动核验中" : "证据待人工核对"}</Tag>}
        {/* 「无证据」警示只给主观项——判定/计算项设计上不产引文，标红是误报 */}
        {score.evidence_status === "none" && isSubjective && !managed && <Tag color="red">无证据</Tag>}
      </Space>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={11}>
          <Card size="small" title="评分项与依据">
            {score.source_quote && (
              <>
                <Typography.Text type="secondary">招标文件原文（第 {score.source_page ?? "?"} 页）</Typography.Text>
                <Typography.Paragraph style={{ background: "#fffbe6", padding: 8, borderRadius: 4 }}>
                  {score.source_quote}
                </Typography.Paragraph>
              </>
            )}
            {score.levels.length > 0 && (
              <>
                <Typography.Text type="secondary">档位判据</Typography.Text>
                <List
                  size="small"
                  dataSource={score.levels}
                  renderItem={(lv) => (
                    <List.Item>
                      <Space align="start">
                        <Tag>{lv.label}</Tag>
                        <span>
                          {lv.min}-{lv.max} 分
                        </span>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {lv.criteria}
                        </Typography.Text>
                      </Space>
                    </List.Item>
                  )}
                />
              </>
            )}
          </Card>

          <Card size="small" title="判定与评分" style={{ marginTop: 12 }}>
            {!managed && !isVeto && (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 8 }}
                message={
                  isSubjective
                    ? "请先给出你的独立评分，再解锁 AI 建议（防锚定：未解锁时系统不返回 AI 分值）"
                    : "该分项由确定性引擎计算，可复算；复核确认后生效"
                }
              />
            )}

            {managed ? (
              <Space direction="vertical" style={{ width: "100%" }}>
                <Alert
                  type={score.machine_status === "execution_failed" ? "error" : "info"}
                  showIcon
                  message={score.machine_status === "execution_failed" ? "自动评审遇到技术故障，已保留进度" : isVeto || isMatch ? score.machine_status === "unverifiable" ? "现有材料无法证实（已评审的外部事实风险结论）" : score.machine_status === "fail" ? "不符合，投标人继续参与计分和排名" : score.review_status === "auto_confirmed" ? "符合" : "待自动完成" : `最终得分：${score.final_score ?? "尚未完成"}${score.final_score == null ? "" : ` / ${score.max_score ?? "—"}`}`}
                  description={score.ai_reasoning}
                />
                {score.warnings_json?.map((w, i) => (w.message || w.formula_explanation) ? (
                  <Typography.Paragraph key={i} style={{ whiteSpace: "pre-wrap" }}>
                    {w.message ?? w.formula_explanation}
                  </Typography.Paragraph>
                ) : null)}
                <Typography.Text type="secondary">本页展示系统评审结果与证据。全部应评项完成后，系统才发布正式报告和最终排名。</Typography.Text>
              </Space>
            ) : isVeto ? (
              <div>
                <Typography.Paragraph>
                  机器判定：
                  <Tag color={score.machine_status === "pass" ? "green" : score.machine_status === "recommend_reject" || score.machine_status === "fail" ? "red" : "orange"}>
                    {score.machine_status === "pass"
                      ? "通过"
                      : score.machine_status === "recommend_reject"
                        ? "建议否决（全自动模式已裁决收敛，见留痕）"
                        : score.machine_status === "fail"
                          ? "不通过（已自动裁决）"
                          : score.machine_status === "conflict_flagged"
                            ? "冲突，需人工裁决"
                            : "待人工"}
                  </Tag>
                </Typography.Paragraph>
                {score.warnings_json?.map((w, i) => (
                  <Alert key={i} type="warning" showIcon message={w.message} style={{ marginBottom: 6 }} />
                ))}
              </div>
            ) : (
              <Space direction="vertical" style={{ width: "100%" }}>
                <Space>
                  <span>你的评分：</span>
                  <InputNumber
                    min={0}
                    max={score.max_score ? Number(score.max_score) : undefined}
                    value={humanScore ?? (score.human_score ? Number(score.human_score) : null)}
                    onChange={(v) => setHumanScore(v)}
                    disabled={!!score.final_score && score.review_status === "human_confirmed"}
                  />
                  <span>/ {score.max_score ?? "?"} 分</span>
                </Space>

                {!revealed && isSubjective && (
                  <Space>
                    <Button
                      icon={<EyeOutlined />}
                      disabled={humanScore == null}
                      onClick={() =>
                        reveal.mutate({ blind_skip: false, reason: "已完成独立评分，解锁 AI 建议" })
                      }
                    >
                      解锁 AI 建议
                    </Button>
                    <Button icon={<EyeInvisibleOutlined />} onClick={() => setBlindDialog(true)}>
                      跳过盲评直接查看
                    </Button>
                  </Space>
                )}

                {revealed && isSubjective && (
                  <Alert
                    type="success"
                    showIcon
                    message={
                      <span>
                        AI 建议：<b>{score.ai_suggested_score ?? "—"}</b> 分
                        {score.ai_level_label ? `（${score.ai_level_label}档）` : ""}
                        {score.ai_confidence != null ? `，置信度 ${Math.round(score.ai_confidence * 100)}%` : ""}
                      </span>
                    }
                    description={
                      <div>
                        <div>{score.ai_reasoning}</div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          AI 建议与推理过程仅供参考与审计；最终分值以裁决记录为准（自动或人工）。
                        </Typography.Text>
                      </div>
                    }
                  />
                )}

                {!isSubjective && score.machine_score != null && (
                  <Typography.Paragraph>
                    确定性计算得分：<b>{score.machine_score}</b> / {score.max_score} 分
                    {score.warnings_json?.map((w) => w.formula_explanation).filter(Boolean).map((f, i) => (
                      <Typography.Paragraph key={i} type="secondary" style={{ fontSize: 12 }}>
                        {f}
                      </Typography.Paragraph>
                    ))}
                  </Typography.Paragraph>
                )}

                {score.warnings_json?.map((w, i) =>
                  w.message ? <Alert key={i} type="warning" showIcon message={w.message} /> : null
                )}
              </Space>
            )}

            {!managed && <>
            <Divider style={{ margin: "12px 0" }} />
            <Input.TextArea
              rows={2}
              placeholder="复核理由（确认/改分/否决均必填，记入留痕）"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
            <Space style={{ marginTop: 8 }} wrap>
              {isVeto ? (
                <>
                  <Button
                    type="primary"
                    disabled={!reason.trim()}
                    loading={review.isPending}
                    onClick={() => review.mutate({ action: "confirm", reason })}
                  >
                    确认通过（不否决）
                  </Button>
                  <Button
                    danger
                    disabled={!reason.trim() || score.machine_status !== "recommend_reject"}
                    onClick={() =>
                      Modal.confirm({
                        title: "确认否决该投标人？",
                        content: "否决为法定评审职责，确认后投标人状态将置为已否决，操作将留痕。",
                        onOk: () => review.mutateAsync({ action: "confirm_reject", reason }),
                      })
                    }
                  >
                    确认否决
                  </Button>
                </>
              ) : (
                <>
                  <Button
                    type="primary"
                    disabled={!reason.trim()}
                    loading={review.isPending}
                    onClick={() =>
                      review.mutate({
                        action: humanScore != null ? "override" : "confirm",
                        score: humanScore,
                        reason,
                      })
                    }
                  >
                    {humanScore != null ? "保存人工分（改分）" : "确认 AI/引擎建议"}
                  </Button>
                  <Button
                    disabled={!reason.trim()}
                    onClick={() => review.mutate({ action: "flag_conflict", reason })}
                  >
                    标记冲突
                  </Button>
                </>
              )}
            </Space>
            <div style={{ marginTop: 8, fontSize: 12, color: "#888" }}>
              每次确认都单独留痕并记入审计（操作人 {score.evaluator_actor ?? "—"}
              {score.confirm_time ? `，${score.confirm_time}` : ""}）；规则包支持"全部采纳/全部签批"批量操作（同样逐条留痕）。
            </div>
            </>}
          </Card>
        </Col>

        <Col xs={24} lg={13}>
          {managed && <AutoReviewDetails metadata={score.auto_review} />}
          <div style={{ marginBottom: 8, textAlign: "right" }}>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => qc.invalidateQueries({ queryKey: ["evidence", scoreId] })}
            >
              刷新证据
            </Button>
          </div>
          <EvidencePanel
            citations={citations ?? []}
            emptyText={managed ? automaticEvidenceEmptyText(score.auto_review) : evidenceEmptyText(sideFromScoreType(score.score_type), verdictSource)}
          />
        </Col>
      </Row>

      {!managed && <Modal
        open={blindDialog}
        title="跳过盲评，直接查看 AI 建议"
        onCancel={() => setBlindDialog(false)}
        onOk={() => reveal.mutate({ blind_skip: true, reason: blindReason })}
        okButtonProps={{ disabled: !blindReason.trim() }}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message="先看 AI 建议可能产生锚定效应，影响评审独立性。跳过盲评必须说明理由（将留痕）。"
        />
        <Input.TextArea
          rows={2}
          placeholder="跳过盲评的理由"
          value={blindReason}
          onChange={(e) => setBlindReason(e.target.value)}
        />
      </Modal>}
    </div>
  );
}
