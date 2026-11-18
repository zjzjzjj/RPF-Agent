# 决策记录 0015：确定性事实源接入证据闸门（检测器结论=有据事实）

- 日期：2026-11-18
- 状态：已采纳
- 关联：决策 0006（客观分规则表）、决策 0014（确定性归正）；冻结报价免口供先例（auto_review "Repeated confirmed inputs are not newly extracted facts"）

## 背景

利群商标条盒评分触发「事实 cert_section_present 缺少对应原页证据，不能只凭模型数值计分」→ 定向补抽 2 次耗尽 → concern+暂定分悬置。病灶三叠：

1. **证据闸门与时序**：`check_scalar` 要求每个标量事实带原页证据；截图节是图片页（OCR 仅标题），模型给不出 ≥12 字逐字引文；而确定性注入 `_inject_deterministic_cert_backing` 跑在证据校验**之后**——注入永远晚于校验。
2. **确定性结论无处落地**：`cert_evidence_present` 双通道（浙江中烟四家实证 3/3/3/1 对齐人工）早已算出该事实，但输出不带页锚（basis 片段被丢弃）、三条执行路径口径不一（SC 主文档页/缺省 True vs MI 全附件页/缺省 False）。
3. **AS 自动路无确定性案例供给**：`_cases_from_blocks`/四个 overlay（只升不降）仅 SC 深评路生效。

## 决策（用户拍板口径）

**确定性检测器的结论 = 有据事实，直接供给批审、免模型口供**，推广到全部有检测器的事实（cert_section_present / 证书三件套 / verified_cases 材料链布尔 / 负责人经验证明页·团队社保页 / deviation_counts）。

1. **供给清单协议**：`criterion["_supplied_facts"]`（发 prompt 前剥离）→ 三插桩点：①schema 抑制（冻结价同款："不填也不为冻结输入寻找或编造原页引文"）+ `supplied_fact_policy` 声明；②模型重复报 → pop 兜底（冲突记 provenance 不抛错——检测器是标定真值源，逼模型改口只会造假引文）；③汇流点注入（`_validate_evidence` 之后、`_finish_decision` 复核之前，单批/综合/shared 三路同口径）。
2. **宿主合成证据**：锚 quote 只由检测器从送审页文本切片、注入期 `_original_quote` 逐字复核；页文本 <12 归一化字（图片/仅标题页）退 **visual 锚**（截图类证据本体就是页图）；复核不过丢锚（`anchor_verify_failed`）**绝不放行未核验引文**。落库 `created_by="engine"` 留痕；`_finish_decision` 结论证据检查**不认 engine 证据**（防供给给空口结论洗白）。
3. **语义红线**：**正向供给**（检出才供；未检出不供给不否定——机器看不见≠不符合）；**只升不降**（案例布尔检测 True 升 None/False，检测 False/None 不降模型 True，overlay 范式）；无锚否定态（如 cert_section_present=False 的可读确认）走 `provenance_only`——只写值做引擎闸门输入，不制造否定引文，豁免口供校验（has_qualification_cert 同款先例）。
4. **推广落点**：
   - a. `cert_section_present`：`detect_cert_section` 三态（True=命中+页锚 / False=文本层可读确认 / **None=盲态不供给**）+ 三执行路径共用同一计算（SC/MI 历史缺省映射**冻结**，收敛为后续项）；
   - b. 证书三件套（certificates/certificate_evidence/cert_backing 逐本）：`find_cert_copy_pages_anchored` 恢复被丢弃的 idx→页锚；未检出证书不进清单（图片扫描件走模型旧通道）；backing False/None 是标定三态（德勤防线）不是"看不见"；
   - c. verified_cases 材料链布尔：四个 overlay **移植入 AS `_verified_count`**（补齐"AS 不经 overlay"缺口，run48/49/64 口径不变）；案例布尔锚合并只升不降；
   - d. 负责人经验证明页/团队社保页：存在性检测补**命中页列表**（`person_stats.exp_pages/social_pages`）；**不做独立事实字段**（引擎消费点在 person_attributes/verified_cases 布尔键）；**页数阈值不复活**（五年<3页/社保<16页是 run 54 拟合反实证）；
   - e. `deviation_counts`：`_extract_deviation_counts_from_pages` 区间页锚 + total/missing/negative 全三键；定位不到表不供给（R33 兜底不变）；
   - f. **明确不接**：facts.py BidFacts 纯值（注册资本/大写金额——无页锚=机器口供；bid_price 系已有冻结价通道）、章节布尔。

## 不变量

- 每一分有据可循不变式不动：供给证据是真页码+逐字引文（或图片页 visual 锚），三道复核网（构造期/落库 verify_automatic_citation/结论证据排除 engine）。
- `match` 不计分、TOTAL_SCORE 核算、score_type 五值、frozen 包只读等 ADR0014 红线沿用。
- 检测器盲态（None）不猜；`_verified_count` "截图须显式核验"契约不动。
- 检测器异常/供给构建异常 fail-open 回模型通道（绝不打挂评审）。
- `CollectionCheckpoint.VALIDATOR_VERSION` 随 schema 语义 bump（签名不含 FACT_SCHEMA）。

## 验证

- `backend/tests/test_deterministic_fact_supply.py`：供给过闸（利群回归锚：图片/短文本页 visual 兜底）、模型重复报 pop 不重试、盲态不供给旧路径保留、篡改锚必丢、案例布尔只升不降、schema 抑制+policy 断言。
- 兼容零漂移：`cert_evidence_present` 2 元组包装（test_cert_evidence 16 用例零改动）、test_count_cases overlay 族、test_objective_fastpath、test_automatic_review_contract contract 族。
- 利群重跑验收：悬置格恢复出分（`ai_raw_json._fact_provenance` 可审计）；无截图节家疑问不硬判（zero_reason=unreadable）；补抽轮消失、速度不劣化。
