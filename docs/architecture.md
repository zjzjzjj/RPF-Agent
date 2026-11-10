# 架构与数据模型

## 分层

```
┌─────────────────────────────────────────────────────────────┐
│ 前端（React + Ant Design）                                   │
│  工作台 / 规则审核 / 投标人 / 自动进度 / 评分矩阵 / 证据 / 报告 │
└──────────────────────────┬──────────────────────────────────┘
                           │ REST（/api）· SSE（任务进度）
┌──────────────────────────▼──────────────────────────────────┐
│ API 层（api/）— 薄：参数校验 + 调 service + 组装响应           │
│  自动运行只读、续评幂等、完整性与报告发布门槛在此强制           │
├─────────────────────────────────────────────────────────────┤
│ 服务层（services/）— 业务编排，不 import fastapi，可被 CLI 直调 │
│  ingest / rule / automatic_scoring / automation / report     │
├──────────────┬──────────────┬───────────────┬───────────────┤
│ rules/       │ engine/      │ evidence/     │ parsing/      │
│ 规则契约      │ 纯代码评分    │ 定位+核验      │ 页/条款树      │
│ 编译器        │ 价格/排名     │ 页图渲染       │ 中文编号       │
│ 12 校验器     │ 三态/计数     │ (verified 唯一 │ 归一化         │
│ 冻结+哈希     │ 主观分校验层  │  写入点)       │               │
├──────────────┴──────────────┴───────────────┴───────────────┤
│ llm/ 网关（缓存/限速/预算/数据分级） → OpenAI 兼容 provider       │
│ jobs/ jobs 表 + 进程内 worker（进度/取消/心跳/僵尸回收）        │
│ db/   SQLAlchemy 2.0（SQLite 起步，DATABASE_URL 可切 Postgres）│
└─────────────────────────────────────────────────────────────┘
```

## 数据模型（18 张表）

```
projects ─┬─ documents ─┬─ document_pages
          │             └─ doc_nodes（条款树，node_id 路径编码、全局唯一稳定）
          ├─ rule_packages ─┬─ criteria ─┬─ criterion_levels（档位判据原文）
          │                 │            └─ criterion_checks（检查原语实例）
          │                 ├─ price_formulas（公式参数）
          │                 └─ package_validations（校验器输出）
          ├─ bidders ── bidder_documents
          ├─ evaluation_runs ── score_records ─┬─ evidence_citations（证据四件套）
          │                                    └─ review_records（复核留痕）
          ├─ jobs
          └─ audit_logs
```

### 关键字段与不变量

**rule_packages**：`status`(draft→confirmed→frozen→superseded) · `content_hash`（canonical JSON 的 sha256 前 16 位）· `snapshot_json`（冻结时全量快照——评分只认快照，业务表被误改也不影响已发生的评分）。

**criteria**：`source_node_id + source_page + source_quote`（规则原文四件套，引用必须可达）· `status` 六态（verified/needs_input/ambiguous/gap/conflict/invalid）· `edit_json`（人工修正保留模型原值 + 谁/何时/为什么）· `weight` **永不进 prompt**。

**evaluation_runs**：新建自动运行的 `config_json.automation_manifest` 冻结所选投标人、报价、文件与解析页哈希、规则、模型配置和全部应评条款；`automation_version=1` 区分历史路径。执行状态与业务结论分离，技术故障为运行 `failed`。

**score_records**：唯一约束 `(run_id, bidder_id, criterion_id)` · `rule_version + package_content_hash`（任何分都能回答“按哪版规则算的”）· `machine_score`（确定性引擎）· `ai_suggested_score`（AI 评分）· `final_score`（有效自动结论与证据校验后事务写入）· `ai_raw_json.auto_review`（执行状态、依据类型、补评过程、确认缺失或视觉观察）· `evaluator_actor + confirm_time`（执行与审计归属）。历史 `human_score`、防锚定与人工复核字段保留。

**evidence_citations**：`document_id + page_no + node_id + quote(逐字) + bbox_json + page_size_json` · `verified`（只由 verifier 写）· `verify_method`(exact/ws_insensitive/prefix_fallback/sentence_fallback) · `candidates_json`（定位失败的近似候选，交人工挑选）。

## 证据定位与核验

```
引文 ──归一化（全角/引号/乘号/零宽）──▶ 候选页排序（hint 优先 → 倒排粗筛 → 逐页）
  ├─ L1  精确子串（归一化后逐字）        → verified=True
  ├─ L1b 空白不敏感（软换行不影响逐字）   → verified=True
  ├─ L2  前缀回退（60/40/25/15 字）      → 候选定位，自动运行进入补评
  ├─ L3  句读切段匹配                    → 候选定位，自动运行进入补评
  └─ 未命中 → 定向检索/OCR/按需视觉复评，仍失败则运行失败
命中 ──▶ index_map 映射回原文区间 ──▶ 行 bbox 并集（top-left 原点）+ 页宽高
前端 ──▶ 归一化 bbox 百分比定位到页图（高亮不烧录，切换零延迟）
```

## 自动评审执行链

`services/scoring_service.py` 创建运行并冻结输入，新运行交给
`services/automatic_scoring_service.py`；`services/automation_service.py` 统一维护恢复与完成判定。

