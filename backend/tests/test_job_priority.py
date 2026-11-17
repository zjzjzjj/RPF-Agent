"""任务领取优先级：关键路径（规则编译/评分/报价/导入）不得被批量文档解析饿死。

2026-11-17 测试11 实证：项目包 zip 一次灌进 40 个 parse_document（扫描件 OCR
每个 10+ 分钟），纯 FIFO 把 compile_rules 饿了半小时以上——规则库页面一直显示
"尚无评分规则"，实际是编译任务排在队尾根本没轮到跑。
"""
from __future__ import annotations

from app.db.models import Job
from app.db.session import session_scope
from app.jobs import queue


def _job_row(job_id: int) -> Job:
    with session_scope() as s:
        return s.get(Job, job_id)


def test_compile_rules_jumps_ahead_of_queued_parses(app_env) -> None:  # noqa: ANN001
    """后到的 compile_rules 先跑；普通 parse 之间仍按提交序 FIFO。"""
    p1 = queue.submit("parse_document", {"document_id": 1}, project_id="P1")
    p2 = queue.submit("parse_document", {"document_id": 2}, project_id="P1")
    p3 = queue.submit("parse_document", {"document_id": 3}, project_id="P1")
    c1 = queue.submit("compile_rules", {"project_id": "P1"}, project_id="P1")

    assert queue._claim_next_job() == c1
    assert queue._claim_next_job() == p1
    assert queue._claim_next_job() == p2
    assert queue._claim_next_job() == p3


def test_evaluate_and_extract_price_also_jump(app_env) -> None:  # noqa: ANN001
    p1 = queue.submit("parse_document", {"document_id": 1}, project_id="P1")
    e1 = queue.submit("evaluate", {"run_id": 9}, project_id="P1")
    x1 = queue.submit("extract_price", {"bidder_id": 4}, project_id="P1")

    assert queue._claim_next_job() == e1
    assert queue._claim_next_job() == x1
    assert queue._claim_next_job() == p1


def test_fifo_within_priority_class(app_env) -> None:  # noqa: ANN001
    """同类任务（同优先级）严格按提交序，优先级不许打乱公平性。"""
    e1 = queue.submit("evaluate", {"run_id": 1}, project_id="P1")
    e2 = queue.submit("evaluate", {"run_id": 2}, project_id="P1")
    c1 = queue.submit("compile_rules", {"project_id": "P1"}, project_id="P1")

    assert queue._claim_next_job() == e1
    assert queue._claim_next_job() == e2
    assert queue._claim_next_job() == c1


def test_critical_flag_jumps_parse_queue(app_env) -> None:  # noqa: ANN001
    """招标文件解析（critical=True）先于同批投标文档解析，触发自动匹配不被压后。"""
    b1 = queue.submit("parse_document", {"document_id": 1}, project_id="P1")
    b2 = queue.submit("parse_document", {"document_id": 2}, project_id="P1")
    tender = queue.submit(
        "parse_document", {"document_id": 3}, project_id="P1", critical=True
    )

    assert _job_row(tender).payload_json.get("critical") is True
    assert queue._claim_next_job() == tender
    assert queue._claim_next_job() == b1
    assert queue._claim_next_job() == b2


def test_queue_position_follows_claim_order(app_env) -> None:  # noqa: ANN001
    """排队位置与领取顺序同口径：后到的编译任务前面是 0，普通解析把编译计入前面。"""
    p1 = queue.submit("parse_document", {"document_id": 1}, project_id="P1")
    p2 = queue.submit("parse_document", {"document_id": 2}, project_id="P1")
    c1 = queue.submit("compile_rules", {"project_id": "P1"}, project_id="P1")
    p3 = queue.submit("parse_document", {"document_id": 3}, project_id="P1")

    assert queue.queue_position(c1) == 0  # 编译先跑：没有任务排在它前面
    assert queue.queue_position(p1) == 1  # 前面是 c1（关键路径，虽然后提交仍先跑）
    assert queue.queue_position(p2) == 2  # 前面是 c1、p1
    assert queue.queue_position(p3) == 3  # 前面是 c1、p1、p2


def test_queue_position_critical_parse_counted_by_low_id_parses(app_env) -> None:  # noqa: ANN001
    """非关键任务的位置要把关键路径任务（无论 id 大小）都算进"前面"。"""
    b1 = queue.submit("parse_document", {"document_id": 1}, project_id="P1")
    tender = queue.submit(
        "parse_document", {"document_id": 2}, project_id="P1", critical=True
    )

    assert queue.queue_position(tender) == 0
    assert queue.queue_position(b1) == 1  # tender 虽然后提交，仍会先跑


def test_queue_position_terminal_is_zero(app_env) -> None:  # noqa: ANN001
    """终态任务位置为 0（不给 UI 假排队提示）。"""
    c1 = queue.submit("compile_rules", {"project_id": "P1"}, project_id="P1")
    queue.request_cancel(c1)
    assert queue.queue_position(c1) == 0


def test_queue_position_counts_running_jobs_for_critical(app_env) -> None:  # noqa: ANN001
    """关键任务排在正在跑的普通任务后面时不得报 0（对抗复查实证的反例：
    单 worker 一个扫描件 OCR 在跑（10+ 分钟），编译入队被显示成「即将开始」）。"""
    p1 = queue.submit("parse_document", {"document_id": 1}, project_id="P1")
    assert queue._claim_next_job() == p1  # p1 进入 running，占住执行槽
    c1 = queue.submit("compile_rules", {"project_id": "P1"}, project_id="P1")
    tender = queue.submit(
        "parse_document", {"document_id": 2}, project_id="P1", critical=True
    )
    assert queue.queue_position(c1) == 1  # running 的 p1 算 1 个
    assert queue.queue_position(tender) == 2  # running 的 p1 + 先入队的 c1


def test_addendum_upload_is_critical(app_env) -> None:  # noqa: ANN001
    """补遗解析完成后同样触发自动匹配（auto_merge_service 对 tender+addendum
    都提交编译），必须与招标文件一样插队，不能排在批量投标解析后。"""
    from app.services import ingest_service

    from app.db.models import Project

    with session_scope() as s:
        s.add(Project(id="P1", name="prio-test"))

    _, add_job = ingest_service.save_upload("P1", "addendum.pdf", b"x", kind="addendum")
    _, tender_job = ingest_service.save_upload("P1", "tender.pdf", b"x", kind="tender")
    _, bid_job = ingest_service.save_upload("P1", "bid.pdf", b"x", kind="bid")

    assert queue.get_job(add_job).payload_json.get("critical") is True
    assert queue.get_job(tender_job).payload_json.get("critical") is True
    assert not (queue.get_job(bid_job).payload_json or {}).get("critical")

    # 领取顺序：补遗与招标文件都先于投标文件
    claimed = [queue._claim_next_job() for _ in range(3)]
    assert claimed[-1] == bid_job
