"""质量处置：按一份证据临时停供、扩大调查、确认不合格、解除某项处置。

所有处置都有独立依据 ID 与适用批次；解除只针对指定处置，不连带解除其他依据。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import scope as scope_mod
from .auth import Actor, audit, case_action, require
from .domain import DISPOSITION_TYPES, DomainError, require_text, utcnow
from .store import Store

NEGATIVE = {"stop_supply", "investigate", "not_conforming"}


def _disposition_view(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    batches = scope_mod.batch_ids_for_disposition(conn, row["id"])
    effective_batches = list(batches)
    scope = json.loads(row["scope_json"] or "{}")
    if scope.get("all_batches"):
        effective_batches = sorted({r["id"] for r in conn.execute("SELECT id FROM batches").fetchall()})
    if scope.get("include_mapped"):
        effective_batches = scope_mod.expand_with_mappings(conn, effective_batches)
    if scope.get("include_supplied"):
        effective_batches = scope_mod.expand_with_supplier(conn, effective_batches)
    items = scope_mod.items_for_batches(conn, effective_batches)
    complaints = scope_mod.open_complaints_for_batches(conn, effective_batches)
    value = {
        "id": row["id"],
        "type": row["type"],
        "type_label": DISPOSITION_TYPES.get(row["type"], row["type"]),
        "status": row["status"],
        "effective": row["status"] == "active",
        "batch_ids": batches,
        "effective_batch_ids": effective_batches,
        "scope": scope,
        "basis_report": row["basis_report"],
        "reason": row["reason"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "lifted_by": row["lifted_by"],
        "lifted_at": row["lifted_at"],
        "lift_reason": row["lift_reason"],
        "lift_disposition": row["lift_disposition"],
        "supersedes": row["supersedes"],
        "basis_retracted": bool(row["basis_retracted"]),
        "basis_retracted_at": row["basis_retracted_at"],
        "basis_retracted_note": row["basis_retracted_note"],
        "affected_items": [{"id": i["id"], "batch": i["batch_id"], "state": i["state"], "outcome": i["current_outcome"]} for i in items],
        "open_complaints": [c["id"] for c in complaints],
    }
    # 每个批次当前仍受哪些有效依据限制（含本处置；解除后不含本处置）。
    value["inventory_still_restricted"] = {
        batch: [d["id"] for d in scope_mod.active_restrictions_for_batch(conn, batch)] for batch in effective_batches
    }
    return value


def _refresh_warehouse(conn: sqlite3.Connection, actor: Actor, disposition: dict[str, Any], effective_batches: list[str], now: str) -> dict[str, Any]:
    """把新处置落到实物：在库/已分拣隔离，已交承运人转拦截跟进，预留换货转待重办。"""
    summary = {"quarantined": [], "blocked_picked": [], "intercepting": [], "reservations_released": [], "fulfillments_reprocessing": [], "exchange_fulfillments": []}
    if not effective_batches:
        return summary
    placeholders = ",".join("?" for _ in effective_batches)
    garments = conn.execute(
        f"SELECT * FROM garments WHERE batch_id IN ({placeholders}) ORDER BY id", tuple(effective_batches)
    ).fetchall()
    # 尚未交运的出库单一律标为受阻，仓库据此停止交运。
    pending_shipments = conn.execute(
        f"SELECT * FROM shipments WHERE batch_id IN ({placeholders}) AND phase IN ('dispatch_pending','carrier_pending') ORDER BY id",
        tuple(effective_batches),
    ).fetchall()
    for shipment in pending_shipments:
        conn.execute("UPDATE shipments SET blocked_reason=?, updated_at=? WHERE id=?",
                     (f"质量处置 {disposition['id']} 阻止交运", now, shipment["id"]))
        summary.setdefault("blocked_shipments", []).append(shipment["id"])
    for garment in garments:
        state = garment["state"]
        if state in {"in_stock", "picked"} or (state == "returned" and garment["current_outcome"] in {None, "awaiting_disposition"}):
            conn.execute(
                "UPDATE garments SET current_outcome='quarantine', outcome_reason=?, updated_at=? WHERE id=?",
                (f"质量处置 {disposition['id']} 适用批次 {garment['batch_id']}", now, garment["id"]),
            )
            if state == "picked":
                summary["blocked_picked"].append(garment["id"])
            else:
                summary["quarantined"].append(garment["id"])
        elif state == "shipped":
            conn.execute(
                "UPDATE garments SET current_outcome='intercepting', outcome_reason=?, updated_at=? WHERE id=?",
                (f"已交承运人，转拦截跟进（依据 {disposition['id']}）；不能回加成可售库存", now, garment["id"]),
            )
            shipments = conn.execute("SELECT * FROM shipments WHERE item_id=? AND phase IN ('with_carrier','intercepting')", (garment["id"],)).fetchall()
            for shipment in shipments:
                if shipment["phase"] == "with_carrier":
                    conn.execute("UPDATE shipments SET phase='intercepting', updated_at=? WHERE id=?", (now, shipment["id"]))
                    conn.execute(
                        "INSERT INTO shipment_events(shipment_id,state,source,detail_json,at) VALUES(?,?,?,?,?)",
                        (shipment["id"], "intercepting", "quality_disposition", json.dumps({"disposition": disposition["id"]}, ensure_ascii=False), now),
                    )
                summary["intercepting"].append(shipment["id"])
        elif state == "intercepted":
            conn.execute(
                "UPDATE garments SET current_outcome='quarantine', outcome_reason=?, updated_at=? WHERE id=?",
                (f"拦截成功但仍受质量处置 {disposition['id']} 限制，不得回可售库存", now, garment["id"]),
            )
            summary["quarantined"].append(garment["id"])
    # 已保留的替换库存需要重新履行，但不能消耗新批次库存。
    fulfillments = conn.execute(
        f"""SELECT f.* FROM fulfillments f JOIN garments g ON g.id=f.reserved_item
            WHERE g.batch_id IN ({placeholders}) AND f.status IN ('reserved','blocked','dispatching')""",
        tuple(effective_batches),
    ).fetchall()
    for fulfillment in fulfillments:
        conn.execute("UPDATE fulfillments SET status='needs_reprocessing', updated_at=? WHERE id=?", (now, fulfillment["id"]))
        conn.execute("UPDATE reservations SET status='released', updated_at=?, note=? WHERE id=(SELECT id FROM reservations WHERE item_id=? AND complaint_id=? AND status='active')",
                     (now, f"批次受处置 {disposition['id']} 限制", fulfillment["reserved_item"], fulfillment["complaint_id"]))
        summary["fulfillments_reprocessing"].append(fulfillment["id"])
    # 任何未完成换货预约，只要目标批次受限制，都标为待重办。
    pending = conn.execute(
        f"""SELECT f.* FROM fulfillments f JOIN garments g ON g.id=f.replacement_item
            WHERE g.batch_id IN ({placeholders}) AND f.status IN ('reserved','dispatching')""",
        tuple(effective_batches),
    ).fetchall()
    for fulfillment in pending:
        conn.execute("UPDATE fulfillments SET status='needs_reprocessing', updated_at=? WHERE id=?", (now, fulfillment["id"]))
        summary["fulfillments_reprocessing"].append(fulfillment["id"])
    return summary


def create_disposition(store: Store, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "quality")
    disposition_id = require_text(payload.get("id"), "id")
    kind = require_text(payload.get("type"), "type")
    if kind not in {"stop_supply", "investigate", "not_conforming"}:
        raise DomainError("invalid_argument", "只能新建停供、调查或不合格处置；解除请用 lift", {"type": kind})
    reason = require_text(payload.get("reason"), "reason")
    with store.transaction() as conn:
        batches, basis = scope_mod.resolve_scope_batches(conn, payload, actor.id)
        if conn.execute("SELECT 1 FROM dispositions WHERE id=?", (disposition_id,)).fetchone():
            raise DomainError("conflict", "处置编号已存在", {"disposition": disposition_id})
        report = payload.get("report")
        if report:
            row = conn.execute("SELECT status FROM reports WHERE id=?", (report,)).fetchone()
            if not row:
                raise DomainError("not_found", "依据报告不存在", {"report": report})
            if row["status"] != "active":
                raise DomainError("conflict", "依据报告已不是有效状态", {"report": report, "status": row["status"]})
        scope = {key: True for key in ("include_mapped", "include_supplied", "all_batches") if payload.get(key)}
        if payload.get("include_supplied_all"):
            scope["include_supplied"] = True
        now = utcnow()
        conn.execute(
            "INSERT INTO dispositions(id,type,scope_json,basis_report,reason,created_by,created_at,status,supersedes) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (disposition_id, kind, json.dumps(scope, ensure_ascii=False), report, reason, actor.id, now, "active", payload.get("supersedes")),
        )
        for row in basis:
            conn.execute(
                "INSERT OR IGNORE INTO disposition_batches(disposition_id,batch_id,basis) VALUES(?,?,?)",
                (disposition_id, row["batch"], row["basis"]),
            )
        disposition_row = dict(conn.execute("SELECT * FROM dispositions WHERE id=?", (disposition_id,)).fetchone())
        effective = sorted({r["id"] for r in conn.execute("SELECT id FROM batches").fetchall()}) if scope.get("all_batches") else list(batches)
        if scope.get("include_mapped"):
            effective = scope_mod.expand_with_mappings(conn, effective)
        if scope.get("include_supplied"):
            effective = scope_mod.expand_with_supplier(conn, effective)
        warehouse = _refresh_warehouse(conn, actor, disposition_row, effective, now)
        audit(conn, actor, "disposition.create", "disposition", disposition_id, {"type": kind, "batches": batches, "warehouse": warehouse})
        for complaint_id in {c["id"] for c in scope_mod.open_complaints_for_batches(conn, effective)}:
            case_action(conn, actor, "disposition.applied", complaint_id, {"disposition": disposition_id, "type": kind}, item_id=None)
        view = _disposition_view(conn, dict(conn.execute("SELECT * FROM dispositions WHERE id=?", (disposition_id,)).fetchone()))
        view["warehouse_effect"] = warehouse
        view["scope_basis"] = basis
        return view


def lift_disposition(store: Store, actor: Actor, disposition_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """解除一项处置；只有该批次不再受其他有效限制时库存才恢复可用。"""
    require(actor, "quality")
    reason = require_text(payload.get("reason"), "reason")
    target_type = payload.get("type") or payload.get("disposition_type")
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM dispositions WHERE id=?", (disposition_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "处置不存在", {"disposition": disposition_id})
        if row["status"] != "active":
            raise DomainError("conflict", "处置已不是有效状态", {"status": row["status"]})
        now = utcnow()
        if target_type and target_type != row["type"]:
            raise DomainError("invalid_argument", "要解除的处置类型与记录不符", {"expected": row["type"], "given": target_type})
        old_view = _disposition_view(conn, dict(row))
        conn.execute(
            "UPDATE dispositions SET status='lifted', lifted_by=?, lifted_at=?, lift_reason=?, lift_disposition=? WHERE id=?",
            (actor.id, now, reason, payload.get("disposition") or payload.get("lift_disposition"), disposition_id),
        )
        batches = old_view["effective_batch_ids"]
        still = {}
        restored_items: list[str] = []
        kept: list[str] = []
        for batch in batches:
            remaining = scope_mod.active_restrictions_for_batch(conn, batch)
            still[batch] = [d["id"] for d in remaining]
            if remaining:
                garments = conn.execute("SELECT id FROM garments WHERE batch_id=?", (batch,)).fetchall()
                kept.extend(g["id"] for g in garments)
                continue
            garments = conn.execute("SELECT id,state,current_outcome FROM garments WHERE batch_id=?", (batch,)).fetchall()
            for garment in garments:
                if garment["current_outcome"] in {"quarantine"}:
                    conn.execute(
                        "UPDATE garments SET current_outcome='awaiting_disposition', outcome_reason=?, updated_at=? WHERE id=?",
                        (f"依据 {disposition_id} 解除；回库仍需按实物状态复核", now, garment["id"]),
                    )
                    restored_items.append(garment["id"])
        # 批次解除全部限制后，未交运出库单的阻断标记清除；已交运仍按承运人事实。
        for batch in batches:
            if still[batch]:
                continue
            conn.execute(
                "UPDATE shipments SET blocked_reason=NULL, updated_at=? WHERE batch_id=? AND blocked_reason IS NOT NULL "
                "AND phase IN ('dispatch_pending','carrier_pending')",
                (now, batch),
            )
        # 被本次处置限制的换货履行恢复为可继续办理，但不自动消耗新库存。
        fulfillments = conn.execute("SELECT * FROM fulfillments WHERE status='needs_reprocessing'").fetchall()
        resumed = []
        for fulfillment in fulfillments:
            item = fulfillment["reserved_item"] or fulfillment["replacement_item"]
            if not item:
                continue
            garment = conn.execute("SELECT batch_id FROM garments WHERE id=?", (item,)).fetchone()
            if garment and garment["batch_id"] in batches and not scope_mod.active_restrictions_for_batch(conn, garment["batch_id"]):
                conn.execute("UPDATE fulfillments SET status='reserved', updated_at=? WHERE id=?", (now, fulfillment["id"]))
                # 重新启用原保留，避免出现"履行已恢复但库存无保留"的悬空状态。
                conn.execute(
                    "UPDATE reservations SET status='active', updated_at=?, note=? WHERE item_id=? AND complaint_id=? AND status='released'",
                    (now, f"处置 {disposition_id} 解除后恢复原保留", item, fulfillment["complaint_id"]),
                )
                resumed.append(fulfillment["id"])
        audit(conn, actor, "disposition.lift", "disposition", disposition_id, {"reason": reason, "still_restricted": still})
        view = _disposition_view(conn, dict(conn.execute("SELECT * FROM dispositions WHERE id=?", (disposition_id,)).fetchone()))
        view["remaining_restrictions"] = still
        view["restored_items"] = restored_items
        view["still_restricted_items"] = sorted(set(kept))
        view["resumed_fulfillments"] = resumed
        return view


def get_disposition(store: Store, disposition_id: str) -> dict[str, Any]:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM dispositions WHERE id=?", (disposition_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "处置不存在", {"disposition": disposition_id})
        view = _disposition_view(conn, dict(row))
        view["events"] = [dict(r) for r in conn.execute(
            "SELECT event_id,at,actor,role,action,detail_json FROM audit WHERE subject_type='disposition' AND subject_id=? ORDER BY event_id",
            (disposition_id,),
        ).fetchall()]
        return view


def list_dispositions(store: Store, query: dict[str, Any]) -> dict[str, Any]:
    with store.read() as conn:
        sql = "SELECT * FROM dispositions WHERE 1=1"
        params: list[Any] = []
        if query.get("status"):
            sql += " AND status=?"
            params.append(query["status"])
        if query.get("type"):
            sql += " AND type=?"
            params.append(query["type"])
        sql += " ORDER BY created_at, id"
        rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
        values = []
        for row in rows:
            view = _disposition_view(conn, row)
            if query.get("batch") and query["batch"] not in view["effective_batch_ids"]:
                continue
            values.append(view)
        return {"dispositions": values}
