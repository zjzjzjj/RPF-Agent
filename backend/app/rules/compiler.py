"""规则编译：招标文件条款 → LLM 抽取 → 草稿规则包 + 证据引用 + 六态分诊。

流程（对应方案 ③④ 步）：
1. 取招标文件全文（页序拼接，与 document_pages 逐字一致，保证引文可定位）；
2. 走 LLM 网关抽取（真实模型语义抽取）；
3. 每条规则的 quote 建证据引用并核验（无证据不评分）；
4. 六态分诊：verified / needs_input / ambiguous（档位无分值）/ gap / conflict / invalid；
5. 跑 12 条确定性校验器，findings 落库。
"""
from __future__ import annotations

import logging
import re
import time
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app import config
from app.db.models import (
    Criterion,
    CriterionCheck,
    CriterionLevel,
    Document,
    DocumentPage,
    DocNode,
    EvidenceCitation,
    PackageValidation,
    PriceFormula,
    RulePackage,
)
from app.db.session import session_scope
from app.errors import AppError, NotFound
from app.evidence.verifier import verify_citation
from app.jobs.handlers import register
from app.jobs.queue import JobContext
from app.llm.base import LLMError, LLMResult
from app.llm.gateway import Gateway, get_gateway
from app.parsing.normalize import normalize_no_ws
from app.rules import prompts
from app.rules.model import ExtractedCriterion, RuleExtraction

log = logging.getLogger("bid_eval.rules")

MAX_COMPILE_CHARS = 30_000  # 送进模型的招标文件字符预算（超预算按章节优先级选取，不再盲目截头）

# 中文按 ~1.4 字符/token 估算（Qwen 实测 29794 字符 ≈ 20147 tokens，即 1.48）。
# 取更保守的 1.4：宁可多留输出空间，也不要让输入把上下文吃光。
_CHARS_PER_TOKEN = 1.4
_CONTEXT_SAFETY_TOKENS = 512


def compile_char_budget() -> int:
    """按模型上下文推导可送入的字符预算；未配置上下文时沿用 MAX_COMPILE_CHARS。

    必须同时给输出留够空间：编译产物是一份完整规则 JSON（可达数千 token）。
    只用 provider 层的 max_tokens 钳制不够——输入占满 20K 后仅剩 4K 输出，
    JSON 必然被截断（2026-10-20 实证：本地 Qwen3-8B 24576 上下文）。
    """
    ctx = config.llm_context_tokens()
    if ctx <= 0:
        return MAX_COMPILE_CHARS
    out = config.llm_max_output_tokens()
    usable = ctx - out - _CONTEXT_SAFETY_TOKENS
    if usable <= 0:
        return MAX_COMPILE_CHARS  # 配置异常时不额外收紧，交给 provider 报错
    return min(MAX_COMPILE_CHARS, int(usable * _CHARS_PER_TOKEN))


def split_for_extraction(text: str, chunk_chars: int | None = None) -> list[str]:
    """把选段按行边界切成若干块，每块尽量不超过 chunk_chars 字符。

    分块是长文档可靠抽取的前提：规则 JSON 的输出量随文档线性增长，而模型的
    单次输出上限是固定的。按行切分保证不会在句子中间断开（条款跨块丢失）。
    """
    size = chunk_chars if chunk_chars is not None else config.compile_chunk_chars()
    if size <= 0 or len(text) <= size:
        return [text]

    chunks: list[str] = []
    buf: list[str] = []
    used = 0
    for line in text.split("\n"):
        piece = line + "\n"
        # 单行本身超长：先冲掉缓冲区，再硬切成整块
        if len(piece) > size:
            if buf:
                chunks.append("".join(buf))
                buf, used = [], 0
            while len(piece) > size:
                chunks.append(piece[:size])
                piece = piece[size:]
            if piece.strip():
                buf.append(piece)
                used = len(piece)
            continue
        if buf and used + len(piece) > size:
            chunks.append("".join(buf))
            buf, used = [], 0
        buf.append(piece)
        used += len(piece)
    if buf:
        chunks.append("".join(buf))
    return [c for c in chunks if c.strip()] or [text]


