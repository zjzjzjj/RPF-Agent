# -*- coding: utf-8 -*-
"""评标细则表格骨架：确定性解析评分项清单（零 LLM）。

真实招标文件的「评标细则」通常是标准表格（序号/评审因素/主客观/分值/评标细则），
pymupdf find_tables 可确定性抽出。难点形态（浙江多媒体维保项目实证）：
- 子项行的「序号/评审因素」列为空（挂父级组，组分值在评审因素 cell 的"（N 分）"里）；
- **续表跨页无表头**（p136/p137 的表是 p135 表的延续，列映射须从上一张表继承）；
- 细则文字续行跨页（分值 cell 空 + 细则 cell 非空 → 追加上一计分行）；
- 【证明材料】以全宽独立行出现（挂上一计分行的 required_evidence，不产计分行）；
- 报价部分（常常 50 分）是**表外正文段落**，不在表里，须进台账（ledger）。

产出 ScoreSkeleton（rows + ledger + 合计），供编译后的对账补齐（reconcile.py）。
任何异常/无表/列头不符 → 返回 None，调用方回退纯 LLM 抽取（不劣于现状）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SkeletonRow:
    """评标细则表的一个计分行（子项粒度）。"""
    group: str                       # 父级评审因素名（如"服务保障情况"）
    group_total: Decimal | None      # 父级"（10 分）"里的分值
    sub_idx: int | None              # 细则文字"（N）"序号（可空）
    subjectivity: str | None         # "subjective" | "objective" | None
    max_score: Decimal               # 分值 cell 权威值（0-N 取上界）
    text: str                        # 细则全文（含续行合并）
    required_evidence: str | None = None
    page_no: int = 0


@dataclass
class LedgerEntry:
    """表外分值声明段（如"3、报价部分（50 分）…偏离扣分…"）。"""
    label: str
    max_score: Decimal
    text: str
    page_no: int


@dataclass
class ScoreSkeleton:
    rows: list[SkeletonRow] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    # P0-B：标段分设/多套评分表时的第二家族（重招实证：一二标段与三标段各一套，
    # 按标段并存不是冲突——主行集取先出现者，其余隔离待人工按标段选用）
    rows_alt: list[SkeletonRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def row_sum(self) -> Decimal:
        return sum((r.max_score for r in self.rows), Decimal(0))

    def ledger_sum(self) -> Decimal:
        return sum((e.max_score for e in self.ledger), Decimal(0))

    def target_total(self) -> Decimal:
        """评标细则声明的目标总分：台账优先（含表外报价段），无台账退行合计。"""
        return self.ledger_sum() if self.ledger else self.row_sum()

    def groups_consistent(self) -> bool:
        """每个带组分值的组：子行合计 == 组分值（只报状态，不改数）。"""
        sums: dict[str, Decimal] = {}
        totals: dict[str, Decimal] = {}
        for r in self.rows:
            sums[r.group] = sums.get(r.group, Decimal(0)) + r.max_score
            if r.group_total is not None:
                totals[r.group] = r.group_total
        return all(sums[g] == t for g, t in totals.items())

    def closed(self) -> bool:
        """行合计 + 表外台账 == 台账合计（报价段只在台账时成立）。"""
        if not self.rows:
            return False
        in_table = self.row_sum()
        ledger = self.ledger_sum()
        if not self.ledger:
            return self.groups_consistent()
        return ledger >= in_table and self.groups_consistent()


# ---------------------------------------------------------------------------
# 列头识别
# ---------------------------------------------------------------------------

_RE_HEADER_FACTOR = re.compile(r"评审因素|评分因素|评审项目")
_RE_HEADER_SCORE_COL = re.compile(r"分值|评分分值|分\s*值")
# 「评审细则」是真实招标文件高频变体（二维码平台运维项目实证：p93 表头即评审细则，
# 缺该变体 → 整个骨架 None → 回退纯 LLM，12 分被误分成 match）
# P0-B 扩展：语料 21 份实证末列三词混用（评标细则/评分细则/评审细则），全部同权
_RE_HEADER_DETAIL = re.compile(r"评标细则|评分细则|评审标准|评分标准|评审细则|评分要点")
_RE_HEADER_SEQ = re.compile(r"序\s*号")
# 主/客观列 5 种形态（语料实证）：主/客观分（新代标准）｜主/客 观分（换行断裂，
# _cell 去空白后同标准形）｜主/客分（一体化维保商务表）｜主/客/观分（数据库）
# ｜打分方式（老代烟丝资信表，行值为"客观分"）
_RE_HEADER_SUBJ = re.compile(r"主\s*/?\s*客(?:\s*/?\s*观)?\s*分?$|主观分|客观分|打分方式")
# 双分值/主客混排行：同一分值 cell 内"客观0-1 主观0-2"（重招标段三/烟丝/智慧实证）。
# 取区间**上界**（0-1 → 1），与 _parse_score_cell 的上界语义一致
_RE_SPLIT_OBJ = re.compile(
    r"客观\s*[^0-9]{0,4}(?:(\d+(?:\.\d+)?)\s*[-–—~～至]\s*)?(\d+(?:\.\d+)?)"
)
_RE_SPLIT_SUBJ = re.compile(
    r"主观\s*[^0-9]{0,4}(?:(\d+(?:\.\d+)?)\s*[-–—~～至]\s*)?(\d+(?:\.\d+)?)"
)
# 占位符/空条款行（薄片"前*名"、宁波"未达到__%"类）——解析时跳过防误启用。
# 只认无歧义的占位形态（双下划线/序数星号），不拦"待定"等可能出现在真实细则中的词
_RE_PLACEHOLDER_ROW = re.compile(r"_{2,}|(?:前|排序前)\s*\*")

_RE_SCORE_CELL = re.compile(
    r"^\s*(?:0\s*[-–—~～至]\s*)?(\d+(?:\.\d+)?)\s*(?:分)?\s*$"
)
_RE_GROUP_TOTAL = re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*分?\s*[）)]")
_RE_SUB_IDX = re.compile(r"^[（(](\d{1,2})[）)]")
_RE_MATERIAL = re.compile(r"^【\s*证明材料\s*】")
# 部分组名：长前缀优先（「商务报价部分（55 分）」先于「商务部分」——
# 二维码项目 p94 实证，旧交替 (商务|技术|价格|报价)部分 漏配复合组名）
_PART_GROUP = r"(商务报价|商务技术|商务|技术|价格|报价)"
# 表外分值声明："3、报价部分（50 分）" / "（一）商务部分（9 分）"
_RE_LEDGER = re.compile(
    r"(?:^|\n)\s*[(（]?[一二三四五六七八九十\d]+[)）、.．]\s*"
    + _PART_GROUP + r"部分\s*[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]"
)
# 章节标题关键词
_RE_SECTION_TITLE = re.compile(r"评标细则|评分细则|评审细则")
_RE_SECTION_ANY = re.compile(
    r"评标细则|评分细则|评审细则|评标办法|评分办法|评分标准|报价部分|基准价"
)


def _cell(text: str | None) -> str:
    """表格 cell 归一：去全部空白（find_tables 会把视觉行拆成 \\n）。"""
    return re.sub(r"[ \t\r\n]+", "", text or "")


def _cell_soft(text: str | None) -> str:
    """折叠空白留单空格（细则正文用）。"""
    return re.sub(r"[ \t\r\n]+", " ", (text or "")).strip()


def _parse_score_cell(text: str) -> Decimal | None:
    """分值 cell → 上界："0-2"→2、"2 分"→2、"（4 分）"→4；空/表头 → None。"""
    t = _cell(text)
    if not t:
        return None
    m = _RE_SCORE_CELL.match(t)
    if m:
        return Decimal(m.group(1))
    return None


def _parse_group_total(text: str) -> Decimal | None:
    m = _RE_GROUP_TOTAL.search(_cell(text))
    return Decimal(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 列头 → 列下标
# ---------------------------------------------------------------------------

def _map_columns(header: list[str]) -> dict[str, int] | None:
    """识别列头行 → {seq, factor, subj, score, detail} 下标。缺 score/detail → None。

    P0-B：分值与细则列**独立判定**——合并表头（"分值/评分标准"同格）同时占
    score+detail 两个角色，旧 elif 链会让这类表整张丢失。
    """
    cols: dict[str, int] = {}
    for i, raw in enumerate(header):
        t = _cell(raw)
        if not t:
            continue
        if "seq" not in cols and _RE_HEADER_SEQ.match(t):
            cols["seq"] = i
            continue
        if "factor" not in cols and _RE_HEADER_FACTOR.search(t):
            cols["factor"] = i
            continue
        if "subj" not in cols and _RE_HEADER_SUBJ.search(t):
            cols["subj"] = i
            continue
        if "score" not in cols and _RE_HEADER_SCORE_COL.search(t):
            cols["score"] = i
        if "detail" not in cols and _RE_HEADER_DETAIL.search(t):
            cols["detail"] = i
    if "score" in cols and "detail" in cols and ("factor" in cols or "seq" in cols):
        # 必须有序号或评审因素列才承认是评分表——「档次|分值区间|评分标准」档位表
        # 没有这两列，其"0-N分"档行会被 _RE_SCORE_CELL 当成计分行（样例项目实证：
        # 差档行 0-5/0-3/0-2 → 垃圾行 5/3/2 → reconcile 据此改分冻结失败）。
        # 档位结构由 LLM 抽取 levels 承担，骨架只产计分行。
        return cols
    return None


# ---------------------------------------------------------------------------
# 表格 → 骨架行
# ---------------------------------------------------------------------------

def _is_material_row(row: list[str]) -> bool:
    texts = [t for t in (_cell(c) for c in row) if t]
    return len(texts) == 1 and bool(_RE_MATERIAL.match(texts[0]))


def _row_cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx] or ""


@dataclass
class _ParseState:
    group: str = ""
    group_total: Decimal | None = None
    subjectivity: str | None = None      # 组级主/客观（子行空白时继承）
    cols: dict[str, int] | None = None   # 跨表/跨页继承（续表无表头）
    rows: list[SkeletonRow] = field(default_factory=list)
    segments: list[list[SkeletonRow]] = field(default_factory=list)  # P0-B 标段分设
    header_sig: str | None = None        # 首个列头行签名（重复出现=疑似新家族）
    pending_break: bool = False          # 重复列头已见，等首个因素行验证
    warnings: list[str] = field(default_factory=list)


def _split_pending_family(st: _ParseState, factor: str) -> bool:
    """重复列头后的首个因素行：组名与当前家族重复 → 真正分段（标段分设）。

    商务表/技术表本来共用同款列头（QR 项目实证），不能见重复列头就分段；
    重招实证是后一标段**重放同名因素**（资信/技术），以因素名复现为分段信号。
    """
    if not st.pending_break:
        return False
    st.pending_break = False
    name = _RE_GROUP_TOTAL.sub("", _cell(factor)).strip() or _cell(factor)
    if name and any(r.group == name for r in st.rows):
        st.segments.append(st.rows)
        st.rows = []
        st.group = ""
        st.group_total = None
        st.subjectivity = None
        return True
    return False


def _feed_table(rows: list[list[str | None]], page_no: int, st: _ParseState) -> None:
    """把一张表的 rows 喂进解析状态（状态跨表/跨页共享以支持续表与跨页续行）。"""
    for raw in rows:
        row = [("" if c is None else c) for c in raw]
        mapped = _map_columns(row)
        if mapped is not None:
            # P0-B 标段分设检测：同款列头**再次整行出现**只挂起疑似标记——
            # 商务表/技术表本来就共用列头（QR 实证），须等首个因素行的组名
            # 与当前家族重复才真正分段（重招标段一二/三实证，见 _split_pending_family）
            sig = "|".join(_cell(c) for c in row)
            if st.header_sig is None:
                st.header_sig = sig
            elif sig == st.header_sig and st.rows:
                st.pending_break = True
            st.cols = mapped
            continue
        if st.cols is None:
            continue  # 尚未见到列头（或列头不符）的表，跳过
        cols = st.cols
        # ① 【证明材料】全宽行 → 挂上一计分行
        if _is_material_row(row):
            mat = next((_cell_soft(c) for c in row if _cell(c)), "")
            if st.rows and mat:
                prev = st.rows[-1]
                prev.required_evidence = (
                    f"{prev.required_evidence}\n{mat}".strip()
                    if prev.required_evidence else mat
                )
            continue
        factor = _cell_soft(_row_cell(row, cols.get("factor")))
        subj_raw = _cell(_row_cell(row, cols.get("subj")))
        score_raw = _row_cell(row, cols.get("score"))
        score = _parse_score_cell(score_raw)
        detail = _cell_soft(_row_cell(row, cols.get("detail")))

        # P0-B 占位符/空条款行防护（"前*名"未填、"未达到__%"空条款）——跳过防误启用
        if _RE_PLACEHOLDER_ROW.search(_cell(factor)) or _RE_PLACEHOLDER_ROW.search(_cell(detail)):
            st.warnings.append(
                f"p{page_no} 占位符/空条款行已跳过：{(factor or detail)[:40]}"
            )
            continue

        # P0-B 双分值/主客混排行：分值 cell 内"客观 0-1 + 主观 0-2" → 拆两行
        if score is None and detail:
            cell_t = _cell(score_raw)
            m_obj = _RE_SPLIT_OBJ.search(cell_t)
            m_subj = _RE_SPLIT_SUBJ.search(cell_t)
            if m_obj and m_subj:
                st.rows.append(SkeletonRow(
                    group=st.group, group_total=st.group_total, sub_idx=None,
                    subjectivity="objective", max_score=Decimal(m_obj.group(2)),
                    text=detail, page_no=page_no,
                ))
                st.rows.append(SkeletonRow(
                    group=st.group, group_total=st.group_total, sub_idx=None,
                    subjectivity="subjective", max_score=Decimal(m_subj.group(2)),
                    text=detail, page_no=page_no,
                ))
                continue

        if factor:
            # 重复列头后的首个因素行：组名复现 → 分段（标段分设）
            _split_pending_family(st, factor)
            # 新评审因素（组）：组名 + "（N 分）"。组名去全部空白——
            # PDF 视觉换行会把词拆进多个 span（"投标人资 质实力"）。
            st.group = _RE_GROUP_TOTAL.sub("", _cell(factor)).strip() or _cell(factor)
            st.group_total = _parse_group_total(factor)
        if subj_raw:
            st.subjectivity = ("subjective" if "主观" in subj_raw
                               else "objective" if "客观" in subj_raw else st.subjectivity)

        if score is not None:
            subj = ("subjective" if "主观" in subj_raw
                    else "objective" if "客观" in subj_raw
                    else st.subjectivity)  # 子行主客观列空白 → 继承组级
            m_idx = _RE_SUB_IDX.match(detail)
            st.rows.append(SkeletonRow(
                group=st.group, group_total=st.group_total,
                sub_idx=int(m_idx.group(1)) if m_idx else None,
                subjectivity=subj, max_score=score,
                text=detail, page_no=page_no,
            ))
        elif detail:
            # 续行：分值 cell 空 + 细则非空 → 直接并入上一计分行（同 cell 断片）
            if st.rows and not _map_columns(row):
                prev = st.rows[-1]
                prev.text = f"{prev.text}{detail}"


# ---------------------------------------------------------------------------
# 表外正文段落台账
# ---------------------------------------------------------------------------

def _feed_ledger(pages_by_no: dict[int, str], ledger: list[LedgerEntry]) -> None:
    best: dict[tuple[str, str], LedgerEntry] = {}
    for pno in sorted(pages_by_no):
        text = pages_by_no[pno]
        for m in _RE_LEDGER.finditer(text):
            label, num = m.group(1), m.group(2)
            start = m.start()
            nxt = re.search(
                r"\n\s*[(（]?[一二三四五六七八九十\d]+[)）、.．]\s*(?:" + _PART_GROUP + r")部分",
                text[m.end():])
            seg = text[start: m.end() + (nxt.start() if nxt else 0)]
            entry = LedgerEntry(
                label=f"{label}部分", max_score=Decimal(num),
                text=_cell_soft(seg), page_no=pno,
            )
            # 同声明多页出现（目录/前文摘要 vs 评标细则正文）→ 留正文（更长者）
            key = (entry.label, num)
            if key not in best or len(entry.text) > len(best[key].text):
                best[key] = entry
    ledger.extend(best[k] for k in sorted(best, key=lambda k: best[k].page_no))


# ---------------------------------------------------------------------------
# 章节定位
# ---------------------------------------------------------------------------

def _page_has_table_header(tables: list) -> bool:
    for rows in tables:
        if rows and _map_columns([("" if c is None else c) for c in rows[0]]):
            return True
    return False


def locate_scoring_section(pages: list[tuple[int, str]],
                           tables_by_page: dict[int, list] | None = None,
                           ) -> tuple[int, int] | None:
    """定位评标细则章节页区间（含表外报价段）。

    优先锚定「含标准列头表」的页；无表可查时退「评标细则/评分细则」标题页
    （取最后一次出现——目录靠前、正文靠后）。锚定后向后延展：后续页只要还有
    表/报价段/分值声明就继续纳入（续表与表外报价段都在正文尾部）。
    """
    if tables_by_page:
        anchors = [pno for pno, tabs in tables_by_page.items()
                   if _page_has_table_header(tabs)]
    else:
        anchors = []
    if not anchors:
        anchors = [pno for pno, t in pages if _RE_SECTION_TITLE.search(t or "")]
    if not anchors:
        return None
    start = min(anchors)
    page_nos = {pno for pno, _ in pages}
    end = max(anchors)
    # 向前扩窗：章节标题/首条分值声明常在**首个含列头表的页之前一页**
    # （二维码项目实证：表在 p93，但「五、评标细则」标题与「（一）商务部分
    # （9 分）」声明都在 p92——旧窗口从 p93 起算，商务 9 分声明漏台账）。
    # 最多向前 3 页（防目录页把窗口拉爆；p91 无标记即停）。
    pages_dict = dict(pages)
    back = 0
    probe = start - 1
    while probe in page_nos and back < 3:
        text = pages_dict[probe] or ""
        if _RE_SECTION_TITLE.search(text) or _RE_LEDGER.search(text):
            start = probe
            back += 1
            probe -= 1
        else:
            break
    # 向后延展到含评标内容的页（续表 / "报价部分（50 分）"）
    probe = end + 1
    while probe in page_nos:
        text = pages_dict[probe] or ""
        tabs = (tables_by_page or {}).get(probe, [])
        if tabs or _RE_SECTION_ANY.search(text):
            end = probe
            probe += 1
        else:
            break
    return (start, end)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_skeleton(tables_by_page: dict[int, list],
                   pages_by_no: dict[int, str]) -> ScoreSkeleton | None:
    """纯函数入口：表 rows + 页文本 → ScoreSkeleton（无有效计分行 → None）。

    P0-B：检测到多套评分表家族（同款列头重复出现，重招按标段分设实证）时，
    首家族为主行集，其余隔离进 rows_alt 并产出告警——禁止静默合并或丢弃。
    """
    st = _ParseState()
    for pno in sorted(tables_by_page):
        for rows in tables_by_page[pno]:
            _feed_table(rows, pno, st)
    if not st.rows and not st.segments:
        return None
    segments = st.segments + ([st.rows] if st.rows else [])
    primary = segments[0]
    if not primary:
        return None
    rows_alt: list[SkeletonRow] = [r for seg in segments[1:] for r in seg]
    warnings = list(st.warnings)
    if rows_alt:
        warnings.append(
            f"ambiguous_dual_scoring：检测到 {len(segments)} 套评分表家族"
            "（可能按标段分设，如重招文件），主行集取先出现者，其余隔离到 rows_alt"
            "供人工按标段选用；请勿视为冲突直接丢弃"
        )
    ledger: list[LedgerEntry] = []
    _feed_ledger(pages_by_no, ledger)
    # 多产品/多标段：同名"报价部分"声明出现不同分值 → 提示（加权小表可能另有其文）
    by_label: dict[str, set[str]] = {}
    for e in ledger:
        by_label.setdefault(e.label, set()).add(str(e.max_score))
    for label, scores in by_label.items():
        if len(scores) > 1:
            warnings.append(
                f"multi_lot_ledger：表外「{label}」出现多个分值声明（{'/'.join(sorted(scores))}），"
                "疑似按标段分设，对账合计前需人工确认口径"
            )
    return ScoreSkeleton(rows=primary, ledger=ledger, rows_alt=rows_alt, warnings=warnings)


def extract_score_skeleton(pdf_path: str,
                           pages: list[tuple[int, str]] | None = None,
                           ) -> ScoreSkeleton | None:
    """PDF 入口：重开 PDF 跑 find_tables（先例 case_evidence.parse_case_table）。

    pages 不传时对全部页做章节定位。任何异常 → None（回退纯 LLM）。
    """
    try:
        import pymupdf
    except Exception:
        return None
    try:
        doc = pymupdf.open(pdf_path)
    except Exception:
        return None
    try:
        if pages is None:
            pages = [(i + 1, p.get_text() or "") for i, p in enumerate(doc)]
        pages_by_no = {pno: t for pno, t in pages}
        # 先全页探表（章节定位需要列头信号），再按 span 归拢
        tables_by_page: dict[int, list] = {}
        for pno in sorted(pages_by_no):
            try:
                tabs = doc[pno - 1].find_tables()
                tables_by_page[pno] = [t.extract() for t in tabs.tables]
            except Exception:
                tables_by_page[pno] = []
        span = locate_scoring_section(pages, tables_by_page)
        if span:
            tables_by_page = {p: t for p, t in tables_by_page.items()
                              if span[0] <= p <= span[1]}
            pages_by_no = {p: t for p, t in pages_by_no.items()
                           if span[0] <= p <= span[1]}
        return parse_skeleton(tables_by_page, pages_by_no)
    except Exception:
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass
