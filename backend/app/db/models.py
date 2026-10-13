"""ORM 模型：评标系统的全部表。

设计要点（与方案文档对应）：
- documents 一张表承载招标文件与投标文件（bidder_id 为 NULL 即招标侧文件）。
- doc_nodes 的 node_id 是**路径编码**（如 tender:P2:C3:S2.1:I(一)），全局唯一且稳定，
  引用可达性校验与证据引用都依赖它。
- rule_packages 是冻结载体：frozen 时写 content_hash + snapshot_json，此后业务表
  被误改也不影响已发生的评分（评分只认快照与哈希）。
- score_records.final_score 只有三处能写：确定性引擎、人工确认、人工改分。
  ai_suggested_score 永远只是建议。
- evidence_citations 即"证据四件套"落点：文件+页码+坐标+逐字原文（+条款号）；
  verified 只由 evidence/verifier.py 置位。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    """朴素 UTC 时间（无时区）。SQLite 存储为字符串，比较行为一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 项目
# ---------------------------------------------------------------------------
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # PRJ-YYYYMMDD-XXXX
    name: Mapped[str] = mapped_column(String(255))
    project_code: Mapped[str | None] = mapped_column(String(128))
    purchaser: Mapped[str | None] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(32), default="comprehensive")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    # draft→tender_parsed→rules_draft→rules_frozen→bids_parsed→evaluating→reviewing→completed→archived
    max_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# 文档与解析
# ---------------------------------------------------------------------------
class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    bidder_id: Mapped[int | None] = mapped_column(ForeignKey("bidders.id"), index=True)  # NULL=招标侧
    kind: Mapped[str] = mapped_column(String(16), default="tender")  # tender|bid|addendum|attachment
    original_filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)
    parse_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending|parsed|degraded|failed|ocr_required
    parse_engine: Mapped[str | None] = mapped_column(String(64))
    author: Mapped[str | None] = mapped_column(String(255))  # 文档作者元数据（串通检测信号源）
    warnings_json: Mapped[list | None] = mapped_column(JSON)
    data_class: Mapped[str] = mapped_column(String(24), default="synthetic")
    # synthetic|redacted_sample|confidential
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("kind IN ('tender','bid','addendum','attachment')", name="kind_valid"),
        CheckConstraint(
            "parse_status IN ('pending','parsed','degraded','failed','ocr_required')",
            name="parse_status_valid",
        ),
        CheckConstraint(
            "data_class IN ('synthetic','redacted_sample','confidential')", name="data_class_valid"
        ),
    )


class DocumentPage(Base):
    __tablename__ = "document_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)  # 从 1 开始
    text: Mapped[str] = mapped_column(Text, default="")
    char_start: Mapped[int] = mapped_column(Integer, default=0)  # 全文区间，跨页条款定位用
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[float] = mapped_column(Float, default=0.0)  # points；bbox 归一化分母
    height: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(8), default="text")  # text|ocr|skipped
    page_approximate: Mapped[bool] = mapped_column(Boolean, default=False)  # docx 虚拟分页
    has_text_layer: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("document_id", "page_no", name="uq_document_pages_doc_page"),)


class DocNode(Base):
    """条款树节点（邻接表 + 路径编码 node_id）。"""

    __tablename__ = "doc_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(String(512), index=True)  # 如 tender:P2:C3:S2.1:I(一)
    parent_node_id: Mapped[str | None] = mapped_column(String(512), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # document|chapter|section|article|item|table|table_row
    number: Mapped[str | None] = mapped_column(String(64))  # 原样编号文本，如"第三章""（一）""2.1"
    label: Mapped[str | None] = mapped_column(String(64))  # 标准化标签，如"C3""I(一)"
    title: Mapped[str | None] = mapped_column(String(512))
    page: Mapped[int] = mapped_column(Integer, default=1)
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    is_toc: Mapped[bool] = mapped_column(Boolean, default=False)  # 目录页节点，不进引用索引
    is_coarse: Mapped[bool] = mapped_column(Boolean, default=False)  # 表格/版面降级标记（不猜）
    depth: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("document_id", "node_id", name="uq_doc_nodes_doc_node"),)


