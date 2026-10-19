import { Alert, Button, Card, Empty, List, Space, Spin, Tag, Tooltip, Typography, Upload } from "antd";
import { InboxOutlined, ReloadOutlined, SyncOutlined } from "@ant-design/icons";
import type { DocumentOut, JobOut, MergeRecord } from "../../types";

const ACTION_META: Record<string, { color: string; text: string }> = {
  override: { color: "blue", text: "覆盖" },
  drop_template: { color: "default", text: "弃用" },
  append_document: { color: "green", text: "追加" },
  keep_template: { color: "orange", text: "保留模板" },
};

const SKIP_REASON: Record<string, string> = {
  llm_not_configured: "未配置模型，已跳过自动匹配（可手动编译）",
  auto_match_disabled: "自动匹配开关已关闭",
  package_not_draft: "规则包已确认/冻结，本次未自动更新；如需更新请先「解冻并创建新版本」",
  already_in_flight: "已有编译/匹配任务在执行，本次未重复触发",
  not_tender: "该文档不是招标文件，未触发自动匹配",
  not_parsed: "文档尚未解析完成",
  auto_match_disabled_by_config: "自动匹配开关已关闭",
};

interface Props {
  tenderDoc?: DocumentOut;
  tenderFailedDoc?: DocumentOut;
  flow: "idle" | "parsing" | "matching";
  parseJob?: JobOut | null;
  matchJob?: JobOut | null;
  uploading: boolean;
  canRematch: boolean;
  rematchPending: boolean;
  onUpload: (file: File) => void;
  onReparse: (docId: number) => void;
  onRematch: () => void;
  records: MergeRecord[];
  onRefreshRecords: () => void;
}

function skipReasonOf(job?: JobOut | null): string | null {
  const r = (job?.result_json ?? {}) as Record<string, unknown>;
  const reason = r.skip_reason;
  return typeof reason === "string" ? reason : null;
}

function postMergeErrors(job?: JobOut | null): { code: string; message: string }[] {
  const r = (job?.result_json ?? {}) as Record<string, unknown>;
  const pm = r.post_merge as { errors?: { code: string; message: string }[] } | undefined;
  return Array.isArray(pm?.errors) ? pm!.errors : [];
}

/** 上传招标文件 → 解析 → 自动匹配规则，并展示变更清单与护栏告警。 */
export default function AutoMatchBanner({
  tenderDoc,
  tenderFailedDoc,
  flow,
  parseJob,
  matchJob,
  uploading,
  canRematch,
  rematchPending,
  onUpload,
  onReparse,
  onRematch,
  records,
  onRefreshRecords,
}: Props) {
  const skip = skipReasonOf(parseJob);
  const errors = postMergeErrors(matchJob);
  const autoResolveError =
    matchJob && (matchJob.result_json as Record<string, unknown> | undefined)?.auto_resolve_error;

  return (
    <Card
      size="small"
      title="招标文件与自动匹配"
      extra={
        <Space size={6}>
          <Upload
            showUploadList={false}
            accept=".pdf,.docx"
            beforeUpload={(file) => {
              onUpload(file);
              return false;
            }}
          >
            <Button size="small" icon={<InboxOutlined />} loading={uploading}>
              {tenderDoc ? "替换招标文件" : "上传招标文件并自动匹配"}
            </Button>
          </Upload>
          <Tooltip title="以当前招标文件重新编译并与现有默认规则合并（需草稿包）">
            <Button
              size="small"
              icon={<ReloadOutlined />}
              disabled={!canRematch}
              loading={rematchPending}
              onClick={onRematch}
            >
              重新匹配
            </Button>
          </Tooltip>
          <Button size="small" icon={<SyncOutlined />} onClick={onRefreshRecords}>
            刷新变更
          </Button>
        </Space>
      }
    >
      {tenderFailedDoc ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message="上次上传的招标文件解析失败"
          description={
            <Space direction="vertical" size={4}>
              <span>{tenderFailedDoc.original_filename}（{tenderFailedDoc.parse_status}）</span>
              <Button size="small" onClick={() => onReparse(tenderFailedDoc.id)}>
                重新解析
              </Button>
            </Space>
          }
        />
      ) : !tenderDoc ? (
        <Typography.Text type="secondary">
          上传招标文件后，系统会自动解析并按评分办法匹配、更新现有规则；
          没有招标文件也可以直接「使用默认规则」完成评分。
        </Typography.Text>
      ) : (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <Space wrap>
            <Tag color={tenderDoc.parse_status === "parsed" ? "green" : "orange"}>
              {tenderDoc.original_filename}（{tenderDoc.parse_status}，
              {tenderDoc.page_count ?? "?"} 页）
            </Tag>
            {flow === "parsing" && (
              <Tag color="blue">
                <Spin size="small" /> 解析中 {parseJob?.message ?? ""}
              </Tag>
            )}
            {flow === "matching" && (
              <Tag color="purple">
                <Spin size="small" /> 自动匹配规则中 {matchJob?.message ?? ""}
              </Tag>
            )}
            {flow === "idle" && matchJob?.status === "succeeded" && (
              <Tag color="green">自动匹配已完成</Tag>
            )}
            {skip && <Tag color="default">{SKIP_REASON[skip] ?? skip}</Tag>}
          </Space>

          {errors.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message="自动匹配后规则不完整，需修正后才能确认/冻结"
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {errors.map((e) => (
                    <li key={e.code}>
                      <Tag color="red">{e.code}</Tag>
                      {e.message}
                    </li>
                  ))}
                </ul>
              }
            />
          )}
          {autoResolveError != null && (
            <Alert
              type="warning"
              showIcon
              message={`自动采纳未完成：${String(autoResolveError)}`}
              description="可在下方「匹配提案审核」抽屉中逐条处置。"
            />
          )}

          {records.length > 0 && (
            <List
              size="small"
              header={
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  最近一次匹配的规则变更（{records.length} 条）
                </Typography.Text>
              }
              dataSource={records.slice(0, 50)}
              locale={{ emptyText: <Empty description="暂无变更记录" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
              renderItem={(r) => {
                const meta = ACTION_META[r.action] ?? { color: "default", text: r.action };
                return (
                  <List.Item style={{ padding: "4px 0" }}>
                    <Space size={6} wrap style={{ fontSize: 12 }}>
                      <Tag color={meta.color}>{meta.text}</Tag>
                      <span>{r.document.code ?? r.template.code ?? "—"}</span>
                      <span>{r.document.name ?? r.template.name ?? "—"}</span>
                      {r.template.name && r.document.name && r.template.name !== r.document.name && (
                        <Typography.Text type="secondary">
                          （原：{r.template.code} {r.template.name}）
                        </Typography.Text>
                      )}
                      {r.match_method && <Tag>{r.match_method}</Tag>}
                      {r.similarity && <Tag>相似度 {r.similarity}</Tag>}
                      {r.document_page != null && <Tag>第 {r.document_page} 页</Tag>}
                    </Space>
                  </List.Item>
                );
              }}
            />
          )}
        </Space>
      )}
    </Card>
  );
}
