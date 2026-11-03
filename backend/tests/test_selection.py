"""公共选段模块（select_chunks_by_tier / split_text_chunks / derive_keywords）。"""
from __future__ import annotations

from app.engine.selection import (
    derive_keywords,
    select_chunks_by_tier,
    select_chunks_for_criterion,
    split_text_chunks,
)


def _mk_chunks(n: int, size: int = 100, filler: str = "正") -> list[tuple[int, str]]:
    return [(i, filler * size) for i in range(n)]


def test_under_budget_returns_all() -> None:
    chunks = _mk_chunks(5)
    picked, meta = select_chunks_by_tier(chunks, (("k", ("正",)),), 10_000)
    assert picked == chunks and meta["truncated"] is False and meta["chars"] == meta["total_chars"]


def test_keyword_chunks_and_neighbors_picked() -> None:
    # 10 块，只有第 8 块（尾部）含"售后服务"；预算 700 容纳命中块 + 相邻块
    chunks = [(i, "正" * 200) for i in range(10)]
    chunks[8] = (8, "售后服务方案很完善" + "正" * 190)
    picked, meta = select_chunks_by_tier(
        chunks, (("criterion", ("售后服务",)),), 700)
    ids = [i for i, _t in picked]
    assert 8 in ids, ids          # 关键词块入选（在尾部，不再被头部截断丢掉）
    assert 7 in ids or 9 in ids, ids  # 相邻块沾光入选
    assert meta["truncated"] is True


def test_budget_hard_limit() -> None:
    chunks = [(i, "正" * 300) for i in range(10)]
    picked, meta = select_chunks_by_tier(chunks, (("k", ("正",)),), 900)
    assert meta["chars"] <= 900
    assert all(len(t) <= 300 for _i, t in picked)


def test_order_preserved() -> None:
    # 命中块乱序出现（idx 2 与 idx 7），预算装得下 → 输出必须仍按原序
    chunks = [(i, "正" * 100) for i in range(10)]
    chunks[7] = (7, "售后保障" + "正" * 90)
    chunks[2] = (2, "售后承诺" + "正" * 90)
    picked, _meta = select_chunks_by_tier(chunks, (("k", ("售后",)),), 100_000)
    assert [i for i, _t in picked] == list(range(10))  # 全量且序不变


def test_diffuse_fallback_when_no_keywords() -> None:
    # 无任何关键词命中 + fallback_diffuse → 头尾散布而非只装头部
    chunks = _mk_chunks(20, size=200)
    picked, meta = select_chunks_by_tier(chunks, (("k", ("没有这个词",)),), 2000,
                                         fallback_diffuse=True)
    ids = [i for i, _t in picked]
    assert 0 in ids and 19 in ids, ids  # 头尾都有
    assert meta["chars"] <= 2000


def test_split_text_chunks() -> None:
    text = "\n".join(f"第{i}行 内容" * 10 for i in range(300))
    chunks = split_text_chunks(text, chunk_size=2000)
    assert sum(len(t) for _i, t in chunks) >= len(text) - 300  # 行边界切，不丢正文（换行归一损失）
    assert all(len(t) <= 4000 for _i, t in chunks)  # 无换行超长兜底硬切


def test_derive_keywords() -> None:
    kws = derive_keywords({
        "name": "售后服务方案",
        "required_evidence": "售后服务承诺函, 响应时间表、培训计划",
    })
    assert "售后服务方案" in kws
    assert "售后服务" in kws          # 去后缀主干
    assert "售后服务承诺函" in kws    # required_evidence 片段
    assert len(kws) == len(set(kws))  # 去重


def test_derive_keywords_empty_falls_back() -> None:
    assert derive_keywords({"name": "评分项"}) != ()
    assert derive_keywords({"name": ""}) == ()


