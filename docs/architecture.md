# 架构与数据模型

## 分层

```
┌─────────────────────────────────────────────────────────────┐
│ 前端（React + Ant Design）                                   │
│  工作台 / 规则审核 / 投标人 / 评分矩阵 / 证据核验 / 复核 / 报告 │
└──────────────────────────┬──────────────────────────────────┘
                           │ REST（/api）· SSE（任务进度）
┌──────────────────────────▼──────────────────────────────────┐
│ API 层（api/）— 薄：参数校验 + 调 service + 组装响应           │
│  防锚定、否决必须人工确认等约束在此强制                       │
├─────────────────────────────────────────────────────────────┤
│ 服务层（services/）— 业务编排，不 import fastapi，可被 CLI 直调 │
│  ingest / rule / scoring / review / ranking / report / audit │
├──────────────┬──────────────┬───────────────┬───────────────┤
│ rules/       │ engine/      │ evidence/     │ parsing/      │
│ 规则契约      │ 纯代码评分    │ 定位+核验      │ 页/条款树      │
│ 编译器        │ 价格/排名     │ 页图渲染       │ 中文编号       │
│ 12 校验器     │ 三态/计数     │ (verified 唯一 │ 归一化         │
│ 冻结+哈希     │ 主观分校验层  │  写入点)       │               │
├──────────────┴──────────────┴───────────────┴───────────────┤
│ llm/ 网关（缓存/限速/预算/数据分级） → Mock | OpenAI 兼容      │
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

**score_records**：唯一约束 `(run_id, bidder_id, criterion_id)` · `rule_version + package_content_hash`（任何分都能回答"按哪版规则算的"）· `machine_score`（确定性引擎）· `ai_suggested_score`（建议，永不直接成最终分）· `human_score`（人工独立分，防锚定）· **`final_score`（只有三处能写）** · `ai_revealed_at`（解锁时间戳）· `evaluator_actor + confirm_time`（法定评审职责归属）。

**evidence_citations**：`document_id + page_no + node_id + quote(逐字) + bbox_json + page_size_json` · `verified`（只由 verifier 写）· `verify_method`(exact/ws_insensitive/prefix_fallback/sentence_fallback) · `candidates_json`（定位失败的近似候选，交人工挑选）。

## 证据定位与核验

```
引文 ──归一化（全角/引号/乘号/零宽）──▶ 候选页排序（hint 优先 → 倒排粗筛 → 逐页）
  ├─ L1  精确子串（归一化后逐字）        → verified=True
  ├─ L1b 空白不敏感（软换行不影响逐字）   → verified=True
  ├─ L2  前缀回退（60/40/25/15 字）      → 候选交人工
  ├─ L3  句读切段匹配                    → 候选交人工
  └─ 未命中 → 返回最相似候选（含页号），进复核队列（引用造假路径）
命中 ──▶ index_map 映射回原文区间 ──▶ 行 bbox 并集（top-left 原点）+ 页宽高
前端 ──▶ 归一化 bbox 百分比定位到页图（高亮不烧录，切换零延迟）
```

## 评分运行顺序（`services/scoring_service.py`）

1. 加载冻结快照（注入 `package_id/rule_version/content_hash`）
2. 读取各投标人文本与结构化事实（`engine/facts.py`）
3. **资格/符合性**：检查原语 → 三态（pass / recommend_reject / need_human / conflict_flagged）；建议否决者计入排除集
4. **价格分**：排除集之外的报价算基准价 → 逐家算分（Decimal，上下限夹取，ROUND_HALF_UP）→ 写入 `final_score`（引擎权限）
5. **计数分**：业绩/认证计数（证书以"证书复印件表"为准，正文声称但无证书 → 冲突标记）
6. **主观分**：一家 × 一项一次调用（不含权重、不含其他投标人）→ 敌意输入校验层 → 只写 `ai_*` 字段
7. 证据核验（引用逐条定位）→ `evidence_status`；未核验的主观分降级待人工

## 任务模型

`jobs` 表 + 进程内 asyncio worker：提交(queued) → 领取(running，原子更新 + 心跳) → 处理器按 `ctx.advance()` 上报进度并检查取消 → 终态(succeeded/failed/cancelled)。
心跳超时的僵尸任务可被重新拾取；`JobRunner.submit()` 是将来换任务框架（如 kuncore workflow）的唯一接缝。

## 为迁移预留的接缝

| 接缝 | 现在 | 将来 |
|---|---|---|
| `LLMProvider` + `Gateway` | Mock / OpenAI 兼容 | kuncore model_router |
| `JobRunner` | jobs 表 + 进程内 worker | kuncore workflow（含人工审批卡点） |
| `StorageBackend` | 本地磁盘 | S3 / MinIO |
| ORM | 纯 SQLAlchemy，无 SQLite 专有 SQL | 改 `DATABASE_URL` 即迁 Postgres |
