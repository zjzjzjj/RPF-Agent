"""招标文件解析成功后，自动排队「匹配并更新规则」。

挂载点：`ingest_service.parse_document` 成功分支。设计原则：

- **默认开、可关**（`BID_EVAL_AUTO_MATCH=0`）；
- **未配置 LLM 不触发**（编译必须走模型；离线测试因此完全不产生额外任务）；
- 当前规则包必须是 **draft 且模板/合并来源** 才做 merge；无包时做一次 replace
  编译（从招标文件直接产出规则包）；
- **frozen 的模板/合并包**：自动派生新版本草稿后再 merge（旧冻结包保持只读、
  仍是评分输入，新草稿确认冻结后才生效）——对应「无招标文件用默认规则、有招标
  文件则按文件覆盖重建」的使用路径；
- confirmed 与 origin=document 的包不自动改动，返回 package_not_draft 提示解冻；
- 同一项目已有在途 `compile_rules` 任务时跳过，防重复解析/多份上传排队多次。
"""
from __future__ import annotations

from sqlalchemy import select

from app import config
from app.db.models import Document, Job
from app.db.session import session_scope


def _skip(reason: str) -> dict:
    return {"auto_match": "skipped", "job_id": None, "mode": None, "skip_reason": reason}


def maybe_schedule_auto_match(project_id: str, document_id: int) -> dict:
    """解析成功后调用；返回可并入 parse job result 的摘要。"""
    if not config.auto_match_enabled():
        return _skip("auto_match_disabled")
    if not config.llm_api_key_configured():
        return _skip("llm_not_configured")

    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None or doc.project_id != project_id:
            return _skip("document_not_found")
        if doc.kind not in ("tender", "addendum"):
            return _skip("not_tender")
        if doc.parse_status not in ("parsed", "degraded"):
            return _skip("not_parsed")
        in_flight = s.execute(
            select(Job.id)
            .where(
                Job.project_id == project_id,
                Job.kind == "compile_rules",
                Job.status.in_(("queued", "running")),
            )
            .limit(1)
        ).first()
        if in_flight is not None:
            return _skip("already_in_flight")

    from app.services import rule_service

    pkg = rule_service.current_package(project_id)
    unfroze_version: int | None = None
    if pkg is None:
        mode, merge_target_id = "replace", None
    elif pkg.status == "draft" and pkg.origin in ("template", "merged"):
        mode, merge_target_id = "merge", pkg.id
    elif pkg.status == "frozen" and pkg.origin in ("template", "merged"):
        # 默认规则已冻结：先派生新版本草稿，再按招标文件覆盖重建。
        # 旧冻结包保持只读且仍是评分输入（latest_frozen_package），
        # 新草稿不确认/冻结就不生效——因此自动执行是安全的。
        new_pkg = rule_service.new_version(
            pkg.id, "auto", "检测到招标文件，自动创建新版本用于覆盖重建"
        )
        mode, merge_target_id = "merge", new_pkg.id
        unfroze_version = new_pkg.version
    else:
        return _skip("package_not_draft")

    from app.jobs.queue import submit

    job_id = submit(
        "compile_rules",
        {
            "project_id": project_id,
            "document_id": document_id,
            "actor": "auto",
            "provider": None,
            "model": None,
            "mode": mode,
            "merge_target_id": merge_target_id,
        },
        project_id=project_id,
    )
    return {
        "auto_match": "scheduled_after_unfreeze" if unfroze_version else "scheduled",
        "job_id": job_id,
        "mode": mode,
        "merge_target_id": merge_target_id,
        "new_version": unfroze_version,
        "skip_reason": None,
    }
