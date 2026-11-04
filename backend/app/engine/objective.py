"""资格/符合性检查原语与三态判定：纯代码，绝不自动否决。

三态语义：
- pass              → 通过
- recommend_reject  → **建议**否决 + 依据（必须人工确认才生效）
- need_human        → 无法自动判定（human_only / 事实缺失），进入复核队列
- conflict_flagged  → 冲突（大小写金额不一致、分项合计≠总价、声称与证书不符），不选边

「机器看不见」≠「不符合」（2026-10-14 浙江中烟项目实证）：证照/凭证/承诺书多为
扫描件，OCR 文本通道看不见或看不见全，一律不得据此直接否决——document_present
未定位到时返回 unknown + need_human（人工核查原件 / auto 模式交 LLM 带上下文裁决）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app import config
from app.engine.facts import BidFacts
from app.engine.models import PrimitiveResult
from app.parsing.normalize import normalize_no_ws

# 材料名按标点拆词组：「营业执照、法人证书或其他组织登记证明文件的复印件」
# → [营业执照, 法人证书, 其他组织登记证明文件, 复印件]。招标文件该句是「或」列举，
# 任一词组在投标文本/章节标题中命中即视为已提供。过滤 <3 字碎片。
_PHRASE_SPLIT = re.compile(r"[、，,；;（）()：:\s]+")


def _document_phrases(name: str) -> list[str]:
    return [p for p in (s.strip() for s in _PHRASE_SPLIT.split(name or "")) if len(p) >= 3]


def _phrase_hit(phrases: list[str], facts: BidFacts) -> str | None:
    """任一词组（归一化后）命中正文或章节标题 → 返回命中的词组。"""
    text_n = normalize_no_ws(facts.text)
    titles_n = [normalize_no_ws(d) for d in facts.documents]
    for p in phrases:
        p_n = normalize_no_ws(p)
        if not p_n:
            continue
        if p_n in text_n or any(p_n in t for t in titles_n if t):
            return p
    return None


# 否定/禁止标记：承诺类声明的语义核心是「内容词 + 否定语境」
_RE_NEG_MARK = re.compile(r"(?:不|无|禁|杜绝|严禁|禁止|不允许|不得|不再)")
# 声明短语里的非实词前缀（「不得转包」→「转包」）
_RE_MODAL_PREFIX = re.compile(
    r"^(?:不得不|不得|不能|不应|不允许|严禁|禁止|杜绝|进行|违法|违规|发生|出现|不|未|无)+"
)


def _commitment_hit(phrase: str, text: str) -> str | None:
    """承诺/禁止类声明的语义检索（contains_phrase 匹配核）。

    精确子串对措辞变体毫无鲁棒性：「不转包、不分包」检索不到「不进行转包」
    「不得转包或违法分包」——2026-10-26 run 54 实证 c13 7/7 全员误否决。
    语义：① 归一化全串命中（快路径）；② 短语按顿号/逗号拆内容词（剥否定/情态
    前缀），内容词在正文出现且**前 8 字内有否定标记**→ 声明成立（"存在转包行为"
    这类肯定语境不算）。多段短语任一段成立即命中（声明条款通常一句涵盖）。
    """
    if not phrase:
        return None
    text_n = normalize_no_ws(text or "")
    if not text_n:
        return None
    phrase_n = normalize_no_ws(phrase)
    if phrase_n and phrase_n in text_n:
        return phrase
    for part in re.split(r"[、，,；;和及与或/]", phrase):
        core = _RE_MODAL_PREFIX.sub("", normalize_no_ws(part))
        core = re.sub(r"(?:行为|现象|情况|的承诺|承诺)$", "", core)
        if len(core) < 2:
            continue
        for m in re.finditer(re.escape(core), text_n):
            window = text_n[max(0, m.start() - 8): m.start()]
            if _RE_NEG_MARK.search(window):
                return f"{core}（否定语境）"
    return None


def run_check(check_type: str, params: dict, facts: BidFacts) -> PrimitiveResult:
    """单一检查原语。params 来自规则包的 criterion_checks。"""
    if check_type == "human_only":
        return PrimitiveResult("unknown", "该项需人工判定（如密封、签字、盖章）", need_human=True)

    if check_type == "document_present":
        name = str(params.get("document") or "")
        phrases = _document_phrases(name)
        if "保证金" in name:
            if facts.has_guarantee:
                return PrimitiveResult("pass", "已找到投标保证金缴纳凭证")
            hit = _phrase_hit(phrases, facts)
            if hit:
                return PrimitiveResult(
                    "pass", f"已找到投标保证金相关内容（依据「{hit}」；缴纳凭证请人工复核原件）")
            return PrimitiveResult(
                "unknown",
                "未在投标文本中定位到投标保证金缴纳凭证（可能为扫描件或 OCR 未识别），须人工核查原件",
                need_human=True,
            )
        if "投标函" in name or "授权" in name:
            # 按正文判断（章节标题在正文里）；文件名不含章节名，不能拿文件名当依据
            if "投标函" in facts.text:
                return PrimitiveResult("pass", "已找到投标函及授权委托书内容")
            return PrimitiveResult(
                "unknown",
                "未在投标文本中定位到投标函及授权委托书（可能为扫描件或 OCR 未识别），须人工核查原件",
                need_human=True,
            )
        if "资质" in name:
            if facts.has_qualification_cert:
                return PrimitiveResult("pass", "已找到资质证书信息")
            hit = _phrase_hit(phrases, facts)
            if hit:
                return PrimitiveResult(
                    "pass", f"已找到资质证书相关内容（依据「{hit}」）")
            return PrimitiveResult(
                "unknown",
                "未在投标文本中定位到资质证书（可能为扫描件或 OCR 未识别），须人工核查原件",
                need_human=True,
            )
        # 兜底：材料名拆词组，任一命中正文/章节标题即 pass
        hit = _phrase_hit(phrases, facts)
        if hit:
            return PrimitiveResult("pass", f"已找到「{name}」相关内容（依据「{hit}」）")
        return PrimitiveResult(
            "unknown",
            f"未在投标文本中定位到「{name}」（可能为扫描件或 OCR 未识别），须人工核查原件",
            need_human=True,
        )

    if check_type == "value_in_range":
        field = params.get("field") or ""
        if field == "registered_capital":
            value = facts.registered_capital
            if value is None:
                return PrimitiveResult("unknown", "未提取到注册资本（需人工核查营业执照）", need_human=True)
            minimum = Decimal(str(params.get("min") or 0))
            if value >= minimum:
                return PrimitiveResult(
                    "pass", f"注册资本 {_money(value)} ≥ 要求 {_money(minimum)}")
            return PrimitiveResult(
                "fail",
                f"注册资本 {_money(value)} 低于要求 {_money(minimum)}",
            )
        return PrimitiveResult("unknown", f"未知数值字段：{field}", need_human=True)

    if check_type == "count_at_least":
        field = params.get("field") or ""
        minimum = int(params.get("min_count") or 0)
        min_amount = Decimal(str(params.get("min_amount_yuan") or 0))
        if minimum <= 0:
            # 门槛缺失 = 不知道要数够几个，不能按 0 个当作"永远满足"。
            # 与 document_present 同一哲学：机器看不见 ≠ 满足，转人工/LLM 裁决。
            return PrimitiveResult(
                "unknown",
                "未提供数量门槛（params.min_count 缺失，需人工核查原文要求）",
                need_human=True,
            )
        if field == "performance":
            qualified = [a for a in facts.performance_amounts if a >= min_amount]
            if len(facts.performance_amounts) == 0:
                return PrimitiveResult("unknown", "未提取到业绩清单（需人工核查）", need_human=True)
            if len(qualified) >= minimum:
                return PrimitiveResult(
                    "pass",
                    f"满足金额要求的业绩 {len(qualified)} 个 ≥ 要求 {minimum} 个",
                )
            return PrimitiveResult(
                "fail", f"满足金额要求的业绩 {len(qualified)} 个 < 要求 {minimum} 个"
            )
        if field == "certificates":
            count = len(facts.certificates)
            if count >= minimum:
                return PrimitiveResult("pass", f"证书 {count} 项 ≥ 要求 {minimum} 项")
            return PrimitiveResult("fail", f"证书 {count} 项 < 要求 {minimum} 项")
        return PrimitiveResult("unknown", f"未知计数字段：{field}", need_human=True)

    if check_type == "date_valid":
        # 确定性日期核查（2026-10-27 无效标识别）：params {document, not_before?,
        # not_after?}——定位文件段落中的落款/签署日期与有效期窗口比对。
        # 找不到日期/文件 → unknown（不可判定）；日期在窗口外 → fail（文本事实，
        # 逐字可核验，如授权书落款晚于投标截止）。
        doc_name = str(params.get("document") or "")
        text = facts.text or ""
        window = text
        if doc_name:
            pos = text.find(doc_name)
            if pos >= 0:
                window = text[pos: pos + 2000]
            else:
                hit = _phrase_hit(_document_phrases(doc_name), facts)
                if hit is None:
                    return PrimitiveResult(
                        "unknown", f"未定位到「{doc_name}」（可能为扫描件），无法核查日期",
                        need_human=True)
        from datetime import date as _date

        dates = []
        for m in re.finditer(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})", window):
            try:
                dates.append(_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                continue
        if not dates:
            return PrimitiveResult(
                "unknown", f"「{doc_name or '文件'}」中未识别到日期（需人工核查落款）",
                need_human=True)
        signed = min(dates) if params.get("use_earliest") else max(dates)

        def _parse_bound(v):  # noqa: ANN001
            try:
                y, mo, d = str(v).split("-")
                return _date(int(y), int(mo), int(d))
            except Exception:  # noqa: BLE001
                return None

        lo = _parse_bound(params.get("not_before"))
        hi = _parse_bound(params.get("not_after"))
        if hi is not None and signed > hi:
            return PrimitiveResult(
                "fail", f"「{doc_name or '文件'}」落款日期 {signed.isoformat()} 晚于截止 {hi.isoformat()}")
        if lo is not None and signed < lo:
            return PrimitiveResult(
                "fail", f"「{doc_name or '文件'}」落款日期 {signed.isoformat()} 早于起始 {lo.isoformat()}")
        return PrimitiveResult(
            "pass", f"「{doc_name or '文件'}」落款日期 {signed.isoformat()} 在有效窗口内")

    if check_type == "contains_phrase":
        phrase = str(params.get("phrase") or "")
        if not phrase:
            return PrimitiveResult("unknown", "未提供检索短语（检查定义缺失）", need_human=True)
        hit = _commitment_hit(phrase, facts.text)
        if hit:
            return PrimitiveResult("pass", f"已找到声明「{hit}」")
        # 检索未命中**不能**证明声明不存在：措辞变体、OCR 断行、扫描件都会漏检。
        # 硬 fail→recommend_reject 是误否决的制造机（2026-10-26 run 54 实证：
        # c13「不得转包」7/7 全员误否决）。未命中一律转人工/auto 模式 LLM 语义裁决。
        return PrimitiveResult(
            "unknown",
            f"未检索到声明「{phrase}」（措辞变体/扫描件可能导致漏检，不作为否决依据）",
            need_human=True,
        )

    if check_type == "math_relation":
        return _check_math(params, facts)

    if check_type == "collusion_check":
        return _check_collusion(params, facts)

    return PrimitiveResult("unknown", f"未知检查类型：{check_type}", need_human=True)


def _check_collusion(params: dict, facts: BidFacts) -> PrimitiveResult:
    """实施条例第 40 条：读跨投标人预计算信号（scoring_service 注入 facts.cross_bidder）。

    信号由 engine/collusion.py 预计算——本函数只解释，不自建跨家视角。
    - 命中信号 → fail + need_human（verdict → conflict_flagged，**绝不自动否决**）；
    - 信号不可用 / 未命中其余情形 → unknown + need_human（机器不可判，转人工）。
    """
    sig = getattr(facts, "cross_bidder", None) or {}
    hits = sig.get("signals") or []
    if hits:
        detail = sig.get("detail") or "、".join(hits)
        return PrimitiveResult(
            "fail",
            f"疑似串通信号（须评标委员会认定，不自动否决）：{detail}",
            need_human=True,
        )
    if not sig.get("available"):
        return PrimitiveResult(
            "unknown",
            "跨投标人信号不可用（有效报价或作者元数据不足），须人工核查",
            need_human=True,
        )
    return PrimitiveResult(
        "unknown",
        "未见报价规律性差异/同作者信号；串通其余情形（同一人编制或办理、文件异常一致、"
        "混装、保证金同账户转出）须评标委员会判定",
        need_human=True,
    )


def _check_math(params: dict, facts: BidFacts) -> PrimitiveResult:
    """报价相关算术检查：限价 / 大小写一致 / 分项合计一致。冲突（不一致）不自动选边。"""
    problems: list[str] = []
    max_price = params.get("max_yuan") or params.get("max")
    if max_price is not None and facts.bid_price is not None:
        if facts.bid_price > Decimal(str(max_price)):
            problems.append(
                f"投标报价 {_money(facts.bid_price)} 超过最高限价 {_money(Decimal(str(max_price)))}"
            )
    if facts.bid_price is not None and facts.bid_price_upper_value is not None:
        if facts.bid_price != facts.bid_price_upper_value:
            problems.append(
                f"大写金额（{facts.bid_price_upper}＝{_money(facts.bid_price_upper_value)}）"
                f"与小写金额（{_money(facts.bid_price)}）不一致"
            )
    if facts.bid_price is not None and facts.price_items_total is not None:
        if facts.bid_price != facts.price_items_total:
            problems.append(
                f"分项报价合计（{_money(facts.price_items_total)}）"
                f"与投标总价（{_money(facts.bid_price)}）不一致"
            )
    if problems:
        return PrimitiveResult("fail", "；".join(problems), need_human=True)  # 冲突交人工
    if facts.bid_price is None:
        return PrimitiveResult("unknown", "未提取到投标报价", need_human=True)
    return PrimitiveResult("pass", "报价算术关系一致且未超限价")


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


# ---------------------------------------------------------------------------
# 判定映射
# ---------------------------------------------------------------------------
def verdict_for(
    criterion_code: str,
    criterion_name: str,
    result: PrimitiveResult,
    on_fail: str = "recommend_reject",
) -> str:
    """PrimitiveResult → 三态。绝不产生"rejected"——否决必须人工确认。"""
    if result.status == "pass":
        return "pass"
    if result.status == "fail":
        if result.need_human:
            return "conflict_flagged"  # 算术/一致性冲突 → 人工裁决
        return "recommend_reject" if on_fail == "recommend_reject" else "need_human"
    return "need_human"


# ---------------------------------------------------------------------------
# 计数类评分（商务分）：确定性
# ---------------------------------------------------------------------------
@dataclass
class CountResult:
    """计数类评分结果。

    ``unresolved`` 是**机器可读**的"算不出来"原因，供下游（报告疑问清单）直接判读：
    靠匹配 detail 里的中文措辞会让两处代码静默漂移——该耦合已经断过一次
    （score_count 说"满足金额要求的业绩 0 个"，而下游在找"未提取到业绩清单"）。
    """

    score: Decimal
    detail: str
    conflict: bool
    # None=正常算出 | "field_missing"=未给出计数口径 | "no_facts"=未抽取到事实
    unresolved: str | None = None
    # 零分理由（W1-C，2026-10-29）：score=0 时必须给理由，0 分不得伪装成
    # 确定性评审结论。取值：
    # - "confirmed_absent"  确认未提供（材料三态 False/计数有依据的 0）
    # - "unreadable"        机器不可见（点名材料页不可读 None / OCR 超限）——
    #                       非确认缺失，强制 need_human + conflict_flagged
    # - "no_facts"          机器没抽到事实（no_facts/口径缺失）
    # - "cases_unverified"  案例核验未完成（LLM 批失败/案例清单不完整/计入却
    #                       算出 0）——机器故障非确认缺失（2026-11-04 诚实化）
    # - "gate_named_missing" 点名材料闸门未核验/未通过（材料要求存在但核验未执行）
    zero_reason: str | None = None
    # 双分值（2026-11-02）：剔除未核验案例后的可复算分（人工复核一眼算账）。
    # None=本格无未核验案例（score 即结论）。
    score_verified_only: Decimal | None = None
    # 未核验案例数（>0 → 机器状态 need_human，不得落 auto_confirmed）
    unverified_counted: int = 0


def _unverified_reason_of(case: dict, required_keys) -> str:
    """案例材料 None 的三因归一（audit 用）：unreadable / scan_unverified / unlinked。

    classify_materials 的 reasons（CaseBlock.reasons）记录每个材料键的来源原因，
    取 required_flags 对应键中最严重者；信息缺失回退 unlinked（最保守）。
    """
    reasons = case.get("material_reasons") or {}
    seen: list[str] = []
    for k in required_keys:
        if case.get(k) is None:
            r = str(reasons.get(k) or "")
            if r and r not in seen:
                seen.append(r)
    if not seen:
        # LLM 路径案例无 material_reasons：引文定位失败型已标 unverified
        v = str(case.get("unverified") or "")
        return v if v in ("unreadable", "scan_unverified", "unlinked") else "unlinked"
    # 优先级：unreadable（页面不可读）> scan_unverified（扫描无特征）> unlinked
    for pref in ("unreadable", "scan_unverified", "unlinked"):
        if pref in seen:
            return pref
    return seen[0]


def _infer_count_field(criterion: dict) -> str | None:
    """field 缺失时按评分项名称推断"数什么"。

    抽取契约长期没有 field 字段（提示词也没写），真实模型路径下 count 项因此恒 0 分。

    推断必须保守，只认**明确属于投标人主体**的两类，且只看名称（描述里常出现
    "验收证书""认证"等无关词，用它判断会把业绩项误判成证书项）：
    - 名称须同时含主体词（投标人/公司/企业）与口径词（业绩/案例 → performance，
      认证/资质/证书 → certificates）
    - 人员级（项目负责人、工程师、团队…）一律不推断：facts 模型里没有个人业绩桶，
      猜错会产出静默错误的数字，比可见的 0 分更危险。
    """
    name = str(criterion.get("name") or "")
    if any(k in name for k in ("项目负责人", "工程师", "人员", "团队", "项目经理", "拟派")):
        return None  # 人员级：不猜
    if not any(k in name for k in ("投标人", "公司", "企业", "供应商")):
        return None  # 主体不明确：不猜
    # 业绩优先于证书：业绩项的名称里也可能出现"证书"（如"业绩证明材料"），
    # 先判业绩可避免把业绩误算成证书。
    if any(k in name for k in ("业绩", "案例", "合同")):
        return "performance"
    if any(k in name for k in ("认证", "资质", "证书", "体系")):
        return "certificates"
    return None


def parse_required_artifacts(source_quote: str) -> list[str]:
    """从评分因素原文提取证明材料要求清单（真实评委的第一道闸门）。

    「投标人业绩…【证明材料】（1）须提供合同复印件、发票复印件和业主证明（或验收
    材料），并加盖投标人公章；（2）不提供或提供不全的不得分」→ ["合同复印件",
    "发票复印件", "业主证明", "验收材料"]。

    无【证明材料】标记时把全文当要求段解析——`required_evidence`（合并/折叠写入）
    本身就是要求文本，不再依赖那个标记。
    """
    if not source_quote:
        return []
    m = re.search(r"【证明材料】(.+)", source_quote, re.S)
    return _parse_artifact_segment(m.group(1) if m else source_quote)


# 常见材料名词直查（覆盖固定搭配，先于通用后缀规则）
_ARTIFACT_WORDS = (
    "合同复印件", "发票复印件", "验收材料", "验收报告", "业主证明", "用户证明",
    "查询结果截图", "查询截图", "社保证明", "社保缴纳证明", "业绩证明文件",
    "证书复印件", "营业执照复印件", "中标通知书", "验收证明", "用户报告",
)
# 通用后缀：以这些词结尾的 2-12 字片段视为材料名词
_ARTIFACT_SUFFIX_RE = re.compile(r"(复印件|证明|材料|截图|报告|清单|凭证|扫描件)$")
# 混进要求段的非名词片段
_ARTIFACT_NOISE = ("不得分", "提供不全", "公章", "否则", "原件")


def _parse_artifact_segment(seg: str) -> list[str]:
    """要求文本 → 材料名词清单（去序号/动词/句式噪声）。

    旧实现调用了一个不存在的 `_parse_artifact_segment`（调用即 NameError），
    整条材料清单解析因此是死代码，`required_evidence` 只能整句当 artifacts 用。
    """
    out: list[str] = []
    text = re.sub(r"[（(]\s*\d+\s*[)）]", "", seg or "")  # （1）（2）
    for word in _ARTIFACT_WORDS:
        if word in text and word not in out:
            out.append(word)
    for part in re.split(r"[、，,；;。：:\n（）()\s]", text):
        p = part.strip()
        if not (2 <= len(p) <= 12) or not _ARTIFACT_SUFFIX_RE.search(p):
            continue
        if any(k in p for k in _ARTIFACT_NOISE):
            continue
        if p not in out:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# 认证"查询截图"证据判定（2026-10-21 重做）
# ---------------------------------------------------------------------------
# 通道 A：文本层标题行。旧实现只认整行"查询结果截图"且跳过 >300 字的页——
# 致成写"查询网址/查询截图："（所在页约 1000 字）被整页跳过，ISO 三项被清零。
#
# 2026-10-22 修复：旧正则 `查询(?:结果)?截图|查询网址` 太宽泛，"行贿犯罪记录
# 查询结果截图"也命中 → cert_section_present=True 对四家都成立 → ISO 全满分。
# 修复：保留原始正则（裸标签"查询截图"仍需匹配），但加两层过滤：
#   ① 排除非认证查询（行贿/犯罪记录/信用中国/无犯罪）
#   ② 排除证书免责声明（"可在官方网站查询"）
#   ③ 平台 URL（cx.cnca.cn）强信号，在行长度检查之前命中
_CERT_TITLE_RE = re.compile(r"查询(?:结果)?截图|查询网址|查询地址|证书查询|认证结果查询")
_CERT_EXCLUDE = re.compile(r"行贿|犯罪记录|信用中国|无犯罪")
_CERT_DISCLAIMER = re.compile(r"官方网站上?查询|可查询")
_CERT_PLATFORM_URL = re.compile(r"cx\.cnca\.cn|cnca\.cn")
# 通道 B：OCR 正文的**平台专有**指纹。"证书编号"这类词证书扫描件也有，不能单独用。
_CERT_STRONG_FINGERPRINTS = ("认证认可信息公共服务平台", "认证行政监管", "证书查询结果")
_CERT_WEAK_FINGERPRINTS = (
    "国家市场监督管理总局", "认证结果", "公共服务平台", "证书状态",
)


def cert_evidence_present(pages: list[tuple[str, str]]) -> tuple[bool, str]:
    """认证"查询截图"证据双通道判定 → (是否存在, 命中依据)。

    `pages` 是 [(document_pages.source, 页文本)]。通道 A 对全部页的文本层生效；
    通道 B 只对 OCR 页生效（防止证书扫描件的"证书编号"误触发）。
    实证校准（浙江中烟 4 家）：龙即/致成有文本标题 → 真；诚道证书页图片就是
    认证平台查询截图（OCR 正文含平台名）→ 真；先数只有证书扫描件 → 假（真实
    评审也是只给先数按"无截图"计 1 分）。
    """
    for _source, text in pages:
        for line in (text or "").split("\n"):
            s = line.strip()
            if not s or s.count("....") >= 2:
                continue
            # 平台 URL 强信号（在行长度检查之前——URL 行可能很长）。
            # 但要求文本也含 cx.cnca.cn（"查询结果截图并加盖公章，未按要求提供不得分"），
            # 需排除要求特征词，只匹配投标人自己的标签/标题行。
            if _CERT_PLATFORM_URL.search(s) and not any(
                k in s for k in ("不得分", "加盖公章", "未按要求", "须提供", "应提供")
            ):
                return True, f"平台 URL：{s[:40]}"
            if len(s) > 60:
                continue
            # 要求复述行（招标文件索引表常整页复述"须提供…截图，未提供不得分"）
            # 不算投标人提供了截图：同时出现"须/应/需"与"提供/不得分"即视为要求句。
            # 2026-10-22 扩展：要求文本也含"加盖公章/未按要求"等特征词，一并排除。
            if any(k in s for k in ("须", "应", "需", "加盖公章", "未按要求")) and (
                "提供" in s or "不得分" in s or "公章" in s or "要求" in s
            ):
                continue
            # 纯要求句（无"须/应/需"但含"不得分"——如"…并加盖公章，未按要求提供不得分"）
            if "不得分" in s or "加盖公章" in s:
                continue
            # 证书上的免责声明（"可在官方网站查询"）不是截图证据
            if _CERT_DISCLAIMER.search(s):
                continue
            # 排除非认证查询截图（行贿犯罪记录/信用中国等——这些是其他资质要求）
            if _CERT_EXCLUDE.search(s):
                continue
            if _CERT_TITLE_RE.search(s):
                return True, f"文本标题：{s[:40]}"
    for source, text in pages:
        if source not in ("ocr", "ocr_img"):
            continue
        t = text or ""
        for fp in _CERT_STRONG_FINGERPRINTS:
            if fp in t:
                return True, f"OCR 正文命中平台指纹：{fp}"
        weak = [k for k in _CERT_WEAK_FINGERPRINTS if k in t]
        if len(weak) >= 2:
            return True, f"OCR 正文命中平台特征：{'、'.join(weak)}"
    return False, "未见查询截图标题或认证平台指纹"



# 证书 backing 三态（2026-11-02 P0-5）：count_tier/attribute_tier 的证据闸门。
# True=计数证书有可读复印件/截图页背书；None=候选页不可读/截图页 OCR 只剩标题
# （c38 实证：p39 只有标题、证书是扫描图）——**计入并标疑问**（防"截图文本层
# 看不见→误杀"，历史实证）；False=可读文本确认无背书页。
# 2026-11-03 R3-4（run78 云平台证书 5/5 全计满实证）：section 级一票让全部证书
# 共享同一判定 → 升级逐本定位 find_cert_copy_pages；section 信号只作先验。

# 复印件页特征（证书名/编号之外的背书指纹）
_RE_CERT_COPY_FP = re.compile(r"复印件|证书编号|发证|有效期|认证日期|颁证")


def find_cert_copy_pages(
    pages: list[tuple[str, str]], cert_name: str
) -> list[tuple[str, int]]:
    """按证书名搜复印件页，返回 [(kind, 页下标)]。

    kind: "copy"=材料页特征命中（复印件/证书编号/发证/OCR 源）；
    "low"=命中但页文本过少（扫描图）；"claim"=仅清单声称/标题行。
    名称取归一化尾段（≥6 字）匹配，防全名跨行/空格漂移。
    """
    from app.parsing.normalize import normalize_no_ws

    name_raw = str(cert_name or "").strip()
    if not name_raw:
        return []
    norm_name = normalize_no_ws(normalize_cert(name_raw))
    tail = norm_name[-6:] if len(norm_name) >= 6 else norm_name
    if not tail:
        return []
    out: list[tuple[str, int]] = []
    for idx, (src, text) in enumerate(pages or []):
        t = text or ""
        nt = normalize_no_ws(t)
        if tail not in nt and norm_name not in nt:
            continue
        if len(t.strip()) < 30:
            out.append(("low", idx))
        elif _RE_CERT_COPY_FP.search(t) or src in ("ocr", "ocr_img"):
            out.append(("copy", idx))
        else:
            out.append(("claim", idx))
    return out


def classify_cert_backing(
    facts, pages: list[tuple[str, str]] | None, cert_section: tuple[bool, str] | None = None
) -> dict[str, bool | None]:
    """每本计数证书的背书三态：{归一化证书键: True/None/False}。

    逐本模式（默认，BID_EVAL_CERT_BACKING_PER_CERT=0 回退 section 级一票）：
    - 复印件页（材料特征/OCR 源）可读 → True；
    - 命中候选页但低证据（扫描图看不清）→ None；
    - 仅声称行命中、且文档有扫描/低证据页 → None（复印件可能在没进文本层的
      图里——看不见≠没有，用友防线）；
    - 仅声称行命中、全文层可读 → False（可读确认无复印件，"须提供有效证书
      复印件，未提供不得分"硬口径，德勤防线）；
    - 完全无命中（名称抽取异常）→ section 级先验兜底，不猜。
    section 级（旧口径）：cert_section_present 双通道一票通/一票否。
    """
    out: dict[str, bool | None] = {}
    certs = list(getattr(facts, "certificates", None) or [])
    if not certs:
        return out
    section_ok, section_why = cert_section if cert_section else (None, "")
    low_evidence = False
    has_scan = False
    if pages:
        for _src, t in pages:
            # 低证据页判定（与 case_evidence._low_evidence 同口径精神）：OCR 只
            # 输出极少字（截图页常见）→ 看不见≠没有
            if len((t or "").strip()) < 30:
                low_evidence = True
                has_scan = True
            if _src in ("ocr", "ocr_img"):
                has_scan = True
    per_cert = config.cert_backing_per_cert_enabled() and bool(pages)
    for name in certs:
        key = normalize_cert(str(name))
        if per_cert:
            hits = find_cert_copy_pages(pages or [], str(name))
            kinds = [k for k, _i in hits]
            if "copy" in kinds:
                out[key] = True
                continue
            if "low" in kinds:
                out[key] = None
                continue
            if "claim" in kinds and not has_scan:
                out[key] = False  # 全文可读、只有声称行 → 确认无复印件
                continue
            if "claim" in kinds:
                out[key] = None  # 扫描页可能藏复印件 → 计入+疑问
                continue
            # 零命中（名称抽取异常）→ 落 section 先验兜底，不猜
        if section_ok is True:
            out[key] = True  # 查询截图/平台指纹确认存在背书
        elif low_evidence:
            out[key] = None  # 有候选页但不可读 → 计入+疑问
        elif section_ok is False and "未见" in (section_why or ""):
            out[key] = False  # 可读文本确认无背书页
        else:
            out[key] = None  # 信息不足 → 不猜
    return out


def recover_artifacts_from_tender(
    criterion: dict, tender_pages: list[tuple[int, str]]
) -> str:
    """从招标文件原文恢复评分项的【证明材料】要求段（原文文本）。

    编译 quote 常漏掉证明材料段（LLM 只抽打分规则），而该段在招标原文中
    紧跟因素原文之后（或由相邻项共享）。用因素原文前缀（空白不敏感，长度
    阶梯回退应对表格布局插字）在招标页中定位，返回自【证明材料】起的原文段。
    找不到返回空串。

    **契约**：`tender_pages` 的页文本必须已归一化（调用方 scoring_service 用
    `normalize_no_ws` 预处理过）——本函数只归一化 quote，不归一化页文本。
    """
    from app.parsing.normalize import normalize_no_ws

    raw_quote = str(criterion.get("source_quote") or "")
    q_full = normalize_no_ws(raw_quote)
    if not q_full or not tender_pages:
        return ""
    prefixes = [q_full[:n] for n in (40, 28, 18, 12) if len(q_full) >= n]
    for _page_no, ptext in tender_pages:
        pos = -1
        for q in prefixes:
            pos = ptext.find(q)
            if pos >= 0:
                break
        if pos < 0:
            continue
        window = ptext[pos : pos + 1600]
        m = re.search(r"【证明材料】", window)
        if not m:
            continue
        seg = window[m.start() :]
        nxt = seg.find("【", 8)
        return seg[: nxt if nxt > 0 else len(seg)][:600]
    return ""


def _tier_score(tiers, n: Decimal) -> Decimal | None:
    """按 min_count 档表查 n 的分值（命中最高档）；无命中返回 None（不猜 0 档）。

    R0 客观路由的人员经验档（"2个及以上经验得1分；4个及以上得2分"）与
    score_count_tier 同语义，抽共用避免两处口径漂移。
    """
    best: Decimal | None = None
    for t in tiers or []:
        try:
            if n >= Decimal(str(t["min_count"])):
                sc = Decimal(str(t["score"]))
                if best is None or sc > best:
                    best = sc
        except Exception:  # noqa: BLE001
            continue
    return best


def _tier_shape(tiers) -> str:
    """档表形状判定（2026-11-04 run79 业绩回归实锤）：count_tier | amount_tier | mixed | none。

    「每提供一个得2分」类规则的 params 里可能带**金额档** tiers
    （min_amount_yuan/max_amount_yuan/per_item 形状）——它是按金额分段的
    每案例分值，不是"计几个得几分"的计数档。计数档查表（_tier_score 只认
    min_count）对金额档无命中 → 旧代码强制 0 分，可计 3-10 个案例照样 0
    （run79 四家「投标人业绩」4→0 回归）。
    """
    shapes: set[str] = set()
    for t in tiers or []:
        if not isinstance(t, dict):
            continue
        if "min_count" in t:
            shapes.add("count_tier")
        if "min_amount_yuan" in t or "max_amount_yuan" in t:
            shapes.add("amount_tier")
    if not shapes:
        return "none"
    return shapes.pop() if len(shapes) == 1 else "mixed"


def score_count(criterion: dict, params: dict, facts: BidFacts) -> CountResult:
    """count 类评分项。

    证书类：只计证书表内的（有复印件的）；正文声称但无证书的 → 冲突标记交人工。
    人员级（person_scope）：只用 LLM 逐案例核验结果，不退回公司级业绩桶。
    """
    per_item = Decimal(str(params.get("per_item") or 0))
    max_items = int(params.get("max_items") or 0)
    max_score = Decimal(str(criterion.get("max_score") or 0))
    field = params.get("field") or ""
    person_scope = str(params.get("person_scope") or "")
    inferred = ""
    unresolved: str | None = None
    zero_reason: str | None = None  # 0 分理由（见 CountResult.zero_reason）
    score_verified_only: Decimal | None = None  # 剔除未核验案例的可复算分
    unverified_counted = 0  # 未核验案例数（>0 → need_human）
    if person_scope:
        # 人员级项：facts.performance_amounts 是公司级业绩，按它计数会让人人封顶。
        # 强制走 verified_cases（LLM 按人核验）；无案例则 unresolved。
        field = ""
        unresolved = "field_missing"
    if not field:
        guessed = None if person_scope else _infer_count_field(criterion)
        if guessed:
            field = guessed
            inferred = "（计数字段由评分项名称推断，请核对）"
        elif not person_scope:
            unresolved = "field_missing"

    conflict = False
    if field == "certificates":
        # 声称了但证书表里没有 → 冲突（可能是引用造假）
        cert_keys = [normalize_cert(c) for c in facts.certificates]
        for claim in facts.cert_claims:
            if normalize_cert(claim) not in cert_keys:
                conflict = True
        # R3-4 证书背书三态（cert_backing 由 scoring_service 逐本定位注入，
        # 与 score_count_tier 同语义）：False=可读确认无复印件不计（细则"须提供
        # 有效证书复印件，未提供不得分"硬口径）；None=复印件看不清/未绑上 →
        # 计入并标疑问（看不见≠没有，用友防线）。
        _backing = criterion.get("cert_backing") or {}
        _unbacked = 0
        _unverified_c = 0
        certs_for_count = list(facts.certificates)
        if _backing:
            _kept_c = []
            for cert in certs_for_count:
                state = _backing.get(normalize_cert(str(cert)))
                if state is False:
                    _unbacked += 1
                    continue
                if state is None:
                    _unverified_c += 1
                _kept_c.append(cert)
            certs_for_count = _kept_c
            if _unbacked:
                conflict = True  # 确认缺失剔除 → 疑问留人工复核
            if _unverified_c:
                conflict = True
                unverified_counted = _unverified_c
        # ISO 三体系与 CMMI 分开计数（ISO 型认证项的计分结构：
        # ISO9001/27001/20000 每项 1 分上限 3，CMMI 二级以上 1 分）。
        # 证书名必须先归一化：OCR 形态（ISO/IEC 20000-1:2018、中文证书名）与
        # 同一体系的两种写法（GB/T19001 与 质量管理体系）旧实现全部匹配不上。
        iso_set: set[str] = set()
        has_cmmi = False
        for cert in certs_for_count:
            key = normalize_cert(cert)
            if key.startswith("ISO"):
                iso_set.add(key)  # 任意 ISO 族（9001/14001/45001…）都计
            elif key == "CMMI":
                has_cmmi = True
    if field == "cmmi":
        # CMMI 独立计分项（reconcile 生成的 field=cmmi）：「CMMI 二级以上 1 项
        # 得 N 分」，存在性计分、与 ISO 项互斥。LLM 曾把此条标 match 丢失满分，
        # 现按单一证书计数（remote_reconcile 2026-10-30）。
        iso_set = set()
        has_cmmi = any(normalize_cert(c) == "CMMI" for c in facts.certificates)
        count = 1 if has_cmmi else 0
        detail = f"提供 CMMI 认证 {count} 项（{('有' if has_cmmi else '无')}有效期内 CMMI 证书）"
        if not facts.certificates and not facts.cert_claims:
            detail = "未提取到证书清单（需人工核查）"
            unresolved = "no_facts"
            zero_reason = "no_facts"
        effective = min(Decimal(count), Decimal(max_items)) if max_items else Decimal(count)
        score = (effective * per_item).quantize(Decimal("0.01"))
        if max_score and score > max_score:
            score = max_score
        if score == 0 and zero_reason is None:
            # 证书清单非空但无 CMMI → 机器确认其无 CMMI 项（确认未提供）
            zero_reason = "confirmed_absent"
        return CountResult(
            score=score, detail=detail, unresolved=unresolved, conflict=conflict,
            zero_reason=zero_reason,
        )
    if field == "certificates":
        # ISO 型判定看规则文本是否点名体系；未点名的证件计数项（资质证/操作证/
        # 职称证等）按证书清单总数计——旧实现无条件走 ISO/CMMI 口径，非 ISO
        # 证件项恒 0（2026-10-26 run 54 通用证件类实证）。
        # 裸数字（9001/27001/20000）不得进正则：「120000 元」「19001 元」会把
        # 非 ISO 项误判成 ISO 型（审查实锤），体系号必须带 ISO/GB/T 前缀。
        _cert_text = (
            str(criterion.get("name") or "")
            + str(criterion.get("description") or "")
            + str(criterion.get("required_artifacts_raw") or criterion.get("required_artifacts") or "")
        )
        _iso_shaped = bool(re.search(
            r"ISO|CMMI|三体系|GB/?T\s*\d"
            r"|质量管理体系|信息安全管理体系|信息技术服务管理体系",
            _cert_text,
        ))
        # 合并项（ISO+CMMI 同一 count）：CMMI 有独立分值（"CMMI…得 2 分"），
        # 单 per_item 装不下两种规则——统一 per_item=1 会让 CMMI 只值 1 分
        # （真实 2 分，run 66 实证 c41 得 4 而人工 5）。从文本解析 CMMI 分值，
        # 解析不到则回退 per_item（保持旧行为，不猜默认值）。
        _cmmi_pts_m = re.search(r"CMMI[^。；]{0,40}?得\s*(\d+(?:\.\d+)?)\s*分", _cert_text)
        _cmmi_pts: Decimal | None = Decimal(_cmmi_pts_m.group(1)) if _cmmi_pts_m else None
        if _iso_shaped:
            # 仅当细则文本点名 CMMI 才与 ISO 合并计数——纯 ISO 项（"ISO9001/
            # 27001/20000 每项1分最高3分"）的 CMMI 由独立 field=cmmi 条目计，
            # 混算会被 max_items/cap 吞掉（2026-10-30 run 66 实证：四家 CMMI
            # 2 分全丢，c47 得 3.00 是 ISO3 被 cap 后 CMMI 挤不进去）。
            if "CMMI" in _cert_text:
                count = len(iso_set) + (1 if has_cmmi else 0)
            else:
                count = len(iso_set)
        else:
            count = len(certs_for_count)
        # 一条都没抽到 ≠ 一条都不合格：前者是抽取能力问题，必须与"确实 0 项"区分开，
        # 否则 0 分会被当成评分结论进入总分。
        if not facts.certificates and not facts.cert_claims:
            detail = "未提取到证书清单（需人工核查）"
            unresolved = "no_facts"
            zero_reason = "no_facts"
        elif _iso_shaped:
            detail = f"提供证书 {count} 项（ISO 三体系 {len(iso_set)} 项" + (
                "、CMMI 1 项）" if has_cmmi else "）"
            )
            if conflict:
                detail += "；正文声称的认证中有未附证书复印件的（待人工核查）"
        else:
            detail = f"提供证书 {count} 项（按证书清单计数）"
            if conflict:
                detail += "；正文声称的证书中有未附复印件的（待人工核查）"
        _bk_note = ""
        if _unbacked:
            _bk_note += f"（{_unbacked} 本确认无复印件不计）"
        if _unverified_c:
            _bk_note += (
                f"（{_unverified_c} 本复印件不可读/未绑上，按未核验计入并标疑问，"
                f"仅计可核验 {len(certs_for_count) - _unverified_c} 本的剔除后口径）"
            )
        detail += _bk_note
    elif field == "performance":
        min_amount = Decimal(str(params.get("min_amount_yuan") or 0))
        count = len([a for a in facts.performance_amounts if a >= min_amount])
        if not facts.performance_amounts:
            detail = "未提取到业绩清单（需人工核查）"
            unresolved = "no_facts"
            zero_reason = "no_facts"
        else:
            detail = f"满足金额要求的业绩 {count} 个"
    else:
        count = 0
        if person_scope:
            # 人员级条目 field 被强制清空走 verified_cases；无案例时的正确文案是
            # 「核验未产出」而非「未知计数字段」（run 54 c59-c62 的误导性告警来源）
            detail = "人员级条目：逐案例核验未产出可计案例"
            unresolved = "no_facts"
            zero_reason = "no_facts"
        elif field:
            detail = f"逐条核验未产出可计条目（计数口径 {field}，需人工核查）"
            unresolved = "no_facts"
            zero_reason = "no_facts"
        else:
            detail = "未知计数字段（评分项未给出 field，且无法从名称安全推断）"
            unresolved = "field_missing"

    if field == "certificates" and _iso_shaped and _cmmi_pts is not None and "CMMI" in _cert_text:
        # 合并项（ISO+CMMI 同一 count）：ISO 按 per_item、CMMI 按文本分值分别计。
        # 单 per_item 装不下两种规则，统一 per_item=1 会让 CMMI 只值 1 分（真实 2 分）。
        _iso_n = min(len(iso_set), max_items) if max_items else len(iso_set)
        score = ((_iso_n * per_item) + (_cmmi_pts if has_cmmi else Decimal(0))).quantize(Decimal("0.01"))
    else:
        effective = min(Decimal(count), Decimal(max_items)) if max_items else Decimal(count)
        if params.get("tiers"):
            # R0 客观路由档表（"提供两项得1分，四项得2分"）：命中最高档
            _ts = _tier_score(params.get("tiers"), effective)
            score = _ts if _ts is not None else Decimal(0)
        else:
            score = (effective * per_item).quantize(Decimal("0.01"))
    if max_score and score > max_score:
        score = max_score

    # 认证项材料门槛（2026-10-16 用户授权 AI 按规则裁量）：因素要求"查询结果
    # 截图"，截图内容 OCR 不可得，改用**截图节存在性**判定（cert_section_present
    # 由 scoring_service 检测）。无截图节 → ISO 部分按"未按要求提供不得分"计 0，
    # CMMI 仅需证书复印件照计。实证：龙即有截en节（真实 ISO 3 分）、先数无（真实 1）。
    _artifact_text = str(criterion.get("required_artifacts") or "")
    _artifact_raw = str(criterion.get("required_artifacts_raw") or "")
    if (
        field == "certificates"
        # 材料清单解析可能只留下"证书复印件"这类实词，要求原文也要一起看
        and "截图" in (_artifact_text + _artifact_raw)
        and not criterion.get("cert_section_present", True)
    ):
        iso_count = len(iso_set)
        # 截图是点名材料：未按要求提供 → ISO 系全项计 0；CMMI 类仅需证书
        # 复印件照计（锚定口径）。通用证件同样归零（"否则不得分"规则执行）。
        count = 1 if has_cmmi else 0
        effective = min(Decimal(count), Decimal(max_items)) if max_items else Decimal(count)
        score = (effective * per_item).quantize(Decimal("0.01"))
        if max_score and score > max_score:
            score = max_score
        detail = (
            f"未见认证平台查询结果截图节：ISO 系 {iso_count} 项按"
            "「未按要求提供不得分」计 0"
            + ("；CMMI 1 项（仅需证书复印件）照计" if has_cmmi else "")
        )
        # 零分理由分离（W1-C）：截图节"看不见"≠"确认未提供"。本文档解析警告
        # 含 OCR 超限/不可用/识别失败 → 页面可能根本没进文本层 → "unreadable"
        # （机器不可见，非确认缺失）；否则维持规则硬口径 "confirmed_absent"。
        # doc_parse_warnings 由 scoring_service 从 Document.warnings_json 传入；
        # 拿不到（旧路径/键缺失）→ 退化 confirmed_absent（保守，不误标转人工）。
        # CMMI 照计时 score>0，不置零分理由（约定：score=0 才必须给理由）。
        if score == 0:
            _doc_warns = criterion.get("doc_parse_warnings") or []
            # warnings_json 条目形态：{"code":..., "message":...} 或裸字符串
            _warn_text = " ".join(
                str(w.get("message") if isinstance(w, dict) else w) for w in _doc_warns
            )
            # 关键词覆盖 pdf_pymupdf 实际文案：逐页「已超出 OCR 页数上限…跳过」、
            # 汇总「超限跳过 N 页」、「OCR 识别失败」「OCR 不可用」「无文字层」
            # （2026-10-29 审查 P2：只认"超限跳过"匹配不上逐页文案）
            _ocr_blind = any(
                k in _warn_text
                for k in ("OCR 页数上限", "超限跳过", "OCR 不可用", "识别失败", "无文字层")
            )
            if _ocr_blind:
                zero_reason = "unreadable"
            elif unresolved:
                # 证书本身没提取到（no_facts/field_missing）→ 不能宣称"确认未提供"
                #（2026-10-29 审查 P2：ISO 门曾把 no_facts 覆盖成 confirmed_absent）
                zero_reason = "no_facts"
            else:
                zero_reason = "confirmed_absent"

    # 证明材料链校验（LLM 逐案例核验版）：有 cases 明细时按"材料齐全的案例"计数；
    # 因素带证明材料要求但 LLM 核验未执行 → 分数照算并标冲突转人工
    #（材料齐全性本质需翻原件核对，AI 判定只作为疑问线索，不直接扣分）
    artifacts = str(criterion.get("required_artifacts") or "")
    cases = criterion.get("verified_cases") or []
    if cases:
        # 引文全部被 sanitize 降级（照抄要求文本/过短且无实体）→ 计数无依据，
        # 不能把 0 分当成评审结论（与"机器看不见 ≠ 不符合"同一哲学）
        clean_cases = [c for c in cases if str(c.get("sanitized") or "ok") == "ok"]
        if not clean_cases:
            unresolved = "cases_unverified"  # 诊断码（不弃评；scoring_service 一律写分）
            conflict = True
            from collections import Counter

            _reasons = Counter(
                str(c.get("sanitized") or "ok") for c in cases if str(c.get("sanitized") or "ok") != "ok"
            )
            _breakdown = "、".join(f"{k}={v}" for k, v in _reasons.items()) or "未知"
            # 级联：引文全不过 → 回退确定性业绩清单计数（事实优先于猜），避免
            # "引文核验全毙 → 0 分"。仍无事实 → 保持当前分数（通常 0）并标疑问。
            if field == "performance" and facts.performance_amounts:
                _fc = (
                    len([a for a in facts.performance_amounts if a >= min_amount])
                    if min_amount > 0
                    else len(facts.performance_amounts)
                )
                _eff = min(Decimal(_fc), Decimal(max_items)) if max_items else Decimal(_fc)
                score = (_eff * per_item).quantize(Decimal("0.01"))
                if max_score and score > max_score:
                    score = max_score
                detail = (
                    f"逐案例引文未通过核验（{_breakdown}），回退确定性业绩清单计数"
                    f"{_fc} 个（标疑问）"
                )
            else:
                detail += (
                    f"；{len(cases)} 个案例的引文均未通过核验（{_breakdown}），"
                    f"按 {score} 分计入并标疑问"
                )
            # 引文全毙且分数仍为 0：作弊型（requirement_shadow 等）→ 计数无依据
            # （no_facts）；**定位失败型**（quote_not_in_bid/quote_too_short，OCR
            # 串行噪声最常见）→ 归 unreadable 转人工——机器没核上 ≠ 没有材料
            if score == 0:
                _locate_fail_kinds = {"quote_not_in_bid", "quote_too_short"}
                if _reasons and all(k in _locate_fail_kinds for k in _reasons):
                    zero_reason = "unreadable"
                else:
                    zero_reason = "no_facts"
        else:
            cases = clean_cases
            # 已有逐案例核验结果 = 计数有依据；清掉"字段缺失/未抽到事实"的 unresolved
            # 及早前分支可能置的 no_facts 零分理由（材料闸门段会按实际 0 因重置）
            unresolved = None
            zero_reason = None
            _type_gate_note = ""
            _type_dropped = 0
            # 人员级：按负责人/成员过滤；模型完全没给 role 时退回全部案例并标疑问
            if person_scope:
                roles = [_normalize_role(c) for c in cases]
                has_role = any(r is not None for r in roles)
                scoped = [
                    c for c, r in zip(cases, roles) if r == person_scope
                ]
                if has_role:
                    cases = scoped
                    if not cases:
                        conflict = True
                        detail += "；未识别到人员范围内的案例"
                else:
                    conflict = True
                    detail += "；模型未标注案例人员归属，按全部案例计（需人工核对）"
                # 负责人经验"类似运维项目"类型确定性过滤（2026-10-29 三态重做）：
                # 旧实现名称须含 运维|维护 才计——"二维码综合管理平台项目"这类
                # 平台名运维案例被当"集成/建设类"系统性误杀（7.10 run 64 实证，
                # 人工计满分）。按细则枚举改为三态：运维类标记 → 保留；**明确**
                # 非运维单一形态（细则点名排除的设备维保/网络建设/系统集成/
                # 软件开发）→ 剔除；两者皆无 → 机器无法确认类型 ≠ 确认不符，
                # 保留并标疑问（同"机器看不见≠不符合"原则）。
                if person_scope == "leader" and cases:
                    _factor_text = (
                        str(criterion.get("name") or "")
                        + str(criterion.get("description") or "")
                    )
                    if "运维" in _factor_text:
                        _RE_OPS_TYPE = re.compile(r"运维|维护|技术服务|保障服务")
                        _RE_NON_OPS_TYPE = re.compile(
                            r"设备维保|硬件维保|网络(?:系统)?建设|系统集成|"
                            r"软件开发|系统开发|开发(?:项目|工程)"
                        )
                        kept: list[dict] = []
                        _ntype = 0
                        _nsure = 0
                        for _c in cases:
                            _nm = str(_c.get("name") or "")
                            if _RE_OPS_TYPE.search(_nm):
                                kept.append(_c)
                            elif _RE_NON_OPS_TYPE.search(_nm):
                                _ntype += 1  # 明确非运维单一形态 → 剔除
                            else:
                                kept.append(_c)
                                _nsure += 1  # 类型无法确认 → 保留+疑问
                        if _ntype or _nsure:
                            cases = kept
                            _type_dropped = _ntype
                            conflict = True
                            _type_gate_note = (
                                (f"（按'类似运维项目'类型门槛剔除 {_ntype} 个"
                                 f"明确非运维类案例）" if _ntype else "")
                                + (f"（{_nsure} 个案例类型无法机器确认，保留计分"
                                   f"并标疑问，建议人工核看）" if _nsure else "")
                            )
            # 金额门槛：明确低于门槛的不计；金额缺失的照计但标疑问
            min_amount = Decimal(str(params.get("min_amount_yuan") or 0))
            if min_amount > 0 and cases:
                qualified: list[dict] = []
                unknown_amount = 0
                for case in cases:
                    amt = case.get("amount_yuan")
                    if amt is None or amt == "":
                        unknown_amount += 1
                        qualified.append(case)
                        continue
                    try:
                        if Decimal(str(amt)) >= min_amount:
                            qualified.append(case)
                    except Exception:  # noqa: BLE001
                        unknown_amount += 1
                        qualified.append(case)
                cases = qualified
                if unknown_amount:
                    conflict = True
                    detail += f"；{unknown_amount} 个案例金额无法确认（需人工核对）"
            required_flags = _required_flags(
                artifacts,
                person_scope=person_scope,
                criterion_name=str(criterion.get("name") or ""),
                rule_text=str(criterion.get("name") or "") + "\n" + str(criterion.get("description") or ""),
            )
            # 窄版材料闸门（2026-10-30）：三态材料 True=确认有 / False=确认无 /
            # None=机器看不清或没绑上（页不可读、案例名没链到材料页、页没送审）。
            # 旧口径把 None 也扣 0，等于把管线故障（OCR 没读出/没绑上/没送审）算在
            # 投标人头上——用友 c54/c55 材料在却判 0、人工给 4 实证。窄版：
            #   any False（可读文本确认缺失）→ 不计（规则"不全不得分"硬口径不变）
            #   all True → 计
            #   否则含 None → 计入并标"未核验"+进人工队列（不再打 0）
            # 开关 config.case_unverified_count_enabled()（BID_EVAL_CASE_UNVERIFIED_COUNT）
            # 关 → 回退"None 不计"（保守），供 eval_suite 锚点对比与回滚。
            complete: list[dict] = []
            unverified_pool: list[dict] = []
            unverified_counted = 0
            null_dropped = 0  # 仅回退模式（None 不计）
            dropped_false = 0
            _unverified_on = config.case_unverified_count_enabled()
            for c in cases:
                vals = {k: c.get(k) for k in required_flags}
                if any(v is False for v in vals.values()):
                    dropped_false += 1  # 确认缺失 → 不计（规则硬口径）
                    continue
                if all(v is True for v in vals.values()):
                    complete.append(c)  # 全部确认 → 计
                    continue
                # 无 False、非全 True → 存在 None（不可读/未绑上）
                if _unverified_on:
                    c["unverified"] = True
                    # None 细分三因（audit）：unreadable=低证据页 / scan_unverified=
                    # 扫描页无特征 / unlinked=案例没绑上材料页（classify_materials 口径）
                    c["unverified_reason"] = _unverified_reason_of(c, required_flags)
                    unverified_pool.append(c)
                    unverified_counted += 1
                    conflict = True
                else:
                    # 回退：None 不计（保守）
                    null_dropped += 1
                    conflict = True
            if dropped_false:
                conflict = True  # 确认缺失 → 0 也标疑问（规则"不全不得分"，留人工复核）
            # 排序收紧（2026-11-02 用户确认口径，次序规则非折扣系数）：verified 在前、
            # unverified 在后——max_items 封顶先砍未核验案例，锚定且材料确认的案例
            # 优先计满（run77 c41：11 个不可读案例全计满 4 分，人工只认 3 个有效）。
            complete = complete + unverified_pool
            _verified_n = len(complete) - unverified_counted
            kept = complete[:max_items] if max_items else complete
            # 计入的未核验数（被封顶砍掉的不计入、不触发 need_human）——语义是
            # "有多少未核验案例的分数进了总分"，双分值/机器状态都以它为准。
            unverified_counted = sum(1 for c in kept if c.get("unverified"))
            effective = Decimal(len(kept))
            # R0 客观路由档表（"2个经验得1分；4个得2分"）：命中最高档，非 per_item 线性。
            # 形状守卫（2026-11-04）：只对**计数档**（min_count 形状）查表；金额档
            # （min_amount_yuan 形状，"按金额分段的每案例分值"）不是计数档，回退
            # per_item 线性——查表无命中强制 0 分曾让可计案例全归零（run79 回归）。
            _tshape = _tier_shape(params.get("tiers"))
            _use_tiers = _tshape == "count_tier"
            if _use_tiers:
                _ts = _tier_score(params.get("tiers"), effective)
                score = _ts if _ts is not None else Decimal(0)
            else:
                score = (effective * per_item).quantize(Decimal("0.01"))
            if max_score and score > max_score:  # 封顶：真实规则"最高得 M 分"
                score = max_score
            # 可复算双分值：剔除全部未核验案例后的分数（warnings 附给人工一眼复算）
            _kept_verified = min(_verified_n, max_items) if max_items else _verified_n
            if _use_tiers:
                _tsv = _tier_score(params.get("tiers"), Decimal(_kept_verified))
                score_verified_only = _tsv if _tsv is not None else Decimal(0)
            else:
                score_verified_only = (_kept_verified * per_item).quantize(Decimal("0.01"))
            if max_score and score_verified_only > max_score:
                score_verified_only = max_score
            detail = (
                f"逐案例核验：提供案例 {len(cases)} 个，其中可计 {len(complete)} 个"
                + (f"（{dropped_false} 个缺证明材料不计分）" if dropped_false else "")
                + (f"（{unverified_counted} 个案例材料不可读/未绑上，按未核验计入并标疑问，"
                   f"剔除后为 {score_verified_only} 分，建议人工核看）"
                   if unverified_counted else "")
                + (f"（{null_dropped} 个案例材料页不可读未计，可能为扫描件，建议人工核看）"
                   if null_dropped else "")
                + _type_gate_note
            )
            # 零分理由：unverified 案例已计入 → score>0，故下面只在回退模式/全 False/
            # 类型剔空时可达。诚实化（2026-11-04）：线性路径下案例**已计入**却算不出
            # 分值（per_item=0 的机器口径问题）绝不伪装"确认未提供"；计数档不命中
            # （低于最低档）是规则结论，走既有 confirmed_absent 映射。
            if score == 0:
                if kept and zero_reason is None and not _use_tiers and per_item <= 0:
                    zero_reason = "cases_unverified"
                    conflict = True
                    detail += "；案例已计入但未算出分值（机器口径问题，非确认未提供，转人工核看）"
                elif null_dropped:
                    zero_reason = "unreadable"
                elif dropped_false:
                    zero_reason = "confirmed_absent"
                elif _type_dropped:
                    # 类型门槛剔空（"类似运维"过滤掉全部案例）：0 分是类型判断
                    # 结论而非"未提供材料"——转人工核看（7.10 实证：集成/维保类
                    # 案例被剔后伪装 confirmed_absent/pass）
                    zero_reason = "type_gate"
            # LLM 案例核验批失败（scoring_service 注入）：案例清单可能不完整，
            # 是机器故障不是"确认没提供"——标 cases_unverified 转人工（surviving
            # 案例照计=计入+疑问，三态哲学）。
            _bf = int(criterion.get("case_batches_failed") or 0)
            _bt = int(criterion.get("case_batches_total") or 0)
            if _bf:
                conflict = True
                if unresolved is None:
                    unresolved = "cases_unverified"
                detail += (
                    f"；LLM 案例核验 {_bf}/{_bt or '?'} 批失败，案例清单可能不完整"
                    f"（按已核验 {len(kept)} 个计 {score} 分，建议人工核对或重跑）"
                )
                if score == 0 and zero_reason in (None, "confirmed_absent"):
                    zero_reason = "cases_unverified"
    elif artifacts:
        conflict = True
        detail += "；该评分项含证明材料要求，材料齐全性需人工核对原件（AI 逐案例核验未执行）"
        if score == 0 and zero_reason is None:
            zero_reason = "gate_named_missing"

    # 约定：score=0 必须给零分理由；score>0 不携带（避免下游误读）。
    if score != 0:
        zero_reason = None
    else:
        # LLM 案例核验批失败优先（2026-11-04 诚实化）：无论前面落到哪个分支
        # （含空 cases 的 no_facts 路径），机器故障都不得伪装"确认未提供/没抽到事实"。
        if int(criterion.get("case_batches_failed") or 0):
            zero_reason = "cases_unverified"
            if unresolved is None:
                unresolved = "cases_unverified"
        elif zero_reason is None:
            # 兜底：计数有依据的 0（如金额门槛过滤后 0 个、分档不命中）→ 确认未提供；
            # unresolved 机器侧故障（口径缺失/没抽到事实）→ no_facts/cases_unverified
            # 诚实细分（2026-11-04）：cases_unverified 是"核验没做完"，不得并进
            # no_facts 也不得伪装 confirmed_absent。
            if unresolved == "cases_unverified":
                zero_reason = "cases_unverified"
            elif unresolved in ("no_facts", "field_missing"):
                zero_reason = "no_facts"
            else:
                zero_reason = "confirmed_absent"

    return CountResult(
        score=score, detail=f"{detail}，得 {score} 分{inferred}", conflict=conflict,
        unresolved=unresolved,
        zero_reason=zero_reason,
        score_verified_only=score_verified_only if unverified_counted else None,
        unverified_counted=unverified_counted,
    )


def _required_flags(
    artifacts: str, person_scope: str = "", criterion_name: str = "", rule_text: str = "",
) -> tuple[str, ...]:
    """材料要求段 → 案例核验布尔键（**只闸细则点名的材料**，规则文本驱动）。

    项目级：见"合同"查合同；见"发票"查发票；见"验收/业主证明"查验收；
    细则未点名任何材料 → 不设材料闸门（旧实现无条件恒查 contract + 人员级
    无条件恒查 social_security，是按旧项目假设的隐性拟合，换项目就误杀——
    2026-10-26 run 54 T5.1 19 案例仅计 1 的放大器之一）。
    人员级：work_proof（人在材料表行出现）恒查——人员证据的存在性锚；
    experience_proof/social_security/cert_proof 仅细则点名时查。
    2026-10-22 修复：旧实现对人员级硬编码 contract → 全部"缺证明材料"→ 0 分。
    2026-11-03 R3（run78 驻厂运维人员 12/12 假通过实证）：
    - experience_proof 触发补「N年以上…经验」措辞（旧只认"五年/负责人"，
      "3年以上经验"漏闸）；
    - cert_proof 新 flag：细则点名证书（认证/培训证书/职称/PMP）是**评分条件**
      （"具有…证书并有…经验的人员每1人得1分"），条件词常在主句而非【证明材料】，
      故 rule_text 一并扫；不做宽松 overlay（work/experience 的宽松是防误杀，
      证书是硬条件）。
    """
    if person_scope:
        flags: list[str] = ["work_proof"]
        # 项目负责人经验要求"五年及以上工作经验证明"（独立文档）
        _exp_re = re.compile(r"\d+\s*年(?:以上|及以上)?[^。；]{0,12}(?:工作)?经验|经验证明")
        if (
            "负责人" in criterion_name or "五年" in artifacts or "5年" in artifacts
            or _exp_re.search(artifacts) or _exp_re.search(rule_text)
        ):
            flags.append("experience_proof")
        if re.search(r"认证|培训证书|职称|PMP", artifacts + "\n" + rule_text):
            flags.append("cert_proof")
        if "社保" in artifacts or "社会保险" in artifacts or "缴纳" in artifacts:
            flags.append("social_security")
        return tuple(flags)
    flags: list[str] = []
    if "合同" in artifacts or "中标通知书" in artifacts:
        flags.append("contract")
    if "发票" in artifacts:
        flags.append("invoice")
    if any(k in artifacts for k in ("验收", "业主证明", "用户证明", "用户报告")):
        flags.append("acceptance")
    if not flags and artifacts:
        # 细则点名了材料但关键词映射不上（"提供用户证书/查询截图"等长尾）→
        # **不能空闸门放行**（空 vals 会把材料全 False 的案例也算成可计 = 假通过，
        # 审查实锤）。退回基线合同闸门并在 detail/conflict 暴露；只有细则完全
        # 没点名材料（artifacts 为空）才不设闸门。
        flags.append("contract")
    return tuple(flags)


# 中文名 → 体系键（OCR 出的证书常只有中文名，完全没有 ISO 字样）
_CERT_ZH_MAP: tuple[tuple[str, str], ...] = (
    ("质量管理体系", "ISO9001"),
    ("信息安全管理体系", "ISO27001"),
    ("信息技术服务管理体系", "ISO20000"),
)
_CERT_GB_MAP: tuple[tuple[str, str], ...] = (
    ("GB/T19001", "ISO9001"), ("GB/T22080", "ISO27001"), ("GB/T24405", "ISO20000"),
)
_RE_ISO_NUM = re.compile(r"ISO(?:/?IEC)?(\d{4,5})")
_RE_GB_STD = re.compile(r"GB/?T(\d{4,5})")
_RE_CMMI = re.compile(r"CMMI")


def normalize_cert(name: str) -> str:
    """证书名归一到稳定键。

    `ISO/IEC 20000-1:2018`→ISO20000、`GB/T19001-2016/ISO9001:2015`→ISO9001、
    `质量管理体系认证证书`→ISO9001、`CMMI 五级`→CMMI。认不出体系时返回去符号的
    原名（供 cert_claims 与 certificates 的同一口径比对）。
    """
    raw = str(name or "")
    t = re.sub(r"\s+", "", raw).upper()
    if not t:
        return ""
    for zh, key in _CERT_ZH_MAP:
        if zh in raw:
            return key
    for gb, key in _CERT_GB_MAP:
        gb_n = gb.replace("/", "")
        if gb.replace("/", "") in t or gb_n in t:
            return key
    m = _RE_ISO_NUM.search(t)
    if m:
        return f"ISO{m.group(1)}"
    m = _RE_GB_STD.search(t)
    if m:
        gb = f"GB/T{m.group(1)}"
        for pat, key in _CERT_GB_MAP:
            if pat.replace("/", "") == gb.replace("/", ""):
                return key
    if _RE_CMMI.search(t):
        return "CMMI"
    return re.sub(r"[^0-9A-Z一-鿿]+", "", t)


def _cert_key(name: str) -> str:
    """兼容别名：旧调用点已全部迁移到 normalize_cert。"""
    return normalize_cert(name)


def _normalize_role(case: dict) -> str | None:
    """案例的人员归属归一：leader | member | None。

    模型输出形态多变（"Leader"/"项目负责人"/"leader（项目负责人）"），字符串
    精确比较 `"leader"` 会把合法案例整项清零（2026-10-21 实证：T1.2 四家全 0）。
    """
    role = str(case.get("role") or "").strip().lower()
    if not role:
        return None
    if "lead" in role or "负责人" in role:
        return "leader"
    if "member" in role or "成员" in role or "团队" in role:
        return "member"
    return None


# ===========================================================================
# P1-E 新计分原语（21 份浙江中烟语料客观分类型学缺口，docs/评分规则库改进方案.md §4）
#
# 统一约定（与 score_count 同构）：
# - **阈值/档位/扣分率只来自项目规则文本**（params，编译期 check_infer 解析）；
# - 投标人侧事实从 facts 确定性抽取，抽取到的值带原文片段（可审计），
#   启发式抽取的值一律 conflict=True → review_status=conflict_flagged 供人工复核；
# - 抽不到事实 → unresolved="no_facts"（机器没抽到 ≠ 0 分结论，照 0 分记但标疑问）。
# ===========================================================================

# 响应时间承诺："2 小时内到达现场" / "响应时间：4 小时"（优先带标签的首匹配）
_RE_HOURS_LABELED = re.compile(
    r"(?:响应时间|故障响应|到场时间|到达时间|响应时限)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:个)?\s*小时"
)
_RE_HOURS = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:个)?\s*小时(?:内|以内|之内)?(?:到达|到现场|到场|响应)"
)
# 质保期承诺："免费保修 3 年" / "质保期：36 个月" / "质保 3 年"
_RE_WARRANTY_LABELED = re.compile(
    r"(?:免费保修|质保期|保修期|质量保证期)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(年|个月|月)"
)
_RE_WARRANTY = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:年|个月|月)\s*(?:的)?(?:免费)?(?:保修|质保)"
)
# 技术偏离表定位与行状态
_RE_DEV_SECTION = re.compile(r"技术偏离表|技术响应偏离表|偏离表")
_RE_DEV_NEG = re.compile(r"负偏离|低于(?:要求|招标)|不满足")
_RE_DEV_MISSING = re.compile(r"未提供|无响应|未响应|未填写")


def _units_to_months(value: Decimal, unit: str) -> Decimal:
    return value * 12 if unit == "年" else value


# 承诺语境/偏离表语境判定（值抽取的语境分用，通用结构性判据非项目拟合）
_RE_COMMIT_CTX = re.compile(r"承诺|保证|书面承诺")
_RE_CLAUSE_SPLIT = re.compile(r"[。；;！!？?\n]")


def _deviation_regions(text: str) -> list[tuple[int, int]]:
    """偏离表/偏差表所在区间（表题起 4000 字符内到下一章节标题），同
    _extract_deviation_counts 的窗口口径。区间内出现的通用条款值要降序。"""
    regions: list[tuple[int, int]] = []
    for m in _RE_DEV_SECTION.finditer(text):
        tail = text[m.end(): m.end() + 4000]
        stop = re.search(r"\n\s*(?:表\s*\d|[一二三四五六]、|第[一二三四五六][章节部分])", tail)
        window = tail[: stop.start()] if stop else tail
        regions.append((m.end(), m.end() + len(window)))
    return regions


def _value_candidates(
    text: str, labeled: re.Pattern, plain: re.Pattern, to_value
) -> list[tuple[Decimal, str, int, int]]:
    """全量收集候选值：(值, 原文片段, 位置, 语境分)。

    2026-11-02 run77 实证：**全文首匹配**会被商务技术偏离表的通用条款
    （"派遣高级技术人员8 小时到达现场"）抢走，而正文服务承诺章节的正式承诺
    （"承诺远程服务团队在4 小时到达现场"）被无视 → 0 分误判。改为全量收集 +
    语境分排序：带标签 +3；与"承诺/保证"同句（句读切分）+2；落在偏离表
    区间内 −5（偏离表行是逐条响应格式文本，不是承诺函）。
    """
    out: list[tuple[Decimal, str, int, int]] = []
    regions = _deviation_regions(text)
    seen: set[tuple[Decimal, int]] = set()
    for regex, base in ((labeled, 3), (plain, 0)):
        for m in regex.finditer(text):
            try:
                value = to_value(m)
            except Exception:  # noqa: BLE001
                continue
            if value is None:
                continue
            key = (value, m.start())
            if key in seen:
                continue
            seen.add(key)
            score = base
            # 承诺同句：从上一个句读到本匹配的区间含"承诺/保证"
            prev = max(
                (mm.end() for mm in _RE_CLAUSE_SPLIT.finditer(text[: m.start()])),
                default=0,
            )
            if _RE_COMMIT_CTX.search(text[prev : m.start() + len(m.group(0))]):
                score += 2
            if any(lo <= m.start() < hi for lo, hi in regions):
                score -= 5
            out.append((value, m.group(0), m.start(), score))
    return out


def _pick_value(cands: list[tuple[Decimal, str, int, int]]) -> tuple[Decimal | None, str]:
    """按语境分取最优候选；同分取文本序最前。多值分歧并列进片段（conflict
    由调用方恒置 True，此处只保证分歧可见、可复算）。"""
    if not cands:
        return None, ""
    best_score = max(c[3] for c in cands)
    top = [c for c in cands if c[3] == best_score]
    value, snippet = top[0][0], top[0][1]
    distinct = sorted({c[0] for c in cands})
    if len(distinct) > 1:
        alts = "、".join(f"{v}（{s[:16]}）" for v, s, _p, _sc in cands[:4])
        snippet = f"{snippet}｜多值分歧：{alts}，取语境最优 {value}"
    return value, snippet


def _extract_response_hours(text: str, window: str | None = None) -> tuple[Decimal | None, str]:
    """投标人响应时间承诺（小时）。

    ``window``（索引指针/承诺章节解析出的文本）非空时只在窗口内收集——投标人的
    索引表自报"服务承诺…见投标文件（N-M）页"，窗口是材料自声明位置，权威且零拟合；
    窗口无匹配回退全文。返回 (值, 原文片段)。
    """
    src = window if (window and window.strip()) else (text or "")
    cands = _value_candidates(
        src, _RE_HOURS_LABELED, _RE_HOURS,
        lambda m: Decimal(m.group(1)),
    )
    if not cands and window and window.strip() and text:
        cands = _value_candidates(
            text, _RE_HOURS_LABELED, _RE_HOURS,
            lambda m: Decimal(m.group(1)),
        )
    return _pick_value(cands)


def _extract_warranty_months(text: str, window: str | None = None) -> tuple[Decimal | None, str]:
    """投标人质保期承诺（统一换算月）。窗口语义同 _extract_response_hours。"""

    def _to_months(m: re.Match) -> Decimal | None:
        try:
            if m.lastindex and m.lastindex >= 2 and m.group(2):
                return _units_to_months(Decimal(m.group(1)), m.group(2))
            raw = m.group(0)
            unit = "月" if "个月" in raw else "年"
            return _units_to_months(Decimal(m.group(1)), unit)
        except Exception:  # noqa: BLE001
            return None

    src = window if (window and window.strip()) else (text or "")
    cands = _value_candidates(src, _RE_WARRANTY_LABELED, _RE_WARRANTY, _to_months)
    if not cands and window and window.strip() and text:
        cands = _value_candidates(text, _RE_WARRANTY_LABELED, _RE_WARRANTY, _to_months)
    return _pick_value(cands)


def _extract_deviation_counts(text: str) -> tuple[dict | None, str]:
    """技术偏离表行状态计数。定位不到该表 → (None, "")。"""
    m = _RE_DEV_SECTION.search(text)
    if not m:
        return None, ""
    # 从表标题向后取窗口：到下一个明显章节标题/表题为止（最多 4000 字符）
    tail = text[m.end(): m.end() + 4000]
    stop = re.search(r"\n\s*(?:表\s*\d|[一二三四五六]、|第[一二三四五六][章节部分])", tail)
    window = tail[: stop.start()] if stop else tail
    rows = [ln for ln in window.splitlines() if re.search(r"\d", ln) and len(ln.strip()) >= 6]
    if not rows:
        return None, ""
    neg = sum(1 for ln in rows if _RE_DEV_NEG.search(ln))
    missing = sum(
        1 for ln in rows if _RE_DEV_MISSING.search(ln) and not _RE_DEV_NEG.search(ln)
    )
    return {"total": len(rows), "negative": neg, "missing": missing}, window[:160]


def _q2(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"))


def score_deviation_deduct_table(
    criterion: dict, params: dict, facts: BidFacts
) -> CountResult:
    """偏离表/清单逐条扣分（G9：缺失每条扣 N、负偏离每条扣 M，单项 0-20）。"""
    try:
        per_missing = Decimal(str(params.get("per_missing") or 0))
        per_negative = Decimal(str(params.get("per_negative") or 0))
    except Exception:  # noqa: BLE001
        return CountResult(
            Decimal(0), "偏离扣分参数非法（非数字）", True, unresolved="field_missing"
        )
    max_score = criterion.get("max_score")
    counts, snippet = _extract_deviation_counts(facts.text or "")
    if counts is None:
        return CountResult(
            Decimal(0),
            "未在投标文件中定位到技术偏离表，无法逐条扣分（按 0 分记，待人工核对）",
            True, unresolved="no_facts",
        )
    deduct = per_missing * counts["missing"] + per_negative * counts["negative"]
    if max_score is not None:
        deduct = min(deduct, Decimal(str(max_score)))
    score = max(Decimal(0), Decimal(str(max_score or 0)) - deduct)
    return CountResult(
        score,
        f"偏离表 {counts['total']} 行：缺失 {counts['missing']}×{per_missing} + "
        f"负偏离 {counts['negative']}×{per_negative} = 扣 {_q2(deduct)} → {score}；"
        f"窗口片段：{snippet[:60]}",
        conflict=True,  # 行状态是启发式判读 → conflict_flagged 供人工复核
    )


def score_value_tier(
    criterion: dict, params: dict, facts: BidFacts
) -> CountResult:
    """连续值→分值档位（G10：响应时间档 10 文件、质保年限档）。"""
    field = str(params.get("field") or "")
    try:
        tiers = list(params.get("tiers") or [])
    except Exception:  # noqa: BLE001
        tiers = []
    if not tiers or field not in ("response_hours", "warranty_months"):
        return CountResult(
            Decimal(0), f"值档位参数不完备（field={field!r}）", True,
            unresolved="field_missing",
        )
    # 窗口文本（索引指针/承诺章节解析）优先——criterion 由 scoring_service 注入
    # value_window_text（2026-11-02 run77：偏离表"8小时"首匹配抢掉承诺函"4小时"）
    _window = criterion.get("value_window_text") if isinstance(criterion, dict) else None
    if field == "response_hours":
        value, snippet = _extract_response_hours(facts.text or "", window=_window)
    else:
        value, snippet = _extract_warranty_months(facts.text or "", window=_window)
    if value is None:
        return CountResult(
            Decimal(0),
            f"未在投标文件中抽取到 {field} 的承诺值（按 0 分记，待人工核对）",
            True, unresolved="no_facts",
        )
    best: Decimal | None = None
    for t in tiers:
        try:
            score_t = Decimal(str(t.get("score")))
            lo = t.get("min_value")
            hi = t.get("max_value")
            if lo is not None and value < Decimal(str(lo)):
                continue
            if hi is not None and value > Decimal(str(hi)):
                continue
        except Exception:  # noqa: BLE001
            continue
        if best is None or score_t > best:
            best = score_t
    if best is None:
        return CountResult(
            Decimal(0),
            f"抽取到 {field}={value}（依据：{snippet[:40]}），但不落入任何档位 → 0 分",
            conflict=True,
        )
    return CountResult(
        best,
        f"{field}={value}（依据：{snippet[:40]}）命中档位 → {best} 分",
        conflict=True,  # 承诺值为文本启发式抽取 → 人工复核
    )


def score_value_linear(
    criterion: dict, params: dict, facts: BidFacts
) -> CountResult:
    """线性步进计分（G10 质保档："满足 1 年以外每增 12 个月 +1 分，封顶 2"）。

    params: {field, base, step, per, cap}——base/step/per/cap 全部来自项目细则原文。
    """
    field = str(params.get("field") or "")
    try:
        base = Decimal(str(params.get("base") or 0))
        step = Decimal(str(params.get("step") or 0))
        per = Decimal(str(params.get("per") or 0))
        cap = (
            Decimal(str(params.get("cap")))
            if params.get("cap") is not None else None
        )
    except Exception:  # noqa: BLE001
        return CountResult(
            Decimal(0), "线性计分参数非法（非数字）", True, unresolved="field_missing"
        )
    if step <= 0 or per <= 0 or field not in ("response_hours", "warranty_months"):
        return CountResult(
            Decimal(0), f"线性计分参数不完备（field={field!r}）", True,
            unresolved="field_missing",
        )
    _window = criterion.get("value_window_text") if isinstance(criterion, dict) else None
    if field == "warranty_months":
        value, snippet = _extract_warranty_months(facts.text or "", window=_window)
    else:
        value, snippet = _extract_response_hours(facts.text or "", window=_window)
    if value is None:
        return CountResult(
            Decimal(0),
            f"未在投标文件中抽取到 {field} 的承诺值（按 0 分记，待人工核对）",
            True, unresolved="no_facts",
        )
    steps = max(Decimal(0), (value - base) / step)
    score = (steps * per).to_integral_value(rounding="ROUND_FLOOR")
    if cap is not None:
        score = min(score, cap)
    score = max(Decimal(0), score)
    return CountResult(
        score,
        f"{field}={value}（依据：{snippet[:40]}），基线 {base} 每 {step} 计 {per} 分"
        f"封顶 {cap} → {score}",
        conflict=True,
    )


def score_attribute_tier(
    criterion: dict, params: dict, facts: BidFacts
) -> CountResult:
    """属性分档计数（G7/G8：16 文件 22 条）——每个对象命中满足的最高档，Σ 封顶。

    params: {attr: years|title|cert_name|count, tiers: [{min|level|match, score}]}
    事实源：cert_name/count → facts.certificates（现网可用）；
    years/title → facts.person_attributes（结构化人员表抽取，缺则 no_facts 不猜测）。
    """
    attr = str(params.get("attr") or "years")
    try:
        tiers = list(params.get("tiers") or [])
    except Exception:  # noqa: BLE001
        tiers = []
    if not tiers:
        return CountResult(
            Decimal(0), "属性分档参数缺 tiers", True, unresolved="field_missing"
        )
    max_score = criterion.get("max_score")

    if attr in ("cert_name", "count", "cert_level"):
        objects: list[str] = list(facts.certificates or [])
        if not objects:
            return CountResult(
                Decimal(0), "未在证书表/正文中抽取到证书清单（按 0 分记，待人工核对）",
                True, unresolved="no_facts",
            )
        # 证书背书三态软闸门（同 score_count_tier）：False 不计，None 计入+疑问
        _backing = criterion.get("cert_backing") or {}
        _unbacked = 0
        _unverified = 0
        if _backing:
            kept: list[str] = []
            for obj in objects:
                state = _backing.get(normalize_cert(str(obj)))
                if state is False:
                    _unbacked += 1
                    continue
                if state is None:
                    _unverified += 1
                kept.append(obj)
            objects = kept
            if not objects:
                return CountResult(
                    Decimal(0),
                    f"证书 {_unbacked} 项确认无背书页不计（可读文本确认缺失），0 分",
                    True,
                )
        total = Decimal(0)
        detail_parts: list[str] = []
        for obj in objects:
            best: Decimal | None = None
            for t in tiers:
                try:
                    score_t = Decimal(str(t.get("score")))
                except Exception:  # noqa: BLE001
                    continue
                # match_any（R0 cert_level 档："三级及以上"→["一级","二级","三级"]）
                # 等级词是中文专名，对原始名与归一名双向包含匹配；match 单键语义不变。
                cands = [str(t.get("match") or "")] + [
                    str(x) for x in (t.get("match_any") or [])
                ]
                hit = False
                for m in cands:
                    if not m:
                        continue
                    nm = normalize_cert(m)
                    if (nm and nm in normalize_cert(obj)) or (m in str(obj)):
                        hit = True
                        break
                if hit:
                    if best is None or score_t > best:
                        best = score_t
                elif attr == "count" and not any(cands):
                    if best is None or score_t > best:
                        best = score_t
            if best is not None:
                total += best
                detail_parts.append(f"{obj}→{best}")
        if max_score is not None:
            total = min(total, Decimal(str(max_score)))
        if not detail_parts:
            return CountResult(
                Decimal(0),
                f"证书 {len(objects)} 项均不命中任何分档（tiers 按名称匹配），0 分",
                True,
            )
        _note = ""
        if _unbacked:
            _note += f"（{_unbacked} 本证书确认无背书页不计）"
        if _unverified:
            _note += f"（{_unverified} 本证书背书页不可读，计入并标疑问）"
        return CountResult(
            total, "；".join(detail_parts) + f"，合计 {total}{_note}", True,
            unverified_counted=_unverified,
        )

    # years / title：人员属性表（facts.person_attributes，表解析/LLM 填充）
    attrs = list(getattr(facts, "person_attributes", None) or [])
    if not attrs:
        return CountResult(
            Decimal(0),
            "人员属性表未抽取到（年限/职称无法结构化核对），按 0 分记待人工核对",
            True, unresolved="no_facts",
        )
    total = Decimal(0)
    detail_parts = []
    for person in attrs:
        best = None
        for t in tiers:
            try:
                score_t = Decimal(str(t.get("score")))
            except Exception:  # noqa: BLE001
                continue
            ok = False
            if attr == "years" and t.get("min") is not None:
                try:
                    years = person.get("years")
                    if years is not None and Decimal(str(years)) >= Decimal(str(t["min"])):
                        ok = True
                except Exception:  # noqa: BLE001
                    ok = False
            elif attr == "title" and t.get("level"):
                if str(t["level"]) in str(person.get("title") or ""):
                    ok = True
            if ok and (best is None or score_t > best):
                best = score_t
        if best is not None:
            total += best
            detail_parts.append(f"{person.get('name', '?')}→{best}")
        else:
            detail_parts.append(f"{person.get('name', '?')}→未命中档位")
    if max_score is not None:
        total = min(total, Decimal(str(max_score)))
    return CountResult(total, "；".join(detail_parts) + f"，合计 {total}", False)


# 中文基数词（分档计数"两项得1分"句式用）
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 分档计数："（ISO三体系）两项得1分，三项得2分" / "提供2项得1分，3项得2分"
# 容忍"三项得得2分"（原文/OCR 双得，run 59 T5.2 实证）——得+ 允许重复
_RE_COUNT_TIER = re.compile(
    r"(一|二|两|三|四|五|六|七|八|九|十|\d+)\s*(?:项|个|本|张|类)"
    r"[^得。；]{0,8}?得+\s*(\d+(?:\.\d+)?)\s*分"
)


def score_count_tier(criterion: dict, params: dict, facts: BidFacts) -> CountResult:
    """分档计数（P3："ISO体系认证两项得1分，三项得2分"）——按对象计数命中最高档。

    params: {field: certificates|performance, tiers: [{min_count, score}]}
    与 score_count 的 per_item×count 不同：分数不是线性，而是"计数落在哪个区间"。
    只认编译期已结构化的 tiers（来自规则原文），不猜档位。
    """
    field = str(params.get("field") or "certificates")
    try:
        tiers = list(params.get("tiers") or [])
    except Exception:  # noqa: BLE001
        tiers = []
    if not tiers:
        return CountResult(
            Decimal(0), "分档计数参数缺 tiers", True, unresolved="field_missing"
        )
    max_score = criterion.get("max_score")
    _unbacked = 0
    conflict_unverified = False

    if field == "performance":
        amounts = list(getattr(facts, "performance_amounts", None) or [])
        if not amounts:
            return CountResult(
                Decimal(0), "未抽取到业绩清单（分档计数按 0 记，待人工核对）",
                True, unresolved="no_facts",
            )
        n = len(amounts)
    else:
        certs = list(facts.certificates or [])
        if not certs:
            return CountResult(
                Decimal(0), "未抽取到证书清单（分档计数按 0 记，待人工核对）",
                True, unresolved="no_facts",
            )
        # 证书背书三态软闸门（2026-11-02 P0-5）：True 计入；None 计入+疑问
        # （防截图文本层看不见→误杀）；False 不计入（可读文本确认无背书页）。
        # 无 backing 信息（旧链路/分类未跑）→ 照计 + conflict（不误杀）。
        _backing = criterion.get("cert_backing") or {}
        if _backing:
            counted = []
            _unbacked = 0
            _unverified = 0
            for name in certs:
                state = _backing.get(normalize_cert(str(name)))
                if state is False:
                    _unbacked += 1
                    continue
                counted.append(name)
                if state is None:
                    _unverified += 1
            certs = counted
            if _unverified:
                conflict_unverified = True
            else:
                conflict_unverified = False
        else:
            _unbacked = 0
            conflict_unverified = False
        n = len(certs)

    best: Decimal | None = None
    best_min = None
    for t in tiers:
        try:
            min_c = int(t.get("min_count"))
            score_t = Decimal(str(t.get("score")))
        except Exception:  # noqa: BLE001
            continue
        if n >= min_c and (best is None or score_t > best):
            best = score_t
            best_min = min_c
    if best is None:
        return CountResult(
            Decimal(0), f"计数 {n} 项不满足任何分档（tiers 按 min_count 匹配），0 分",
            conflict=True,
        )
    if max_score is not None:
        best = min(best, Decimal(str(max_score)))
    _note = ""
    if _unbacked:
        _note += f"（{_unbacked} 本证书确认无背书页不计）"
    if conflict_unverified:
        _note += f"（{_unverified} 本证书背书页不可读，计入并标疑问）"
    return CountResult(
        best,
        f"计数 {n} 项命中「≥{best_min} 项」档 → {best} 分{_note}",
        conflict=True,  # 计数来自 facts 抽取 → 人工复核
        unverified_counted=_unverified if conflict_unverified else 0,
    )
