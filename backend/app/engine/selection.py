"""超长文档的章节感知选段：compiler（招标侧）与 subjective/auto_judge（投标侧）共用。

从 compiler.select_compile_text 泛化而来（页 → 任意有序 chunk）：
- 贪心分档：本 chunk 命中数 + 0.5×相邻 chunk 最大命中数 排序，预算内逐档装桶；
- 未满预算按原序回填普通 chunk；
- **输出保持传入顺序**——招标侧保证 quote 能在页序拼接上逐字回查；投标侧保持
  文档原次序让模型按篇章阅读；
- meta 结构 {truncated, chars, total_chars, ids, tiers} 与既有契约逐字段兼容
  （test_compile_selection 7 条为此重构的门禁）。

"投标书超长"的系统性错评（200 页标书头部截断只看前 4%，尾部"售后方案"必然
被评最低档）由此修复：(subjective/auto_judge) 超预算时按评分项关键词选段，
替代盲目的头部截断。
"""
from __future__ import annotations

import re

Chunk = tuple[int, str]  # (chunk 序号, 文本)

# 通用后缀：名称关键词的主干提炼（"售后服务方案" → "售后服务"）
_GENERIC_SUFFIXES = ("方案", "情况", "能力", "程度", "水平", "承诺", "响应", "措施")
# 衍生关键词的分隔符（required_evidence / quote 常是顿号列举）
_KEYWORD_DELIM = re.compile(r"[，,、;；\s/]+")


def split_text_chunks(text: str, chunk_size: int = 2000) -> list[Chunk]:
    """平面文本 → ≈chunk_size 的伪页序列：按换行边界切，段内不硬截；
    超长单段（无换行的表格流）按硬上限再切。序号 = 全局递增。"""
    chunks: list[Chunk] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append((len(chunks), "\n".join(buf)))
            buf, size = [], 0

    for line in text.split("\n"):
        buf.append(line)
        size += len(line) + 1
        if size >= chunk_size:
            flush()
    flush()
    # 无换行的超长流兜底：硬切（split 之后 buf 里可能还有超长单行）
    out: list[Chunk] = []
    for no, body in chunks:
        if len(body) <= chunk_size * 2:
            out.append((no, body))
            continue
        for i in range(0, len(body), chunk_size):
            out.append((len(out), body[i:i + chunk_size]))
    return out


def derive_keywords(criterion: dict) -> tuple[str, ...]:
    """评分项 → 选段关键词（去重、≥2 字符、子串 `in` 匹配口径）。

    ① name 全串；② name 去通用后缀的主干；③ required_evidence / quote 的
    ≥4 字符片段（截前 8，防爆量）。全空 → ()，调用方退化均分策略。
    """
    words: list[str] = []
    name = str(criterion.get("name") or "").strip()
    if name:
        words.append(name)
        stem = name
        for suffix in _GENERIC_SUFFIXES:
            if stem.endswith(suffix) and len(stem) > len(suffix) + 1:
                stem = stem[: -len(suffix)]
                break
        if stem != name and len(stem) >= 2:
            words.append(stem)
    source = str(criterion.get("required_evidence") or "") or str(
        criterion.get("quote") or criterion.get("source_quote") or "")
    for piece in _KEYWORD_DELIM.split(source):
        piece = piece.strip()
        if len(piece) >= 4:
            words.append(piece)
        if len(words) >= 10:
            break
    seen: set[str] = set()
    return tuple(w for w in words if not (w in seen or seen.add(w)))


_FALLBACK_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = ()


