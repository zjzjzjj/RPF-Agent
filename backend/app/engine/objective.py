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

    if check_type == "contains_phrase":
        phrase = str(params.get("phrase") or "")
        if phrase and phrase in facts.text:
            return PrimitiveResult("pass", f"已找到声明「{phrase}」")
        return PrimitiveResult("fail", f"投标文件中未找到声明「{phrase}」")

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
_CERT_TITLE_RE = re.compile(r"查询(?:结果)?截图|查询网址|查询地址|证书查询|认证结果查询")
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
            if not s or len(s) > 60 or s.count("....") >= 2:
                continue
            # 要求复述行（招标文件索引表常整页复述"须提供…截图，未提供不得分"）
            # 不算投标人提供了截图：同时出现"须/应/需"与"提供/不得分"即视为要求句
            if any(k in s for k in ("须", "应", "需")) and (
                "提供" in s or "不得分" in s
            ):
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
        # ISO 三体系与 CMMI 分开计数（本项目认证项的计分结构：
        # ISO9001/27001/20000 每项 1 分上限 3，CMMI 二级以上 1 分）。
        # 证书名必须先归一化：OCR 形态（ISO/IEC 20000-1:2018、中文证书名）与
        # 同一体系的两种写法（GB/T19001 与 质量管理体系）旧实现全部匹配不上。
        iso_set: set[str] = set()
        has_cmmi = False
        for cert in facts.certificates:
            key = normalize_cert(cert)
            if key in ("ISO9001", "ISO27001", "ISO20000"):
                iso_set.add(key)
            elif key == "CMMI":
                has_cmmi = True
        count = len(iso_set) + (1 if has_cmmi else 0)
        # 一条都没抽到 ≠ 一条都不合格：前者是抽取能力问题，必须与"确实 0 项"区分开，
        # 否则 0 分会被当成评分结论进入总分。
        if not facts.certificates and not facts.cert_claims:
            detail = "未提取到证书清单（需人工核查）"
            unresolved = "no_facts"
        else:
            detail = f"提供证书 {count} 项（ISO 三体系 {len(iso_set)} 项" + (
                "、CMMI 1 项）" if has_cmmi else "）"
            )
            if conflict:
                detail += "；正文声称的认证中有未附证书复印件的（待人工核查）"
    elif field == "performance":
        min_amount = Decimal(str(params.get("min_amount_yuan") or 0))
        count = len([a for a in facts.performance_amounts if a >= min_amount])
        if not facts.performance_amounts:
            detail = "未提取到业绩清单（需人工核查）"
            unresolved = "no_facts"
        else:
            detail = f"满足金额要求的业绩 {count} 个"
    else:
        count = 0
        detail = "未知计数字段（评分项未给出 field，且无法从名称安全推断）"
        unresolved = "field_missing"

    effective = min(Decimal(count), Decimal(max_items)) if max_items else Decimal(count)
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
        count = 1 if has_cmmi else 0
        effective = min(Decimal(count), Decimal(max_items)) if max_items else Decimal(count)
        score = (effective * per_item).quantize(Decimal("0.01"))
        if max_score and score > max_score:
            score = max_score
        detail = (
            f"未见认证平台查询结果截图节：ISO 三体系 {iso_count} 项按"
            "「未按要求提供不得分」计 0"
            + ("；CMMI 1 项（仅需证书复印件）照计" if has_cmmi else "")
        )

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
        else:
            cases = clean_cases
            # 已有逐案例核验结果 = 计数有依据；清掉"字段缺失/未抽到事实"的 unresolved
            unresolved = None
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
            required_flags = _required_flags(artifacts)
            # 三态材料：True=确认有；False=确认无（规则"不全不得分"→不计）；
            # None=页不可读/未提供。None 不直接当 0——"机器看不见 ≠ 没有"
            # （与确定性路径同一哲学）。仅当确定性业绩表依据 + 合同已确认 +
            # 策略允许时计入并标疑问，避免"发票没 OCR 就整项清零"。
            complete: list[dict] = []
            null_counted = 0
            null_policy = config.case_null_policy()
            for c in cases:
                vals = {k: c.get(k) for k in required_flags}
                if any(v is False for v in vals.values()):
                    continue  # 确认缺失 → 不计
                if all(v is True for v in vals.values()):
                    complete.append(c)  # 全部确认 → 计
                    continue
                if (
                    c.get("case_source") == "table"
                    and vals.get("contract") is True
                    and null_policy == "count_with_contract"
                ):
                    complete.append(c)
                    null_counted += 1
                    conflict = True
            effective = min(Decimal(len(complete)), Decimal(max_items)) if max_items else Decimal(len(complete))
            score = (effective * per_item).quantize(Decimal("0.01"))
            if max_score and score > max_score:  # 封顶：真实规则"最高得 M 分"
                score = max_score
            incomplete = len(cases) - len(complete)
            detail = (
                f"逐案例核验：提供案例 {len(cases)} 个，其中可计 {len(complete)} 个"
                + (f"（{incomplete} 个缺证明材料不计分）" if incomplete else "")
                + (f"（{null_counted} 个材料页不可读，按已提供计并标疑问）" if null_counted else "")
            )
            if incomplete:
                conflict = True
    elif artifacts:
        conflict = True
        detail += "；该评分项含证明材料要求，材料齐全性需人工核对原件（AI 逐案例核验未执行）"

    return CountResult(
        score=score, detail=f"{detail}，得 {score} 分{inferred}", conflict=conflict,
        unresolved=unresolved,
    )


def _required_flags(artifacts: str) -> tuple[str, ...]:
    """材料要求段 → 案例核验布尔键：合同恒查；见"发票"查发票；见"验收/业主证明"查验收。"""
    flags: list[str] = ["contract"]
    if "发票" in artifacts:
        flags.append("invoice")
    if "验收" in artifacts or "业主证明" in artifacts:
        flags.append("acceptance")
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