# ---------------------------------------------------------------------------
# 规则包（评分细则）
# ---------------------------------------------------------------------------
class RulePackage(Base):
    __tablename__ = "rule_packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # draft|confirmed|frozen|superseded
    origin: Mapped[str] = mapped_column(String(16), default="document")
    # document|template|merged|law（包级来源：编译自招标文件/实例化自模板库/合并覆盖/法律通用规则包导入）
    template_id: Mapped[str | None] = mapped_column(String(64))
    template_hash: Mapped[str | None] = mapped_column(String(32))
    library_version: Mapped[str | None] = mapped_column(String(32))
    merge_target_id: Mapped[int | None] = mapped_column(ForeignKey("rule_packages.id"))
    # merge 模式的临时编译包指向的模板包（S1）；该列非空 = 合并辅助包，不算"当前规则"
    content_hash: Mapped[str | None] = mapped_column(String(32))
    snapshot_json: Mapped[dict | None] = mapped_column(JSON)  # 冻结时全量快照
    parent_version_id: Mapped[int | None] = mapped_column(ForeignKey("rule_packages.id"))
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    compiled_by: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    confirmed_by: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    frozen_by: Mapped[str | None] = mapped_column(String(64))
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_rule_packages_project_version"),
        CheckConstraint(
            "status IN ('draft','confirmed','frozen','superseded')", name="status_valid"
        ),
        CheckConstraint(
            "origin IN ('document','template','merged','law')", name="origin_valid"
        ),
    )


class Criterion(Base):
    """评分项（评分单元）。weight 绝不进 prompt。"""

    __tablename__ = "criteria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("rule_packages.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64))  # 如 2.3.1 或 x1（人工新增）
    category: Mapped[str] = mapped_column(String(24))  # qualification|compliance|price|technical|commercial
    name: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text)
    max_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    score_type: Mapped[str] = mapped_column(String(16))  # veto|formula|count|match|subjective
    is_veto: Mapped[bool] = mapped_column(Boolean, default=False)
    veto_consequence: Mapped[str | None] = mapped_column(String(24))  # disqualify|may_request|discretionary
    required_evidence: Mapped[str | None] = mapped_column(Text)
    # 规则原文四件套（source_node_id 必须在 doc_nodes 中可达）
    source_node_id: Mapped[str | None] = mapped_column(String(512))
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_quote: Mapped[str | None] = mapped_column(Text)
    # 规则库溯源（模板路径，设计稿 §4.1）
    origin: Mapped[str] = mapped_column(String(16), default="document")
    # template|document|manual|law（当前生效来源；覆盖后由 template → document）
    template_code: Mapped[str | None] = mapped_column(String(64))  # 模板原始编号（覆盖时保留）
    template_hash: Mapped[str | None] = mapped_column(String(32))
    library_version: Mapped[str | None] = mapped_column(String(32))
    override_json: Mapped[dict | None] = mapped_column(JSON)  # 最近一次覆盖详情（S1）
    signoff_json: Mapped[dict | None] = mapped_column(JSON)  # {by,at,reason} 模板兜底签批
    p_source: Mapped[str | None] = mapped_column(String(2))  # 'P0'=法律强制规定；'P5'=模板；'P1'-'P4'=文档；NULL=人工
    status: Mapped[str] = mapped_column(String(16), default="needs_input")
    # verified|needs_input|ambiguous|gap|conflict|invalid|needs_signoff|retired
    edit_json: Mapped[dict | None] = mapped_column(JSON)  # {by,at,reason,model_value}
    confidence: Mapped[float | None] = mapped_column(Float)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("package_id", "code", name="uq_criteria_package_code"),
        CheckConstraint(
            "category IN ('qualification','compliance','price','technical','commercial')",
            name="category_valid",
        ),
        CheckConstraint(
            "score_type IN ('veto','formula','count','match','subjective')", name="score_type_valid"
        ),
        CheckConstraint(
            "status IN ('verified','needs_input','ambiguous','gap','conflict','invalid',"
            "'needs_signoff','retired')",
            name="status_valid",
        ),
        CheckConstraint(
            "origin IN ('template','document','manual','law')", name="origin_valid"
        ),
    )


class CriterionLevel(Base):
    """评分档位（如 优17-20 / 良12-16 …）。description 是档位判据原文。"""

    __tablename__ = "criterion_levels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    criterion_id: Mapped[int] = mapped_column(ForeignKey("criteria.id", ondelete="CASCADE"), index=True)
    level_label: Mapped[str] = mapped_column(String(64))
    score_min: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    score_max: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    description: Mapped[str] = mapped_column(Text)
    source_node_id: Mapped[str | None] = mapped_column(String(512))
    source_quote: Mapped[str | None] = mapped_column(Text)


class CriterionCheck(Base):
    """客观项检查原语实例（资格/符合性、count/match 类评分项的判定参数）。"""

    __tablename__ = "criterion_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    criterion_id: Mapped[int] = mapped_column(ForeignKey("criteria.id", ondelete="CASCADE"), index=True)
    check_type: Mapped[str] = mapped_column(String(32))
    # document_present|contains_phrase|value_in_range|date_valid|math_relation|
    # cross_document_match|count_at_least|human_only
    field: Mapped[str | None] = mapped_column(String(255))
    params_json: Mapped[dict | None] = mapped_column(JSON)
    expected: Mapped[str | None] = mapped_column(String(512))
    on_fail: Mapped[str] = mapped_column(String(24), default="need_human")
    # pass|recommend_reject|need_human
    source_quote: Mapped[str | None] = mapped_column(Text)


