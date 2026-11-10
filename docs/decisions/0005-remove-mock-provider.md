# 0005 — 彻底移除 Mock LLM 提供者

- 状态：已采纳（2026-10-14）
- 关联：0003（full-auto-mode，其"mock 裁决是管路测试"的免责条款因本决策失效）

## 背景

`app/llm/mock.py`（488 行）是随测试搭建的确定性假 LLM：按 `[[TASK:]]` 标记分发到正则抽取 /
LCS 逐字引文 / 关键词裁决，且曾是默认 provider（`BID_EVAL_LLM_PROVIDER` 未设置时）。它存在
两个根本问题：

1. **它不是产品**。生产环境从一开始就被 `.env` 的真实模型（OpenAI 兼容端点）覆盖；mock 唯一的
   真实用户是离线测试（171 项中约 15 项）与两条验收脚本的离线模式。默认值 `"mock"` 反而构成
   陷阱：忘配模型时系统不会报错，而是静默产出一堆看似合法的假结果。
2. **它让测试锚定在假输出上**。评分全链 7 项测试断言的精确分数（36.13/40.00、核验率 100%、
   商务 9/6…）全部是 mock 输出形状——"离线全绿"不能证明真实模型下的任何行为。

用户指令（2026-10-14）：mock 没什么用，全部删去，**不留测试桩**。

## 决策

1. **删除 `app/llm/mock.py` 与 registry 的 mock 分支**。生产代码零 mock（`grep -ri mock
   backend/app scripts` 为空）。
2. **未配置 provider 时 fail-fast**：新增 `LLMNotConfigured`（`AppError` 子类，code=
   `LLM_NOT_CONFIGURED`，中文可操作报错并列出可选别名）。注意它**不得继承 `LLMError`**——
   compile 的重试循环只对 `LLMError` 退避，配置错误应立即失败而不是伪装成模型故障。
3. **Gateway 改惰性解析**：构造网关不再触发 provider 解析，首次调用才解析。客观-only 评分链路
   （不产生任何 LLM 调用）因此可以在未配置模型的进程里全程离线运行——这是删除 mock 后 174 项
   测试仍能离线全绿的关键。
4. **conftest 不再钉任何 provider**：顶部先导入 `app.config`（承重——先让 `.env` 注入，
   `delenv` 才有效），app_env 里 delenv 全部 LLM 变量。测试内任何意外 LLM 调用都会立即抛
   `LLM_NOT_CONFIGURED`，绝不会静默摸到真实 API。
5. **`[[TASK:]]` 标签移除**（唯一消费方就是 mock），`BID_EVAL_PROMPT_VERSION` 升
   `2026-10-14.1`（缓存键与审计随之自然换代）。
6. **mock 耦合测试删除或转为 LLM-free 等价物**：
   - `test_scoring_e2e`：mock 编译链 → 模板实例化链（objective+price 阶段全确定性）；防锚定
     用例改 DB 直种主观分记录；精确分数断言删除（价格分公式正确性由 test_engine 37 项守护）；
   - `test_merge`：新增 `deterministic_match` 纯函数单元测试；resolve 全链改 DB 直种合并辅助包
     （顺带修了 `llm_match` 网关构建在 try 块外的 bug——未配置时现在按设计降级为仅确定性匹配）；
   - `test_auto_pipeline`：改用合成"全确定性可通过"迷你法规包，离线验证 auto 收敛的结构保证；
   - `test_auto_resolve`：删除（需真实编译才能触达，由真实模型验收间接覆盖）。
   - 171 → 174（LLM-free 测试净增：确定性配对与模板链覆盖更细）。
7. **验收脚本转真实模型**：`run_e2e.py` 退化为冒烟（结构断言：编译 ≥1 项含价格项、未决项自动
   补全留痕后可冻结、5 家报价抽取与独立真值精确一致、报告可导出；抽取质量类指标改打印不判分）；
   `run_auto_pipeline.py --assert-final` 保留——零 need_human 是 auto 模式的结构保证（模型故障
   也降级为 fail 终态），对任何**已配置**的模型成立。`seed_demo.py` 去掉 provider 硬编码。
8. **错误类型边界（降级 vs fail-fast）**：`LLMError`（超时/限流/输出坏）= 模型故障，逐项降级
   （裁决 fail / 主观 need_human）；配置类 `AppError`（未配置 provider、**密钥/base_url 缺失**
   ——`OpenAICompatProvider` 同样抛 `LLMNotConfigured`、预算超限、数据分级拒绝）= 整个运行
   显式失败。配置错误不得被降级处理器吞掉、伪装成模型结论。

## 后果

- **回归保障 = 174 项 LLM-free 测试 + 两个真实模型验收脚本**。引擎/解析/规则状态机/报告等
  确定性主干不受影响；语义质量（抽取完整度、裁决准确率）只能靠真实模型验收观察，没有离线替身。
  注意成本变化：mock 时代的离线验收是秒级；`run_auto_pipeline` 全量 87 条法规包 × 5 家约 400 次
  裁决调用，推理型模型下需 30-120 分钟（`BID_EVAL_LLM_CONCURRENCY` 可提并发）——管线结构的
  快速回归靠离线 pytest（test_auto_pipeline 验证同一套自动收敛保证），真实模型验收是重炮而非日常。
- 历史库中 `llm_provider='mock'` 的行**保留为审计事实**，不迁移——无任何代码再校验该字符串，
  报告模板对空值/历史值都安全渲染。
- LLM 响应缓存因 prompt_version 变化自然失效一次。
- 前端零改动（本就无任何 mock/provider 引用）。
