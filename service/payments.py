"""赔付：复核实际交易、已退金额与处置依据；支付未确认时保持待查。

金额单位：分。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .auth import Actor, audit, case_action, require
from .domain import DomainError, require_int, require_text, utcnow
from .store import Store

PAYMENT_KINDS = {"refund", "exchange_refund", "compensation"}
REFUND_KINDS = {"refund", "exchange_refund"}
PAYMENT_STATES = {"pending_payment", "paid", "failed", "cancelled", "reversed"}


def _line_paid(conn: sqlite3.Connection, order_id: str, item_id: str) -> int:
    row = conn.execute("SELECT paid FROM order_lines WHERE order_id=? AND item_id=?", (order_id, item_id)).fetchone()
    return row["paid"] if row else -1


def _sum(conn: sqlite3.Connection, order_id: str, item_id: str, states: tuple[str, ...], kinds: tuple[str, ...], exclude: str | None = None) -> int:
    ph_s = ",".join("?" for _ in states)
    ph_k = ",".join("?" for _ in kinds)
    params: list[Any] = [order_id, item_id, *states, *kinds]
    sql = f"SELECT COALESCE(SUM(amount),0) s FROM payments WHERE order_id=? AND item_id=? AND state IN ({ph_s}) AND kind IN ({ph_k})"
    if exclude:
        sql += " AND id<>?"
        params.append(exclude)
    return conn.execute(sql, tuple(params)).fetchone()["s"]


def entitlement(conn: sqlite3.Connection, order_id: str, item_id: str) -> dict[str, Any]:
    paid = _line_paid(conn, order_id, item_id)
    if paid < 0:
        raise DomainError("not_found", "订单中没有该商品分项", {"order": order_id, "item": item_id})
    refunded = _sum(conn, order_id, item_id, ("paid",), tuple(REFUND_KINDS))
    pending = _sum(conn, order_id, item_id, ("pending_payment",), tuple(REFUND_KINDS))
    return {
        "order": order_id,
        "item": item_id,
        "paid": paid,
        "refunded": refunded,
        "pending_reserved": pending,
        "remaining_refundable": max(0, paid - refunded - pending),
    }


def create_payment(store: Store, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "independent-aftercare")
    payment_id = require_text(payload.get("id"), "id")
    order_id = require_text(payload.get("order"), "order")
    item_id = require_text(payload.get("item"), "item")
    kind = require_text(payload.get("kind") or "refund", "kind")
    if kind not in PAYMENT_KINDS:
        raise DomainError("invalid_argument", "未知赔付类型", {"kind": kind})
    amount = require_int(payload.get("amount"), "amount", 1)
    reason = require_text(payload.get("reason"), "reason")
    basis = payload.get("basis") or []
    if isinstance(basis, str):
        basis = [basis]
    if not basis:
        raise DomainError("invalid_argument", "赔付必须列明处置依据（报告/处置/已确认投诉）")
    with store.transaction() as conn:
        if not conn.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone():
            raise DomainError("not_found", "订单不存在", {"order": order_id})
        if not conn.execute("SELECT 1 FROM garments WHERE id=?", (item_id,)).fetchone():
            raise DomainError("not_found", "商品不存在", {"item": item_id})
        if conn.execute("SELECT 1 FROM payments WHERE id=?", (payment_id,)).fetchone():
            raise DomainError("conflict", "赔付编号已存在", {"payment": payment_id})
        for ref in basis:
            row = conn.execute("SELECT 1 FROM reports WHERE id=?", (ref,)).fetchone()
            row = row or conn.execute("SELECT 1 FROM dispositions WHERE id=?", (ref,)).fetchone()
            row = row or conn.execute("SELECT 1 FROM complaint_scopes WHERE complaint_id=?", (ref,)).fetchone()
            if not row:
                raise DomainError("invalid_argument", "处置依据不存在", {"basis": ref})
        complaint_id = payload.get("complaint")
        if complaint_id and not conn.execute("SELECT 1 FROM complaints WHERE id=?", (complaint_id,)).fetchone():
            raise DomainError("not_found", "投诉不存在", {"complaint": complaint_id})
        if kind == "compensation" and not payload.get("review_note"):
            raise DomainError("invalid_argument", "额外补偿需要独立复核记录 review_note")
        paid_line = _line_paid(conn, order_id, item_id)
        if paid_line < 0:
            raise DomainError("not_found", "订单中没有该商品分项，不能凭款号赔付", {"order": order_id, "item": item_id})
        refunded = _sum(conn, order_id, item_id, ("paid",), tuple(REFUND_KINDS))
        pending = _sum(conn, order_id, item_id, ("pending_payment",), tuple(REFUND_KINDS))
        if kind in REFUND_KINDS:
            if amount > paid_line:
                raise DomainError("exceeds_transaction", "赔付不得超过该分项实际成交金额", {"paid": paid_line, "amount": amount})
            if refunded + pending + amount > paid_line:
                raise DomainError(
                    "duplicate_payment",
                    "该分项已有已付或待查退款，不能再按退款与换货退款各付一遍",
                    {"paid": paid_line, "refunded": refunded, "pending": pending, "amount": amount},
                )
        returned = conn.execute("SELECT 1 FROM returns WHERE item_id=? AND order_id=?", (item_id, order_id)).fetchone()
        not_returned = bool(payload.get("not_returned"))
        if not returned and not not_returned:
            raise DomainError("invalid_argument", "没有退货记录时须明确 not_returned，并写明商品去向与顾客告知")
        if not_returned and not returned:
            if not payload.get("item_disposition") or not payload.get("customer_notice"):
                raise DomainError("invalid_argument", "不退回商品时须记录 item_disposition 与 customer_notice，不得伪造回收入库")
        now = utcnow()
        conn.execute(
            "INSERT INTO payments(id,order_id,item_id,complaint_id,kind,amount,state,payment_command_id,voucher,reviewed_by,reviewed_at,review_note,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payment_id, order_id, item_id, complaint_id, kind, amount, "pending_payment", payload.get("command_id"),
             None, actor.id, now, payload.get("review_note"), actor.id, now, now),
        )
        details = {
            "returned": bool(returned),
            "not_returned": not_returned and not returned,
            "item_disposition": payload.get("item_disposition"),
            "customer_notice": payload.get("customer_notice"),
            "basis": basis,
            "entitlement": entitlement(conn, order_id, item_id),
        }
        conn.execute("INSERT INTO payment_links(id,payment_id,kind,ref_id,created_at) VALUES(?,?,?,?,?)",
                     (f"link-{payment_id}-{len(basis)}", payment_id, "basis", json.dumps(basis, ensure_ascii=False), now))
        audit(conn, actor, "payment.create", "payment", payment_id, {"kind": kind, "amount": amount, "details": details})
        if complaint_id:
            case_action(conn, actor, "payment.created", complaint_id, {"payment": payment_id, "amount": amount, "kind": kind,
                                                                       "not_returned": details["not_returned"]}, item_id=item_id, order_id=order_id)
            current = conn.execute("SELECT status FROM complaints WHERE id=?", (complaint_id,)).fetchone()
            if current and current["status"] not in {"resolved"}:
                conn.execute("UPDATE complaints SET status='pending_payment', updated_at=? WHERE id=?", (now, complaint_id))
        row = dict(conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone())
        view = _payment_view(conn, row)
        view["review"] = details
        return view


def _payment_view(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    value = {
        "id": row["id"],
        "order": row["order_id"],
        "item": row["item_id"],
        "complaint": row["complaint_id"],
        "kind": row["kind"],
        "amount": row["amount"],
        "state": row["state"],
        "command_id": row["payment_command_id"],
        "voucher": row["voucher"],
        "reviewed_by": row["reviewed_by"],
        "reviewed_at": row["reviewed_at"],
        "review_note": row["review_note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "state_label": {
            "pending_payment": "支付受理结果待查，该笔仍待处理",
            "paid": "协作方已确认支付",
            "failed": "协作方确认未支付",
            "cancelled": "已撤销申请",
            "reversed": "已冲正",
        }.get(row["state"], row["state"]),
    }
    value["entitlement"] = entitlement(conn, row["order_id"], row["item_id"])
    value["links"] = [dict(r) for r in conn.execute("SELECT kind,ref_id FROM payment_links WHERE payment_id=?", (row["id"],)).fetchall()]
    return value


def get_payment(store: Store, payment_id: str) -> dict[str, Any]:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "赔付记录不存在", {"payment": payment_id})
        return _payment_view(conn, dict(row))


def submit_payment(store: Store, actor: Actor, payment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """登记向支付侧发送的流水；受理结果不明确时仍待查。"""
    require(actor, "independent-aftercare", "system")
    command_id = require_text(payload.get("command_id"), "command_id")
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "赔付记录不存在", {"payment": payment_id})
        if row["state"] in {"paid", "cancelled", "failed"}:
            raise DomainError("conflict", "该笔赔付已有确定结果", {"state": row["state"]})
        if row["payment_command_id"] and row["payment_command_id"] != command_id:
            raise DomainError("conflict", "该笔赔付已关联另一支付流水，不能同时发起第二笔", {
                "payment": payment_id, "existing": row["payment_command_id"], "given": command_id})
        holder = conn.execute("SELECT id FROM payments WHERE payment_command_id=? AND id<>?", (command_id, payment_id)).fetchone()
        if holder:
            raise DomainError("conflict", "该支付流水已关联其他赔付", {"payment": holder["id"]})
        now = utcnow()
        conn.execute("UPDATE payments SET payment_command_id=?, state='pending_payment', updated_at=? WHERE id=?", (command_id, now, payment_id))
        # 支付受理未知期间保留一项未结责任；协作方确认已付后据实解除。
        issue_id = f"issue-pending-payment-{payment_id}"
        conn.execute(
            "INSERT OR IGNORE INTO issues(id,complaint_id,item_id,order_id,kind,status,detail,priority,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (issue_id, row["complaint_id"], row["item_id"], row["order_id"], "refund_shortfall", "open",
             f"赔付 {payment_id} 已提交支付侧，受理结果待查；不得重复支付", 3, actor.id, now),
        )
        # 登记协作方流水，便于之后按次读取观测；没有外部地址时也可离线写入观测。
        existing = conn.execute("SELECT * FROM partners WHERE command_id=?", (command_id,)).fetchone()
        request = {"order": row["order_id"], "item": row["item_id"], "amount": row["amount"]}
        if not existing:
            conn.execute(
                "INSERT INTO partners(command_id,operation,request_json,linked_kind,linked_id,first_status,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (command_id, "refund", json.dumps(request, ensure_ascii=False, sort_keys=True), "payment", payment_id,
                 None, "registered", now, now),
            )
        audit(conn, actor, "payment.submit", "payment", payment_id, {"command_id": command_id,
                                                                     "note": "受理状态未知时保持待查，不视为已付或失败"})
        return _payment_view(conn, dict(conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()))


def apply_partner_result(conn: sqlite3.Connection, command_id: str, observation: dict[str, Any], actor: Actor | None = None) -> dict[str, Any] | None:
    """把支付侧观测落到赔付记录。仅 paid/failed 改变责任状态。"""
    row = conn.execute("SELECT * FROM payments WHERE payment_command_id=?", (command_id,)).fetchone()
    if not row:
        return None
    state = observation.get("state")
    if state in {"pending", "unknown", None}:
        return {"payment": row["id"], "state": "pending_payment", "note": "支付侧受理未知，仍待查"}
    if state == "paid":
        amount = observation.get("amount")
        if amount is not None and amount != row["amount"]:
            raise DomainError("invalid_argument", "支付金额与复核金额不一致", {"expected": row["amount"], "actual": amount})
        now = utcnow()
        conn.execute("UPDATE payments SET state='paid', voucher=?, updated_at=? WHERE id=?", (observation.get("voucher"), now, row["id"]))
        if actor:
            audit(conn, actor, "payment.paid", "payment", row["id"], {"voucher": observation.get("voucher")})
        # 据实解除未结责任。
        open_issues = conn.execute(
            "SELECT * FROM issues WHERE status='open' AND (order_id=? AND item_id=?)", (row["order_id"], row["item_id"])
        ).fetchall()
        for issue in open_issues:
            if issue["kind"] in {"duplicate_payment_review", "refund_shortfall", "manual_followup"}:
                conn.execute("UPDATE issues SET status='resolved', resolved_at=?, resolution=? WHERE id=?",
                             (now, "支付侧已确认该笔款项，相应未结责任据实解除", issue["id"]))
        if row["complaint_id"]:
            case_action(conn, actor or Actor("system:partner", "system"), "payment.paid", row["complaint_id"],
                        {"payment": row["id"], "voucher": observation.get("voucher")}, item_id=row["item_id"], order_id=row["order_id"])
        return {"payment": row["id"], "state": "paid", "voucher": observation.get("voucher")}
    if state in {"failed", "cancelled"}:
        now = utcnow()
        conn.execute("UPDATE payments SET state='failed', updated_at=? WHERE id=?", (now, row["id"]))
        # 支付侧确认未付：未结责任保留给人工改用其他方式处理，不能视为已赔付。
        conn.execute(
            "UPDATE issues SET detail=? WHERE id=? AND status='open'",
            (f"赔付 {row['id']} 支付侧确认未付，需人工处理", f"issue-pending-payment-{row['id']}"),
        )
        if actor:
            audit(conn, actor, "payment.failed", "payment", row["id"], {"state": state})
        return {"payment": row["id"], "state": "failed", "note": "支付侧确认未付，未结责任仍需人工处理"}
    return None


def list_payments(store: Store, query: dict[str, Any]) -> dict[str, Any]:
    with store.read() as conn:
        sql = "SELECT * FROM payments WHERE 1=1"
        params: list[Any] = []
        for field in ("order_id", "item_id", "complaint_id", "state", "kind"):
            if query.get(field):
                sql += f" AND {field}=?"
                params.append(query[field])
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY created_at,id", tuple(params)).fetchall()]
        return {"payments": [_payment_view(conn, r) for r in rows]}
