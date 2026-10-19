# RPF Agent（智能评标系统）

面向**招标人与代理机构**的 AI 智能评标系统：读取招标文件 → 结构化评分细则（可冻结/可解冻）→ 解析多家投标文件 → 逐项评分 → 计算排名，**每个评分结果都带可核验的原文依据与完整留痕**。

核心是一条"规则编译器 + 通用法规规则包 + 确定性/LLM 评分引擎 + 证据审计"的流水线：

```
招标文件 ──PyMuPDF/OCR解析──▶ 条款树 ──┬─ LLM抽取评分细则（编译）
                                        └─ 通用法规规则包（87条法条规则，仓库内固化）
                                             │
                              规则包（可自动采纳/批量采纳）──▶ 冻结（内容哈希，可解冻出新版本）
                                             │
投标文件 ──解析──▶ 结构化事实 ──┬─ 资格/符合性审查（确定性原语 + LLM 带证据裁决）
                                ├─ 价格分公式（纯代码，可复算）
                                ├─ 计数类评分（纯代码）
                                ├─ 判断项（match：三态判定，不打分）
                                └─ 技术分建议（LLM + 逐字引用 + 代码核验）
                                             │
                        全自动：LLM 带原文证据裁决，直接收敛终态（auto_confirmed）
                        回退：人工三态复核（BID_EVAL_AUTO_MODE=0）
                                             │
                                    排名 ──▶ 评标报告（自动/人工裁决留痕）
```

## 两种运行模式

| | 全自动模式（默认） | 人工回退模式 |
|---|---|---|
| 开关 | `BID_EVAL_AUTO_MODE=1`（缺省） | `BID_EVAL_AUTO_MODE=0` |
| 机器判不了的条款（密封/盖章等 72 条 human_only） | LLM 带原文证据裁决（附逐字引用） | `need_human` 进复核队列 |
| 冲突信号（大小写金额、分项合计、串通） | LLM 裁决收敛 + 留痕 | `conflict_flagged` 待人工裁决 |
| 主观分 | LLM 直接写 `final_score`（给不出按最低档兜底） | 只写 `ai_suggested`，人工 confirm/override |
| 报价 | 评分入口自动确认（无报价者自动剔除留痕） | 人工确认（卡点） |
| 规则确认/冻结 | 阻断项自动采纳（逐条留痕） | 逐条 accept / 全部采纳按钮 |
| 报告 | 直接 **final**（含"自动裁决"记录） | 草稿 → 人工处置 → final |

两种模式共用同一数据模型与审计（`ReviewRecord`/`audit_logs`，全自动模式 actor="auto"）；任何路径都不丢弃留痕——**事后可完整还原每条结论的依据**。

## 设计原则

| 原则 | 落地方式 |
|---|---|
| 确定性任务交给程序 | 客观分（价格公式、计数分）纯 Python + Decimal，可复算；有独立真值双向核对 |
| 语义判断：LLM 带证据裁决 | 裁决输出强制附**逐字引用**（给不出原文 = 该字段 null，不得编造）；LLM 失败 → fail 终态（宁严勿纵，不悬挂） |
| 事实与判定分离 | 事实抽取（报价/业绩/证书）、信号计算、终态判读三层解耦；规则包只是数据，评估引擎是唯一结论层 |
| 无证据不评分 | 证据四件套：文件 + 页码 + 坐标 + 逐字原文；`verified` 只由 `evidence/verifier.py` 写 |
| 规则冻结 | draft → confirmed → frozen（内容哈希 + 快照），冻结后写入 409；**「解冻并创建新版本」**派生可编辑草稿（origin 溯源随版本链延续），历史评分不受影响 |
| 全程留痕 | 自动裁决/人工确认/改分/否决全部记录 actor、时间、理由；报告附完整留痕链 |
| 数据不出域 | 数据分级：`confidential`（真实投标文件）只允许发往本地模型端点 |
| 规则即数据 | 法规规则包 = 仓库内声明式 JSON（一条一文件 + pack.json 元信息 + policies.json 法条原文登记），生成脚本唯一通道（`--check` 防手改漂移），借鉴 coll_det 围串标规则包框架 |

对应法规锚点：《招标投标法实施条例》49 条（只能用招标文件载明的标准）、87 号令 55 条（独立评价）、财库〔2012〕69 号（客观分必须一致）。

## 快速开始

