# -*- coding: utf-8 -*-
"""材料存在性评分（presence）单元测试。

覆盖 run144 的 4 个 existence 项形态：目录/索引定位 + 整页扫描/正文材料判定，
不依赖 LLM/OCR/DB。"""
from __future__ import annotations

from decimal import Decimal

from app.engine import presence


def _page(no: int, text: str = "", ratio: float = 0.0, *,
          doc: int = 1, source: str = "text", **extra) -> dict:
    return {
        "document_id": doc,
        "page_no": no,
        "text": text,
        "source": source,
        "embedded_image_ratio": ratio,
        **extra,
    }


def _criterion(name: str, description: str, *, max_score: str = "1",
               per_item: str = "1", field: str = "items") -> dict:
    return {
        "name": name,
        "score_type": "count",
        "max_score": max_score,
        "description": description,
        "params": {
            "_objective_form": "existence",
            "field": field,
            "per_item": per_item,
            "max_items": "1",
        },
    }


BANK = _criterion(
    "银行资信",
    "投标人具有银行资信证明且未出现负面信息，得1分，否则不得分。"
    "【证明材料：提供开标日前三个月之内出具的银行资信证明】",
)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def test_presence_route_matches_existence_items() -> None:
    assert presence.is_presence_criterion(BANK)


def test_presence_route_excludes_certificates_and_tiers() -> None:
    cert = _criterion("体系认证", "提供认证证书的得1分。", field="certificates")
    assert not presence.is_presence_criterion(cert)
    tiered = _criterion("财务制度", "提供近三年财务报表的得2分。", max_score="2", per_item="1")
    tiered["params"]["tiers"] = [{"min_count": 2, "score": "2"}]
    assert not presence.is_presence_criterion(tiered)


def test_sample_route_requires_in_total_false() -> None:
    sample = {
        "name": "样品评审", "score_type": "subjective", "max_score": "10",
        "params": {"in_total": False, "_objective_form": "unparsed_objective"},
    }
    assert presence.is_sample_criterion(sample)
    sample["params"]["in_total"] = True
    assert presence.is_sample_criterion(sample)
    sample["name"] = "样品材料"
    assert not presence.is_sample_criterion(sample)


# ---------------------------------------------------------------------------
# 单项三态
# ---------------------------------------------------------------------------
def _bank_pages(ratio: float, text: str = "2.12.2 银行资信证明") -> list[dict]:
    return [
        _page(1, "详细评审索引表\n4 见投标文件（10）页 银行资信\n"),
        _page(10, text, ratio),
    ]


def test_present_via_index_and_full_page_scan() -> None:
    result = presence.score_material_presence(BANK, _bank_pages(0.67))
    assert result.state == "present"
    assert result.score == Decimal("1.00")
    assert "内容未核验" in result.detail
    assert result.material_pages == [{"document_id": 1, "page_no": 10}]


def test_present_via_substantive_text_page() -> None:
    text = "银行资信证明：我行兹证明投标人资信状况良好，未出现负面信息。" * 6
    result = presence.score_material_presence(BANK, _bank_pages(0.0, text))
    assert result.state == "present"


def test_absent_only_when_window_located_and_all_text_readable() -> None:
    result = presence.score_material_presence(BANK, _bank_pages(0.0))
    assert result.state == "absent"
    assert result.score == Decimal(0)
    assert result.reason_code == "presence_absent"


def test_no_window_is_unverified_not_zero() -> None:
    pages = [_page(1, "无关内容"), _page(2, "其他材料")]
    result = presence.score_material_presence(BANK, pages)
    assert result.state == "unverified"
    assert result.score is None


def test_untrusted_window_is_unverified() -> None:
    """指针只在邻行命中（置信度 0.7）且窗口首部无评分项线索 → 不判 0。"""
    pages = [
        _page(1, "详细评审索引表\n4 见投标文件（10）页\n银行资信\n"),
        _page(10, "企业简介 作为国内优秀的印刷包装企业"),
        _page(11, "历史沿革与发展"),
    ]
    result = presence.score_material_presence(BANK, pages)
    assert result.state == "unverified"


def test_document_grouping_does_not_mix_page_numbers() -> None:
    pages = [
        _page(1, "详细评审索引表\n4 见投标文件（10）页 银行资信\n", doc=1),
        _page(10, "2.12.2 银行资信证明", doc=1),
        _page(10, "银行资信证明正文", ratio=0.67, doc=2),
    ]
    result = presence.score_material_presence(BANK, pages)
    assert result.state == "absent"


# ---------------------------------------------------------------------------
# 多年
# ---------------------------------------------------------------------------
AUDIT = _criterion(
    "财务制度",
    "提供近3年（2022年、2023年、2024年）经第三方审计的财务报表的得2分，"
    "提供不全的均不得分。",
    max_score="2", per_item="2",
)


def _audit_pages(*, year_2022=True, year_2022_material=True) -> list[dict]:
    toc = "目录\n（1）2024 年财务报表（经第三方审计）.....10\n" \
          "（2）2023 年财务报表（经第三方审计）.....20\n"
    if year_2022:
        toc += "（3）2022 年财务报表（经第三方审计）.....30\n"
    target = 30 if year_2022_material else 0.0
    return [
        _page(1, toc),
        _page(10, "（1）2024 年财务报表", 0.67),
        _page(20, "（2）2023 年财务报表", 0.67),
        _page(30, "（3）2022 年财务报表", target),
    ]


