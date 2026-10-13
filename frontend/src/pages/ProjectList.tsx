import { useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Pagination,
  Popconfirm,
  Segmented,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Tooltip,
} from "antd";
import {
  ArrowRightOutlined,
  AuditOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  FileProtectOutlined,
  FolderOpenOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  TableOutlined,
  AppstoreOutlined,
} from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useProjects, useTemplates } from "../api/hooks";
import DashboardMetrics from "../components/DashboardMetrics";
import {
  formatDate,
  formatPrice,
  isActiveProject,
  PROJECT_STATUS,
  projectStatus,
  projectTimestamp,
} from "../utils/project";
import type { Project } from "../types";

export default function ProjectList() {
  const { data, isLoading, isError, error, refetch, isFetching } =
    useProjects();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [view, setView] = useState<string | number>(() =>
    window.matchMedia("(max-width: 767px)").matches ? "grid" : "table",
  );
  const [page, setPage] = useState(1);
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const { data: templates } = useTemplates();
  const pendingTemplate = useRef<string | undefined>(undefined);
  const projects = data ?? [];
  const active = projects.filter(isActiveProject).length;
  const reviewing = projects.filter((p) => p.status === "reviewing").length;
  const completed = projects.filter((p) => p.status === "completed").length;
  const filtered = useMemo(
    () =>
      (data ?? [])
        .filter((p) => {
          const query = search.trim().toLocaleLowerCase();
          const match = [p.name, p.id, p.project_code, p.purchaser].some((v) =>
            v?.toLocaleLowerCase().includes(query),
          );
          const group =
            filter === "all" ||
            (filter === "active" && isActiveProject(p)) ||
            (filter === "reviewing" && p.status === "reviewing") ||
            (filter === "completed" && p.status === "completed");
          return (
            match &&
            group &&
            (statusFilter === "all" || p.status === statusFilter)
          );
        })
        .sort(
          (a, b) =>
            projectTimestamp(b.updated_at) - projectTimestamp(a.updated_at),
        ),
    [data, search, filter, statusFilter],
  );
  const currentPage = Math.min(
    page,
    Math.max(1, Math.ceil(filtered.length / 8)),
  );

  const create = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      api.post<Project>("/projects", {
        ...values,
        name: String(values.name).trim(),
        project_code:
          typeof values.project_code === "string"
            ? values.project_code.trim() || null
            : null,
        purchaser:
          typeof values.purchaser === "string"
            ? values.purchaser.trim() || null
            : null,
      }),
    onSuccess: async (project) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      const tid = pendingTemplate.current;
      pendingTemplate.current = undefined;
      if (tid) {
        try {
          // S0：创建成功即从通用模板实例化评分规则草稿（同步、确定性）
          await api.post(`/projects/${project.id}/rules/instantiate`, {
            template_id: tid,
          });
          message.success("项目已创建，已从通用模板实例化评分规则草稿");
        } catch (e) {
          message.error(
            (e as Error).message || "项目已创建，但模板实例化失败，请稍后重试",
          );
        }
      } else {
        message.success("评标项目已创建");
      }
      setOpen(false);
      form.resetFields();
      navigate(`/p/${project.id}`);
    },
    onError: (e: Error) =>
      message.error(e.message || "创建项目失败，请稍后重试"),
  });

  const remove = useMutation({
    mutationFn: (pid: string) => api.del<{ deleted: string }>(`/projects/${pid}`),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      message.success(`项目 ${r.deleted} 已删除`);
    },
    onError: (e: Error) => message.error(e.message || "删除项目失败"),
  });
  const resetFilters = () => {
    setSearch("");
    setFilter("all");
    setStatusFilter("all");
    setPage(1);
  };
  const statusTag = (p: Project) => {
    const status = projectStatus(p.status);
    return <Tag color={status.color}>{status.text}</Tag>;
  };
  const empty = (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={
        projects.length ? "没有符合条件的项目" : "从第一个评标项目开始"
      }
    >
      <p className="empty-hint">
        {projects.length
          ? "试试其他关键词，或清除筛选条件。"
          : "创建项目后，上传招标文件并逐步完成评审。"}
      </p>
      <Button
        type={projects.length ? "default" : "primary"}
        icon={projects.length ? undefined : <PlusOutlined />}
        onClick={projects.length ? resetFilters : () => setOpen(true)}
      >
        {projects.length ? "清除筛选" : "新建评标项目"}
      </Button>
    </Empty>
  );

  return (
    <div className="project-overview">
      <div className="page-heading">
        <div>
          <div className="eyebrow">PROJECT OVERVIEW</div>
          <h1>项目总览</h1>
          <p>从招标规则到评标报告，让每一步评审清晰、有据。</p>
        </div>
        <Button
          type="primary"
          size="large"
          icon={<PlusOutlined />}
          onClick={() => setOpen(true)}
        >
          新建评标项目
        </Button>
      </div>
      <div className="overview-banner">
        <div className="banner-copy">
          <span className="banner-kicker">
            <span className="small-dot" /> AI 辅助评审，人工确认结果
          </span>
          <h2>每一分，都有据可循。</h2>
          <p>以冻结规则为起点，用原文证据连接评分与复核。</p>
          <div className="banner-tags">
            <span>
              <CheckCircleOutlined /> 规则可追溯
            </span>
            <span>
              <FileProtectOutlined /> 证据可核验
            </span>
            <span>
              <AuditOutlined /> 操作有留痕
            </span>
          </div>
        </div>
        <div className="banner-flow" aria-label="评标流程">
          <span className="flow-node">
            <FileProtectOutlined />
            编译规则
          </span>
          <ArrowRightOutlined />
          <span className="flow-node">
            <TableOutlined />
            逐项评分
          </span>
          <ArrowRightOutlined />
          <span className="flow-node">
            <AuditOutlined />
            人工复核
          </span>
        </div>
      </div>
      <DashboardMetrics
        items={[
          {
            label: "全部项目",
            value: data ? projects.length : "—",
            hint: "工作空间中的评标项目",
            icon: <FolderOpenOutlined />,
          },
          {
            label: "进行中",
            value: data ? active : "—",
            hint: "尚未完成或归档",
            icon: <ClockCircleOutlined />,
            tone: "blue",
          },
          {
            label: "人工复核中",
            value: data ? reviewing : "—",
            hint: "当前处于复核阶段的项目",
            icon: <AuditOutlined />,
            tone: "amber",
          },
          {
            label: "已完成",
            value: data ? completed : "—",
            hint: "已完成评标流程的项目",
            icon: <CheckCircleOutlined />,
          },
        ]}
      />
      <Card className="projects-panel">
        <div className="panel-heading">
          <div>
            <h2>
              评标项目{" "}
              <span className="count-badge">
                {data ? projects.length : "—"}
              </span>
            </h2>
            <p>管理项目进度，继续你的评审工作。</p>
          </div>
          <div className="panel-actions">
            <Tooltip title="刷新项目">
              <Button
                icon={<ReloadOutlined spin={isFetching} />}
                aria-label="刷新项目"
                onClick={() => refetch()}
              />
            </Tooltip>
            <Segmented
              value={view}
              onChange={setView}
              options={[
                {
                  value: "table",
                  icon: <TableOutlined />,
                  label: <span className="sr-only">列表视图</span>,
                },
                {
                  value: "grid",
                  icon: <AppstoreOutlined />,
                  label: <span className="sr-only">卡片视图</span>,
                },
              ]}
            />
          </div>
        </div>
        <div className="project-toolbar">
          <Segmented
            className="project-filter"
            value={filter}
            onChange={(v) => {
              setFilter(String(v));
              setPage(1);
            }}
            options={[
              { label: "全部", value: "all" },
              { label: "进行中", value: "active" },
              { label: "复核中", value: "reviewing" },
              { label: "已完成", value: "completed" },
            ]}
          />
          <div className="search-controls">
            <Input
              allowClear
              prefix={<SearchOutlined />}
              placeholder="搜索项目、编号或招标人"
              aria-label="搜索项目"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(1);
              }}
            />
            <Select
              virtual={false}
              aria-label="筛选项目状态"
              value={statusFilter}
              onChange={(v) => {
                setStatusFilter(v);
                setPage(1);
              }}
              options={[
                { value: "all", label: "全部状态" },
                ...Object.entries(PROJECT_STATUS).map(([value, s]) => ({
                  value,
                  label: s.text,
                })),
              ]}
            />
          </div>
        </div>
        {isError ? (
          <Alert
            type="error"
            showIcon
            message="项目列表加载失败"
            description={error.message || "请检查服务连接后重试。"}
            action={
              <Button onClick={() => refetch()} loading={isFetching}>
                重试
              </Button>
            }
          />
        ) : isLoading ? (
          <Skeleton active paragraph={{ rows: 5 }} />
        ) : view === "table" ? (
          <Table<Project>
            className="project-table"
            rowKey="id"
            dataSource={filtered}
            scroll={{ x: 820 }}
            locale={{ emptyText: empty }}
            pagination={{
              current: currentPage,
              pageSize: 8,
              onChange: setPage,
              showSizeChanger: false,
              hideOnSinglePage: true,
              showTotal: (total) => `共 ${total} 个项目`,
            }}
            columns={[
              {
                title: "项目名称 / 编号",
                dataIndex: "name",
                width: 300,
                render: (_, p) => (
                  <div className="project-name-cell">
                    <span className="project-file-icon">
                      <FileProtectOutlined />
                    </span>
                    <div>
                      <Link className="project-name" to={`/p/${p.id}`}>
                        {p.name}
                      </Link>
                      <div className="project-code">
                        {p.project_code || p.id}
                      </div>
                    </div>
                  </div>
                ),
              },
              {
                title: "招标人",
                dataIndex: "purchaser",
                width: 190,
                render: (v) => v || "—",
              },
              {
                title: "最高限价",
                dataIndex: "max_price",
                width: 150,
                render: (_, p) => (
                  <span className="price-value">
                    {formatPrice(p.max_price, p.currency)}
                  </span>
                ),
              },
              {
                title: "项目状态",
                dataIndex: "status",
                width: 155,
                render: (_, p) => statusTag(p),
              },
              {
                title: "最近更新",
                dataIndex: "updated_at",
                width: 120,
                render: formatDate,
              },
              {
                title: "",
                key: "action",
                width: 160,
                render: (_, p) => (
                  <Space>
                    <Link className="project-enter" to={`/p/${p.id}`}>
                      进入项目 <ArrowRightOutlined />
                    </Link>
                    <Popconfirm
                      title="删除该项目？"
                      description="文档、规则、评分与报告数据将一并删除且不可恢复。"
                      okText="删除"
                      okButtonProps={{ danger: true }}
                      cancelText="取消"
                      onConfirm={(e) => {
                        e?.stopPropagation();
                        remove.mutate(p.id);
                      }}
                      onCancel={(e) => e?.stopPropagation()}
                    >
                      <Button
                        danger
                        type="text"
                        size="small"
                        icon={<DeleteOutlined />}
                        aria-label={`删除项目 ${p.name}`}
                        onClick={(e) => e.stopPropagation()}
                      />
                    </Popconfirm>
                  </Space>
                ),
              },
            ]}
          />
        ) : filtered.length ? (
          <>
            <div className="project-card-grid">
              {filtered
                .slice((currentPage - 1) * 8, currentPage * 8)
                .map((p) => (
                  <div key={p.id} className="project-grid-card">
                    <Link to={`/p/${p.id}`} className="project-grid-card-link">
                      <div className="project-grid-top">
                        <span className="project-file-icon">
                          <FileProtectOutlined />
                        </span>
                        {statusTag(p)}
                      </div>
                      <h3>{p.name}</h3>
                      <div className="project-code">{p.project_code || p.id}</div>
                      <dl>
                        <div>
                          <dt>招标人</dt>
                          <dd>{p.purchaser || "—"}</dd>
                        </div>
                        <div>
                          <dt>最高限价</dt>
                          <dd>{formatPrice(p.max_price, p.currency)}</dd>
                        </div>
                      </dl>
                      <div className="project-grid-footer">
                        <span>{formatDate(p.updated_at)} 更新</span>
                        <span>
                          进入项目 <ArrowRightOutlined />
                        </span>
                      </div>
                    </Link>
                    <Popconfirm
                      title="删除该项目？"
                      description="文档、规则、评分与报告数据将一并删除且不可恢复。"
                      okText="删除"
                      okButtonProps={{ danger: true }}
                      cancelText="取消"
                      onConfirm={() => remove.mutate(p.id)}
                    >
                      <Button
                        danger
        type="text"
                        size="small"
                        icon={<DeleteOutlined />}
                        aria-label={`删除项目 ${p.name}`}
                        className="project-grid-delete"
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                        }}
                      />
                    </Popconfirm>
                  </div>
                ))}
            </div>
            <Pagination
              current={currentPage}
              pageSize={8}
              total={filtered.length}
              onChange={setPage}
              showSizeChanger={false}
              hideOnSinglePage
            />
          </>
        ) : (
          empty
        )}
        <div className="panel-footnote">
          <FileProtectOutlined /> 最终评分与否决结果均需评审人员确认。
        </div>
      </Card>
      <Modal
        title="新建评标项目"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        okText="创建项目"
        cancelText="取消"
        confirmLoading={create.isPending}
        closable={!create.isPending}
        maskClosable={!create.isPending}
        keyboard={!create.isPending}
        cancelButtonProps={{ disabled: create.isPending }}
      >
        <p className="modal-intro">
          先填写项目信息。可选择一份通用评标办法模板：创建后直接生成规则草稿
          （逐条签批后即可冻结），无需先上传招标文件。
        </p>
        <Form
          form={form}
          layout="vertical"
          onFinish={(v) => {
            pendingTemplate.current =
              typeof v.base_template_id === "string" && v.base_template_id
                ? v.base_template_id
                : undefined;
            create.mutate(v);
          }}
          disabled={create.isPending}
        >
          <Form.Item
            name="name"
            label="项目名称"
            rules={[
              { required: true, whitespace: true, message: "请输入项目名称" },
            ]}
          >
            <Input
              placeholder="如：某市智慧政务云平台建设项目"
              maxLength={200}
            />
          </Form.Item>
          <Form.Item name="base_template_id" label="评标办法基线模板（可选）">
            <Select
              allowClear
              placeholder="暂不使用（仅上传招标文件编译）"
              options={[
                {
                  value: "",
                  label: "暂不使用（仅上传招标文件编译）",
                },
                ...(templates ?? []).map((t) => ({
                  value: t.template_id,
                  label: `${t.name}（${t.criteria_count} 项）`,
                })),
              ]}
            />
          </Form.Item>
          <Form.Item name="project_code" label="招标编号">
            <Input placeholder="如：ZJZB-2026-0117" />
          </Form.Item>
          <Form.Item name="purchaser" label="招标人">
            <Input placeholder="填写招标单位名称" />
          </Form.Item>
          <Form.Item name="max_price" label="最高限价（元）">
            <InputNumber
              style={{ width: "100%" }}
              min={0}
              precision={2}
              placeholder="填写项目最高限价（选填）"
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