def dedupe_extracted(items: list["ExtractedCriterion"]) -> list["ExtractedCriterion"]:
    """跨块去重：同 category + 规范化名称只保留第一条。

    分块边界附近的上下文重叠会让模型在相邻两块里各抽一次同一条规则。
    """
    seen: set[tuple[str, str]] = set()
    out: list["ExtractedCriterion"] = []
    for c in items:
        key = (c.category, normalize_no_ws(c.name or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# 抽取结果的确定性补全（8B 模型经常漏字段，不能把正确性押在提示词上）
# ---------------------------------------------------------------------------
_RE_RATE_DOWN = re.compile(
    r"每低\s*(\d+(?:\.\d+)?)\s*个百分点[^。；\n]{0,12}?扣\s*(\d+(?:\.\d+)?)\s*分"
)
_RE_RATE_UP = re.compile(
    r"每高\s*(\d+(?:\.\d+)?)\s*个百分点[^。；\n]{0,12}?扣\s*(\d+(?:\.\d+)?)\s*分"
)
_RE_TRIM_BOTH = re.compile(r"扣除一个最高价和一个最低价")
_RE_ARITH_AVG = re.compile(r"算术平均")
_RE_MAX_ITEM_SCORE = re.compile(r"最高(?:得|扣)?\s*(\d+(?:\.\d+)?)\s*分")
_RE_AMOUNT_WAN = re.compile(r"合同金额(?:达到|≥|不低于|不少于)?\s*(\d+(?:\.\d+)?)\s*万元")
_PRICE_HINTS = ("报价", "价格", "评标价", "基准价", "投标价")


def _rate_per_point(match: re.Match[str]) -> Decimal:
    """把"每低 X 个百分点扣 Y 分"换算成每 1 个百分点扣分。"""
    points = Decimal(match.group(1))
    deduct = Decimal(match.group(2))
    return (deduct / points).quantize(Decimal("0.0001"))


def parse_price_formula_text(text: str) -> dict[str, Any]:
    """从一段招标原文解析偏离扣分参数；非偏离扣分型返回空 dict。

    供编译期（enrich_extracted）与草稿自愈（normalize_package）共用。
    """
    m_down = _RE_RATE_DOWN.search(text or "")
    m_up = _RE_RATE_UP.search(text or "")
    if not m_down and not m_up:
        return {}
    out: dict[str, Any] = {
        "formula_type": "deviation_deduct",
        "k": Decimal("1"),
    }
    if m_down:
        out["deduct_rate_down"] = _rate_per_point(m_down)
    if m_up:
        out["deduct_rate_up"] = _rate_per_point(m_up)
    if _RE_TRIM_BOTH.search(text):
        out["benchmark_method"] = "average_trimmed"
        out["trim_count"] = 1
        out["trim_threshold"] = 5
    elif _RE_ARITH_AVG.search(text):
        out["benchmark_method"] = "average"
    return out


def parse_count_params_text(
    text: str,
    max_score: Decimal | None,
    per_item: Decimal | None,
    name: str = "",
) -> dict[str, str]:
    """从评分项原文补 count 参数：最高项数/金额门槛/人员范围。"""
    params: dict[str, str] = {}
    if per_item is not None and per_item > 0 and max_score is not None:
        derived = int(Decimal(str(max_score)) / per_item)
        if derived > 0:
            params["max_items"] = str(derived)
    m = _RE_AMOUNT_WAN.search(text or "")
    if m:
        params["min_amount_yuan"] = str(int(Decimal(m.group(1)) * Decimal(10000)))
    # 人员范围只看评分项名称（描述里常出现"项目负责人/团队"只是证明材料要求）
    if "团队" in name or "成员" in name:
        params["person_scope"] = "member"
    elif "负责人" in name:
        params["person_scope"] = "leader"
    return params


def _price_window(c: "ExtractedCriterion", tender_text: str) -> str:
    """价格项的正则作用域：先看自身描述/quote，再看 quote 之后的窗口。

    不能扫全文——正文里的工期/违约条款也会出现"每低/每高 1 个百分点扣…"。
    """
    own = f"{c.description or ''}\n{c.quote or ''}"
    if _RE_RATE_DOWN.search(own) or _RE_RATE_UP.search(own):
        return own
    quote = (c.quote or "").strip()
    if quote:
        # 前 20 字里通常没有换行，直接定位；找不到再用去空白匹配
        pos = tender_text.find(quote[:20])
        if pos < 0:
            q_norm = normalize_no_ws(quote)
            t_norm = normalize_no_ws(tender_text)
            npos = t_norm.find(q_norm[:20])
            if npos >= 0:
                # 归一化索引无法直接回映原文，退化为从价格关键词附近找
                for hint in _PRICE_HINTS:
                    pos = tender_text.find(hint)
                    if pos >= 0:
                        break
        if pos >= 0:
            return tender_text[pos : pos + 4000]
    return own


def enrich_extracted(
    criteria: list["ExtractedCriterion"], tender_text: str
) -> list["ExtractedCriterion"]:
    """用确定性规则补齐 LLM 漏掉的公式/计数参数（就地修改并返回）。"""
    for c in criteria:
        if c.category == "price":
            window = _price_window(c, tender_text)
            parsed = parse_price_formula_text(window)
            if parsed:
                c.formula_type = "deviation_deduct"
                c.k = Decimal("1")
                if parsed.get("deduct_rate_down") is not None:
                    c.deduct_rate_down = parsed["deduct_rate_down"]
                if parsed.get("deduct_rate_up") is not None:
                    c.deduct_rate_up = parsed["deduct_rate_up"]
            # 基准法只在原文明确表述时改写，否则保留模型给出的值
            if _RE_TRIM_BOTH.search(window):
                c.benchmark_method = "average_trimmed"
                c.trim_count = 1
                c.trim_threshold = 5
            elif _RE_ARITH_AVG.search(window) and not c.benchmark_method:
                c.benchmark_method = "average"

        if c.score_type == "count":
            params = dict(c.params or {})
            per_item = c.count_per_item
            if per_item is None and params.get("per_item") is not None:
                try:
                    per_item = Decimal(str(params["per_item"]))
                except Exception:  # noqa: BLE001
                    per_item = None
            if per_item is not None and c.count_per_item is None:
                c.count_per_item = per_item
            derived = parse_count_params_text(
                f"{c.description or ''}\n{c.quote or ''}",
                c.max_score,
                per_item,
                c.name,
            )
            for key, value in derived.items():
                params.setdefault(key, value)
            c.params = params
    return criteria


def material_block_applies(block: str, criterion_name: str) -> bool:
    """【证明材料】块是否适用于某个计数项（按主体词匹配，避免串到别的评分项）。"""
    name = criterion_name or ""
    if any(k in block for k in ("负责人", "团队", "成员", "人员")):
        return any(k in name for k in ("负责人", "团队", "成员", "人员"))
    if any(k in block for k in ("业绩", "案例", "合同")):
        return any(k in name for k in ("业绩", "案例", "合同"))
    return True


def fold_material_blocks(
    criteria: list["ExtractedCriterion"],
) -> list["ExtractedCriterion"]:
    """把单独抽出来的【证明材料】块折叠进同页 count 项，避免重复计分。

    真实标书里【证明材料】通常是评分项单元格的续行，模型会把它当成一条
    score_type=veto 的独立条目（浙江中烟项目实证：c30 导致总分 106）。
    """
    out: list["ExtractedCriterion"] = []
    for c in criteria:
        text = f"{c.description or ''}\n{c.quote or ''}".strip()
        is_material_block = text.startswith("【证明材料】")
        if is_material_block and c.score_type in ("veto", "match") and not c.is_veto:
            targets = [
                t
                for t in out
                if t.score_type == "count"
                and material_block_applies(text, t.name)
                and (
                    c.page_hint is None
                    or t.page_hint is None
                    or abs(int(t.page_hint) - int(c.page_hint)) <= 1
                )
            ]
            for t in targets:
                existing = (t.required_evidence or "").strip()
                if text not in existing:
                    t.required_evidence = (
                        f"{existing}\n{text}".strip() if existing else text
                    )
            continue  # 不再作为独立评分项落库
        out.append(c)
    return out


def _triage(c: ExtractedCriterion, quote_verified: bool, has_node: bool) -> str:
    """六态分诊。"""
    if c.score_type == "subjective" and c.levels and any(
        lv.min is None or lv.max is None for lv in c.levels
    ):
        return "ambiguous"  # 原文没给分值区间——不得由系统猜
    if not quote_verified or (c.quote is None):
        return "needs_input"
    # 主观项无档位不再阻断：引擎支持按 description 在 0-满分内评分，
    # 校验器 LEVELS_MISSING 也只是 warn（ADR 0004 的口径）。
    if c.score_type == "formula":
        if c.max_score is None:
            return "needs_input"
        if c.formula_type == "deviation_deduct":
            has_rate = bool(
                c.deduct_rate
                or c.deduct_rate_down
                or c.deduct_rate_up
                or (c.params or {}).get("deduct_rate_down")
                or (c.params or {}).get("deduct_rate_up")
            )
            if not has_rate:
                return "needs_input"
        elif c.k is None:
            return "needs_input"
    if c.score_type == "veto" and not c.check_type:
        return "needs_input"
    if has_node:
        return "verified"
    return "needs_input"


def _extract_chunk(
    gateway: "Gateway", chunk: str, *, data_class: str, label: str = ""
) -> tuple["RuleExtraction", "LLMResult"]:
    """单块抽取（契约不符允许一次修复重试）。"""
    user_prompt = prompts.RULE_USER_TEMPLATE.format(clause_text=chunk)
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            prompt = user_prompt
            if attempt == 2 and last_error is not None:
                prompt = (
                    f"{user_prompt}\n\n【修复请求】你上一次的输出存在如下问题：\n{last_error}\n"
                    "请修正后重新输出**完整**的 JSON（不要只输出差异部分）。"
                )
            return gateway.chat_json(
                prompts.RULE_SYSTEM_PROMPT,
                prompt,
                RuleExtraction,
                data_class=data_class,
                max_tokens=config.llm_max_output_tokens(),
            )
        except LLMError as exc:
            last_error = exc
            log.warning(
                "规则抽取%s第 %d 次尝试失败：%s",
                f"[分块 {label}]" if label else "", attempt, exc,
            )
            if attempt == 1:
                time.sleep(config.llm_retry_backoff_s())
    raise AppError(
        f"规则抽取失败（含修复重试）{f'（分块 {label}）' if label else ''}：{last_error}；"
        "若持续失败，多为模型输出上限不足或模型上下文过小——"
        "可换用上下文更大的模型、提高模型 max-model-len，或缩短招标文件后再试"
    )

# 章节优先级：真实招标文件的"评标办法/评分细则"通常排在最后（本文档在第 89-97 页），
# 盲目截前 N 字符会把它整段丢掉——评分项一条都抽不出来，只剩前附表的资格与否决条款。
# 这里按关键词给页分档，先保证评分办法进模型，再补资格与否决依据。
COMPILE_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "scoring",
        (
            "评标办法", "评标方法", "评分办法", "评分细则", "评分标准", "评分内容",
            "评审因素", "评审办法", "评审细则", "价格分", "报价部分", "最佳报价",
            "商务部分", "技术部分", "商务分", "技术分", "主观分", "客观分",
        ),
    ),
    (
        "eligibility",
        (
            "资格要求", "资格审查", "符合性审查", "否决", "废标", "无效标", "投标人须知",
        ),
    ),
)


