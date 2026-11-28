"""HTTP 接口层。标准库实现，便于本地复算与重启后继续查询。"""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import aftercare, complaints, corrections, dispositions, materials, partners, payments, quality, returns, scope as scope_mod, warehouse
from .auth import Idempotency, Actor, audit, require, resolve_actor
from .domain import DomainError, utcnow
from .store import Store

ERROR_STATUS = {
    "missing_actor": 401,
    "unknown_actor": 401,
    "role_mismatch": 403,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "idempotency_conflict": 409,
    "blocked": 409,
    "duplicate_payment": 409,
    "exceeds_transaction": 409,
    "choice_required": 409,
    "consent_required": 409,
    "invalid_argument": 400,
    "insufficient_scope": 400,
    "invalid_mapping": 400,
    "invalid_material": 400,
    "partner_error": 502,
    "internal_error": 500,
}


PUBLIC_GET = {"/health", "/"}  # 存活检查不需要演练身份


class Application:
    def __init__(self, store: Store, partner_url: str | None = None, default_materials: str | None = None):
        self.store = store
        self.partner_url = partner_url
        self.default_materials = default_materials
        self.idempotency = Idempotency(store)

    # -- 协作方观测落到业务记录 ----------------------------------------
    def partner_handler(self, command_id: str):
        def handle(conn: sqlite3.Connection, command: str, observation: dict[str, Any], actor: Actor | None):
            row = conn.execute("SELECT * FROM partners WHERE command_id=?", (command,)).fetchone()
            if not row:
                return None
            payload = dict(observation)
            payload.pop("command_id", None)
            operation = row["operation"]
            linked_kind = row["linked_kind"]
            linked_id = row["linked_id"]
            if operation == "refund" or linked_kind in {"payment", "refund"}:
                return payments.apply_partner_result(conn, command, payload, actor)
            if linked_kind in {"shipment", "intercept", "ship"} or operation in {"intercept", "ship"}:
                if not linked_id:
                    return {"note": "流水未关联出库单，观测已登记"}
                try:
                    return warehouse.apply_observation(conn, linked_id, payload, "partner", actor.id if actor else None)
                except DomainError as error:
                    return {"error": error.as_dict()}
            return {"note": "观测已登记，无自动业务动作"}
        return handle

    def fetch_partner(self, actor: Actor, command_id: str, partner_url: str | None = None) -> dict[str, Any]:
        return partners.fetch(self.store, actor, command_id, partner_url or self.partner_url, handler=self.partner_handler(command_id))


def _query(parsed: urllib.parse.ParseResult) -> dict[str, Any]:
    return {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}