def test_index_text_weight_toc_vs_content():
    """目录/索引页降权（结构性判据，2026-10-27 run 58 目录中毒实证）。"""
    from app.engine.selection import index_text_weight

    toc = "\n".join(
        ["服务团队技术力量（20分）见投标文件（63~131）页",
         "投标方案评析（10分）………132",
         "评析（10分）详见第173页",
         "应急预案………136"])
    assert index_text_weight(toc) <= 0.4  # 强索引 → 封顶档
    content = (
        "驻场服务团队由项目经理1名、运维工程师6名组成，实行AB角轮岗。"
        "月度巡检计划表见附件，故障响应承诺30分钟内到达现场并出具处理单。")
    assert index_text_weight(content) == 1.0
    assert index_text_weight("") == 1.0


def test_tier_selection_prefers_content_over_index(monkeypatch):
    """同一关键词：索引 chunk 分数不得压过正文 chunk。"""
    from app.engine.selection import split_text_chunks, select_chunks_by_tier

    # 3 个 chunk：正文命中1词、索引命中多词（逐字含评分项名+见投标文件页引用）
    text = (
        "驻场服务团队技术力量由项目经理和运维工程师组成，分工明确。\n"
        + "x" * 600
        + "服务团队技术力量（20分）见投标文件（63~131）页\n服务团队健全合理性（5分）………64\n"
        + "服务团队技术力量职责分工（10分）详见第70页\n服务团队（20）………71\n"
        + "y" * 600)
    chunks = split_text_chunks(text, chunk_size=400)
    picked, meta = select_chunks_by_tier(
        chunks, (("scoring", ("服务团队", "技术力量", "健全")),), budget=1200)
    picked_text = "\n".join(t for _i, t in picked)
    assert "驻场服务团队技术力量由项目经理" in picked_text  # 正文入选


def test_vector_selection_downweights_toc_chunks(monkeypatch):
    """vector 路径目录降权：确定性向量下目录块余弦 1.0 高于正文 0.8，
    降权后正文块入选、目录块不入选（2026-10-29 run 60 补：服务器
    BID_EVAL_SELECTION=vector，此前只有关键词/证据地图路径有此降权）。"""
    from app.evidence import embed_client
    from app.engine.selection import select_chunks_by_vector

    def pad(s: str) -> str:
        return s + "x" * (100 - len(s))

    # 5 块各 100 字符；目录块(4)与正文块(2)远离，防目录块当选后把正文
    # 当邻居捎带入选。预算 220 只装得下 主命中块 + 1 邻居（100+100）。
    toc = pad("\n".join([
        "驻场服务团队技术力量（20分）见投标文件（63~131）页",
        "服务团队健全合理性（5分）………64",
        "团队职责分工（10分）详见第70页",
        "服务承诺（5分）………71",
    ]))
    chunks = [
        (0, pad("无关填充块零")),
        (1, pad("无关填充块一")),
        (2, pad("驻场服务团队由项目经理1名、运维工程师6名组成，AB角轮岗。")),
        (3, pad("无关填充块三")),
        (4, toc),
    ]
    assert all(len(t) == 100 for _i, t in chunks)

    def fake_embed_batch(texts):
        out = []
        for t in texts:
            if "见投标文件" in t:
                out.append([1.0, 0.0])    # 目录块：与查询完全对齐，余弦 1.0
            elif "驻场服务团队由项目经理" in t:
                out.append([0.8, 0.6])    # 正文块：余弦 0.8，与目录接近（已 L2 归一）
            else:
                out.append([0.0, 1.0])    # 无关块：余弦 0
        return out

    monkeypatch.setattr(embed_client, "embed_one", lambda t: [1.0, 0.0])
    monkeypatch.setattr(embed_client, "embed_batch", fake_embed_batch)

    out = select_chunks_by_vector(chunks, "驻场服务团队技术力量", budget=220)
    assert out is not None
    picked, meta = out
    picked_text = "\n".join(t for _i, t in picked)
    assert "驻场服务团队由项目经理" in picked_text   # 正文入选
    assert "见投标文件" not in picked_text           # 目录块被降权后排不进预算
    assert meta["ids"] == [1, 2], meta["ids"]  # 正文 + 左邻居


