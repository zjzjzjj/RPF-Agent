import { Layout, Menu, Typography, Tag, Input, Space, message } from "antd";
import {
  FileTextOutlined,
  TeamOutlined,
  TableOutlined,
  AuditOutlined,
  TrophyOutlined,
  HomeOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { Link, Route, Routes, useLocation, useParams } from "react-router-dom";
import { useState } from "react";
import ProjectList from "./pages/ProjectList";
import ProjectWorkbench from "./pages/ProjectWorkbench";
import TenderRules from "./pages/TenderRules";
import Bidders from "./pages/Bidders";
import ScoreMatrix from "./pages/ScoreMatrix";
import EvidenceWorkbench from "./pages/EvidenceWorkbench";
import ReviewQueue from "./pages/ReviewQueue";
import RankingReport from "./pages/RankingReport";
import { getActor, setActor } from "./api/client";

const { Sider, Content, Header } = Layout;

function ProjectShell({ children }: { children: React.ReactNode }) {
  const { pid = "" } = useParams();
  const location = useLocation();
  const [actor, setActorState] = useState(getActor());

  const items = [
    { key: `/p/${pid}`, icon: <HomeOutlined />, label: <Link to={`/p/${pid}`}>项目工作台</Link> },
    { key: `/p/${pid}/rules`, icon: <FileTextOutlined />, label: <Link to={`/p/${pid}/rules`}>招标文件与规则</Link> },
    { key: `/p/${pid}/bidders`, icon: <TeamOutlined />, label: <Link to={`/p/${pid}/bidders`}>投标人管理</Link> },
    { key: `/p/${pid}/matrix`, icon: <TableOutlined />, label: <Link to={`/p/${pid}/matrix`}>评分矩阵</Link> },
    { key: `/p/${pid}/review`, icon: <AuditOutlined />, label: <Link to={`/p/${pid}/review`}>复核队列</Link> },
    { key: `/p/${pid}/report`, icon: <TrophyOutlined />, label: <Link to={`/p/${pid}/report`}>排名与报告</Link> },
  ];
  const selected = items
    .map((i) => i.key)
    .filter((k) => location.pathname === k || location.pathname.startsWith(k + "/"))
    .sort((a, b) => b.length - a.length)[0];

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider width={220} theme="light" style={{ borderRight: "1px solid #eee" }}>
        <div style={{ padding: "16px 20px" }}>
          <Link to="/">
            <Typography.Text strong style={{ fontSize: 16, color: "#1f4e79" }}>
              RPF Agent
            </Typography.Text>
          </Link>
          <div>
            <Tag color="blue" style={{ marginTop: 6 }}>
              {pid}
            </Tag>
          </div>
        </div>
        <Menu mode="inline" selectedKeys={selected ? [selected] : []} items={items} />
      </Sider>
      <Layout>
        <Header
          style={{
            background: "#fff",
            borderBottom: "1px solid #eee",
            display: "flex",
            alignItems: "center",
            justifyContent: "flex-end",
            paddingInline: 20,
            height: 52,
            lineHeight: "52px",
          }}
        >
          <Space>
            <Typography.Text type="secondary">操作者</Typography.Text>
            <Input
              size="small"
              style={{ width: 140 }}
              value={actor}
              prefix={<SettingOutlined />}
              onChange={(e) => {
                setActorState(e.target.value);
                setActor(e.target.value);
                message.success("操作者已更新（所有留痕将记录该标识）");
              }}
            />
          </Space>
        </Header>
        <Content style={{ padding: 20, background: "#f5f7fa" }}>{children}</Content>
      </Layout>
    </Layout>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<ProjectList />} />
      <Route
        path="/p/:pid/*"
        element={
          <ProjectShell>
            <Routes>
              <Route path="/" element={<ProjectWorkbench />} />
              <Route path="rules" element={<TenderRules />} />
              <Route path="bidders" element={<Bidders />} />
              <Route path="matrix" element={<ScoreMatrix />} />
              <Route path="evidence/:sid" element={<EvidenceWorkbench />} />
              <Route path="review" element={<ReviewQueue />} />
              <Route path="report" element={<RankingReport />} />
            </Routes>
          </ProjectShell>
        }
      />
    </Routes>
  );
}
