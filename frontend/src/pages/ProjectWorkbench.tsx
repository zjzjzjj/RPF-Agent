import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Progress,
  Result,
  Skeleton,
  Steps,
  Tag,
} from "antd";
import {
  ArrowRightOutlined,
  AuditOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  FileProtectOutlined,
  FileTextOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  TableOutlined,
  TeamOutlined,
  TrophyOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { useWorkbench } from "../api/hooks";
import DashboardMetrics from "../components/DashboardMetrics";
import { formatPrice, projectStatus } from "../utils/project";

const STATE_LABEL = {
  done: "已完成",
  active: "进行中",
  blocked: "待处理",
  pending: "未开始",
};
const STATE_COLOR = {
  done: "green",
  active: "blue",
  blocked: "orange",
  pending: "default",
};
const STAGE_PATH: Record<string, string> = {
  tender: "/rules",
  rules: "/rules",
  bidders: "/bidders",
  evaluate: "/results",
  review: "/results?tab=report",
  report: "/results?tab=report",
};
const STAGE_ICON: Record<string, ReactNode> = {
  tender: <FileTextOutlined />,
  rules: <LockOutlined />,
  bidders: <TeamOutlined />,
  evaluate: <TableOutlined />,
  review: <AuditOutlined />,
  report: <TrophyOutlined />,
};

export default function ProjectWorkbench() {
  const { pid = "" } = useParams();
  const { data, isLoading, isError, error, refetch, isFetching } =
    useWorkbench(pid);
  if (isLoading)
    return (
      <Card>
        <Skeleton active paragraph={{ rows: 8 }} />
      </Card>
    );
  if (isError || !data)
    return (
      <Result
        status="warning"
        title="项目工作台暂时无法加载"
        subTitle={error?.message ?? "请检查项目是否存在后重试。"}
        extra={
          <Button type="primary" loading={isFetching} onClick={() => refetch()}>
            重新加载
          </Button>
        }
      />
    );

  const firstAction = data.stages.findIndex(
    (s) => s.state === "active" || s.state === "blocked",
  );
  const firstPending = data.stages.findIndex((s) => s.state === "pending");
  const currentIndex =
    firstAction >= 0
      ? firstAction
      : firstPending >= 0
        ? firstPending
        : data.stages.length;
  const nextStage = data.stages[currentIndex];
  const done = data.stages.filter((s) => s.state === "done").length;
  const todo = data.stages.reduce((total, s) => total + s.todo_count, 0);
  const percent = data.stages.length
    ? Math.round((done / data.stages.length) * 100)
    : 0;
  const status = projectStatus(data.project.status);

  return (
    <div className="project-workbench">
      <div className="page-heading">
        <div>
          <div className="eyebrow">PROJECT WORKBENCH</div>
          <h1>{data.project.name}</h1>
          <p>
            {data.project.project_code || data.project.id}
            <span className="meta-divider">/</span>
            {data.project.purchaser || "招标人未填写"}
          </p>
        </div>
        <Tag className="heading-status" color={status.color}>
          {status.text}
        </Tag>
      </div>
      <div className="workbench-banner">
        <div>
          <span className="banner-kicker">
            <span className="small-dot" />
            {nextStage
              ? "继续推进评审流程"
              : data.stages.length
                ? "评审流程已完成"
                : "等待评审流程初始化"}
          </span>
          <h2>
            {nextStage
              ? `下一步：${nextStage.label}`
              : data.stages.length
                ? "评审阶段已全部完成"
                : "准备开始评审"}
          </h2>
          <p>
            {nextStage?.detail ||
              (data.stages.length
                ? "前往排名与报告查看评审结果及完整留痕。"
                : "刷新工作台以查看评审阶段。")}
          </p>
          {nextStage && (
            <Link to={`/p/${pid}${STAGE_PATH[nextStage.key] ?? ""}`}>
              <Button type="primary" icon={<ArrowRightOutlined />}>
                前往{nextStage.label}
              </Button>
            </Link>
          )}
          {!nextStage && data.stages.length > 0 && (
            <Link to={`/p/${pid}/results?tab=report`}>
              <Button type="primary" icon={<ArrowRightOutlined />}>
                查看评分结果
              </Button>
            </Link>
          )}
        </div>
        <div className="workbench-progress">
          <Progress
            type="circle"
            percent={percent}
            size={110}
            strokeColor="#29745a"
            trailColor="#dce9e1"
            strokeWidth={7}
          />
          <span>流程阶段完成度</span>
          <small>
            {done} / {data.stages.length} 个阶段已完成
          </small>
        </div>
      </div>
      <DashboardMetrics
        items={[
          {
            label: "已完成阶段",
            value: (
              <>
                {done}
                <span className="metric-unit"> / {data.stages.length}</span>
              </>
            ),
            hint: "从文件解析到报告生成",
            icon: <CheckCircleOutlined />,
          },
          {
            label: "阶段待办",
            value: todo,
            hint: "各流程阶段待处理事项合计",
            icon: <ClockCircleOutlined />,
            tone: "blue",
          },
          {
            label: "当前阻塞项",
            value: data.blockers.length,
            hint: "处理后继续推进评审",
            icon: <WarningOutlined />,
            tone: "amber",
          },
          {
            label: "最高限价",
            value: (
              <span className="metric-price">
                {formatPrice(data.project.max_price, data.project.currency)}
              </span>
            ),
            hint: "项目设定的报价上限",
            icon: <SafetyCertificateOutlined />,
          },
        ]}
      />
      {data.blockers.length > 0 && (
        <Alert
          className="blocker-alert"
          type="warning"
          showIcon
          message="这些事项需要先处理"
          description={
            <ul>
              {data.blockers.map((b, i) => (
                <li key={i}>{b}</li>
              ))}
            </ul>
          }
        />
      )}
      <Card className="workflow-panel">
        <div className="panel-heading">
          <div>
            <h2>评审流程</h2>
            <p>按阶段推进，在关键节点完成确认与复核。</p>
          </div>
          <span className="section-note">{data.stages.length} 个阶段</span>
        </div>
        <Steps
          className="workflow-steps"
          responsive
          current={currentIndex}
          items={data.stages.map((s) => ({
            title: s.label,
            status:
              s.state === "done"
                ? "finish"
                : s.state === "blocked"
                  ? "error"
                  : s.state === "active"
                    ? "process"
                    : "wait",
            description: STATE_LABEL[s.state],
          }))}
        />
      </Card>
      <div className="section-heading">
        <h2>阶段工作台</h2>
        <span>规则、评分与证据相互连接</span>
      </div>
      <div className="stage-grid">
        {data.stages.map((s, i) => (
          <Card key={s.key} className={`stage-card stage-${s.state}`}>
            <div className="stage-card-top">
              <span className="stage-icon">
                {STAGE_ICON[s.key] ?? <FileProtectOutlined />}
              </span>
              <Tag color={STATE_COLOR[s.state]}>{STATE_LABEL[s.state]}</Tag>
            </div>
            <div className="stage-title">
              <span>{String(i + 1).padStart(2, "0")}</span>
              <h3>{s.label}</h3>
            </div>
            <p>{s.detail}</p>
            <div className="stage-card-footer">
              <span className={s.todo_count ? "stage-todo" : "stage-neutral"}>
                {s.todo_count > 0
                  ? `${s.todo_count} 项待办`
                  : s.state === "done"
                    ? "阶段已完成"
                    : "按流程推进"}
              </span>
              <Link to={`/p/${pid}${STAGE_PATH[s.key] ?? ""}`}>
                进入阶段 <ArrowRightOutlined />
              </Link>
            </div>
          </Card>
        ))}
      </div>
      <Card className="principles-panel">
        <div className="panel-heading">
          <div>
            <h2>
              <SafetyCertificateOutlined /> 评审原则
            </h2>
            <p>在每一个阶段保留可核验、可追溯的依据。</p>
          </div>
        </div>
        <div className="principles-grid">
          <div>
            <TableOutlined />
            <h3>客观分可复算</h3>
            <p>资格、价格与计数类评分由确定性计算产生。</p>
          </div>
          <div>
            <AuditOutlined />
            <h3>评分有据可依</h3>
            <p>先依据原文证据裁决，AI 建议与推理过程全部可查。</p>
          </div>
          <div>
            <SafetyCertificateOutlined />
            <h3>否决可追溯</h3>
            <p>系统提供建议与依据，否决结论由裁决记录（人工或自动）给出并留痕。</p>
          </div>
          <div>
            <FileProtectOutlined />
            <h3>操作全程留痕</h3>
            <p>确认、改分与否决记录操作者、时间和理由。</p>
          </div>
        </div>
      </Card>
    </div>
  );
}
