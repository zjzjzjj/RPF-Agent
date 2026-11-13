"""项目包（zip）解压与结构识别。

输入格式（用户约定）：

    项目包.zip
    ├── 招标/                  招标文件 PDF/DOCX（首个 = 主招标文件，其余=补遗）
    └── 投标/
        ├── 公司甲/            每个公司一个文件夹（文件夹名 = 投标人名称，严格一层）
        │   ├── 开标一览表.pdf / 开标一览表.xlsx
        │   ├── 分项报价表.xlsx
        │   └── …投标文件.pdf
        └── 公司乙/

容错：资源管理器"压缩父文件夹"时 zip 内多一层包装根目录，自动逐层剥掉
（最多 _MAX_WRAPPER_DEPTH 层单子目录链）。

安全边界：
- zip-slip：解压目标必须落在导入目录内，拒绝绝对路径/盘符/`..` 段；
- 文件名编码：Windows 打包的 zip 中文名多为 GBK 且不置 UTF-8 flag，
  zipfile 按 cp437 解出乱码，需转码修复（不修则「招标/投标」目录都识别不出）；
- 限额：条目数与解压总量（config.import_*）；
- 嵌套压缩包（.zip/.7z/.rar）不解压，原样落盘并警告；
- symlink/特殊文件跳过并警告。
"""
from __future__ import annotations

import logging
import stat
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app import config
from app.errors import (
    ImportBundleError,
    ZipDuplicateBidder,
    ZipEmptyTender,
    ZipMissingBidDir,
    ZipMissingTenderDir,
    ZipNoBidders,
    ZipTooLarge,
)

log = logging.getLogger("bid_eval.zip_intake")

TENDER_DIR = "招标"
BID_DIR = "投标"
TENDER_SUFFIXES = {".pdf", ".docx"}
_ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}
_IGNORE_NAMES = {".DS_Store"}
_MAX_WRAPPER_DEPTH = 5
_MAX_NAME_LEN = 255  # Bidder.name = String(255)

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class TenderFile:
    path: Path
    rel_name: str  # 招标/ 下的相对路径（原文件名留痕用）
    is_primary: bool  # 主招标文件（规则编译入口）


@dataclass(frozen=True)
class CompanyBundle:
    name: str  # 文件夹名去空白 = 投标人名称
    folder: Path
    files: tuple[Path, ...]


@dataclass
class BundlePlan:
    """仅凭 zip 条目名做出的结构判定（同步预检与解压共用）。"""

    wrapper_stripped: bool
    tender_root: str  # 形如 "招标" 或 "包装根/招标"（zip 内前缀，'/'分隔）
    bid_root: str
    tender_files: list[tuple[str, bool]]  # (招标/下相对路径, 是否主文件)
    companies: list[tuple[str, str, list[str]]]  # (投标人名称, 磁盘原始文件夹段名, [相对路径...])
    warnings: list[str] = field(default_factory=list)


@dataclass
class BundleLayout:
    """解压落地后的规范化布局。"""

    root: Path  # 有效根（已剥包装根）：其下为 招标/ 与 投标/
    wrapper_stripped: bool
    tender_files: list[TenderFile]
    companies: list[CompanyBundle]
    warnings: list[str] = field(default_factory=list)


