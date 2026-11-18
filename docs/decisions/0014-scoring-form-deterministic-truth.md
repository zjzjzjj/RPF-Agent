# 决策记录 0014：评标细则抽取的确定性归正（主/客观列权威 + match 不带分值 + 权重表非评分项）

- 日期：2026-11-18
- 状态：已采纳
- 关联：决策 0004（真实档位形态）、决策 0006（客观分规则表）

## 背景

真实项目「2025-2028年利群（休闲细支）商标条盒」（p123-125 评标细则：资信 8 + 技术 32 + 价格 50 + 样品 10 = 100）编译后被确认闸门双重阻塞（TOTAL_SCORE Σ=95≠100 + QUOTE_REQUIRED 假项），只读实测钉死三叠根因：

1. **三级表分值 cell 形态解析不了**：该表分值列是「子项名（0-N 分）」（如"技术研发\n（0-2 分）"），旧 `_RE_SCORE_CELL` 只认裸分值 → 技术 14 行（32 分）骨架整体蒸发（rows=6、row_sum=28）；
2. **权重表数据行被误收成评分项**：p125「标段|产品名称|所占样品分 → 1|利群(休闲细支)商标条盒|10分」列头过不了列映射门槛，数据行却按继承的列映射被骨架收成计分行（text=""、quote=None），或在纯 LLM 路径被直接抽成假项——与样品分重复计 10 分；
3. **客观分计分行被 LLM 自报成 match**：prompt 里"match（满足条件得分）"措辞误导（2026-10-26 即有先例）+ 多档「一条…得2分、两条…得3分」/扣分式「缺少一项扣0.5分」/二值「提供…得1分」三形态是 `_score_type_for_row` 的正则盲区 → match 不计分语义下 15 分永远兑现不了，Σ 恒缺。

## 决策（用户拍板口径）

1. **「主/客观分」列 = score_type 的 ground truth，全面覆盖**：骨架捕获到该列的行，score_type 以列为准（客观→count 族、主观→subjective），不信 LLM 自报。归正只依据细则原文明示信息（列标注、"得N分/扣N分"行文形态、权重表列头词），零项目专名（不拟合单项目答案红线）。
2. **match 不带分值硬不变式**：`score_type=match ∧ max_score>0` 是自相矛盾数据（match 不计分，分值永远兑现不了）——明示形态可判即由 `normalize_package` 确定性归正（含 check 参数补建），判不出由 **MATCH_WITH_SCORE（error）** 阻断交人工，不猜。TOTAL_SCORE 核算口径（match 不计分）不动。
3. **客观全有全无形态（"任一不能满足得0分、全部符合得满分"，如样品评审）→ subjective 引擎代判 + `_subjectivity_hint=objective` 留痕 + OBJECTIVE_AS_SUBJECTIVE（warn）披露**。理由：判定对象是实体样品/整体符合性，文本无计数对象——硬映射 count 会被事实层悬置（final=None 不进总分，10 分蒸发）或暂定 0，比代判更糟；主观引擎的材料链校验本有"任一不符→0"语义。客观性以标注+校验披露+人工复核承载，不伪装确定性计数。
4. **权重表不是评分项**：列头含 产品名称/所占分值/分值计算权重/评审权重 的表整表跳过（骨架层，warning `weight_table_skipped`）；空细则文本行不产行（`empty_text_row_dropped`）；另设抽取期剪枝（`compiler.prune_weight_table_items` → `selection_meta.weight_pruned`）与 normalize 退役（`weight_rows_retired`）两道网——判定要求 quote 空 + 权重表上下文 + 无计分语言，有原文引用的真评分项不受波及。QUOTE_REQUIRED 的 hint 指引人工停用权重行。

## 不变量

- score_type 五值（veto|formula|count|match|subjective）不变，零迁移；frozen 包只读不迁；
- veto/formula 一律不被归正 pass 触碰；
- 一切归正留痕且幂等：`report["score_type_fixed"]` 带 `by` 层指纹（subj_column/form）、`ReviewRecord.reason` 前缀（"自动修复：判断项不得带分值"/"自动修复：权重表"）、`normalize_package` stats 键（`match_scored_fixed`/`weight_rows_retired`）——三层指纹字符串互不相同，变异测试摘除任一层必红；
- 不猜：形态判不出 / 列缺失且形态不明 → 维持现状交人工（MATCH_WITH_SCORE 等校验阻断）。

## 验证

- fixture：`backend/tests/fixtures/scoring_skeleton/liqun_2026/`（p123-125 三件套，dump_scoring_fixture.py 再生）；
- 测试：`test_scoring_skeleton.py`（命名分值解析/权重表跳过/空行过滤/expected 快照）、`test_compile_reconcile.py`（列覆盖归正 by=subj_column、三形态映射、全有全无、利群形态 Σ=100 端到端、job 恒写对账报告）、`test_validators_score_invariants.py`（MATCH_WITH_SCORE/OBJECTIVE_AS_SUBJECTIVE/QUOTE hint/prompt 契约）、`test_normalize_invariants.py`（②b②c+幂等）；
- 真实验收：UI 重新解析利群招标文件 → Σ=100、无「利群(休闲细支)商标条盒」假项、7 条误标归 count 带可执行参数、确认/冻结零阻塞。
