"""投诉受理、补证、目标分项与一件商品的进度解释。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import scope as scope_mod
from .auth import Actor, audit, case_action, require
from .domain import (
    DomainError,
    PENDING_LABELS,
    fingerprint,
    normalize_complaint_clue,
    require_text,
    utcnow,
)
from .store import Store


def _new_id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{fingerprint(list(parts))[:10]}"


def _garment_batch(conn: sqlite3.Connection, item_id: str) -> str | None:
    row = conn.execute("SELECT batch_id FROM garments WHERE id=?", (item_id,)).fetchone()
    return row["batch_id"] if row else None


def _record_input(conn: sqlite3.Connection, complaint_id: str, payload: dict[str, Any], actor: Actor, now: str) -> None:
    conn.execute(
        "INSERT INTO complaint_inputs(id,complaint_id,payload_json,fingerprint,submitted_by,submitted_at) VALUES(?,?,?,?,?,?)",
        (f"input-{fingerprint([complaint_id, payload])[:12]}", complaint_id, json.dumps(payload, ensure_ascii=False, sort_keys=True),
         fingerprint(payload), actor.id, now),
    )


def _target_detail(conn: sqlite3.Connection, complaint_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT t.item_id, t.batch_id, t.basis, t.status, t.resolution, g.serial, g.state,
                  (SELECT l.order_id FROM order_lines l JOIN orders o ON o.id=l.order_id
                   WHERE l.item_id=t.item_id ORDER BY o.id LIMIT 1) AS order_id
           FROM complaint_targets t
           JOIN garments g ON g.id=t.item_id
           WHERE t.complaint_id=? ORDER BY t.item_id""",
        (complaint_id,),
    ).fetchall()
    value = []
    for row in rows:
        item = dict(row)
        item["order_id"] = row["order_id"]
        item["batches"] = _item_batch_basis(conn, complaint_id, row["item_id"])
        value.append(item)
    return value


