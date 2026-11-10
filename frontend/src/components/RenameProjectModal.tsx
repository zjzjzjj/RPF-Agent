import { useEffect } from "react";
import { App, Form, Input, Modal } from "antd";
import { useRenameProject } from "../api/hooks";
import type { Project } from "../types";

/**
 * 重命名评标项目（PATCH /projects/{pid} 的 name）。
 * 受控 Modal + Form.submit（antd 静态 Modal.confirm 的按钮 props 一次性求值，不随闭包更新）。
 */
export default function RenameProjectModal({
  project,
  open,
  onClose,
}: {
  project: Project | null;
  open: boolean;
  onClose: () => void;
}) {
  const [form] = Form.useForm<{ name: string }>();
  const { message } = App.useApp();
  const rename = useRenameProject();

  // 只在「打开/切换项目」时同步一次；project 引用变化（后台 refetch）不重置，
  // 否则弹窗开着时的静默刷新会把用户正在输入的新名覆盖回旧名。
  useEffect(() => {
    if (open && project) form.setFieldsValue({ name: project.name });
  }, [open, project?.id, form]);

  return (
    <Modal
      title="重命名项目"
      open={open}
      onCancel={() => {
        if (!rename.isPending) onClose();
      }}
      onOk={() => form.submit()}
      okText="保存"
      cancelText="取消"
      confirmLoading={rename.isPending}
      closable={!rename.isPending}
      maskClosable={!rename.isPending}
      keyboard={!rename.isPending}
      cancelButtonProps={{ disabled: rename.isPending }}
    >
      <p className="modal-intro">修改项目名称，评审数据与留痕不受影响。</p>
      <Form
        form={form}
        layout="vertical"
        onFinish={(v) => {
          if (!project) return;
          const name = String(v.name).trim();
          if (name === project.name) {
            onClose();
            return;
          }
          rename.mutate(
            { pid: project.id, name },
            {
              onSuccess: () => {
                message.success("项目已重命名");
                onClose();
              },
              onError: (e: Error) =>
                message.error(e.message || "重命名失败，请稍后重试"),
            },
          );
        }}
        disabled={rename.isPending}
      >
        <Form.Item
          name="name"
          label="项目名称"
          rules={[
            { required: true, whitespace: true, message: "请输入项目名称" },
          ]}
        >
          <Input placeholder="输入新的项目名称" maxLength={255} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