def select_chunks_by_tier(
    chunks: list[Chunk],
    tiers: tuple[tuple[str, tuple[str, ...]], ...],
    budget: int,
    fallback_diffuse: bool = False,
) -> tuple[list[Chunk], dict]:
    """贪心分档选取。fallback_diffuse=True 且关键词全未命中时，退化
    "头 30% + 尾 30% + 中段均分"的散布选取（无词可用时不至于只看头部）。"""
    full_chars = sum(len(t) for _i, t in chunks)
    if full_chars <= budget:
        return list(chunks), {
            "truncated": False, "chars": full_chars, "total_chars": full_chars,
            "ids": [i for i, _t in chunks],
            "tiers": {name: 0 for name, _kw in tiers},
        }

    # 打分：本 chunk 命中 + 0.5×相邻最大
    scores = [0.0] * len(chunks)
    for tier_no, (_name, keywords) in enumerate(tiers):
        hits = [0] * len(chunks)
        for idx, (_no, body) in enumerate(chunks):
            hits[idx] = sum(1 for kw in keywords if kw and kw in body)
        for idx, hit in enumerate(hits):
            if not hit:
                continue
            neighbors = [
                hits[j] for j in (idx - 1, idx + 1) if 0 <= j < len(hits)
            ]
            bonus = 0.5 * max(neighbors) if neighbors else 0.0
            # 同 chunk 命中多档：以最高档计入（tier_no 越小优先级越高）
            tier_weight = 1.0 / (tier_no + 1)
            scores[idx] = max(scores[idx], (hit + bonus) * tier_weight)

    scored_order = sorted(
        range(len(chunks)), key=lambda i: (-scores[i], i)
    )
    picked_ids: set[int] = set()
    used = 0
    tiers_used: dict[str, int] = {name: 0 for name, _kw in tiers}
    for tier_no, (name, _kw) in enumerate(tiers):
        for idx in scored_order:
            if scores[idx] <= 0:
                break
            if idx in picked_ids:
                continue
            length = len(chunks[idx][1])
            if used + length > budget:
                continue
            picked_ids.add(idx)
            used += length
            tiers_used[name] = tiers_used.get(name, 0) + 1
            # 相邻块与命中块成组入选（命中块孤悬会让文本上下文断裂）——
            # 邻居自身 scores 可能是 0（bonus 加在命中块上），无需正分检查
            for j in (idx - 1, idx + 1):
                if 0 <= j < len(chunks) and j not in picked_ids:
                    jlen = len(chunks[j][1])
                    if used + jlen <= budget:
                        picked_ids.add(j)
                        used += jlen
    # 未满预算：按原序回填——正分块（tier 没装下的）优先于零分普通块，
    # 否则预算会被原文开头的低价值块先耗尽（实测邻居 7 被 0/1 挤掉）
    any_hit = any(s > 0 for s in scores)
    if any_hit:
        ordered = sorted(range(len(chunks)), key=lambda i: (0 if scores[i] > 0 else 1, i))
        for idx in ordered:
            if used >= budget:
                break
            if idx in picked_ids:
                continue
            length = len(chunks[idx][1])
            if used + length > budget:
                continue
            picked_ids.add(idx)
            used += length

    if not picked_ids and fallback_diffuse:
        return _diffuse_pick(chunks, budget)

    final_ids = sorted(picked_ids)
    total = sum(len(chunks[i][1]) for i in final_ids)
    return (
        [chunks[i] for i in final_ids],
        {
            "truncated": total < full_chars,
            "chars": total,
            "total_chars": full_chars,
            "ids": [chunks[i][0] for i in final_ids],
            "tiers": tiers_used,
        },
    )


def _diffuse_pick(chunks: list[Chunk], budget: int) -> tuple[list[Chunk], dict]:
    """无词可用时的散布选取：预算内均匀采样（含头尾），保持覆盖全文跨度。"""
    total_all = sum(len(t) for _i, t in chunks)
    # 均匀步长采样到预算内（头尾块优先保底）
    keep = max(1, budget * len(chunks) // max(1, total_all))
    if keep >= len(chunks):
        picked = list(range(len(chunks)))
    else:
        step = len(chunks) / keep
        picked = sorted({min(len(chunks) - 1, int(i * step)) for i in range(keep)})
        picked[0] = 0  # 头
        picked[-1] = len(chunks) - 1  # 尾，必含
    total = sum(len(chunks[i][1]) for i in picked)
    return (
        [chunks[i] for i in picked],
        {
            "truncated": total < total_all,
            "chars": total,
            "total_chars": total_all,
            "ids": [chunks[i][0] for i in picked],
            "tiers": {},
        },
    )
