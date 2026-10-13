"""法条知识库：法律规则包 source_node_id（`法条://法规名/条号`）的核验源。

数据源是本地法规库编译出的 `生成_法条知识库.json`（150 部法规、按"第X条"切分），
结构为 `[{lawName, sourceFile, sourceUrl, articleCount, articles: [{articleNo, text}]}]`。

职责边界（设计稿 §4.4）：
- `node_exists` 只回答"该法条是否在知识库中真实存在"——**真实核验，不是放行**；
- 知识库文件读不到 → 一律 False（fail-closed）：confirm/freeze 会因此报
  CITATION_UNREACHABLE 422，绝不静默通过；
- 缓存按 (path, mtime) 失效：法规库重新编译后无需重启服务。
"""
from __future__ import annotations

import json
import logging
import unicodedata
from pathlib import Path

from app import config

log = logging.getLogger("bid_eval.law_kb")

LAW_PREFIX = "法条://"

_cache: dict = {"key": None, "index": None}  # key=(str(path), mtime_ns)


def _norm(s: str) -> str:
    """NFKC 归一（全角/半角括号、空格）+ 去首尾空白。"""
    return unicodedata.normalize("NFKC", s or "").strip()


def load_index() -> dict[str, set[str]]:
    """法名 → 条号集合。文件缺失/损坏返回空索引（配合 node_exists 即 fail-closed）。"""
    path = config.law_kb_path()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns)
    except OSError:
        log.warning("法条知识库不可读：%s（法条核验将全部不通过）", path)
        return {}
    if _cache["key"] == key and _cache["index"] is not None:
        return _cache["index"]

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("法条知识库解析失败：%s（%s）", path, exc)
        return {}

    index: dict[str, set[str]] = {}
    for law in data if isinstance(data, list) else []:
        name = _norm(law.get("lawName"))
        if not name:
            continue
        arts = {_norm(a.get("articleNo")) for a in law.get("articles") or [] if a.get("articleNo")}
        index.setdefault(name, set()).update(arts)
    _cache["key"] = key
    _cache["index"] = index
    log.info("法条知识库已加载：%d 部法规 <- %s", len(index), path)
    return index


def node_exists(node_id: str) -> bool:
    """`法条://法规名/条号` 是否在知识库中命中。非该前缀一律 False。"""
    if not node_id or not node_id.startswith(LAW_PREFIX):
        return False
    rest = node_id[len(LAW_PREFIX) :]
    if "/" not in rest:
        return False
    law_name, _, article_no = rest.rpartition("/")
    if not law_name or not article_no:
        return False
    index = load_index()
    arts = index.get(_norm(law_name))
    return bool(arts and _norm(article_no) in arts)


def reset_cache() -> None:
    """测试用：强制下次重新读盘。"""
    _cache["key"] = None
    _cache["index"] = None
