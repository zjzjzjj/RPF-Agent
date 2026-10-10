"""Mock provider：确定性离线实现，供测试与无网环境跑通全链路。

它不是评委，是管路测试（RPF 的自我描述，同样适用）：
- 规则抽取：用正则从招标条款文本里抽结构（中文标书的编号体系是有规律的）。
- 主观评分：用字符 bigram 覆盖率对照档位判据文本，选最匹配档位，取档位中值。
两条路径都完全确定，同样的输入永远给同样的输出。
"""
from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal

from app.llm.base import LLMResult
from app.parsing.normalize import normalize_no_ws_with_map

# 提示词模板中的任务标记（对真实模型无害的标签，mock 用它分派行为）
TASK_RULE_EXTRACTION = "extract_rules"
TASK_SUBJECTIVE_SCORE = "score_subjective"

_RE_TASK = re.compile(r"\[\[TASK:([a-z_]+)\]\]")
_RE_DELIM = re.compile(r"<(\w+)>\n(.*?)\n</\1>", re.S)


def _longest_common_substring(a: str, b: str) -> str:
    """最长公共子串（动态规划）。输入已去空白；样例规模下开销可忽略。"""
    if not a or not b:
        return ""
    prev = [0] * (len(b) + 1)
    best_len = 0
    best_end = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len = cur[j]
                    best_end = i
        prev = cur
    return a[best_end - best_len : best_end]


