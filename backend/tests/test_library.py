"""通用评分规则库（S0）：模板契约、加载器、校验器全绿。

覆盖：
- 4 个内置模板全部可加载、通过 schema 强校验；
- 模板实例化后的快照形态跑既有 run_validators，只允许 TEMPLATE_FALLBACK_SIGNOFF error
  （模板兜底未签批）且无其他 error —— 保证签批后 confirm/freeze 不被既有校验器卡死；
- 坏模板 fail-fast（文件名≠template_id / 档位不闭合 / 否决项带分值 / 总分≠100）。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.rules.library.loader import LibraryError, load_templates, template_content_hash
from app.rules.library.schema import RuleTemplateFile
from app.rules.validators import run_validators

LIBRARY_DIR = Path(__file__).resolve().parents[1] / "app" / "rules" / "library" / "templates"

ALL_TEMPLATE_IDS = [
    "comprehensive-goods",
    "comprehensive-works",
    "comprehensive-services",
    "lowest-price",
]


def _snapshot_for_template(tid: str) -> dict:
    """把模板条目组装成快照形状（等价 load_criteria_for_snapshot 的模板路径输出）。"""
    tpl = load_templates()[tid]
    criteria = []
    for c in tpl.criteria:
        item = {
            "code": c.template_code,
            "category": c.category,
            "name": c.name,
            "description": c.description,
            "max_score": c.max_score,
            "weight": None,
            "score_type": c.score_type,
            "is_veto": c.is_veto,
            "veto_consequence": c.veto_consequence,
            "source_node_id": None,
            "source_page": None,
            "source_quote": None,
            "status": "needs_signoff",
            "origin": "template",
            "template_code": c.template_code,
            "template_hash": None,
            "library_version": tpl.library_version,
            "p_source": "P5",
            "signoff": None,
            "levels": [
                {"label": lv.label, "min": lv.min, "max": lv.max, "criteria": lv.criteria}
                for lv in c.levels
            ],
            "check_type": c.checks[0].check_type if c.checks else None,
            "params": c.checks[0].params if c.checks else {},
            "formula": (
                {
                    "formula_type": c.formula.formula_type,
                    "benchmark_method": c.formula.benchmark_method,
                    "k": c.formula.k,
                    "upper_bound": c.formula.upper_bound,
                    "lower_bound": c.formula.lower_bound,
                    "rounding": c.formula.rounding,
                }
                if c.formula
                else None
            ),
        }
        criteria.append(item)
    return {"package": {"total_max": 100}, "criteria": criteria}


def test_all_templates_load_and_hash() -> None:
    tpls = load_templates()
    assert set(tpls) == set(ALL_TEMPLATE_IDS)
    for tid in ALL_TEMPLATE_IDS:
        tpl = tpls[tid]
        assert tpl.template_id == tid
        assert len(tpl.criteria) >= 7
        # 模板级内容哈希稳定（canonical JSON 的 SHA-256 前 12 位）
        assert len(template_content_hash(tpl)) == 12


def test_template_snapshot_validators_only_signoff_errors() -> None:
    """实例化后校验器只应报 TEMPLATE_FALLBACK_SIGNOFF error（模板兜底未签批）。"""
    for tid in ALL_TEMPLATE_IDS:
        snapshot = _snapshot_for_template(tid)
        findings = run_validators(snapshot)
        errors = [f for f in findings if f.severity == "error"]
        assert errors, tid
        for f in errors:
            assert f.code == "TEMPLATE_FALLBACK_SIGNOFF", (tid, f.code, f.message)
        # 签批后：全部条目置 signoff，不应再有 error
        for c in snapshot["criteria"]:
            c["signoff"] = {"by": "tester", "at": "2026-01-01T00:00:00", "reason": "测试签批"}
            c["status"] = "verified"
        assert [f for f in run_validators(snapshot) if f.severity == "error"] == [], tid


def test_template_criteria_contract() -> None:
    """模板条目的类别/档位/总分必须与既有契约一致（字段词汇表契约）。"""
    for tid in ALL_TEMPLATE_IDS:
        tpl = load_templates()[tid]
        # 否决项不得给分，非否决项必须给分
        for c in tpl.criteria:
            if c.is_veto:
                assert c.max_score is None, (tid, c.template_code)
            else:
                assert c.max_score is not None, (tid, c.template_code)
            if c.score_type == "subjective":
                assert c.levels, (tid, c.template_code)
            else:
                assert not c.levels, (tid, c.template_code)
        # 非否决项合计 = 100
        total = sum(c.max_score for c in tpl.criteria if not c.is_veto)
        assert total == Decimal("100"), (tid, total)


def _write_template(tmp_path: Path, raw: dict, filename: str) -> Path:
    p = tmp_path / filename
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return p


def test_bad_template_rejected(tmp_path) -> None:  # noqa: ANN001
    good = json.loads((LIBRARY_DIR / "comprehensive-goods.json").read_text("utf-8"))

    # 文件名 ≠ template_id
    d = tmp_path / "case_filename"
    d.mkdir()
    bad = dict(good)
    _write_template(d, bad, "renamed.json")
    with pytest.raises(LibraryError, match="template_id"):
        load_templates(d)

    # 档位不闭合（最低档下界 ≠ 0）
    d = tmp_path / "case_levels"
    d.mkdir()
    bad = json.loads((LIBRARY_DIR / "comprehensive-goods.json").read_text("utf-8"))
    for c in bad["criteria"]:
        if c["score_type"] == "subjective":
            c["levels"][-1]["min"] = 1
            break
    _write_template(d, bad, f"{bad['template_id']}.json")
    with pytest.raises(LibraryError, match="LEVEL_BOTTOM_MISMATCH"):
        load_templates(d)

    # 否决项带分值（VETO_SCORED）
    d = tmp_path / "case_veto_score"
    d.mkdir()
    bad = json.loads((LIBRARY_DIR / "comprehensive-goods.json").read_text("utf-8"))
    for c in bad["criteria"]:
        if c["is_veto"]:
            c["max_score"] = 5
            break
    _write_template(d, bad, f"{bad['template_id']}.json")
    with pytest.raises(LibraryError, match="VETO_SCORED|否决项不得设置 max_score"):
        load_templates(d)

    # 非否决项总分 ≠ 100（改 count 项分值，避免破坏档位闭合）
    d = tmp_path / "case_total"
    d.mkdir()
    bad = json.loads((LIBRARY_DIR / "comprehensive-goods.json").read_text("utf-8"))
    for c in bad["criteria"]:
        if c["score_type"] == "count":
            c["max_score"] = 5
            break
    _write_template(d, bad, f"{bad['template_id']}.json")
    with pytest.raises(LibraryError, match="合计"):
        load_templates(d)

    # 未知字段（词汇表契约：extra=forbid）
    d = tmp_path / "case_extra"
    d.mkdir()
    bad = json.loads((LIBRARY_DIR / "comprehensive-goods.json").read_text("utf-8"))
    bad["criteria"][0]["quote"] = "模板不应携带原文"
    _write_template(d, bad, f"{bad['template_id']}.json")
    with pytest.raises(LibraryError, match="校验失败"):
        load_templates(d)


def test_rule_template_file_pydantic() -> None:
    """schema 模型可直接解析（Decimal 字段、模板一致性校验）。"""
    raw = json.loads((LIBRARY_DIR / "lowest-price.json").read_text("utf-8"))
    tpl = RuleTemplateFile.model_validate(raw)
    assert tpl.method == "lowest-price"
    assert tpl.criteria[0].is_veto
    assert tpl.criteria[-1].formula is not None
    assert tpl.criteria[-1].formula.formula_type == "lowest_ratio"