class Handler(BaseHTTPRequestHandler):
    app: Application

    # -- 基础设施 -------------------------------------------------------
    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False, sort_keys=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # 保持测试输出干净
        pass

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise DomainError("invalid_argument", "请求体不是有效 JSON", {"error": str(error)})
        if not isinstance(value, dict):
            raise DomainError("invalid_argument", "请求体必须是 JSON 对象")
        return value

    def _actor(self, conn: sqlite3.Connection) -> Actor:
        return resolve_actor(conn, self.app.store, self.headers.get("X-Actor"), self.headers.get("X-Role"))

    def _idempotency_key(self) -> str | None:
        return self.headers.get("Idempotency-Key") or self.headers.get("X-Idempotency-Key")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = _query(parsed)
        try:
            body = self._read_json() if method == "POST" else {}
            key = self._idempotency_key()
            if method == "POST":
                cached = self.app.idempotency.lookup(key, body)
                if cached:
                    self._send(cached[0], {**cached[1], "idempotent_replay": True})
                    return
            if method == "GET":
                # 读请求不持有写事务，避免并发查询互相阻塞。
                with self.app.store.read() as conn:
                    if path in PUBLIC_GET:
                        status, result = public_route(self.app, conn, path, query)
                    else:
                        actor = self._actor(conn)
                        status, result = route(self.app, conn, actor, method, path, query, body)
            else:
                with self.app.store.transaction() as conn:
                    actor = self._actor(conn)
                    status, result = route(self.app, conn, actor, method, path, query, body)
                    self.app.idempotency.record(conn, key, body, status, result)
            self._send(status, result)
        except DomainError as error:
            self._send(ERROR_STATUS.get(error.code, 400), {"error": error.as_dict()})
        except sqlite3.IntegrityError as error:
            self._send(409, {"error": {"code": "conflict", "message": "记录冲突", "detail": {"reason": str(error)}}})
        except Exception as error:  # 不泄露堆栈，但仍给出可诊断编号
            self._send(500, {"error": {"code": "internal_error", "message": "服务内部错误", "detail": {"reason": str(error)}}})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def public_route(app: Application, conn: sqlite3.Connection, path: str, query: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if path == "/health":
        return 200, {"status": "ok", "service": "服装批次争议售后办理", "synthetic": True,
                     "now": utcnow(), "schema": "1"}
    if path == "/":
        return 200, {
            "service": "服装批次争议售后办理",
            "synthetic": True,
            "note": "所有业务接口需要 X-Actor；合成资料不构成真实产品安全结论。",
            "docs": "docs/service.md",
            "endpoints": ["/complaints", "/reports", "/dispositions", "/warehouse/inventory",
                          "/warehouse/shipments", "/warehouse/returns", "/payments", "/corrections", "/issues"],
        }
    raise DomainError("not_found", "接口不存在", {"path": path})


def route(app: Application, conn: sqlite3.Connection, actor: Actor, method: str, path: str,
          query: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    parts = [p for p in path.split("/") if p]
    if method == "GET" and path == "/whoami":
        return 200, {"actor": actor.id, "role": actor.role}
    if method == "POST" and path == "/admin/materials/import":
        require(actor, "system")
        target = body.get("path") or app.default_materials
        if not target:
            raise DomainError("invalid_argument", "需要 path 或启动时指定 --materials")
        return 200, materials.import_catalog(app.store, target, actor_id=actor.id)
    if method == "GET" and path == "/materials":
        return 200, {"materials": _materials(app, conn)}

    # 批次与分装关系 ---------------------------------------------------
    if method == "POST" and path == "/batches":
        require(actor, "warehouse", "quality")
        batch_id = body.get("id")
        if not batch_id:
            raise DomainError("invalid_argument", "批次需要 id")
        conn.execute("INSERT OR IGNORE INTO batches(id,style,color,supplier,source) VALUES(?,?,?,?,?)",
                     (batch_id, body.get("style") or "", body.get("color") or "", body.get("supplier"), "manual"))
        audit(conn, actor, "batch.create", "batch", batch_id, {"style": body.get("style"), "color": body.get("color"),
                                                              "supplier": body.get("supplier")})
        return 201, batch_view(conn, batch_id)
    if method == "POST" and path == "/batch-mappings":
        require(actor, "warehouse", "quality")
        if not body.get("evidence_ref"):
            raise DomainError("invalid_argument", "分装/子批次关系必须引用进货或出库记录 evidence_ref")
        mapping = scope_mod.add_batch_mapping(conn, body.get("parent_batch"), body.get("child_batch"),
                                              body.get("evidence_ref"), body.get("note"), actor.id, utcnow())
        audit(conn, actor, "batch_mapping.add", "batch_mapping", mapping["id"],
              {"parent": mapping["parent_batch"], "child": mapping["child_batch"], "evidence": mapping["evidence_ref"]})
        return 201, mapping
    if method == "GET" and path == "/batch-mappings":
        return 200, {"mappings": scope_mod.list_batch_mappings(conn, query.get("batch"))}
    if method == "GET" and path == "/batches":
        rows = [batch_view(conn, r["id"]) for r in conn.execute("SELECT id FROM batches ORDER BY id").fetchall()]
        return 200, {"batches": rows}
    if method == "GET" and path == "/repacks":
        return 200, {"repacks": repack_list(conn, query.get("id"))}
    if method == "GET" and len(parts) == 2 and parts[0] == "repacks":
        items = repack_list(conn, parts[1])
        if not items:
            raise DomainError("not_found", "分装/混箱记录不存在", {"repack": parts[1]})
        return 200, items[0]

    # 转换出库单为交运事实 -----------------------------------------------
    if method == "GET" and path == "/warehouse/stock":
        return 200, stock_summary(conn, query)
    if method == "GET" and path == "/cases":
        return 200, cases_overview(conn, query)
    if method == "GET" and len(parts) == 2 and parts[0] == "orders" and parts[1] != "overview":
        return 200, order_view(conn, parts[1])
    if method == "GET" and path == "/orders":
        return 200, orders_overview(conn, query)

    # 投诉 -------------------------------------------------------------
    if method == "POST" and path == "/complaints":
        return 201, complaints.accept_complaint(app.store, actor, body)
    if method == "GET" and path == "/complaints":
        return 200, complaints.list_complaints(app.store, query)
    if method == "POST" and path == "/complaints/query":
        clues = body.get("clues") or body
        return 200, complaints.search_candidates(app.store, clues)
    if len(parts) >= 2 and parts[0] == "complaints":
        complaint_id = parts[1]
        if method == "GET" and len(parts) == 2:
            return 200, complaints.complaint_view(app.store, complaint_id)
        if method == "GET" and len(parts) == 3 and parts[2] == "case":
            return 200, aftercare.case_explanation(app.store, complaint_id)
        if method == "GET" and len(parts) == 4 and parts[2] == "items":
            with app.store.read() as read_conn:
                explanation = complaints.item_explanation(read_conn, complaint_id, parts[3])
            return 200, explanation
        if method == "POST" and len(parts) == 3 and parts[2] == "evidence":
            return 201, complaints.add_evidence(app.store, actor, complaint_id, body)
        if method == "POST" and len(parts) == 3 and parts[2] == "targets":
            return 201, complaints.add_target(app.store, actor, complaint_id, body)
        if method == "POST" and len(parts) == 3 and parts[2] == "confirm-scope":
            return 200, complaints.confirm_scope(app.store, actor, complaint_id, body)
        if method == "POST" and len(parts) == 3 and parts[2] == "notify":
            return 201, aftercare.notify(app.store, actor, complaint_id, body)
        if method == "POST" and len(parts) == 3 and parts[2] == "choices":
            return 201, aftercare.record_choice(app.store, actor, complaint_id, body)
        if method == "POST" and len(parts) == 3 and parts[2] == "exchanges":
            return 201, aftercare.reserve_exchange(app.store, actor, complaint_id, body)
        if method == "POST" and len(parts) == 3 and parts[2] == "exchange-advance":
            return 200, aftercare.advance_exchange(app.store, actor, complaint_id, body)
        if method == "POST" and len(parts) == 3 and parts[2] == "resolve-item":
            return 200, aftercare.resolve_item(app.store, actor, complaint_id, body)

    # 检测报告 ---------------------------------------------------------
    if method == "POST" and path == "/reports":
        return 201, quality.create_report(app.store, actor, body)
    if len(parts) >= 2 and parts[0] == "reports":
        report_id = parts[1]
        if method == "GET" and len(parts) == 2:
            return 200, quality.get_report(app.store, report_id)
        if method == "POST" and len(parts) == 3 and parts[2] == "withdraw":
            return 200, quality.withdraw_report(app.store, actor, report_id, body)
        if method == "POST" and len(parts) == 3 and parts[2] == "correct":
            return 201, quality.correct_report(app.store, actor, report_id, body)
        if method == "GET" and len(parts) == 3 and parts[2] == "impact":
            return 200, impact_by_report(app, conn, report_id, query)

    # 质量处置 ---------------------------------------------------------
    if method == "POST" and path == "/dispositions":
        return 201, dispositions.create_disposition(app.store, actor, body)
    if method == "GET" and path == "/dispositions":
        return 200, dispositions.list_dispositions(app.store, query)
    if len(parts) >= 2 and parts[0] == "dispositions":
        disposition_id = parts[1]
        if method == "GET" and len(parts) == 2:
            return 200, dispositions.get_disposition(app.store, disposition_id)
        if method == "POST" and len(parts) == 3 and parts[2] == "lift":
            return 200, dispositions.lift_disposition(app.store, actor, disposition_id, body)
        if method == "GET" and len(parts) == 3 and parts[2] == "impact":
            return 200, impact_by_disposition(app, conn, disposition_id)

    # 仓库 -------------------------------------------------------------
    if method == "POST" and path == "/warehouse/returns":
        return 201, returns.receive_return(app.store, actor, body)
    if method == "GET" and path == "/warehouse/returns":
        return 200, returns.list_returns(app.store, query)
    if len(parts) == 4 and parts[0] == "warehouse" and parts[1] == "returns" and method == "POST" and parts[3] == "decide":
        return 200, returns.decide_return(app.store, actor, parts[2], body)
    if method == "POST" and path == "/warehouse/shipments":
        return 201, warehouse.record_dispatch(app.store, actor, body)
    if method == "GET" and path == "/warehouse/shipments":
        return 200, warehouse.list_shipments(app.store, query)
    if len(parts) >= 3 and parts[0] == "warehouse" and parts[1] == "shipments":
        shipment_id = parts[2]
        if method == "GET" and len(parts) == 3:
            with app.store.read() as read_conn:
                return 200, warehouse.shipment_view(read_conn, shipment_id)
        if method == "POST" and len(parts) == 4 and parts[3] == "handover":
            return 200, warehouse.payload_for(app.store, shipment_id, body, actor, kind="with_carrier",
                                               command_id=body.get("command_id"))
        if method == "POST" and len(parts) == 4 and parts[3] == "phase":
            return 200, warehouse.set_phase(app.store, actor, shipment_id, body.get("phase"), body)
        if method == "POST" and len(parts) == 4 and parts[3] == "tracking":
            require(actor, "warehouse", "quality", "independent-aftercare")
            if not body.get("state"):
                raise DomainError("invalid_argument", "运单查询需要 state")
            with app.store.transaction() as write_conn:
                result = warehouse.apply_observation(write_conn, shipment_id, body, "tracking_query", actor.id)
            return 200, result
    if method == "GET" and path == "/warehouse/inventory":
        return 200, inventory_view(conn, query)
    if method == "GET" and len(parts) == 3 and parts[0] == "warehouse" and parts[1] == "items":
        return 200, item_view(conn, parts[2])

    # 协作方 -----------------------------------------------------------
    if method == "POST" and path == "/partners/commands":
        status = partners.dispatch(app.store, actor, body, body.get("partner_url") or app.partner_url)
        require(actor, "warehouse", "independent-aftercare", "quality", "system")
        first = status.get("first_status")
        # 未发起（仅登记）也接受；已发起时回传协作方首次 HTTP 状态。
        return (first if first in (200, 202) else 202), status
    if len(parts) >= 3 and parts[0] == "partners" and parts[1] == "commands":
        command_id = parts[2]
        if method == "GET" and len(parts) == 3:
            return 200, app.fetch_partner(actor, command_id)
        if method == "POST" and len(parts) == 4 and parts[3] == "observation" and not body.get("sequence"):
            # 离线演练：直接写入一条合成观测，等价于 GET 协作方后的应用。
            with app.store.transaction() as write_conn:
                row = write_conn.execute("SELECT * FROM partners WHERE command_id=?", (command_id,)).fetchone()
                if not row:
                    raise DomainError("not_found", "未登记该协作方流水", {"command_id": command_id})
                handler = app.partner_handler(command_id)
                applied = handler(write_conn, command_id, body, actor)
                write_conn.execute("UPDATE partners SET state='observed', last_observation_json=?, updated_at=? WHERE command_id=?",
                                   (json.dumps(body, ensure_ascii=False), utcnow(), command_id))
                audit(write_conn, actor, "partner.observation.manual", "partner_command", command_id, body)
            return 200, {"command_id": command_id, "observation": body, "applied": applied}
        if method == "POST" and len(parts) == 4 and parts[3] == "replay":
            # 按给定观测序列逐条应用，用于复算协作方"每次查询只返回下一条"的行为。
            sequence = body.get("observations")
            if not isinstance(sequence, list) or not sequence:
                raise DomainError("invalid_argument", "replay 需要非空 observations 列表")
            applied_list = []
            for observation in sequence:
                if not isinstance(observation, dict):
                    raise DomainError("invalid_argument", "每条观测必须是对象")
                with app.store.transaction() as write_conn:
                    row = write_conn.execute("SELECT * FROM partners WHERE command_id=?", (command_id,)).fetchone()
                    if not row:
                        raise DomainError("not_found", "未登记该协作方流水", {"command_id": command_id})
                    handler = app.partner_handler(command_id)
                    applied = handler(write_conn, command_id, observation, actor)
                    write_conn.execute("UPDATE partners SET state='observed', last_observation_json=?, updated_at=? WHERE command_id=?",
                                       (json.dumps(observation, ensure_ascii=False), utcnow(), command_id))
                    audit(write_conn, actor, "partner.observation.replay", "partner_command", command_id, observation)
                applied_list.append({"observation": observation, "applied": applied})
            return 200, {"command_id": command_id, "applied": applied_list}

    # 赔付 -------------------------------------------------------------
    if method == "POST" and path == "/payments":
        return 201, payments.create_payment(app.store, actor, body)
    if method == "GET" and path == "/payments":
        return 200, payments.list_payments(app.store, query)
    if len(parts) >= 2 and parts[0] == "payments":
        payment_id = parts[1]
        if method == "GET" and len(parts) == 2:
            return 200, payments.get_payment(app.store, payment_id)
        if method == "POST" and len(parts) == 3 and parts[2] == "submit":
            return 200, payments.submit_payment(app.store, actor, payment_id, body)

    # 更正影响与待办 ---------------------------------------------------
    if method == "POST" and path == "/corrections":
        return 201, corrections.assess_correction(app.store, actor, body)
    if method == "GET" and path == "/corrections":
        return 200, corrections.list_corrections(app.store, query)
    if method == "POST" and path == "/issues":
        return 201, aftercare.open_issue(app.store, actor, body)
    if method == "GET" and path == "/issues":
        return 200, aftercare.list_issues(app.store, query)
    if len(parts) == 3 and parts[0] == "issues" and method == "POST" and parts[2] == "resolve":
        return 200, aftercare.resolve_issue(app.store, actor, parts[1], body)

    # 只读查询 ---------------------------------------------------------
    if method == "GET" and len(parts) == 2 and parts[0] == "batches":
        return 200, batch_view(conn, parts[1])
    if method == "GET" and path == "/audit":
        return 200, {"events": [dict(r) for r in conn.execute(
            "SELECT event_id,at,actor,role,action,subject_type,subject_id,detail_json FROM audit ORDER BY event_id DESC LIMIT ?",
            (int(query.get("limit", "200")),)).fetchall()]}
    raise DomainError("not_found", "接口不存在", {"path": path, "method": method})


def _materials(app: Application, conn: sqlite3.Connection) -> Any:
    return app.store.get_meta(conn, "materials", None)


def impact_by_report(app: Application, conn: sqlite3.Connection, report_id: str, query: dict[str, Any]) -> dict[str, Any]:
    report = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not report:
        raise DomainError("not_found", "检测报告不存在", {"report": report_id})
    payload: dict[str, Any] = {"report": report_id}
    payload["include_mapped"] = query.get("include_mapped") in {"1", "true", "yes"}
    payload["include_supplied"] = query.get("include_supplied") in {"1", "true", "yes"}
    batches, basis = scope_mod.resolve_scope_batches(conn, payload)
    result = _impact(conn, {"report": report_id, "status": report["status"]}, payload, batches, basis)
    if report["status"] != "active":
        result["warning"] = f"该报告当前状态为 {report['status']}，以下范围仅用于追溯，不能作为新的处置依据。"
    return result


def impact_by_disposition(app: Application, conn: sqlite3.Connection, disposition_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM dispositions WHERE id=?", (disposition_id,)).fetchone()
    if not row:
        raise DomainError("not_found", "处置不存在", {"disposition": disposition_id})
    scope = json.loads(row["scope_json"] or "{}")
    payload: dict[str, Any] = {"disposition": disposition_id, "include_mapped": scope.get("include_mapped", False),
                               "include_supplied": scope.get("include_supplied", False)}
    batches, basis = scope_mod.resolve_scope_batches(conn, payload)
    if scope.get("all_batches"):
        batches = [r["id"] for r in conn.execute("SELECT id FROM batches ORDER BY id").fetchall()]
        basis = [{"batch": b, "basis": "处置声明全部批次"} for b in batches]
    return _impact(conn, {"disposition": disposition_id}, payload, batches, basis)


def _impact(conn: sqlite3.Connection, subject: dict[str, Any], payload: dict[str, Any],
            batches: list[str], basis: list[dict[str, str]]) -> dict[str, Any]:
    items = scope_mod.items_for_batches(conn, batches)
    item_ids = [i["id"] for i in items]
    shipments = []
    fulfilments = []
    if item_ids:
        ph = ",".join("?" for _ in item_ids)
        shipments = [dict(r) for r in conn.execute(
            f"SELECT id,item_id,batch_id,phase,kind FROM shipments WHERE item_id IN ({ph}) ORDER BY id", tuple(item_ids)).fetchall()]
        fulfilments = [dict(r) for r in conn.execute(
            f"SELECT id,complaint_id,item_id,status,reserved_item,replacement_item FROM fulfillments WHERE reserved_item IN ({ph}) OR replacement_item IN ({ph}) ORDER BY id",
            tuple(item_ids) + tuple(item_ids)).fetchall()]
    states: dict[str, int] = {}
    for item in items:
        states[item["state"]] = states.get(item["state"], 0) + 1
    return {
        "subject": subject,
        "bases": basis,
        "batches": batches,
        "batch_count": len(batches),
        "items": [{"id": i["id"], "batch": i["batch_id"], "state": i["state"], "outcome": i["current_outcome"]} for i in items],
        "item_states": states,
        "shipments": shipments,
        "fulfilments": fulfilments,
        "open_complaints": [c["id"] for c in scope_mod.open_complaints_for_batches(conn, batches)],
        "restriction_by_batch": {b: [d["id"] for d in scope_mod.active_restrictions_for_batch(conn, b)] for b in batches},
        "note": "范围来自证据列明批次及其进货/分装映射；款号相同不自动并入。",
    }


def inventory_view(conn: sqlite3.Connection, query: dict[str, Any]) -> dict[str, Any]:
    sql = "SELECT g.*, b.style, b.color, b.supplier FROM garments g JOIN batches b ON b.id=g.batch_id WHERE 1=1"
    params: list[Any] = []
    if query.get("batch"):
        sql += " AND g.batch_id=?"
        params.append(query["batch"])
    if query.get("state"):
        sql += " AND g.state=?"
        params.append(query["state"])
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY g.id", tuple(params)).fetchall()]
    items = []
    for row in rows:
        restrictions = scope_mod.active_restrictions_for_batch(conn, row["batch_id"])
        items.append({
            "id": row["id"], "batch": row["batch_id"], "serial": row["serial"], "state": row["state"],
            "outcome": row["current_outcome"], "reason": row["outcome_reason"],
            "restricted": bool(restrictions), "restrictions": [d["id"] for d in restrictions],
            "available": row["state"] == "in_stock" and not restrictions and row["current_outcome"] in {None, "resell"},
        })
    counts: dict[str, int] = {}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {"items": items, "counts": counts,
            "note": "in_stock 仅表示实物在库；available 还需该批次无有效质量限制。"}


def item_view(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT g.*, b.style, b.color, b.supplier FROM garments g JOIN batches b ON b.id=g.batch_id WHERE g.id=?", (item_id,)).fetchone()
    if not row:
        raise DomainError("not_found", "商品不存在", {"item": item_id})
    value = dict(row)
    value["restrictions"] = [d["id"] for d in scope_mod.active_restrictions_for_batch(conn, row["batch_id"])]
    value["fulfilments"] = [dict(r) for r in conn.execute(
        "SELECT id,complaint_id,status,reserved_item,replacement_item FROM fulfillments WHERE item_id=? OR reserved_item=? OR replacement_item=? ORDER BY id",
        (item_id, item_id, item_id)).fetchall()]
    value["returns"] = [dict(r) for r in conn.execute("SELECT id,warehouse_outcome,outcome_reason FROM returns WHERE item_id=?", (item_id,)).fetchall()]
    value["shipments"] = [dict(r) for r in conn.execute("SELECT id,phase,kind FROM shipments WHERE item_id=?", (item_id,)).fetchall()]
    value["orders"] = [dict(r) for r in conn.execute("SELECT order_id,paid FROM order_lines WHERE item_id=?", (item_id,)).fetchall()]
    return value


def order_view(conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        raise DomainError("not_found", "订单不存在", {"order": order_id})
    value = dict(row)
    lines = [dict(r) for r in conn.execute("SELECT item_id,paid FROM order_lines WHERE order_id=? ORDER BY item_id", (order_id,)).fetchall()]
    for line in lines:
        line["batch"] = (conn.execute("SELECT batch_id FROM garments WHERE id=?", (line["item_id"],)).fetchone() or {"batch_id": None})["batch_id"]
        line["payments"] = [dict(r) for r in conn.execute(
            "SELECT id,kind,amount,state FROM payments WHERE order_id=? AND item_id=?", (order_id, line["item_id"])).fetchall()]
        line["complaints"] = [r["id"] for r in conn.execute(
            "SELECT DISTINCT c.id FROM complaints c JOIN complaint_targets t ON t.complaint_id=c.id WHERE t.item_id=?", (line["item_id"],)).fetchall()]
    value["lines"] = lines
    value["complaints"] = [r["id"] for r in conn.execute("SELECT id FROM complaints WHERE order_id=? ORDER BY id", (order_id,)).fetchall()]
    return value


def batch_view(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise DomainError("not_found", "批次不存在", {"batch": batch_id})
    value = dict(row)
    value["items"] = [dict(r) for r in conn.execute("SELECT id,serial,state,current_outcome FROM garments WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()]
    value["restrictions"] = [d["id"] for d in scope_mod.active_restrictions_for_batch(conn, batch_id)]
    value["mappings"] = [
        {"parent": r["parent_batch"], "child": r["child_batch"]}
        for r in conn.execute("SELECT parent_batch,child_batch FROM batch_mappings WHERE parent_batch=? OR child_batch=?", (batch_id, batch_id)).fetchall()
    ]
    return value


def build_server(store: Store, host: str = "127.0.0.1", port: int = 0, partner_url: str | None = None,
                 materials_path: str | None = None) -> ThreadingHTTPServer:
    store.initialize()
    app = Application(store, partner_url=partner_url, default_materials=materials_path)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    return server


def stock_summary(conn: sqlite3.Connection, query: dict[str, Any]) -> dict[str, Any]:
    """仓库视角：在库、已分拣未交运、在途、拦截中、退货隔离分别多少。"""
    return inventory_view(conn, query)


def orders_overview(conn: sqlite3.Connection, query: dict[str, Any]) -> dict[str, Any]:
    sql = "SELECT id FROM orders WHERE 1=1"
    params: list[Any] = []
    if query.get("customer"):
        sql += " AND customer=?"
        params.append(query["customer"])
    rows = conn.execute(sql + " ORDER BY id", tuple(params)).fetchall()
    return {"orders": [order_view(conn, r["id"]) for r in rows]}


def cases_overview(conn: sqlite3.Connection, query: dict[str, Any]) -> dict[str, Any]:
    sql = "SELECT id FROM complaints WHERE 1=1"
    params: list[Any] = []
    for field in ("status", "order_id"):
        if query.get(field):
            sql += f" AND {field}=?"
            params.append(query[field])
    rows = conn.execute(sql + " ORDER BY created_at, id", tuple(params)).fetchall()
    case_views = {r["id"]: complaints.complaint_view(_StoreView(conn), r["id"]) for r in rows}
    cases = []
    for row in rows:
        complaint = case_views[row["id"]]
        cases.append({
            "id": complaint["id"],
            "order_id": complaint["order_id"],
            "status": complaint["status"],
            "status_label": complaint["status_label"],
            "targets": [{"item": t["item_id"], "status": t["status"], "batch": t["batch_id"]} for t in complaint["targets"]],
            "open_issues": len([i for i in complaint["issues"] if i["status"] == "open"]),
            "pending_actions": sorted({p for e in complaint["item_explanations"] for p in e["pending"]}),
        })
    return {"cases": cases}


def repack_list(conn: sqlite3.Connection, repack_id: str | None = None) -> list[dict[str, Any]]:
    """分装/混箱资料：同一箱内可能有多批次；同箱不等于同批。"""
    sql = "SELECT * FROM repacks"
    params: tuple = ()
    if repack_id:
        sql += " WHERE id=?"
        params = (repack_id,)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    value = []
    for row in rows:
        items = json.loads(row["items_json"])
        detail = []
        batches = set()
        for item_id in items:
            garment = conn.execute("SELECT id,batch_id,serial,state FROM garments WHERE id=?", (item_id,)).fetchone()
            if garment:
                detail.append({"id": garment["id"], "batch": garment["batch_id"], "serial": garment["serial"], "state": garment["state"]})
                batches.add(garment["batch_id"])
            else:
                detail.append({"id": item_id, "batch": None, "note": "资料中缺少该实物记录"})
        value.append({
            "id": row["id"],
            "from_box": row["from_box"],
            "to_box": row["to_box"],
            "items": detail,
            "batch_ids": sorted(b for b in batches if b),
            "mixed_batches": len(batches) > 1,
            "note": "同一箱内可能存在多个批次；分装不改变每件实物所属批次。",
        })
    return value


class _StoreView:
    """把现有连接包装成只读 Store 接口，供按件解释复用。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def read(self):
        from contextlib import contextmanager

        @contextmanager
        def _read():
            yield self._conn

        return _read()
