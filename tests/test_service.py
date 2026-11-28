"""业务服务端到端检查：投诉、处置、仓库、换货与赔付。

全部使用合成资料；重启用同一 SQLite 文件验证结果可继续查询。
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service import materials  # noqa: E402
from service.auth import Actor  # noqa: E402
from service.http_api import build_server  # noqa: E402
from service.store import Store  # noqa: E402


class Client:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, body=None, actor=None, role=None, key=None):
        raw = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor"] = actor
        if role:
            headers["X-Role"] = role
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(self.base + path, data=raw, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def get(self, path, actor):
        return self.call("GET", path, actor=actor)

    def post(self, path, body, actor, **kw):
        return self.call("POST", path, body, actor=actor, **kw)


class ServiceBase(unittest.TestCase):
    extra_materials = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.temp.name) / "state.sqlite3")
        materials_path = ROOT / "data" / "catalog.json"
        if self.extra_materials:
            custom = Path(self.temp.name) / "materials.json"
            base = json.loads(materials_path.read_text())
            base["records"].extend(self.extra_materials)
            custom.write_text(json.dumps(base, ensure_ascii=False))
            materials_path = custom
        self.materials_path = materials_path
        self.start()

    def start(self):
        store = Store(self.state)
        store.initialize()
        materials.import_catalog(store, self.materials_path, actor_id="system:seed")
        self.server = build_server(store, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client("http://127.0.0.1:" + str(self.server.server_address[1]))

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def restart(self):
        self.stop()
        self.start()

    def tearDown(self):
        try:
            self.stop()
        finally:
            self.temp.cleanup()

    def disposition(self, actor="quality-A", **body):
        body.setdefault("id", "disp-test")
        body.setdefault("reason", "合成检查")
        return self.client.post("/dispositions", body, actor)

    def handover(self, shipment, item, actor="warehouse-A", **body):
        status, _ = self.client.post("/warehouse/shipments", {"id": shipment, "item": item, "hand_over": True, **body}, actor)
        self.assertEqual(status, 409 if body.pop("expect_blocked", False) else 201)
        return status


class BatchIsolation(ServiceBase):
    def test_report_only_affects_its_own_batch(self):
        status, view = self.client.get("/reports/report-B/impact", "quality-A")
        self.assertEqual(status, 200)
        self.assertEqual(view["batches"], ["batch-B"])
        self.assertNotIn("batch-A", view["batches"])
        self.assertNotIn("batch-C", view["batches"])
        self.assertEqual({i["id"] for i in view["items"]}, {"item-B"})

    def test_style_alone_rejected_but_explicit_color_expands(self):
        status, body = self.client.post("/complaints/query", {"style": "dress-1"}, "quality-A")
        self.assertEqual(status, 200)
        candidates = {c["batch"] for c in body["batches"]}
        self.assertEqual(candidates, {"batch-A", "batch-B", "batch-C"})
        self.assertFalse(body["confirmed"])
        status, body = self.client.post("/dispositions", {"id": "disp-style", "type": "stop_supply", "reason": "x", "style": "dress-1"}, "quality-A")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "insufficient_scope")

    def test_mapping_expansion_is_evidence_based(self):
        status, batch = self.client.get("/batches/batch-B", "quality-A")
        self.assertEqual(status, 200)
        self.assertEqual(batch["mappings"], [])
        self.assertNotIn("batch-A", batch["restrictions"])
        status, impact = self.client.get("/reports/report-B/impact?include_supplied=1", "quality-A")
        self.assertEqual(status, 200)
        # 供应商不同，不得因同款同色并入。
        self.assertEqual(impact["batches"], ["batch-B"])


class IntakeAndScope(ServiceBase):
    def test_unknown_clue_stays_open_and_can_add_evidence(self):
        status, complaint = self.client.get("/complaints/complaint-unknown", "customer-A")
        self.assertEqual(status, 200)
        self.assertEqual(complaint["status"], "open")
        self.assertTrue(complaint["confirmed_scope"]["has_unconfirmed_batch"])
        status, body = self.client.post("/dispositions", {"id": "disp-guess", "type": "stop_supply", "reason": "猜",
                                                          "complaint": "complaint-unknown"}, "quality-A")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "insufficient_scope")
        status, updated = self.client.post("/complaints/complaint-unknown/evidence",
                                           {"id": "ev-1", "kind": "tag_photo", "value": "cloth-C", "item": "item-C"}, "customer-A")
        self.assertEqual(status, 201)
        self.assertTrue(any(t["item_id"] == "item-C" for t in updated["targets"]))
        self.assertEqual(updated["status"], "investigating")
        status, body = self.client.post("/dispositions", {"id": "disp-guess2", "type": "stop_supply", "reason": "猜",
                                                          "complaint": "complaint-unknown"}, "quality-A")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "insufficient_scope")

    def test_merchant_cannot_overwrite_complaint_input_or_report(self):
        status, body = self.client.post("/complaints", {"id": "complaint-A", "order": "order-A"}, "merchant-A")
        self.assertEqual(status, 409)
        status, body = self.client.post("/reports/report-B/withdraw", {"reason": "商家要求"}, "merchant-A")
        self.assertEqual(status, 403)
        status, body = self.client.post("/reports", {"id": "report-B", "conclusion": "商家改写", "digest": "a" * 64,
                                                     "batches": ["batch-A"]}, "merchant-A")
        self.assertEqual(status, 403)

    def test_complaint_with_order_splits_items(self):
        # 投诉只点了一件商品时，不把同订单其他商品自动认定为同一问题。
        status, complaint = self.client.get("/complaints/complaint-A", "customer-A")
        self.assertEqual(status, 200)
        self.assertEqual({t["item_id"] for t in complaint["targets"]}, {"item-B"})
        status, complaint = self.client.post("/complaints/complaint-A/targets",
                                             {"item": "item-A", "basis": "商家确认同订单同批出库，需分开核查"}, "merchant-A")
        self.assertEqual(status, 201)
        self.assertEqual({t["item_id"] for t in complaint["targets"]}, {"item-A", "item-B"})
        status, body = self.client.post("/complaints/complaint-A/resolve-item",
                                        {"item": "item-A", "reason": "该件另行处理"}, "reviewer-A")
        self.assertEqual(status, 200)
        self.assertNotEqual(body["status"], "resolved")
        self.assertTrue(any(t["status"] == "open" for t in body["targets"] if t["item_id"] == "item-B"))


class DispositionRules(ServiceBase):
    extra_materials = [
        {"id": "item-D", "kind": "garment", "refs": ["batch-A"], "serial": "cloth-D", "state": "picked"},
    ]

    def test_stop_supply_quarantines_stock_and_picked_not_handed(self):
        status, view = self.disposition(id="disp-B", type="stop_supply", report="report-B")
        self.assertEqual(status, 201, view)
        self.assertEqual(view["effective_batch_ids"], ["batch-B"])
        self.assertIn("item-B", view["warehouse_effect"]["quarantined"])
        status, inventory = self.client.get("/warehouse/inventory", "warehouse-A")
        item_b = next(i for i in inventory["items"] if i["id"] == "item-B")
        self.assertTrue(item_b["restricted"])
        self.assertFalse(item_b["available"])
        item_a = next(i for i in inventory["items"] if i["id"] == "item-A")
        self.assertFalse(item_a["restricted"])

    def test_picked_package_blocked_from_handover(self):
        status, view = self.disposition(id="disp-A", type="stop_supply", batches=["batch-A"])
        self.assertEqual(status, 201)
        self.assertIn("item-D", view["warehouse_effect"]["blocked_picked"])
        status, body = self.client.post("/warehouse/shipments",
                                        {"id": "ship-D", "item": "item-D", "hand_over": True}, "warehouse-A")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "blocked")

    def test_withdraw_report_keeps_other_basis_and_restriction(self):
        self.disposition(id="disp-B1", type="stop_supply", report="report-B")
        self.disposition(id="disp-B2", type="not_conforming", batches=["batch-B"])
        status, body = self.client.post("/reports/report-B/withdraw", {"reason": "取样口径更正"}, "quality-A")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "withdrawn")
        status, batch = self.client.get("/batches/batch-B", "quality-A")
        self.assertIn("disp-B2", batch["restrictions"])
        self.assertNotIn("batch-A", batch["restrictions"])
        status, inv = self.client.get("/warehouse/items/item-B", "warehouse-A")
        self.assertIn("disp-B2", inv["restrictions"])

    def test_lift_one_basis_restores_only_unrestricted_inventory(self):
        self.disposition(id="disp-1", type="investigate", batches=["batch-B"])
        self.disposition(id="disp-2", type="stop_supply", batches=["batch-B"])
        status, view = self.client.post("/dispositions/disp-1/lift", {"reason": "调查结束，另一依据仍有效"}, "quality-A")
        self.assertEqual(status, 200)
        self.assertIn("disp-2", view["remaining_restrictions"]["batch-B"])
        self.assertEqual(view["restored_items"], [])
        status, view2 = self.client.post("/dispositions/disp-2/lift", {"reason": "另一依据解除"}, "quality-A")
        self.assertEqual(status, 200)
        status, batch = self.client.get("/batches/batch-B", "quality-A")
        self.assertEqual(batch["restrictions"], [])

    def test_disposition_scope_can_find_truly_affected_batch(self):
        status, view = self.disposition(id="disp-C", type="stop_supply", report="report-C")
        self.assertEqual(status, 201)
        self.assertEqual(view["effective_batch_ids"], ["batch-C"])
        self.assertIn("item-C", view["warehouse_effect"]["quarantined"])


class CarrierAndWarehouse(ServiceBase):
    extra_materials = [
        {"id": "item-D", "kind": "garment", "refs": ["batch-A"], "serial": "cloth-D", "state": "picked"},
    ]

    def test_intercept_unknown_then_confirmed(self):
        self.handover("SHIP-A", "item-A")
        status, view = self.disposition(id="disp-A", type="stop_supply", batches=["batch-A"])
        self.assertEqual(status, 201)
        self.assertIn("SHIP-A", view["warehouse_effect"]["intercepting"])
        status, body = self.client.post("/partners/commands",
                                        {"command_id": "intercept-A", "operation": "intercept", "request": {"shipment": "SHIP-A"},
                                         "linked_kind": "shipment", "linked_id": "SHIP-A"}, "warehouse-A")
        self.assertIn(status, (202, 504))
        status, body = self.client.post("/partners/commands/intercept-A/observation", {"state": "pending"}, "warehouse-A")
        self.assertEqual(status, 200)
        status, ship = self.client.get("/warehouse/shipments/SHIP-A", "warehouse-A")
        self.assertEqual(ship["phase"], "intercepting")
        self.assertFalse(ship["intercept_confirmed"])
        status, body = self.client.post("/partners/commands/intercept-A/observation",
                                        {"state": "not_intercepted", "carrier_revision": 3, "picked_up_at": "2026-12-05T09:58:00+08:00"},
                                        "warehouse-A")
        status, ship = self.client.get("/warehouse/shipments/SHIP-A", "warehouse-A")
        self.assertFalse(ship["intercept_confirmed"])
        status, item = self.client.get("/warehouse/items/item-A", "warehouse-A")
        self.assertEqual(item["current_outcome"], "intercept_failed")
        # 质量处置仍在。
        self.assertIn("disp-A", item["restrictions"])
        # 只有承运人明确拦截成功才确认，但仍不回可售库存。
        status, body = self.client.post("/partners/commands/intercept-A/observation", {"state": "intercepted", "carrier_revision": 4}, "warehouse-A")
        status, ship = self.client.get("/warehouse/shipments/SHIP-A", "warehouse-A")
        self.assertTrue(ship["intercept_confirmed"])
        status, item = self.client.get("/warehouse/items/item-A", "warehouse-A")
        self.assertEqual(item["current_outcome"], "quarantine")
        self.assertEqual(item["state"], "intercepted")

    def test_stale_carrier_revision_does_not_clear_disposition(self):
        self.handover("SHIP-A", "item-A")
        self.disposition(id="disp-A", type="stop_supply", batches=["batch-A"])
        status, body = self.client.post("/partners/commands",
                                        {"command_id": "intercept-A", "operation": "intercept", "request": {"shipment": "SHIP-A"},
                                         "linked_kind": "shipment", "linked_id": "SHIP-A"}, "warehouse-A")
        self.assertIn(status, (202, 504))
        self.client.post("/partners/commands/intercept-A/observation", {"state": "intercepted", "carrier_revision": 5}, "warehouse-A")
        status, body = self.client.post("/partners/commands/intercept-A/observation",
                                        {"state": "not_intercepted", "carrier_revision": 2}, "warehouse-A")
        self.assertEqual(status, 200)
        self.assertTrue(body["applied"]["stale"])
        status, ship = self.client.get("/warehouse/shipments/SHIP-A", "warehouse-A")
        self.assertEqual(ship["phase"], "intercepted")
        status, item = self.client.get("/warehouse/items/item-A", "warehouse-A")
        self.assertIn("disp-A", item["restrictions"])

    def test_returned_item_not_resold_after_refund(self):
        status, payment = self.client.post("/payments", {
            "id": "pay-B", "order": "order-A", "item": "item-B", "kind": "refund", "amount": 8900,
            "complaint": "complaint-A", "basis": ["report-B"], "reason": "检测确认不合格",
        }, "reviewer-A")
        self.assertEqual(status, 201, payment)
        self.disposition(id="disp-B", type="not_conforming", report="report-B")
        status, ret = self.client.post("/warehouse/returns",
                                       {"id": "ret-B2", "item": "item-B", "order": "order-A", "condition": "used",
                                        "complaint": "complaint-A"}, "warehouse-A")
        self.assertEqual(status, 201)
        self.assertEqual(ret["warehouse_outcome"], "quarantine")
        self.assertFalse(ret["may_resell"])
        status, body = self.client.post("/warehouse/returns/ret-B2/decide",
                                        {"outcome": "resell", "reason": "已退款可再售"}, "warehouse-A")
        self.assertEqual(status, 409)
        status, ret = self.client.get("/warehouse/returns?item=item-B", "warehouse-A")
        self.assertEqual(ret["returns"][0]["warehouse_outcome"], "quarantine")


class AftercareFlow(ServiceBase):
    def _confirm_scope(self):
        status, body = self.client.post("/complaints/complaint-A/confirm-scope", {"batches": ["batch-B"]}, "quality-A")
        self.assertEqual(status, 200, body)
        return body

    def test_merchant_choice_not_consent(self):
        self._confirm_scope()
        status, body = self.client.post("/complaints/complaint-A/choices",
                                        {"id": "choice-merchant", "item": "item-B", "choice": "exchange"}, "merchant-A")
        self.assertEqual(status, 201)
        choice = next(c for c in body["choices"] if c["id"] == "choice-merchant")
        self.assertEqual(choice["confirmed"], 0)
        status, body = self.client.post("/complaints/complaint-A/exchanges",
                                        {"item": "item-B", "replacement_item": "item-C", "choice": "choice-merchant"}, "reviewer-A")
        self.assertEqual(status, 409)
        self.assertIn(body["error"]["code"], {"choice_required", "consent_required"})
        status, inv = self.client.get("/warehouse/items/item-C", "warehouse-A")
        self.assertIsNone(inv["current_outcome"])
        # 顾客本人确认后可以保留另一批次库存。
        status, body = self.client.post("/complaints/complaint-A/choices",
                                        {"id": "choice-cust", "item": "item-B", "choice": "exchange"}, "customer-A")
        status, body = self.client.post("/complaints/complaint-A/exchanges",
                                        {"item": "item-B", "replacement_item": "item-C", "choice": "choice-cust",
                                         "fulfillment_id": "ful-1"}, "reviewer-A")
        self.assertEqual(status, 201, body)
        status, inv = self.client.get("/warehouse/items/item-C", "warehouse-A")
        self.assertEqual(inv["current_outcome"], "exchange_reserved")

    def test_reserved_exchange_hits_new_stop_supply(self):
        self._confirm_scope()
        self.client.post("/complaints/complaint-A/choices",
                         {"id": "choice-cust", "item": "item-B", "choice": "exchange"}, "customer-A")
        self.client.post("/complaints/complaint-A/exchanges",
                         {"item": "item-B", "replacement_item": "item-C", "choice": "choice-cust", "fulfillment_id": "ful-1"}, "reviewer-A")
        status, view = self.disposition(id="disp-C", type="stop_supply", report="report-C")
        self.assertEqual(status, 201)
        self.assertIn("ful-1", view["warehouse_effect"]["fulfillments_reprocessing"])
        status, complaint = self.client.get("/complaints/complaint-A", "reviewer-A")
        ful = complaint["fulfillments"][0]
        self.assertEqual(ful["status"], "needs_reprocessing")
        # 原退款权利仍可行使，不因换货预约而消失。
        status, payment = self.client.post("/payments", {
            "id": "pay-refund", "order": "order-A", "item": "item-B", "kind": "refund", "amount": 8900,
            "complaint": "complaint-A", "basis": ["report-B"], "reason": "顾客改为退款",
        }, "reviewer-A")
        self.assertEqual(status, 201, payment)

    def test_item_level_resolution_keeps_order_open(self):
        self.client.post("/complaints/complaint-A/targets", {"item": "item-A", "basis": "同订单另件需核查"}, "merchant-A")
        status, body = self.client.post("/complaints/complaint-A/resolve-item",
                                        {"item": "item-A", "reason": "非本批次问题"}, "reviewer-A")
        self.assertEqual(status, 200)
        self.assertNotEqual(body["status"], "resolved")
        status, body = self.client.post("/complaints/complaint-A/resolve-item",
                                        {"item": "item-B", "reason": "处理完毕"}, "reviewer-A")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "resolved")

    def test_notified_requires_confirmed_scope(self):
        status, body = self.client.post("/complaints/complaint-unknown/notify", {}, "reviewer-A")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "insufficient_scope")


class PaymentRules(ServiceBase):
    def test_unknown_payment_blocks_double_payment(self):
        status, payment = self.client.post("/payments", {
            "id": "pay-1", "order": "order-A", "item": "item-B", "kind": "refund", "amount": 8900,
            "complaint": "complaint-A", "basis": ["report-B"], "reason": "确认不合格", "not_returned": True,
            "item_disposition": "顾客自行处置，已告知", "customer_notice": "已短信告知",
        }, "reviewer-A")
        self.assertEqual(status, 201, payment)
        self.assertEqual(payment["state"], "pending_payment")
        status, body = self.client.post("/payments", {
            "id": "pay-2", "order": "order-A", "item": "item-B", "kind": "exchange_refund", "amount": 8900,
            "complaint": "complaint-A", "basis": ["report-B"], "reason": "换货退款", "not_returned": True,
            "item_disposition": "顾客自行处置，已告知", "customer_notice": "已短信告知",
        }, "reviewer-A")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "duplicate_payment")
        status, body = self.client.post("/payments/pay-1/submit", {"command_id": "refund-A"}, "reviewer-A")
        self.assertEqual(status, 200)
        status, body = self.client.post("/partners/commands/refund-A/observation", {"state": "pending"}, "reviewer-A")
        status, viewer = self.client.get("/payments/pay-1", "reviewer-A")
        self.assertEqual(viewer["state"], "pending_payment")
        status, body = self.client.post("/partners/commands/refund-A/observation",
                                        {"state": "paid", "amount": 8900, "voucher": "REF-1"}, "reviewer-A")
        status, viewer = self.client.get("/payments/pay-1", "reviewer-A")
        self.assertEqual(viewer["state"], "paid")
        self.assertEqual(viewer["voucher"], "REF-1")

    def test_cannot_exceed_transaction(self):
        status, body = self.client.post("/payments", {
            "id": "pay-x", "order": "order-A", "item": "item-B", "kind": "refund", "amount": 9000,
            "complaint": "complaint-A", "basis": ["report-B"], "reason": "多退", "not_returned": True,
            "item_disposition": "顾客留用", "customer_notice": "已告知",
        }, "reviewer-A")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "exceeds_transaction")

    def test_not_returned_requires_disclosure(self):
        # item-A 没有退货记录，必须写明商品去向与顾客告知。
        status, body = self.client.post("/payments", {
            "id": "pay-y", "order": "order-A", "item": "item-A", "kind": "refund", "amount": 8900,
            "complaint": "complaint-A", "basis": ["report-B"], "reason": "未退回", "not_returned": True,
        }, "reviewer-A")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_argument")
        status, payments = self.client.get("/payments?item=item-A", "reviewer-A")
        self.assertEqual(payments["payments"], [])


class CorrectionAndRestart(ServiceBase):
    def test_correction_lists_actions_without_clawback(self):
        # 已结案且已赔付款的案件：更正后保留原决定，列出通知与补付复核，不自动扣回。
        status, _ = self.client.post("/payments", {
            "id": "pay-closed", "order": "order-A", "item": "item-B", "kind": "refund", "amount": 8900,
            "complaint": "complaint-A", "basis": ["report-B"], "reason": "确认不合格", "not_returned": True,
            "item_disposition": "顾客留用", "customer_notice": "已告知",
        }, "reviewer-A")
        self.assertEqual(status, 201)
        status, _ = self.client.post("/payments/pay-closed/submit", {"command_id": "refund-A"}, "reviewer-A")
        self.assertEqual(status, 200)
        status, _ = self.client.post("/partners/commands/refund-A/observation",
                                     {"state": "paid", "amount": 8900, "voucher": "REF-1"}, "reviewer-A")
        self.assertEqual(status, 200)
        self.client.post("/complaints/complaint-A/resolve-item", {"item": "item-A", "reason": "不涉及"}, "reviewer-A")
        status, _ = self.client.post("/complaints/complaint-A/resolve-item", {"item": "item-B", "reason": "已赔"}, "reviewer-A")
        self.assertEqual(status, 200)
        status, body = self.client.post("/corrections", {"old_report": "report-B", "note": "口径更正"}, "quality-A")
        self.assertEqual(status, 201, body)
        self.assertTrue(body["policy"]["accepted_refunds_are_not_clawed_back"])
        self.assertEqual(body["affected_batches"], ["batch-B"])
        types = {a["type"] for a in body["actions"]}
        self.assertIn("notify_customer_of_correction", types)
        self.assertIn("review_shortfall_or_top_up", types)
        top_up = next(a for a in body["actions"] if a["type"] == "review_shortfall_or_top_up")
        self.assertTrue(top_up["do_not_auto_clawback"])
        # 原结案决定仍可查。
        status, complaint = self.client.get("/complaints/complaint-A", "reviewer-A")
        self.assertEqual(complaint["status"], "resolved")

    def test_state_survives_restart(self):
        status, _ = self.disposition(id="disp-persist", type="stop_supply", report="report-B")
        self.assertEqual(status, 201)
        status, payment = self.client.post("/payments", {
            "id": "pay-persist", "order": "order-A", "item": "item-B", "kind": "refund", "amount": 8900,
            "complaint": "complaint-A", "basis": ["report-B"], "reason": "确认不合格", "not_returned": True,
            "item_disposition": "顾客留用", "customer_notice": "已告知",
        }, "reviewer-A")
        self.assertEqual(status, 201)
        self.restart()
        status, disp = self.client.get("/dispositions/disp-persist", "quality-A")
        self.assertEqual(status, 200)
        self.assertEqual(disp["status"], "active")
        status, payment = self.client.get("/payments/pay-persist", "reviewer-A")
        self.assertEqual(payment["state"], "pending_payment")
        status, item = self.client.get("/warehouse/items/item-B", "warehouse-A")
        self.assertIn("disp-persist", item["restrictions"])
        status, complaint = self.client.get("/complaints/complaint-A", "reviewer-A")
        self.assertTrue(complaint["item_explanations"])

    def test_idempotency_does_not_duplicate(self):
        status, body = self.client.post("/complaints", {"id": "complaint-new", "order": "order-A"}, "customer-A",
                                        key="idem-1")
        self.assertEqual(status, 201)
        status, again = self.client.post("/complaints", {"id": "complaint-new", "order": "order-A"}, "customer-A",
                                         key="idem-1")
        self.assertEqual(status, 201)
        self.assertTrue(again.get("idempotent_replay"))
        self.assertEqual(again["id"], "complaint-new")

    def test_roles_cannot_submit_quality_conclusions(self):
        status, body = self.client.post("/dispositions", {"id": "d1", "type": "not_conforming", "reason": "x",
                                                          "batches": ["batch-B"]}, "reviewer-A")
        self.assertEqual(status, 403)
        status, body = self.client.post("/payments", {
            "id": "p1", "order": "order-A", "item": "item-B", "kind": "refund", "amount": 100,
            "basis": ["report-B"], "reason": "x", "not_returned": True,
            "item_disposition": "留用", "customer_notice": "已知",
        }, "merchant-A")
        self.assertEqual(status, 403)


class BatchRelationsAndLivePartner(ServiceBase):
    extra_materials = [
        {"id": "batch-D", "kind": "batch", "refs": [], "style": "dress-2", "color": "red", "supplier": "supplier-A"},
        {"id": "item-D", "kind": "garment", "refs": ["batch-D"], "serial": "cloth-D"},
        {"id": "item-E", "kind": "garment", "refs": ["batch-D"], "serial": "cloth-E", "state": "picked"},
    ]

    def test_mapping_expands_report_scope(self):
        status, body = self.client.post("/batch-mappings", {
            "parent_batch": "batch-B", "child_batch": "batch-D", "evidence_ref": "inbound-77",
            "note": "同一次进货分装"}, "warehouse-A")
        self.assertEqual(status, 201, body)
        status, body = self.client.post("/batch-mappings", {
            "parent_batch": "batch-D", "child_batch": "batch-B", "evidence_ref": "inbound-77"}, "warehouse-A")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_mapping")
        status, mapping = self.client.post("/batch-mappings", {
            "parent_batch": "batch-B", "child_batch": "batch-D"}, "warehouse-A")
        self.assertEqual(status, 400)
        status, impact = self.client.get("/reports/report-B/impact?include_mapped=1", "quality-A")
        self.assertEqual(status, 200)
        self.assertEqual(impact["batches"], ["batch-B", "batch-D"])
        status, impact_plain = self.client.get("/reports/report-B/impact", "quality-A")
        self.assertEqual(impact_plain["batches"], ["batch-B"])
        # 供应商扩展只并入同供应商批次；batch-D 的供应商不同，不会因同款并入。
        status, by_supplier = self.client.get("/reports/report-B/impact?include_supplied=1", "quality-A")
        self.assertEqual(by_supplier["batches"], ["batch-B"])
        status, supplier_scope = self.client.post("/dispositions", {
            "id": "disp-supplier", "type": "investigate", "report": "report-B",
            "reason": "同供应商进货排查", "include_supplied": True}, "quality-A")
        self.assertEqual(status, 201, supplier_scope)
        self.assertEqual(supplier_scope["effective_batch_ids"], ["batch-B"])
        status, view = self.disposition(id="disp-map", type="stop_supply", report="report-B", include_mapped=True)
        self.assertEqual(status, 201)
        self.assertIn("item-D", view["warehouse_effect"]["quarantined"])
        self.assertIn("item-E", view["warehouse_effect"]["blocked_picked"])
        status, repack = self.client.get("/repacks/box-A", "warehouse-A")
        self.assertEqual(repack["batch_ids"], ["batch-A", "batch-B"])
        self.assertTrue(repack["mixed_batches"])

    def test_live_partner_observations_advance_payment(self):
        import os
        partner = os.path.join(str(ROOT), "partner", "server.py")
        import subprocess
        state = str(Path(self.temp.name) / "partner.sqlite3")
        process = subprocess.Popen([sys.executable, partner, "--port", "0", "--state", state],
                                   stdout=subprocess.PIPE, text=True)
        try:
            port = json.loads(process.stdout.readline())["port"]
            self.stop()
            store = Store(self.state)
            store.initialize()
            materials.import_catalog(store, self.materials_path, actor_id="system:seed")
            self.server = build_server(store, port=0, partner_url="http://127.0.0.1:" + str(port))
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.client = Client("http://127.0.0.1:" + str(self.server.server_address[1]))
            status, payment = self.client.post("/payments", {
                "id": "pay-live", "order": "order-A", "item": "item-B", "kind": "refund", "amount": 8900,
                "complaint": "complaint-A", "basis": ["report-B"], "reason": "确认不合格", "not_returned": True,
                "item_disposition": "顾客留用", "customer_notice": "已告知",
            }, "reviewer-A")
            self.assertEqual(status, 201)
            status, _ = self.client.post("/payments/pay-live/submit", {"command_id": "refund-A"}, "reviewer-A")
            self.assertEqual(status, 200)
            status, first = self.client.get("/partners/commands/refund-A", "reviewer-A")
            self.assertEqual(status, 200)
            self.assertEqual(first["command"]["first_status"], 504)
            self.assertEqual(first["observation"]["state"], "pending")
            status, viewer = self.client.get("/payments/pay-live", "reviewer-A")
            self.assertEqual(viewer["state"], "pending_payment")
            status, second = self.client.get("/partners/commands/refund-A", "reviewer-A")
            self.assertEqual(second["observation"]["state"], "paid")
            status, viewer = self.client.get("/payments/pay-live", "reviewer-A")
            self.assertEqual(viewer["state"], "paid")
            # 重启后仍能读到已付事实；重复读取不重复应用。
            self.restart()
            status, viewer = self.client.get("/payments/pay-live", "reviewer-A")
            self.assertEqual(viewer["state"], "paid")
            status, again = self.client.get("/partners/commands/refund-A", "reviewer-A")
            self.assertTrue(again["repeated"])
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()


class CustomerAndIssueRules(ServiceBase):
    def test_consumer_cannot_confirm_other_customers_choice(self):
        status, body = self.client.post("/complaints/complaint-A/choices",
                                        {"id": "choice-x", "item": "item-B", "choice": "exchange"}, "customer-B")
        self.assertIn(status, (401, 403))
        status, complaint = self.client.get("/complaints/complaint-A", "customer-A")
        self.assertFalse(any(c["id"] == "choice-x" for c in complaint["choices"]))

    def test_refund_choice_creates_manual_work(self):
        status, body = self.client.post("/complaints/complaint-A/choices",
                                        {"id": "choice-refund", "item": "item-B", "choice": "refund"}, "customer-A")
        self.assertEqual(status, 201)
        status, issues = self.client.get("/issues?complaint_id=complaint-A&status=open", "reviewer-A")
        self.assertTrue(any(i["id"] == "issue-choice-choice-refund" for i in issues["issues"]))
        status, explanation = self.client.get("/complaints/complaint-A/items/item-B", "customer-A")
        self.assertEqual(status, 200)
        self.assertTrue(explanation["refund_right_retained"])

    def test_unknown_actor_rejected(self):
        status, body = self.client.get("/complaints", "customer-Z")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unknown_actor")

    def test_role_header_must_match(self):
        status, body = self.client.call("GET", "/complaints", actor="warehouse-A", role="quality")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "role_mismatch")


class HeldReplacementRules(ServiceBase):
    def test_held_replacement_after_stop_requires_reprocessing_and_keeps_refund(self):
        self.client.post("/complaints/complaint-A/confirm-scope", {"batches": ["batch-B"]}, "quality-A")
        self.client.post("/complaints/complaint-A/choices",
                         {"id": "choice-ok", "item": "item-B", "choice": "exchange"}, "customer-A")
        self.client.post("/complaints/complaint-A/exchanges",
                         {"item": "item-B", "replacement_item": "item-C", "choice": "choice-ok",
                          "fulfillment_id": "ful-x"}, "reviewer-A")
        self.disposition(id="disp-c", type="stop_supply", report="report-C")
        status, body = self.client.post("/complaints/complaint-A/exchange-advance",
                                        {"fulfillment": "ful-x", "action": "dispatch"}, "warehouse-A")
        self.assertEqual(status, 409)
        self.assertTrue(body["error"]["detail"]["refund_right_preserved"])
        status, complaint = self.client.get("/complaints/complaint-A", "reviewer-A")
        explanation = next(e for e in complaint["item_explanations"] if e["item"] == "item-B")
        self.assertTrue(explanation["customer_confirmed_exchange"])
        self.assertTrue(any("换货履行待完成" in p for p in explanation["pending"]))
        # 解除后履行恢复，且原保留重新生效。
        status, lifted = self.client.post("/dispositions/disp-c/lift", {"reason": "调查完成"}, "quality-A")
        self.assertEqual(status, 200)
        self.assertIn("ful-x", lifted["resumed_fulfillments"])
        status, complaint = self.client.get("/complaints/complaint-A", "reviewer-A")
        self.assertEqual(complaint["fulfillments"][0]["status"], "reserved")


class ShipmentPhases(ServiceBase):
    extra_materials = [
        {"id": "item-D", "kind": "garment", "refs": ["batch-A"], "serial": "cloth-D", "state": "picked"},
    ]

    def test_pending_shipment_blocked_until_lifted(self):
        status, ship = self.client.post("/warehouse/shipments",
                                        {"id": "SHIP-P", "item": "item-D"}, "warehouse-A")
        self.assertEqual(status, 201, ship)
        self.assertEqual(ship["phase"], "carrier_pending")
        status, view = self.disposition(id="disp-P", type="stop_supply", batches=["batch-A"])
        self.assertIn("SHIP-P", view["warehouse_effect"]["blocked_shipments"])
        status, body = self.client.post("/warehouse/shipments/SHIP-P/handover", {}, "warehouse-A")
        self.assertEqual(status, 409)
        self.client.post("/dispositions/disp-P/lift", {"reason": "复核后解除"}, "quality-A")
        status, ship = self.client.post("/warehouse/shipments/SHIP-P/handover", {}, "warehouse-A")
        self.assertEqual(status, 200)
        self.assertEqual(ship["phase"], "with_carrier")

    def test_accepted_does_not_mean_intercepted(self):
        status, _ = self.client.post("/warehouse/shipments",
                                     {"id": "SHIP-P", "item": "item-D", "hand_over": True}, "warehouse-A")
        self.assertEqual(status, 201)
        self.disposition(id="disp-P", type="stop_supply", batches=["batch-A"])
        self.client.post("/partners/commands", {"command_id": "ic-1", "operation": "intercept",
                                                "request": {}, "linked_kind": "shipment",
                                                "linked_id": "SHIP-P"}, "warehouse-A")
        self.client.post("/partners/commands/ic-1/observation", {"state": "accepted"}, "warehouse-A")
        status, ship = self.client.get("/warehouse/shipments/SHIP-P", "warehouse-A")
        self.assertEqual(ship["phase"], "intercepting")
        self.assertFalse(ship["intercept_confirmed"])
        self.client.post("/partners/commands/ic-1/observation", {"state": "lost"}, "warehouse-A")
        status, item = self.client.get("/warehouse/items/item-D", "warehouse-A")
        self.assertEqual(item["state"], "lost")
        self.assertIn("disp-P", item["restrictions"])
        # 丢失后按运单查询得到迟到揽收记录：记录事实，但不清除处置。
        status, result = self.client.post("/warehouse/shipments/SHIP-P/tracking",
                                          {"state": "in_transit", "carrier_revision": 1,
                                           "picked_up_at": "2026-12-05T09:58:00+08:00"}, "warehouse-A")
        self.assertEqual(status, 200)
        status, item = self.client.get("/warehouse/items/item-D", "warehouse-A")
        self.assertIn("disp-P", item["restrictions"])

    def test_rejected_handover_returns_to_picked(self):
        status, _ = self.client.post("/warehouse/shipments",
                                     {"id": "SHIP-P", "item": "item-D", "hand_over": True}, "warehouse-A")
        self.assertEqual(status, 201)
        self.client.post("/partners/commands", {"command_id": "ex-1", "operation": "ship",
                                                "request": {}, "linked_kind": "shipment",
                                                "linked_id": "SHIP-P"}, "warehouse-A")
        status, body = self.client.post("/partners/commands/ex-1/observation",
                                        {"state": "rejected", "reason": "尚未交运"}, "warehouse-A")
        status, ship = self.client.get("/warehouse/shipments/SHIP-P", "warehouse-A")
        self.assertEqual(ship["phase"], "carrier_pending")
        status, item = self.client.get("/warehouse/items/item-D", "warehouse-A")
        self.assertEqual(item["state"], "picked")


class ReplayAndResources(ServiceBase):
    def test_sequential_replay_from_commands_catalog(self):
        commands = json.loads((ROOT / "partner" / "commands.json").read_text())["commands"]
        intercept = next(c for c in commands if c["id"] == "intercept-A")
        self.handover("SHIP-A", "item-A")
        self.disposition(id="disp-A", type="stop_supply", batches=["batch-A"])
        status, _ = self.client.post("/partners/commands", {
            "command_id": "intercept-A", "operation": intercept["operation"],
            "request": intercept["request"], "linked_kind": "shipment", "linked_id": "SHIP-A"}, "warehouse-A")
        self.assertIn(status, (202, 504))
        status, body = self.client.post("/partners/commands/intercept-A/replay",
                                        {"observations": intercept["observations"]}, "warehouse-A")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["applied"]), 2)
        status, ship = self.client.get("/warehouse/shipments/SHIP-A", "warehouse-A")
        self.assertEqual(ship["phase"], "with_carrier")
        self.assertFalse(ship["intercept_confirmed"])
        self.assertEqual(ship["picked_up_at"], intercept["observations"][-1]["picked_up_at"])
        status, item = self.client.get("/warehouse/items/item-A", "warehouse-A")
        self.assertEqual(item["current_outcome"], "intercept_failed")
        self.assertIn("disp-A", item["restrictions"])

    def test_compact_endpoints_have_expected_shape(self):
        status, body = self.client.get("/orders", "customer-A")
        self.assertEqual(status, 200)
        self.assertTrue(any(o["id"] == "order-A" for o in body["orders"]))
        status, body = self.client.get("/cases", "reviewer-A")
        self.assertEqual(status, 200)
        self.assertTrue(any(c["id"] == "complaint-A" for c in body["cases"]))
        status, body = self.client.get("/warehouse/stock", "warehouse-A")
        self.assertEqual(status, 200)
        self.assertIn("counts", body)
        status, body = self.client.get("/batches", "warehouse-A")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["batches"]), 3)

    def test_replacement_must_come_from_safe_batch(self):
        self.client.post("/complaints/complaint-A/confirm-scope", {"batches": ["batch-B"]}, "quality-A")
        self.client.post("/complaints/complaint-A/choices",
                         {"id": "choice-ok", "item": "item-B", "choice": "exchange"}, "customer-A")
        status, body = self.client.post("/complaints/complaint-A/exchanges",
                                        {"item": "item-B", "replacement_item": "item-B", "choice": "choice-ok"},
                                        "reviewer-A")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "unsafe_replacement")


class OrderOnlyComplaint(ServiceBase):
    def test_order_only_complaint_lists_lines_separately(self):
        status, complaint = self.client.post("/complaints", {
            "id": "complaint-order-only", "order": "order-A",
            "description": "两件都刺痒，先按订单登记"}, "customer-A")
        self.assertEqual(status, 201, complaint)
        self.assertEqual({t["item_id"] for t in complaint["targets"]}, {"item-A", "item-B"})
        self.assertEqual(complaint["status"], "investigating")
        self.assertTrue(complaint["confirmed_scope"]["has_unconfirmed_batch"])
        # 没有质量确认前仍不能据此停供。
        status, body = self.client.post("/dispositions", {
            "id": "disp-order", "type": "stop_supply", "reason": "仅凭订单",
            "complaint": "complaint-order-only"}, "quality-A")
        self.assertEqual(status, 400)
        status, body = self.client.post("/complaints/complaint-order-only/resolve-item",
                                        {"item": "item-A", "reason": "已单独处理"}, "reviewer-A")
        self.assertEqual(status, 200)
        self.assertNotEqual(body["status"], "resolved")
        self.assertEqual({t["status"] for t in body["targets"]}, {"open", "resolved"})

    def test_consumer_cannot_complain_for_someone_elses_order(self):
        status, body = self.client.post("/complaints", {"id": "complaint-other", "order": "order-A"}, "customer-B")
        self.assertIn(status, (401, 403))


class GlobalScopeRules(ServiceBase):
    def test_all_batches_requires_explicit_quality_decision(self):
        status, body = self.client.post("/dispositions", {
            "id": "disp-all", "type": "investigate", "reason": "全批次排查", "all_batches": True}, "quality-A")
        self.assertEqual(status, 201, body)
        self.assertEqual(body["effective_batch_ids"], ["batch-A", "batch-B", "batch-C"])
        self.assertEqual(len(body["warehouse_effect"]["quarantined"]), 3)
        status, lifted = self.client.post("/dispositions/disp-all/lift", {"reason": "排查结束"}, "quality-A")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(lifted["restored_items"]), ["item-A", "item-B", "item-C"])
        for batch in ("batch-A", "batch-B", "batch-C"):
            self.assertEqual(lifted["remaining_restrictions"][batch], [])

    def test_style_plus_color_does_not_claim_other_colors(self):
        status, body = self.client.post("/dispositions", {
            "id": "disp-color", "type": "investigate", "reason": "同色排查",
            "style": "dress-1", "color": "blue"}, "quality-A")
        self.assertEqual(status, 201, body)
        self.assertEqual(body["effective_batch_ids"], ["batch-A", "batch-B", "batch-C"])
        status, body = self.client.post("/dispositions", {
            "id": "disp-color2", "type": "investigate", "reason": "缺色号",
            "style": "dress-1"}, "quality-A")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "insufficient_scope")


if __name__ == "__main__":
    unittest.main()
