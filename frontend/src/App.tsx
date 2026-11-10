import { lazy, Suspense } from "react";
import { Button, Card, Result, Skeleton } from "antd";
import { Link, Navigate, Route, Routes, useParams } from "react-router-dom";
import AppShell from "./components/AppShell";
import ProjectList from "./pages/ProjectList";

const ProjectWorkbench = lazy(() => import("./pages/ProjectWorkbench"));
const TenderRules = lazy(() => import("./pages/TenderRules"));
const Bidders = lazy(() => import("./pages/Bidders"));
const ScoreResults = lazy(() => import("./pages/ScoreResults"));
const EvidenceWorkbench = lazy(() => import("./pages/EvidenceWorkbench"));

function NotFound() {
  return (
    <Result
      status="404"
      title="页面不存在"
      subTitle="请返回项目列表，或通过侧栏继续评审。"
      extra={
        <Link to="/">
          <Button type="primary">返回项目列表</Button>
        </Link>
      }
    />
  );
}

/** 旧路由（评分矩阵 / 排名与报告）已并入「评分结果」，保留重定向以免书签失效。 */
function LegacyRedirect({ tab }: { tab: "matrix" | "report" }) {
  const { pid = "" } = useParams();
  return <Navigate to={`/p/${pid}/results?tab=${tab}`} replace />;
}

function ProjectShell() {
  const { pid = "" } = useParams();
  return (
    <AppShell pid={pid}>
      <Suspense
        fallback={
          <Card>
            <Skeleton active paragraph={{ rows: 6 }} />
          </Card>
        }
      >
        <Routes>
          <Route path="/" element={<ProjectWorkbench />} />
          <Route path="rules" element={<TenderRules />} />
          <Route path="bidders" element={<Bidders />} />
          <Route path="results" element={<ScoreResults />} />
          <Route path="evidence/:sid" element={<EvidenceWorkbench />} />
          <Route path="matrix" element={<LegacyRedirect tab="matrix" />} />
          <Route path="report" element={<LegacyRedirect tab="report" />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Suspense>
    </AppShell>
  );
}

export default function App() {
  return (
    <Routes>
      <Route
        path="/"
        element={
          <AppShell>
            <ProjectList />
          </AppShell>
        }
      />
      <Route path="/p/:pid/*" element={<ProjectShell />} />
      <Route
        path="*"
        element={
          <AppShell>
            <NotFound />
          </AppShell>
        }
      />
    </Routes>
  );
}
