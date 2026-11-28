"""承运方协作：受理结果未知、观测推进、拦截与运单查询。

关键规则：
- 202 只表示协作方收到，不等于已交运；
- 504/未知时保持待查，不重复发起；
- 只有承运方明确报告拦截成功才从在途转拦截成功；
- 迟到的已揽收记录更新事实，但不清除质量处置。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .auth import Actor, audit, case_action, require
from .domain import DomainError, require_text, utcnow
from .store import Store

CARRIER_STATES = {
    "pending": "响应未知，待查",
    "accepted": "承运人已接单",
    "rejected": "承运人拒绝交运（尚未交运）",
    "in_transit": "运输中",
    "intercepted": "拦截成功",
    "not_intercepted": "拦截未成功",
    "delivered": "已妥投",
    "lost": "承运人报告丢失",
    "returned": "已退回",
}


def record_dispatch(store: Store, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "warehouse", "system")
    shipment_id = require_text(payload.get("id"), "id")
    item_id = require_text(payload.get("item"), "item")
    kind = payload.get("kind") or "outbound"
    with store.transaction() as conn:
        garment = conn.execute("SELECT * FROM garments WHERE id=?", (item_id,)).fetchone()
        if not garment:
            raise DomainError("not_found", "商品不存在", {"item": item_id})
        if conn.execute("SELECT 1 FROM shipments WHERE id=?", (shipment_id,)).fetchone():
            raise DomainError("conflict", "出库单号已存在", {"shipment": shipment_id})
        from . import scope as scope_mod
        restriction = scope_mod.active_restrictions_for_batch(conn, garment["batch_id"])
        if restriction and payload.get("hand_over"):
            raise DomainError("blocked", "批次受质量处置限制，不得交运", {"item": item_id,
                                "dispositions": [d["id"] for d in restriction]})
        now = utcnow()
        phase = "with_carrier" if payload.get("hand_over") else ("dispatch_pending" if garment["state"] != "picked" else "carrier_pending")
        conn.execute(
            "INSERT INTO shipments(id,kind,order_id,item_id,batch_id,carrier,tracking,phase,command_id,created_by,created_at,updated_at,blocked_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (shipment_id, kind, payload.get("order"), item_id, garment["batch_id"], payload.get("carrier"), payload.get("tracking"),
             phase, payload.get("command_id"), actor.id, now, now,
             json.dumps([d["id"] for d in restriction], ensure_ascii=False) if restriction else None),
        )
        conn.execute("INSERT INTO shipment_events(shipment_id,state,source,detail_json,at) VALUES(?,?,?,?,?)",
                     (shipment_id, phase, "warehouse", json.dumps(payload, ensure_ascii=False), now))
        if phase in {"with_carrier", "intercepting"}:
            conn.execute("UPDATE garments SET state='shipped', outcome_reason=?, updated_at=? WHERE id=?", (f"出库单 {shipment_id}", now, item_id))
        elif phase == "dispatch_pending":
            # 已分配待出库的实物不再作为可分配库存。
            conn.execute("UPDATE garments SET state='picked', current_outcome=COALESCE(current_outcome,'awaiting_disposition'), "
                         "outcome_reason=?, updated_at=? WHERE id=?", (f"出库单 {shipment_id} 待交运", now, item_id))
        audit(conn, actor, "shipment.record", "shipment", shipment_id, {"item": item_id, "phase": phase, "restriction": [d["id"] for d in restriction]})
        if payload.get("order"):
            case_action(conn, actor, "shipment.recorded", None, {"shipment": shipment_id, "phase": phase}, item_id=item_id, order_id=payload["order"])
        return shipment_view(conn, shipment_id)


def apply_observation(conn: sqlite3.Connection, shipment_id: str, observation: dict[str, Any], source: str, actor_id: str | None = None) -> dict[str, Any]:
    """把协作方观测写入发货单。仅推进事实，不自行作出质量决定。"""
    now = utcnow()
    state = observation.get("state")
    if state not in CARRIER_STATES:
        raise DomainError("invalid_argument", "未知承运状态", {"state": state})
    shipment = conn.execute("SELECT * FROM shipments WHERE id=?", (shipment_id,)).fetchone()
    if not shipment:
        raise DomainError("not_found", "出库单不存在", {"shipment": shipment_id})
    carrier_revision = observation.get("carrier_revision")
    if carrier_revision is not None:
        previous = conn.execute(
            "SELECT MAX(carrier_revision) m FROM shipment_events WHERE shipment_id=? AND carrier_revision IS NOT NULL", (shipment_id,)
        ).fetchone()["m"]
        if previous is not None and carrier_revision < previous:
            # 迟到的旧修订仍需记录事实，但不能覆盖较新状态。
            conn.execute(
                "INSERT INTO shipment_events(shipment_id,state,carrier_revision,picked_up_at,source,detail_json,at) VALUES(?,?,?,?,?,?,?)",
                (shipment_id, state, carrier_revision, observation.get("picked_up_at"), source, json.dumps({"stale": True, **observation}, ensure_ascii=False), now),
            )
            if actor_id:
                audit(conn, _actor_stub(actor_id), "shipment.stale_observation", "shipment", shipment_id,
                      {"state": state, "carrier_revision": carrier_revision})
            if observation.get("picked_up_at"):
                conn.execute("UPDATE shipments SET picked_up_at=? WHERE id=?", (observation["picked_up_at"], shipment_id))
            return {"shipment": _shipment(conn, shipment_id), "stale": True,
                    "late_pickup": observation.get("picked_up_at"),
                    "note": "该承运事实修订早于已记录修订，仅留存，不改变当前阶段；迟到的揽收事实已记录，质量处置不受影响"}
    conn.execute(
        "INSERT INTO shipment_events(shipment_id,state,carrier_revision,picked_up_at,source,detail_json,at) VALUES(?,?,?,?,?,?,?)",
        (shipment_id, state, carrier_revision, observation.get("picked_up_at"), source, json.dumps(observation, ensure_ascii=False), now),
    )
    phase = shipment["phase"]
    if state == "pending":
        phase = "intercepting" if shipment["phase"] in {"with_carrier", "intercepting"} else shipment["phase"]
    elif state == "accepted":
        # 承运人接单不是拦截结果；原本在拦截跟进的保持待查。
        phase = "intercepting" if shipment["phase"] == "intercepting" else "with_carrier"
    elif state == "rejected":
        phase = "carrier_pending"
    elif state == "in_transit":
        phase = "intercepting" if shipment["phase"] == "intercepting" else "with_carrier"
    elif state == "intercepted":
        phase = "intercepted"
    elif state == "not_intercepted":
        phase = "with_carrier"
    elif state == "delivered":
        phase = "delivered"
    elif state == "returned":
        phase = "returned"
    elif state == "lost":
        phase = "lost"
    # 已揽收是承运方事实：即使修订迟到也记录，供下一步判断；但它不覆盖阶段，也不清除质量处置。
    picked_up = observation.get("picked_up_at")
    conn.execute("UPDATE shipments SET phase=?, picked_up_at=COALESCE(?, picked_up_at), updated_at=? WHERE id=?",
                 (phase, picked_up, now, shipment_id))
    garment = conn.execute("SELECT * FROM garments WHERE id=?", (shipment["item_id"],)).fetchone()
    from . import scope as scope_mod
    restriction = scope_mod.active_restrictions_for_batch(conn, shipment["batch_id"])
    if state == "intercepted":
        # 拦截成功也不回可售库存：仍需按该件批次与当前处置判断去向。
        outcome = "quarantine" if restriction else "intercept_succeeded"
        conn.execute("UPDATE garments SET state='intercepted', current_outcome=?, outcome_reason=?, updated_at=? WHERE id=?",
                     (outcome, f"承运人拦截确认（{shipment_id}）" + ("；仍受质量处置限制" if restriction else ""), now, shipment["item_id"]))
    elif state == "not_intercepted":
        conn.execute("UPDATE garments SET current_outcome='intercept_failed', outcome_reason=?, updated_at=? WHERE id=?",
                     (f"承运人报告拦截未成功（{shipment_id}）", now, shipment["item_id"]))
    elif state == "rejected":
        if restriction:
            conn.execute("UPDATE garments SET state='picked', current_outcome='quarantine', outcome_reason=?, updated_at=? WHERE id=?",
                         (f"承运人未接受交运（{shipment_id}），批次仍受质量处置限制", now, shipment["item_id"]))
        else:
            conn.execute("UPDATE garments SET state='picked', outcome_reason=?, updated_at=? WHERE id=?",
                         (f"承运人未接受交运（{shipment_id}），尚未实际发出", now, shipment["item_id"]))
    elif state == "lost":
        conn.execute("UPDATE garments SET state='lost', current_outcome='awaiting_disposition', outcome_reason=?, updated_at=? WHERE id=?",
                     (f"承运人报告丢失，按运单查询跟进（{shipment_id}）", now, shipment["item_id"]))
    elif state == "delivered" and not restriction:
        conn.execute("UPDATE garments SET outcome_reason=?, updated_at=? WHERE id=?", (f"承运人报告妥投（{shipment_id}）", now, shipment["item_id"]))
    if actor_id:
        audit(conn, _actor_stub(actor_id), "shipment.observation", "shipment", shipment_id, {"state": state, "phase": phase, "carrier_revision": carrier_revision})
    return {"shipment": _shipment(conn, shipment_id), "stale": False, "quality_restriction_preserved": [d["id"] for d in restriction]}


class _ActorStub:
    def __init__(self, actor_id: str):
        self.id = actor_id
        self.role = "system"


def _actor_stub(actor_id: str) -> _ActorStub:
    return _ActorStub(actor_id)


def _shipment(conn: sqlite3.Connection, shipment_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM shipments WHERE id=?", (shipment_id,)).fetchone()
    if not row:
        raise DomainError("not_found", "出库单不存在", {"shipment": shipment_id})
    value = dict(row)
    value["events"] = [dict(r) for r in conn.execute(
        "SELECT state,carrier_revision,picked_up_at,source,detail_json,at FROM shipment_events WHERE shipment_id=? ORDER BY id", (shipment_id,)
    ).fetchall()]
    value["phase_label"] = CARRIER_STATES.get(value["phase"], value["phase"])
    value["intercept_confirmed"] = value["phase"] == "intercepted"
    value["picked_up_recorded"] = bool(value.get("picked_up_at"))
    value["next_actions"] = _next_actions(value)
    return value


def _next_actions(shipment: dict[str, Any]) -> list[str]:
    phase = shipment["phase"]
    actions: list[str] = []
    if shipment.get("blocked_reason") and phase in {"dispatch_pending", "carrier_pending"}:
        actions.append("质量处置阻止交运；未交运前不得交承运人")
    if phase in {"dispatch_pending", "carrier_pending"}:
        actions.append("等待交运；交运后才进入在途")
    if phase == "with_carrier":
        actions.append("如需拦截，发起拦截流水并按次查询；承运人接单不等于拦截成功")
    if phase == "intercepting":
        actions.append("拦截待查：继续按流水/运单查询，不能按已拦截处理")
    if phase == "intercepted":
        actions.append("拦截成功：按该件批次与当前处置决定隔离、报废或退货复核；不得直接回可售库存")
    if phase == "not_intercepted":
        actions.append("拦截未成功：按运单继续跟进，并保留质量处置")
    if phase == "lost":
        actions.append("承运人报告丢失：按运单查询进展，保留质量处置，按既定向顾客与赔付规则处理")
    if phase == "delivered":
        actions.append("已妥投：如顾客退回，入库时按批次与当前处置重新判断去向")
    return actions


def shipment_view(conn: sqlite3.Connection, shipment_id: str) -> dict[str, Any]:
    return _shipment(conn, shipment_id)


def payload_for(store: Store, shipment_id: str, payload: dict[str, Any], actor: Actor, kind: str = "with_carrier",
                command_id: str | None = None) -> dict[str, Any]:
    """记录交运事实（仓库动作），不把承运人受理当作拦截成功。"""
    require(actor, "warehouse")
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM shipments WHERE id=?", (shipment_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "出库单不存在", {"shipment": shipment_id})
        now = utcnow()
        from . import scope as scope_mod
        restriction = scope_mod.active_restrictions_for_batch(conn, row["batch_id"])
        if restriction and kind == "with_carrier":
            raise DomainError("blocked", "批次受质量处置限制，不得交运", {"shipment": shipment_id,
                                "dispositions": [d["id"] for d in restriction]})
        if kind == "with_carrier" and row["phase"] not in {"dispatch_pending", "carrier_pending"}:
            raise DomainError("conflict", "该出库单当前阶段不能再次交运", {"shipment": shipment_id, "phase": row["phase"]})
        conn.execute("UPDATE shipments SET phase=?, carrier=COALESCE(?,carrier), tracking=COALESCE(?,tracking), command_id=COALESCE(?,command_id), updated_at=? WHERE id=?",
                     (kind, payload.get("carrier"), payload.get("tracking"), command_id, now, shipment_id))
        conn.execute("UPDATE garments SET state='shipped', updated_at=? WHERE id=?", (now, row["item_id"]))
        conn.execute("INSERT INTO shipment_events(shipment_id,state,source,detail_json,at) VALUES(?,?,?,?,?)",
                     (shipment_id, kind, "warehouse", json.dumps(payload, ensure_ascii=False), now))
        audit(conn, actor, "shipment.handover", "shipment", shipment_id, {"phase": kind, "command_id": command_id})
        return _shipment(conn, shipment_id)


def set_phase(store: Store, actor: Actor, shipment_id: str, phase: str, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "warehouse", "quality", "system")
    if phase not in {"dispatch_pending", "carrier_pending", "with_carrier", "intercepting", "intercepted",
                     "delivered", "returned", "cancelled", "lost"}:
        raise DomainError("invalid_argument", "未知出库单阶段", {"phase": phase})
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM shipments WHERE id=?", (shipment_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "出库单不存在", {"shipment": shipment_id})
        now = utcnow()
        conn.execute("UPDATE shipments SET phase=?, updated_at=? WHERE id=?", (phase, now, shipment_id))
        conn.execute("INSERT INTO shipment_events(shipment_id,state,source,detail_json,at) VALUES(?,?,?,?,?)",
                     (shipment_id, phase, actor.role, json.dumps(payload, ensure_ascii=False), now))
        return _shipment(conn, shipment_id)


def list_shipments(store: Store, query: dict[str, Any]) -> dict[str, Any]:
    with store.read() as conn:
        sql = "SELECT * FROM shipments WHERE 1=1"
        params: list[Any] = []
        for field in ("phase", "item_id", "batch_id", "order_id"):
            if query.get(field):
                sql += f" AND {field}=?"
                params.append(query[field])
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY created_at,id", tuple(params)).fetchall()]
        return {"shipments": [_shipment(conn, r["id"]) for r in rows]}