def test_ocr_config_defaults(monkeypatch) -> None:
    """OCR 护栏默认值：60 页会截断 334 页大标书（run 60 实证跳过 92 页
    扫描件），默认 350；材料救援 20→60。服务器可经 .env 单键回退。"""
    from app import config

    monkeypatch.delenv("BID_EVAL_OCR_MAX_PAGES", raising=False)
    monkeypatch.delenv("BID_EVAL_MATERIAL_OCR_MAX_PAGES", raising=False)
    assert config.ocr_max_pages() == 350
    assert config.material_ocr_max_pages() == 60


# ---------------------------------------------------------------------------
# 证据地图：阈值与加成分离 + 复述块降权（2026-10-29 run 63 云锦汇智 c49 实证）
# ---------------------------------------------------------------------------
def test_evidence_map_boost_does_not_starve_neighbors(monkeypatch):
    """类内加成只影响种子排序，不得抬高 floor_extend 饿死真实内容簇的邻居。

    run 63 根因链：chunk_cats 加成乘进 scores → top 抬高 → 相对阈值
    floor_seed/floor_extend 连锁抬升 → 邻居（原始 0.5~0.66）全够不着 →
    簇退化单块 → top-3 预算全被抬分的通用块占住 → 模型无证据弃权 0 分。
    """
    from app.evidence import embed_client
    from app.engine.selection import build_evidence_map

    criterion = {
        "name": "订单管理功能",
        "description": "订单管理模块需支持订单全生命周期管理，含状态同步与异常回执。",
        "category": "technical",
        "max_score": "5",
    }
    # 余弦：核心内容 0.64 > 类内通用块 0.55（加成后 0.715 反超）> 邻居 0.50/0.52
    cos_by_marker = {
        "核心内容块": 0.64,
        "类内通用块": 0.55,
        "相邻扩展块乙": 0.52,
        "相邻扩展块甲": 0.50,
    }
    chunks = [
        (0, "无关填充块零" + "x" * 60),
        (1, "核心内容块：订单创建、审批流转、状态同步与异常回执均已实现。" + "x" * 40),
        (2, "相邻扩展块甲：异常回执的通知渠道与重试策略说明。" + "x" * 50),
        (3, "相邻扩展块乙：状态同步的接口协议与幂等处理。" + "x" * 50),
        (4, "无关填充块四" + "x" * 60),
        (5, "类内通用块：运维服务体系与巡检计划概述。" + "x" * 50),
        (6, "无关填充块六" + "x" * 60),
        (7, "无关填充块七" + "x" * 60),
    ]
    chunk_cats = [None, None, None, None, None, "technical", None, None]

    def fake_embed_batch(texts):
        out = []
        for t in texts:
            s = next((v for m, v in cos_by_marker.items() if m in t), 0.1)
            out.append([s, 0.0])  # qvec=[1,0] → 余弦 = s
        return out

    monkeypatch.setattr(embed_client, "embed_one", lambda t: [1.0, 0.0])
    monkeypatch.setattr(embed_client, "embed_batch", fake_embed_batch)

    emap = build_evidence_map(chunks, criterion, chunk_cats=chunk_cats)
    assert emap is not None
    clusters, _q = emap
    # 核心内容簇不止 1 块（扩展按 raw 判定：0.50/0.52 ≥ 0.64×0.75=0.48）
    core = next(cl for cl in clusters if 1 in cl["ids"])
    assert {2, 3} <= set(core["ids"]), core
    assert len(core["ids"]) >= 2, core
    # 真实内容簇进入 top-3 预算（suggest_score_hierarchical 只取前 3 簇）
    top3_ids = [i for cl in clusters[:3] for i in cl["ids"]]
    assert 1 in top3_ids
    # 类内加成语义保留在展示分：0.55×1.3=0.715
    boosted = next(cl for cl in clusters if 5 in cl["ids"])
    assert boosted["score"] >= 0.71, boosted