def _load_tender_text(document_id: int) -> tuple[str, Document]:
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None:
            raise NotFound("文档不存在")
        if doc.kind not in ("tender", "addendum"):
            raise AppError("只能从招标文件编译规则")
        if doc.parse_status not in ("parsed", "degraded"):
            raise AppError(f"招标文件尚未解析完成（{doc.parse_status}）")
        rows = s.execute(
            select(DocumentPage).where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_no)
        ).scalars().all()
        text = "\n".join(p.text or "" for p in rows)
    return text, doc


def _load_tender_pages(document_id: int) -> list[tuple[int, str]]:
    with session_scope() as s:
        rows = s.execute(
            select(DocumentPage.page_no, DocumentPage.text)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_no)
        ).all()
    return [(int(no), txt or "") for no, txt in rows]


def select_compile_text(pages: list[tuple[int, str]], budget: int = MAX_COMPILE_CHARS) -> tuple[str, dict]:
    """章节感知选取：预算内优先保住评分办法，再补资格与否决依据。

    输出始终按页序拼接，页间以换行相连——与 ``_load_tender_text`` 同源同序，
    因此 LLM 给出的 quote 仍能在 document_pages 上逐字定位核验。

    返回 (text, meta)，meta 记录各档入选页数与是否发生裁剪，便于审计与排查。
    """
    full = "\n".join(t for _no, t in pages)
    if len(full) <= budget:
        return full, {
            "truncated": False,
            "chars": len(full),
            "total_chars": len(full),
            "pages": [no for no, _t in pages],
            "tiers": {},
        }

    by_page = dict(pages)
    chosen: set[int] = set()
    used = 0
    tier_hits: dict[str, int] = {}
    for name, keywords in COMPILE_TIERS:
        # 关键词只出现在章节首页是常态：续页（如"符合性审查"否决情形 (2)-(15)）自身零命中，
        # 若只按本页命中排序，续页会被同档位里低信息量的页挤掉。这里给相邻页半个权重，
        # 让章节成段入选。输出顺序另行按页序重排，不受影响。
        raw = {
            page_no: sum(1 for k in keywords if k in text)
            for page_no, text in pages
            if page_no not in chosen
        }
        order = [p for p, _t in pages]
        pos = {p: i for i, p in enumerate(order)}
        scored: list[tuple[float, int]] = []
        for page_no in order:
            if page_no in chosen:
                continue
            i = pos[page_no]
            neighbor = 0.0
            if i > 0:
                neighbor = max(neighbor, raw.get(order[i - 1], 0))
            if i + 1 < len(order):
                neighbor = max(neighbor, raw.get(order[i + 1], 0))
            score = raw.get(page_no, 0) + 0.5 * neighbor
            if score > 0:
                scored.append((score, page_no))
        scored.sort(key=lambda x: (-x[0], x[1]))
        picked = 0
        for _score, page_no in scored:
            cost = len(by_page[page_no]) + 1
            if used + cost > budget:
                continue
            chosen.add(page_no)
            used += cost
            picked += 1
        tier_hits[name] = picked

    # 预算未用满时按页序回填，尽量让模型看到上下文（不改变已入选页的相对顺序）
    if used < budget:
        for page_no, text in pages:
            if page_no in chosen:
                continue
            cost = len(text) + 1
            if used + cost > budget:
                break
            chosen.add(page_no)
            used += cost
        tier_hits["fill"] = len(chosen) - sum(tier_hits.values())

    selected = [(no, t) for no, t in pages if no in chosen]
    meta = {
        "truncated": True,
        "chars": sum(len(t) + 1 for _no, t in selected),
        "total_chars": len(full),
        "pages": [no for no, _t in selected],
        "tiers": tier_hits,
    }
    return "\n".join(t for _no, t in selected), meta


