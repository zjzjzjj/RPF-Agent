# general-law：法律通用规则包（固化版）

87 条法规通用评审规则（FORM 形式评审 37 / QUAL 资格评审 23 / RES 响应性评审 12 / REJ 否决投标 15），
依据《招标投标法》《招标投标法实施条例》《评标委员会和评标方法暂行规定》30 号令、87 号令、
《电子招标投标办法》等编制，每条规则的 `source_node_id` 指向 `法条://法规名/条号`，
原文登记在 `policies.json`。

## 本目录是生成物，禁止手改 `rules/*.json`

唯一修改通道：改法规库制品后重跑生成脚本：

```bash
python scripts/build_law_pack.py                # 从 E:\ZJUBJ\法规库\00_索引\ 制品同步
python scripts/build_law_pack.py --check        # 只比对，漂移 exit 2（测试/CI 护栏）
python scripts/build_law_pack.py --set-version  # 内容变化时人工定版
```

- 同一制品重跑输出逐字节一致（pack.json 记 `artifact_sha256_16`，不含时间戳）；
- 法条引用在生成期对 **外部知识库** 与 **仓库 `data/law_kb.json`** 双端核验，查不出原文直接失败、零写入；
- 5 条 `招标文件://投标人须知前附表` 引用（FORM-002/008/028/034/035）是随项目变化的检查项，
  在 `pack.json.external_refs` 声明，导入项目时由既有逻辑置空并记 `dropped_nodes`。

## 消费方式

```python
from app.rules.library.law_pack_loader import load_law_pack, law_pack_content_hash

doc = load_law_pack()        # {"package": {...}, "criteria": [87 条]}
```

文档结构与法规库制品完全一致，可直接交给
`app/services/law_import_service.py::import_law_package / validate_only`，
或 `POST /api/projects/{id}/rules/import-law`。导入后 origin='law'、p_source='P0'、
免签批；confirm/freeze 时逐条过法条知识库核验（fail-closed）。
