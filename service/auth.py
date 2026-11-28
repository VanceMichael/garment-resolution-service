"""权限、审计与幂等。

身份来自每次请求的 X-Actor 头，仅用于演练归属，不是认证凭据。
服务只接受 catalog.json 中的演练身份和明确登记的角色。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .domain import DomainError, fingerprint, utcnow
from .store import Store

ROLES = {"consumer", "merchant", "warehouse", "quality", "independent-aftercare", "system"}


class Actor:
    def __init__(self, actor_id: str, role: str, affiliation: str | None = None):
        self.id = actor_id
        self.role = role
        self.affiliation = affiliation

    def as_dict(self) -> dict[str, str]:
        return {"actor": self.id, "role": self.role}


def resolve_actor(conn: sqlite3.Connection, store: Store, actor_id: str | None, claimed_role: str | None = None) -> Actor:
    if not actor_id:
        raise DomainError("missing_actor", "请求需要 X-Actor 头指明办理人")
    row = conn.execute("SELECT id, role FROM principals WHERE id=?", (actor_id,)).fetchone()
    if row:
        registered = row["role"]
        # principals 的值可以是角色，也可以是该身份所属商家/地点（例如 merchant-A -> shop-A）。
        role = registered if registered in ROLES else _role_from_identity(row["id"])
        if claimed_role and claimed_role != role:
            raise DomainError("role_mismatch", "X-Role 与登记角色不一致", {"registered": role, "claimed": claimed_role})
        actor = Actor(row["id"], role)
        actor.affiliation = registered if registered not in ROLES else None
        return actor
    if actor_id.startswith("system:") and (claimed_role or "system") == "system":
        return Actor(actor_id, "system")
    raise DomainError("unknown_actor", "未登记的演练身份", {"actor": actor_id})


_IDENTITY_ROLES = (
    ("customer", "consumer"),
    ("merchant", "merchant"),
    ("shop", "merchant"),
    ("warehouse", "warehouse"),
    ("quality", "quality"),
    ("reviewer", "independent-aftercare"),
    ("aftercare", "independent-aftercare"),
)


def _role_from_identity(actor_id: str) -> str:
    lowered = actor_id.lower()
    for prefix, role in _IDENTITY_ROLES:
        if lowered.startswith(prefix):
            return role
    return "unknown"


def require(actor: Actor, *roles: str) -> None:
    if actor.role not in roles:
        raise DomainError("forbidden", "该角色不能办理此事项", {"role": actor.role, "required": list(roles)})


def audit(conn: sqlite3.Connection, actor: Actor, action: str, subject_type: str, subject_id: str | None, detail: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO audit(at,actor,role,action,subject_type,subject_id,detail_json) VALUES(?,?,?,?,?,?,?)",
        (utcnow(), actor.id, actor.role, action, subject_type, subject_id, json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)),
    )


def case_action(conn: sqlite3.Connection, actor: Actor, action: str, complaint_id: str | None, detail: dict[str, Any] | None = None,
                item_id: str | None = None, order_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO case_actions(complaint_id,item_id,order_id,action,detail_json,actor,role,at) VALUES(?,?,?,?,?,?,?,?)",
        (complaint_id, item_id, order_id, action, json.dumps(detail or {}, ensure_ascii=False, sort_keys=True), actor.id, actor.role, utcnow()),
    )


class Idempotency:
    """同一 Idempotency-Key + 同一请求体只执行一次。

    键冲突但请求体不同会被拒绝，避免把不同业务动作悄悄合并。
    """

    def __init__(self, store: Store):
        self.store = store

    def lookup(self, key: str | None, request_body: Any) -> tuple[int, dict[str, Any]] | None:
        if not key:
            return None
        with self.store.read() as conn:
            row = conn.execute("SELECT fingerprint,status,body_json FROM idempotency WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        if row["fingerprint"] != fingerprint(request_body):
            raise DomainError("idempotency_conflict", "相同幂等键提交了不同内容", {"key": key})
        return row["status"], json.loads(row["body_json"])

    def record(self, conn: sqlite3.Connection, key: str | None, request_body: Any, status: int, body: dict[str, Any]) -> None:
        if not key:
            return
        conn.execute(
            "INSERT OR REPLACE INTO idempotency(key,fingerprint,status,body_json,created_at) VALUES(?,?,?,?,?)",
            (key, fingerprint(request_body), status, json.dumps(body, ensure_ascii=False), utcnow()),
        )