1. 加载并核对冻结清单；全部“所选投标人 × 应评条款”构成预期矩阵，缺记录同样未完成。
2. 跳过已完成项，仅对未完成项执行初评 → 关联文件定向检索 → OCR 补读 → 按需视觉核验 → AI 复评 → 确定性重算，默认最多增加两轮。
3. 资格与符合性输出 `pass / fail / unverifiable`。`unverifiable` 只表示外部事实缺记录；读取或技术故障不能使用该结论。所有投标人继续计分和排名。
4. 客观项由 AI 核验事实、程序计件和套公式；主观项由 AI 按细则评分。弃权、错误引文、案例核验失败与证据不足进入补评；关联页面存在未解决的读取故障时，局部有效引文也不能使该项结束；确认材料缺失后按细则扣分。
5. 报价缺失或冲突先自动补抽，依赖的上游结果完整后才计算价格。严格价格引擎拒绝未执行门槛、未知公式、缺失费率及近似替代，记录报价纳入或排除基准的理由。
6. 每项分数、证据与完成状态同一事务保存。文本证据逐页核验；视觉模型通过现有网关预算、缓存与数据分级机制调用，未配置所需能力明确报错。
7. 技术故障重试后仍失败时整场 `failed`，保留成功项；`POST /runs/{run_id}/resume` 返回 `{run_id, job_id}`，幂等恢复未完成项，同一运行禁止并发执行。输入变化须新建运行。
8. 完成前在最终写事务内重新核对文件、解析页、报价、规则及模型配置的冻结清单；运行期间输入变化也会失败并要求新建运行。工作台、聚合、排名和报告共用完整性判定：完整矩阵全部有效终态、计分项具有合法终分且运行成功，才能发布正式报告和最终排名。失败运行只展示已完成结果和故障，不批量改写历史状态。

查询返回 `automation.expected/completed/failed/remaining/resumable`。评分详情与聚合项返回
只读 `auto_review`，用于展示补评过程和视觉观察；原始模型请求与图片数据不对外返回。
历史人工模式仍走原复核与防锚定路径，不适用于新建自动运行。

## 任务模型

`jobs` 表 + 进程内 asyncio worker：提交(queued) → 领取(running，原子更新 + 心跳) → 处理器按 `ctx.advance()` 上报进度并检查取消 → 终态(succeeded/failed/cancelled)。
心跳超时的僵尸任务可被重新拾取；`JobRunner.submit()` 是将来换任务框架（如 kuncore workflow）的唯一接缝。
自动评审额外持有 `data_dir()/run_locks/{run_id}.lock` 的操作系统锁，防止超时回收后仍存活的旧进程与新进程同时评标。共享数据库的进程须使用同一数据目录；进程退出或被终止时锁自动释放，锁文件保留。重复 worker 遇到占用时退出，不覆盖原任务或运行状态。

## 为迁移预留的接缝

| 接缝 | 现在 | 将来 |
|---|---|---|
| `LLMProvider` + `Gateway` | OpenAI 兼容 provider（未配置 fail-fast） | kuncore model_router |
| `JobRunner` | jobs 表 + 进程内 worker | kuncore workflow（含人工审批卡点） |
| `StorageBackend` | 本地磁盘 | S3 / MinIO |
| ORM | 纯 SQLAlchemy，无 SQLite 专有 SQL | 改 `DATABASE_URL` 即迁 Postgres |

---

## 变更补充（2026-10-13，详见 docs/decisions/0001~0003）

**历史兼容**：2026-10-13 的双轨约定保留用于解释存量结果，0003 已由
[0007](decisions/0007-automatic-review-completion.md) 取代。`BID_EVAL_AUTO_MODE=1`（默认）
新建运行使用上述执行器与完成门槛；置 0 保留历史人工流程和离线回归。两路径共用
ReviewRecord/audit 留痕，旧“最低档兜底、无报价剔除、直接定稿”不适用于自动运行。

**数据模型增补**：
- `score_records.review_status` 扩 `'auto_confirmed'`（自动裁决终态，CHECK 已迁移，
  `scripts/migrate_auto_review.py`）；`machine_status` 全自动下可落 `fail`/`rejected` 语义
- `rule_packages` 版本链：`new_version` 仅 frozen 可派生，origin/template_*/library_version
  随版本链延续；物理删除项目走 `services/project_service.py` 外键依赖序（审计保留）
- **法规规则包**（rules/library/law_packs/general-law）：84 条一条一文件 + pack.json +
  policies.json（18 个法条原文），`law_pack_loader` fail-fast 组装 `{package, criteria}`
  喂既有 `law_import_service`（origin='law'/p_source='P0'/免签批，法条逐条核验 fail-closed）
- 解析层：主引擎 **PyMuPDF**（`parsing/pdf_pymupdf.py`，扫描页逐页 OCR 兜底
  `parsing/ocr.py` rapidocr，磁盘缓存），`BID_EVAL_PDF_ENGINE=pdfplumber` 一行回退
- 长文档：`engine/selection.py` 章节约束选段（编译超 30k 按档位关键词选页；
  投标文件超 20k 按评分项关键词选段）；compile 传 max_tokens（默认 8192）