def test_evidence_map_restatement_chunk_downweighted(monkeypatch):
    """逐字复述 criteria 文本的块经 criterion_restatement_weight 后不得成为 top 簇种子。

    run 63 实证：p38 应答表逐字照抄评分标准（余弦 0.669、目录降权 1.00 未拦），
    在选段路径冒充证据——复述块降权后种子应是真实内容块。
    """
    from app.evidence import embed_client
    from app.engine.selection import (
        build_evidence_map,
        criterion_restatement_weight,
    )

    crit_desc = "订单管理模块需支持订单全生命周期管理，含状态同步与异常回执。"
    criterion = {"name": "订单管理功能", "description": crit_desc, "max_score": "5"}
    restatement = "订单管理（应答）：" + crit_desc
    content = "系统实测：订单创建到回执归档共 12 步，异常单自动重推三次后转人工。"
    chunks = [
        (0, restatement),
        (1, "无关填充块一" + "z" * 60),
        (2, content),
        (3, "无关填充块三" + "z" * 60),
        (4, "无关填充块四" + "z" * 60),
    ]

    def fake_embed_batch(texts):
        out = []
        for t in texts:
            if "订单管理（应答）" in t:
                out.append([0.9, 0.0])   # 复述块余弦最高（run 63 p38 同型）
            elif "订单创建到回执归档" in t:
                out.append([0.6, 0.0])
            else:
                out.append([0.1, 0.0])
        return out

    monkeypatch.setattr(embed_client, "embed_one", lambda t: [1.0, 0.0])
    monkeypatch.setattr(embed_client, "embed_batch", fake_embed_batch)

    # 判据本身：复述块降权 0.25，真实内容块不受影响
    assert criterion_restatement_weight(restatement, criterion) == 0.25
    assert criterion_restatement_weight(content, criterion) == 1.0

    emap = build_evidence_map(chunks, criterion)
    assert emap is not None
    clusters, _q = emap
    # 复述块降权（0.9×0.25=0.225 < 0.6×0.55=0.33）→ 不得成任何簇种子；
    # top 簇是真实内容块
    assert clusters[0]["ids"] == [2], clusters
    assert all(0 not in cl["ids"] for cl in clusters), clusters


def test_vector_selection_restatement_downweighted(monkeypatch):
    """select_chunks_by_vector 路径复述块降权生效：预算只装得下内容块时，
    余弦更高的复述块不得挤掉真实内容块。"""
    from app.evidence import embed_client
    from app.engine.selection import select_chunks_by_vector

    crit_desc = "订单管理模块需支持订单全生命周期管理，含状态同步与异常回执。"
    criterion = {"name": "订单管理功能", "description": crit_desc, "max_score": "5"}

    def pad(s: str) -> str:
        return s + "x" * (100 - len(s))

    chunks = [
        (0, pad("无关填充块零")),
        (1, pad("无关填充块一")),
        (2, pad("系统实测订单创建到回执归档共12步，异常单自动重推。")),
        (3, pad("无关填充块三")),
        (4, pad("订单管理（应答）：" + crit_desc)),
    ]
    assert all(len(t) == 100 for _i, t in chunks)

    def fake_embed_batch(texts):
        out = []
        for t in texts:
            if "订单管理（应答）" in t:
                out.append([0.9, 0.0])   # 复述块余弦最高
            elif "订单创建到回执归档" in t:
                out.append([0.7, 0.0])   # 真实内容块次高
            else:
                out.append([0.1, 0.0])
        return out

    monkeypatch.setattr(embed_client, "embed_one", lambda t: [1.0, 0.0])
    monkeypatch.setattr(embed_client, "embed_batch", fake_embed_batch)

    # 预算 220 只装得下 内容块 + 1 邻居；复述块降权 0.9×0.25=0.225 < 0.7 后
    # 内容块先入选（旧语义复述块 0.9 首选，内容块被挤出预算）
    out = select_chunks_by_vector(
        chunks, "订单管理功能", budget=220, criterion=criterion)
    assert out is not None
    picked, meta = out
    picked_text = "\n".join(t for _i, t in picked)
    assert "订单创建到回执归档" in picked_text
    assert "订单管理（应答）" not in picked_text
    assert 2 in meta["ids"]


