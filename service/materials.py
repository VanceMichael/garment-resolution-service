"""资料导入与应用初始化。

- 导入 data/catalog.json 中的合成事实（批次、实物、订单、分装、投诉、报告、退货）；
- 识别 import 记录形状与 catalog 记录形状；
- 导入只写原始事实，不代替业务人员作出处置或赔付决定；
- 重复导入同一文件是安全的：按记录 id 幂等。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .auth import Actor, audit
from .domain import DomainError, fingerprint, utcnow
from .store import Store

CATALOG_KINDS = {"batch", "garment", "order", "repack", "complaint_input", "quality_report", "customer_choice", "return_input"}


def load_catalog(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise DomainError("invalid_material", "资料文件必须是 JSON 对象")
    return value


def import_catalog(store: Store, path: str | Path, actor_id: str = "system:import") -> dict[str, Any]:
    catalog = load_catalog(path)
    actor = Actor(actor_id, "system")
    counts = {kind: 0 for kind in sorted(CATALOG_KINDS)}
    skipped: list[str] = []
    with store.transaction() as conn:
        for principal, role in (catalog.get("principals") or {}).items():
            conn.execute("INSERT OR IGNORE INTO principals(id,role) VALUES(?,?)", (principal, role))
        for record in catalog.get("records", []):
            kind = record.get("kind")
            if kind not in CATALOG_KINDS:
                skipped.append(record.get("id") or "<无编号>")
                continue
            getattr(_importer(kind), "apply")(conn, record, actor)
            counts[kind] += 1
        for order in catalog.get("orders", []) if isinstance(catalog.get("orders"), list) else []:
            _import_order(conn, order)
        store.set_meta(conn, "materials", {"path": str(path), "fingerprint": fingerprint(catalog), "at": utcnow()})
        audit(conn, actor, "materials.import", "catalog", str(path), {"counts": counts, "skipped": skipped})
    return {"imported": counts, "skipped": skipped, "principals": sorted((catalog.get("principals") or {}).keys())}


def _importer(kind: str):
    return _Importers()


class _Importers:
    def apply(self, conn, record, actor):  # pragma: no cover - 分发器
        kind = record.get("kind")
        handler = getattr(self, "_" + kind, None)
        if handler is None:
            raise DomainError("invalid_material", "未知记录类型", {"kind": kind})
        handler(conn, record, actor)

    def _batch(self, conn, record, actor):
        batch_id = record.get("id")
        if not batch_id:
            raise DomainError("invalid_material", "批次缺少 id")
        conn.execute(
            "INSERT OR IGNORE INTO batches(id,style,color,supplier,source) VALUES(?,?,?,?,?)",
            (batch_id, record.get("style") or "", record.get("color") or "", record.get("supplier"), record.get("source") or "catalog"),
        )

    def _garment(self, conn, record, actor):
        garment_id = record.get("id")
        refs = record.get("refs") or []
        if not garment_id or not refs:
            raise DomainError("invalid_material", "实物记录需要 id 和所属批次 refs", {"record": record})
        batch_id = refs[0]
        if not conn.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone():
            raise DomainError("invalid_material", "实物引用的批次不存在", {"item": garment_id, "batch": batch_id})
        conn.execute(
            "INSERT OR IGNORE INTO garments(id,batch_id,serial,state,updated_at) VALUES(?,?,?,?,?)",
            (garment_id, batch_id, record.get("serial"), record.get("state") or "in_stock", utcnow()),
        )

    def _order(self, conn, record, actor):
        order_id = record.get("id")
        if not order_id:
            raise DomainError("invalid_material", "订单缺少 id")
        lines = record.get("lines") or []
        total = sum(int(line.get("paid", 0)) for line in lines)
        extra = {k: v for k, v in record.items() if k not in {"id", "kind", "refs", "customer", "lines", "shop", "placed_at"}}
        conn.execute(
            "INSERT OR IGNORE INTO orders(id,customer,shop,placed_at) VALUES(?,?,?,?)",
            (order_id, record.get("customer"), record.get("shop"), record.get("placed_at")),
        )
        for line in lines:
            item_id = line.get("item")
            if not item_id:
                raise DomainError("invalid_material", "订单分项缺少商品", {"order": order_id})
            if not conn.execute("SELECT 1 FROM garments WHERE id=?", (item_id,)).fetchone():
                raise DomainError("invalid_material", "订单分项引用的商品不存在；款号单据不能替代实物记录", {"order": order_id, "item": item_id})
            conn.execute(
                "INSERT OR IGNORE INTO order_lines(order_id,item_id,paid) VALUES(?,?,?)",
                (order_id, item_id, int(line.get("paid", 0))),
            )
        if extra:
            conn.execute("INSERT OR IGNORE INTO order_meta(order_id,data_json) VALUES(?,?)", (order_id, json.dumps(extra, ensure_ascii=False)))

    def _repack(self, conn, record, actor):
        items = record.get("refs") or []
        if not items:
            raise DomainError("invalid_material", "分装记录需要列明实物 refs")
        conn.execute(
            "INSERT OR IGNORE INTO repacks(id,from_box,to_box,items_json,evidence_ref,created_at) VALUES(?,?,?,?,?,?)",
            (record.get("id"), record.get("from_box"), record.get("to_box"), json.dumps(items, ensure_ascii=False), record.get("id"), utcnow()),
        )

    def _complaint_input(self, conn, record, actor):
        """原始投诉线索只保存，不自动确认批次，也不自动作出处置。"""
        complaint_id = record.get("id")
        if not complaint_id:
            raise DomainError("invalid_material", "投诉线索缺少 id")
        order = None
        item = None
        for ref in record.get("refs") or []:
            if conn.execute("SELECT 1 FROM orders WHERE id=?", (ref,)).fetchone():
                order = ref
            elif conn.execute("SELECT 1 FROM garments WHERE id=?", (ref,)).fetchone():
                item = ref
        clues = {key: record.get(key) for key in ("style", "color", "shop", "purchase_date", "batch", "description", "reporter")}
        if order:
            clues["order"] = order
        if item:
            clues["items"] = [item]
        if not order and not item and not any(clues.get(k) for k in ("style", "shop", "purchase_date")):
            raise DomainError("invalid_material", "投诉线索不足以保留待查范围")
        now = utcnow()
        conn.execute(
            "INSERT OR IGNORE INTO complaints(id,order_id,item_id,clues_json,status,stage,description,reporter,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (complaint_id, order, item, json.dumps(clues, ensure_ascii=False, sort_keys=True),
             "investigating" if (order or item) else "open", "intake", record.get("description"), record.get("reporter"), actor.id, now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO complaint_inputs(id,complaint_id,payload_json,fingerprint,submitted_by,submitted_at) VALUES(?,?,?,?,?,?)",
            (f"catalog-{complaint_id}", complaint_id, json.dumps(record, ensure_ascii=False, sort_keys=True), fingerprint(record), actor.id, now),
        )
        if item:
            batch = conn.execute("SELECT batch_id FROM garments WHERE id=?", (item,)).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO complaint_targets(complaint_id,item_id,batch_id,basis,status,added_by,added_at) VALUES(?,?,?,?,?,?,?)",
                (complaint_id, item, batch["batch_id"] if batch else None, "catalog 原始投诉线索", "open", actor.id, now),
            )
        if order:
            # 订单只有部分分项被投诉时，不自动把所有分项都列入；
            # 本投诉的范围由质量负责人凭证据确认。
            pass

    def _quality_report(self, conn, record, actor):
        report_id = record.get("id")
        refs = record.get("refs") or []
        if not report_id or not refs:
            raise DomainError("invalid_material", "检测报告需要 id 与批次 refs")
        digest = record.get("digest")
        if not digest:
            raise DomainError("invalid_material", "检测报告需要文档摘要 digest")
        now = utcnow()
        conn.execute(
            "INSERT OR IGNORE INTO reports(id,batch_id,scope_json,conclusion,digest,signed_by,issued_at,status,source_document,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (report_id, refs[0], "{}", record.get("conclusion") or "", digest, record.get("signed_by") or "未签名",
             record.get("issued_at") or now, "active", json.dumps(record, ensure_ascii=False, sort_keys=True), now),
        )
        for batch in refs:
            if conn.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
                conn.execute("INSERT OR IGNORE INTO report_batches(report_id,batch_id,basis) VALUES(?,?,?)",
                             (report_id, batch, "catalog 报告引用"))

    def _customer_choice(self, conn, record, actor):
        """catalog 中的顾客选择只作为资料线索：未经独立确认不消耗库存。"""
        choice_id = record.get("id") or f"catalog-choice-{fingerprint(record)[:10]}"
        refs = record.get("refs") or []
        order = next((r for r in refs if conn.execute("SELECT 1 FROM orders WHERE id=?", (r,)).fetchone()), None)
        items = [r for r in refs if conn.execute("SELECT 1 FROM garments WHERE id=?", (r,)).fetchone()]
        if not items:
            return
        complaint = conn.execute("SELECT id FROM complaints WHERE order_id=?", (order,)).fetchone() if order else None
        now = utcnow()
        conn.execute(
            "INSERT OR IGNORE INTO choices(id,complaint_id,item_id,choice,confirmed,confirmed_by,confirmed_at,note) VALUES(?,?,?,?,?,?,?,?)",
            (choice_id, complaint["id"] if complaint else None, items[0], record.get("choice") or "exchange", 0, None, None,
             "catalog 资料中的选择记录；需顾客独立确认后才推进"),
        )

    def _return_input(self, conn, record, actor):
        return_id = record.get("id")
        refs = record.get("refs") or []
        item = next((r for r in refs if conn.execute("SELECT 1 FROM garments WHERE id=?", (r,)).fetchone()), None)
        order = next((r for r in refs if conn.execute("SELECT 1 FROM orders WHERE id=?", (r,)).fetchone()), None)
        if not return_id or not item:
            raise DomainError("invalid_material", "退货资料需要 id 与实物 refs")
        from . import scope as scope_mod
        garment = conn.execute("SELECT * FROM garments WHERE id=?", (item,)).fetchone()
        restrictions = scope_mod.active_restrictions_for_batch(conn, garment["batch_id"])
        now = utcnow()
        complaint = conn.execute("SELECT id FROM complaints WHERE order_id=?", (order,)).fetchone() if order else None
        outcome = "quarantine" if restrictions else "awaiting_disposition"
        reason = ("该件批次受有效质量处置限制：" + "、".join(d["id"] for d in restrictions)) if restrictions else "退货已接收；需完成状态复核"
        conn.execute(
            "INSERT OR IGNORE INTO returns(id,item_id,order_id,complaint_id,condition,received_by,received_at,warehouse_outcome,outcome_reason,restricted_batch,disposition_basis_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (return_id, item, order, complaint["id"] if complaint else None, record.get("condition") or "unknown",
             record.get("received_by") or actor.id, record.get("received_at") or now, outcome, reason,
             1 if restrictions else 0, json.dumps([d["id"] for d in restrictions], ensure_ascii=False)),
        )
        conn.execute("UPDATE garments SET state='returned', current_outcome=?, outcome_reason=?, updated_at=? WHERE id=?",
                     (outcome, reason, now, item))


def _import_order(conn, order: dict[str, Any]) -> None:
    order_id = order.get("id") or order.get("order")
    if not order_id:
        raise DomainError("invalid_material", "订单缺少 id")
    conn.execute("INSERT OR IGNORE INTO orders(id,customer,shop,placed_at) VALUES(?,?,?,?)",
                 (order_id, order.get("customer"), order.get("shop"), order.get("placed_at")))
    for line in order.get("lines", []):
        conn.execute("INSERT OR IGNORE INTO order_lines(order_id,item_id,paid) VALUES(?,?,?)",
                     (order_id, line.get("item"), int(line.get("paid", 0))))
