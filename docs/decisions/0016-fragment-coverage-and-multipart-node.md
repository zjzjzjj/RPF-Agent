# 决策记录 0016：片段覆盖对账（每条骨架行恰好计一次）+ 多分部节点不作类别权威

- 日期：2026-11-18
- 状态：已采纳
- 关联：决策 0004（真实档位形态）、0014（评标细则确定性归正）

## 背景

真实项目「测试7-1.信息化运维-2026-2028年一体化运维平台维保服务采购」（PRJ-20261118-RGX5，p88–p90 评标细则：商务 10 + 技术 30 + 报价 60 = 100）编译后 **Σ=103** + `TOTAL_SCORE` 阻断；同包技术 30 分被误归 commercial。

两处根因（只读实库 + `jobs.id=919` 对账报告）：

1. **片段拆行导致双计**：骨架第 0 行「投标人实力 0-3 分（ISO27001/ISO20000/ISO9000 每证 1 分）」被 LLM 抽成三条各 1 分子项，引文分别是该行原文的三个分句（归一化 25/25/20 字符）。`_covers` 要求 `row.text` 的 40 字符切片命中，三条都判「未覆盖」；1:1 校正与父子裁剪又都要求单条覆盖/父项 ≥2 行 → 全部跳过；缺口补齐再生成一条 3 分父项 → 该行计 3+3=6。生成父项形态是 `existence + max_items=1`，执行层最多只认 1 张证书，保留子项反而更正确。
2. **多分部节点强改类别**：`doc_nodes` 把商务表与技术表并在节点 `tender356:root:C12:SEC6:I5:N1`（title「1、商务部分（10 分）」、正文含「2、技术部分（30 分）」）。`apply_category_from_nodes` 的节点标题通道把该节点下 LLM 原本正确的 8 条技术项全部改成 commercial（`edit_json.category_from_node` 留痕）。

## 决策

1. **每条骨架行的 max_score 在规则包中恰好计一次**。reconcile 在缺口补齐前做独立片段对账 pass：一条计分条目若 `rows_of` 为空（未完整覆盖任何骨架行），其引文（quote → description → name，去空白后 ≥8 字符；8–15 字符必须带「得N分/扣N分/计N分/满分/不得分」锚点）唯一命中某无计分覆盖行的**评分正文**（第一个【证明材料】之前），则可作为该行片段。片段集合同时满足：
   - 分值全部非空、合计 **严格等于** `row.max_score`；
   - 主引文两两不重复；
   - 并集覆盖（首次出现区间合并）≥ **60%**；
   才把这些片段登记进 `covered_by`（该行不再生成父项）。任一不满足 → **不删条目、不改分值**，只写 `fragment_unmatched` / `fragment_ambiguous` / `fragment_shadowed` 报告，仍由 `TOTAL_SCORE` error 阻断人工处理（fail-closed）。
2. **片段识别不反向放宽 `_covers`**：`_covers` 被父项整组覆盖、score_type 校正、1:1 分值校正、category 校正、父子裁剪、缺口补齐六处共用；把「引文是行文本子串」塞进 `_covers` 会让一条 1 分子句直接"覆盖" 3 分行，导致缺口不生成、该行只计 1 分。片段集合整体闭合才是唯一裁决条件。
3. **多分部节点不作类别权威**：节点正文含 ≥2 个不同 `(商务|技术|价格|报价)部分（N分）` 章节标记时，节点标题只描述了首个分部——跳过节点标题通道，退回引文位置章节标题；引文通道也给不出类别时**保持 LLM 原类别**。单分部节点行为不变。
4. **不迁移存量包**：包 61（v1，Σ=103）保持只读证据；修复后重新编译生成新草稿包（本项目 pkg 63，v2）。veto/formula/价格 ledger 语义零改动。

## 不变量

- 片段 pass 只原地修改 `covered_by`，不删条目、不改 max_score；`dropped_idx` / 价格 ledger / `target_total` 口径不动。
- 零项目专名：判据全部来自骨架行原文、引文逐字包含关系与页面窗口（`page_hint` 可解析时与行页相差 ≤1）。
- report 恒带 `fragment_covered` / `fragment_unmatched` / `fragment_ambiguous` / `fragment_shadowed` / `multi_covered_rows`（Decimal 转 float，Job.result_json 可序列化）；`multi_covered_rows` 只披露，不自动去重（单行多覆盖无法从骨架推出权威取舍）。
- 多分部撤权只影响节点标题通道；`formula ⇒ price` 强制不变式保留。

## 验证

- 新增 fixture `backend/tests/fixtures/scoring_skeleton/yitihua_weiyun_2026/`（p88–90 三件套 + expected：11 行、row_sum=40、ledger 商务10/技术30/报价60、target=100、closed）。
- 新增 `tests/test_reconcile_fragments.py`（11 例：核心闭合/证明材料段/页码窗/分值不符/覆盖不足/重复引文/跨行歧义/多覆盖披露/并集覆盖/probe 阈值/compile 端到端接线）、`test_scoring_skeleton.py` +2、`test_category_map.py` +3。
- 变异抽检：摘除片段 pass → 回 103 且生成父项；覆盖率阈值改 0 → 覆盖不足用例变登记；去掉多分部判定 → 技术项被改 commercial。
- 服务器验收（pkg 63）：`skeleton_reconcile.final_sum=100.0`、`generated=[]`、`fragment_covered` 1 条（sum=3、coverage=1.0）；无 `TOTAL_SCORE`；c18–c20 保留、无 c32；c23–c30 category=technical；分桶 商务 10 / 技术 30 / 报价 60。