def test_restatement_weight_mixed_block_not_downweighted():
    """"前半引用细则 + 后半真实方案"的混合块不得被整块降权。

    2026-10-29 审查 P2：反向包含（细则⊆块正文）曾把混合块降 0.25——
    唯一实质证据被踢出证据簇，恰好复现"无证据弃权 0 分"的失败模式。
    二元组占比天然处理：混合块随真实内容占比上升自然豁免。
    """
    from app.engine.selection import criterion_restatement_weight

    crit_desc = "订单管理模块需支持订单全生命周期管理，含状态同步与异常回执。"
    criterion = {"name": "订单管理功能", "description": crit_desc, "max_score": "5"}
    pure = "订单管理（应答）：" + crit_desc
    mixed = (
        crit_desc
        + "我司实现方案：订单创建到回执归档共 12 步，异常单自动重推三次后转人工，"
        + "并配套监控告警与月度对账机制，已在我司承建的三个同类项目上线运行。"
    )
    assert criterion_restatement_weight(pure, criterion) == 0.25
    assert criterion_restatement_weight(mixed, criterion) == 1.0


def test_keyword_selection_restatement_downweighted(monkeypatch):  # noqa: ANN001
    """关键词路径（缺省模式/嵌入熔断回退）复述块降权：逐字含全部关键词的
    复述应答表不得霸占预算，真实内容块入选。

    2026-10-29 审查 P2：keyword 是 config.selection_mode() 缺省值，
    _select_once 未接 restatement_w 时 run 63 同型故障在缺省路径原样复现。
    """
    from app import config as app_config
    from app.engine.selection import _select_once

    crit_desc = "订单管理模块需支持订单全生命周期管理，含状态同步与异常回执。"
    criterion = {"name": "订单管理", "description": crit_desc, "max_score": "5"}

    def pad(s: str) -> str:
        return s + "x" * (80 - len(s))

    chunks = [
        (0, pad("订单管理（应答）：" + crit_desc)),  # 复述：逐字含 name+description
        (1, pad("无关填充块一")),
        (2, pad("订单管理实测：创建到回执归档 12 步，异常单自动重推。")),
        (3, pad("无关填充块三")),
    ]

    monkeypatch.setattr(app_config, "selection_mode", lambda: "keyword")
    # 预算只装得下内容块+1 邻居：复述块降权后不得挤掉真实内容块
    picked, meta = _select_once(chunks, criterion, budget=len(chunks[2][1]) * 2 + 10,
                                 fallback_diffuse=False, score_scale=None, pool=None)
    picked_ids = set(meta["ids"])
    assert meta["mode"] == "keyword"
    assert 2 in picked_ids, picked_ids
    assert 0 not in picked_ids, picked_ids


def test_score_scale_extra_promotes_window_chunks() -> None:
    """P1-6（2026-11-02 run77 c54）：窗口页 chunk 加成后优先入选。

    场景镜像 run77：偏离表"8小时"块在前、承诺函"4小时"块在后，同命中关键词；
    窗口加成后承诺函块先入选（旧实现偏离表块按文本序霸占预算）。
    """
    chunks = [(i, "正" * 100) for i in range(6)]
    chunks[1] = (1, "应急服务：派遣高级技术人员8 小时到达现场" + "正" * 60)  # 偏离表页
    chunks[4] = (4, "服务承诺：承诺远程服务团队在4 小时到达现场" + "正" * 60)  # 窗口页
    scale = [1.0, 0.25, 1.0, 1.0, 1.3, 1.0]
    picked, meta = select_chunks_for_criterion(
        chunks, {"name": "服务承诺", "description": "到达现场"}, 150,
        score_scale_extra=scale,
    )
    ids = [i for i, _t in picked]
    assert 4 in ids, f"窗口块必须入选：{ids}"
    # 预算 150 只容 1-2 块：窗口块排在偏离表块之前
    assert ids.index(4) < (ids.index(1) if 1 in ids else 99), ids


