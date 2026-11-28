"""退货入库：先按该件的批次和当前处置判断去向，退款不自动导致重新销售。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import scope as scope_mod
from .auth import Actor, audit, case_action, require
from .domain import DomainError, require_text, utcnow
from .store import Store


def receive_return(store: Store, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "warehouse", "quality")
    return_id = require_text(payload.get("id"), "id")
    item_id = require_text(payload.get("item"), "item")
    condition = require_text(payload.get("condition"), "condition")
    with store.transaction() as conn:
        garment = conn.execute("SELECT * FROM garments WHERE id=?", (item_id,)).fetchone()
        if not garment:
            raise DomainError("not_found", "商品不存在", {"item": item_id})
        if conn.execute("SELECT 1 FROM returns WHERE id=?", (return_id,)).fetchone():
            raise DomainError("conflict", "退货接收编号已存在", {"return": return_id})
        order = payload.get("order")
        complaint = payload.get("complaint")
        if order and not conn.execute("SELECT 1 FROM orders WHERE id=?", (order,)).fetchone():
            raise DomainError("not_found", "订单不存在", {"order": order})
        if complaint and not conn.execute("SELECT 1 FROM complaints WHERE id=?", (complaint,)).fetchone():
            raise DomainError("not_found", "投诉不存在", {"complaint": complaint})
        restrictions = scope_mod.active_restrictions_for_batch(conn, garment["batch_id"])
        if restrictions:
            outcome = "quarantine"
            outcome_reason = "该件批次受有效质量处置限制：" + "、".join(d["id"] for d in restrictions)
        elif garment["current_outcome"] == "scrap":
            outcome = "scrap"
            outcome_reason = "该件已有报废去向记录"
        elif garment["state"] == "in_stock" and garment["current_outcome"] in {"resell", None}:
            outcome = "awaiting_disposition"
            outcome_reason = "退货已接收；未发现有效质量限制，仍须完成状态复核后才可再销售"
        else:
            outcome = "awaiting_disposition"
            outcome_reason = "退货已接收；需按实际状况完成复核"
        now = utcnow()
        conn.execute(
            "INSERT INTO returns(id,item_id,order_id,complaint_id,condition,received_by,received_at,warehouse_outcome,outcome_reason,restricted_batch,disposition_basis_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (return_id, item_id, order, complaint, condition, actor.id, now, outcome, outcome_reason,
             1 if restrictions else 0, json.dumps([d["id"] for d in restrictions], ensure_ascii=False)),
        )
        conn.execute(
            "UPDATE garments SET state='returned', current_outcome=?, outcome_reason=?, updated_at=? WHERE id=?",
            (outcome, outcome_reason, now, item_id),
        )
        audit(conn, actor, "return.receive", "return", return_id, {"item": item_id, "outcome": outcome, "restrictions": [d["id"] for d in restrictions]})
        if complaint:
            case_action(conn, actor, "return.received", complaint, {"return": return_id, "outcome": outcome}, item_id=item_id, order_id=order)
        return return_view(conn, return_id)


def _return_view(conn: sqlite3.Connection, return_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM returns WHERE id=?", (return_id,)).fetchone()
    if not row:
        raise DomainError("not_found", "退货记录不存在", {"return": return_id})
    value = dict(row)
    value["disposition_basis"] = json.loads(value.pop("disposition_basis_json") or "[]")
    value["restricted_batch"] = bool(value["restricted_batch"])
    value["may_resell"] = value["warehouse_outcome"] == "resell"
    value["note"] = "退款状态不改变本件的批次处置；仓库按批次与当前处置决定再销售、隔离或报废。"
    return value


def return_view(conn: sqlite3.Connection, return_id: str) -> dict[str, Any]:
    return _return_view(conn, return_id)


def decide_return(store: Store, actor: Actor, return_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """复核退货去向。不得在批次受限制时改为可售。"""
    require(actor, "warehouse", "quality")
    outcome = require_text(payload.get("outcome"), "outcome")
    if outcome not in {"resell", "quarantine", "scrap"}:
        raise DomainError("invalid_argument", "退货去向只能是 resell/quarantine/scrap", {"outcome": outcome})
    reason = require_text(payload.get("reason"), "reason")
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM returns WHERE id=?", (return_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "退货记录不存在", {"return": return_id})
        garment = conn.execute("SELECT * FROM garments WHERE id=?", (row["item_id"],)).fetchone()
        restrictions = scope_mod.active_restrictions_for_batch(conn, garment["batch_id"])
        if outcome == "resell" and restrictions:
            raise DomainError("blocked", "该件批次仍受质量处置限制，不能转为可售", {
                "return": return_id, "batch": garment["batch_id"], "dispositions": [d["id"] for d in restrictions]})
        now = utcnow()
        conn.execute("UPDATE returns SET warehouse_outcome=?, outcome_reason=?, reviewed_by=?, reviewed_at=? WHERE id=?",
                     (outcome, reason, actor.id, now, return_id))
        new_state = "in_stock" if outcome == "resell" else "returned"
        conn.execute("UPDATE garments SET state=?, current_outcome=?, outcome_reason=?, updated_at=? WHERE id=?",
                     (new_state, outcome, reason, now, row["item_id"]))
        audit(conn, actor, "return.decide", "return", return_id, {"outcome": outcome, "reason": reason, "restrictions": [d["id"] for d in restrictions]})
        if row["complaint_id"]:
            case_action(conn, actor, "return.decided", row["complaint_id"], {"return": return_id, "outcome": outcome}, item_id=row["item_id"], order_id=row["order_id"])
        return _return_view(conn, return_id)


def list_returns(store: Store, query: dict[str, Any]) -> dict[str, Any]:
    with store.read() as conn:
        sql = "SELECT id FROM returns WHERE 1=1"
        params: list[Any] = []
        if query.get("item"):
            sql += " AND item_id=?"
            params.append(query["item"])
        if query.get("complaint"):
            sql += " AND complaint_id=?"
            params.append(query["complaint"])
        rows = conn.execute(sql + " ORDER BY received_at,id", tuple(params)).fetchall()
        return {"returns": [_return_view(conn, r["id"]) for r in rows]}
