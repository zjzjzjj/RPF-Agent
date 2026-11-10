# RPF Agent（智能评标系统）

面向**招标人与代理机构**的 AI 智能评标系统：读取招标文件 → 结构化评分细则（可冻结/可解冻）→ 解析多家投标文件 → 逐项评分 → 计算排名，**每个评分结果都带可核验的原文依据与完整留痕**。

核心是一条"规则编译器 + 通用法规规则包 + 确定性/LLM 评分引擎 + 证据审计"的流水线：

```
招标文件 ──PyMuPDF/OCR解析──▶ 条款树 ──┬─ LLM抽取评分细则（编译）
                                        └─ 通用法规规则包（84条法条规则，仓库内固化）
                                             │
                              规则包（可自动采纳/批量采纳）──▶ 冻结（内容哈希，可解冻出新版本）
                                             │
投标文件 ──解析──▶ 结构化事实 ──┬─ 资格/符合性审查（AI 带证据形成最终结论）
                                ├─ 价格分公式（纯代码，可复算）
                                ├─ 计数类评分（纯代码）
                                ├─ 判断项（match：自动判定，不打分）
                                └─ 技术分评分（LLM + 逐字引用 + 代码核验）
                                             │
                        全自动：LLM 带原文证据裁决，直接收敛终态（auto_confirmed）
                        回退：人工三态复核（BID_EVAL_AUTO_MODE=0）
                                             │
                                    排名 ──▶ 评标报告（自动/人工裁决留痕）
```

## 全自动完成与故障恢复（2026-11-06）

默认自动评审资格、符合性、客观和主观评分的全部条款；未解决项自动补检关联文件、补做 OCR，并按需进行视觉复核，最多追加两轮。AI 的“不符合”是最终评审结论，投标人继续参与计分和排名；缺少平台回执或现场记录等外部证据时，明确记录“现有材料无法证实”的风险结论。

模型、OCR、证据核验或价格规则无法执行时，运行明确失败并保留已完成项，不写默认通过、零分或最低档替代结论。结果页提供“恢复自动评审”，接口为 `POST /api/runs/{run_id}/resume`；输入已变化时需新建运行。只有完整应评矩阵、最终分值和证据都通过检查，才生成正式报告及最终排名。

视觉复核需显式配置 `BID_EVAL_VISION_MODEL`。可通过 `BID_EVAL_VISION_BASE_URL`、`BID_EVAL_VISION_API_KEY` 指定视觉服务；缺省沿用文本模型的端点和凭据。未配置所需视觉能力时明确报错，补齐尚未使用的视觉能力后可续评。配置与行为依据见 [决策记录 0007](docs/decisions/0007-automatic-review-completion.md)。

```dotenv
BID_EVAL_AUTO_MODE=1
BID_EVAL_AUTO_REVIEW_ROUNDS=2
BID_EVAL_VISION_MODEL=<已部署且支持图像输入的模型ID>
# 使用独立视觉服务时设置以下两项；省略则沿用文本服务。
# BID_EVAL_VISION_BASE_URL=<视觉服务的兼容API端点>
# BID_EVAL_VISION_API_KEY=<视觉服务密钥>
```

PDF 会检查实际嵌入图像，文字层较长也不能跳过扫描图核验。含图片的 DOCX 当前无法可靠映射原始页图，会明确失败；应转换为保留分页的 PDF 后新建运行，避免把漏读图片当成材料缺失。

历史结果保留；后文涉及人工确认、三态待办的说明仅适用于 `BID_EVAL_AUTO_MODE=0` 的历史兼容路径，不是默认自动运行的兜底方式。

> 2026-11-10 更新：故障处理与出分语义有修订（故障默认保留已完成项续评、失败格写显式暂定分），见下节。

## 资格轻量化与全量出分（2026-11-10）

