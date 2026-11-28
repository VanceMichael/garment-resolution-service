"""按认定依据计算真实受影响的批次、库内实物与已有履行。

原则：
- 款号相同不合并批次；
- 批次只能通过进货/分装映射关系扩展；
- 处置状态实时派生，不依赖一次性的分析结果；
- 同一批次被多项有效依据覆盖时，解除其中一项不恢复库存。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .domain import DomainError, find_batch_cycle

SCOPE_BASES = {
    "report_batch": "检测报告直接列明批次",
    "report_scope": "检测报告声明的影响范围",
    "disposition_batch": "质量处置适用批次",
    "complaint_confirmed": "已确认投诉关联批次",
    "supplier": "同供应商进货批次",
    "batch_mapping": "分装/子批次关系",
    "manual": "人工列明",
}


def batch_ids_for_report(conn: sqlite3.Connection, report_id: str) -> list[str]:
    rows = conn.execute("SELECT batch_id FROM report_batches WHERE report_id=? ORDER BY batch_id", (report_id,)).fetchall()
    return [r["batch_id"] for r in rows]


def batch_ids_for_disposition(conn: sqlite3.Connection, disposition_id: str) -> list[str]:
    rows = conn.execute("SELECT batch_id FROM disposition_batches WHERE disposition_id=? ORDER BY batch_id", (disposition_id,)).fetchall()
    return [r["batch_id"] for r in rows]


def expand_with_mappings(conn: sqlite3.Connection, seeds: list[str]) -> list[str]:
    """沿分装/子批次关系双向扩展。款号相同不是依据。"""
    found = set(seeds)
    changed = True
    while changed:
        changed = False
        if not found:
            break
        placeholders = ",".join("?" for _ in found)
        rows = conn.execute(
            f"SELECT parent_batch, child_batch FROM batch_mappings WHERE parent_batch IN ({placeholders}) OR child_batch IN ({placeholders})",
            tuple(found) + tuple(found),
        ).fetchall()
        for row in rows:
            for batch in (row["parent_batch"], row["child_batch"]):
                if batch not in found:
                    found.add(batch)
                    changed = True
    return sorted(found)


def expand_with_supplier(conn: sqlite3.Connection, seeds: list[str]) -> list[str]:
    if not seeds:
        return []
    placeholders = ",".join("?" for _ in seeds)
    suppliers = [r["supplier"] for r in conn.execute(
        f"SELECT DISTINCT supplier FROM batches WHERE id IN ({placeholders}) AND supplier IS NOT NULL AND supplier<>''", tuple(seeds)
    ).fetchall()]
    if not suppliers:
        return sorted(seeds)
    sp = ",".join("?" for _ in suppliers)
    rows = conn.execute(f"SELECT id FROM batches WHERE supplier IN ({sp})", tuple(suppliers)).fetchall()
    return sorted(set(seeds) | {r["id"] for r in rows})


def all_batches_by_style(conn: sqlite3.Connection, style: str, color: str | None = None) -> list[str]:
    if color:
        rows = conn.execute("SELECT id FROM batches WHERE style=? AND color=? ORDER BY id", (style, color)).fetchall()
    else:
        rows = conn.execute("SELECT id FROM batches WHERE style=? ORDER BY id", (style,)).fetchall()
    return [r["id"] for r in rows]


def active_negative_dispositions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT d.* FROM dispositions d WHERE d.status='active' AND d.type IN ('stop_supply','investigate','not_conforming') ORDER BY d.created_at, d.id"
    ).fetchall()
    value = []
    for row in rows:
        item = dict(row)
        item["type_label"] = {"stop_supply": "停供", "investigate": "扩大调查", "not_conforming": "不合格"}[row["type"]]
        item["batch_ids"] = batch_ids_for_disposition(conn, row["id"])
        item["scope"] = json.loads(row["scope_json"])
        value.append(item)
    return value


def active_restrictions_for_batch(conn: sqlite3.Connection, batch_id: str) -> list[dict[str, Any]]:
    """返回覆盖该批次的有效负面处置依据。没有任何依据时返回空列表。"""
    result = []
    for disposition in active_negative_dispositions(conn):
        if batch_id in disposition["batch_ids"]:
            result.append(disposition)
            continue
        scope = disposition["scope"]
        if scope.get("all_batches"):
            result.append(disposition)
            continue
        if scope.get("include_supplied") and disposition["batch_ids"]:
            expanded = expand_with_supplier(conn, disposition["batch_ids"])
            if batch_id in expanded:
                result.append(disposition)
                continue
        if scope.get("include_mapped") and disposition["batch_ids"]:
            expanded = expand_with_mappings(conn, disposition["batch_ids"])
            if batch_id in expanded:
                result.append(disposition)
    return result


def batch_restricted(conn: sqlite3.Connection, batch_id: str) -> bool:
    return bool(active_restrictions_for_batch(conn, batch_id))


def resolve_scope_batches(conn: sqlite3.Connection, payload: dict[str, Any], actor_id: str | None = None) -> tuple[list[str], list[dict[str, str]]]:
    """把一份证据的适用范围解析为批次列表，并逐批次给出依据。"""
    basis: list[dict[str, str]] = []
    seeds: list[str] = []

    def add(batch_id: str, reason: str) -> None:
        if batch_id not in seeds:
            seeds.append(batch_id)
            basis.append({"batch": batch_id, "basis": reason})

    report_id = payload.get("report")
    if report_id:
        report = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not report:
            raise DomainError("not_found", "检测报告不存在", {"report": report_id})
        for batch in batch_ids_for_report(conn, report_id):
            add(batch, SCOPE_BASES["report_batch"])
        scope = json.loads(report["scope_json"] or "{}")
        for batch in scope.get("batch_ids", []):
            if conn.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
                add(batch, SCOPE_BASES["report_scope"])

    disposition_id = payload.get("disposition")
    if disposition_id:
        row = conn.execute("SELECT * FROM dispositions WHERE id=?", (disposition_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "质量处置不存在", {"disposition": disposition_id})
        for batch in batch_ids_for_disposition(conn, disposition_id):
            add(batch, SCOPE_BASES["disposition_batch"])

    complaint_id = payload.get("complaint")
    if complaint_id:
        rows = conn.execute(
            "SELECT batch_id FROM complaint_scopes WHERE complaint_id=? ORDER BY batch_id", (complaint_id,)
        ).fetchall()
        if not rows:
            raise DomainError("insufficient_scope", "投诉尚未由质量负责人确认批次，不能据此确定影响范围",
                              {"complaint": complaint_id, "next": "先补证并经质量人员确认范围"})
        for row in rows:
            add(row["batch_id"], SCOPE_BASES["complaint_confirmed"])

    explicit = payload.get("batches") or payload.get("batch_ids") or []
    if isinstance(explicit, str):
        explicit = [explicit]
    for batch in explicit:
        if not conn.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
            raise DomainError("not_found", "批次不存在", {"batch": batch})
        add(batch, SCOPE_BASES["manual"])

    if payload.get("style"):
        if not payload.get("all_colors") and not payload.get("color"):
            raise DomainError(
                "insufficient_scope",
                "仅款式不能确定批次；请列明颜色、供应商或批次，或使用 all_colors=True 显式扩大范围",
                {"style": payload.get("style")},
            )
        style_batches = all_batches_by_style(conn, payload["style"], payload.get("color"))
        if not style_batches:
            raise DomainError("not_found", "没有匹配的批次", {"style": payload.get("style")})
        for batch in style_batches:
            add(batch, "款号+颜色/显式全色号")

    if payload.get("all_batches"):
        # 只有质量人员显式声明全部批次时才采用；款号/颜色不会隐式触发。
        for row in conn.execute("SELECT id FROM batches ORDER BY id").fetchall():
            add(row["id"], "质量声明全部批次")

    if payload.get("supplier"):
        if not seeds:
            rows = conn.execute("SELECT id FROM batches WHERE supplier=? ORDER BY id", (payload["supplier"],)).fetchall()
            for row in rows:
                add(row["id"], SCOPE_BASES["supplier"])
        else:
            placeholders = ",".join("?" for _ in seeds)
            rows = conn.execute(f"SELECT id FROM batches WHERE supplier=? AND id NOT IN ({placeholders})", (payload["supplier"],) + tuple(seeds)).fetchall()
            for row in rows:
                add(row["id"], SCOPE_BASES["supplier"])

    if not seeds:
        raise DomainError("insufficient_scope", "没有可用于确定影响范围的批次依据")

    mappings = [dict(r) for r in conn.execute("SELECT parent_batch, child_batch FROM batch_mappings").fetchall()]
    cycle = find_batch_cycle(mappings)
    if cycle:
        raise DomainError("invalid_mapping", "分装关系存在环，无法可靠推导范围", {"cycle": cycle})

    if payload.get("include_mapped"):
        expanded = expand_with_mappings(conn, seeds)
        for batch in expanded:
            add(batch, SCOPE_BASES["batch_mapping"])
    if payload.get("include_supplied"):
        expanded = expand_with_supplier(conn, seeds)
        for batch in expanded:
            add(batch, SCOPE_BASES["supplier"])
    return seeds, basis


def items_for_batches(conn: sqlite3.Connection, batch_ids: list[str]) -> list[dict[str, Any]]:
    if not batch_ids:
        return []
    placeholders = ",".join("?" for _ in batch_ids)
    rows = conn.execute(
        f"SELECT g.*, b.style, b.color, b.supplier FROM garments g JOIN batches b ON b.id=g.batch_id WHERE g.batch_id IN ({placeholders}) ORDER BY g.id",
        tuple(batch_ids),
    ).fetchall()
    return [dict(r) for r in rows]


def open_complaints_for_batches(conn: sqlite3.Connection, batch_ids: list[str]) -> list[dict[str, Any]]:
    if not batch_ids:
        return []
    placeholders = ",".join("?" for _ in batch_ids)
    rows = conn.execute(
        f"""SELECT DISTINCT c.id, c.status, c.stage, c.order_id, c.item_id, t.batch_id, t.item_id AS target_item
            FROM complaints c JOIN complaint_targets t ON t.complaint_id=c.id
            WHERE t.batch_id IN ({placeholders}) AND c.status<>'resolved'
            ORDER BY c.id""",
        tuple(batch_ids),
    ).fetchall()
    return [dict(r) for r in rows]


def restricted_batch_ids(conn: sqlite3.Connection) -> list[str]:
    """所有当前被负面处置覆盖的批次，含身份批次与派生批次。"""
    found: set[str] = set()
    for disposition in active_negative_dispositions(conn):
        found.update(disposition["batch_ids"])
        scope = disposition["scope"]
        if scope.get("all_batches"):
            found.update(r["id"] for r in conn.execute("SELECT id FROM batches").fetchall())
        if scope.get("include_mapped") and disposition["batch_ids"]:
            found.update(expand_with_mappings(conn, disposition["batch_ids"]))
        if scope.get("include_supplied") and disposition["batch_ids"]:
            found.update(expand_with_supplier(conn, disposition["batch_ids"]))
    return sorted(found)


def list_batch_mappings(conn: sqlite3.Connection, batch_id: str | None = None) -> list[dict[str, Any]]:
    if batch_id:
        rows = conn.execute(
            "SELECT * FROM batch_mappings WHERE parent_batch=? OR child_batch=? ORDER BY created_at, id",
            (batch_id, batch_id),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM batch_mappings ORDER BY created_at, id").fetchall()
    return [dict(r) for r in rows]


def add_batch_mapping(conn: sqlite3.Connection, parent: str, child: str, evidence_ref: str | None,
                      note: str | None, actor_id: str, now: str) -> dict[str, Any]:
    """分装/子批次关系必须来自进货或出库记录；款号相同不是依据。"""
    if parent == child:
        raise DomainError("invalid_argument", "批次不能映射到自身", {"batch": parent})
    for batch, label in ((parent, "parent_batch"), (child, "child_batch")):
        if not conn.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
            raise DomainError("not_found", "批次不存在", {"field": label, "batch": batch})
    existing = conn.execute(
        "SELECT * FROM batch_mappings WHERE parent_batch=? AND child_batch=?", (parent, child)
    ).fetchone()
    if existing:
        if evidence_ref and existing["evidence_ref"] != evidence_ref:
            raise DomainError("conflict", "同一映射已有不同依据", {"mapping": existing["id"]})
        return dict(existing)
    mappings = [dict(r) for r in conn.execute("SELECT parent_batch, child_batch FROM batch_mappings").fetchall()]
    mappings.append({"parent_batch": parent, "child_batch": child})
    cycle = find_batch_cycle(mappings)
    if cycle:
        raise DomainError("invalid_mapping", "分装关系会形成环，拒绝登记", {"cycle": cycle})
    mapping_id = f"map-{parent}-{child}"
    conn.execute(
        "INSERT INTO batch_mappings(id,parent_batch,child_batch,evidence_ref,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
        (mapping_id, parent, child, evidence_ref, note, actor_id, now),
    )
    return dict(conn.execute("SELECT * FROM batch_mappings WHERE id=?", (mapping_id,)).fetchone())
