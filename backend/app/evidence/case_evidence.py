"""计数类评分依据抽取（业绩表 → 案例行 → 材料块 → 三态材料）。

**确定性优先**：用标书自带索引「第X页到第Y页」+ 页脚印刷页码定位证据段；用
PyMuPDF `find_tables()` 还原交错 OCR 表（这是纯文本正则做不到的，2026-10-21 实测
4/4 家真实标书可正确还原）；按案例名前缀绑定材料页；用强信号判定
合同/发票/验收三态（true=确认有 / false=确认无 / null=页不可读或未提供）。

本模块**不调 LLM**：LLM 只在 `scoring_service` 里对歧义材料做裁决。定位/抽取
失败时返回空/低置信结果，调用方回退到 section-aware 的 LLM 路径。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

log = logging.getLogger(__name__)

# ---- 材料强信号（宁可漏判也不要把合同付款条款里的"发票"字样当发票）----
_RE_CONTRACT_PARTY = re.compile(r"甲方|乙方|发包人|承包人|买受人|出卖人")
_RE_CONTRACT_HEAD = re.compile(r"合同扫描件|项目合同|采购合同|运维合同|服务合同|合同协议书|合同书")
# 发票：只认表单字段级强信号。"增值税专用发票"单独出现是合同付款条款
# （"收到乙方出具的…发票类型增值税专用发票…"）或报价表税率列；"纳税人
# 识别号"单独出现是合同乙方账户信息（先数 p165 实证）——先数全文无一张
# 真发票却靠此误判 invoice=True（2026-10-23 run 48-51 实证）。
_RE_INVOICE_STRONG = re.compile(
    r"发票代码|发票号码|开票日期|价税合计|校验码|发票专用章|购买方[：:]"
)


def _is_invoice_page(text: str) -> bool:
    """真发票页判定：表单字段强信号，或"增值税专用发票"+购买方/销售方字段。"""
    if _RE_INVOICE_STRONG.search(text):
        return True
    return "增值税专用发票" in text and bool(
        re.search(r"购买方|销售方|纳税人识别号|发票专用章", text)
    )


_RE_INVOICE = _RE_INVOICE_STRONG  # 兼容旧引用
_RE_ACCEPTANCE = re.compile(
    r"验收报告|验收结论|验收意见|验收日期|验收合格|验收单|业主证明|用户证明|使用证明|项目评价"
)
_RE_MATERIAL_HEADING = re.compile(r"发票|验收|业主证明|用户证明|合同|证书|扫描件")

_RE_DATE = re.compile(r"(\d{4})\s*[年.\-/]\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})\s*日?")
# 索引行：业绩案例 …… 第118页到第167页 / 第45页到第71页 …… 业绩案例（7.10 实证：
# 真实行文是"第45 页到第71 页 2.8 投标人类似项目业绩案例"——**页码在前、业绩在后**，
# 旧正则只认"业绩在前"→不命中，随后被无邻近约束的 PAREN 变体抢配到同页其他模块的
# （112-127）页 → 段错位到技术方案章节）。故双向支持；分组 (1,2)=(前,后)。
_RE_INDEX_RANGE = re.compile(
    r"(?:业绩[^\n]{0,40}?第?\s*(\d{1,4})\s*页?\s*(?:到|至|—|－|-|~|～)\s*第?\s*(\d{1,4})\s*页"
    r"|第?\s*(\d{1,4})\s*页?\s*(?:到|至|—|－|-|~|～)\s*第?\s*(\d{1,4})\s*页[^\n]{0,40}?业绩)"
)
# 括号变体**必须**限定在含"业绩"的行内匹配（同页其他模块的（X-Y）页不得抢配）
_RE_INDEX_RANGE_PAREN = re.compile(r"[（(]\s*(\d{1,4})\s*[-—~～至到]\s*(\d{1,4})\s*[）)]\s*页")
# 页脚印刷页码： ``- 118 -`` / ``第16 页`` / 裸数字行
_RE_FOOTER_DASH = re.compile(r"^[-—－]\s*(\d{1,4})\s*[-—－]$")
_RE_FOOTER_CN = re.compile(r"^第\s*(\d{1,4})\s*页$")
_RE_FOOTER_BARE = re.compile(r"^(\d{1,4})$")


@dataclass
class PageRec:
    page_no: int
    text: str
    source: str = "text"


@dataclass
class SectionGuess:
    start: int = 0
    end: int = 0
    method: str = "none"  # index_offset | index_offset_near | heading_window | none
    confidence: float = 0.0
    offset: int | None = None

    @property
    def found(self) -> bool:
        return self.start > 0 and self.end >= self.start


@dataclass
class CaseRow:
    index: int
    name: str
    amount_yuan: Decimal | None
    sign_date: str | None
    page: int | None
    raw: str = ""


@dataclass
class CaseBlock:
    row: CaseRow
    pages: list[int] = field(default_factory=list)  # 本案证据页区间（含材料页）
    status: dict[str, bool | None] = field(default_factory=dict)  # contract/invoice/acceptance
    pages_by_material: dict[str, list[int]] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    ambiguous: bool = False


# ---------------------------------------------------------------------------
# 页图片占比（材料页可见性判定）
# ---------------------------------------------------------------------------
def page_image_ratios(pdf_path: str) -> dict[int, float]:
    """每页图片块覆盖占比（图片面积 / 页面积）。失败返回 {}。"""
    try:
        import pymupdf as fitz
    except Exception:
        return {}
    out: dict[int, float] = {}
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return {}
    try:
        for i in range(doc.page_count):
            page = doc[i]
            area = max(1.0, float(page.rect.width * page.rect.height))
            img_area = 0.0
            try:
                blocks = page.get_text("dict").get("blocks", [])
            except Exception:
                blocks = []
            for b in blocks:
                if b.get("type") == 1:
                    x0, y0, x1, y1 = b["bbox"]
                    img_area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
            out[i + 1] = min(1.0, img_area / area)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# 金额
# ---------------------------------------------------------------------------
def parse_amount_yuan(text: str | None, header_unit_yuan: int = 1) -> Decimal | None:
    """金额串 → 元（Decimal）。支持 ¥/￥/RMB、万/万元/元、OCR 拆字（RMB9 3.6 万元）。

    ``header_unit_yuan``：单元格无单位时按表头单位换算（表头"合同金额（万元）"→10000）。
    """
    if not text:
        return None
    t = str(text).replace(" ", "").replace("　", "").replace(",", "").replace("，", "")
    t = re.sub(r"(?i)rmb|¥|￥", "", t)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万元|万|元)?", t)
    if not m:
        return None
    try:
        val = Decimal(m.group(1))
    except Exception:
        return None
    unit = m.group(2)
    if unit in ("万", "万元"):
        val *= Decimal(10000)
    elif unit == "元":
        pass
    else:
        val *= Decimal(int(header_unit_yuan or 1))
    return val if val > 0 else None


def _looks_amount(cell: str) -> bool:
    if not cell:
        return False
    if re.search(r"[¥￥]|\bRMB\b|万元|万|元", cell, re.IGNORECASE):
        return True
    # 纯数字/千分位且位数够（≥5 位数，排除序号/年份）
    return bool(re.fullmatch(r"[\d,，]{5,}(?:\.\d+)?", cell.strip()))


def _cell_date(cell: str) -> str | None:
    m = _RE_DATE.search((cell or "").replace(" ", ""))
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def _header_cols(cells: list[str]) -> dict[str, int | None]:
    """表头 → 列序号（项目名称/合同金额/签订时间/序号）。"""
    out: dict[str, int | None] = {"name": None, "amount": None, "date": None, "seq": None}
    for i, c in enumerate(cells):
        if out["name"] is None and "项目名称" in c:
            out["name"] = i
        if out["amount"] is None and ("金额" in c):
            out["amount"] = i
        if out["date"] is None and ("签订" in c or "时间" in c or "日期" in c):
            out["date"] = i
        if out["seq"] is None and "序号" in c:
            out["seq"] = i
    return out


# ---------------------------------------------------------------------------
# 章节定位
# ---------------------------------------------------------------------------
def _printed_page(text: str) -> int | None:
    """从页文本的最后 3 个非空行里找印刷页码。"""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    for ln in reversed(lines[-3:]):
        if len(ln) > 12:
            continue
        m = _RE_FOOTER_DASH.match(ln) or _RE_FOOTER_CN.match(ln) or _RE_FOOTER_BARE.match(ln)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                continue
    return None


def _has_case_table_header(pages: list[PageRec], center: int, window: int) -> bool:
    lo, hi = center - window, center + window
    for p in pages:
        if lo <= p.page_no <= hi:
            t = p.text or ""
            if "项目名称" in t and ("合同金额" in t or "金额" in t):
                return True
    return False


def _find_case_table_header_page(pages: list[PageRec]) -> int | None:
    for p in pages:
        t = p.text or ""
        if "项目名称" in t and ("合同金额" in t or "金额" in t) and (
            "业绩" in t or "案例" in t or "序号" in t
        ):
            return p.page_no
    # 退一步：只要有 项目名称+金额 表头
    for p in pages:
        t = p.text or ""
        if "项目名称" in t and ("合同金额" in t or "金额" in t):
            return p.page_no
    return None


_BOUNDARY = re.compile(r"保密承诺书|项目团队|技术方案|技术服务方案|服务方案|售后服务|培训服务")
# 章节边界命中必须落在**页首标题区**：扫描合同条款正文天然含"服务方案/技术方案"等词
# （7.10 实证：云锦案例#1 合同扫描页正文「经甲方审核通过的项目运维服务方案一份」@807
#  → 段被截在 p59，其后 60 余页材料全部丢失）。真正的下章节标题（p65「乙方项目团队」
#  @40）总在页首区。300 字符 = 通用标题区窗口，非项目拟合。
_BOUNDARY_HEADING_ZONE = 300


def _extend_until_boundary(pages: list[PageRec], start: int, max_pages: int) -> int:
    ordered = sorted(p.page_no for p in pages if p.page_no >= start)
    count = 0
    end = start
    for pno in ordered:
        p = next((x for x in pages if x.page_no == pno), None)
        if p is None:
            continue
        text = p.text or ""
        # 起始页之后遇到章节边界（且命中在页首标题区）且有较实质正文 → 截断
        m = _BOUNDARY.search(text)
        if (
            pno > start
            and m
            and m.start() < _BOUNDARY_HEADING_ZONE
            and len(text) > 200
        ):
            break
        end = pno
        count += 1
        if count >= max_pages:
            break
    return end


def locate_section(
    pages: list[PageRec], criterion_name: str = "", required_artifacts: str = ""
) -> SectionGuess:
    """定位业绩案例证据段（物理页码区间）。

    主判据：索引页「业绩…第X页到第Y页」+ 同页页脚印刷页码求 offset。
    回退：找表头页（项目名称+合同金额）后向前扩展到章节边界。
    """
    # 1) 索引 + 页脚 offset
    for p in pages:
        t = p.text or ""
        if "业绩" not in t:
            continue
        if not ("索引" in t or "详细评审" in t or "评审索引" in t):
            continue
        m = _RE_INDEX_RANGE.search(t)
        if not m:
            # 括号变体只在"业绩行"内匹配——同页其他模块的（X-Y）页不得抢配
            for ln in t.splitlines():
                if "业绩" in ln:
                    m = _RE_INDEX_RANGE_PAREN.search(ln)
                    if m:
                        break
        if not m:
            continue
        try:
            # 双向主正则：前向（业绩…范围）用组 1/2，反向（范围…业绩）用组 3/4；
            # PAREN 变体只有组 1/2（后两组为 None）。注意 re.Match.group 无默认值
            # 参数——m.group(3, None) 会被当成请求两个分组而抛 "no such group"。
            g1, g2, g3, g4 = (m.groups() + (None, None, None, None))[:4]
            printed_start = int(g1 or g3)
            printed_end = int(g2 or g4)
        except Exception:
            continue
        printed_here = _printed_page(t)
        offset = (p.page_no - printed_here) if printed_here else 0
        start, end = printed_start + offset, printed_end + offset
        if _has_case_table_header(pages, start, 3):
            return SectionGuess(start, end, "index_offset", 0.95, offset)
        for d in (-4, -3, -2, -1, 1, 2, 3, 4, 5, -5):
            if _has_case_table_header(pages, start + d, 0):
                return SectionGuess(start + d, end + d, "index_offset_near", 0.8, offset)
        # 表头找不到也接受索引区间（低置信），交给后续 parse 判断
        return SectionGuess(start, end, "index_offset", 0.5, offset)

    # 2) 表头页 + 前向扩展
    head = _find_case_table_header_page(pages)
    if head is not None:
        end = _extend_until_boundary(pages, head, max_pages=120)
        return SectionGuess(head, max(head, end), "heading_window", 0.6, None)
    return SectionGuess(0, 0, "none", 0.0, None)


# ---------------------------------------------------------------------------
# 业绩表解析（PyMuPDF find_tables）
# ---------------------------------------------------------------------------
def _norm_cell(cell: str) -> str:
    return re.sub(r"\s+", " ", (cell or "").replace("\n", " ")).strip()


def _is_header_row(cells: list[str]) -> bool:
    joined = "".join(cells)
    return "项目名称" in joined and ("金额" in joined) and ("序号" in joined or "委托" in joined or "时间" in joined)


def _header_unit(cells: list[str]) -> int:
    for c in cells:
        if "金额" in c and ("万元" in c):
            return 10000
    return 1


def parse_case_table(
    pdf_path: str, start: int, end: int, max_pages: int = 4
) -> list[CaseRow]:
    """从证据段前几页解析业绩表行。无表返回 []。"""
    if start <= 0 or end < start:
        return []
    try:
        import pymupdf as fitz
    except Exception:
        try:
            import fitz  # type: ignore
        except Exception:
            log.warning("PyMuPDF 不可用，跳过业绩表解析")
            return []
    rows_out: list[CaseRow] = []
    last = min(end, start + max(1, max_pages) - 1)
    header_cols: dict[str, int | None] | None = None
    unit_yuan = 1
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        log.warning("打开 PDF 失败 %s：%s", pdf_path, exc)
        return []
    try:
        for pno in range(start, last + 1):
            if pno < 1 or pno > doc.page_count:
                continue
            page = doc[pno - 1]
            try:
                tables = page.find_tables()
            except Exception:
                tables = None
            for table in tables or []:
                try:
                    data = table.extract()
                except Exception:
                    continue
                if not data:
                    continue
                for raw_row in data:
                    cells = [_norm_cell(c) for c in raw_row]
                    if not any(cells):
                        continue
                    if _is_header_row(cells):
                        header_cols = _header_cols(cells)
                        unit_yuan = _header_unit(cells)
                        continue
                    if header_cols is None:
                        # 没有表头时不猜测，避免把索引表当业绩表
                        continue

                    def at(key: str) -> str:
                        i = header_cols.get(key) if header_cols else None
                        return cells[i] if isinstance(i, int) and 0 <= i < len(cells) else ""

                    seq = at("seq")
                    name = at("name")
                    amount_raw = at("amount")
                    date_raw = at("date")
                    amount_val = parse_amount_yuan(amount_raw, unit_yuan) if amount_raw else None
                    date_val = _cell_date(date_raw) if date_raw else None
                    # 表头无项目名称列 → 回退"最长文本"启发
                    if header_cols.get("name") is None:
                        nonempty = [c for c in cells if c]
                        for c in nonempty:
                            d = _cell_date(c)
                            if d and date_val is None:
                                date_val = d
                                continue
                            if amount_val is None and _looks_amount(c) and not _cell_date(c):
                                amount_val = parse_amount_yuan(c, unit_yuan)
                        name = max(
                            (c for c in nonempty if not _looks_amount(c) and not _cell_date(c)),
                            key=len,
                            default="",
                        )
                    # 续行合并：无名称，或无序号且名称短 → 并入上一行
                    if rows_out and (not name or (not seq and len(name) < 10)):
                        prev = rows_out[-1]
                        if name and len(prev.name) < 200:
                            prev.name = (prev.name + name)[:200]
                        if prev.amount_yuan is None and amount_val is not None:
                            prev.amount_yuan = amount_val
                        if prev.sign_date is None and date_val is not None:
                            prev.sign_date = date_val
                        continue
                    if not name:
                        continue
                    rows_out.append(
                        CaseRow(
                            index=len(rows_out) + 1,
                            name=name,
                            amount_yuan=amount_val,
                            sign_date=date_val,
                            page=pno,
                            raw=" | ".join(c for c in cells if c),
                        )
                    )
    finally:
        try:
            doc.close()
        except Exception:
            pass
    # 去掉明显不是案例的行（如只有表头/合计/序号）
    cleaned = [
        r
        for r in rows_out
        if r.name and not re.fullmatch(r"(合计|小计|序号|总计)\s*", r.name)
        and not re.fullmatch(r"\d{1,3}", r.name)
    ]
    for i, r in enumerate(cleaned, start=1):
        r.index = i
    return cleaned


# ---------------------------------------------------------------------------
# 案例 → 材料页绑定
# ---------------------------------------------------------------------------
def _norm_text(text: str) -> str:
    from app.parsing.normalize import normalize_no_ws

    return normalize_no_ws(text or "")


def _name_prefixes(name: str, lengths: tuple[int, ...] | None = None) -> list[str]:
    """案例名归一化前缀（长→短）。默认尾长接 `config.case_name_prefix_min()`
    （默认 8），与 scoring_service `_sanitize_case._case_name_hit` 前缀梯同源
    （C2：消除两处硬编码漂移）。"""
    if lengths is None:
        from app import config

        lengths = (12, 10, config.case_name_prefix_min())
    n = _norm_text(name)
    out: list[str] = []
    for L in lengths:
        if len(n) >= L:
            out.append(n[:L])
    if n and not out:
        out.append(n)
    return out


def link_case_blocks(
    rows: list[CaseRow], pages: list[PageRec], section: SectionGuess
) -> list[CaseBlock]:
    """按案例名前缀定位每个案例的证据块起始页，块区间 = 到下一案例起始页前。"""
    if not rows:
        return []
    ordered_pages = sorted((p for p in pages if p.page_no >= section.start), key=lambda p: p.page_no)
    # 案例名会出现在业绩表页上——块起点必须从**表页之后**开始找，否则每个案例都
    # 绑到表页本身、材料页全被漏掉（2026-10-21 实证：doc2 #1-4 全绑 126-126）。
    table_end = max((r.page or 0) for r in rows) if rows else section.start
    search_from = max(section.start + 1, table_end + 1)
    starts: dict[int, int] = {}
    for r in rows:
        prefixes = _name_prefixes(r.name)
        if not prefixes:
            continue
        for p in ordered_pages:
            if p.page_no < search_from:
                continue
            if p.page_no > section.end:
                break
            pn = _norm_text(p.text)
            if any(pref and pref in pn for pref in prefixes):
                starts[r.index] = p.page_no
                break
    known = sorted(starts.values())
    blocks: list[CaseBlock] = []
    for r in rows:
        st = starts.get(r.index)
        if st is None:
            blocks.append(CaseBlock(row=r, ambiguous=True))
            continue
        nxt = next((s for s in known if s > st), section.end + 1)
        block_pages = [p.page_no for p in ordered_pages if st <= p.page_no < nxt]
        blocks.append(CaseBlock(row=r, pages=block_pages))
    return blocks


def _low_evidence(text: str, ratio: float) -> bool:
    """低证据页（统一口径）：图占比≥0.10 且文本<800 字符。

    800 = `config.OCR_MIXED_PAGE_MAX_CHARS`（救援选靶与混合页 OCR 触发同口径）。
    旧分类口径用 <200，与选靶的 <800 不一致——200~800 字的「标题+扫描图」页被
    判 False/absent（"确认缺失"）且**永远进不了救援名单**（7.10 派克实证 5 页）。
    """
    return ratio >= 0.10 and len((text or "").strip()) < 800


def classify_materials(
    blocks: list[CaseBlock],
    pages: list[PageRec],
    image_ratios: dict[int, float] | None = None,
) -> None:
    """对每个案例块按强信号判定 contract/invoice/acceptance 三态（就地修改）。

    - 强信号命中且页可读 → True
    - 页在但低证据（短文本 + 图片占比≥0.10，`_low_evidence` 口径）→ None(reason=unreadable)
    - **扫描页无特征 → None(reason=scan_unverified)**：图占比高或 OCR 来源的页
      上找不到票据特征 = 不可见 ≠ 不存在（2026-10-27 训练集测试2 实证：有业主
      证明但发票是扫描件，旧口径判 False「确认缺失」→ 业绩全灭，人工照计）
    - 全部页均为文字层可读且无信号 → False(reason=absent)
    - 案例未绑定页 → None(reason=unlinked, ambiguous)
    """
    by_no = {p.page_no: p for p in pages}
    ratios = image_ratios or {}
    for b in blocks:
        if b.ambiguous or not b.pages:
            for m in ("contract", "invoice", "acceptance"):
                b.status[m] = None
                b.reasons[m] = "unlinked"
            continue
        unreadable_pages = [
            no
            for no in b.pages
            if _low_evidence(
                by_no.get(no).text if by_no.get(no) else "", ratios.get(no, 0.0)
            )
        ]
        # 扫描页（图占比 ≥0.35 或 OCR 来源）：文本层不可信，缺席判定不成立
        scan_pages = [
            no
            for no in b.pages
            if ratios.get(no, 0.0) >= 0.35
            or (by_no.get(no) is not None and (by_no.get(no).source or "text") != "text")
        ]
        for mat, pat in (
            ("contract", None),
            ("invoice", _RE_INVOICE),
            ("acceptance", _RE_ACCEPTANCE),
        ):
            hit_pages: list[int] = []
            for no in b.pages:
                p = by_no.get(no)
                if p is None:
                    continue
                text = p.text or ""
                if mat == "contract":
                    ok = bool(
                        _RE_CONTRACT_PARTY.search(text)
                        and ("合同" in text or _RE_CONTRACT_HEAD.search(text))
                    ) or bool(_RE_CONTRACT_HEAD.search(text) and len(text) >= 200)
                elif mat == "invoice":
                    ok = _is_invoice_page(text)
                else:
                    ok = bool(pat.search(text)) if pat else False
                if ok:
                    hit_pages.append(no)
            if hit_pages:
                b.status[mat] = True
                b.pages_by_material[mat] = hit_pages[:3]
                b.reasons[mat] = "signal"
            elif unreadable_pages:
                b.status[mat] = None
                b.pages_by_material[mat] = unreadable_pages[:3]
                b.reasons[mat] = "unreadable"
            elif scan_pages:
                # 扫描页无特征：不可见 ≠ 缺失，留 null 走疑问/救援，不判 False
                b.status[mat] = None
                b.pages_by_material[mat] = scan_pages[:3]
                b.reasons[mat] = "scan_unverified"
            else:
                b.status[mat] = False
                b.reasons[mat] = "absent"


def material_ocr_targets(
    pages: list[PageRec],
    image_ratios: dict[int, float],
    section: SectionGuess,
    blocks: list[CaseBlock] | None = None,
    per_block: int = 2,
    max_total: int | None = None,
) -> list[int]:
    """选出需定向 OCR 的材料页。

    有案例块时**只救援"材料不可读/扫描待核"的案例块**（reasons ∈ unreadable/
    scan_unverified——scan_unverified 页同样需要 OCR 后才能核验信号），且每块每轮
    最多 `per_block` 页——证据段里短文本+图片页可能几十页，全送 OCR 会打爆服务
    （2026-10-21 doc24 61 页断言实证：远程 OCR 熔断→整本救援失败）。
    块间**轮转分配**（第一轮每块 ≤per_block，预算未用完第二轮追加）——旧实现按
    块顺序截断，排在后面的案例块整批饿死（run 60 实证）。
    块内排序：材料标题命中 → 图占比降序 → 页码升序（把预算花在最像材料页的页上）。
    无块时回退为证据段内低证据页（限 `max_total` 或 8 页），同排序。
    """
    if not section.found:
        return []
    by_no = {p.page_no: p for p in pages}

    def unreadable(no: int) -> bool:
        p = by_no.get(no)
        if p is None or (p.source or "") != "text":
            return False
        return _low_evidence(p.text or "", image_ratios.get(no, 0.0))

    def _rank(no: int) -> tuple:
        p = by_no.get(no)
        t = ((p.text if p else "") or "")
        return (
            0 if _RE_MATERIAL_HEADING.search(t) else 1,
            -image_ratios.get(no, 0.0),
            no,
        )

    out: list[int] = []
    seen: set[int] = set()
    if blocks:
        needing: list[list[int]] = []
        for b in blocks:
            needs = any(
                (b.status or {}).get(m) is None
                and (b.reasons or {}).get(m) in ("unreadable", "scan_unverified")
                for m in ("contract", "invoice", "acceptance")
            )
            if not needs:
                continue
            cand = [no for no in b.pages if no not in seen and unreadable(no)]
            if cand:
                needing.append(sorted(cand, key=_rank))
        if not needing:
            return []
        cap = max(1, per_block)
        # 第一轮：每块 ≤per_block 页，块间轮转（防顺序偏置）
        for k in range(cap):
            for cand in needing:
                if max_total is not None and len(out) >= max_total:
                    return out
                remaining = [no for no in cand if no not in seen]
                if remaining:
                    out.append(remaining[0])
                    seen.add(remaining[0])
        # 第二轮：仅在有总预算时追加（无预算时 per_block 即上限，防失控扩张）
        if max_total is None:
            return out
        while len(out) < max_total:
            progressed = False
            for cand in needing:
                if max_total is not None and len(out) >= max_total:
                    return out
                remaining = [no for no in cand if no not in seen]
                if remaining:
                    out.append(remaining[0])
                    seen.add(remaining[0])
                    progressed = True
            if not progressed:
                break
        return out
    # 无块兜底：段内低证据页（旧硬编码 8 页 → max_total 驱动）
    limit = max_total if max_total is not None else 8
    cands = [
        p.page_no
        for p in pages
        if section.start <= p.page_no <= section.end and unreadable(p.page_no)
    ]
    return sorted(cands, key=_rank)[: max(0, limit)]


# ---------------------------------------------------------------------------
# LLM 摘要（只送歧义材料所需的最小证据）
# ---------------------------------------------------------------------------
def build_case_digest(
    blocks: list[CaseBlock], pages: list[PageRec], max_chars: int = 12000
) -> str:
    by_no = {p.page_no: p for p in pages}
    parts: list[str] = []
    used = 0
    for b in blocks:
        r = b.row
        head = (
            f"【案例{b.row.index}】{r.name}｜金额={r.amount_yuan or '未知'}"
            f"｜签订={r.sign_date or '未知'}｜表页={r.page}"
        )
        parts.append(head)
        used += len(head)
        # 每材料块取 1-2 页摘要
        for mat, pnos in b.pages_by_material.items():
            for no in pnos[:2]:
                p = by_no.get(no)
                if p is None:
                    continue
                snippet = re.sub(r"\s+", " ", (p.text or "")).strip()[:700]
                if not snippet:
                    continue
                piece = f"  <{mat} 第{no}页> {snippet}"
                if used + len(piece) > max_chars:
                    return "\n".join(parts)
                parts.append(piece)
                used += len(piece)
    return "\n".join(parts)


def summarize_status(blocks: list[CaseBlock]) -> str:
    """供日志/诊断的一行摘要。"""
    out = []
    for b in blocks:
        st = b.status
        out.append(
            f"#{b.row.index}:{b.row.name[:12]} 合{_sym(st.get('contract'))}"
            f"票{_sym(st.get('invoice'))}验{_sym(st.get('acceptance'))}"
        )
    return "；".join(out)


def _sym(v: bool | None) -> str:
    return "✓" if v is True else ("✗" if v is False else "△")