def _next_version(project_id: str) -> int:
    with session_scope() as s:
        row = s.execute(
            select(RulePackage.version).where(RulePackage.project_id == project_id)
            .order_by(RulePackage.version.desc()).limit(1)
        ).scalar_one_or_none()
    return (row or 0) + 1


def find_node_for_quote(document_id: int, page_no: int | None, quote: str) -> str | None:
    """在给定页（及相邻页）上找"包含该引文的最深节点"。

    两个真实世界的坑（浙江中烟项目实测，26 条里 19 条栽在这上面）：

    ① 软换行：节点文本里是换行，normalize 后成空格，而模型引文跨行逐字复制、不含
       空格。用朴素子串包含会因这一个空格失配——引文核验通过了，节点却定位不到。
    ② 跨页节点只归属一页：如"报价部分（60分）"节点落在 p96，而引文页码是 p97；
       模型引文也常非逐字（尾部有出入），证据层用 prefix_fallback 才通过。

    因此这里与证据核验保持同一口径：空白不敏感 + 相邻页 + 前缀逐级回退。
    """
    q = normalize_no_ws(quote)
    if not q:
        return None
    page = page_no or -1
    pages = [page] + [p for p in (page - 1, page + 1) if p > 0]
    with session_scope() as s:
        rows = s.execute(
            select(DocNode).where(DocNode.document_id == document_id, DocNode.page.in_(pages))
        ).scalars().all()
    needles = [q] + [normalize_no_ws(quote[:n]) for n in config.PREFIX_FALLBACK_LENGTHS]
    for needle in needles:
        if not needle:
            continue
        best: DocNode | None = None
        for n in rows:
            if needle in normalize_no_ws(n.text or ""):
                if best is None or n.depth > best.depth:
                    best = n
        if best is not None:
            return best.node_id
    return None


