import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Button, Card, Col, Empty, Form, Input, InputNumber, Modal, Row, Spin, Tag, Typography } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useProjects } from "../api/hooks";
import type { Project } from "../types";

const STATUS_LABELS: Record<string, { text: string; color: string }> = {
  draft: { text: "草稿", color: "default" },
  tender_parsed: { text: "招标文件已解析", color: "blue" },
  rules_draft: { text: "规则草稿", color: "orange" },
  rules_frozen: { text: "规则已冻结", color: "cyan" },
  bids_parsed: { text: "投标文件已解析", color: "blue" },
  evaluating: { text: "评分中", color: "processing" },
  reviewing: { text: "复核中", color: "gold" },
  completed: { text: "已完成", color: "green" },
  archived: { text: "已归档", color: "default" },
};

export default function ProjectList() {
  const { data, isLoading } = useProjects();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const create = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.post<Project>("/projects", values),
    onSuccess: (project) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setOpen(false);
      form.resetFields();
      navigate(`/p/${project.id}`);
    },
  });

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: 24 }}>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Typography.Title level={3} style={{ margin: 0, color: "#1f4e79" }}>
          RPF Agent
        </Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
          新建评标项目
        </Button>
      </Row>
      <Typography.Paragraph type="secondary">
        面向招标人与评审人员的 AI 智能评标系统：读取招标文件、结构化评分细则、解析多家投标文件、逐项评分并给出可核验的原文依据。
      </Typography.Paragraph>

      {isLoading ? (
        <Spin />
      ) : !data?.length ? (
        <Empty description="还没有项目，点击右上角新建" />
      ) : (
        <Row gutter={[16, 16]}>
          {data.map((p) => {
            const status = STATUS_LABELS[p.status] ?? { text: p.status, color: "default" };
            return (
              <Col key={p.id} xs={24} md={12} lg={8}>
                <Link to={`/p/${p.id}`}>
                  <Card hoverable title={p.name} extra={<Tag color={status.color}>{status.text}</Tag>}>
                    <div>
                      <Typography.Text type="secondary">编号：{p.id}</Typography.Text>
                    </div>
                    <div>
                      <Typography.Text type="secondary">招标人：{p.purchaser || "—"}</Typography.Text>
                    </div>
                    <div>
                      <Typography.Text type="secondary">
                        最高限价：{p.max_price ? `${Number(p.max_price).toLocaleString()} 元` : "—"}
                      </Typography.Text>
                    </div>
                  </Card>
                </Link>
              </Col>
            );
          })}
        </Row>
      )}

      <Modal
        title="新建评标项目"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={create.isPending}
      >
        <Form form={form} layout="vertical" onFinish={(v) => create.mutate(v)}>
          <Form.Item name="name" label="项目名称" rules={[{ required: true, message: "请输入项目名称" }]}>
            <Input placeholder="如：某市智慧政务云平台建设项目" />
          </Form.Item>
          <Form.Item name="project_code" label="招标编号">
            <Input placeholder="如：ZJZB-2026-0117" />
          </Form.Item>
          <Form.Item name="purchaser" label="招标人">
            <Input />
          </Form.Item>
          <Form.Item name="max_price" label="最高限价（元）">
            <InputNumber style={{ width: "100%" }} min={0} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