**资格只报告，不阻断**（[决策记录 0011](docs/decisions/0011-qualification-light.md)）：新运行默认 `qualification_mode=light`——每家投标人一次确定性预检 + 一次合并 LLM 快审（不 OCR、不逐条款深评），结论为"符合 / 不符合 / 现有材料无法证实 / 未深度复核"，**只写进报告，不参与计分、排名与价格基准价**；未知一律记"未深度复核"，不伪造通过。旧运行保留 full 模式；`BID_EVAL_QUALIFICATION_MODE_DEFAULT=full` 可切回。

**每个评分格都必须落终态**（[决策记录 0012](docs/decisions/0012-always-score-deterministic-objective.md)）：

- 模型/OCR/证据路径失败的格写**显式暂定分**（`basis=fallback`），进报告"兜底分清单"并带警示，**不隐藏缺失格、不阻塞定稿**；公式价格格不参与闭合（否则价格阶段被整体跳过）。`BID_EVAL_SCORE_CLOSURE=0` 恢复历史 `execution_failed` 语义。
- **客观分确定性快路径**：材料清单（material_inventory，页码绑定）+ 确定性计分计划（objective_fastpath）在 OCR/LLM **之前**给计数类格出分；未检出事实 = 0 分 + 明确理由，不整格失败。
- **主观分直评**（`BID_EVAL_SUBJECTIVE_DIRECT=1` 默认开）：按关键词选页（≤6 页）单轮 LLM 直接出分，缺引文/缺子项不挡出分；置 0 回退分层证据簇传统路径。
- **暂定价格**：严格门槛无法形成基准价时按门槛前口径暂定计算，价格分标 `price_provisional`，不再整体跳过。

**故障语义（R22）**：新运行默认 `failure_policy=continue`——故障保留已完成项、运行可落 partial 终态，`POST /api/runs/{run_id}/resume` 只补未完成项；进度/心跳锁失败不再杀任务。上一节"运行明确失败"描述的是故障即停的早期语义（旧运行 `failure_policy=stop` 仍逐字保持），两者以本节为准。

## 评分全流程

**总原则：AI 负责规则补全、证据与事实核验、符合性判断和主观评分；程序负责引文核验、完整性检查及客观计分。客观分只按冻结规则和已核验事实计算。**

### 招标侧：规则编译与冻结

```
招标文件上传
  → PyMuPDF 解析（文字层优先，扫描页才触发 OCR：PaddleOCR-VL 自托管 + rapidocr 回退）
  → 条款树（章节目录结构）
  → 规则编译 compile_rules：
      ① 确定性表格骨架抽取（find_tables，零 LLM）——评标细则是标准表格
      ② LLM 分块抽取补全（云端模型，按评标细则章节原子化分块，防超长截断）
      ③ 落库前对账校正（骨架↔LLM 逐行对账、Σ=100 闭合、父子双计裁剪、score_type 校正）
      ④ 12 条确定性校验器 → confirm → freeze
  → 冻结快照（canonical JSON + SHA-256 前 16 位）——此后评分只认快照，写入 409
```

规则是**结构化 JSON 数据**不是代码；LLM 只补骨架没覆盖的部分，错漏由对账与校验器修回。

### 投标侧：解析与证据准备

```
投标文件上传
  → 同款解析（文字层优先 + 扫描页 OCR）
  → 材料四类分类（material_index：资格及证明文件 / 商务服务 / 技术方案 / 报价；
     页级+段级标签，规则优先（目录/标题链/关键词/碎页继承），LLM 只兜未知页）
  → 业绩证据链（确定性，不调 LLM）：
      索引「第X页到第Y页」+ 页脚 offset 定位业绩章节 → PyMuPDF 还原案例表
      → 案例名前缀绑定材料页 → 合同/发票/验收三态标注（True/False/None，
        扫描页无特征判 None 不判 False——「不可见≠不存在」）
  → 报价抽取（五级兜底：总价行 → 标签窗口 → 含税合计行 → 开标一览表扫描 → 语境启发）
```

### 评分 run：逐项自动评审，依赖就绪后计算价格