def _persist_criterion(
    s,  # noqa: ANN001
    package_id: int,
    document_id: int,
    c: ExtractedCriterion,
    seq: int,
    used_codes: set[str],
) -> tuple[int, int | None]:
    """写入一条评分项及其子结构；返回 (criterion_id, citation_id)。

    编号唯一性由这里兜底：真实模型可能返回重复或空编号，唯一约束必须先行满足。
    **不在本函数内做核验**——核验要用独立会话读引用，必须等外层事务提交后
    （SQLite 事务隔离下，未提交的行对其他连接不可见）。
    """
    citation_id: int | None = None
    if c.quote:
        cit = EvidenceCitation(
            document_id=document_id,
            quote=c.quote,
            page_no=c.page_hint,
            citation_role="criterion_source",
            created_by="llm",
        )
        s.add(cit)
        s.flush()
        citation_id = cit.id

    # 先按"未核验"落库；_verify_and_backfill 会在核验后重新分诊
    status = _triage(c, quote_verified=False, has_node=False)
    code = c.code.strip() or f"c{seq}"
    if code in used_codes:  # 模型返回重复编号 → 加序号后缀保证唯一
        n = 2
        base = code
        while f"{base}~{n}" in used_codes:
            n += 1
        code = f"{base}~{n}"
    used_codes.add(code)
    is_veto = c.score_type == "veto"
    crit = Criterion(
        package_id=package_id,
        code=code,
        category=c.category,
        name=c.name,
        description=c.description,
        # 否决项不得同时作为评分因素给分（法规红线）——忽略模型给的分值
        max_score=None if is_veto else c.max_score,
        weight=None,  # 权重永不来自模型
        score_type=c.score_type,
        is_veto=is_veto,
        veto_consequence=c.veto_consequence if is_veto else None,
        required_evidence=c.required_evidence,
        source_node_id=None,  # 核验通过后回填
        source_page=c.page_hint,
        source_quote=c.quote,
        status=status,
        confidence=c.confidence,
        sort_order=seq,
    )
    s.add(crit)
    s.flush()

    # 档位
    for lv in c.levels:
        s.add(
            CriterionLevel(
                criterion_id=crit.id,
                level_label=lv.label,
                score_min=lv.min if lv.min is not None else Decimal(0),
                score_max=lv.max if lv.max is not None else Decimal(0),
                description=lv.criteria,
                source_quote=lv.quote,
            )
        )

    # 客观检查原语（否决/匹配/计数类需要 params 供确定性引擎执行）。
    # 价格项的参数（trim_count 等）走 PriceFormula.params_json，不能误建 human_only check。
    wants_check = bool(c.check_type) or (
        c.score_type in ("veto", "match", "count")
        and (bool(c.params) or c.count_per_item is not None)
    )
    if wants_check:
        from app.rules.freeze import _jsonable

        params = dict(c.params or {})
        # 契约允许模型单独给 count_per_item；确定性引擎只认 params["per_item"]，
        # 这里必须折叠，否则真实模型路径下 count 项全部恒 0 分。
        if c.count_per_item is not None and "per_item" not in params:
            params["per_item"] = c.count_per_item
        s.add(
            CriterionCheck(
                criterion_id=crit.id,
                check_type=c.check_type or ("count_at_least" if c.score_type == "count" else "human_only"),
                field=params.get("field") if isinstance(params.get("field"), str) else None,
                params_json=_jsonable(params),
                on_fail="recommend_reject" if is_veto else "need_human",
                source_quote=c.quote,
            )
        )

    # 价格公式：扣分率与基准法参数全部落盘（快照是评分唯一输入，漏一个字段
    # 就会让偏离扣分退化成"人人满分"）
    if c.category == "price":
        from app.rules.freeze import _jsonable

        ext: dict[str, str] = {}
        if c.deduct_rate is not None:
            ext["deduct_rate"] = str(c.deduct_rate)
        if c.deduct_rate_down is not None:
            ext["deduct_rate_down"] = str(c.deduct_rate_down)
        if c.deduct_rate_up is not None:
            ext["deduct_rate_up"] = str(c.deduct_rate_up)
        if c.trim_count is not None:
            ext["trim_count"] = str(c.trim_count)
        if c.trim_threshold is not None:
            ext["trim_threshold"] = str(c.trim_threshold)
        s.add(
            PriceFormula(
                package_id=package_id,
                criterion_id=crit.id,
                formula_type=c.formula_type or "benchmark_ratio",
                benchmark_method=c.benchmark_method or "average",
                k=c.k,
                trim_ratio=c.trim_ratio,
                deduct_rate=c.deduct_rate,
                upper_bound=c.upper_bound,
                lower_bound=c.lower_bound,
                rounding=c.rounding if c.rounding is not None else 2,
                exclude_invalid=True,
                params_json=_jsonable(ext) if ext else None,
                source_node_id=None,  # 核验通过后回填
                source_quote=c.quote,
            )
        )
    return crit.id, citation_id


