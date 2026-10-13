# 决策记录 0002：法规通用规则固化为仓库内规则包

- 日期：2026-10-13
- 状态：已采纳

## 背景

S-Law 的 87 条通用评审规则（FORM/QUAL/RES/REJ）此前只以**外部制品**存在
（`E:\ZJUBJ\法规库\00_索引\生成_规则包_通用规则库全量.json`），按项目临时导入 DB。
两个实际问题：

1. **路径漂移**：`scripts/import_law_rules.py` 默认源指向 `E:\ZJUBJ\261010\文件\法规库\`，
   该目录上移为 `E:\ZJUBJ\法规库\` 后默认路径失效，真制品端到端测试长期被 skipif 跳过。
2. **规则不是一等公民**：87 条规则不受版本管理、无加载期契约校验、无法独立于
   外部目录存在——与同类系统（mwb/coll_det 围串标规则包 `rules/packs/bid-rigging-common`）
   的"仓库内声明式规则包"框架相比，少了专家可审、git 可溯、坏包 fail-fast 这一层。

## 决定

1. **包固化**：新增 `backend/app/rules/library/law_packs/general-law/`——
   `pack.json`（元信息 + 制品指纹 artifact_sha256_16）+ `policies.json`（21 个被引
   法条节点原文登记）+ `rules/*.json`（87 条，一条一文件，文件名==code）。
   包文件提交 git，是唯一事实源。
2. **契约与加载器**：`law_pack_schema.py`（extra=forbid 契约）+
   `law_pack_loader.py`（fail-fast 加载，组装为 `{package, criteria}` 喂给既有
   `law_import_service`——**导入服务与 API 零改动**）。
3. **生成通道唯一**：`scripts/build_law_pack.py` 从法规库制品生成/同步；`--check`
   抓手改漂移；幂等（同制品重跑逐字节一致，pack.json 不含时间戳，指纹记
   artifact_sha256_16）。法条引用在生成期对**外部知识库与仓库 `data/law_kb.json`**
   双端核验，查不出原文零写入。
4. **默认源切换**：`import_law_rules.py` 缺省用仓库包 + 仓库 vendored 知识库；
   外部制品导入改走显式 `--artifact/--kb`。
5. **测试**：`test_law_pack.py` 九用例；`test_law_e2e.py` 真制品用例取消 skipif，
   改用仓库包。

## 影响

- 87 条规则的变更路径变为：改法规库制品 → 重跑 build → diff 审查 → 提交。
  手改包内文件会被 `--check` 与契约校验抓住。
- 法规库目录的移动不再影响运行时：导入/校验/测试只依赖仓库内包与
  `data/law_kb.json`；只有 build 脚本依赖外部目录且可显式传参。
- 5 条 `招标文件://投标人须知前附表` 引用原样保留在包内（external_refs 白名单），
  导入期既有逻辑置空并记 `dropped_nodes`。
- coll_det 框架的"行业包继承（extends）"仅在 pack.json 预留字段，暂不实现——
  等出现第二个包再设计叠加语义。
