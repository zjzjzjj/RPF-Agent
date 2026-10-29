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

向量选段（BID_EVAL_SELECTION=vector，2026-10-14）：bge-m3（mwb 自部署）对
评分项描述与标书分块做语义检索，同义表述的证据不再依赖字面关键词命中；
嵌入服务不可用时自动回退关键词分档。
"""
from __future__ import annotations

import logging
import re

from app import config

log = logging.getLogger("bid_eval.engine.selection")

Chunk = tuple[int, str]  # (chunk 序号, 文本)
PageChunkSrc = tuple[int, int]  # chunk 出处：(document_id, page_no)

# 通用后缀：名称关键词的主干提炼（"售后服务方案" → "售后服务"）
_GENERIC_SUFFIXES = ("方案", "情况", "能力", "程度", "水平", "承诺", "响应", "措施")
# 衍生关键词的分隔符（required_evidence / quote 常是顿号列举）
_KEYWORD_DELIM = re.compile(r"[，,、;；\s/]+")


def _split_spans(text: str, chunk_size: int = 2000) -> list[tuple[int, int, str]]:
    """平面文本 → (start, end, body) 切块（切分口径即原 split_text_chunks）。"""
    spans: list[tuple[int, int, str]] = []
    buf: list[str] = []
    size = 0
    start = 0
    pos = 0

    def flush() -> None:
        nonlocal buf, size, start
        if buf:
            body = "\n".join(buf)
            spans.append((start, start + len(body), body))
            buf, size = [], 0
            start = pos

    for line in text.split("\n"):
        if not buf:
            start = pos
        buf.append(line)
        size += len(line) + 1
        pos += len(line) + 1
        if size >= chunk_size:
            flush()
    flush()
    # 无换行的超长流兜底：硬切
    out: list[tuple[int, int, str]] = []
    for s, _e, body in spans:
        if len(body) <= chunk_size * 2:
            out.append((s, s + len(body), body))
            continue
        for i in range(0, len(body), chunk_size):
            out.append((s + i, s + min(i + chunk_size, len(body)), body[i:i + chunk_size]))
    return out


def split_text_chunks(text: str, chunk_size: int = 2000) -> list[Chunk]:
    """平面文本 → ≈chunk_size 的伪页序列：按换行边界切，段内不硬截；
    超长单段（无换行的表格流）按硬上限再切。序号 = 全局递增。"""
    return [(i, body) for i, (_s, _e, body) in enumerate(_split_spans(text, chunk_size))]


def split_page_chunks(
    pages: list[tuple[int, int, str]], chunk_size: int = 2000
) -> tuple[list[Chunk], list[PageChunkSrc]]:
    """(document_id, page_no, text) 有序页序列 → 页出处标注的 chunk 序列。

    切分口径与 split_text_chunks(joined) **逐字节一致**（同算法同输入），
    仅多返回每块的出处 (document_id, page_no)；跨页块归入起始页。
    """
    joined = "\n".join(t for _d, _p, t in pages)
    # 页起点在 joined 中的偏移（每页文本 + 连接换行）
    starts: list[int] = []
    acc = 0
    for _d, _p, t in pages:
        starts.append(acc)
        acc += len(t) + 1
    spans = _split_spans(joined, chunk_size)
    chunks: list[Chunk] = []
    srcs: list[PageChunkSrc] = []
    for i, (s, _e, body) in enumerate(spans):
        chunks.append((i, body))
        # 起点落在的页（最后一个 start ≤ s 的页）
        page_idx = 0
        for j, st in enumerate(starts):
            if st <= s:
                page_idx = j
            else:
                break
        doc_id, page_no, _t = pages[page_idx]
        srcs.append((doc_id, page_no))
    return chunks, srcs


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


# 目录/索引页识别（结构性，零项目拟合）：目录点线、页码引用行、"见投标文件X页"
# 交叉引用——这类 chunk 逐字含评分项名（关键词/向量都会命中它）却**没有实质
# 内容**，毒化选段与证据簇（2026-10-27 run 58 实证：模型只见目录标题 → 按
# "只罗列标题 0-30%" 打出系统性低分，实际方案在132页却未入选）。
_RE_TOC_LINE = re.compile(
    r"(?:[.。…·]{3,}\s*\d{1,3}\s*$)"        # 点线目录：标题………42
    r"|(?:[（(]?\s*(?:第\s*)?\d{1,3}\s*页\s*[)）]?\s*$)"  # 行尾页码
    r"|(?:见\s*投标\s*文件)"                  # 交叉引用：见投标文件（63~131）页
    r"|(?:详见\s*第?\s*\d)"                   # 详见第86页
)


def index_text_weight(body: str) -> float:
    """chunk 的目录/索引浓度 → 权重（1.0 正常，最低 0.25）。

    按行比例降权：索引/目录行占比越高越不可信；结构性判据（点线/页码引用），
    与具体项目无关。
    """
    lines = [ln for ln in body.split("\n") if ln.strip()]
    if not lines:
        return 1.0
    hits = sum(1 for ln in lines if _RE_TOC_LINE.search(ln.strip()))
    frac = hits / len(lines)
    if frac <= 0.2:
        return 1.0
    # 例：40% 目录行 → 0.55；60%+ → 0.25 封底
    return max(0.25, round(1.0 - (frac - 0.2) * 1.25, 2))


def _norm_restatement(s: str) -> str:
    """复述判定的归一化：去空白与中英标点（与 subjective 原判据逐字一致）。"""
    return re.sub(
        r"[\s　，。；：、（）()【】\[\]《》〈〉.,;:!?？！“”‘’\"']", "", s
    )


def quote_is_restatement(quote: str, criterion: dict | None) -> bool:
    """引文是否是细则原文的复述（点对点应答表逐字照抄评分标准）。

    复述文本在标书里逐字可定位，但不构成"未提供"的证明——拿要求文本当
    证据是 2026-10-15 _sanitize_case 防过的同一反模式的镜像（2026-10-29
    审查 P2：real_zero 曾被应答表复述骗入均值）。结构性判据：归一化后
    引文与因素名/描述/逐字引文互为包含，或字符二元组重合度 ≥ 0.6。
    subjective._quote_is_restatement 委托此函数（判据单一来源）。
    """
    if not criterion:
        return False
    q = _norm_restatement(quote)
    if len(q) < 8:
        return False  # 太短无法判定，交给引文闸门
    for key in ("name", "description", "source_quote"):
        blob = _norm_restatement(str(criterion.get(key) or ""))
        if not blob:
            continue
        # 只认"引文⊆细则文本"（纯复述）；不反向包含——"本节未涉及订单管理功能"
        # 这类真实缺证陈述会包含短因素名，反向判会误伤真实 0 票
        if q in blob:
            return True
        gb = set(zip(blob, blob[1:]))
        gq = set(zip(q, q[1:]))
        if gb and len(gq & gb) / len(gq) >= 0.6:
            return True
    return False


def criterion_restatement_weight(body: str, criterion: dict | None) -> float:
    """chunk 正文是细则复述时的降权乘数（0.25），否则 1.0。

    点对点应答表逐字照抄评分标准的块在所有选段路径都会冒充证据
    （2026-10-29 run 63 实证：p38 复述块余弦 0.669、index_text_weight=1.00
    未被目录降权拦下）。判据与 quote_is_restatement 同口径（前向包含 +
    二元组重合度 ≥0.6）。**不做"细则⊆块正文"的反向包含**——"前半引用
    细则+后半真实方案"的混合块会被整块降权、把唯一实质证据踢出证据簇
    （2026-10-29 审查 P2）；二元组占比天然处理该场景：纯复述块占比高被
    降权，混合块随真实内容占比上升自然豁免。
    """
    if not criterion or not body:
        return 1.0
    return 0.25 if quote_is_restatement(body, criterion) else 1.0


def select_chunks_by_tier(
    chunks: list[Chunk],
    tiers: tuple[tuple[str, tuple[str, ...]], ...],
    budget: int,
    fallback_diffuse: bool = False,
    *,
    score_scale: list[float] | None = None,
    pool: list[int] | None = None,
    restatement_w: list[float] | None = None,
) -> tuple[list[Chunk], dict]:
    """贪心分档选取。fallback_diffuse=True 且关键词全未命中时，退化
    "头 30% + 尾 30% + 中段均分"的散布选取（无词可用时不至于只看头部）。

    score_scale：每块分数的乘数（类别软过滤的类内加成用），默认全 1。
    pool：只在这些下标构成的子集里选（类内硬过滤用）；None=全部。
    restatement_w：细则复述降权乘数（criterion_restatement_weight），默认
    全 1——复述应答表逐字含关键词，命中计数必然满分，不降权必然霸占预算
    （2026-10-29 审查 P2：keyword 是缺省模式且是嵌入熔断的实际回退路径）。
    """
    if pool is not None:
        return select_chunks_by_tier(
            [chunks[i] for i in pool],
            tiers,
            budget,
            fallback_diffuse,
            score_scale=([score_scale[i] for i in pool] if score_scale else None),
            restatement_w=([restatement_w[i] for i in pool] if restatement_w else None),
        )
    full_chars = sum(len(t) for _i, t in chunks)
    if full_chars <= budget:
        return list(chunks), {
            "truncated": False, "chars": full_chars, "total_chars": full_chars,
            "ids": [i for i, _t in chunks],
            "tiers": {name: 0 for name, _kw in tiers},
        }

    # 打分：本 chunk 命中 + 0.5×相邻最大；目录/索引 chunk 降权（逐字含评分项名
    # 却无实质内容——毒化第一名，正文永远选不进）；细则复述块同理降权
    scores = [0.0] * len(chunks)
    idx_weights = [
        index_text_weight(body) * (restatement_w[i] if restatement_w else 1.0)
        for i, (_i_no, body) in enumerate(chunks)
    ]
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
            candidate = (hit + bonus) * tier_weight * idx_weights[idx]
            # 强索引 chunk（点线/页码引用行过半）封顶 0.5：多关键词连击也不得
            # 压过单命中的正常正文（索引表一行可同时含多个评分项名）
            if idx_weights[idx] <= 0.4:
                candidate = min(candidate, 0.5)
            if score_scale is not None:
                candidate *= score_scale[idx]
            scores[idx] = max(scores[idx], candidate)

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


# ---------------------------------------------------------------------------
# 向量选段（bge-m3）与统一入口
# ---------------------------------------------------------------------------
def select_chunks_by_vector(
    chunks: list[Chunk],
    query_text: str,
    budget: int,
    *,
    score_scale: list[float] | None = None,
    pool: list[int] | None = None,
    criterion: dict | None = None,
) -> tuple[list[Chunk], dict] | None:
    """bge-m3 语义检索选段。嵌入服务不可用 → None（调用方回退关键词分档）。

    打分 = 查询向量与 chunk 向量的余弦（bge-m3 输出已 L2 归一化，点积即余弦）；
    高分块连同相邻块成组入选（保持上下文完整），输出按原序。
    score_scale/pool 语义同 select_chunks_by_tier（类别软/硬过滤用）。
    criterion 非空时对细则复述块降权（criterion_restatement_weight，与目录
    降权同位相乘；2026-10-29 run 63：复述应答表块冒充证据）。
    """
    from app.evidence import embed_client

    if pool is not None:
        return select_chunks_by_vector(
            [chunks[i] for i in pool],
            query_text,
            budget,
            score_scale=([score_scale[i] for i in pool] if score_scale else None),
            criterion=criterion,
        )
    if not chunks or not query_text.strip():
        return None
    try:
        qvec = embed_client.embed_one(query_text)
        if qvec is None:
            return None
        cvecs = embed_client.embed_batch([body for _i, body in chunks])
        if any(v is None for v in cvecs):
            return None
    except RuntimeError as exc:
        log.warning("向量选段不可用，回退关键词：%s", exc)
        return None

    scores = [embed_client.cosine(qvec, v) for v in cvecs if v is not None]
    # 目录/索引 chunk 降权（与关键词路径 :206-224 / build_evidence_map :567-570
    # 同一判据；2026-10-29 run 60 补：服务器 BID_EVAL_SELECTION=vector 时此前
    # 无此降权——目录页逐字含评分项名，余弦必然压过正文，模型只见标题）。
    # 权重乘完后，强索引块（weight<=0.4）再对最终排序分 min(score, 0.3) 封顶：
    # 关键词路径的 min(candidate, 0.5) 是命中计数域的封顶；余弦值域下多关键词
    # 连击不复存在，但 weight=0.4 边界处高余弦（0.9×0.4=0.36）仍可能压过弱相关
    # 正文，0.3 绝对封顶保证强索引块永远排不进正文梯队（与 cap 语义一致）。
    # 注意：放在 pool 递归之后的真实执行路径上（递归会重建 chunks 列表）。
    # 复述块降权与目录降权同位相乘（2026-10-29 run 63：逐字复述评分标准的
    # 应答表块余弦天然高，只降目录拦不住它冒充证据）。
    for i, (_no, body) in enumerate(chunks):
        w = index_text_weight(body) * criterion_restatement_weight(body, criterion)
        if w >= 1.0:
            continue
        scores[i] = scores[i] * w
        if w <= 0.4:
            scores[i] = min(scores[i], 0.3)
    if score_scale is not None:
        scores = [sc * score_scale[i] for i, sc in enumerate(scores)]
    scored_order = sorted(range(len(chunks)), key=lambda i: -scores[i])
    picked_ids: set[int] = set()
    used = 0
    vector_hits = 0
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
        vector_hits += 1
        for j in (idx - 1, idx + 1):
            if 0 <= j < len(chunks) and j not in picked_ids:
                jlen = len(chunks[j][1])
                if used + jlen <= budget:
                    picked_ids.add(j)
                    used += jlen

    if not picked_ids:
        return None
    final_ids = sorted(picked_ids)
    total = sum(len(chunks[i][1]) for i in final_ids)
    return (
        [chunks[i] for i in final_ids],
        {
            "truncated": total < sum(len(t) for _i, t in chunks),
            "chars": total,
            "total_chars": sum(len(t) for _i, t in chunks),
            "ids": [chunks[i][0] for i in final_ids],
            "tiers": {"vector": vector_hits},
        },
    )


def _criterion_query_text(criterion: dict) -> str:
    parts = [
        str(criterion.get("name") or ""),
        str(criterion.get("description") or ""),
        str(criterion.get("required_evidence") or ""),
        str(criterion.get("quote") or criterion.get("source_quote") or ""),
    ]
    return "\n".join(p for p in parts if p.strip())[:2000]


# 类内加成（软过滤的"优先"落点）与低置信继承页的减半加成
_IN_CLASS_BOOST = 1.3
_INHERIT_BOOST = 1.1
# 类内产出低于预算该比例 → 判"类内不足"，回退全库混选
_SOFT_FILL_RATIO = 0.5
# vector 混选触发：类内最高分低于全体最高分该比例
_VECTOR_MIX_RATIO = 0.6


def _select_once(
    chunks: list[Chunk],
    criterion: dict,
    budget: int,
    fallback_diffuse: bool,
    *,
    score_scale: list[float] | None,
    pool: list[int] | None,
) -> tuple[list[Chunk], dict]:
    """vector 优先、失败回退 keyword（单次选段，无类别逻辑）。"""
    if config.selection_mode() == "vector":
        try:
            out = select_chunks_by_vector(
                chunks, _criterion_query_text(criterion), budget,
                score_scale=score_scale, pool=pool, criterion=criterion,
            )
            if out is not None:
                out[1]["mode"] = "vector"
                return out
        except Exception as exc:  # noqa: BLE001 —— 选段是尽力环节，绝不阻塞评分
            log.warning("向量选段异常，回退关键词：%s", exc)
    picked, meta = select_chunks_by_tier(
        chunks,
        (("criterion", derive_keywords(criterion)),),
        budget,
        fallback_diffuse=fallback_diffuse,
        score_scale=score_scale,
        pool=pool,
        restatement_w=[criterion_restatement_weight(body, criterion) for _i, body in chunks],
    )
    meta["mode"] = "keyword"
    return picked, meta


def select_chunks_for_criterion(
    chunks: list[Chunk],
    criterion: dict,
    budget: int,
    fallback_diffuse: bool = True,
    *,
    chunk_cats: list[str | None] | None = None,
    chunk_cat_confs: list[float] | None = None,
) -> tuple[list[Chunk], dict]:
    """统一入口：vector 模式优先（BID_EVAL_SELECTION=vector），失败回退关键词分档。

    chunk_cats（与 chunks 等长）非空时启用类别软过滤：
    - price 类评分项：**硬过滤**——只在 price 类块里选；price 块为空（分类缺失/
      判丢）→ 回退全库 + meta.price_fallback=True（防分类故障打死报价分）；
    - 其余类：**软过滤**——先在类内选；类内为空或产出 < budget×0.5 → 全库混选
      且类内块分数 ×1.3（低置信继承页 ×1.1）；meta.soft_fallback=True。
    chunk_cats=None（默认）→ 与旧实现逐字节一致（兼容门禁）。
    chunk_cat_confs：每块类别置信度（可选，控制加成幅度）。
    """
    meta_keys: dict = {}
    if chunk_cats is None or len(chunk_cats) != len(chunks) or not chunks:
        picked, meta = _select_once(
            chunks, criterion, budget, fallback_diffuse,
            score_scale=None, pool=None,
        )
        return picked, meta

    from app.material_index.catalog import material_category_for

    target = material_category_for(criterion)
    meta_keys["in_class"] = sum(1 for c in chunk_cats if c == target)
    meta_keys["soft_fallback"] = False

    in_class = [i for i, c in enumerate(chunk_cats) if c == target]

    def _scale() -> list[float]:
        out = [1.0] * len(chunks)
        for i in in_class:
            conf = chunk_cat_confs[i] if chunk_cat_confs and i < len(chunk_cat_confs) else 1.0
            out[i] = _IN_CLASS_BOOST if conf >= 0.85 else _INHERIT_BOOST
        return out

    if target == "price":
        # 硬过滤：只在报价类块里选
        if in_class:
            picked, meta = _select_once(
                chunks, criterion, budget, fallback_diffuse,
                score_scale=None, pool=in_class,
            )
            meta.update(meta_keys)
            return picked, meta
        meta_keys["price_fallback"] = True
        meta_keys["soft_fallback"] = True
        picked, meta = _select_once(
            chunks, criterion, budget, fallback_diffuse,
            score_scale=None, pool=None,
        )
        meta.update(meta_keys)
        return picked, meta

    if not in_class:
        meta_keys["soft_fallback"] = True
        picked, meta = _select_once(
            chunks, criterion, budget, fallback_diffuse,
            score_scale=None, pool=None,
        )
        meta.update(meta_keys)
        return picked, meta

    # 阶段 1：类内选
    picked, meta = _select_once(
        chunks, criterion, budget, fallback_diffuse,
        score_scale=None, pool=in_class,
    )
    # 充足性只看类内产出量（truncated=False 只说明池内全选完，池本身可能很小）
    if meta.get("chars", 0) >= budget * _SOFT_FILL_RATIO:
        meta.update(meta_keys)
        return picked, meta

    # 阶段 2：全库混选 + 类内加成
    meta_keys["soft_fallback"] = True
    picked, meta = _select_once(
        chunks, criterion, budget, fallback_diffuse,
        score_scale=_scale(), pool=None,
    )
    meta.update(meta_keys)
    return picked, meta


def build_evidence_map(
    chunks: list[Chunk],
    criterion: dict,
    *,
    min_gap: int = 2,
    chunk_cats: list[str | None] | None = None,
) -> tuple[list[dict], str] | None:
    """证据地图：全量分块按相关性排序并聚类为不相交证据簇（零 LLM 成本）。

    返回 (簇列表, 排序用查询文本)；嵌入服务不可用返回 None。
    簇结构：{"ids": [块序号], "chars": 总字符, "score": 聚合相似度}——
    相邻高分组块合并为一簇，簇与簇之间至少间隔 min_gap 个低分块（保证语义独立）。
    chunk_cats 非空时类内块种子分 ×1.3（软过滤口径，不硬排除）。
    阈值与加成分离（2026-10-29 run 63 实证）：raw 数组只含目录/复述降权，
    floor_seed/floor_extend/种子与邻居资格一律按 raw 判定；类内加成只影响
    种子排序次序（让内容簇在 top-K 预算里优先），cluster.score 保留加成后
    分数供展示——加成乘进阈值曾把扩展门槛抬到够不着邻居，簇退化单块。
    """
    from app.evidence import embed_client

    query = _criterion_query_text(criterion)
    if not chunks or not query.strip():
        return None
    try:
        qvec = embed_client.embed_one(query)
        if qvec is None:
            return None
        cvecs = embed_client.embed_batch([body for _i, body in chunks])
        if any(v is None for v in cvecs):
            return None
    except RuntimeError as exc:
        log.warning("证据地图构建失败：%s", exc)
        return None

    # raw：TOC/复述降权后、类内加成前的原始分——阈值与成员资格的唯一依据
    raw = [embed_client.cosine(qvec, v) for v in cvecs if v is not None]
    # 目录/索引 chunk 降权（与关键词选段同一判据）：索引表逐字是评分项名，
    # 余弦必然最高——不降权则证据簇永远由目录页当种子（run 58 实证）；
    # 复述块降权同理（逐字照抄评分标准的应答表块冒充证据，run 63 实证）
    for i, (_no, body) in enumerate(chunks):
        w = index_text_weight(body) * criterion_restatement_weight(body, criterion)
        if w < 1.0:
            raw[i] = round(raw[i] * w, 6)
    # scores：类内加成后的展示/排序分——只决定种子遍历次序，不决定资格
    scores = list(raw)
    if chunk_cats is not None and len(chunk_cats) == len(chunks):
        from app.material_index.catalog import material_category_for

        target = material_category_for(criterion)
        for i, c in enumerate(chunk_cats):
            if c == target:
                scores[i] = round(scores[i] * _IN_CLASS_BOOST, 6)
    order = sorted(range(len(chunks)), key=lambda i: -scores[i])

    # 贪心聚类：从最高分块起，双向行走吸收「相对高分」相邻块成簇。
    # 阈值必须用相对分（top 分的 ≥55%）——bge-m3 对中文文本的余弦普遍为正，
    # 绝对阈值 >0 会让整本文本聚成一个巨型簇（2026-10-16 实证：57 万字符
    # 单簇，分层形同虚设）。相对阈值保证只有与核心证据同量级相关的块才并入。
    # 阈值基于 raw（加成前）：加成抬高 top 会连锁抬高 floor_extend，邻居
    # （原始 0.5~0.66）全部够不着 → 簇退化单块 → top-3 全被通用块占满。
    raw_top = max(raw)
    if raw_top <= 0:
        return None
    floor_seed = raw_top * 0.55  # 开新簇的种子门槛（保留多簇潜力）
    floor_extend = raw_top * 0.75  # 行走扩展门槛（更严——防连续大片被吞成巨型簇）
    claimed: set[int] = set()
    clusters: list[dict] = []
    for seed_idx in order:
        # 种子资格用 raw（防"低原始分靠加成当种子"）
        if raw[seed_idx] < floor_seed or seed_idx in claimed:
            continue
        if any(abs(seed_idx - c_id) <= min_gap for cid in clusters for c_id in cid["ids"]):
            continue
        ids = [seed_idx]
        claimed.add(seed_idx)
        for direction in (-1, 1):
            j = seed_idx
            while True:
                nxt = j + direction
                # 邻居扩展资格也用 raw（加成不决定成员资格）
                if 0 <= nxt < len(chunks) and nxt not in claimed and raw[nxt] >= floor_extend:
                    ids.append(nxt)
                    claimed.add(nxt)
                    j = nxt
                else:
                    break
        ids.sort()
        chars = sum(len(chunks[i][1]) for i in ids)
        clusters.append({
            "ids": ids,
            "chunk_nos": [chunks[i][0] for i in ids],
            "chars": chars,
            # 展示分保留加成后分数（含类内优先语义）
            "score": round(max(scores[i] for i in ids), 4),
        })
        if len(clusters) >= 12:  # 安全上限：证据簇过多时只保留最相关的前 12 簇
            break
    if not clusters:
        return None
    return clusters, query