def run_validations(package_id: int) -> list[dict]:
    """跑 12 条校验器并把 findings 落库（confirm/freeze 前置）。"""
    from app.rules.validators import run_validators as run_checks
    from app.services.rule_service import load_criteria_for_snapshot

    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is None:
            raise NotFound("规则包不存在")
        criteria = load_criteria_for_snapshot(s, package_id)
    snapshot = {
        "package": {"project_id": pkg.project_id, "version": pkg.version,
                    "status": pkg.status, "total_max": 100},
        "criteria": criteria,
    }
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        source_doc = pkg.source_document_id if pkg else None

    def _node_exists(node_id: str) -> bool:
        # 法律规则的引用指向法条知识库而非项目文档（设计稿 §4.4）：
        # 真实核验"该法规该条是否在知识库里"，查无即 CITATION_UNREACHABLE。
        from app.rules.law_kb import LAW_PREFIX, node_exists as law_node_exists

        if node_id.startswith(LAW_PREFIX):
            return law_node_exists(node_id)
        if source_doc is None:
            return False
        with session_scope() as s:
            return s.execute(
                select(DocNode.id).where(DocNode.document_id == source_doc, DocNode.node_id == node_id)
            ).first() is not None

    findings = run_checks(snapshot, node_exists=_node_exists)
    # Finding.subject_id 是 criterion code（字符串）；落库列是 Integer，
    # 解析成数据库 id，前端才能把 issue 定位到具体规则。
    code_to_id = {c["code"]: c.get("id") for c in criteria}
    with session_scope() as s:
        from sqlalchemy import delete

        # 校验结果是"当前草稿"的派生数据：直接清空重写
        s.execute(delete(PackageValidation).where(PackageValidation.package_id == package_id))
        for f in findings:
            subject_id = None
            if f.subject_type in ("criterion", "formula") and f.subject_id:
                resolved = code_to_id.get(str(f.subject_id))
                subject_id = int(resolved) if isinstance(resolved, int) else None
            s.add(
                PackageValidation(
                    package_id=package_id,
                    code=f.code,
                    severity=f.severity,
                    subject_type=f.subject_type,
                    subject_id=subject_id,
                    message=f.message,
                    hint=f.hint,
                )
            )
    return [f.as_dict() for f in findings]


def compile_rules(
    project_id: str,
    document_id: int,
    actor: str,
    provider: str | None = None,
    model: str | None = None,
    merge_target_id: int | None = None,
    *,
    selection_meta_out: dict | None = None,
    ctx: JobContext | None = None,
) -> int:
    """执行一次编译，返回新草稿规则包 id。

    merge_target_id 非空 = merge 模式（S1）：产物为临时编译包，指向模板包，
    供逐条覆盖匹配（设计稿 §5.1 阶段 A）。
    selection_meta_out 非空时被原地写入选段元数据（truncated/chars/tiers），供
    调用层（compile_rules_job）落 Job.result_json 审计——不改返回值契约。
    """
    from app.llm.registry import get_provider

    _text, doc = _load_tender_text(document_id)  # 校验 kind/parse_status 并取回 doc
    text, sel = select_compile_text(_load_tender_pages(document_id), budget=compile_char_budget())
    if selection_meta_out is not None:
        selection_meta_out.update(sel)
    if sel["truncated"]:
        log.warning(
            "招标文件超长（%d 字符），按章节优先级选取 %d 字符，入选页 %s，档位命中 %s",
            sel["total_chars"], sel["chars"], sel.get("pages"), sel.get("tiers"),
        )

    gateway = get_gateway() if provider is None and model is None else Gateway(get_provider(provider, model))

    # 分块抽取：长招标文件的规则集合可能远超单次输出上限（99 页文档实测 12000
    # tokens 仍被截断）。按字符预算切块分别抽取，再合并去重——每块输入小，
    # 输出空间充足，模型不会中途被截断。
    chunks = split_for_extraction(text)
    criteria_acc: list[ExtractedCriterion] = []
    result_model: str | None = None
    for idx, chunk in enumerate(chunks, start=1):
        if ctx is not None and len(chunks) > 1:
            ctx.advance(message=f"规则抽取 {idx}/{len(chunks)}")
        extraction, result = _extract_chunk(
            gateway, chunk, data_class=doc.data_class,
            label=f"{idx}/{len(chunks)}" if len(chunks) > 1 else "",
        )
        criteria_acc.extend(extraction.criteria)
        result_model = result.model
    criteria_acc = dedupe_extracted(criteria_acc)
    # 确定性补全：先把【证明材料】块折叠进 count 项，再补公式/计数参数
    criteria_acc = fold_material_blocks(criteria_acc)
    # 类别确定性校正（落库前）：category=="price" 是价格公式落盘的门槛，
    # LLM 把商务/价格项标错类别会让对应链路整体失效（2026-10-21 实证）。
    from app.rules.category_map import apply_category_to_extracted

    apply_category_to_extracted(criteria_acc, document_id)
    criteria_acc = enrich_extracted(criteria_acc, text)

    version = _next_version(project_id)
    with session_scope() as s:
        pkg = RulePackage(
            project_id=project_id,
            version=version,
            status="draft",
            source_document_id=document_id,
            merge_target_id=merge_target_id,
            compiled_by=actor,
            model=result_model,
            prompt_version=config.llm_prompt_version(),
            created_by=actor,
        )
        s.add(pkg)
        s.flush()
        package_id = pkg.id

        seq = 0
        used_codes: set[str] = set()
        pending: list[tuple[int, int | None, ExtractedCriterion]] = []
        for c in criteria_acc:
            seq += 1
            crit_id, cit_id = _persist_criterion(s, package_id, document_id, c, seq, used_codes)
            pending.append((crit_id, cit_id, c))

    # 事务已提交：核验引用（独立会话）→ 回填页码/条款节点 → 重新分诊
    _verify_and_backfill(document_id, pending)
    # 节点已确定：按节点标题链再校正一次类别（权威页码，留痕），
    # 并把表格里被模型漏掉的子项/档位续行补进 description
    from app.rules.category_map import apply_category_from_nodes

    apply_category_from_nodes(package_id)
    enrich_descriptions_from_nodes(package_id)

    run_validations(package_id)
    return package_id


