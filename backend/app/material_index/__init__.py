"""投标材料分类索引（material_index）。

四类标签（资格及证明文件/商务服务/技术方案/报价）+ 段级（表格/图片）归属：
- catalog：类别词表与 Criterion.category 映射
- rules：确定性规则层（目录/标题链/关键词/区间继承）
- llm_fallback：LLM 兜底（只填规则未判出的页）
- service：classify_material job、ensure_index、索引查询
- hints：评分侧消费的类别化材料索引文本
"""
