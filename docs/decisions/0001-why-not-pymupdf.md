# 决策记录 0001：是否使用 PyMuPDF

- 日期：2026-10-09
- 状态：**已反转（2026-10-13，主路径切换为 PyMuPDF，见文末）**

## 背景

PDF 文本与坐标提取的候选方案：

| 方案 | 许可证 | 能力 |
|---|---|---|
| PyMuPDF (fitz) | **AGPL-3.0**（或向 Artifex 购买商业许可） | 最快，文本/坐标/渲染一体 |
| pdfplumber | MIT | 文本、行/词 bbox、表格检测（基于 pdfminer.six） |
| pypdfium2 | Apache-2.0 / BSD-3 | 页面渲染（PDFium），速度快 |

本系统面向招标人/代理机构交付，存在**闭源商用与私有化交付**的现实场景。AGPL 的传染性意味着：
一旦以 PyMuPDF 作为运行依赖对外提供网络服务，整个系统须以 AGPL 开放源码。

参考项目 `tender-evaluation-assistant` 的 `docs/decisions/0002-pymupdf-licences.md` 已就此踩坑，
本项目直接规避。

## 决定

1. **主解析路径**：`pdfplumber`（文本 + 行级 bbox + 表格检测）+ `pypdfium2`（页图渲染）。
2. **PyMuPDF 不进入 requirements**；保留 `parsing/pdf_pymupdf.py` 作为**可选加速**入口（本地实验用），
   默认关闭，且不参与任何对外交付的依赖闭包。
3. docx 走 `python-docx`（MIT）。

## 影响

- 解析性能略低于 PyMuPDF（pdfminer 较慢），实测样例（6 页招标文件 + 5 份投标文件）全链路 < 30 秒，可接受。
- 渲染由 pypdfium2 承担（PDFium，BSD 系），中文页图无字体坑。
- 若未来客户明确接受 AGPL，可一行配置切换到 PyMuPDF 加速路径（`Extractor` 协议已预留）。

## 附带教训（同批次踩坑记录）

- **表格单元格折行**：PDF 中一行长判据会被排版切成多行，按行正则匹配会截断判据。
  档位解析必须把续行拼回同一单元格（`llm/mock.py::_levels_for`）。
- **软换行 vs 逐字引用**：模型逐字引用常不含换行产生的空格，导致"引文看似一致却核验失败"。
  证据定位增加 `ws_insensitive`（空白不敏感）一级，与 L1 精确命中同为 `verified=True`。
- **目录页误判**：仅凭"编号密集"会把条款列表页误判为目录。判据须加"条目以页码/点线结尾"
  （`numbering.py::looks_like_toc`）。
- **SQLite 事务隔离**：未提交的行对其他连接不可见。核验（独立会话）必须在事务提交后进行
  （`rules/compiler.py` 的"先提交、后核验、再回填"）。

---

## 反转记录（2026-10-13）：主路径切换为 PyMuPDF

按项目方向调整（用户指令），主解析路径改用 PyMuPDF，理由：

1. **真实上传失败修复需要**：pdfplumber 主路径的魔数检查（头 5 字节必须 %PDF）
   会拒收政府平台下载的带电子签章/前导垃圾字节的 PDF；PyMuPDF 天然容忍前导垃圾。
2. **文字层提取更稳**：`get_text("dict")` 的行/坐标质量优于 pdfminer 的长尾表现
   （CID 字体乱码等），且内置 `find_tables`。
3. **OCR 集成底座**：扫描件 OCR 兜底（rapidocr-onnxruntime）需要稳定的页渲染与
   文本坐标底座，PyMuPDF 一体化提供。
4. **AGPL 义务可满足**：系统定位为招标人/代理机构**内部自用部署**，向最终用户
   提供源码的义务可接受并以此留痕。

回退开关：`BID_EVAL_PDF_ENGINE=pdfplumber` 一行切回（pdfplumber 保留为回退引擎，
MIT 闭包不变，不加 OCR）。requirements.txt 已更新。