def _verify_and_backfill(
    document_id: int, pending: list[tuple[int, int | None, ExtractedCriterion]]
) -> None:
    """核验每条引用并把页码、条款节点回填到评分项，按核验结果重新分诊。"""
    from app.db.models import Criterion

    for crit_id, cit_id, c in pending:
        if cit_id is None:
            continue
        cit = verify_citation(cit_id)
        if cit is None:
            continue
        page_no = cit.page_no
        node_id = None
        if cit.verify_method is not None and page_no is not None:
            node_id = find_node_for_quote(document_id, page_no, c.quote or "")
        status = _triage(c, bool(cit.verified), node_id is not None)
        with session_scope() as s:
            crit = s.get(Criterion, crit_id)
            if crit is None:
                continue
            crit.source_page = page_no
            crit.source_node_id = node_id
            crit.status = status
            s.add(crit)


def _has_scoring_detail(text: str) -> bool:
    """描述里是否已经有可执行的子项/档位明细。"""
    return bool(
        re.search(r"[（(]\s*0\s*[-~至]\s*\d", text or "")
        or re.search(r"得\s*\d+(?:\.\d+)?\s*[-~至]\s*\d+(?:\.\d+)?\s*分", text or "")
    )


def _normalized_span(haystack: str, needle: str) -> tuple[int, int] | None:
    """忽略空白/排版归一化定位 needle，返回其在 haystack 原文中的 [start, end)。"""
    from app.parsing.normalize import normalize_no_ws_with_map

    needle_n = normalize_no_ws(needle)
    if not needle_n:
        return None
    norm, idx = normalize_no_ws_with_map(haystack)
    pos = norm.find(needle_n)
    if pos < 0:
        return None
    return idx[pos], idx[pos + len(needle_n) - 1] + 1


def _quote_span(haystack: str, quote: str) -> tuple[int, int, bool] | None:
    """定位 quote；表格列文字插入把原句打断时退回前缀匹配。

    返回 (start, end, exact)。exact=False 表示只匹配到前缀，end 不是 quote 的真实结尾。
    """
    full = _normalized_span(haystack, quote)
    if full is not None:
        return full[0], full[1], True
    q = (quote or "").strip()
    # 真实 PDF 表格会把"3 运维方案 0-8 主观分"插进句子中间，整句无法连续匹配；
    # 逐步缩短前缀，取第一个能定位到开头的长度。
    for size in (60, 40, 30, 20, 12):
        if len(q) <= size:
            continue
        part = _normalized_span(haystack, q[:size])
        if part is not None:
            return part[0], part[1], False
    return None


