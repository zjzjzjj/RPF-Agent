import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  App,
  Button,
  Card,
  Progress,
  Space,
  Typography,
  Upload,
} from "antd";
import {
  ArrowRightOutlined,
  CheckCircleOutlined,
  CloudUploadOutlined,
  FileZipOutlined,
} from "@ant-design/icons";
import { useBidders, useHealth, useImportBundle, useJob, useProjectJobs } from "../api/hooks";
import JobProgress from "../components/JobProgress";

interface ImportResult {
  counts?: { bidders?: number; priced?: number; unpriced?: number };
  bidders?: { name: string; price?: { status?: string } }[];
  warnings?: string[];
}

const fmtMb = (bytes: number) => (bytes / 1048576).toFixed(1);

export default function ProjectUpload() {
  const { pid = "" } = useParams();
  const navigate = useNavigate();
  const { message } = App.useApp();

  const { data: jobs } = useProjectJobs(pid, "import_bundle");
  const importJob = jobs?.[0] ?? null;
  const importing = !!importJob && ["queued", "running"].includes(importJob.status);
  const importMut = useImportBundle();
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState<{ loaded: number; total: number } | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const { data: health } = useHealth();

  const { data: bidders } = useBidders(pid);
  const hasData = !!bidders && bidders.length > 0;

  const result = (importJob?.result_json as ImportResult | null) ?? null;
  const unpricedNames =
    result?.bidders?.filter((b) => b.price?.status === "missing").map((b) => b.name) ?? [];

  // 导入完成 → 自动跳规则页
  const [handled, setHandled] = useState(false);
  if (
    !handled &&
    importJob?.status === "succeeded" &&
    !importing &&
    !importMut.isPending
  ) {
    setHandled(true);
    message.success("项目包导入完成，进入规则解析");
    navigate(`/p/${pid}/rules`);
  }

  return (
    <div
      style={{
        maxWidth: 640,
        margin: "48px auto",
        padding: "0 24px",
      }}
    >
      <div style={{ textAlign: "center", marginBottom: 32 }}>
        <Typography.Title level={2} style={{ marginBottom: 8 }}>
          上传项目文件
        </Typography.Title>
        <Typography.Text type="secondary">
          上传项目包（zip），系统自动解析招标文件、建档投标人并抽取报价
        </Typography.Text>
      </div>

      <Card>
        <Upload.Dragger
          accept=".zip"
          maxCount={1}
          showUploadList={false}
          disabled={importing || importMut.isPending}
          beforeUpload={(file) => {
            const limitMb = health?.import_max_zip_mb;
            if (limitMb && file.size > limitMb * 1024 * 1024) {
              const msg = `项目包 ${fmtMb(file.size)} MB，超出大小限制（上限 ${limitMb} MB）`;
              setLocalError(msg);
              message.error(msg);
              return false;
            }
            setLocalError(null);
            setUploading(true);
            setProgress({ loaded: 0, total: file.size });
            importMut.mutate(
              {
                pid,
                file,
                onProgress: (loaded, total) => setProgress({ loaded, total }),
              },
              {
                onError: (e: Error) => {
                  setUploading(false);
                  setProgress(null);
                  message.error(e.message || "导入失败，请检查 zip 文件结构");
                },
                onSuccess: () => {
                  setUploading(false);
                  setProgress(null);
                },
              },
            );
            return false;
          }}
          style={{ padding: "24px 0" }}
        >
          <p className="ant-upload-drag-icon">
            <FileZipOutlined style={{ fontSize: 48, color: "#1677ff" }} />
          </p>
          <p className="ant-upload-text" style={{ fontSize: 16 }}>
            点击或拖拽 zip 文件到此处上传
          </p>
          <p className="ant-upload-hint">
            zip 内含两个顶层文件夹：<b>招标</b>（招标文件 PDF/Word）与
            <b>投标</b>（每个公司一个文件夹，内含开标一览表与投标文件）
          </p>
        </Upload.Dragger>

        {(importing || importMut.isPending || uploading) && (
          <div style={{ marginTop: 16 }}>
            {importMut.isPending && progress ? (
              <Space direction="vertical" style={{ width: "100%" }} size={4}>
                <Progress
                  percent={
                    progress.total
                      ? Math.min(100, Math.round((progress.loaded / progress.total) * 1000) / 10)
                      : 0
                  }
                  status="active"
                />
                <Typography.Text type="secondary">
                  正在上传 {fmtMb(progress.loaded)} / {fmtMb(progress.total)} MB（大文件上传需要几分钟，请勿关闭页面）
                </Typography.Text>
              </Space>
            ) : (
              <JobProgress job={importJob} width={240} />
            )}
          </div>
        )}

        {localError && (
          <Alert
            type="error"
            showIcon
            closable
            style={{ marginTop: 16 }}
            message={localError}
            onClose={() => setLocalError(null)}
          />
        )}

        {importMut.isError && !importMut.isPending && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 16 }}
            message="导入失败"
            description={(importMut.error as Error)?.message || "上传失败，请重试"}
          />
        )}

        {importJob?.status === "failed" && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 16 }}
            message="导入失败"
            description={importJob.error?.split(/\r?\n/, 1)[0] ?? "请检查 zip 文件结构后重试"}
          />
        )}

        {hasData && !importing && !importMut.isPending && importJob?.status !== "failed" && (
          <div style={{ marginTop: 16 }}>
            <Alert
              type="info"
              showIcon
              message={`该项目已有 ${bidders!.length} 家投标人`}
              description="重新上传将覆盖现有数据（已有评分运行时不可覆盖）"
              action={
                <Button
                  size="small"
                  onClick={() => setHandled(false)}
                  disabled={importing || importMut.isPending}
                >
                  重新导入
                </Button>
              }
            />
          </div>
        )}

        {result?.counts && importJob?.status === "succeeded" && (
          <div style={{ marginTop: 16 }}>
            <Alert
              type="success"
              showIcon
              message={
                <Space>
                  <CheckCircleOutlined />
                  <span>
                    导入完成：{result.counts.bidders ?? 0} 家公司 /
                    {result.counts.priced ?? 0} 家抽价成功
                  </span>
                </Space>
              }
              description={
                unpricedNames.length > 0
                  ? `未抽到报价（不参与价格分）：${unpricedNames.join("、")}`
                  : "全部公司报价抽取成功"
              }
            />
          </div>
        )}
      </Card>

      <div style={{ textAlign: "center", marginTop: 24 }}>
        <Button
          type="link"
          icon={<ArrowRightOutlined />}
          onClick={() => navigate(`/p/${pid}/rules`)}
        >
          直接进入规则页
        </Button>
      </div>
    </div>
  );
}
