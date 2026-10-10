"""条款树构建：把逐页文本变成带层级、带页码、带字符区间的节点树。

要点：
- 父子靠**"后继关系"**判定（标记栈），不依赖缩进——中文标书缩进不可靠。
- 目录页整页剔除（is_toc），避免目录条目冒充正文条款。
- 表格区域内的文本不参与编号扫描（评分表里的"1."是分值不是编号）。
- node_id 为路径编码（含页码，便于人工排查），同一文档内全局唯一且稳定。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.parsing.base import ExtractedNode, ExtractedPage
from app.parsing.numbering import NumberingContext, detect_marker, looks_like_toc

_LABEL_PREFIX = {
    "chapter": "C",
    "section": "SEC",
    "article": "A",
    "level_a": "I",
    "level_b": "II",
    "num_n": "N",
    "num_1": "N",
    "digit_bracket": "D",
    "circled": "O",
}

_KIND_MAP = {
    "chapter": "chapter",
    "section": "section",
    "article": "article",
    "level_a": "item",
    "level_b": "item",
    "num_n": "item",
    "num_1": "item",
    "digit_bracket": "item",
    "circled": "item",
}


@dataclass
class _Line:
    page_no: int
    text: str
    start: int  # 全文坐标
    end: int
    bbox: tuple[float, float, float, float] | None = None


def _flatten(pages: list[ExtractedPage]) -> tuple[list[_Line], str]:
    lines: list[_Line] = []
    parts: list[str] = []
    offset = 0
    for p in pages:
        raw_lines = p.text.split("\n")
        for i, raw in enumerate(raw_lines):
            t = raw.strip()
            bbox = None
            if p.line_boxes and i < len(p.line_boxes):
                bbox = p.line_boxes[i]
            lines.append(_Line(p.page_no, t, offset, offset + len(t), bbox))
            parts.append(t)
            offset += len(t) + 1  # join("\n") 的 1 个字符
    return lines, "\n".join(parts)


def _in_table(ln: _Line, table_boxes: dict[int, list[tuple[float, float, float, float]]]) -> bool:
    boxes = table_boxes.get(ln.page_no)
    if not boxes or ln.bbox is None:
        return False
    cx = (ln.bbox[0] + ln.bbox[2]) / 2
    cy = (ln.bbox[1] + ln.bbox[3]) / 2
    for x0, y0, x1, y1 in boxes:
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return True
    return False


def build_chapter_tree(
    doc_prefix: str,
    pages: list[ExtractedPage],
    table_boxes: dict[int, list[tuple[float, float, float, float]]] | None = None,
) -> tuple[list[ExtractedNode], list[str], dict[int, tuple[int, int]]]:
    """返回 (节点列表, 警告, {page_no: (char_start, char_end)})。"""
    warnings: list[str] = []
    table_boxes = table_boxes or {}
    lines, full_text = _flatten(pages)

    # 1) 目录页剔除
    by_page: dict[int, list[_Line]] = {}
    for ln in lines:
        by_page.setdefault(ln.page_no, []).append(ln)
    toc_pages: set[int] = set()
    for page_no, page_lines in by_page.items():
        if looks_like_toc([ln.text for ln in page_lines]):
            toc_pages.add(page_no)
    if toc_pages:
        warnings.append(
            f"疑似目录页已跳过编号扫描：第 {', '.join(str(p) for p in sorted(toc_pages))} 页"
        )

    # 2) 标记扫描
    ctx = NumberingContext()
    markers: list[tuple[_Line, object]] = []  # (line, Marker)
    for ln in lines:
        if ln.page_no in toc_pages or not ln.text:
            continue
        if _in_table(ln, table_boxes):
            continue
        m = detect_marker(ln.text, ctx)
        if m is not None:
            if m.restart:
                ctx.reset()
            ctx.note_seen(m.value)
            markers.append((ln, m))

    # 3) 建树（标记栈 + 后继关系）
    root = ExtractedNode(
        node_id=f"{doc_prefix}:root",
        parent_node_id=None,
        kind="document",
        label="root",
        title="文档根",
        page=1,
        char_start=0,
        char_end=len(full_text),
        text="",
        depth=0,
    )
    nodes: list[ExtractedNode] = [root]
    stack: list[ExtractedNode] = [root]
    used_ids: set[str] = {root.node_id}

    for idx, (ln, m) in enumerate(markers):
        while len(stack) > 1 and stack[-1].depth >= m.depth:
            stack.pop()
        parent = stack[-1]

        seg_end = markers[idx + 1][0].start if idx + 1 < len(markers) else len(full_text)
        text = full_text[ln.start:seg_end].strip()

        label = f"{_LABEL_PREFIX.get(m.kind, 'X')}{m.value}"
        node_id = f"{parent.node_id}:{label}"
        n = 2
        base_id = node_id
        while node_id in used_ids:  # 附件重启编号等场景：保证唯一
            node_id = f"{base_id}~{n}"
            n += 1
        used_ids.add(node_id)

        node = ExtractedNode(
            node_id=node_id,
            parent_node_id=parent.node_id,
            kind=_KIND_MAP.get(m.kind, "item"),
            number=m.raw,
            label=label,
            title=(m.rest[:120] or None),
            page=ln.page_no,
            char_start=ln.start,
            char_end=seg_end,
            text=text,
            depth=m.depth,
        )
        nodes.append(node)
        stack.append(node)

    # 4) 页字符区间（document_pages 落库用）
    page_ranges: dict[int, tuple[int, int]] = {}
    for page_no, page_lines in by_page.items():
        if page_lines:
            page_ranges[page_no] = (page_lines[0].start, page_lines[-1].end)

    return nodes, warnings, page_ranges


def subtree_text(nodes: list[ExtractedNode], node_id: str) -> str:
    """节点自身 + 全部后代的文本（证据核验在子树内查找时用）。"""
    by_parent: dict[str | None, list[ExtractedNode]] = {}
    for n in nodes:
        by_parent.setdefault(n.parent_node_id, []).append(n)
    out: list[str] = []

    def _walk(nid: str) -> None:
        for child in by_parent.get(nid, []):
            if child.text:
                out.append(child.text)
            _walk(child.node_id)

    target = next((n for n in nodes if n.node_id == node_id), None)
    if target is not None and target.text:
        out.append(target.text)
    _walk(node_id)
    return "\n".join(out)