def test_score_scale_extra_dev_downweighted_not_removed() -> None:
    """偏离表块降权但保留入选资格（真承诺写在偏离表的标书不许被挤掉）。"""
    chunks = [(i, "正" * 100) for i in range(4)]
    chunks[1] = (1, "服务承诺：4 小时内到达现场" + "正" * 70)
    scale = [0.25, 0.25, 0.25, 0.25]
    picked, _ = select_chunks_for_criterion(
        chunks, {"name": "服务承诺", "description": "到达现场"}, 100,
        score_scale_extra=scale,
    )
    assert 1 in [i for i, _t in picked], "降权不移除"


# --- R1-2 强制入选（2026-11-03 run78）：双向防线 ----------------------------

def test_select_chunks_must_include_forced_low_rank() -> None:
    """用友式：窗口块关键词垫底（无任何关键词）仍必进选段。"""
    chunks = [(i, "普通段落填充" + "x" * 100) for i in range(10)]
    chunks[0] = (0, "ITSS 信息技术服务标准符合性证书 证书复印件 说明。" + "x" * 60)
    chunks[1] = (1, "ITSS 证书资质介绍与证书等级说明。" + "x" * 70)
    chunks[2] = (2, "证书清单与认证情况：ITSS 证书等。" + "x" * 70)
    chunks[9] = (9, "窗口材料页扫描文本 abcdefg" + "z" * 80)  # 无关键词、尾部
    picked, meta = select_chunks_for_criterion(
        chunks, {"name": "ITSS 证书", "description": "ITSS"}, 350,
        must_include=[9],
    )
    ids = [i for i, _t in picked]
    assert 9 in ids, ids
    assert meta.get("must_include", 0) >= 1


def test_select_chunks_must_include_overflow_keeps_forced() -> None:
    """超预算砍常规保强制：forced 一定在，常规尾部被挤出（meta 记溢出数）。"""
    chunks = [(i, "内容" + "y" * 190) for i in range(8)]
    chunks[7] = (7, "窗口材料页 " + "z" * 190)
    picked, meta = select_chunks_for_criterion(
        chunks, {"name": "服务承诺", "description": "承诺"}, 400,
        must_include=[7],
    )
    ids = [i for i, _t in picked]
    assert 7 in ids, ids
    assert meta.get("must_include_overflow", 0) >= 1


def test_evidence_map_must_include_forced_cluster_first(monkeypatch):
    """用友式：窗口块 raw 垫底仍成簇且占 top-K 首位（分层评审只取前 K 簇）。"""
    from app.evidence import embed_client
    from app.engine.selection import build_evidence_map

    criterion = {
        "name": "ITSS 信息技术服务标准符合性证书",
        "description": "具有 ITSS 证书，四级得1分，三级及以上得2分。",
        "category": "commercial",
        "max_score": "2",
    }
    cos_by_marker = {
        "强相关块": 0.80,
        "次相关块甲": 0.70,
        "次相关块乙": 0.65,
        "窗口材料块": 0.10,  # 扫描件文本少 → 余弦垫底
    }
    chunks = [
        (0, "强相关块：ITSS 信息技术服务标准符合性证书业务领域运行维护。" + "x" * 40),
        (1, "次相关块甲：公司资质概况介绍。" + "x" * 60),
        (2, "次相关块乙：认证情况综述。" + "x" * 60),
        (3, "窗口材料块：证书编号 ITSS-456 有效期至2027年 发证机构。" + "x" * 40),
    ]

    def fake_embed_batch(texts):
        out = []
        for t in texts:
            s = next((v for m, v in cos_by_marker.items() if m in t), 0.1)
            out.append([s, 0.0])
        return out

    monkeypatch.setattr(embed_client, "embed_one", lambda t: [1.0, 0.0])
    monkeypatch.setattr(embed_client, "embed_batch", fake_embed_batch)

    emap = build_evidence_map(chunks, criterion, must_include=[3])
    assert emap is not None
    clusters, _q = emap
    # 强制簇在最前（K=3 的 top-3 必含），且标记 forced
    assert clusters[0].get("forced") is True
    assert 3 in clusters[0]["ids"]
    top3_ids = [i for cl in clusters[:3] for i in cl["ids"]]
    assert 3 in top3_ids
