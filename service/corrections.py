"""理赔更正影响：报告更正后列出应通知、应补付、应转人工的事项。

禁止自动向顾客扣回已退款；已结束案件的原决定保留可查。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import scope as scope_mod
from .auth import Actor, audit, require
from .domain import DomainError, require_text, utcnow
from .store import Store


def assess_correction(store: Store, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "quality", "independent-aftercare")
    old_report = require_text(payload.get("old_report") or payload.get("report"), "old_report")
    new_report = payload.get("new_report")
    now = utcnow()
    with store.transaction() as conn:
        old = conn.execute("SELECT * FROM reports WHERE id=?", (old_report,)).fetchone()
        if not old:
            raise DomainError("not_found", "原报告不存在", {"report": old_report})
        if new_report:
            new = conn.execute("SELECT * FROM reports WHERE id=?", (new_report,)).fetchone()
            if not new:
                raise DomainError("not_found", "更正报告不存在", {"report": new_report})
            if new["corrects"] != old_report and new["supersedes"] != old_report:
                raise DomainError("invalid_argument", "更正报告未声明更正原报告", {"new_report": new_report})
        actions: list[dict[str, Any]] = []
        affected_decisions = _affected_decisions(conn, old_report)
        affected_batches = set(scope_mod.batch_ids_for_report(conn, old_report))
        if new_report:
            affected_batches |= set(scope_mod.batch_ids_for_report(conn, new_report))
        affected_batches = set(scope_mod.expand_with_mappings(conn, sorted(affected_batches)))

        # 已结束的投诉：保留原决定，列出需通知/补付/转人工。
        for complaint in conn.execute(
            "SELECT * FROM complaints WHERE status='resolved' ORDER BY id"
        ).fetchall():
            targets = conn.execute("SELECT item_id,batch_id FROM complaint_targets WHERE complaint_id=?", (complaint["id"],)).fetchall()
            if not any(t["batch_id"] in affected_batches for t in targets):
                continue
            actions.append({
                "type": "notify_customer_of_correction",
                "complaint": complaint["id"],
                "order": complaint["order_id"],
                "reason": "报告更正涉及该已结案投诉的批次；原决定保留可查，需向顾客说明更正事实",
                "original_decision_retained": True,
            })
            for payment in conn.execute("SELECT * FROM payments WHERE complaint_id=? AND state='paid'", (complaint["id"],)).fetchall():
                actions.append({
                    "type": "review_shortfall_or_top_up",
                    "complaint": complaint["id"],
                    "payment": payment["id"],
                    "amount_paid": payment["amount"],
                    "reason": "报告更正可能改变应补付金额；不得自动向顾客扣回已退款，需人工复核是否补付",
                    "do_not_auto_clawback": True,
                })
        # 未结投诉：转人工待办。
        for complaint_id, item_id, order_id in conn.execute(
            "SELECT DISTINCT c.id, t.item_id, c.order_id FROM complaints c JOIN complaint_targets t ON t.complaint_id=c.id "
            "WHERE c.status<>'resolved' AND t.batch_id IS NOT NULL"
        ).fetchall():
            row = conn.execute("SELECT batch_id FROM complaint_targets WHERE complaint_id=? AND item_id=?", (complaint_id, item_id)).fetchone()
            if row and row["batch_id"] in affected_batches:
                already = conn.execute(
                    "SELECT 1 FROM issues WHERE complaint_id=? AND item_id=? AND status='open' AND kind='manual_followup' AND detail LIKE ?",
                    (complaint_id, item_id, f"%报告更正 {old_report}%"),
                ).fetchone()
                if not already:
                    issue_id = f"issue-correction-{old_report}-{complaint_id}-{item_id}"
                    conn.execute(
                        "INSERT OR IGNORE INTO issues(id,complaint_id,item_id,order_id,kind,status,detail,priority,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (issue_id, complaint_id, item_id, order_id, "manual_followup", "open",
                         f"报告更正 {old_report} 涉及该分项批次，需人工复核处置与赔付", 5, actor.id, now),
                    )
                    actions.append({"type": "manual_review", "complaint": complaint_id, "item": item_id, "issue": issue_id,
                                    "reason": "未结投诉，需复核是否调整处置范围与赔付"})
        correction_id = require_text(payload.get("id") or f"correction-{old_report}-{new_report or now}", "id")
        conn.execute(
            "INSERT OR REPLACE INTO corrections(id,report_id,old_report_id,status,affected_decision_ids_json,actions_json,note,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (correction_id, new_report or old_report, old_report, "assessed",
             json.dumps(affected_decisions, ensure_ascii=False), json.dumps(actions, ensure_ascii=False),
             payload.get("note"), actor.id, now),
        )
        audit(conn, actor, "correction.assess", "correction", correction_id,
              {"old_report": old_report, "new_report": new_report, "actions": len(actions)})
        return {
            "id": correction_id,
            "old_report": old_report,
            "new_report": new_report,
            "affected_batches": sorted(affected_batches),
            "affected_decisions": affected_decisions,
            "actions": actions,
            "policy": {
                "accepted_refunds_are_not_clawed_back": True,
                "closed_case_decisions_remain_queryable": True,
                "auto_actions": False,
            },
        }


def _affected_decisions(conn: sqlite3.Connection, report_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM dispositions WHERE basis_report=? ORDER BY created_at, id", (report_id,)
    ).fetchall()
    decisions = []
    for row in rows:
        decisions.append({
            "type": "disposition",
            "id": row["id"],
            "disposition_type": row["type"],
            "status": row["status"],
            "requires_review": True,
            "note": "更正不自动解除处置；需质量人员按新报告重新判断",
        })
    return decisions


def list_corrections(store: Store, query: dict[str, Any]) -> dict[str, Any]:
    with store.read() as conn:
        sql = "SELECT * FROM corrections WHERE 1=1"
        params: list[tuple] = []
        if query.get("report"):
            sql += " AND (report_id=? OR old_report_id=?)"
            params.extend([query["report"], query["report"]])
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY created_at,id", tuple(params)).fetchall()]
        for row in rows:
            row["affected_decision_ids"] = json.loads(row.pop("affected_decision_ids_json") or "[]")
            row["actions"] = json.loads(row.pop("actions_json") or "[]")
        return {"corrections": rows}
