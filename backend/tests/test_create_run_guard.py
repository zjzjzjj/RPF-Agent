"""发起评分前置护栏：文档解析中禁止创建自动化运行。

2026-11-18 PRJ-20261118-42FO 实证：导入后立刻发起评分（30 份文档仍在解析），
自动化运行在创建瞬间冻结的输入快照随后漂移，评分任务启动时 initialize_run
判"运行输入或评审配置已变化，请新建评分运行"→用户只看到一条无头绪的 Conflict。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models import Bidder, Document, Project, RulePackage
from app.db.session import session_scope
from app.errors import ValidationFailed
from app.services.scoring_service import create_run


def test_create_run_blocked_while_documents_parsing(app_env, monkeypatch):
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    with session_scope() as s:
        s.add(Project(id="PRJ-GUARD", name="解析中护栏"))
    with session_scope() as s:
        pkg = RulePackage(project_id="PRJ-GUARD", version=1, status="frozen", content_hash="frozen")
        bidder = Bidder(project_id="PRJ-GUARD", name="甲", bid_price=Decimal("100"),
                        price_status="confirmed")
        s.add_all([pkg, bidder])
        s.flush()
        s.add(Document(project_id="PRJ-GUARD", bidder_id=bidder.id, kind="bid",
                       original_filename="标书.pdf", stored_path="/tmp/x.pdf",
                       sha256="0" * 64, parse_status="pending"))
        pkg_id, bidder_id = pkg.id, bidder.id

    with pytest.raises(ValidationFailed) as exc:
        create_run("PRJ-GUARD", pkg_id, [bidder_id], ["objective"], "tester")
    assert "正在解析" in str(exc.value)


def test_create_run_not_blocked_by_settled_documents(app_env, monkeypatch):
    """解析失败的文档是已定格状态，不拦发起（结果页会显著告警）。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "0")  # 人工回退模式无自动化快照，不触发该护栏
    with session_scope() as s:
        s.add(Project(id="PRJ-GUARD2", name="失败文档不拦"))
    with session_scope() as s:
        pkg = RulePackage(project_id="PRJ-GUARD2", version=1, status="frozen", content_hash="frozen")
        bidder = Bidder(project_id="PRJ-GUARD2", name="乙", bid_price=Decimal("100"),
                        price_status="confirmed")
        s.add_all([pkg, bidder])
        s.flush()
        s.add(Document(project_id="PRJ-GUARD2", bidder_id=bidder.id, kind="bid",
                       original_filename="标书.pdf", stored_path="/tmp/x.pdf",
                       sha256="0" * 64, parse_status="failed"))
        pkg_id, bidder_id = pkg.id, bidder.id

    run_id, job_id = create_run("PRJ-GUARD2", pkg_id, [bidder_id], ["objective"], "tester")
    assert run_id and job_id
