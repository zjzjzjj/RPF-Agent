"""Export every frozen matrix cell, including skipped and never executed cells."""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from sqlalchemy import select

from app.db.models import EvaluationRun, ScoreRecord
from app.db.session import session_scope
from app.errors import NotFound, ValidationFailed
from app.services.automation_service import completed_keys, progress


def _text(value) -> str:
    text = str(value or "")
    # User-supplied names, quotations and error text must remain spreadsheet text.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def export_results_csv(run_id: int) -> str:
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("评分运行不存在")
        manifest = (run.config_json or {}).get("automation_manifest")
        if not manifest:
            raise ValidationFailed("该运行没有冻结评审矩阵")
        state = progress(run_id, session=s)
        records = {(r.bidder_id, r.criterion_id): r for r in s.execute(
            select(ScoreRecord).where(ScoreRecord.run_id == run_id)
        ).scalars()}
        valid = completed_keys(run_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["评分运行", run_id, "运行状态", run.status, "已评分项", state["completed"],
                         "故障项", state["failed"], "未执行项", state["remaining"] - state["failed"]])
        writer.writerow(["说明", "合计仅包含有效完成的计分项；故障和未执行项留空。评分覆盖率不同的合计为已评分小计。"])
        writer.writerow([])
        writer.writerow(["投标人", "已评分小计", "已定分项", "应计分项", "评分覆盖率", "故障项"])
        for bidder in manifest["bidders"]:
            bid = int(bidder["id"])
            scoring = [c for c in manifest["criteria"] if c["score_type"] not in ("veto", "match")
                       and (c.get("params") or {}).get("in_total") is not False]
            scores = [records[bid, int(c["id"])].final_score for c in scoring if (bid, int(c["id"])) in valid]
            failed = sum(((records.get((bid, int(c["id"]))).ai_raw_json or {}).get("auto_review") or {}).get("state") == "failed"
                         for c in manifest["criteria"] if (bid, int(c["id"])) in records)
            writer.writerow([_text(bidder["name"]), str(sum(scores, Decimal(0))) if scores else "",
                             len(scores), len(scoring), f"{len(scores) / len(scoring):.1%}" if scoring else "—", failed])
        writer.writerow([])
        writer.writerow(["投标人", "条款编号", "评审条款", "满分", "有效得分", "业务结论", "执行结果", "理由或故障原因"])
        verdicts = {"pass": "符合", "fail": "不符合", "unverifiable": "现有材料无法证实"}
        light_verdicts = {"pass": "预检通过", "fail": "预检不符合",
                          "concern": "预检存疑（未深评）", "not_deeply_reviewed": "未深评"}
        for bidder in manifest["bidders"]:
            for criterion in manifest["criteria"]:
                key = (int(bidder["id"]), int(criterion["id"]))
                row = records.get(key)
                meta = ((row.ai_raw_json or {}).get("auto_review") or {}) if row else {}
                completed = key in valid
                status = "已完成" if completed else "故障跳过" if meta.get("state") == "failed" and state["failure_policy"] == "continue" else "执行失败" if meta.get("state") == "failed" else "未完成"
                labels = light_verdicts if meta.get("basis") == "lightweight" else verdicts
                writer.writerow([_text(bidder["name"]), _text(criterion.get("code")), _text(criterion.get("name")),
                                 criterion.get("max_score"), str(row.final_score) if completed and row.final_score is not None else "",
                                 labels.get(row.machine_status, row.machine_status) if completed else "", status,
                                 _text((row.ai_reasoning if completed else meta.get("error")) if row else "尚未执行")])
        return "\ufeff" + output.getvalue()
