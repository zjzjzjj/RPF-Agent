# 决策记录 0017：资格审查二值化——否决点筛查（通过 / 存疑）

- 日期：2026-11-23
- 状态：已采纳
- 关联：决策 0011（资格轻量）、0013（三态化）、0015（确定性事实源证据闸门）

## 背景

决策 0013 上线后用户反馈：资格与符合性审查**全是"存疑"，等于无用**，无法准确评价一个标书是否通过。根因是"通过"通道被四层堵死：
① 快审只看 8000 字节选（`qualification_quick_chars` 默认 8000），而节选下的快审 pass 被降级"存疑"——pass 在产出端不可达；
② 快审 prompt 按"宁存疑不错杀"设计，LLM 默认全给 concern；
③ 视觉/外部事项（签章、密封、递交时间）按 0013 规则恒"存疑"；
④ 汇总口径"任一非 pass→存疑"，一格污染整家。

用户拍板（2026-11-23）：**二值化（通过/不通过），"不通过"不能写"不通过"，显示为"存疑"（AI 不完全否决，只提出否决建议）；误否决完全不可允许——只有发现确定性的否决点才提议否决，"没发现证明材料/没发现资质证明"绝不否决。**

## 决策（用户拍板口径）

1. **二值必给**：每条必落 `pass / fail`（显示 **通过 / 存疑**）。`fail` 的语义=发现确定性否决点，显示层即"建议否决（仅建议，需人工确认）"，理由前缀标注；"不通过/建议否决"不再作为轻量判定的显示词。
2. **通过=未发现确定性否决点**（不等于确认符合）。材料未见、扫描件看不见、快审失败/未覆盖、反面引文核验不过、签章类视觉/外部事项、节选覆盖有限的快审 pass、快审存疑线索——一律 `pass` 并留理由注记（needs_human_check 标记"AI 未经完整核验"）。缺证据≠不符合。
3. **否决点只有两类**：
   - 事实矛盾型确定性预检不符：`value_in_range` / `count_at_least` / `date_valid` 的提取事实与规则原文矛盾（注册资本低于门槛、有效期越窗、业绩计数不足），以及 `math_relation` 的报价大小写/分项合计矛盾（矛盾本身是反面事实，不自动选边）；
   - LLM 快审 `reject` 且逐字反面引文经本地核验命中（`_verified_counter_quotes` 口径不变，引文落库不变）。
   `document_present` / `contains_phrase` 的"材料/短语未见"永不构成否决点（run_check 本就不产 fail；"机器看不见≠不符合"）。
4. **每家汇总二值**：任一 `fail` →"存疑（建议否决，需人工确认）"（附否决点条数）；否则 →"通过"（附"待人工核对"注记=**非清洁通过格**计数：needs_human_check 的通过格（签章/材料未见/节选覆盖有限/快审存疑线索/快审失败/引文核验不过）+ 故障兜底/未评/外部边界残留格——误放可容忍但必须可见，2026-11-16"残留在通过里必须可见"教训沿用）。仍是纯投影（`derive_qualification_suggestion`），按 payload schema 分流：v3 走二值投影，v1/v2 旧运行投影逐字保留。
5. **schema_version 2→3**：payload 增 `needs_human_check`；`_valid_cell` v3 只收 `pass/fail` + LLM 否决必带逐字引文（缺失则不收口，逼出 prompt 漂移）；v1/v2 校验与显示不变。
6. **快审入料加宽**：页面预算默认 8000→24000 字（`BID_EVAL_QUALIFICATION_QUICK_CHARS`），每条款候选页 4→6——否决点筛查要看得见反面证据。prompt 口径改为"只有 reject 会被采纳且必须逐字引文；pass/concern 仅人工参考线索；宁漏勿错杀（误否决绝对不可接受）"。
7. **结论仍只报告不阻断**：照常出分/排名/价格基准价；`machine_status` 沿用 `fail` 不用 `recommend_reject`（0013 所述"收口失败/veto_flag 误亮/确认否决按钮误活"三重涟漪不变）；签署/签章类仍由人工查验。

## 不变量

- 误否决不可接受：缺证据、机器看不见、无法核验绝不否决；LLM 否决引文逐字核验双闸门不变（prompt 约束 + 本地子串核验，不过降级 pass 保线索）。
- 旧运行不改写：v1/v2 payload、`not_deeply_reviewed` 显示、旧标签表（LIGHTWEIGHT_VERDICT_LABELS / QUALIFICATION_SUGGESTION_LABELS）与派生逻辑逐字保留，按 schema 分流。
- full 模式（深评通道）与计分项词表（objective_fastpath 的 concern 等）不动；排名 veto_flag、"确认否决"按钮语义不动。
- 不引入跨项目经验阈值；判定只依据规则原文；不自动否决。

## 验证

- `backend/tests/test_qualification_light.py`：二值归并全分支（事实矛盾→否决点、math 矛盾→否决点、LLM 否决+引文命中/重定位→否决点、核验不过/无引文→pass 保线索、材料未见→pass、签章类→pass+待核对、快审失败→pass+待核对、节选 pass 不降级）、执行器引文落库与每家汇总端到端（含 manual_check_count）、`_valid_cell` v1/v2/v3（v3 收二值、拒三态残留、LLM 否决必带引文）、`derive_qualification_suggestion` v1/v2 与 v3 分支、报告标签按 schema 分流、二值核心锚（引擎任意路径只产 pass/fail）。
- 全量回归 1642 passed（唯一失败 `test_pdf_runtime::test_truncated_segment_is_never_accepted_by_strict_client` 为既有失败，干净 HEAD 复现，与本改动无关）。
- 前端 `tsc -b` 零错误。