```bash
# 后端（Python 3.12）
cd backend
pip install -r requirements.txt        # 国内镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple
python -m pytest -q                    # 216 项测试，全离线（不依赖任何模型）

# 端到端验收（二选一，按运行模式；编译/裁决走真实模型——先配置模型，见下）
cd ..
python scripts/generate_sample_tender.py && python scripts/generate_sample_bids.py
python scripts/run_e2e.py                              # 人工回退模式口径（真实模型冒烟，约 3-10 分钟）
python scripts/run_auto_pipeline.py --assert-final     # 全自动模式口径（默认语义；87 条法规包
                                                       # × 5 家约 400 次裁决，约 30-120 分钟，
                                                       # 可用 BID_EVAL_LLM_CONCURRENCY 提并发）

# 启动服务
cd backend && uvicorn app.main:app --port 8000

# 前端（Node 18+）
cd frontend
npm install --registry=https://registry.npmmirror.com
npm run dev                            # http://localhost:5173（/api 代理到 8000）
```

存量数据库升级（在旧版本库上使用新模式时跑一次，幂等、自动备份）：

```bash
python scripts/migrate_auto_review.py     # review_status 扩 'auto_confirmed'
```

配置模型（**必须**：编译/语义裁决/主观分都走真实模型；未配置时 fail-fast 报 `LLM_NOT_CONFIGURED`，OpenAI 兼容，如 DeepSeek / Qwen / 硅基流动）：

```bash
export BID_EVAL_LLM_PROVIDER=openai
export BID_EVAL_LLM_BASE_URL=https://api.deepseek.com/v1   # 或 https://api.siliconflow.cn/v1
export BID_EVAL_LLM_API_KEY=sk-...
export BID_EVAL_LLM_MODEL=deepseek-chat
```

## 使用流程

1. **新建项目** → 上传招标文件（**PDF 含扫描件自动 OCR** / Word）→ 自动解析出页文本与条款树；解析/编译失败会在界面显示**具体原因**（如 `[NO_TEXT_LAYER] …`）并提供「重新解析」
2. **规则三选一（可组合）**：
   - **编译**：LLM 从招标文件抽取评分项/档位/价格公式（超长文档按章节优先级自动选段，任务栏显示选取范围）
   - **模板实例化**：四套内置评标办法模板（综合货物/服务/工程、最低价）作为基线，之后可上传招标文件**自动合并优化**（配对自动采纳：覆盖/追加/弃用全留痕）
   - **导入法规包**：规则页「导入法规包」按钮（`POST /projects/{id}/rules/import-law-pack`，按 `pack_id` 取仓库内固化包；也可用 `POST /rules/import-law` 投递制品 JSON 或 `scripts/import_law_rules.py`）——87 条法规通则，法条逐条核验 fail-closed
   - **人工新增评分项**：规则页「新增评分项」（仅草稿包，理由必填，编号 x1/x2…，来源标「人工」）
3. **批量处置**：「全部采纳」/「全部签批」按钮（公共理由留痕）→ 确认 → **冻结**（内容哈希，此后只读）
4. **添加投标人** → 上传投标文件 → 抽取报价（五级兜底：总价行 → 标签窗口 → 含税合计行 → **开标一览表章节扫描（税率配对判含税价）** → 语境启发）；抽不到「人工录入」兜底
5. **发起评分运行** → 资格/符合性（确定性原语 + LLM 裁决）+ 价格分 + 计数分 + 判断项 + 技术分（逐字引用与核验）；长标书**按评分项关键词选段**送审（尾部章节不再被盲截）
6. **排名与报告** → 确定性排名（含并列说明）→ HTML 评标报告（规则版本哈希、证据、自动裁决/人工处置留痕）——全自动模式直接**定稿**
7. **改规则** → 冻结包点「解冻并创建新版本」→ 复制为可编辑草稿（v N+1，原包只读留痕）→ 改后重新冻结
8. **删项目** → 项目列表删除按钮（二次确认；文档/规则/评分连带清理，审计留痕保留）

## 目录结构

