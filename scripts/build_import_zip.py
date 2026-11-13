"""项目包（zip）打包工具：把招标文件 + 投标文件夹集打成规范输入 zip。

输入格式（评标系统一键导入）：
    项目包.zip
    ├── 招标/                招标文件 PDF/DOCX（首个含"招标"者为主招标文件）
    └── 投标/
        ├── 公司甲/          每个公司一个文件夹（文件夹名 = 投标人名称）
        └── 公司乙/

用法：
    python scripts/build_import_zip.py --tender 某项目(招标文件).pdf \\
        --bidders 投标目录 --out 项目包.zip
    python scripts/build_import_zip.py --tender a.pdf --company 甲=目录A \\
        --company 乙=目录B --out 项目包.zip --wrap-root

- --tender：一个或多个招标侧 PDF/DOCX（可重复）；
- --bidders：投标目录（其下每个子文件夹 = 一个公司）；--company 名=目录 可重复混用；
- --wrap-root：故意多包一层包装根（测试导入端的包装根剥除容错）。

build_zip() 供 run_e2e / run_auto_pipeline / seed_demo 复用。
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def build_zip(
    tender_files: list[Path],
    bidder_files: dict[str, list[Path]],
    out: Path,
    wrap_root: bool = False,
) -> Path:
    """打包项目包 zip。

    tender_files: 招标侧文件（顺序即主文件优先序）；
    bidder_files: 公司名 → 该公司文件列表；
    wrap_root: 多包一层"项目包/"包装根（容错测试用）。
    """
    prefix = "项目包/" if wrap_root else ""
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in tender_files:
            zf.write(f, f"{prefix}招标/{f.name}")
        for name, files in sorted(bidder_files.items()):
            for p in files:
                zf.write(p, f"{prefix}投标/{name}/{p.name}")
    return out


def _companies_from_dir(bidders_dir: Path) -> dict[str, list[Path]]:
    companies: dict[str, list[Path]] = {}
    for d in sorted(b for b in bidders_dir.iterdir() if b.is_dir()):
        files = [
            p
            for p in sorted(d.rglob("*"))
            if p.is_file() and not p.name.startswith("~$") and p.name != ".DS_Store"
        ]
        companies[d.name] = files
    return companies


def main() -> int:
    ap = argparse.ArgumentParser(description="打包评标系统输入项目包（zip）")
    ap.add_argument("--tender", action="append", required=True,
                    help="招标侧 PDF/DOCX，可重复（首个含'招标'文件名者为主文件）")
    ap.add_argument("--bidders", default=None,
                    help="投标目录：其下每个子文件夹 = 一个公司")
    ap.add_argument("--company", action="append", default=[],
                    help="公司名=目录，可重复（与 --bidders 混用）")
    ap.add_argument("--out", required=True, help="输出 zip 路径")
    ap.add_argument("--wrap-root", action="store_true",
                    help="多包一层包装根（测试导入容错）")
    args = ap.parse_args()

    tender_files = [Path(t) for t in args.tender]
    for f in tender_files:
        if not f.is_file():
            raise SystemExit(f"招标文件不存在：{f}")

    bidder_files: dict[str, list[Path]] = {}
    if args.bidders:
        bidder_files.update(_companies_from_dir(Path(args.bidders)))
    for spec in args.company:
        name, _, folder = spec.partition("=")
        if not name or not folder:
            raise SystemExit(f"--company 需要 名=目录 形式：{spec}")
        files = [
            p
            for p in sorted(Path(folder).rglob("*"))
            if p.is_file() and not p.name.startswith("~$") and p.name != ".DS_Store"
        ]
        bidder_files[name.strip()] = files
    if not bidder_files:
        raise SystemExit("没有任何公司文件夹（--bidders 或 --company 至少给一个）")

    out = build_zip(tender_files, bidder_files, Path(args.out), wrap_root=args.wrap_root)
    n_files = sum(len(v) for v in bidder_files.values())
    print(f"已打包：{out}（招标 {len(tender_files)} 份，公司 {len(bidder_files)} 家 / {n_files} 个文件"
          f"{'，含包装根' if args.wrap_root else ''}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
