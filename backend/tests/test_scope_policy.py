"""围串标条款留空策略：只识别明确的串通类条款，不误伤普通资格条款。"""
from __future__ import annotations

from app.rules.scope import OUT_OF_SCOPE_REASON, collusion_reason, split_out_of_scope


def test_explicit_collusion_clause_is_out_of_scope():
    criterion = {"id": 1, "code": "c17", "name": "串通投标情形（否决投标）",
                 "description": "投标人有下列情形之一的，将被认定为串通投标行为并否决其投标。"}
    assert collusion_reason(criterion) == OUT_OF_SCOPE_REASON


def test_same_environment_mechanism_is_out_of_scope():
    criterion = {"name": "不同投标人投标文件由同一单位或个人编制",
                 "description": "不同投标人从同一IP地址下载采购文件、上传投标文件。"}
    assert collusion_reason(criterion) == OUT_OF_SCOPE_REASON


def test_same_deposit_account_is_out_of_scope():
    criterion = {"name": "投标保证金来源核查",
                 "source_quote": "不同投标人的投标保证金从同一账户转出，视为串通投标。"}
    assert collusion_reason(criterion) == OUT_OF_SCOPE_REASON


def test_same_legal_representative_or_controlling_relationship_is_out_of_scope():
    criterion = {"code": "c15", "name": "不得为同一负责人或存在控股管理关系的单位同时投标",
                 "description": "单位负责人为同一人或者存在控股、管理关系的不同单位，"
                                "不得参加同一标段投标或者未划分标段的同一招标项目投标。"}
    assert collusion_reason(criterion) == OUT_OF_SCOPE_REASON


def test_ordinary_qualification_clause_is_kept():
    criterion = {"name": "营业执照注册资本门槛",
                 "description": "注册资本不得低于人民币1000万元，须提供营业执照复印件。"}
    assert collusion_reason(criterion) is None


def test_conflict_of_interest_clause_is_not_collusion_scope():
    criterion = {"name": "不得与招标项目设计、技术规格编制或咨询公司存在关联",
                 "source_quote": "投标人不得与本次招标项下设计、编制技术规格的公司存在关联。"}
    assert collusion_reason(criterion) is None


def test_split_preserves_kept_order_and_records_reason():
    criteria = [
        {"id": 1, "code": "c1", "name": "营业执照"},
        {"id": 2, "code": "c2", "name": "串通投标情形（否决投标）"},
        {"id": 3, "code": "c3", "name": "业绩要求"},
    ]
    kept, excluded = split_out_of_scope(criteria)
    assert [c["code"] for c in kept] == ["c1", "c3"]
    assert excluded == [{
        "id": 2, "code": "c2", "name": "串通投标情形（否决投标）",
        "score_type": None, "source_page": None, "reason": OUT_OF_SCOPE_REASON,
    }]
