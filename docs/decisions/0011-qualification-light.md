# 决策记录 0011：资格/符合性轻量审核

- 日期：2026-11-10
- 状态：已采纳
- 关联：决策 0007/0010、围串标留空

## 背景

用户明确：资格审核不是重点，不应为资格/符合性条款逐条调用 LLM 或重复读取材料；算力与准确性应集中到计分项，尤其客观分。原 managed 执行器对每个 veto/match 格都走 `review_criterion`（LLM + 补评轮），且 `prepare_bidder` 会触发 OCR——run 85 的漫长耗时与失败链主要由此放大。

## 决策

新运行默认 `qualification_mode="light"`（`BID_EVAL_QUALIFICATION_MODE_DEFAULT`，旧运行缺键回退 full）：

1. **确定性预检**：每投标人一次 `extract_bid_facts`，对每条资格条款复用 `run_check`（document_present/contains_phrase/value_in_range/count_at_least/date_valid/math_relation/human_only），并保留扫描件豁免语义。
2. **每家一次合并快审**：仅把预检未知、且非视觉/外部事实的条款合并为**一次** LLM 调用（`2026-11-10.qual-light.1`），输出 pass/concern/unknown；不产引文、不 OCR、不建采集批次、不发图片。快审失败只降级为「未深评」，整场不失败。
3. **结论只报告不阻断**：`basis="lightweight"`，状态 `pass/fail/concern/not_deeply_reviewed`；不影响计分、排名、价格基准价或投标人状态；`_valid_cell` 新增 lightweight 分支保证矩阵闭合。
4. **执行顺序**：客观分（count/客观路由）→ 主观分 → 资格轻量；旧运行保持原顺序。
5. 报告/矩阵/API/前端显示「确定性预检 + 每家一次轻量快审（不构成完整审查）」，并有免责声明。

## 不变量

- 未知/读不到/需视觉/外部记录 → `not_deeply_reviewed`，绝不伪造 pass。
- 确定性硬 fail 只报告，不自动否决、不影响排名。
- 快审 schema 不产出 fail；确定性 fail 不被快审推翻。
- 不改 `automation_manifest`/`_scoring_config`；模式存 `run.config_json` 顶层，finish/resume 不漂移。
- 旧运行（缺 `qualification_mode`）逐字保持 full 语义。

## 验证

- `backend/tests/test_qualification_light.py`：每家一次调用、硬 fail 不被覆盖、快审失败不产 pass、视觉条款未深评、执行器不 OCR/不建 collection、`_valid_cell` 四状态、计分项拒绝 lightweight。
- 全量回归（基线 1346 passed）与真实项目小样验证；报告中资格 fail/concern 不进入 `has_noncompliance`。