| 阶段 | 内容 | 机制 |
|---|---|---|
| count | 计数分（最先执行） | 确定性快路径在 OCR/LLM 前出分（material_inventory + objective_fastpath）；未覆盖格再走 AI 逐件核验；故障按闭合写显式暂定分 |
| subjective | 主观分 | 默认直评：选页后单轮 AI 出分，缺引文/子项不挡；`BID_EVAL_SUBJECTIVE_DIRECT=0` 回退分层证据簇 + 引文闸门路径 |
| veto / match | 资格与符合性（收尾执行） | 默认轻量快审、只报告不阻断（见「资格轻量化」节）；全量模式 AI 输出“符合 / 不符合 / 现有材料无法证实”，投标人继续参与评分和排名 |
| price | 价格分（最后执行） | 报价缺失或冲突先自动补抽；全部计分格完成后由纯 Decimal 引擎执行门槛、基准价与冻结公式；无法形成基准价时暂定出分 |

```
初评 → 定向检索关联文件 → 补 OCR → 按需视觉核验 → AI 复评 → 确定性重算
  → 每项分数、证据与状态事务保存
  → 核对全部“所选投标人 × 应评条款” → 成功后发布最终排名与正式报告
```

文件内结论必须有有效的逐字引文或原页视觉证据（直评主观分与资格轻量快审除外，结果均明确标注、可回退）；确认缺失必须先完成关联材料检索。
引文经 `evidence/verifier.py` 逐页核验（精确 / 空白不敏感）。模型、OCR 等故障经自动
重试和补评仍失败时，按 `failure_policy` 保留已完成项（默认 continue，可落 partial 终态），
修复后通过 `POST /runs/{run_id}/resume` 续评。

## 自动评审与历史兼容

| | 全自动模式（默认，新建运行） | 历史人工模式兼容 |
|---|---|---|
| 开关 | `BID_EVAL_AUTO_MODE=1`（缺省） | `BID_EVAL_AUTO_MODE=0` |
| `human_only` 条款 | 同样自动评审；签章扫描按需视觉核验，外部事实缺记录明确证据边界 | `need_human` 进复核队列 |
| 冲突信号（大小写金额、分项合计） | LLM 裁决收敛 + 留痕 | `conflict_flagged` 待人工裁决 |
| 主观分 | 直评单轮出分事务写入 `final_score`（缺引文/子项不挡）；故障格写显式暂定分 | 只写 `ai_suggested`，人工 confirm/override |
| 报价 | 缺失或冲突自动补抽；无法形成基准价时暂定出分（`price_provisional`），不整场失败 | 人工确认（历史流程） |
| 规则确认/冻结 | 阻断项自动采纳（逐条留痕） | 逐条 accept / 全部采纳按钮 |
| 报告 | 完整矩阵全部落终态（含显式暂定分，列兜底清单）后 **final**；partial 运行出 partial 报告 | 草稿 → 人工处置 → final |

历史运行保留原样，通过新建自动重评运行消化待人工项，不批量改状态。自动运行记录
冻结文件哈希、报价、规则和模型配置；`POST /runs/{run_id}/resume` 幂等恢复未完成项，
输入变化须新建运行，已完成分数和证据不重复写入。

## 设计原则

| 原则 | 落地方式 |
|---|---|
| 确定性任务交给程序 | 客观分（价格公式、计数分）纯 Python + Decimal，可复算；有独立真值双向核对 |
| 语义判断：LLM 带证据裁决 | 结论附有效引文或页图证据；确认缺失与外部事实证据边界分别记录；技术故障最终使运行失败，不伪装业务不符合 |
| 事实与判定分离 | 事实抽取（报价/业绩/证书）、信号计算、终态判读三层解耦；规则包只是数据，评估引擎是唯一结论层 |
| 证据可追溯 | 原文证据保留文件、页码、坐标与逐字引文；视觉证据保留原页、哈希与观察记录；`verified` 只由核验器写 |
| 规则冻结 | draft → confirmed → frozen（内容哈希 + 快照），冻结后写入 409；**「解冻并创建新版本」**派生可编辑草稿（origin 溯源随版本链延续），历史评分不受影响 |
| 全程留痕 | 自动裁决/人工确认/改分/否决全部记录 actor、时间、理由；报告附完整留痕链 |
| 数据不出域 | 数据分级：`confidential`（真实投标文件）只允许发往本地模型端点 |
| 规则即数据 | 法规规则包 = 仓库内声明式 JSON（一条一文件 + pack.json 元信息 + policies.json 法条原文登记），生成脚本唯一通道（`--check` 防手改漂移），借鉴 coll_det 规则包框架 |

