# -*- coding: utf-8 -*-
"""材料分类词表与类别映射。

四类（用户口径）↔ Criterion.category（评分快照口径）：
- qualification（资格及证明文件） ← qualification | compliance
- commercial（商务服务）          ← commercial
- technical（技术方案）            ← technical
- price（报价）                    ← price

category 管检索分桶，tags_json 管证据语义（营业执照/社保证明/开标一览表…），
两者解耦：检索按 category 软过滤，跨类材料靠 tag 继续全库可见。
"""
from __future__ import annotations

MATERIAL_CATEGORIES = ("qualification", "commercial", "technical", "price", "other")

CATEGORY_LABELS: dict[str, str] = {
    "qualification": "资格及证明文件",
    "commercial": "商务服务",
    "technical": "技术方案",
    "price": "报价",
    "other": "其他",
}

# 顺序即优先级：同一标题命中多个类别词时取先声明者（price 最先——报价表名
# 常含"投标/报价"，易与商务词交叉）。
# 词同时是细粒度 tag（命中词原样落 tags_json）。
MATERIAL_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (
        "price",
        (
            "开标一览表",
            "开标一览",
            "报价一览",
            "投标报价",
            "报价表",
            "分项报价",
            "投标总价",
            "报价函",
            "报价单",
            "最后报价",
        ),
    ),
    (
        "qualification",
        (
            "资格证明",
            "资格文件",
            "资格声明",
            "资格审查",
            "资质证书",
            "营业执照",
            "信用中国",
            "社保证明",
            "社保缴纳",
            "纳税证明",
            "完税证明",
            "无行贿",
            "审计报告",
            "承诺函",
            "劳动合同",
            "职称证书",
            "资质证明",
            "资格及证明",
        ),
    ),
    (
        "commercial",
        (
            "商务偏离",
            "商务服务",
            "商务响应",
            "服务承诺",
            "售后",
            "培训方案",
            "培训计划",
            "运维服务",
            "业绩",
            "类似项目",
            "合同复印件",
            "验收报告",
            "用户证明",
            "发票",
            "商务部分",
        ),
    ),
    (
        "technical",
        (
            "技术方案",
            "实施方案",
            "技术路线",
            "架构设计",
            "质量保证",
            "质量保障",
            "应急预案",
            "项目管理",
            "进度计划",
            "技术偏离",
            "技术参数",
            "技术部分",
            "服务方案",
            "保障方案",
        ),
    ),
]

# 细粒度标签的同义归并（tags_json 落归并后的词，basis 记原词）
TAG_NORMALIZE: dict[str, str] = {
    "开标一览": "开标一览表",
    "报价一览": "报价表",
    "报价单": "报价表",
    "社保缴纳": "社保证明",
    "完税证明": "纳税证明",
    "合同复印件": "合同",
    "验收报告": "验收",
    "用户证明": "业主证明",
}


def normalize_tag(word: str) -> str:
    return TAG_NORMALIZE.get(word, word)


def category_for_keyword(word: str) -> str | None:
    """命中词 → 类别（按 MATERIAL_KEYWORDS 声明序）。"""
    for cat, kws in MATERIAL_KEYWORDS:
        if word in kws:
            return cat
    return None


def all_keywords() -> list[tuple[str, str]]:
    """[(category, keyword)] 平铺（扫描用）。"""
    return [(cat, kw) for cat, kws in MATERIAL_KEYWORDS for kw in kws]


def material_category_for(criterion: dict) -> str:
    """评分项 → 材料检索目标类。

    qualification|compliance→qualification；其余直接映射；
    未识别（理论不可达，CHECK 约束挡住）→ other（软过滤退化为全库优先）。
    """
    cat = str(criterion.get("category") or "").strip()
    if cat in ("qualification", "compliance"):
        return "qualification"
    if cat in ("commercial", "technical", "price"):
        return cat
    return "other"


# 业绩三态证据词 → 材料标签词（2026-10-30 阶段4）：派生自 TAG_NORMALIZE 归并目标
# （合同复印件→合同、验收报告→验收、用户证明→业主证明），发票无归并词原样落 tags。
# 页级强标签（material_index.rules.CONF_STRONG + method=rule）命中这些词时，
# 业绩三态判定借为 tag_signal 先验（见 case_evidence.classify_materials）。
EVIDENCE_TAG_WORDS: dict[str, tuple[str, ...]] = {
    "contract": ("合同",),
    "invoice": ("发票",),
    "acceptance": ("验收", "业主证明"),
}
