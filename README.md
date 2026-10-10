# RPF Agent（智能评标系统）

面向**招标人与评审人员**的 AI 智能评标系统：读取招标文件 → 结构化评分细则（可冻结）→ 解析多家投标文件 → 逐项评分 → 计算排名，**每个评分结果都带可核验的原文依据**。

这不是聊天机器人。核心是一条"规则编译器 + 确定性评分引擎 + 证据审计"的流水线：

```
招标文件 ──解析──▶ 条款树 ──LLM抽取+人工确认──▶ 冻结的规则包（内容哈希）
                                                     │
投标文件 ──解析──▶ 结构化事实 ──┬─ 资格/符合性审查（纯代码，三态）
                                ├─ 价格分公式（纯代码，可复算）
                                ├─ 计数类评分（纯代码）
                                └─ 技术分建议（LLM + 逐字引用 + 代码核验）
                                                     │
                                          人工逐项复核（留痕）──▶ 排名 ──▶ 评标报告
```

## 设计原则（合规底线）

| 原则 | 落地方式 |
|---|---|
| 确定性任务交给程序 | 客观分（资格/符合性、价格公式、计数分）全部纯 Python + Decimal，可复算；有独立真值双向核对 |
| 语义判断交给大模型 | 技术分只产出**建议分**，永不直接成为最终分（`final_score` 仅三处可写：确定性引擎 / 人工确认 / 人工改分） |
| 法定评审职责交给制度主体 | 否决类只能"建议否决 + 依据"，必须人工确认；防锚定（先人工独立判断再解锁 AI 建议）；无"全部采纳"按钮 |
| 无证据不评分 | 证据四件套：文件 + 页码 + 坐标 + 逐字原文；`verified` 只由 `evidence/verifier.py` 写；证据不核验则降级待人工 |
| 规则冻结 | draft → confirmed → frozen（内容哈希 + 快照），冻结后一切写入 409，评分只认快照 |
| 全程留痕 | 每次确认/改分/否决记录操作人、时间、理由；报告附完整留痕链，可直接用于答复质疑 |
| 数据不出域 | 数据分级：`confidential`（真实投标文件）只允许发往本地模型端点 |

对应法规锚点：《招标投标法实施条例》49 条（只能用招标文件载明的标准）、87 号令 55 条（独立评价）、财库〔2012〕69 号（客观分必须一致）。

## 快速开始

```bash
# 后端（Python 3.12）
cd backend
pip install -r requirements.txt        # 国内可用镜像：pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
python -m pytest -q                    # 57 项测试，全离线

# 端到端验收（Mock 模型，离线可重复）
cd ..
python scripts/generate_sample_tender.py
python scripts/generate_sample_bids.py
python scripts/run_e2e.py --assert-expected

# 启动服务
cd backend && uvicorn app.main:app --reload --port 8000

# 前端（Node 18+）
cd frontend
npm install --registry=https://registry.npmmirror.com
npm run dev                            # http://localhost:5173（/api 代理到 8000）
```

接真实模型（OpenAI 兼容，如 DeepSeek / Qwen）：

```bash
export BID_EVAL_LLM_PROVIDER=openai
export BID_EVAL_LLM_BASE_URL=https://api.deepseek.com/v1
export BID_EVAL_LLM_API_KEY=sk-...
export BID_EVAL_LLM_MODEL=deepseek-chat
```

## 使用流程（四个强制人工卡点）

1. **新建项目** → 上传招标文件（PDF/Word）→ 自动解析出页文本与条款树
2. **编译评分规则** → AI 抽取评分项/档位/价格公式 → 12 条确定性校验器跑出 error/warn 清单
3. **★卡点① 逐条确认规则**（无"全部采纳"；歧义档位必须人工补全）→ **★卡点② 冻结**（生成内容哈希，此后只读）
4. **添加投标人** → 上传投标文件（一家可多份）→ 抽取报价 → **★卡点③ 人工确认报价**
5. **发起评分运行** → 资格/符合性三态 + 价格分 + 计数分 + 技术分建议（含逐字引用与核验）
6. **★卡点④ 逐项复核**：评分矩阵点进证据核验工作台（PDF 页图 + bbox 高亮）；先给独立分再解锁 AI 建议
7. **排名与报告** → 确定性排名（含并列说明）→ 导出 HTML 评标报告（含规则版本哈希、证据、留痕）

## 目录结构

```
backend/app/
├─ parsing/      PDF/docx 解析、中文条款编号、条款树、归一化（定位与核验共用）
├─ evidence/     引文→页码+坐标定位（三级回退）、核验（verified 唯一写入点）、页图渲染
├─ rules/        规则契约、LLM 编译器、12 条确定性校验器、冻结（canonical JSON + 哈希）
├─ engine/       纯代码评分：价格公式、排名并列、资格三态原语、事实抽取、主观分校验层
├─ llm/          provider 抽象（Mock/OpenAI 兼容）、网关（缓存/限速/预算/数据分级）
├─ services/     业务编排（评分运行、规则状态机、报告组装）
├─ api/          REST 端点（防锚定在服务端强制）
└─ jobs/         jobs 表 + 进程内 worker（进度/取消/心跳/僵尸任务回收）
frontend/src/    React + Ant Design：工作台、规则审核、评分矩阵、证据核验、复核队列、报告
scripts/         样例数据生成（内容与答案同源）、端到端验收
```

## 验收指标（`scripts/run_e2e.py` 实测）

| 指标 | 目标 | 现状 |
|---|---|---|
| 证据核验率（Mock 引文逐字） | 100% | ✅ 100% |
| 客观分复算一致性 | 0 差异 | ✅ 价格分与独立真值 0 差异 |
| 规则覆盖 | 7 否决 + 1 价格 + 3 技术 + 2 商务 | ✅ 13/13，校验 error 0 |
| 冻结不可变性 | 冻结后写入 409 | ✅ |
| 建议否决语义 | 保留在榜、绝不自动否决 | ✅ |
| 端到端耗时（5 家 × 13 项，离线） | < 60 秒 | ✅ 约 25 秒 |

## MVP 边界（已预留接口）

OCR（扫描件，`parse_status='ocr_required'` + `parsing/ocr.py` 协议已就绪）· 向量检索 · 多人权限（`X-Actor` 已真实记录）· Word/PDF 报告导出 · Excel 报价表 · 补遗文件时间线 · 多轮报价 · 电子签章。

## 许可证注意

主解析路径使用 pdfplumber (MIT) + pypdfium2 (Apache/BSD)，**刻意不用 AGPL 的 PyMuPDF**（见 `docs/decisions/0001-why-not-pymupdf.md`）。
