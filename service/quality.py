"""检测报告：追加保存、只由质量人员撤回/更正，原文可核验。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import scope as scope_mod
from .auth import Actor, audit, require
from .domain import DomainError, require_text, utcnow
from .store import Store


def _report_view(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    value = {
        "id": row["id"],
        "batch_ids": scope_mod.batch_ids_for_report(conn, row["id"]),
        "scope": json.loads(row["scope_json"] or "{}"),
        "conclusion": row["conclusion"],
        "digest": row["digest"],
        "signed_by": row["signed_by"],
        "issued_at": row["issued_at"],
        "status": row["status"],
        "supersedes": row["supersedes"],
        "corrects": row["corrects"],
        "correction_reason": row["correction_reason"],
        "withdrawn_by": row["withdrawn_by"],
        "withdrawn_at": row["withdrawn_at"],
        "withdraw_reason": row["withdraw_reason"],
        "created_at": row["created_at"],
    }
    value["effective"] = row["status"] == "active"
    value["affected_batches"] = (
        scope_mod.expand_with_mappings(conn, value["batch_ids"]) if value["effective"] else []
    )
    value["affects"] = scope_mod.resolve_scope_batches(conn, {"report": row["id"]})[1] if value["effective"] else []
    return value


def create_report(store: Store, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "quality")
    report_id = require_text(payload.get("id"), "id")
    conclusion = require_text(payload.get("conclusion"), "conclusion")
    digest = require_text(payload.get("digest"), "digest")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise DomainError("invalid_argument", "digest 必须是 64 位十六进制文档摘要", {"digest": digest})
    batches = payload.get("batches") or payload.get("batch_ids") or []
    if isinstance(batches, str):
        batches = [batches]
    if not batches and not payload.get("report_scope") and not payload.get("scope_batches"):
        raise DomainError("invalid_argument", "报告必须列明适用批次或范围")
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM reports WHERE id=?", (report_id,)).fetchone():
            raise DomainError("conflict", "检测报告编号已存在，原文不可覆盖", {"report": report_id})
        known = set(r["id"] for r in conn.execute("SELECT id FROM batches").fetchall())
        for batch in batches:
            if batch not in known:
                raise DomainError("not_found", "报告引用了不存在的批次", {"batch": batch})
        scope_batches = payload.get("report_scope") or payload.get("scope_batches") or []
        if isinstance(scope_batches, str):
            scope_batches = [scope_batches]
        for batch in scope_batches:
            if batch not in known:
                raise DomainError("not_found", "报告范围引用了不存在的批次", {"batch": batch})
        scope_json = {"batch_ids": list(scope_batches)}
        for key in ("include_mapped", "include_supplied", "all_batches"):
            if payload.get(key):
                scope_json[key] = True
        if payload.get("scope_note"):
            scope_json["note"] = payload["scope_note"]
        now = utcnow()
        issued_at = payload.get("issued_at") or now
        source = payload.get("source_document")
        if source is not None and not isinstance(source, str):
            raise DomainError("invalid_argument", "source_document 必须是文本")
        conn.execute(
            "INSERT INTO reports(id,batch_id,scope_json,conclusion,digest,signed_by,issued_at,status,supersedes,corrects,correction_reason,source_document,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (report_id, batches[0] if batches else None, json.dumps(scope_json, ensure_ascii=False), conclusion, digest.lower(),
             actor.id, issued_at, "active", payload.get("supersedes"), payload.get("corrects"), payload.get("correction_reason"),
             source, now),
        )
        for batch in batches:
            conn.execute("INSERT INTO report_batches(report_id,batch_id,basis) VALUES(?,?,?)", (report_id, batch, "report_batch"))
        audit(conn, actor, "report.create", "report", report_id, {"batches": batches, "digest": digest})
        row = dict(conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone())
        return _report_view(conn, row)


def withdraw_report(store: Store, actor: Actor, report_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    require(actor, "quality")
    reason = require_text(payload.get("reason"), "reason")
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "检测报告不存在", {"report": report_id})
        if row["status"] != "active":
            raise DomainError("conflict", "报告已不是有效状态", {"status": row["status"]})
        now = utcnow()
        conn.execute(
            "UPDATE reports SET status='withdrawn', withdrawn_by=?, withdrawn_at=?, withdraw_reason=? WHERE id=?",
            (actor.id, now, reason, report_id),
        )
        # 以该报告为依据的处置不自动失效：撤回一份依据不能清除其他仍有效的停供事实。
        # 这里标记"依据已撤回、待质量复核"，库存仍保持限制，直到质量人员明确解除。
        dependent = conn.execute(
            "SELECT * FROM dispositions WHERE basis_report=? AND status='active'", (report_id,)
        ).fetchall()
        for disposition in dependent:
            conn.execute(
                "UPDATE dispositions SET basis_retracted=1, basis_retracted_at=?, basis_retracted_note=? WHERE id=?",
                (now, reason, disposition["id"]),
            )
            audit(conn, actor, "disposition.basis_retracted", "disposition", disposition["id"],
                  {"report": report_id, "note": "报告撤回；处置仍有效，需质量人员复核后可单独解除"})
        report_batches = scope_mod.batch_ids_for_report(conn, report_id)
        still_restricted = {b: bool(scope_mod.active_restrictions_for_batch(conn, b)) for b in scope_mod.expand_with_mappings(conn, report_batches)}
        audit(conn, actor, "report.withdraw", "report", report_id, {"reason": reason, "remaining_restrictions": still_restricted})
        row = dict(conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone())
        view = _report_view(conn, row)
        view["subsequent_batch_status"] = [
            {"batch": batch, "still_restricted": restricted,
             "restrictions": [d["id"] for d in scope_mod.active_restrictions_for_batch(conn, batch)],
             "explanation": "其他有效处置仍覆盖该批次" if restricted else "本报告撤回后该批次不再受本报告限制"}
            for batch, restricted in sorted(still_restricted.items())
        ]
        return view


def correct_report(store: Store, actor: Actor, report_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """报告更正生成新的报告记录；旧报告保留可查，已结束案件不被改写。"""
    require(actor, "quality")
    new_id = require_text(payload.get("id"), "id")
    conclusion = require_text(payload.get("conclusion"), "conclusion")
    digest = require_text(payload.get("digest"), "digest")
    reason = require_text(payload.get("reason"), "reason")
    batches = payload.get("batches") or payload.get("batch_ids") or []
    if isinstance(batches, str):
        batches = [batches]
    with store.transaction() as conn:
        old = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not old:
            raise DomainError("not_found", "检测报告不存在", {"report": report_id})
        if conn.execute("SELECT 1 FROM reports WHERE id=?", (new_id,)).fetchone():
            raise DomainError("conflict", "更正后的报告编号已存在", {"report": new_id})
        if not batches:
            batches = [r["batch_id"] for r in conn.execute(
                "SELECT batch_id FROM report_batches WHERE report_id=? ORDER BY batch_id", (report_id,)).fetchall()]
        known = set(r["id"] for r in conn.execute("SELECT id FROM batches").fetchall())
        for batch in batches:
            if batch not in known:
                raise DomainError("not_found", "更正报告引用了不存在的批次", {"batch": batch})
        now = utcnow()
        conn.execute(
            "INSERT INTO reports(id,batch_id,scope_json,conclusion,digest,signed_by,issued_at,status,supersedes,corrects,correction_reason,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id, batches[0] if batches else None, old["scope_json"], conclusion, digest.lower(), actor.id,
             payload.get("issued_at") or now, "active", old["id"], old["id"], reason, now),
        )
        for batch in batches:
            conn.execute("INSERT INTO report_batches(report_id,batch_id,basis) VALUES(?,?,?)", (new_id, batch, "corrected_report"))
        conn.execute("UPDATE reports SET status='superseded' WHERE id=?", (report_id,))
        audit(conn, actor, "report.correct", "report", new_id, {"corrects": report_id, "reason": reason})
        new_row = dict(conn.execute("SELECT * FROM reports WHERE id=?", (new_id,)).fetchone())
        return {"report": _report_view(conn, new_row), "superseded": _report_view(conn, dict(conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()))}


def get_report(store: Store, report_id: str) -> dict[str, Any]:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "检测报告不存在", {"report": report_id})
        view = _report_view(conn, dict(row))
        view["corrections"] = [
            {"id": r["id"], "status": r["status"], "correction_reason": r["correction_reason"]}
            for r in conn.execute("SELECT id,status,correction_reason FROM reports WHERE corrects=? ORDER BY created_at", (report_id,)).fetchall()
        ]
        return view