对应法规锚点：《招标投标法实施条例》49 条（只能用招标文件载明的标准）、87 号令 55 条（独立评价）、财库〔2012〕69 号（客观分必须一致）。

## 快速开始

```bash
# 后端（Python 3.12）
cd backend
pip install -r requirements.txt        # 国内镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple
python -m pytest -q                    # 离线回归（不依赖真实模型）

# 端到端验收（二选一，按运行模式；编译/裁决走真实模型——先配置模型，见下）
cd ..
python scripts/generate_sample_tender.py && python scripts/generate_sample_bids.py
python scripts/run_e2e.py                              # 人工回退模式口径（真实模型冒烟，约 3-10 分钟）
python scripts/run_auto_pipeline.py --assert-final     # 全自动模式口径（默认语义；84 条法规包
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

### 切到硅基流动（云端模型）

```bash
export BID_EVAL_LLM_PROVIDER=siliconflow
export BID_EVAL_LLM_BASE_URL=https://api.siliconflow.cn/v1
export BID_EVAL_LLM_API_KEY=sk-...
export BID_EVAL_LLM_MODEL=deepseek-ai/DeepSeek-V4.1-Flash   # 或 zai-org/GLM-5.3-Flash
export BID_EVAL_LLM_THINKING=auto                # auto|disabled|enabled
export BID_EVAL_LLM_DEFAULT_MAX_TOKENS=8192      # 推理模型的思考也算输出预算
export BID_EVAL_LLM_MAX_TOKENS=20000             # 规则抽取
export BID_EVAL_LLM_CONTEXT_TOKENS=65536
export BID_EVAL_LLM_TRANSIENT_ATTEMPTS=4         # 云端高峰期 503 重试次数
```

> 2026-10-21 实测：`zai-org/GLM-5.3-Flash` 高峰期间歇甚至持续返回
> `503 System is too busy`（同账户 GLM-5.2 / DeepSeek-V4.1-Flash 正常），
> 故默认改用 `deepseek-ai/DeepSeek-V4.1-Flash`。

要点：

- **推理模型的思考计入 `max_tokens`**：预算太小会出现 `finish_reason=length` 且
  `content` 为空。系统默认给每次调用兜底 `BID_EVAL_LLM_DEFAULT_MAX_TOKENS`，
  并在"空输出 + length"时抛出可操作错误。
- `BID_EVAL_LLM_DISABLE_THINKING=1` 走的是 **vLLM 专用** `chat_template_kwargs`，
  云端不认；云端请用 `BID_EVAL_LLM_THINKING=disabled`（`thinking:{"type":"disabled"}`）。
- **真实招标/投标文件属 `confidential`，默认只准本地端点**。要让云端模型处理这些
  文件，必须显式设置 `BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL=1`（默认 0）；开启后网关
  会按端点记一次性告警，`GET /api/health` 的 `allow_remote_confidential` 字段
  可随时确认当前状态。这等于接受"文件原文会离开本机发往第三方 API"。
- 切换后 `GET /api/health` 应显示 `llm_model` 为新模型；前端模型下拉来自
  `BID_EVAL_MODEL_PRESETS`（默认预设已含 GLM-5.3-Flash）。

## 使用流程

**规则有两条一等路径**：有招标文件时上传后自动匹配更新；没有招标文件时用内置默认规则直接评分。

1. **新建项目**（招标文件可选）→ 上传招标文件（**PDF 含扫描件自动 OCR** / Word）→ 自动解析出页文本与条款树；解析/编译失败会在界面显示**具体原因**（如 `[NO_TEXT_LAYER] …`）并提供「重新解析」
2. **规则**：
   - **上传即自动匹配**：招标文件解析成功后，后端自动排队「编译 + 匹配」并按评分办法更新规则
     （覆盖 / 弃用 / 追加全留痕），页面展示变更清单与总分/价格公式告警；
     规则包已确认/冻结时不自动改动，提示先「解冻并创建新版本」。也可手动「重新匹配」。
   - **无招标文件 → 使用默认规则**：规则页「使用默认规则」选五套内置模板（**综合默认（通用·推荐：价格50/技术40/商务10）**、综合货物/服务/工程、最低价），
     一键完成 **实例化 → 全部签批 → 确认 → 冻结**，随后直接进入投标人与评分。
   - **导入法规包**：规则页「导入法规包」按钮（`POST /projects/{id}/rules/import-law-pack`，按 `pack_id` 取仓库内固化包；也可用 `POST /rules/import-law` 投递制品 JSON 或 `scripts/import_law_rules.py`）——84 条法规通则，法条逐条核验 fail-closed
   - **逐条启用/停用**：规则列表右侧开关（仅草稿包；停用项退出计分与冻结快照，恢复按停用前状态回滚）
   - **人工新增评分项**：规则页「新增评分项」（仅草稿包，理由必填，编号 x1/x2…，来源标「人工」）
3. **批量处置**：「全部采纳」/「全部签批」按钮（公共理由留痕）→ 确认 → **冻结**（内容哈希，此后只读）
4. **添加投标人** → 上传投标文件 → 抽取报价（五级兜底：总价行 → 标签窗口 → 含税合计行 → **开标一览表章节扫描（税率配对判含税价）** → 语境启发）；抽不到「人工录入」兜底
5. **发起评分运行** → 资格/符合性（默认轻量快审，只报告不阻断）+ 价格分 + 计数分 + 判断项 + 技术分（逐字引用与核验；直评主观分单轮出分）；长标书**按评分项关键词选段**送审（尾部章节不再被盲截）
6. **评分结果** → 主从分栏：左栏排名（总分/价格分/技术分/商务分），右栏逐项**加减分明细 + 理由**
   （AI 评审理由 / 机器判定说明 / 人工复核理由，可下钻证据引文与原件高亮）；同页含**完整矩阵**与
   **报告与导出**两个标签页（HTML / 正式评审报告 HTML / Word），全自动模式报告直接**定稿**
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
│                主观分校验层、LLM 裁决（auto_judge）、章节约束选段
├─ llm/          provider 抽象（OpenAI 兼容：DeepSeek/Qwen/GLM/Kimi/硅基流动；未配置 fail-fast）、
│                网关（缓存/限速/预算/数据分级）
├─ services/     业务编排（评分运行、规则状态机、法规导入、模板库、合并、报告、项目删除）
├─ api/          REST 端点（项目 CRUD / 规则 / 评分 / 报告）
└─ jobs/         jobs 表 + 进程内 worker（进度/取消/心跳/僵尸任务回收）
frontend/src/    React + Ant Design：项目列表（含删除）、工作台、规则库与招标文件（上传自动匹配/
                 默认规则一键启用/逐条启停/法规导入/新增评分项/全部采纳/解冻）、
                 投标人（报价五级兜底交互）、
                 评分结果（排名 + 分项加减分明细与理由 + 完整矩阵 + 报告导出）、证据查看器
scripts/         样例数据生成、双模式端到端验收（run_e2e / run_auto_pipeline）、
                 法规包生成（build_law_pack.py，唯一生成通道 + --check 漂移护栏）、
                 数据库迁移（migrate_rule_library / migrate_law_layer / migrate_auto_review）
```