class PriceFormula(Base):
    """价格分公式参数（确定性引擎的输入）。"""

    __tablename__ = "price_formulas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("rule_packages.id", ondelete="CASCADE"), index=True)
    criterion_id: Mapped[int | None] = mapped_column(ForeignKey("criteria.id", ondelete="CASCADE"))
    formula_type: Mapped[str] = mapped_column(String(32))
    # lowest_ratio|benchmark_ratio|deviation_deduct|above_deduct
    benchmark_method: Mapped[str | None] = mapped_column(String(32))
    # lowest|average|average_trimmed|second_lowest|average_below
    trim_ratio: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    k: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))  # 基准价系数（如 0.95）
    deduct_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))  # 每偏离 1% 扣分
    upper_bound: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # 价格分上限
    lower_bound: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # 价格分下限
    rounding: Mapped[int] = mapped_column(Integer, default=2)
    exclude_invalid: Mapped[bool] = mapped_column(Boolean, default=True)
    tax_basis: Mapped[str | None] = mapped_column(String(64))
    params_json: Mapped[dict | None] = mapped_column(JSON)  # 扩展位：新公式参数不改表
    source_node_id: Mapped[str | None] = mapped_column(String(512))
    source_quote: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str | None] = mapped_column(String(64))


class OverrideRecord(Base):
    """逐条覆盖审计（S1，设计稿 §4.1）。

    独立于 criteria 行内 override_json，供包级时间线与 new_version 复制。
    记录"压过了哪一条"（won_source/lost_source，调研报告 §2.3）。
    """

    __tablename__ = "override_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("rule_packages.id"), index=True)  # 模板包
    merge_package_id: Mapped[int | None] = mapped_column(ForeignKey("rule_packages.id"))
    action: Mapped[str] = mapped_column(String(16))  # override|keep_template|drop_template|append_document
    template_criterion_id: Mapped[int | None] = mapped_column(ForeignKey("criteria.id"))
    document_criterion_id: Mapped[int | None] = mapped_column(ForeignKey("criteria.id"))
    match_method: Mapped[str | None] = mapped_column(String(16))  # exact|deterministic|llm|human
    similarity: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    won_source: Mapped[str | None] = mapped_column(String(2))  # P1..P4（压过 P5）
    lost_source: Mapped[str | None] = mapped_column(String(2))  # 通常为 P5
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    document_node_id: Mapped[str | None] = mapped_column(String(512))
    document_page: Mapped[int | None] = mapped_column(Integer)
    quote: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PackageValidation(Base):
    """确定性校验器的输出记录（12 条校验器写这里）。"""

    __tablename__ = "package_validations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("rule_packages.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64))  # 如 SCORE_CLOSURE / WEIGHT_SUM
    severity: Mapped[str] = mapped_column(String(8))  # error|warn
    subject_type: Mapped[str | None] = mapped_column(String(32))  # criterion|formula|package
    subject_id: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)
    hint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# 投标人
# ---------------------------------------------------------------------------
class Bidder(Base):
    __tablename__ = "bidders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    unified_code: Mapped[str | None] = mapped_column(String(64))  # 统一社会信用代码
    bid_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    bid_price_upper: Mapped[str | None] = mapped_column(String(255))  # 大写金额原文
    price_breakdown_json: Mapped[dict | None] = mapped_column(JSON)
    price_status: Mapped[str] = mapped_column(String(16), default="extracted")  # extracted|confirmed|conflict
    price_evidence_id: Mapped[int | None] = mapped_column(Integer)  # → evidence_citations.id（避免循环 FK）
    submission_time: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # pending|qualified|recommend_reject|rejected|scored
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BidderDocument(Base):
    """一家投标人可有多份文件（主件/资格/技术/报价/其他）。"""

    __tablename__ = "bidder_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bidder_id: Mapped[int] = mapped_column(ForeignKey("bidders.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16), default="main")
    # main|qualification|technical|price|other

    __table_args__ = (UniqueConstraint("bidder_id", "document_id", name="uq_bidder_documents_pair"),)


