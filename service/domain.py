"""领域对象、状态常量与派生视图。

这里只做确定性的数据整理：批次归属、处置覆盖关系、实物状态解释。
所有金额以整数分保存，不进行四舍五入。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1

# 实物状态：说明一件衣服在仓库/承运方链路中的位置。
ITEM_STATES = {
    "in_stock",       # 在库可分配
    "picked",         # 已分拣、未交承运人
    "shipped",        # 已记录出库、在途
    "intercepted",    # 承运人确认拦截成功（仍需按批次处置另行判断是否可售）
    "returned",       # 退货已实际接收
    "lost",           # 承运人报告丢失
}

# 实物去向（回报/分配结果），与实物状态分开保存。
ITEM_OUTCOMES = {
    "quarantine",      # 隔离，不得分配或交运
    "intercepting",    # 已交承运人，拦截待查
    "intercept_failed",
    "intercept_succeeded",
    "resell",          # 仅当批次没有有效限制时才允许
    "scrap",
    "awaiting_disposition",
    "exchange_reserved",
}

DISPOSITION_TYPES = {
    "stop_supply": "停供",
    "investigate": "扩大调查",
    "not_conforming": "不合格",
    "released": "解除处置",
}

DISPOSITION_STATES = {"active", "lifted", "revealed", "withdrawn"}

NEGATIVE_TYPES = {"stop_supply", "investigate", "not_conforming"}

COMPLAINT_STATUSES = {
    "open",              # 已受理，线索不足以确定批次
    "investigating",     # 已关联订单或指定批次，待质量确认
    "notified",          # 已按确认范围通知顾客
    "choice_made",       # 顾客已选择
    "in_fulfillment",    # 换货履行中
    "pending_payment",   # 赔付待复核/待协作方确认
    "resolved",          # 该分项已结案
}

CASE_STATUSES = {"open", "closed"}

SHIPMENT_PHASES = {"dispatch_pending", "carrier_pending", "with_carrier", "intercepting", "intercepted",
                    "delivered", "returned", "cancelled", "lost"}

# 对"未完成"状态的解释：供一件商品视图直接展示待办。
PENDING_LABELS = {
    "open": "投诉已受理，等待补证或按线索定位批次",
    "investigating": "等待质量负责人确认范围",
    "notified": "已通知，等待顾客选择",
    "choice_made": "顾客已选择，等待履行",
    "in_fulfillment": "换货履行中",
    "pending_payment": "赔付待复核或等待协作方确认",
    "resolved": "该件已结案",
}

PAYMENT_STATES = {"no_payment", "pending_payment", "paid", "reversed"}

ISSUE_KINDS = {"quality_dispute", "refund_shortfall", "exchange_entitlement", "manual_followup", "duplicate_payment_review"}


class DomainError(Exception):
    """可预期的业务规则拒绝。调用方应返回 400/409 而不是 500。"""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        value = {"code": self.code, "message": self.message}
        if self.detail:
            value["detail"] = self.detail
        return value


def utcnow() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v is not None and str(v).strip() != ""]
    raise DomainError("invalid_argument", "期望列表或单个标识", {"value": value})


def require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError("invalid_argument", "字段不能为空", {"field": field})
    return value.strip()


def require_int(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainError("invalid_argument", "字段必须是整数分", {"field": field, "value": value})
    if value < minimum:
        raise DomainError("invalid_argument", "金额不能为负", {"field": field, "value": value})
    return value


def find_batch_cycle(mappings: list[dict[str, Any]]) -> list[str] | None:
    """检测子批次/分装关系中的环，返回环上的批次链。"""
    graph: dict[str, list[str]] = {}
    for row in mappings:
        parent, child = row.get("parent_batch"), row.get("child_batch")
        if parent and child:
            graph.setdefault(parent, []).append(child)
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> list[str] | None:
        if node in visiting:
            return visiting[visiting.index(node):] + [node]
        if node in done:
            return None
        visiting.append(node)
        for nxt in graph.get(node, []):
            cycle = visit(nxt)
            if cycle:
                return cycle
        visiting.pop()
        done.add(node)
        return None

    for node in list(graph):
        cycle = visit(node)
        if cycle:
            return cycle
    return None


def normalize_complaint_clue(payload: dict[str, Any]) -> dict[str, Any]:
    """投诉线索可以来自订单，也可以只有吊牌、商家与购买时间。"""
    order = payload.get("order")
    items = payload.get("items")
    if items is None:
        items = as_list(payload.get("item")) + as_list(payload.get("items"))
        items = list(dict.fromkeys(items))
    serials = list(dict.fromkeys(as_list(payload.get("serials")) + as_list(payload.get("serial"))))
    tags = list(dict.fromkeys(as_list(payload.get("tags")) + as_list(payload.get("tag"))))
    clues = {
        "order": order.strip() if isinstance(order, str) else order,
        "style": payload.get("style"),
        "color": payload.get("color"),
        "shop": payload.get("shop"),
        "purchase_date": payload.get("purchase_date"),
        "items": items,
        "serials": serials,
        "tags": tags,
        "batch": payload.get("batch") or None,
        "description": payload.get("description"),
        "reporter": payload.get("reporter"),
    }
    if not order and not items and not serials and not tags and not (clues["style"] or clues["shop"] or clues["purchase_date"]):
        raise DomainError("invalid_argument", "投诉至少需要订单、商品线索或吊牌/商家/购买时间之一")
    if order and not isinstance(order, str):
        raise DomainError("invalid_argument", "order 必须是订单编号")
    return clues