## 法规规则包（law_packs/general-law）

84 条法规通用评审规则（FORM 形式 37 / QUAL 资格 23 / RES 响应性 12 / REJ 否决 12），依据《招标投标法》《实施条例》《评标委员会和评标方法暂行规定》30 号令、87 号令等编制，每条 `source_node_id: 法条://法规名/条号`，原文登记于包内 `policies.json`（18 个被引法条节点）。围串标三条（REJ-008/009/010，条例 39/40/41）经 `build_law_pack.EXCLUDED_CODES` 产品级裁剪不随包发布——围串标由独立的围串标智能体负责。**包是仓库内生成物，禁止手改**：

```bash
python scripts/build_law_pack.py            # 从 E:\ZJUBJ\法规库\00_索引\ 制品生成/同步（幂等）
python scripts/build_law_pack.py --check    # 只比对，漂移 exit 2（测试/CI 护栏）
```

行业扩展预留 `extends` 继承语义（见 `docs/decisions/0002`）。

## 验收指标与验证入口

| 指标 | 目标 | 现状 |
|---|---|---|
| 客观分复算一致性 | 0 差异 | ✅ pytest + 冒烟：价格分与独立真值 0 差异 |
| 报价抽取（确定性五级启发） | 与生成侧真值一致 | ✅ 5 家全对（run_e2e 精确断言） |
| 法规包导入 | 84 条全过法条核验 | ✅ error 0，快照带 law 溯源 |
| 冻结不可变性 | 冻结后写入 409 | ✅ |
| 全自动完成 | 成功运行完整矩阵全部落终态、合法终分、零未完成项后 final；暂定分显式列兜底清单（不隐藏缺失格） | 离线自动评审/生命周期回归；真实模型运行 `run_auto_pipeline --assert-final` 验证 |
| 技术故障与续评 | 故障明确失败，保留成功项；恢复不重评、不重复证据 | `test_automatic_scoring` / `test_automation_lifecycle` |
| 人工回退语义 | 三态 + 复核队列 + 绝不自动否决 | ✅ pytest（conftest auto=0 守护） |
| 真实模型验收 | 配置文本与所需视觉能力后验证 | 补评轮数、材料规模和模型吞吐决定耗时；脚本会实际调用模型 |
| 测试 | 离线可重复 | **1365 passed**（2026-11-10 实测约 3.6 分钟；`python -m pytest -q`，固定测试 provider，不依赖真实模型） |