def test_multi_year_all_present() -> None:
    result = presence.score_material_presence(AUDIT, _audit_pages())
    assert result.state == "present"
    assert result.score == Decimal("2.00")
    assert result.years == {"2022": "present", "2023": "present", "2024": "present"}


def test_multi_year_missing_text_only_is_zero() -> None:
    result = presence.score_material_presence(
        AUDIT, _audit_pages(year_2022_material=False))
    assert result.state == "absent"
    assert result.score == Decimal(0)
    assert result.years["2022"] == "missing"


def test_multi_year_unknown_year_is_unverified() -> None:
    pages = [
        _page(1, "目录\n（1）2024 年财务报表（经第三方审计）.....10\n"
                  "（2）2023 年财务报表（经第三方审计）.....20\n"),
        _page(10, "（1）2024 年财务报表", 0.67),
        _page(20, "（2）2023 年财务报表", 0.67),
    ]
    result = presence.score_material_presence(AUDIT, pages)
    assert result.state == "unverified"
    assert result.score is None
    assert result.years["2022"] == "unknown"


# ---------------------------------------------------------------------------
# 样品数量档位
# ---------------------------------------------------------------------------
def _sample_criterion() -> dict:
    return {
        "name": "样品评审",
        "score_type": "subjective",
        "max_score": "10",
        "description": "样品评审由样品评审专家小组负责评审。",
        "params": {"in_total": False, "_objective_form": "unparsed_objective"},
    }


def _sample_pages(count: int) -> list[dict]:
    pages = [_page(1, "目录\n样品评审.....10\n")]
    for offset in range(count):
        pages.append(_page(10 + offset, "样品照片" if offset % 2 == 0 else "", 0.5))
    return pages


def test_sample_volume_tiers() -> None:
    cases = {1: Decimal("3.00"), 3: Decimal("6.00"),
             6: Decimal("8.00"), 10: Decimal("10.00")}
    for count, expected in cases.items():
        result = presence.score_sample_materials(_sample_criterion(), _sample_pages(count))
        assert result is not None, count
        assert result.score == expected, (count, result.score)
        assert "数量档位" in result.detail


def test_sample_without_section_falls_through() -> None:
    pages = [_page(1, "无关内容"), _page(2, "合同正文")]
    assert presence.score_sample_materials(_sample_criterion(), pages) is None


# ---------------------------------------------------------------------------
# absence 防线共用判据
# ---------------------------------------------------------------------------
def test_unverified_full_page_scan_signal() -> None:
    assert presence.is_unverified_full_page_scan(_page(1, "", 0.67))
    assert not presence.is_unverified_full_page_scan(_page(1, "", 0.67, source="ocr_img"))
    assert not presence.is_unverified_full_page_scan(_page(1, "", 0.67, known_blank=True))
    assert not presence.is_unverified_full_page_scan(_page(1, "正文" * 200, 0.67))
    assert not presence.is_unverified_full_page_scan(_page(1, "", 0.05))
    # 拼接扫描页：单图 <0.35 但图像占比≥15% 且正文稀少 → 仍是未 OCR 扫描件
    assert presence.is_unverified_full_page_scan(_page(1, "页眉", 0.24))
    assert not presence.is_unverified_full_page_scan(_page(1, "正文" * 200, 0.24))


def test_multi_year_alternative_with_unknown_year_is_unverified() -> None:
    """主集缺 2024 可判 0，但替代集（2021-2023）含 unknown → 不能判 0。"""
    criterion = {
        "name": "财务制度",
        "score_type": "count",
        "max_score": "2",
        "description": "提供近3年（2022年、2023年、2024年）经第三方审计的财务报表的得2分，"
                       "提供不全的均不得分。若尚未取得2024年度，则提供（2021年、2022年、2023年）年度。",
        "params": {"_objective_form": "existence", "field": "items",
                   "per_item": "2", "max_items": "1"},
    }
    pages = [
        _page(1, "目录\n（1）2024 年财务报表（经第三方审计）.....10\n"
                  "（2）2023 年财务报表（经第三方审计）.....20\n"
                  "（3）2022 年财务报表（经第三方审计）.....30\n"),
        _page(10, "（1）2024 年财务报表", 0.0),
        _page(20, "（2）2023 年财务报表", 0.67),
        _page(30, "（3）2022 年财务报表", 0.67),
    ]
    result = presence.score_material_presence(criterion, pages)
    assert result.state == "unverified", result.years
    assert result.years["2021"] == "unknown"


def test_toc_tagged_full_page_scan_still_counts_as_material() -> None:
    """材料分类器会把只有页码的扫描页误标 toc；整页扫描必须先于标签计数。"""
    page = _page(1, "", 0.67, material_tags=["toc"])
    assert presence.classify_material_page(page, BANK) == "scan"
    assert presence.is_unverified_full_page_scan(page)