```
backend/app/
├─ parsing/      PDF 主引擎 PyMuPDF（含扫描页 OCR 兜底）/pdfplumber 回退、docx、
│                中文条款编号、条款树、公共选段选页（selection）、归一化
├─ evidence/     引文→页码+坐标定位（三级回退）、核验（verified 唯一写入点）、页图渲染
├─ rules/        规则契约、LLM 编译器、12 条确定性校验器、冻结（canonical JSON + 哈希）、
│                规则包库（templates/ 内置模板 + law_packs/ 固化法规包，loader fail-fast）
├─ engine/       纯代码评分：价格公式、排名并列、资格三态原语、事实抽取（五级报价兜底）、
│                主观分校验层、跨投标人串通信号、LLM 裁决（auto_judge）、章节约束选段
├─ llm/          provider 抽象（OpenAI 兼容：DeepSeek/Qwen/GLM/Kimi/硅基流动；未配置 fail-fast）、
│                网关（缓存/限速/预算/数据分级）
├─ services/     业务编排（评分运行、规则状态机、法规导入、模板库、合并、报告、项目删除）
├─ api/          REST 端点（项目 CRUD / 规则 / 评分 / 报告）
└─ jobs/         jobs 表 + 进程内 worker（进度/取消/心跳/僵尸任务回收）
frontend/src/    React + Ant Design：项目列表（含删除）、工作台、规则（模板/编译/法规导入/
                 新增评分项/全部采纳/解冻）、投标人（报价五级兜底交互）、评分矩阵、
                 证据查看器、报告
scripts/         样例数据生成、双模式端到端验收（run_e2e / run_auto_pipeline）、
                 法规包生成（build_law_pack.py，唯一生成通道 + --check 漂移护栏）、
                 数据库迁移（migrate_rule_library / migrate_law_layer / migrate_auto_review）
```

## 法规规则包（law_packs/general-law）

87 条法规通用评审规则（FORM 形式 37 / QUAL 资格 23 / RES 响应性 12 / REJ 否决 15），依据《招标投标法》《实施条例》《评标委员会和评标方法暂行规定》30 号令、87 号令等编制，每条 `source_node_id: 法条://法规名/条号`，原文登记于包内 `policies.json`（21 个被引法条节点）。**包是仓库内生成物，禁止手改**：

```bash
python scripts/build_law_pack.py            # 从 E:\ZJUBJ\法规库\00_索引\ 制品生成/同步（幂等）
python scripts/build_law_pack.py --check    # 只比对，漂移 exit 2（测试/CI 护栏）
```

行业扩展预留 `extends` 继承语义（见 `docs/decisions/0002`）。

## 验收指标（`run_e2e.py` / `run_auto_pipeline.py` 实测）

| 指标 | 目标 | 现状 |
|---|---|---|
| 客观分复算一致性 | 0 差异 | ✅ pytest + 冒烟：价格分与独立真值 0 差异 |
| 报价抽取（确定性五级启发） | 与生成侧真值一致 | ✅ 5 家全对（run_e2e 精确断言） |
| 法规包导入 | 87 条全过法条核验 | ✅ error 0，快照带 law 溯源 |
| 冻结不可变性 | 冻结后写入 409 | ✅ |
| 全自动收敛 | 零 need_human 残留、报告直接 final | ✅ run_auto_pipeline `--assert-final`（auto 模式结构保证） |
| 人工回退语义 | 三态 + 复核队列 + 绝不自动否决 | ✅ pytest（conftest auto=0 守护） |
| 端到端耗时（真实模型） | — | ✅ run_e2e 约 3-10 分钟；run_auto_pipeline 全量裁决约 30-120 分钟（并发可调） |
| 测试 | 全部离线可重复 | ✅ 216 passed（不依赖任何模型） |

## 模型与超长文档处理

- **超长规则抽取**：招标文件超 30k 字符按"评分办法 > 资格/否决"章节优先级选页（相邻页半权重成段入选），选取范围落任务消息与 `result_json.selection_meta` 审计；
- **LLM 输出保护**：`BID_EVAL_LLM_MAX_TOKENS`（默认 8192，防 provider 默认 4K 截断长规则包 JSON）、超时默认 300s（`BID_EVAL_LLM_TIMEOUT_S`）、重试退避；
- **长标书评审**：投标文件超 20k 字符**按评分项关键词选段**（含相邻章节、保留原文次序），替代头部盲截——尾部"售后服务方案"等不再被系统性评成最低档；证据核验始终对照全量 `document_pages`，选段不影响可溯性。

## MVP 边界（已预留接口）

Excel 报价表结构解析（`.xlsx` 含"开标一览表/分项报价表"，需引入 openpyxl，mwb `xlsx_parser.py` 双开模式可整段移植——**二期**）· 向量检索 · 多人权限（`X-Actor` 已真实记录）· Word/PDF 报告导出 · 补遗文件时间线 · 多轮报价 · 电子签章。

## 许可证注意

主解析路径已切换为 **PyMuPDF（AGPL-3.0）**——2026-10-13 决策反转（系统定位内部自用部署，AGPL 义务可满足；容错与 OCR 底座收益），见 `docs/decisions/0001-why-not-pymupdf.md` 文末反转记录。闭源交付场景可 `BID_EVAL_PDF_ENGINE=pdfplumber` 一行切回 MIT 回退引擎。OCR 用 rapidocr-onnxruntime（Apache-2.0）。
