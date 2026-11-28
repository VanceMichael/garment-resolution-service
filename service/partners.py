"""协作方协议接入：发起流水、按次读取观测、重启后继续。

业务服务把协作方观测落到自己的记录里；504/202 都不是业务失败结论。
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from typing import Any, Callable

from .auth import Actor, audit, require
from .domain import DomainError, require_text, utcnow
from .store import Store


def _partner_row(conn: sqlite3.Connection, command_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM partners WHERE command_id=?", (command_id,)).fetchone()
    return dict(row) if row else None


def partner_view(conn: sqlite3.Connection, command_id: str) -> dict[str, Any]:
    row = _partner_row(conn, command_id)
    if not row:
        raise DomainError("not_found", "未登记该协作方流水", {"command_id": command_id})
    return {
        "command_id": row["command_id"],
        "operation": row["operation"],
        "request": json.loads(row["request_json"]),
        "linked_kind": row["linked_kind"],
        "linked_id": row["linked_id"],
        "first_status": row["first_status"],
        "state": row["state"],
        "last_observation": json.loads(row["last_observation_json"]) if row["last_observation_json"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "note": "首次响应未知(504)或已收到(202)都只表示协作方受理情况；最终事实以观测为准。",
    }


def _register(conn: sqlite3.Connection, command_id: str, operation: str, request: dict[str, Any], linked_kind: str, linked_id: str | None) -> dict[str, Any]:
    existing = _partner_row(conn, command_id)
    if existing:
        if existing["operation"] != operation or json.loads(existing["request_json"]) != request:
            raise DomainError("conflict", "同一流水ID不能更换操作或请求内容", {"command_id": command_id})
        return existing
    now = utcnow()
    conn.execute(
        "INSERT INTO partners(command_id,operation,request_json,linked_kind,linked_id,first_status,state,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (command_id, operation, json.dumps(request, ensure_ascii=False, sort_keys=True), linked_kind, linked_id, None, "registered", now, now),
    )
    return _partner_row(conn, command_id)  # type: ignore[return-value]


def dispatch(store: Store, actor: Actor, payload: dict[str, Any], partner_url: str | None, timeout: float = 3.0) -> dict[str, Any]:
    """登记并向协作方发起一次流水。网络异常保持未知状态，可稍后查询。"""
    require(actor, "warehouse", "independent-aftercare", "quality", "system")
    command_id = require_text(payload.get("command_id"), "command_id")
    operation = require_text(payload.get("operation"), "operation")
    request = payload.get("request")
    if not isinstance(request, dict):
        raise DomainError("invalid_argument", "request 必须是对象")
    linked_kind = require_text(payload.get("linked_kind") or operation, "linked_kind")
    linked_id = payload.get("linked_id")
    with store.transaction() as conn:
        row = _register(conn, command_id, operation, request, linked_kind, linked_id)
    if row["state"] not in {"registered", "unknown"}:
        return _with_conn(store, command_id)
    if not partner_url:
        if row["state"] == "registered":
            with store.transaction() as conn:
                now = utcnow()
                conn.execute("UPDATE partners SET state='unknown', updated_at=? WHERE command_id=?", (now, command_id))
                audit(conn, actor, "partner.dispatch", "partner_command", command_id,
                      {"operation": operation, "status": None, "state": "unknown", "note": "未配置协作方地址，待补查"})
        return _with_conn(store, command_id)
    status: int | None = None
    body: dict[str, Any] = {}
    if partner_url:
        body_bytes = json.dumps({"command_id": command_id, "operation": operation, "request": request}, ensure_ascii=False).encode()
        req = urllib.request.Request(partner_url.rstrip("/") + "/commands", data=body_bytes, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status = response.status
                body = json.load(response)
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                body = json.load(error)
            except Exception:
                body = {"error": "协作方返回非JSON"}
        except Exception as error:  # 网络问题：保持未知，可重试查询
            status = None
            body = {"error": f"协作方连接失败：{error}"}
    state = "accepted" if status in (200, 201, 202) else ("unknown" if status is None or status >= 500 else "rejected")
    with store.transaction() as conn:
        now = utcnow()
        conn.execute("UPDATE partners SET first_status=?, state=?, response_json=?, updated_at=? WHERE command_id=?",
                     (status, state, json.dumps(body, ensure_ascii=False), now, command_id))
        audit(conn, actor, "partner.dispatch", "partner_command", command_id, {"operation": operation, "status": status, "state": state})
        return partner_view(conn, command_id)


def _with_conn(store: Store, command_id: str) -> dict[str, Any]:
    with store.read() as conn:
        return partner_view(conn, command_id)


def fetch(store: Store, actor: Actor, command_id: str, partner_url: str | None, timeout: float = 3.0,
          handler: Callable[[sqlite3.Connection, str, dict[str, Any], Actor | None], Any] | None = None) -> dict[str, Any]:
    """读取下一条观测；每条观测只应用一次，最后一条保持稳定。"""
    require(actor, "warehouse", "independent-aftercare", "quality", "system")
    with store.transaction() as conn:
        row = _partner_row(conn, command_id)
        if not row:
            raise DomainError("not_found", "未登记该协作方流水", {"command_id": command_id})
    if partner_url and row["state"] == "registered":
        # 只登记未发出的流水在首次查询时补发；已发出的不会重复发起。
        with store.transaction() as conn:
            conn.execute("UPDATE partners SET state='unknown', updated_at=? WHERE command_id=?", (utcnow(), command_id))
        dispatch(store, Actor("system:partner", "system"),
                 {"command_id": command_id, "operation": row["operation"], "request": json.loads(row["request_json"]),
                  "linked_kind": row["linked_kind"], "linked_id": row["linked_id"]}, partner_url, timeout)
    observation: dict[str, Any] | None = None
    if partner_url:
        req = urllib.request.Request(partner_url.rstrip("/") + "/commands/" + command_id)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                observation = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                observation = None
            else:
                raise DomainError("partner_error", "读取协作方观测失败", {"status": error.code})
    elif row["last_observation_json"]:
        observation = json.loads(row["last_observation_json"])
    applied = None
    with store.transaction() as conn:
        row = _partner_row(conn, command_id)
        previous = json.loads(row["last_observation_json"]) if row["last_observation_json"] else None
        if observation is not None:
            now = utcnow()
            # 协作方只返回最新一条；相同内容不重复应用。
            if observation != previous:
                conn.execute("UPDATE partners SET last_observation_json=?, state='observed', updated_at=? WHERE command_id=?",
                             (json.dumps(observation, ensure_ascii=False, sort_keys=True), now, command_id))
                if handler:
                    applied = handler(conn, command_id, observation, actor)
                audit(conn, actor, "partner.observation", "partner_command", command_id, observation)
        return {"command": partner_view(conn, command_id), "observation": observation, "applied": applied,
                "repeated": observation is not None and observation == previous}
