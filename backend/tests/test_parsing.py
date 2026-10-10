"""解析层单测：中文编号、目录判据、条款树、归一化。"""
from __future__ import annotations

from app.parsing.base import ExtractedPage
from app.parsing.chapter_tree import build_chapter_tree
from app.parsing.normalize import normalize, normalize_with_map
from app.parsing.numbering import NumberingContext, cn_to_int, detect_marker, looks_like_toc


# ---------------------------------------------------------------------------
# 中文数字与编号识别
# ---------------------------------------------------------------------------
def test_cn_to_int():
    assert cn_to_int("一") == 1
    assert cn_to_int("十") == 10
    assert cn_to_int("十二") == 12
    assert cn_to_int("二十三") == 23
    assert cn_to_int("一百零三") == 103
    assert cn_to_int("一百一十") == 110
    assert cn_to_int("200") == 200
    assert cn_to_int("甲乙") is None


def test_detect_chapter_section_article():
    ctx = NumberingContext()
    m = detect_marker("第三章 评标办法", ctx)
    assert m is not None and m.kind == "chapter" and m.value == "3" and m.rest == "评标办法"
    assert m.restart is True

    m = detect_marker("第二节 投标人须知", ctx)
    assert m is not None and m.kind == "section" and m.value == "2"

    m = detect_marker("第十二条 投标文件的编制", ctx)
    assert m is not None and m.kind == "article" and m.value == "12"


def test_detect_level_markers():
    ctx = NumberingContext()
    assert detect_marker("一、投标人资格要求", ctx).kind == "level_a"
    assert detect_marker("（一）投标人须具有独立法人资格。", ctx).kind == "level_b"
    assert detect_marker("(三) 报价要求", ctx).kind == "level_b"
    assert detect_marker("1. 技术方案（20分）", ctx).kind == "num_1"
    assert detect_marker("（1）第一项要求", ctx).kind == "digit_bracket"
    assert detect_marker("① 第一点", ctx).kind == "circled"


def test_decimal_protection():
    """小数误判防护：'2.5 万元'不是编号；父编号未出现的多级数字不算编号。"""
    ctx = NumberingContext()
    # 数字后紧跟计量单位 → 不是编号
    assert detect_marker("2.5 万元", ctx) is None
    assert detect_marker("0.95倍", ctx) is None
    # 父编号未出现 → 不是编号
    assert detect_marker("2.3.1 评分标准", ctx) is None
    # 父编号出现后 → 是编号
    ctx.note_seen("2")
    ctx.note_seen("2.3")
    m = detect_marker("2.3.1 评分标准", ctx)
    assert m is not None and m.kind == "num_n" and m.value == "2.3.1"


def test_num_1_vs_decimal():
    ctx = NumberingContext()
    m = detect_marker("1. 技术方案", ctx)
    assert m is not None and m.kind == "num_1"
    # "1.5 米" 会被 _RE_NUM_N 捕获，父编号 "1" 未出现 → 拒绝
    assert detect_marker("1.5 米", ctx) is None


# ---------------------------------------------------------------------------
# 目录页判据（回归：内容页不得被误判为目录页）
# ---------------------------------------------------------------------------
def test_content_page_not_toc():
    """资格要求 + 编制要求这样的条款列表页不是目录页。"""
    lines = [
        "第二章 投标人须知",
        "一、投标人资格要求",
        "（一）投标人须具有独立法人资格，注册资本不低于人民币1000万元。",
        "（二）投标人须具有信息系统集成及服务资质二级及以上证书。",
        "（三）投标人近三年须具有至少2个合同金额不低于300万元的类似项目业绩。",
        "（四）投标人未被列入失信被执行人名单。",
        "二、投标文件的编制",
        "（一）投标文件须包含投标函及法定代表人授权委托书。",
        "（二）投标人须提交投标保证金人民币5万元。",
        "（三）投标报价须唯一且不超过最高限价。",
    ]
    assert looks_like_toc(lines) is False


