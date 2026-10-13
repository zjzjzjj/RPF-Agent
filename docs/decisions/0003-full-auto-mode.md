# 决策记录 0003：全链自动化模式（无人工环节）

- 日期：2026-10-13
- 状态：已采纳
- 关联：0002-general-law-rule-pack.md（法规规则包固化）

## 背景

0001/0002 与设计稿确立的原原则是"绝不自动否决、所有否决类结论必须人工确认"，
评分链路上因此存在三组人工闸门：

1. **三态判定不收敛**：`human_only` 条款（密封/签字/盖章类，法规包 87 条中 72 条）、
   算术冲突、串通信号命中，全部路由 `need_human/conflict_flagged`；
2. **主观分无机器终态**：LLM 只写 `ai_suggested_score`，转 `final_score` 必须人工
   confirm/override（含防锚定的盲评-解锁流程）；
3. **报告定稿以人工处置为唯一依据**：`SETTLED_STATUSES` 只认 human_confirmed/human_overridden。

项目方向调整（用户指令 2026-10-13）：**删除人工复核环节，实现全自动化流程**；
机器判不了的模糊项由 **LLM 带原文证据裁决**。

## 决定

1. **全自动模式为默认**（`BID_EVAL_AUTO_MODE=1`，`config.auto_mode()`）；置 0 回退
   人工模式（三态/复核队列代码路径保留为可配置回退，不是死代码）。
2. **LLM 带证据裁决**：`engine/auto_judge.py`——`run_check` 产生 unknown 或
   fail+need_human 时，把条款原文 + 法条原文 + 投标事实摘要 + 投标文件文本
   （截断 15k 字）送 LLM，输出 pass/fail 终态 + reasoning + 逐字引用；
   证据不足按 fail（宁严勿纵）；LLM 失败重试后仍失败 → fail + 警告（终态不悬挂）。
   `<bid>` 防注入声明沿用抽取提示词的模式。
3. **终态词汇**：新增 `review_status = 'auto_confirmed'`（CHECK 约束与
   `scripts/migrate_auto_review.py` 存量库迁移）；否决语义沿用
   `machine_status='recommend_reject'`，但 review 直接 auto_confirmed——
   **不再有"建议否决须人工确认"这道闸**，投标人状态由评分运行直接置 rejected/scored。
4. **主观分直写**：auto 模式下 LLM 建议分直接写 `final_score`；模型给不出分的
   按最低档兜底（宁严勿纵），警告留痕。防锚定盲评-解锁流程仅保留在人工回退模式。
5. **报价自动确认**：评分入口对未确认报价自动 confirmed（留痕）；无报价的投标人
   自动剔除出本场评分（留痕），不阻塞整场。
6. **报告**：`SETTLED_STATUSES` 增加 auto_confirmed——全自动跑完后报告直接 final；
   原"疑问清单"语义保留给人工回退模式与 LLM 裁决失败的真未定项。
7. **前端**：复核队列页删除（路由/导航/文件）；工作台"人工复核"阶段替换为
   "自动裁决"（指向报告的自动裁决信息）；证据工作台保留为只读查看器。
8. **验收脚本成对**：`run_e2e.py`（人工回退模式口径）+ `run_auto_pipeline.py`
   （全自动口径：零 need_human 残留、报告直接 final）。存量测试经 conftest
   显式 `BID_EVAL_AUTO_MODE=0` 继续守护回退路径；全自动语义由
   `test_auto_pipeline.py` 覆盖。

## 影响

- **责任转移**：否决与评分的最终结论由"评标委员会人工确认"变为"LLM 证据裁决 +
  全量审计留痕"。每条 auto_confirmed 记录可在 `ai_reasoning`/`warnings_json`/
  ReviewRecord（actor="auto"）中还原裁决依据；报告含自动化声明。
- 若交付场景要求法定人工评审，置 `BID_EVAL_AUTO_MODE=0` 即恢复原三态流程，
  数据模型与 API 兼容（跑一次 `scripts/migrate_auto_review.py` 即可升级存量库）。
- mock provider 的裁决实现是确定性关键词命中（管路测试），不代表真实模型效果；
  真实模型需配置 provider 后用 run_auto_pipeline 复验。
