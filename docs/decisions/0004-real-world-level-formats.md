# 决策记录 0004：档位校验对真实标书格式的适配（端点共享降 warn、缺档位分级）

- 日期：2026-10-14
- 状态：已采纳
- 关联：0003-full-auto-mode.md（auto 模式下 confirm/freeze 死胡同的直接诱因）

## 背景

浙江中烟真实招标文件（PRJ-20261014-78MT，99 页）编译出的规则包在 confirm/freeze
被 12 条确定性校验器中的两类 error 全量阻断（9 条），全链（上传投标 → 报价抽取 →
评分）随之不可用：

1. **LEVEL_OVERLAP ×6**：原文档位写作「得0-2分 / 得2-3分 / 得3-4分」——相邻档
   **共享边界值**。LLM 忠实转录为 [0,2],[2,3],[3,4]；校验器原语义
   `hi1 >= lo2` 判 error（含端点相接），假设档位整数不相交。
2. **LEVELS_MISSING ×3**：需求理解(7分)/系统运行环境理解(3分)/运维方案(8分)的
   分数在原文里**拆在子项里**（"协同办公（0-2 分）、行政管理（0-2 分）、党建工团
   （0-1 分）…"），不存在档位表，LLM 抽不出 levels。

死锁机制：`_auto_settle_blocking`（0003）只收敛**状态类**阻断
（needs_input → verified），不处理**内容类**校验错误 → 全自动模式下
confirm/freeze 无解。

关键事实：评分引擎对这两种情况**本就有确定性兜底**——

- `engine/subjective.py` 对无档位项提示"按判据在 0-满分内给分"；
- `level_for_score` 对共享边界的分值取**首个命中档**（即较低档），解析确定。

即：校验闸门比引擎能力更严，把引擎能正确处理的真实标书挡在门外。

## 决定

1. **端点共享（hi1 == lo2）降为 warn**：message 注明"原文如此，评分时该分值
   归入较低档"；**内部相交（hi1 > lo2）保持 error**（真歧义）。LEVEL_GAP 判定
   不变（`hi1 + 1 < lo2` 才算空洞，整数相邻档 [0,2]/[3,4] 仍合法）。
2. **LEVELS_MISSING 分级**：`max_score` 非空 → warn（"按 0-满分综合评定"）；
   `max_score` 为 None → 保持 error（无满分则完全不可评）。
3. **不做数据归一化**：档位数值保持与原文逐字一致（项目哲学"信数字不信标签"、
   引用可核验），歧义以 warn 留痕而非静默改写。

## 后果

- 真实标书的「得X-Y分」共享边界与"分数拆子项"两种格式不再阻断冻结；
- 报告/前端校验面板仍展示 warn（边界共享与缺档位可见、可审计）；
- 共享边界的分值归属是确定性规则（较低档），不引入评分歧义；
- 锚定测试更新：`test_validator_level_overlap_and_top`（内部相交仍 error）、
  `test_validator_level_shared_boundary_warn_only`（真实形状零 error）、
  `test_validator_levels_missing_downgrade`（warn/error 分级）。
