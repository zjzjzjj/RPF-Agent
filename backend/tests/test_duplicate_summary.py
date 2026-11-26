"""重复汇总父项（c38 形态）展示层判定测试。

测试2（多媒体维保）实证形态：LLM 把细则组行抽成 match 带分值父项（满分4），
骨架缺口补齐又生成 2 个计分子项（各2分，同 source_node_id）——分数层已有
duplicate_summary 兜底不给分，但列表仍双份呈现（2026-11-26 用户口径：
项目中不要出现重复计分项）。判定谓词必须精确命中该形态且不误杀。
"""
from __future__ import annotations

from app.services.duplicate_summary import (
    duplicate_summary_ids,
    is_duplicate_summary,
)

NODE = "tender:root:C12:SEC6:I5:N1"


def _crit(**kw):  # noqa: ANN001, ANN202
    base = {"id": 1, "score_type": "count", "max_score": None,
            "source_node_id": NODE, "description": "", "name": ""}
    base.update(kw)
    return base


def _c38_shape() -> list[dict]:
    """测试2 商务分四条同构数据（c38 父 + c39 异项 + c61/c62 子）。"""
    return [
        _crit(id=38, name="投标人资质实力", score_type="match", max_score=4,
              description="（1）投标人具有ISO9001质量管理体系认证证书等信息"
                          "并附证书扫描件得2分（2）有效期内的电子与智能化专业"
                          "承包资质得0.5分且具有建筑智能化工程得1分"),
        _crit(id=39, name="投标人业绩实力", score_type="count", max_score=6,
              description="具有视频监控、安防、系统维保类合同业绩，每份2分"),
        _crit(id=61, name="投标人资质实力（1）投标人具有ISO9001质量管理体系",
              score_type="count", max_score=2,
              description="（1）投标人具有ISO9001质量管理体系认证证书等信息"
                          "并附证书扫描件得2分"),
        _crit(id=62, name="投标人资质实力（2）有效期内的电子与智能化专业承包",
              score_type="count", max_score=2,
              description="（2）有效期内的电子与智能化专业承包资质得0.5分"
                          "且具有建筑智能化工程得1分"),
    ]


def test_c38_parent_hidden_children_kept() -> None:
    crits = _c38_shape()
    parent = crits[0]
    assert is_duplicate_summary(parent, crits)
    hidden = duplicate_summary_ids(crits)
    assert hidden == {38}
    # 正常项不受影响
    assert 39 not in hidden and 61 not in hidden and 62 not in hidden


def test_normal_match_without_score_never_hidden() -> None:
    """正常判断项（max_score=None）永不进谓词——资格/符合性 match 保留。"""
    crits = [
        _crit(id=1, name="符合性判断", score_type="match", max_score=None,
              description="投标文件报价出现前后不一致的按开标一览表为准"),
        _crit(id=2, name="子项A", score_type="count", max_score=2,
              description="投标文件报价出现前后不一致的按开标一览表为准得2分"),
        _crit(id=3, name="子项B", score_type="count", max_score=2,
              description="投标文件报价出现前后不一致的按开标一览表为准得2分"),
    ]
    assert not is_duplicate_summary(crits[0], crits)
    assert duplicate_summary_ids(crits) == set()


def test_single_child_keeps_parent() -> None:
    """只有 1 个计分兄弟 → 不构成「平铺替代」，父项保留（保守不误杀）。"""
    crits = [
        _crit(id=1, name="组父项", score_type="match", max_score=4,
              description="（1）证书扫描件得4分"),
        _crit(id=2, name="组父项（1）", score_type="count", max_score=4,
              description="（1）证书扫描件得4分"),
    ]
    assert not is_duplicate_summary(crits[0], crits)


def test_children_sum_exceeding_parent_keeps_parent() -> None:
    """Σ兄弟 > 父满分 → 不隐藏（宁显双份也不静默吞分值）。"""
    crits = [
        _crit(id=1, name="组父项", score_type="match", max_score=3,
              description="（1）证书得2分（2）资质得2分"),
        _crit(id=2, name="组父项（1）", score_type="count", max_score=2,
              description="（1）证书得2分"),
        _crit(id=3, name="组父项（2）", score_type="count", max_score=2,
              description="（2）资质得2分"),
    ]
    assert not is_duplicate_summary(crits[0], crits)


def test_different_source_node_not_sibling() -> None:
    """同名但 source_node_id 不同 → 非同细则兄弟，不隐藏。"""
    crits = [
        _crit(id=1, name="组父项", score_type="match", max_score=4,
              source_node_id="other:node",
              description="（1）证书得2分（2）资质得2分"),
        _crit(id=2, name="组父项（1）", score_type="count", max_score=2,
              description="（1）证书得2分"),
        _crit(id=3, name="组父项（2）", score_type="count", max_score=2,
              description="（2）资质得2分"),
    ]
    assert not is_duplicate_summary(crits[0], crits)


def test_non_match_parent_never_hidden() -> None:
    """c39 类正常计分项（非 match）不进谓词。"""
    crits = _c38_shape()
    assert not is_duplicate_summary(crits[1], crits)  # count 父项