## 模型与超长文档处理

- **超长规则抽取**：招标文件超 30k 字符按"评分办法 > 资格/否决"章节优先级选页（相邻页半权重成段入选），选取范围落任务消息与 `result_json.selection_meta` 审计；
- **LLM 输出保护**：`BID_EVAL_LLM_MAX_TOKENS`（默认 8192，防 provider 默认 4K 截断长规则包 JSON）、超时默认 300s（`BID_EVAL_LLM_TIMEOUT_S`）、重试退避；
- **长标书评审**：投标文件超 20k 字符**按评分项关键词选段**（含相邻章节、保留原文次序），替代头部盲截——尾部"售后服务方案"等不再被系统性评成最低档；证据核验始终对照全量 `document_pages`，选段不影响可溯性。

## MVP 边界（已预留接口）

Excel 报价表结构解析（`.xlsx` 含"开标一览表/分项报价表"，需引入 openpyxl，mwb `xlsx_parser.py` 双开模式可整段移植——**二期**）· PDF 报告导出（HTML 与 Word 正式报告已交付）· 多人权限（`X-Actor` 已真实记录）· 补遗文件时间线 · 多轮报价 · 电子签章。

（已交付不再列入：**向量选段检索**——`BID_EVAL_SELECTION=vector` 走 bge-m3，失败回退关键词；**Word 报告导出**——`GET /projects/{id}/report/docx` 正式评审报告。）

## 许可证注意

主解析路径已切换为 **PyMuPDF（AGPL-3.0）**——2026-10-13 决策反转（系统定位内部自用部署，AGPL 义务可满足；容错与 OCR 底座收益），见 `docs/decisions/0001-why-not-pymupdf.md` 文末反转记录。闭源交付场景可 `BID_EVAL_PDF_ENGINE=pdfplumber` 一行切回 MIT 回退引擎。OCR 用 rapidocr-onnxruntime（Apache-2.0）。
