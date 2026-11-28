"""售后办理：通知、顾客确认、换货履行、结案与人工待办。

原则：
- 替换品同款同价不代表顾客同意；没有独立确认不消耗另一批次库存；
- 已保留替换库存遇到新停供需重新处理，原退款权利保留；
- 同一订单多件商品分别推进，不能因一件已处理关闭整单；
- 没有确认批次的顾客仍可补证和查询，不强迫先选批次。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import scope as scope_mod
from .auth import Actor, audit, case_action, require
from .complaints import _garment_batch, complaint_view, item_explanation
from .domain import DomainError, require_text, utcnow
from .store import Store

CHOICES = {"refund", "exchange", "keep"}


def notify(store: Store, actor: Actor, complaint_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "independent-aftercare", "quality")
    with store.transaction() as conn:
        complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if not complaint:
            raise DomainError("not_found", "投诉不存在", {"complaint": complaint_id})
        scope_rows = conn.execute(
            "SELECT s.batch_id, s.basis FROM complaint_scopes s WHERE s.complaint_id=? ORDER BY s.batch_id", (complaint_id,)
        ).fetchall()
        if not scope_rows:
            raise DomainError("insufficient_scope", "尚未确认受影响批次，不能按范围通知；可继续补证或保留待查",
                              {"complaint": complaint_id})
        batch_ids = [r["batch_id"] for r in scope_rows]
        items = scope_mod.items_for_batches(conn, batch_ids)
        item_ids = [i["id"] for i in items]
        if payload.get("items"):
            requested = payload["items"]
            if isinstance(requested, str):
                requested = [requested]
            item_ids = [i for i in requested if i in item_ids]
            if not item_ids:
                raise DomainError("invalid_argument", "通知商品不在已确认范围内")
        message = payload.get("message") or "您的商品所在批次已按质量处置范围暂停处理，请选择退款、经确认的安全批次换货或保留商品。"
        # 通知范围按已确认批次对应到实际订单；本投诉自己的订单必在其中。
        if item_ids:
            order_rows = conn.execute(
                "SELECT DISTINCT l.order_id FROM order_lines l WHERE l.item_id IN ("
                + ",".join("?" for _ in item_ids) + ")",
                tuple(item_ids),
            ).fetchall()
        else:
            order_rows = []
        if complaint["order_id"]:
            order_rows = list(order_rows) + [{"order_id": complaint["order_id"]}]
        affected_orders = sorted({row["order_id"] for row in order_rows if row["order_id"]})
        if not affected_orders:
            raise DomainError("insufficient_scope", "确认范围内没有可通知的订单", {"complaint": complaint_id})
        notification_id = require_text(payload.get("id") or f"notice-{complaint_id}-{len(item_ids)}", "id")
        if conn.execute("SELECT 1 FROM notifications WHERE id=?", (notification_id,)).fetchone():
            raise DomainError("conflict", "通知编号已存在", {"notification": notification_id})
        now = utcnow()
        conn.execute(
            "INSERT INTO notifications(id,complaint_id,item_id,order_id,scope,batch_ids_json,message,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (notification_id, complaint_id, None, complaint["order_id"], "confirmed_batches", json.dumps(batch_ids, ensure_ascii=False),
             message, actor.id, now),
        )
        conn.execute("UPDATE complaints SET status='notified', stage='notified', updated_at=? WHERE id=?", (now, complaint_id))
        audit(conn, actor, "complaint.notify", "complaint", complaint_id,
              {"notification": notification_id, "batches": batch_ids, "items": item_ids, "orders": affected_orders})
        case_action(conn, actor, "customer.notified", complaint_id,
                    {"notification": notification_id, "items": item_ids, "orders": affected_orders}, order_id=complaint["order_id"])
        view = complaint_view(store, complaint_id)
        view["notification_result"] = {"notification": notification_id, "batches": batch_ids,
                                       "items": item_ids, "orders": affected_orders}
        return view


def record_choice(store: Store, actor: Actor, complaint_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """顾客选择必须由顾客本人确认；商家代填只能标记未确认。"""
    require(actor, "consumer", "independent-aftercare", "merchant")
    choice = require_text(payload.get("choice"), "choice")
    if choice not in CHOICES:
        raise DomainError("invalid_argument", "顾客选择只能是 refund/exchange/keep", {"choice": choice})
    item_id = require_text(payload.get("item"), "item")
    choice_id = require_text(payload.get("id") or f"choice-{complaint_id}-{item_id}-{choice}", "id")
    with store.transaction() as conn:
        complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if not complaint:
            raise DomainError("not_found", "投诉不存在", {"complaint": complaint_id})
        target = conn.execute("SELECT * FROM complaint_targets WHERE complaint_id=? AND item_id=?", (complaint_id, item_id)).fetchone()
        if not target:
            raise DomainError("not_found", "该商品不在本投诉的分项中", {"item": item_id})
        if actor.role == "consumer":
            _assert_consumer_owns(conn, complaint, actor)
        if conn.execute("SELECT 1 FROM choices WHERE id=?", (choice_id,)).fetchone():
            raise DomainError("conflict", "顾客选择编号已存在", {"choice": choice_id})
        confirmed = actor.role == "consumer" and payload.get("confirmed", True) is not False
        if actor.role == "merchant":
            confirmed = False  # 商家代填不等于顾客同意
        now = utcnow()
        conn.execute(
            "INSERT INTO choices(id,complaint_id,item_id,choice,confirmed,confirmed_by,confirmed_at,note) VALUES(?,?,?,?,?,?,?,?)",
            (choice_id, complaint_id, item_id, choice, 1 if confirmed else 0, actor.id if confirmed else None,
             now if confirmed else None, payload.get("note")),
        )
        if target["batch_id"] is None:
            batch = _garment_batch(conn, item_id)
            conn.execute("UPDATE complaint_targets SET batch_id=? WHERE complaint_id=? AND item_id=?", (batch, complaint_id, item_id))
        if confirmed:
            conn.execute("UPDATE complaints SET status='choice_made', updated_at=? WHERE id=?", (now, complaint_id))
            if choice in {"refund", "keep"}:
                issue_id = f"issue-choice-{choice_id}"
                detail = ("顾客选择退款，等待售后复核实际交易与处置依据" if choice == "refund"
                          else "顾客选择保留商品，需人工记录商品去向与告知")
                conn.execute(
                    "INSERT OR IGNORE INTO issues(id,complaint_id,item_id,order_id,kind,status,detail,priority,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (issue_id, complaint_id, item_id, complaint["order_id"], "manual_followup", "open", detail, 2, actor.id, now),
                )
        audit(conn, actor, "complaint.choice", "complaint", complaint_id, {"item": item_id, "choice": choice, "confirmed": confirmed})
        case_action(conn, actor, "choice.recorded", complaint_id, {"item": item_id, "choice": choice, "confirmed": confirmed}, item_id=item_id)
        return complaint_view(store, complaint_id)


def reserve_exchange(store: Store, actor: Actor, complaint_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """为已确认换货的顾客保留替换库存。

    只允许顾客已确认的选择；替换品同款同价不代表同意。
    受质量限制的批次不能作为替换库存。
    """
    require(actor, "independent-aftercare", "warehouse")
    item_id = require_text(payload.get("item"), "item")
    replacement = require_text(payload.get("replacement_item") or payload.get("reserve_item"), "replacement_item")
    choice_id = payload.get("choice")
    with store.transaction() as conn:
        complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if not complaint:
            raise DomainError("not_found", "投诉不存在", {"complaint": complaint_id})
        query = "SELECT * FROM choices WHERE complaint_id=? AND item_id=? AND choice='exchange'"
        params: list[Any] = [complaint_id, item_id]
        if choice_id:
            query += " AND id=?"
            params.append(choice_id)
        choice = conn.execute(query + " ORDER BY id DESC", tuple(params)).fetchone()
        if not choice:
            raise DomainError("choice_required", "顾客尚未确认换货选择，不能消耗替换库存", {"complaint": complaint_id, "item": item_id})
        if not choice["confirmed"]:
            raise DomainError("consent_required", "换货选择尚未由顾客确认，不能保留或消耗另一批次库存",
                              {"choice": choice["id"]})
        garment = conn.execute("SELECT * FROM garments WHERE id=?", (replacement,)).fetchone()
        if not garment:
            raise DomainError("not_found", "替换商品不存在", {"replacement_item": replacement})
        restrictions = scope_mod.active_restrictions_for_batch(conn, garment["batch_id"])
        if restrictions:
            raise DomainError("blocked", "替换商品所在批次受质量处置限制，不能作为替换库存",
                              {"batch": garment["batch_id"], "dispositions": [d["id"] for d in restrictions]})
        implicated = {row["batch_id"] for row in conn.execute(
            "SELECT batch_id FROM complaint_scopes WHERE complaint_id=?", (complaint_id,)).fetchall()}
        if garment["batch_id"] in implicated:
            raise DomainError("unsafe_replacement", "替换库存不能来自本投诉待查或受影响的批次",
                              {"batch": garment["batch_id"], "complaint": complaint_id})
        existing = conn.execute("SELECT * FROM reservations WHERE item_id=? AND status='active'", (replacement,)).fetchone()
        if existing:
            raise DomainError("conflict", "替换商品已被其他投诉保留", {"item": replacement, "complaint": existing["complaint_id"]})
        now = utcnow()
        reservation_id = require_text(payload.get("id") or f"res-{complaint_id}-{replacement}", "id")
        fulfillment_id = require_text(payload.get("fulfillment_id") or f"ful-{complaint_id}-{replacement}", "fulfillment_id")
        conn.execute(
            "INSERT INTO reservations(id,item_id,complaint_id,order_id,status,created_by,created_at,updated_at,note) VALUES(?,?,?,?,?,?,?,?,?)",
            (reservation_id, replacement, complaint_id, complaint["order_id"], "active", actor.id, now, now, payload.get("note")),
        )
        conn.execute(
            "INSERT INTO fulfillments(id,complaint_id,item_id,kind,status,reserved_item,replacement_item,detail_json,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (fulfillment_id, complaint_id, item_id, "exchange", "reserved", replacement, replacement,
             json.dumps({"choice": choice["id"], "requested_shipment": payload.get("shipment")}, ensure_ascii=False), actor.id, now, now),
        )
        conn.execute("UPDATE garments SET current_outcome='exchange_reserved', outcome_reason=?, updated_at=? WHERE id=?",
                     (f"为投诉 {complaint_id} 保留替换；尚未交运", now, replacement))
        audit(conn, actor, "exchange.reserve", "fulfillment", fulfillment_id, {"complaint": complaint_id, "replacement": replacement})
        case_action(conn, actor, "exchange.reserved", complaint_id, {"fulfillment": fulfillment_id, "replacement": replacement}, item_id=item_id)
        return complaint_view(store, complaint_id)


def advance_exchange(store: Store, actor: Actor, complaint_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """推进换货履行，遇到限制保持待重办且不吞掉退款权利。"""
    require(actor, "warehouse", "independent-aftercare")
    fulfillment_id = require_text(payload.get("fulfillment"), "fulfillment")
    action = require_text(payload.get("action"), "action")
    with store.transaction() as conn:
        fulfillment = conn.execute("SELECT * FROM fulfillments WHERE id=? AND complaint_id=?", (fulfillment_id, complaint_id)).fetchone()
        if not fulfillment:
            raise DomainError("not_found", "换货履行不存在", {"fulfillment": fulfillment_id})
        replacement = fulfillment["replacement_item"]
        garment = conn.execute("SELECT * FROM garments WHERE id=?", (replacement,)).fetchone()
        restrictions = scope_mod.active_restrictions_for_batch(conn, garment["batch_id"]) if garment else []
        now = utcnow()
        if action == "dispatch":
            if restrictions:
                conn.execute("UPDATE fulfillments SET status='needs_reprocessing', updated_at=? WHERE id=?", (now, fulfillment_id))
                audit(conn, actor, "exchange.blocked", "fulfillment", fulfillment_id, {"dispositions": [d["id"] for d in restrictions]})
                raise DomainError("blocked", "替换库存所在批次受质量处置限制，需重新处理履行", {
                    "fulfillment": fulfillment_id, "dispositions": [d["id"] for d in restrictions],
                    "refund_right_preserved": True})
            shipment_id = require_text(payload.get("shipment_id") or f"ship-{fulfillment_id}", "shipment_id")
            if conn.execute("SELECT 1 FROM shipments WHERE id=?", (shipment_id,)).fetchone():
                raise DomainError("conflict", "出库单号已存在", {"shipment": shipment_id})
            conn.execute(
                "INSERT INTO shipments(id,kind,order_id,item_id,batch_id,phase,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (shipment_id, "exchange", customer_order(conn, complaint_id), replacement, garment["batch_id"], "carrier_pending", actor.id, now, now),
            )
            conn.execute("INSERT INTO shipment_events(shipment_id,state,source,detail_json,at) VALUES(?,?,?,?,?)",
                         (shipment_id, "carrier_pending", actor.role, "{}", now))
            conn.execute("UPDATE fulfillments SET status='dispatching', shipment_id=?, updated_at=? WHERE id=?", (shipment_id, now, fulfillment_id))
            conn.execute("UPDATE garments SET current_outcome='exchange_reserved', outcome_reason=?, updated_at=? WHERE id=?",
                         (f"替换出库单 {shipment_id} 待交运", now, replacement))
        elif action == "complete":
            shipment = conn.execute("SELECT * FROM shipments WHERE id=?", (fulfillment["shipment_id"],)).fetchone() if fulfillment["shipment_id"] else None
            if not shipment or shipment["phase"] not in {"with_carrier", "delivered"}:
                raise DomainError("conflict", "替换尚未交运，不能标记完成", {"fulfillment": fulfillment_id})
            conn.execute("UPDATE fulfillments SET status='done', updated_at=? WHERE id=?", (now, fulfillment_id))
            conn.execute("UPDATE reservations SET status='consumed', updated_at=? WHERE item_id=? AND complaint_id=? AND status='active'",
                         (now, replacement, complaint_id))
        elif action == "cancel":
            reason = require_text(payload.get("reason"), "reason")
            conn.execute("UPDATE fulfillments SET status='cancelled', updated_at=?, detail_json=? WHERE id=?",
                         (now, json.dumps({"cancel_reason": reason}, ensure_ascii=False), fulfillment_id))
            conn.execute("UPDATE reservations SET status='released', updated_at=?, note=? WHERE item_id=? AND complaint_id=? AND status='active'",
                         (now, reason, replacement, complaint_id))
            conn.execute("UPDATE garments SET current_outcome=NULL, outcome_reason=?, updated_at=? WHERE id=?",
                         (f"换货取消：{reason}；原退款权利不受影响", now, replacement))
        else:
            raise DomainError("invalid_argument", "未知换货动作", {"action": action})
        audit(conn, actor, "exchange.advance", "fulfillment", fulfillment_id, {"action": action})
        case_action(conn, actor, f"exchange.{action}", complaint_id, {"fulfillment": fulfillment_id, "action": action}, item_id=fulfillment["item_id"])
        return complaint_view(store, complaint_id)


def _assert_consumer_owns(conn: sqlite3.Connection, complaint: Any, actor: Actor) -> None:
    """消费者只能确认自己订单/自己投诉的选择；没有归属记录时不臆测。"""
    if complaint["reporter"] == actor.id:
        return
    if complaint["order_id"]:
        row = conn.execute("SELECT customer FROM orders WHERE id=?", (complaint["order_id"],)).fetchone()
        if row and row["customer"] == actor.id:
            return
        raise DomainError("forbidden", "只能确认本人订单中的选择", {"complaint": complaint["id"]})
    clues = json.loads(complaint["clues_json"] or "{}")
    if clues.get("reporter") and clues["reporter"] != actor.id:
        raise DomainError("forbidden", "只能确认本人投诉的选择", {"complaint": complaint["id"]})


def customer_order(conn: sqlite3.Connection, complaint_id: str) -> str | None:
    row = conn.execute("SELECT order_id FROM complaints WHERE id=?", (complaint_id,)).fetchone()
    return row["order_id"] if row else None


def resolve_item(store: Store, actor: Actor, complaint_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """按一件商品结案；同一订单其他商品保持未结。"""
    require(actor, "independent-aftercare")
    item_id = require_text(payload.get("item"), "item")
    reason = require_text(payload.get("reason"), "reason")
    with store.transaction() as conn:
        complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if not complaint:
            raise DomainError("not_found", "投诉不存在", {"complaint": complaint_id})
        target = conn.execute("SELECT * FROM complaint_targets WHERE complaint_id=? AND item_id=?", (complaint_id, item_id)).fetchone()
        if not target:
            raise DomainError("not_found", "该商品不在本投诉分项中", {"item": item_id})
        pending_payments = conn.execute("SELECT COUNT(*) c FROM payments WHERE complaint_id=? AND item_id=? AND state='pending_payment'",
                                        (complaint_id, item_id)).fetchone()["c"]
        if pending_payments:
            raise DomainError("conflict", "该商品仍有待查赔付，不能结案", {"item": item_id, "pending_payments": pending_payments})
        open_fulfillments = conn.execute(
            "SELECT COUNT(*) c FROM fulfillments WHERE complaint_id=? AND item_id=? AND status IN ('reserved','dispatching','needs_reprocessing')",
            (complaint_id, item_id)).fetchone()["c"]
        if open_fulfillments and not payload.get("force"):
            raise DomainError("conflict", "该商品仍有未完成换货履行", {"item": item_id})
        now = utcnow()
        conn.execute("UPDATE complaint_targets SET status='resolved', resolution=?, resolved_at=? WHERE complaint_id=? AND item_id=?",
                     (reason, now, complaint_id, item_id))
        remaining = conn.execute("SELECT COUNT(*) c FROM complaint_targets WHERE complaint_id=? AND status<>'resolved'", (complaint_id,)).fetchone()["c"]
        if remaining == 0:
            conn.execute("UPDATE complaints SET status='resolved', stage='closed', closed_at=?, close_reason=?, updated_at=? WHERE id=?",
                         (now, reason, now, complaint_id))
        audit(conn, actor, "complaint.item.resolve", "complaint", complaint_id, {"item": item_id, "reason": reason, "remaining_items": remaining})
        case_action(conn, actor, "item.resolved", complaint_id, {"item": item_id, "reason": reason, "remaining_items": remaining}, item_id=item_id)
        return complaint_view(store, complaint_id)


def open_issue(store: Store, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "independent-aftercare", "quality", "warehouse", "merchant", "system")
    issue_id = require_text(payload.get("id") or f"issue-{payload.get('kind','manual')}-{utcnow()}", "id")
    kind = require_text(payload.get("kind") or "manual_followup", "kind")
    detail = require_text(payload.get("detail"), "detail")
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM issues WHERE id=?", (issue_id,)).fetchone():
            raise DomainError("conflict", "待办编号已存在", {"issue": issue_id})
        now = utcnow()
        conn.execute(
            "INSERT INTO issues(id,complaint_id,item_id,order_id,kind,status,detail,priority,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (issue_id, payload.get("complaint"), payload.get("item"), payload.get("order"), kind, "open", detail,
             int(payload.get("priority") or 0), actor.id, now),
        )
        audit(conn, actor, "issue.open", "issue", issue_id, {"kind": kind, "detail": detail})
        return {"id": issue_id, "kind": kind, "status": "open", "detail": detail}


def resolve_issue(store: Store, actor: Actor, issue_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "independent-aftercare", "quality")
    resolution = require_text(payload.get("resolution"), "resolution")
    with store.transaction() as conn:
        issue = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if not issue:
            raise DomainError("not_found", "待办不存在", {"issue": issue_id})
        if issue["status"] != "open":
            raise DomainError("conflict", "待办已关闭", {"issue": issue_id})
        now = utcnow()
        conn.execute("UPDATE issues SET status='resolved', resolved_by=?, resolved_at=?, resolution=? WHERE id=?",
                     (actor.id, now, resolution, issue_id))
        audit(conn, actor, "issue.resolve", "issue", issue_id, {"resolution": resolution})
        return {"id": issue_id, "status": "resolved", "resolution": resolution}


def list_issues(store: Store, query: dict[str, Any]) -> dict[str, Any]:
    with store.read() as conn:
        sql = "SELECT * FROM issues WHERE 1=1"
        params: list[Any] = []
        for field in ("complaint_id", "item_id", "order_id", "status", "kind"):
            if query.get(field):
                sql += f" AND {field}=?"
                params.append(query[field])
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY priority DESC, created_at, id", tuple(params)).fetchall()]
        return {"issues": rows}


def case_explanation(store: Store, complaint_id: str) -> dict[str, Any]:
    """一件商品级别的解释汇总，含未结事项与顾客可继续做的动作。"""
    view = complaint_view(store, complaint_id)
    items = []
    with store.read() as conn:
        for target in view["targets"]:
            explanation = item_explanation(conn, complaint_id, target["item_id"])
            items.append(explanation)
    view["item_explanations"] = items
    view["pending_actions"] = sorted({p for i in items for p in i["pending"]})
    view["customer_rights"] = {
        "can_add_evidence": True,
        "can_query_without_choosing_batch": True,
        "note": "未确认批次的分项仍可补证与查询；系统不会要求顾客先选一个可能错误的批次。",
    }
    return view