# ---------------------------------------------------------------------------
# 评分运行与评分记录
# ---------------------------------------------------------------------------
class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("rule_packages.id"))
    package_version: Mapped[int] = mapped_column(Integer)
    package_content_hash: Mapped[str | None] = mapped_column(String(32))  # 规则版本绑定
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # queued|running|succeeded|partial|failed
    stages_json: Mapped[list | None] = mapped_column(JSON)  # 本次启用的阶段
    llm_provider: Mapped[str | None] = mapped_column(String(32))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    config_json: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScoreRecord(Base):
    """一家投标人 × 一个评分项的评分记录。

    final_score 的写入权限（服务层强制）：
      1) 确定性引擎（价格分/count/match/veto 判定）
      2) 人工确认（confirm，写入 human_score 与 final_score）
      3) 人工改分（override）
    ai_suggested_score 永远只是建议，不得直接成为 final_score。
    """

    __tablename__ = "score_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True)
    bidder_id: Mapped[int] = mapped_column(ForeignKey("bidders.id"), index=True)
    criterion_id: Mapped[int] = mapped_column(ForeignKey("criteria.id"), index=True)
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    package_content_hash: Mapped[str | None] = mapped_column(String(32))

    max_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    score_type: Mapped[str | None] = mapped_column(String(16))
    veto_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    machine_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # 确定性引擎产出
    machine_status: Mapped[str | None] = mapped_column(String(32))  # pass|fail|need_human|...

    ai_suggested_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    ai_level_label: Mapped[str | None] = mapped_column(String(64))
    ai_reasoning: Mapped[str | None] = mapped_column(Text)
    ai_confidence: Mapped[float | None] = mapped_column(Float)
    ai_raw_json: Mapped[dict | None] = mapped_column(JSON)

    human_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # 人工独立分（防锚定）
    human_level_label: Mapped[str | None] = mapped_column(String(64))

    final_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    evidence_status: Mapped[str] = mapped_column(String(16), default="none")  # verified|unverified|none
    review_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # pending|ai_suggested|human_confirmed|human_overridden|conflict_flagged|not_applicable
    ai_revealed_at: Mapped[datetime | None] = mapped_column(DateTime)  # 防锚定解锁时间戳
    evaluator_actor: Mapped[str | None] = mapped_column(String(64))
    confirm_time: Mapped[datetime | None] = mapped_column(DateTime)
    warnings_json: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "bidder_id", "criterion_id", name="uq_score_records_run_bidder_criterion"),
        CheckConstraint(
            "review_status IN ('pending','ai_suggested','human_confirmed','human_overridden',"
            "'conflict_flagged','not_applicable')",
            name="review_status_valid",
        ),
    )


class EvidenceCitation(Base):
    """证据四件套落点：文件 + 页码 + 坐标 + 逐字原文（+ 条款号）。

    verified 只由 evidence/verifier.py 写入：L1 精确命中且引文 ≥ MIN_QUOTE_CHARS。
    """

    __tablename__ = "evidence_citations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    score_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("score_records.id", ondelete="CASCADE"), index=True
    )
    criterion_id: Mapped[int | None] = mapped_column(ForeignKey("criteria.id"), index=True)
    citation_role: Mapped[str] = mapped_column(String(24), default="score")
    # score|criterion_source|veto|price|level
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int | None] = mapped_column(Integer)
    node_id: Mapped[str | None] = mapped_column(String(512))
    quote: Mapped[str] = mapped_column(Text)
    bbox_json: Mapped[list | None] = mapped_column(JSON)  # [[x0,y0,x1,y1], ...] top-left 原点
    page_size_json: Mapped[list | None] = mapped_column(JSON)  # [width, height] points
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verify_method: Mapped[str | None] = mapped_column(String(24))  # exact|normalized|prefix_fallback
    verify_score: Mapped[float | None] = mapped_column(Float)
    candidates_json: Mapped[list | None] = mapped_column(JSON)  # 定位失败时的近似候选
    created_by: Mapped[str] = mapped_column(String(16), default="llm")  # llm|human|engine
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# 留痕
# ---------------------------------------------------------------------------
class ReviewRecord(Base):
    """复核留痕：谁、何时、为什么，以及变更前后的值。reason 必填。"""

    __tablename__ = "review_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_type: Mapped[str] = mapped_column(String(32))  # score|criterion|bidder|package|price
    subject_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    # confirm|override|reject_ai|flag_conflict|skip_blind|give_reason|freeze|confirm_reject|accept
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    old_value_json: Mapped[dict | None] = mapped_column(JSON)
    new_value_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AuditLog(Base):
    """全局粗粒度流水（比 review_records 更宽，覆盖非复核类动作）。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str | None] = mapped_column(String(32))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    detail_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    # parse_document|compile_rules|evaluate|export_report
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # queued|running|succeeded|failed|cancelled
    done_steps: Mapped[int] = mapped_column(Integer, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(String(512))
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    result_json: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')", name="status_valid"
        ),
    )