def decode_entry_name(info: zipfile.ZipInfo) -> str:
    """修复 Windows 打包 zip 的中文文件名乱码。

    zipfile 对未置 UTF-8 flag（0x800）的条目按 cp437 解码；Windows 资源管理器
    写的 zip 实际是 GBK 字节。把解出的字符串按 cp437 还原为字节再按 GBK 解码
    即可还原；任一步失败（真 UTF-8 / 纯 ASCII 等）则原样返回。
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _norm(name: str) -> str:
    return name.replace("\\", "/")


def _is_safe(rel: PurePosixPath) -> bool:
    if rel.is_absolute():
        return False
    if any(part == ".." for part in rel.parts):
        return False
    if rel.parts and len(rel.parts[0]) == 2 and rel.parts[0][1] == ":":
        return False  # 盘符（C:）
    return True


def _entry_parts(name: str) -> PurePosixPath | None:
    """条目名 → 安全相对路径（去掉末尾'/'）；不安全返回 None。"""
    n = name.rstrip("/")
    if not n:
        return None
    p = PurePosixPath(n)
    return p if _is_safe(p) else None


def _has_dir(entries: list[tuple[PurePosixPath, bool]], prefix: str) -> bool:
    """prefix（'a/b' 形式，尾斜杠可有可无）是否为目录：存在以其开头的条目。"""
    prefix = prefix.rstrip("/")
    head = prefix + "/"
    for parts, is_dir in entries:
        s = parts.as_posix()
        if s == prefix and is_dir:
            return True
        if s.startswith(head):
            return True
    return False


def _child_dirs(entries: list[tuple[PurePosixPath, bool]], prefix: str) -> list[str]:
    """prefix 下一级子目录名（去重排序）。"""
    prefix = prefix.rstrip("/")
    head = prefix + "/" if prefix else ""
    kids: set[str] = set()
    for parts, is_dir in entries:
        s = parts.as_posix()
        if prefix and not s.startswith(head):
            continue
        rest = s[len(head):] if prefix else s
        if not rest:
            continue
        seg = rest.split("/", 1)[0]
        if "/" in rest or is_dir:
            kids.add(seg)
    return sorted(kids)


def _resolve_roots(entries: list[tuple[PurePosixPath, bool]]) -> tuple[str, bool]:
    """定位 招标/投标 两目录（容错剥包装根）。返回 (前缀, 是否剥了包装根)。"""
    prefix = ""
    for _ in range(_MAX_WRAPPER_DEPTH + 1):
        t_found = _has_dir(entries, f"{prefix}{TENDER_DIR}")
        b_found = _has_dir(entries, f"{prefix}{BID_DIR}")
        if t_found and b_found:
            return prefix, bool(prefix)
        if t_found != b_found:
            # 只有一半：缺哪个报哪个，比笼统报错可操作
            if t_found:
                raise ZipMissingBidDir(
                    f"项目包缺少「{BID_DIR}」文件夹（找到「{TENDER_DIR}」但无配对目录）"
                )
            raise ZipMissingTenderDir(
                f"项目包缺少「{TENDER_DIR}」文件夹（找到「{BID_DIR}」但无配对目录）"
            )
        kids = _child_dirs(entries, prefix)
        if len(kids) != 1:
            break
        prefix = f"{prefix}{kids[0]}/"
    raise ZipMissingTenderDir(
        f"项目包缺少「{TENDER_DIR}」与「{BID_DIR}」两个顶层文件夹"
    )


def plan_from_names(names: Iterable[str]) -> BundlePlan:
    """仅凭条目名判定结构；结构不合法抛 ImportBundleError 子类。"""
    entries: list[tuple[PurePosixPath, bool]] = []
    warnings: list[str] = []
    for name in names:
        parts = _entry_parts(name)
        if parts is None:
            warnings.append(f"已跳过不安全条目：{name}")
            continue
        entries.append((parts, name.endswith("/")))
    if not entries:
        raise ImportBundleError("项目包内没有任何文件")

    prefix, wrapper_stripped = _resolve_roots(entries)
    tender_root = f"{prefix}{TENDER_DIR}"
    bid_root = f"{prefix}{BID_DIR}"
    t_head, b_head = tender_root + "/", bid_root + "/"

    # ---- 招标侧 ----
    tender: list[tuple[str, bool]] = []
    for parts, is_dir in entries:
        s = parts.as_posix()
        if not s.startswith(t_head) or is_dir:
            continue
        rel = s[len(t_head):]
        if PurePosixPath(rel).suffix.lower() in TENDER_SUFFIXES:
            tender.append((rel, False))
        else:
            warnings.append(f"「{TENDER_DIR}」文件夹内已忽略非 PDF/Word 文件：{rel}")
    if not tender:
        raise ZipEmptyTender(f"「{TENDER_DIR}」文件夹内没有 PDF/Word 文件（.pdf/.docx）")
    # 主招标文件：文件名含"招标"者优先（真实数据命名 项目名(招标文件).pdf，
    # 评审材料/答疑不应被误当主文件），同组内按路径排序取首个。
    tender.sort(key=lambda t: (0 if "招标" in PurePosixPath(t[0]).name else 1, t[0]))
    tender[0] = (tender[0][0], True)

    # ---- 投标侧 ----
    order: list[str] = []  # 显示名顺序
    comp: dict[str, tuple[str, list[str]]] = {}  # 显示名 → (原始段名, 文件)
    for parts, is_dir in entries:
        s = parts.as_posix()
        if not s.startswith(b_head):
            continue
        rest = s[len(b_head):]
        if not rest:
            continue
        seg, _, tail = rest.partition("/")
        if not tail and not is_dir:
            warnings.append(f"「{BID_DIR}」文件夹根下的散文件已忽略：{rest}")
            continue
        name = seg.strip()
        if not name:
            warnings.append(f"已跳过无名文件夹条目：{rest}")
            continue
        if len(name) > _MAX_NAME_LEN:
            warnings.append(f"公司名过长已截断（原名 {len(name)} 字符）：{name[:_MAX_NAME_LEN]}…")
            name = name[:_MAX_NAME_LEN]
        if name in comp and comp[name][0] != seg:
            raise ZipDuplicateBidder(
                f"「{BID_DIR}」文件夹内存在同名公司文件夹：{seg.strip()}",
                detail={"name": name},
            )
        if name not in comp:
            comp[name] = (seg, [])
            order.append(name)
        if not tail:
            continue
        base = PurePosixPath(tail).name
        if base in _IGNORE_NAMES or base.startswith("~$"):
            continue
        if PurePosixPath(tail).suffix.lower() in _ARCHIVE_SUFFIXES:
            warnings.append(f"嵌套压缩包未解压（原样保留）：{seg}/{tail}")
        comp[name][1].append(tail)

    if not order:
        raise ZipNoBidders(f"「{BID_DIR}」文件夹内没有公司文件夹（每个公司一个文件夹）")

    companies: list[tuple[str, str, list[str]]] = []
    for name in order:
        seg, files = comp[name]
        files.sort()
        if not files:
            warnings.append(f"公司文件夹为空：{name}")
        companies.append((name, seg, files))

    return BundlePlan(
        wrapper_stripped=wrapper_stripped,
        tender_root=tender_root,
        bid_root=bid_root,
        tender_files=tender,
        companies=companies,
        warnings=warnings,
    )


def peek_zip(zip_path: Path) -> BundlePlan:
    """同步预检：只读中央目录判结构，不做解压。失败抛 ImportBundleError 子类。"""
    max_zip = config.import_max_zip_mb() * 1024 * 1024
    if zip_path.stat().st_size > max_zip:
        raise ZipTooLarge(
            f"项目包超出大小限制（{config.import_max_zip_mb()} MB）",
            detail={"limit_mb": config.import_max_zip_mb()},
        )
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as e:
        raise ImportBundleError(f"项目包不是有效的 zip 文件：{e}") from e
    with zf:
        infos = zf.infolist()
        _check_limits(infos)
        return plan_from_names([_decoded(i) for i in infos])


def _decoded(info: zipfile.ZipInfo) -> str:
    return _norm(decode_entry_name(info))


def _check_limits(infos: list[zipfile.ZipInfo]) -> None:
    max_entries = config.import_max_entries()
    if len(infos) > max_entries:
        raise ZipTooLarge(
            f"项目包条目数超限（{len(infos)} > {max_entries}）",
            detail={"entries": len(infos), "limit": max_entries},
        )
    total = sum(i.file_size for i in infos)
    limit = config.import_max_uncompressed_mb() * 1024 * 1024
    if total > limit:
        raise ZipTooLarge(
            f"项目包解压后体积超限（{config.import_max_uncompressed_mb()} MB）",
            detail={
                "uncompressed_bytes": total,
                "limit_mb": config.import_max_uncompressed_mb(),
            },
        )


def extract_bundle(zip_path: Path, dest_root: Path) -> BundleLayout:
    """安全解压 zip 到 dest_root 并返回规范化布局（含包装根剥除结果）。"""
    dest_root.mkdir(parents=True, exist_ok=True)
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as e:
        raise ImportBundleError(f"项目包不是有效的 zip 文件：{e}") from e
    with zf:
        infos = zf.infolist()
        _check_limits(infos)
        plan = plan_from_names([_decoded(i) for i in infos])
        warnings = list(plan.warnings)
        limit = config.import_max_uncompressed_mb() * 1024 * 1024
        written = 0
        for info in infos:
            name = _decoded(info)
            parts = _entry_parts(name)
            if parts is None:
                continue  # 不安全条目，预检已警告
            mode = info.external_attr >> 16
            fmt = stat.S_IFMT(mode)
            if fmt == stat.S_IFLNK or fmt not in (0, stat.S_IFREG, stat.S_IFDIR):
                warnings.append(f"已跳过特殊文件：{name}")
                continue
            target = dest_root.joinpath(*parts.parts)
            if info.is_dir() or fmt == stat.S_IFDIR:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise ZipTooLarge(
                            f"项目包解压后体积超限（{config.import_max_uncompressed_mb()} MB）",
                            detail={
                                "limit_mb": config.import_max_uncompressed_mb(),
                            },
                        )
                    dst.write(chunk)

    # 路径拼接统一走 zip 内前缀（tender_root/bid_root 已含包装根段）
    root = dest_root.joinpath(*PurePosixPath(plan.tender_root).parts[:-1])
    tender_files = [
        TenderFile(
            path=dest_root.joinpath(
                *PurePosixPath(plan.tender_root).parts, *PurePosixPath(rel).parts
            ),
            rel_name=rel,
            is_primary=is_primary,
        )
        for rel, is_primary in plan.tender_files
    ]
    companies = [
        CompanyBundle(
            name=name,
            folder=dest_root.joinpath(*PurePosixPath(plan.bid_root).parts, seg),
            files=tuple(
                dest_root.joinpath(
                    *PurePosixPath(plan.bid_root).parts, seg, *PurePosixPath(f).parts
                )
                for f in files
            ),
        )
        for name, seg, files in plan.companies
    ]
    return BundleLayout(
        root=root,
        wrapper_stripped=plan.wrapper_stripped,
        tender_files=tender_files,
        companies=companies,
        warnings=warnings,
    )