def test_real_toc_detected():
    lines = [
        "目 录",
        "第一章 招标公告 ................................ 1",
        "第二章 投标人须知 .............................. 3",
        "第三章 评标办法 ................................ 4",
        "第四章 合同条款 ................................ 5",
        "第五章 投标文件格式 ............................ 6",
        "（一）价格分 ................................... 4",
        "（二）技术分 ................................... 4",
        "（三）商务分 ................................... 4",
        "1. 技术方案 .................................... 4",
    ]
    assert looks_like_toc(lines) is True


# ---------------------------------------------------------------------------
# 条款树
# ---------------------------------------------------------------------------
def _page(text: str, page_no: int = 1) -> ExtractedPage:
    return ExtractedPage(page_no=page_no, text=text, width=595, height=842)


def test_chapter_tree_nesting():
    text = "\n".join(
        [
            "第一章 总则",
            "一、目的",
            "（一）为规范项目管理，制定本办法。",
            "（二）本办法适用于本项目。",
            "二、适用范围",
            "第二章 评标办法",
            "一、评标方法",
            "（一）价格分（40分）",
            "1. 基准价计算",
            "2. 价格分计算",
            "（二）技术分（45分）",
            "1. 技术方案（20分）",
        ]
    )
    nodes, warnings, _ranges = build_chapter_tree("t1", [_page(text)])
    by_id = {n.node_id: n for n in nodes}

    # 章节层级
    assert by_id["t1:root:C1"].kind == "chapter"
    assert by_id["t1:root:C2"].page == 1
    # （一）挂在"一、目的"下
    assert by_id["t1:root:C1:I1:II1"].parent_node_id == "t1:root:C1:I1"
    # "1. 基准价计算" 挂在"（一）价格分"下（深度：level_b=5 < num_1=6）
    assert by_id["t1:root:C2:I1:II1:N1"].parent_node_id == "t1:root:C2:I1:II1"
    # "（二）技术分"下的 "1. 技术方案" 不能挂到"（一）"下
    assert by_id["t1:root:C2:I1:II2:N1"].parent_node_id == "t1:root:C2:I1:II2"
    assert warnings == []


def test_chapter_tree_dedup_ids():
    """附件重启编号：同父下重复编号也要生成唯一 id。"""
    text = "\n".join(
        [
            "第一章 正文",
            "一、第一条要求",
            "第二章 附件",
            "一、附件中的第一条要求",
        ]
    )
    nodes, _warnings, _ranges = build_chapter_tree("t1", [_page(text)])
    ids = [n.node_id for n in nodes]
    assert len(ids) == len(set(ids)), "node_id 必须全局唯一"


def test_subtree_text_and_char_ranges():
    text = "第一章 总则\n一、目的\n（一）本项目的目的是建设政务云平台。\n第二章 评标办法\n一、评标方法\n（一）价格分（40分）\n评标基准价的计算：算术平均值乘以0.95。"
    nodes, _warnings, ranges = build_chapter_tree("t1", [_page(text)])
    from app.parsing.chapter_tree import subtree_text

    sub = subtree_text(nodes, "t1:root:C2")
    assert "算术平均值乘以0.95" in sub
    assert 1 in ranges


# ---------------------------------------------------------------------------
# 归一化（定位与核验共用）
# ---------------------------------------------------------------------------
def test_normalize_fullwidth_and_quotes():
    assert normalize("投标报价：７，６００，０００元") == "投标报价:7,600,000元"
    assert normalize("“信用中国”网站") == '"信用中国"网站'
    assert normalize("ＡＢＣ　ｄｅｆ") == "ABC def"
    assert normalize("第一行\n第二行") == "第一行 第二行"


def test_normalize_with_map():
    text = "ＡＢ\nＣ"
    norm, index_map = normalize_with_map(text)
    assert norm == "AB C"
    # 归一化第 0 个字符 'A' 来自原文下标 0；空格来自原文下标 2（'\n'）；'C' 来自下标 3
    assert index_map[0] == 0
    assert index_map[1] == 1
    assert index_map[2] == 2
    assert index_map[3] == 3


def test_normalize_zero_width():
    assert normalize("投标​文件") == "投标文件"