class MockProvider:
    name = "mock"
    model = "mock-deterministic"
    # 本地确定性实现，不产生任何网络请求——数据分级策略按本地端点放行
    # （host="mock" 既不在白名单也不是 IP，曾导致 confidential 请求被误拒）
    host = "localhost"

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResult:
        task = self._task_of(system + "\n" + user)
        blocks = self._blocks(user)
        if task == TASK_RULE_EXTRACTION:
            payload = self._extract_rules(blocks)
        elif task == TASK_SUBJECTIVE_SCORE:
            payload = self._score_subjective(blocks)
        else:
            payload = {"error": f"mock 不支持的任务：{task}"}
        return LLMResult(
            text=json.dumps(payload, ensure_ascii=False),
            provider=self.name,
            model=self.model,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _task_of(prompt: str) -> str | None:
        m = _RE_TASK.search(prompt)
        return m.group(1) if m else None

    @staticmethod
    def _blocks(prompt: str) -> dict[str, str]:
        return {m.group(1): m.group(2) for m in _RE_DELIM.finditer(prompt)}

    # ------------------------------------------------------------------
    # 规则抽取（确定性正则）
    # ------------------------------------------------------------------
    def _extract_rules(self, blocks: dict[str, str]) -> dict:
        text = blocks.get("clause", "") or blocks.get("tender", "")
        criteria: list[dict] = []
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        counters = {"qualification": 0, "compliance": 0, "technical": 0, "commercial": 0}

        # 资格/符合性条目：（一）…… 或 （1）……，按父标题归类
        current_section = ""
        for ln in lines:
            if re.match(r"^[一二三四五六七八九十]+、", ln):
                current_section = ln
            m = re.match(r"^[（(]([一二三四五六七八九十\d]+)[）)]\s*(.+)$", ln)
            if not m:
                continue
            body = m.group(2)
            if "资格要求" in current_section or "投标人资格" in current_section:
                counters["qualification"] += 1
                criteria.append(self._veto_criterion(
                    body, category="qualification", code=f"qual-{counters['qualification']}"))
            elif "投标文件的编制" in current_section or "编制" in current_section:
                counters["compliance"] += 1
                criteria.append(self._veto_criterion(
                    body, category="compliance", code=f"comp-{counters['compliance']}"))

        # 价格分
        price = self._price_criterion(text)
        if price:
            criteria.append(price)

        # 技术分档位（表格行）
        for name, max_score in (("技术方案", 20), ("项目实施方案", 15), ("售后服务方案", 10)):
            levels = self._levels_for(text, name)
            if levels:
                counters["technical"] += 1
                criteria.append(
                    {
                        "code": f"tech-{counters['technical']}",
                        "category": "technical",
                        "name": name,
                        "max_score": max_score,
                        "score_type": "subjective",
                        "is_veto": False,
                        "description": None,
                        "levels": levels,
                        "quote": self._find_line(text, name),
                        "confidence": 0.9,
                    }
                )

        # 商务分（计数类）
        for name, max_score, per_item in (("类似业绩", 9, 3), ("体系认证", 6, 2)):
            ln = self._find_line(text, name)
            if ln:
                counters["commercial"] += 1
                params: dict = {"per_item": per_item, "max_items": max_score // per_item}
                if name == "类似业绩":
                    params["field"] = "performance"
                    m = re.search(r"不低于(\d+)万元", ln)
                    params["min_amount_yuan"] = int(m.group(1)) * 10_000 if m else 3_000_000
                else:
                    params["field"] = "certificates"
                criteria.append(
                    {
                        "code": f"comm-{counters['commercial']}",
                        "category": "commercial",
                        "name": name,
                        "max_score": max_score,
                        "score_type": "count",
                        "is_veto": False,
                        "description": ln,
                        "check_type": "count_at_least",
                        "params": params,
                        "quote": ln,
                        "confidence": 0.85,
                    }
                )
        return {"criteria": criteria}

    @staticmethod
    def _veto_criterion(body: str, category: str, code: str) -> dict:
        check_type = "human_only"
        params: dict = {}
        if "注册资本" in body:
            check_type = "value_in_range"
            m = re.search(r"不低于人民币([\d,]+)万元", body)
            if m:
                params = {"field": "registered_capital", "min": int(m.group(1).replace(",", "")) * 10_000}
        elif "资质" in body:
            check_type = "document_present"
            params = {"document": "信息系统集成及服务资质证书"}
        elif "业绩" in body:
            check_type = "count_at_least"
            m = re.search(r"至少(\d+)个", body)
            params = {"field": "performance", "min_count": int(m.group(1)) if m else 2}
        elif "失信" in body:
            check_type = "contains_phrase"
            # 短语必须取自原文（"未被列入失信被执行人名单"），否则投标人的声明
            # 无法命中——要求与声明用词不同的失败模式在样例里真实存在过。
            m = re.search(r"(未被列入[^（(。，；]+)", body)
            phrase = m.group(1).strip() if m else "未被列入失信被执行人名单"
            params = {"field": "credit_statement", "phrase": phrase}
        elif "保证金" in body:
            check_type = "document_present"
            m = re.search(r"人民币([\d,]+)万元", body)
            params = {"document": "投标保证金缴纳凭证",
                      "amount": int(m.group(1).replace(",", "")) * 10_000 if m else None}
        elif "投标函" in body:
            check_type = "document_present"
            params = {"document": "投标函及法定代表人授权委托书"}
        elif "报价" in body or "限价" in body:
            check_type = "math_relation"
            m = re.search(r"最高限价([\d,]+)万元", body)
            params = {"field": "bid_price",
                      "max": int(m.group(1).replace(",", "")) * 10_000 if m else None}
        name = re.sub(r"[，。；、].*$", "", body)[:40]
        return {
            "code": code,
            "category": category,
            "name": name or body[:20],
            "max_score": None,
            "score_type": "veto",
            "is_veto": True,
            "veto_consequence": "disqualify",
            "description": body,
            "check_type": check_type,
            "params": params,
            "quote": body,
            "confidence": 0.8,
        }

    @staticmethod
    def _price_criterion(text: str) -> dict | None:
        if "价格分" not in text:
            return None
        k = None
        m = re.search(r"平均值乘以([\d.]+)", text)
        if m:
            k = m.group(1)
        upper = None
        m2 = re.search(r"价格分=(\d+)[×x]", text)
        max_score = int(m2.group(1)) if m2 else None
        if "不超过40分" in text or "最高不超过" in text:
            upper = max_score
        return {
            "code": "auto-price",
            "category": "price",
            "name": "价格分",
            "max_score": max_score,
            "score_type": "formula",
            "is_veto": False,
            "description": text.strip()[:200],
            "formula_type": "benchmark_ratio",
            "benchmark_method": "average",
            "k": k,
            "upper_bound": upper,
            "rounding": 2,
            "quote": MockProvider._find_line(text, "价格分=40"),
            "confidence": 0.85,
        }

    @staticmethod
    def _levels_for(text: str, crit_name: str) -> list[dict]:
        """提取档位。兼容两种行形态，并处理**表格单元格折行**：

        PDF 里一行长判据会被排版切成多行（"…数据加密、" / "容灾备份等…"），
        按行匹配会把判据截断——截断的判据会污染后续所有对照与证据。
        因此：档位行开始后，把非档位行、非新评分项标题的行拼回该档位。
        """
        levels: list[dict] = []
        idx = text.find(crit_name)
        if idx < 0:
            return []
        segment = text[idx : idx + 2500]
        line_re = re.compile(
            r"^(优|良|一般|差)\s*[|｜]?\s*(\d+)\s*[-–—]\s*(\d+)\s*分?\s*[|｜]?\s*(.*)$"
        )
        current: dict | None = None
        for raw in segment.split("\n"):
            line = raw.strip()
            if not line:
                if current:
                    levels.append(current)
                    current = None
                continue
            m = line_re.match(line)
            if m:
                if current:
                    levels.append(current)
                current = {
                    "label": m.group(1),
                    "min": int(m.group(2)),
                    "max": int(m.group(3)),
                    "criteria": m.group(4).strip(),
                    "quote": line,
                }
                continue
            if current is not None and not re.match(r"^\d+\s*[、.]", line):
                # 折行续行 → 拼回当前档位（判据与引文都要完整）
                current["criteria"] += line
                current["quote"] += line
                continue
            if current:
                levels.append(current)
                current = None
        if current:
            levels.append(current)

        # 同名档位去重（同一页可能有多张评分表，取首个 = 该评分项的档位）
        seen: set[str] = set()
        out: list[dict] = []
        for lv in levels:
            if lv["label"] in seen:
                continue
            seen.add(lv["label"])
            out.append(lv)
        return out

    @staticmethod
    def _find_line(text: str, needle: str) -> str | None:
        for line in text.split("\n"):
            if needle in line:
                return line.strip()
        return None

    # ------------------------------------------------------------------
    # 主观评分（最长公共子串对照档位判据）
    # ------------------------------------------------------------------
    def _score_subjective(self, blocks: dict[str, str]) -> dict:
        bid_text = blocks.get("bid", "")
        levels_raw = blocks.get("levels", "")
        levels: list[dict] = []
        for line in levels_raw.split("\n"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4:
                levels.append({"label": parts[0], "min": float(parts[1]), "max": float(parts[2]),
                               "criteria": parts[3]})
        if not levels:
            return {"level_label": None, "score": None, "reasoning": "无档位信息", "evidence": [],
                    "confidence": 0.0, "insufficient_evidence": True}

        bid_nows, _ = normalize_no_ws_with_map(bid_text)
        best: dict | None = None
        best_lcs = ""
        for lv in levels:
            crit_nows, _ = normalize_no_ws_with_map(lv["criteria"])
            lcs = _longest_common_substring(crit_nows, bid_nows).strip()
            if len(lcs) > len(best_lcs):
                best, best_lcs = lv, lcs
        assert best is not None

        if len(best_lcs) < 12:
            # 没有任何一段逐字吻合 → 无证据不评分（与真实链路的降级一致）
            return {
                "level_label": best["label"],
                "score": None,
                "reasoning": "[mock] 与各档判据均无≥12字的逐字吻合片段。",
                "evidence": [],
                "confidence": 0.2,
                "insufficient_evidence": True,
            }

        score = Decimal(str((best["min"] + best["max"]) / 2)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
        ratio = len(best_lcs) / max(1, len(normalize_no_ws_with_map(best["criteria"])[0]))
        return {
            "level_label": best["label"],
            "score": str(score),
            "reasoning": (
                f"[mock] 与「{best['label']}」档判据的最长逐字吻合片段 {len(best_lcs)} 字，"
                f"占该档判据 {ratio:.0%}。"
            ),
            "evidence": [{"quote": best_lcs, "page_hint": None}],
            "confidence": round(min(0.95, 0.35 + ratio), 2),
            "insufficient_evidence": False,
        }
