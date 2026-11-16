# 决策记录 0013：资格轻量审核三态化（通过 / 建议否决 / 存疑）

- 日期：2026-11-16
- 状态：已采纳
- 关联：决策 0011（资格轻量）、0007（自动评审完成）、证据引文口径分层

## 背景

决策 0011 上线后，用户反馈：资格性审查缺乏确定性结果，**大部分条目显示"未深评"**（视觉/外部事项、快审失败、节选未给结论等都兜底成 `not_deeply_reviewed`），不可接受。要求：每个资格审查项必须给出 AI 的确定性三态结论——通过 / 否决 / 存疑，并按项汇总每家投标人的总体资格建议。

## 决策（用户拍板口径）

1. **三态必给**：每条资格/符合性条款必落 `pass / fail / concern`（显示：**通过 / 建议否决 / 存疑**），`not_deeply_reviewed` 从产生端删除（旧 v1 行显示保留"未深评"，数据不改写）。逐项三态 + 每家汇总（任一否决→建议否决；有存疑无否决→存疑；全过→通过）。
2. **否决来源**：确定性预检硬性不符（有效期越窗、数值门槛、计数不足等）→ `fail`；LLM 快审发现明确反面事实**且能给出逐字反面引文**时也可 `fail`。prompt 强约束"否决必须极度克制、宁存疑不错杀"。
3. **缺失证据≠不满足**：材料缺失、扫描件不可读、抽取不到、视觉/外部事项、快审失败——一律 `concern`，绝不因此否决（机器看不见≠不符合，run 6 误否决教训）。
4. **LLM 否决双闸门**：prompt 约束 + 代码本地逐字核验（`_verified_counter_quotes`：归一化空白后子串命中送审页文本，不信任 LLM 页码、重定位真实页；引文缺失/核验不过 → 降级 `concern`）。核验通过的反面引文落 `evidence_citations`（role 按 score_type，created_by=llm，复用 `_persist_verdict_citations` 口径）。
5. **否决不阻断**：照常出分/价格/排名；资格结论仍只报告，不参与排名/价格/基准价剔除。
6. **machine_status 用 `fail` 不用 `recommend_reject`**：`recommend_reject` 会触发 `_valid_cell` 收口失败、ranking veto_flag 误亮、人工确认否决按钮语义触活三重涟漪；资格结论保持纯咨询，"建议否决"只作显示层标签（LIGHTWEIGHT_VERDICT_LABELS）。
7. **汇总建议是纯投影**：`derive_qualification_suggestion`（score_summary_service）由逐格状态派生，不落表不迁移；报告汇总表渲染〔资格建议：X〕、前端投标人行同口径。
8. **schema_version 1→2**：payload 增 `reject_source`/`reject_quotes`；`_valid_cell` 兼容 1/2，v2 校验三态状态 + LLM 否决必带引文（缺失则 cell 不算完成、run 不收口）。

## 不变量

- 旧运行不改写：v1 payload、`not_deeply_reviewed` 显示与 `_valid_cell` 白名单全部保留。
- full 模式（决策 0011 前语义）不动；count fastpath 的 duplicate_summary `not_deeply_reviewed`（basis=fallback）不动。
- 确定性否决不产引文（过程即证据）；LLM 否决引文落库且逐字可核验。
- 不引入跨项目经验阈值；判定只依据规则原文。

## 验证

- `backend/tests/test_qualification_light.py`：三态归并全分支（确定性否决、LLM 否决+引文命中/重定位/核验失败降级、无引文降级、乱码映射存疑、节选快审通过存疑、快审失败/视觉事项存疑）、执行器引文落库与汇总建议端到端、`_valid_cell` v1/v2、`derive_qualification_suggestion` 五种输入、报告标签。
- 三态化核心锚：任意输入组合下引擎不再产生 `not_deeply_reviewed`。
- test_verdict_citations 全绿（引文助手迁至 `app/evidence/verdict_citations.py` 的搬移回归网）。