def enrich_descriptions_from_nodes(package_id: int) -> int:
    """把 source node 里紧跟 quote 的评分细则续行补进主观项 description。

    真实招标文件的表格在 PDF 里被拆成多行：模型只抄了首句，子项分值
    （"协同办公（0-2 分）…"）或档位文字（"得 3-4 分…"）留在同一节点后半段。
    不补进描述，主观评分就只能按名字整体印象打分（评分趋同的直接原因）。
    """
    from app.db.models import Criterion

    changed = 0
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is None or pkg.source_document_id is None:
            return 0
        doc_id = pkg.source_document_id
        crits = s.execute(
            select(Criterion).where(
                Criterion.package_id == package_id,
                Criterion.status != "retired",
                Criterion.score_type == "subjective",
            )
        ).scalars().all()
        groups: dict[str, list[Criterion]] = {}
        for c in crits:
            if c.source_node_id and (c.source_quote or "").strip():
                groups.setdefault(c.source_node_id, []).append(c)
        for node_id, group in groups.items():
            node = s.execute(
                select(DocNode).where(
                    DocNode.document_id == doc_id, DocNode.node_id == node_id
                )
            ).scalar_one_or_none()
            if node is None or not (node.text or "").strip():
                continue
            group.sort(key=lambda x: (x.sort_order or 0, x.id))
            spans = [
                (c, _quote_span(node.text, c.source_quote or "")) for c in group
            ]
            # 必须按 quote 在节点中的实际位置排序：合并包的 sort_order 来自模板
            # （T4.1/T4.2/T4.3 在前，c23… 追加项在后），与原文表格顺序不一致。
            spans = [x for x in spans if x[1] is not None]
            spans.sort(key=lambda x: x[1][0])  # type: ignore[index]
            for i, (crit, span) in enumerate(spans):
                if span is None:
                    continue
                desc = crit.description or ""
                # 已带可执行细则且长度正常 → 不重复追加。
                # 超过 800 字符的"细则"通常是上一版把整个节点都塞进了描述
                # （表格列打断导致 next_start 找不到），需要按 quote 重建。
                if _has_scoring_detail(desc) and len(desc) <= 800:
                    continue
                start, quote_end, exact = span
                next_start = (
                    spans[i + 1][1][0]  # type: ignore[index]
                    if i + 1 < len(spans)
                    else len(node.text)
                )
                segment = node.text[start:next_start].strip()
                if not segment or not _has_scoring_detail(segment):
                    continue
                # 整句能连续定位：去掉 quote 本身只留续行；
                # 表格列打断只能前缀定位：保留整格文本（含被打断的原句），
                # 否则会把 T4.2 的"运维服务方[3 运维方案 0-8 主观分]案"截断。
                tail = (
                    node.text[quote_end:next_start].strip()
                    if exact
                    else segment
                )
                if not tail:
                    continue
                base = (crit.source_quote or "").strip()
                new_desc = (
                    f"{base}\n{tail}".strip()
                    if base and tail not in base
                    else (desc + "\n" + tail).strip()
                )
                if new_desc == desc:
                    continue
                if tail in desc and len(desc) <= 800:
                    continue
                crit.description = new_desc[:4000]
                s.add(crit)
                changed += 1
    return changed


@register("compile_rules")
def compile_rules_job(ctx: JobContext, payload: dict) -> dict:
    project_id = payload["project_id"]
    document_id = int(payload["document_id"])
    actor = payload.get("actor", "system")
    provider = payload.get("provider")
    model = payload.get("model")
    merge_target_id = payload.get("merge_target_id")
    ctx.start(total=4, message="加载招标文件")
    _load_tender_text(document_id)
    ctx.advance(message="LLM 抽取评分规则")
    selection_meta: dict = {}
    package_id = compile_rules(
        project_id, document_id, actor, provider, model,
        merge_target_id=int(merge_target_id) if merge_target_id else None,
        selection_meta_out=selection_meta,
        ctx=ctx,
    )
    if selection_meta.get("truncated"):
        ctx.advance(message=(
            f"已按章节优先级选取 {selection_meta['chars']}/{selection_meta['total_chars']} 字符"
            f"（{len(selection_meta.get('pages') or [])} 页；档位命中 {selection_meta.get('tiers') or {}}）"
        ))
    else:
        ctx.advance(message="校验与分诊完成")

    result: dict = {"package_id": package_id}
    if selection_meta.get("truncated"):
        result["selection_meta"] = selection_meta
    if merge_target_id and config.auto_mode():
        # 全自动模式：合并配对自动采纳（覆盖/追加/弃用全留痕），不再进人工抽屉。
        # best-effort：合并处置失败不让编译失败，原因进 result_json 供前端提示。
        ctx.advance(message="自动合并：生成匹配提案并采纳")
        try:
            from app.services import merge_service

            target_id = merge_service.auto_resolve_merge(
                package_id, actor=actor, provider=provider, model=model)
            result.update(auto_resolved=True, target_package_id=target_id)
            # 合并后校验摘要：总分/价格公式可能被覆盖/追加破坏，
            # 自动路径只 merge、不 confirm/freeze，由前端提示人工修正。
            from app.services import rule_service

            findings = rule_service.get_validations(target_id)
            result["post_merge"] = {
                "error_codes": sorted({v.code for v in findings if v.severity == "error"}),
                "warn_codes": sorted({v.code for v in findings if v.severity != "error"}),
                "errors": [
                    {"code": v.code, "message": v.message}
                    for v in findings
                    if v.severity == "error"
                ],
            }
        except Exception as exc:  # noqa: BLE001 —— 人工兜底通道保留（匹配提案抽屉）
            log.warning("自动合并失败（可人工在匹配提案抽屉处置）：%s", exc)
            result.update(auto_resolved=False, auto_resolve_error=str(exc)[:300])

    ctx.advance(message="完成")
    return result