def _item_batch_basis(conn: sqlite3.Connection, complaint_id: str, item_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT t.batch_id, t.basis, t.status AS target_status, b.style, b.color, b.supplier
           FROM complaint_targets t LEFT JOIN batches b ON b.id=t.batch_id
           WHERE t.complaint_id=? AND t.item_id=?""",
        (complaint_id, item_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _clue_status(clues: dict[str, Any]) -> str:
    if clues.get("order") or clues.get("items") or clues.get("serials") or clues.get("tags") or clues.get("batch"):
        return "investigating"
    return "open"


def _matches_for_clues(conn: sqlite3.Connection, clues: dict[str, Any]) -> list[dict[str, Any]]:
    """把吊牌/商家/购买时间线索与进货出库记录对照，只给出候选，不直接认定批次。"""
    matches: list[dict[str, Any]] = []
    serials = list(clues.get("serials") or [])
    tags = list(clues.get("tags") or [])
    for serial in serials:
        row = conn.execute("SELECT id, batch_id, serial FROM garments WHERE serial=? OR id=?", (serial, serial)).fetchone()
        if row:
            matches.append({"hint": serial, "item": row["id"], "batch": row["batch_id"], "basis": "吊牌/序列号与实物编号一致"})
    for tag in tags:
        row = conn.execute("SELECT id, batch_id, serial FROM garments WHERE serial=? OR id=?", (tag, tag)).fetchone()
        if row:
            matches.append({"hint": tag, "item": row["id"], "batch": row["batch_id"], "basis": "吊牌与外箱实物编号一致"})
    for item in clues.get("items") or []:
        row = conn.execute("SELECT id, batch_id, serial FROM garments WHERE id=? OR serial=?", (item, item)).fetchone()
        if row:
            matches.append({"hint": item, "item": row["id"], "batch": row["batch_id"], "basis": "投诉列出了具体商品"})
    if not matches and (clues.get("style") or clues.get("shop") or clues.get("purchase_date")):
        # 只有款号/商家/购买时间，可能对应多个批次；列为待查候选，不视为已确认。
        if clues.get("style"):
            rows = conn.execute("SELECT id FROM batches WHERE style=? ORDER BY id", (clues["style"],)).fetchall()
            for row in rows:
                matches.append({"hint": clues.get("style"), "batch": row["id"], "basis": "款号相同，批次未确认"})
            if clues.get("shop"):
                # 商家资料只能作为补充线索，不能在没有进货记录时认定批次。
                pass
    unique = []
    seen = set()
    for row in matches:
        key = (row.get("item"), row.get("batch"))
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def accept_complaint(store: Store, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "consumer", "merchant", "quality", "warehouse", "independent-aftercare")
    complaint_id = require_text(payload.get("id") or payload.get("complaint_id"), "id")
    clues = normalize_complaint_clue(payload)
    reporter = payload.get("reporter") or actor.id
    description = payload.get("description")
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM complaints WHERE id=?", (complaint_id,)).fetchone():
            raise DomainError("conflict", "投诉编号已存在，原始线索不可覆盖", {"complaint": complaint_id})
        if clues.get("order"):
            order_row = conn.execute("SELECT * FROM orders WHERE id=?", (clues["order"],)).fetchone()
            if not order_row:
                raise DomainError("not_found", "订单不存在", {"order": clues["order"]})
            if actor.role == "consumer" and order_row["customer"] and order_row["customer"] != actor.id:
                raise DomainError("forbidden", "消费者只能为自己的订单发起投诉", {"order": clues["order"]})
        for item in clues["items"]:
            if not conn.execute("SELECT 1 FROM garments WHERE id=? OR serial=?", (item, item)).fetchone():
                raise DomainError("not_found", "商品不存在", {"item": item})
        now = utcnow()
        status = _clue_status(clues)
        conn.execute(
            "INSERT INTO complaints(id,order_id,item_id,clues_json,status,stage,description,reporter,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (complaint_id, clues.get("order"), (clues["items"] or [None])[0], json.dumps(clues, ensure_ascii=False, sort_keys=True),
             status, "intake", description, reporter, actor.id, now, now),
        )
        _record_input(conn, complaint_id, payload, actor, now)
        for item in clues["items"]:
            _attach_item(conn, complaint_id, item, actor, basis="投诉列明商品", status="open")
        if clues.get("order") and not clues["items"]:
            # 只给订单时，把订单分项作为待顾客指认的分项；每件独立推进。
            lines = conn.execute("SELECT item_id FROM order_lines WHERE order_id=? ORDER BY item_id", (clues["order"],)).fetchall()
            for line in lines:
                _attach_item(conn, complaint_id, line["item_id"], actor,
                             basis="订单分项（具体商品待顾客指认）", status="open")
        matches = _matches_for_clues(conn, clues)
        for match in matches:
            if match.get("item"):
                _attach_item(conn, complaint_id, match["item"], actor, basis=match["basis"], status="open")
        if clues.get("batch"):
            batch = conn.execute("SELECT id FROM batches WHERE id=?", (clues["batch"],)).fetchone()
            if not batch:
                raise DomainError("not_found", "批次不存在", {"batch": clues["batch"]})
            # 点名批次只是线索，仍须质量负责人确认范围后才能据此处置或通知。
        audit(conn, actor, "complaint.accept", "complaint", complaint_id, {"clues": clues, "matches": matches})
        case_action(conn, actor, "complaint.accepted", complaint_id, {"status": status, "candidate_batches": [m.get("batch") for m in matches]}, order_id=clues.get("order"))
        return complaint_view(store, complaint_id)


def _attach_item(conn: sqlite3.Connection, complaint_id: str, item_id: str, actor: Actor, basis: str, status: str = "open") -> None:
    batch = _garment_batch(conn, item_id)
    conn.execute(
        """INSERT INTO complaint_targets(complaint_id,item_id,batch_id,basis,status,added_by,added_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(complaint_id,item_id) DO UPDATE SET
             batch_id=COALESCE(excluded.batch_id, complaint_targets.batch_id),
             basis=complaint_targets.basis || '；' || excluded.basis""",
        (complaint_id, item_id, batch, basis, status, actor.id, utcnow()),
    )


def add_evidence(store: Store, actor: Actor, complaint_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "consumer", "merchant", "warehouse", "quality", "independent-aftercare")
    evidence_id = require_text(payload.get("id") or payload.get("evidence_id") or f"ev-{fingerprint([complaint_id, payload])[:10]}", "id")
    kind = require_text(payload.get("kind"), "kind")
    value = payload.get("value")
    if value is None:
        raise DomainError("invalid_argument", "补证需要 value")
    item = payload.get("item")
    with store.transaction() as conn:
        complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if not complaint:
            raise DomainError("not_found", "投诉不存在", {"complaint": complaint_id})
        if conn.execute("SELECT 1 FROM evidence WHERE id=?", (evidence_id,)).fetchone():
            raise DomainError("conflict", "补证编号已存在", {"evidence": evidence_id})
        batch = payload.get("batch")
        if item:
            garment = conn.execute("SELECT id, batch_id FROM garments WHERE id=? OR serial=?", (item, item)).fetchone()
            if not garment:
                raise DomainError("not_found", "补证关联的商品不存在", {"item": item})
            item = garment["id"]
            batch = garment["batch_id"]
            _attach_item(conn, complaint_id, item, actor, basis="顾客补证关联实物", status="open")
        if batch and not conn.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
            raise DomainError("not_found", "补证关联的批次不存在", {"batch": batch})
        now = utcnow()
        conn.execute(
            "INSERT INTO evidence(id,complaint_id,kind,value,batch_id,submitted_by,submitted_at,note) VALUES(?,?,?,?,?,?,?,?)",
            (evidence_id, complaint_id, kind, str(value), batch, actor.id, now, payload.get("note")),
        )
        _record_input(conn, complaint_id, payload, actor, now)
        conn.execute(
            "UPDATE complaints SET status=CASE WHEN status='open' THEN 'investigating' ELSE status END, updated_at=? WHERE id=?",
            (now, complaint_id),
        )
        audit(conn, actor, "complaint.evidence.add", "complaint", complaint_id, {"evidence": evidence_id, "kind": kind, "batch": batch})
        case_action(conn, actor, "evidence.added", complaint_id, {"evidence": evidence_id, "kind": kind, "batch": batch}, item_id=item)
        return complaint_view(store, complaint_id)


def add_target(store: Store, actor: Actor, complaint_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "merchant", "warehouse", "quality", "independent-aftercare")
    item_id = require_text(payload.get("item"), "item")
    basis = require_text(payload.get("basis") or "人工补充关联", "basis")
    with store.transaction() as conn:
        if not conn.execute("SELECT 1 FROM complaints WHERE id=?", (complaint_id,)).fetchone():
            raise DomainError("not_found", "投诉不存在", {"complaint": complaint_id})
        garment = conn.execute("SELECT id FROM garments WHERE id=? OR serial=?", (item_id, item_id)).fetchone()
        if not garment:
            raise DomainError("not_found", "商品不存在", {"item": item_id})
        _attach_item(conn, complaint_id, garment["id"], actor, basis=basis, status="open")
        conn.execute("UPDATE complaints SET status=CASE WHEN status='open' THEN 'investigating' ELSE status END, updated_at=? WHERE id=?", (utcnow(), complaint_id))
        audit(conn, actor, "complaint.target.add", "complaint", complaint_id, {"item": item_id, "basis": basis})
        case_action(conn, actor, "target.added", complaint_id, {"item": garment["id"], "basis": basis}, item_id=garment["id"])
        return complaint_view(store, complaint_id)


def search_candidates(store: Store, clues: dict[str, Any]) -> dict[str, Any]:
    with store.read() as conn:
        matches = _matches_for_clues(conn, clues)
        batches = []
        for match in matches:
            batch = match.get("batch")
            if batch and batch not in [b["id"] for b in batches]:
                row = conn.execute("SELECT * FROM batches WHERE id=?", (batch,)).fetchone()
                if row:
                    batches.append({
                        "id": row["id"], "batch": row["id"], "style": row["style"], "color": row["color"], "supplier": row["supplier"],
                        "confirmed": False,
                        "restrictions": [d["id"] for d in scope_mod.active_restrictions_for_batch(conn, batch)],
                    })
        return {"matches": matches, "batches": batches, "confirmed": False,
                "note": "候选批次仅用于继续查证；未取得进货或出库对应关系前不认定为受影响批次。"}


def confirm_scope(store: Store, actor: Actor, complaint_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """质量负责人确认投诉覆盖的批次范围；确认后投诉才可通知顾客。"""
    require(actor, "quality")
    with store.transaction() as conn:
        complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if not complaint:
            raise DomainError("not_found", "投诉不存在", {"complaint": complaint_id})
        batches, basis = scope_mod.resolve_scope_batches(conn, payload, actor.id)
        now = utcnow()
        for row in basis:
            conn.execute(
                "INSERT OR IGNORE INTO complaint_scopes(id,complaint_id,batch_id,basis,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?)",
                (f"scope-{fingerprint([complaint_id, row['batch']])[:12]}", complaint_id, row["batch"], row["basis"], actor.id, now),
            )
        targets = conn.execute("SELECT item_id FROM complaint_targets WHERE complaint_id=?", (complaint_id,)).fetchall()
        for target in targets:
            batch = _garment_batch(conn, target["item_id"])
            conn.execute("UPDATE complaint_targets SET batch_id=?, basis=basis || '；质量确认范围' WHERE complaint_id=? AND item_id=?", (batch, complaint_id, target["item_id"]))
        conn.execute("UPDATE complaints SET status='investigating', updated_at=? WHERE id=?", (now, complaint_id))
        audit(conn, actor, "complaint.scope.confirm", "complaint", complaint_id, {"batches": batches, "basis": basis})
        case_action(conn, actor, "scope.confirmed", complaint_id, {"batches": batches}, order_id=complaint["order_id"])
        return complaint_view(store, complaint_id)


def complaint_view(store: Store, complaint_id: str) -> dict[str, Any]:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "投诉不存在", {"complaint": complaint_id})
        complaint = dict(row)
        scopes = [
            dict(r) for r in conn.execute(
                "SELECT s.batch_id, s.basis, s.confirmed_by, s.confirmed_at, b.style, b.color, b.supplier "
                "FROM complaint_scopes s JOIN batches b ON b.id=s.batch_id WHERE s.complaint_id=? ORDER BY s.batch_id",
                (complaint_id,),
            ).fetchall()
        ]
        evidence = [dict(r) for r in conn.execute("SELECT * FROM evidence WHERE complaint_id=? ORDER BY submitted_at, id", (complaint_id,)).fetchall()]
        inputs = [dict(r) for r in conn.execute("SELECT id,submitted_by,submitted_at,fingerprint FROM complaint_inputs WHERE complaint_id=? ORDER BY submitted_at,id", (complaint_id,)).fetchall()]
        notifications = [dict(r) for r in conn.execute("SELECT * FROM notifications WHERE complaint_id=? ORDER BY created_at,id", (complaint_id,)).fetchall()]
        choices = [dict(r) for r in conn.execute("SELECT * FROM choices WHERE complaint_id=? ORDER BY id", (complaint_id,)).fetchall()]
        targets = _target_detail(conn, complaint_id)
        fulfillments = [dict(r) for r in conn.execute("SELECT * FROM fulfillments WHERE complaint_id=? ORDER BY id", (complaint_id,)).fetchall()]
        payments = [dict(r) for r in conn.execute("SELECT * FROM payments WHERE complaint_id=? ORDER BY id", (complaint_id,)).fetchall()]
        issues = [dict(r) for r in conn.execute("SELECT * FROM issues WHERE complaint_id=? ORDER BY id", (complaint_id,)).fetchall()]
        confirmations = [dict(r) for r in conn.execute(
            "SELECT event_id,at,actor,action,detail_json FROM audit WHERE subject_type='complaint' AND subject_id=? AND action='complaint.scope.confirm' ORDER BY event_id",
            (complaint_id,),
        ).fetchall()]
        value = {
            "id": complaint["id"],
            "order_id": complaint["order_id"],
            "status": complaint["status"],
            "status_label": PENDING_LABELS.get(complaint["status"], complaint["status"]),
            "stage": complaint["stage"],
            "description": complaint["description"],
            "reporter": complaint["reporter"],
            "created_at": complaint["created_at"],
            "updated_at": complaint["updated_at"],
            "closed_at": complaint["closed_at"],
            "close_reason": complaint["close_reason"],
            "clues": json.loads(complaint["clues_json"]),
            "raw_inputs": inputs,
            "confirmed_scope": {"batches": scopes, "has_unconfirmed_batch": not scopes},
            "evidence": evidence,
            "targets": targets,
            "notifications": notifications,
            "choices": choices,
            "fulfillments": fulfillments,
            "payments": payments,
            "issues": issues,
            "scope_confirmations": confirmations,
        }
        value["history"] = [dict(r) for r in conn.execute(
            "SELECT action,item_id,order_id,detail_json,actor,role,at FROM case_actions WHERE complaint_id=? ORDER BY id",
            (complaint_id,),
        ).fetchall()]
        value["item_explanations"] = [item_explanation(conn, complaint_id, t["item_id"]) for t in targets]
        return value


def item_explanation(conn: sqlite3.Connection, complaint_id: str, item_id: str) -> dict[str, Any]:
    """按一件商品列出已完成与待办，供售后人员解释。"""
    garment = conn.execute("SELECT g.*, b.style, b.color, b.supplier FROM garments g JOIN batches b ON b.id=g.batch_id WHERE g.id=?", (item_id,)).fetchone()
    if not garment:
        raise DomainError("not_found", "商品不存在", {"item": item_id})
    target = conn.execute("SELECT * FROM complaint_targets WHERE complaint_id=? AND item_id=?", (complaint_id, item_id)).fetchone()
    done: list[str] = []
    pending: list[str] = []
    if target:
        done.append("投诉已把该商品列为待查分项")
    restrictions = scope_mod.active_restrictions_for_batch(conn, garment["batch_id"])
    if restrictions:
        done.append("该商品所在批次受有效质量处置限制：" + "、".join(d["id"] for d in restrictions))
    returns = conn.execute("SELECT * FROM returns WHERE item_id=? AND complaint_id=?", (item_id, complaint_id)).fetchall()
    if returns:
        done.append("已登记退货实物接收：" + "、".join(r["id"] for r in returns))
    payments = conn.execute("SELECT * FROM payments WHERE complaint_id=? AND item_id=?", (complaint_id, item_id)).fetchall()
    if payments:
        states = {p["state"] for p in payments}
        if "paid" in states:
            done.append("赔付已由协作方确认支付")
        if "pending_payment" in states:
            pending.append("赔付结果待协作方确认，不能重复支付")
    choices = conn.execute("SELECT * FROM choices WHERE complaint_id=? AND item_id=? ORDER BY id", (complaint_id, item_id)).fetchall()
    confirmed_choices = [c for c in choices if c["confirmed"]]
    unconfirmed_choices = [c for c in choices if not c["confirmed"]]
    for choice in confirmed_choices:
        done.append(f"顾客本人确认选择：{choice['choice']}（{choice['id']}）")
    for choice in unconfirmed_choices:
        pending.append(f"选择 {choice['choice']}（{choice['id']}）尚未经顾客确认，不能据此消耗替换库存")
    fulfillments = conn.execute("SELECT * FROM fulfillments WHERE complaint_id=? AND item_id=?", (complaint_id, item_id)).fetchall()
    for f in fulfillments:
        if f["status"] in {"reserved", "dispatching", "blocked", "needs_reprocessing"}:
            pending.append(f"换货履行待完成：{f['id']}（{f['status']}）")
        else:
            done.append(f"换货履行已完成：{f['id']}（{f['status']}）")
    issues = conn.execute("SELECT * FROM issues WHERE complaint_id=? AND item_id=? AND status='open'", (complaint_id, item_id)).fetchall()
    for issue in issues:
        pending.append(f"人工待办：{issue['detail']}（{issue['id']}）")
    shipments = conn.execute("SELECT * FROM shipments WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
    for shipment in shipments:
        phase = shipment["phase"]
        if phase in {"dispatch_pending", "carrier_pending"}:
            state = "待交运"
            if shipment["blocked_reason"]:
                pending.append(f"出库单 {shipment['id']} 因质量处置受阻，需停止交运或重新安排")
            else:
                pending.append(f"出库单 {shipment['id']} 尚未交运（{state}）")
        elif phase == "with_carrier":
            done.append(f"出库单 {shipment['id']} 已交承运人")
        elif phase == "intercepting":
            pending.append(f"出库单 {shipment['id']} 拦截待承运人确认，接单不等于拦截成功")
        elif phase == "intercepted":
            done.append(f"出库单 {shipment['id']} 承运人已确认拦截")
        elif phase == "lost":
            pending.append(f"出库单 {shipment['id']} 承运人报告丢失，需按运单查询跟进")
        elif phase == "delivered":
            done.append(f"出库单 {shipment['id']} 承运人报告已妥投")
    notifications = conn.execute("SELECT * FROM notifications WHERE complaint_id=? AND (item_id=? OR item_id IS NULL)", (complaint_id, item_id)).fetchall()
    if notifications:
        done.append("已按确认范围通知顾客")
    elif not confirmed_choices:
        pending.append("等待按确认范围通知并记录顾客选择")
    if target and target["status"] == "resolved":
        done.append("该分项已结案")
    elif not pending:
        pending.append("等待进一步证据或质量确认")
    value = {
        "item": item_id,
        "order_id": (conn.execute("SELECT order_id FROM order_lines WHERE item_id=?", (item_id,)).fetchone() or {"order_id": None})["order_id"],
        "batch": garment["batch_id"],
        "style": garment["style"],
        "color": garment["color"],
        "supplier": garment["supplier"],
        "physical_state": garment["state"],
        "outcome": garment["current_outcome"],
        "target_status": target["status"] if target else None,
        "done": done,
        "pending": pending,
        "restricted_by": [d["id"] for d in restrictions],
        "returned": bool(returns),
        "return_outcome": returns[0]["warehouse_outcome"] if returns else None,
        "refunded": any(p["kind"] in {"refund", "exchange_refund"} and p["state"] == "paid" for p in payments),
        "pending_payment": any(p["state"] == "pending_payment" for p in payments),
        "customer_confirmed_exchange": any(c["choice"] == "exchange" and c["confirmed"] for c in choices),
        "shipments": [{"id": s["id"], "phase": s["phase"], "intercept_confirmed": s["phase"] == "intercepted",
                       "blocked": bool(s["blocked_reason"])} for s in shipments],
        "refund_right_retained": any(c["choice"] == "refund" for c in choices) or not confirmed_choices,
    }
    return value


def list_complaints(store: Store, query: dict[str, Any]) -> dict[str, Any]:
    with store.read() as conn:
        sql = "SELECT * FROM complaints WHERE 1=1"
        params: list[Any] = []
        if query.get("status"):
            sql += " AND status=?"
            params.append(query["status"])
        if query.get("order"):
            sql += " AND order_id=?"
            params.append(query["order"])
        if query.get("batch"):
            sql += " AND id IN (SELECT complaint_id FROM complaint_targets WHERE batch_id=?)"
            params.append(query["batch"])
        sql += " ORDER BY created_at, id"
        rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
        return {"complaints": [{"id": r["id"], "status": r["status"], "order_id": r["order_id"], "created_at": r["created_at"],
                                 "item_count": conn.execute("SELECT COUNT(*) c FROM complaint_targets WHERE complaint_id=?", (r["id"],)).fetchone()["c"]}
                                for r in rows]}
