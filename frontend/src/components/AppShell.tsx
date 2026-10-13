import { useEffect, useState, type ReactNode } from "react";
import {
  App,
  Avatar,
  Breadcrumb,
  Button,
  Drawer,
  Form,
  Grid,
  Input,
  Layout,
  Menu,
  Popover,
  Tooltip,
} from "antd";
import {
  ApartmentOutlined,
  ArrowLeftOutlined,
  CheckCircleOutlined,
  FileProtectOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  HomeOutlined,
  MenuOutlined,
  SettingOutlined,
  TableOutlined,
  TeamOutlined,
  TrophyOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Link, useLocation } from "react-router-dom";
import { getActor, setActor } from "../api/client";
import { useProject } from "../api/hooks";

const NAVIGATION = [
  { path: "", label: "项目工作台", icon: <HomeOutlined /> },
  { path: "/rules", label: "招标文件与规则", icon: <FileTextOutlined /> },
  { path: "/bidders", label: "投标人管理", icon: <TeamOutlined /> },
  { path: "/matrix", label: "评分矩阵", icon: <TableOutlined /> },
  { path: "/report", label: "排名与报告", icon: <TrophyOutlined /> },
];

export default function AppShell({
  pid = "",
  children,
}: {
  pid?: string;
  children: ReactNode;
}) {
  const location = useLocation();
  const screens = Grid.useBreakpoint();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [actorOpen, setActorOpen] = useState(false);
  const [actor, setActorState] = useState(getActor);
  const [form] = Form.useForm<{ actor: string }>();
  const { message } = App.useApp();
  const { data: project } = useProject(pid);
  const current = NAVIGATION.slice()
    .reverse()
    .find(
      (item) =>
        location.pathname === `/p/${pid}${item.path}` ||
        (item.path && location.pathname.startsWith(`/p/${pid}${item.path}/`)),
    );
  const isEvidence = location.pathname.includes("/evidence/");
  const pageName = pid
    ? isEvidence
      ? "证据核验"
      : (current?.label ?? "项目工作台")
    : "项目总览";

  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);
  useEffect(() => {
    document.title = `${pageName} · RPF Agent`;
  }, [pageName]);

  const navigation = (
    <div className="sidebar-inner">
      <Link to="/" className="brand" aria-label="RPF Agent 项目总览">
        <span className="brand-mark">
          <ApartmentOutlined />
        </span>
        <span>
          <strong>
            RPF Agent<span className="brand-dot">.</span>
          </strong>
          <small>智能评标工作空间</small>
        </span>
      </Link>
      <div className="nav-section-label">工作空间</div>
      <Menu
        className="workspace-menu"
        theme="dark"
        mode="inline"
        selectedKeys={pid ? [] : ["projects"]}
        items={[
          {
            key: "projects",
            icon: <FolderOpenOutlined />,
            label: <Link to="/">全部项目</Link>,
          },
        ]}
      />
      <div className="nav-section-label">
        项目评审 <span>WORKFLOW</span>
      </div>
      <Menu
        className="workspace-menu"
        theme="dark"
        mode="inline"
        selectedKeys={
          pid ? [isEvidence ? "/matrix" : (current?.path ?? "")] : []
        }
        items={NAVIGATION.map((item) => ({
          key: item.path,
          icon: item.icon,
          disabled: !pid,
          label: pid ? (
            <Link to={`/p/${pid}${item.path}`}>{item.label}</Link>
          ) : (
            <Tooltip title="打开一个项目后开始评审" placement="right">
              <span>{item.label}</span>
            </Tooltip>
          ),
        }))}
      />
      {pid && (
        <Link to="/" className="back-to-projects">
          <ArrowLeftOutlined /> 返回项目列表
        </Link>
      )}
      <div className="sidebar-bottom">
        <div className="sidebar-note">
          <FileProtectOutlined />
          <strong>每一分，都有据可循</strong>
          <p>原文证据 · 人工复核 · 全程留痕</p>
        </div>
        <div className="sidebar-version">
          <span className="version-dot" />
          RPF Agent <span>评标工作空间</span>
        </div>
      </div>
    </div>
  );

  return (
    <Layout className="app-layout">
      {screens.lg && (
        <Layout.Sider width={236} className="app-sidebar" theme="dark">
          {navigation}
        </Layout.Sider>
      )}
      <Drawer
        placement="left"
        width={260}
        open={!screens.lg && mobileOpen}
        onClose={() => setMobileOpen(false)}
        className="navigation-drawer"
        title="工作空间导航"
      >
        {navigation}
      </Drawer>
      <Layout className="main-layout">
        <Layout.Header className="app-header">
          <div className="header-location">
            {!screens.lg && (
              <Button
                type="text"
                icon={<MenuOutlined />}
                aria-label="打开导航"
                onClick={() => setMobileOpen(true)}
              />
            )}
            <Breadcrumb
              items={[
                { title: <Link to="/">工作空间</Link> },
                ...(pid
                  ? [
                      {
                        title: (
                          <Link to={`/p/${pid}`}>
                            {project?.name ?? "当前项目"}
                          </Link>
                        ),
                      },
                    ]
                  : []),
                { title: pageName },
              ]}
            />
          </div>
          <div className="header-actions">
            <span className="audit-label">
              <CheckCircleOutlined /> 操作全程留痕
            </span>
            <Popover
              trigger="click"
              placement="bottomRight"
              open={actorOpen}
              onOpenChange={(open) => {
                setActorOpen(open);
                if (open) form.setFieldsValue({ actor });
              }}
              title="设置操作者"
              content={
                <Form
                  form={form}
                  layout="vertical"
                  className="actor-form"
                  onFinish={({ actor: value }) => {
                    const name = value.trim();
                    try {
                      setActor(name);
                    } catch {
                      message.error("无法保存操作者，请检查浏览器存储设置");
                      return;
                    }
                    setActorState(name);
                    setActorOpen(false);
                    message.success("操作者已更新，后续操作将使用该标识留痕");
                  }}
                >
                  <p>
                    后续操作将记录此标识，请使用英文标识，如 reviewer-zhang。
                  </p>
                  <Form.Item
                    name="actor"
                    label="操作者标识"
                    rules={[
                      {
                        required: true,
                        whitespace: true,
                        message: "请输入操作者标识",
                      },
                      { max: 64, message: "标识最多 64 个字符" },
                      {
                        pattern: /^[A-Za-z0-9._@-]+$/,
                        message: "请使用字母、数字、点、下划线、@ 或连字符",
                      },
                    ]}
                  >
                    <Input
                      prefix={<UserOutlined />}
                      placeholder="如 reviewer-zhang"
                      maxLength={64}
                      autoComplete="off"
                    />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" block>
                    保存操作者
                  </Button>
                </Form>
              }
            >
              <button
                type="button"
                className="actor-button"
                aria-label={`设置操作者，当前为 ${actor}`}
              >
                <Avatar size={30} icon={<UserOutlined />} />
                <span>{actor}</span>
                <SettingOutlined />
              </button>
            </Popover>
          </div>
        </Layout.Header>
        <Layout.Content className="app-content">
          <div className="page-container">{children}</div>
        </Layout.Content>
        <Layout.Footer className="app-footer">
          <span>RPF Agent · 智能评标工作空间</span>
          <span>规则可追溯，证据可核验，结果可复算</span>
        </Layout.Footer>
      </Layout>
    </Layout>
  );
}
