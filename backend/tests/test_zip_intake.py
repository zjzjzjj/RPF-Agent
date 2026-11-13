"""项目包解压与结构识别：编码修复、包装根剥除、错误码、安全边界与限额。"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.errors import (
    ImportBundleError,
    ZipDuplicateBidder,
    ZipEmptyTender,
    ZipMissingBidDir,
    ZipMissingTenderDir,
    ZipNoBidders,
    ZipTooLarge,
)
from app.ingest.zip_intake import extract_bundle, peek_zip, plan_from_names


def _make_zip(path: Path, entries: dict[str, str], modes: dict[str, int] | None = None) -> Path:
    """entries: 条目名 → 内容；modes: 条目名 → external_attr 高 16 位（st_mode）。"""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zi = zipfile.ZipInfo(name)
            if modes and name in modes:
                zi.external_attr = modes[name] << 16
            zf.writestr(zi, data)
    return path


STANDARD = {
    "招标/某项目(招标文件).pdf": "%PDF-1.4 tender",
    "招标/评审材料.pdf": "%PDF-1.4 review",
    "投标/公司甲/开标一览表.pdf": "%PDF-1.4 price",
    "投标/公司甲/投标文件.pdf": "%PDF-1.4 bid",
    "投标/公司乙/开标一览表.pdf": "%PDF-1.4 price2",
}


def test_extract_standard_layout(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path / "p.zip", STANDARD)
    layout = extract_bundle(zp, tmp_path / "out")
    assert layout.wrapper_stripped is False
    assert layout.root == tmp_path / "out"
    # 主招标文件：文件名含"招标"优先
    primary = [t for t in layout.tender_files if t.is_primary]
    assert len(primary) == 1
    assert "招标文件" in primary[0].path.name
    assert primary[0].path.read_text(errors="ignore").startswith("%PDF")
    assert {t.path.name for t in layout.tender_files} == {"某项目(招标文件).pdf", "评审材料.pdf"}
    assert [c.name for c in layout.companies] == ["公司甲", "公司乙"]
    jia = layout.companies[0]
    assert jia.folder.is_dir()
    assert {p.name for p in jia.files} == {"开标一览表.pdf", "投标文件.pdf"}
    assert all(p.exists() for c in layout.companies for p in c.files)


def test_primary_prefers_tender_name(tmp_path: Path) -> None:
    """同目录多份 PDF 时，文件名含"招标"者为主文件（评审材料不应被误当）。"""
    plan = plan_from_names(
        ["招标/a.pdf", "招标/项目(招标文件).pdf", "投标/公司甲/开标一览表.pdf"]
    )
    assert plan.tender_files == [("项目(招标文件).pdf", True), ("a.pdf", False)]


def test_wrapper_root_stripped(tmp_path: Path) -> None:
    entries = {f"包装根/{k}": v for k, v in STANDARD.items()}
    zp = _make_zip(tmp_path / "p.zip", entries)
    layout = extract_bundle(zp, tmp_path / "out")
    assert layout.wrapper_stripped is True
    assert layout.root == tmp_path / "out" / "包装根"
    assert (layout.root / "招标").is_dir()
    assert layout.companies[0].folder == tmp_path / "out" / "包装根" / "投标" / "公司甲"


def test_wrapper_two_levels_stripped(tmp_path: Path) -> None:
    entries = {f"外层/内层/{k}": v for k, v in STANDARD.items()}
    zp = _make_zip(tmp_path / "p.zip", entries)
    layout = extract_bundle(zp, tmp_path / "out")
    assert layout.wrapper_stripped is True
    assert layout.root == tmp_path / "out" / "外层" / "内层"
    assert layout.companies[0].folder.is_dir()


def test_missing_tender_dir(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path / "p.zip", {"投标/公司甲/开标一览表.pdf": "x"})
    with pytest.raises(ZipMissingTenderDir) as e:
        peek_zip(zp)
    assert e.value.code == "ZIP_MISSING_TENDER_DIR"


def test_missing_bid_dir(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path / "p.zip", {"招标/项目(招标文件).pdf": "x"})
    with pytest.raises(ZipMissingBidDir) as e:
        peek_zip(zp)
    assert e.value.code == "ZIP_MISSING_BID_DIR"


def test_empty_tender(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path / "p.zip", {"招标/说明.txt": "x", "投标/公司甲/开标一览表.pdf": "x"})
    with pytest.raises(ZipEmptyTender) as e:
        peek_zip(zp)
    assert e.value.code == "ZIP_EMPTY_TENDER"


def test_no_bidders(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path / "p.zip", {"招标/项目(招标文件).pdf": "x", "投标/说明.txt": "x"})
    with pytest.raises(ZipNoBidders) as e:
        peek_zip(zp)
    assert e.value.code == "ZIP_NO_BIDDERS"


def test_duplicate_bidder(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path / "p.zip",
        {
            "招标/项目(招标文件).pdf": "x",
            "投标/公司甲/a.pdf": "x",
            "投标/公司甲 /b.pdf": "x",  # 去空白后同名
        },
    )
    with pytest.raises(ZipDuplicateBidder) as e:
        peek_zip(zp)
    assert e.value.code == "ZIP_DUPLICATE_BIDDER"


def test_gbk_filenames_recovered(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """模拟 Windows 资源管理器打包：GBK 字节 + 无 UTF-8 flag → 乱码修复后可识别。"""
    names = {gbk_mojibake(k): v for k, v in STANDARD.items()}
    monkeypatch.setattr(zipfile.ZipInfo, "_encodeFilenameFlags", _cp437_no_flag)
    zp = _make_zip(tmp_path / "p.zip", names)
    plan = peek_zip(zp)
    assert [c[0] for c in plan.companies] == ["公司甲", "公司乙"]
    assert plan.tender_files[0][0].endswith("(招标文件).pdf")


def gbk_mojibake(name: str) -> str:
    return name.encode("gbk").decode("cp437")


def _cp437_no_flag(self) -> tuple[bytes, int]:  # noqa: ANN001
    return self.filename.encode("cp437"), 0


def test_zip_slip_skipped(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path / "p.zip",
        {
            "../evil.pdf": "x",
            "C:/evil2.pdf": "x",
            "投标/公司甲/../越级.pdf": "x",
            "招标/项目(招标文件).pdf": "x",
            "投标/公司甲/ok.pdf": "x",
        },
    )
    layout = extract_bundle(zp, tmp_path / "out")
    assert not (tmp_path / "evil.pdf").exists()
    assert not (tmp_path / "out" / "evil2.pdf").exists()
    assert not (tmp_path / "out" / "投标" / "越级.pdf").exists()
    assert any("不安全条目" in w for w in layout.warnings)
    assert (layout.companies[0].folder / "ok.pdf").exists()


def test_nested_archive_kept_unextracted(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path / "p.zip",
        {
            "招标/项目(招标文件).pdf": "x",
            "投标/公司甲/打样.zip": "PK\x03\x04fake",
            "投标/公司甲/开标一览表.pdf": "x",
        },
    )
    layout = extract_bundle(zp, tmp_path / "out")
    nested = layout.companies[0].folder / "打样.zip"
    assert nested.is_file()
    assert not (layout.companies[0].folder / "打样").exists()
    assert any("嵌套压缩包未解压" in w for w in layout.warnings)


def test_symlink_skipped(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path / "p.zip",
        {"招标/项目(招标文件).pdf": "x", "投标/公司甲/link.pdf": "target"},
        modes={"投标/公司甲/link.pdf": 0o120777},
    )
    layout = extract_bundle(zp, tmp_path / "out")
    assert not (layout.companies[0].folder / "link.pdf").exists()
    assert any("特殊文件" in w for w in layout.warnings)


def test_bid_root_loose_files_warned(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path / "p.zip",
        {
            "招标/项目(招标文件).pdf": "x",
            "投标/散落说明.txt": "x",
            "投标/公司甲/开标一览表.pdf": "x",
        },
    )
    layout = extract_bundle(zp, tmp_path / "out")
    assert [c.name for c in layout.companies] == ["公司甲"]
    assert any("根下的散文件已忽略" in w for w in layout.warnings)


def test_entries_limit(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_IMPORT_MAX_ENTRIES", "3")
    zp = _make_zip(tmp_path / "p.zip", STANDARD)  # 5 条目
    with pytest.raises(ZipTooLarge) as e:
        peek_zip(zp)
    assert e.value.code == "ZIP_TOO_LARGE"


def test_uncompressed_limit(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_IMPORT_MAX_UNCOMPRESSED_MB", "0")
    zp = _make_zip(tmp_path / "p.zip", STANDARD)
    with pytest.raises(ZipTooLarge):
        extract_bundle(zp, tmp_path / "out")


def test_not_a_zip(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(ImportBundleError) as e:
        peek_zip(bad)
    assert e.value.code == "ZIP_INVALID"
