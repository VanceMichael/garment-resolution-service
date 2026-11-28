"""SQLite 持久化。

设计要点：
- 业务记录只追加，投诉原始线索与检测报告原文以摘要保存并可核验；
- 状态迁移写事件表，重启后可从表内重建；
- 不依赖内存缓存，因此同一状态文件重启后查询结果一致。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS principals(
  id TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  label TEXT
);

CREATE TABLE IF NOT EXISTS batches(
  id TEXT PRIMARY KEY,
  style TEXT NOT NULL,
  color TEXT NOT NULL,
  supplier TEXT,
  source TEXT
);

CREATE TABLE IF NOT EXISTS garments(
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES batches(id),
  serial TEXT,
  state TEXT NOT NULL DEFAULT 'in_stock',
  location TEXT,
  current_outcome TEXT,
  outcome_reason TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS orders(
  id TEXT PRIMARY KEY,
  customer TEXT,
  shop TEXT,
  placed_at TEXT
);

CREATE TABLE IF NOT EXISTS order_meta(
  order_id TEXT PRIMARY KEY REFERENCES orders(id),
  data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_lines(
  order_id TEXT NOT NULL REFERENCES orders(id),
  item_id TEXT NOT NULL REFERENCES garments(id),
  paid INTEGER NOT NULL,
  PRIMARY KEY(order_id, item_id)
);

CREATE TABLE IF NOT EXISTS batch_mappings(
  id TEXT PRIMARY KEY,
  parent_batch TEXT NOT NULL REFERENCES batches(id),
  child_batch TEXT NOT NULL REFERENCES batches(id),
  evidence_ref TEXT,
  note TEXT,
  created_by TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS repacks(
  id TEXT PRIMARY KEY,
  from_box TEXT,
  to_box TEXT,
  items_json TEXT NOT NULL,
  evidence_ref TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS reports(
  id TEXT PRIMARY KEY,
  batch_id TEXT REFERENCES batches(id),
  scope_json TEXT NOT NULL DEFAULT '{}',
  conclusion TEXT NOT NULL,
  digest TEXT NOT NULL,
  signed_by TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  supersedes TEXT REFERENCES reports(id),
  corrects TEXT REFERENCES reports(id),
  correction_reason TEXT,
  withdrawn_by TEXT,
  withdrawn_at TEXT,
  withdraw_reason TEXT,
  source_document TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_batches(
  report_id TEXT NOT NULL REFERENCES reports(id),
  batch_id TEXT NOT NULL REFERENCES batches(id),
  basis TEXT NOT NULL,
  PRIMARY KEY(report_id, batch_id)
);

CREATE TABLE IF NOT EXISTS dispositions(
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  scope_json TEXT NOT NULL DEFAULT '{}',
  basis_report TEXT REFERENCES reports(id),
  reason TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  lifted_by TEXT,
  lifted_at TEXT,
  lift_reason TEXT,
  lift_disposition TEXT,
  basis_retracted INTEGER NOT NULL DEFAULT 0,
  basis_retracted_at TEXT,
  basis_retracted_note TEXT,
  supersedes TEXT
);

CREATE TABLE IF NOT EXISTS disposition_batches(
  disposition_id TEXT NOT NULL REFERENCES dispositions(id),
  batch_id TEXT NOT NULL REFERENCES batches(id),
  basis TEXT NOT NULL,
  PRIMARY KEY(disposition_id, batch_id)
);

CREATE TABLE IF NOT EXISTS complaints(
  id TEXT PRIMARY KEY,
  order_id TEXT REFERENCES orders(id),
  item_id TEXT REFERENCES garments(id),
  clues_json TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  description TEXT,
  reporter TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT,
  close_reason TEXT
);

CREATE TABLE IF NOT EXISTS complaint_targets(
  complaint_id TEXT NOT NULL REFERENCES complaints(id),
  item_id TEXT NOT NULL REFERENCES garments(id),
  batch_id TEXT REFERENCES batches(id),
  basis TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  resolution TEXT,
  resolved_at TEXT,
  added_by TEXT,
  added_at TEXT,
  PRIMARY KEY(complaint_id, item_id)
);

CREATE TABLE IF NOT EXISTS complaint_scopes(
  id TEXT PRIMARY KEY,
  complaint_id TEXT NOT NULL REFERENCES complaints(id),
  batch_id TEXT NOT NULL REFERENCES batches(id),
  basis TEXT NOT NULL,
  confirmed_by TEXT NOT NULL,
  confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS complaint_inputs(
  id TEXT PRIMARY KEY,
  complaint_id TEXT NOT NULL REFERENCES complaints(id),
  payload_json TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  submitted_by TEXT NOT NULL,
  submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence(
  id TEXT PRIMARY KEY,
  complaint_id TEXT NOT NULL REFERENCES complaints(id),
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  batch_id TEXT REFERENCES batches(id),
  submitted_by TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS notifications(
  id TEXT PRIMARY KEY,
  complaint_id TEXT NOT NULL REFERENCES complaints(id),
  item_id TEXT REFERENCES garments(id),
  order_id TEXT REFERENCES orders(id),
  scope TEXT NOT NULL,
  batch_ids_json TEXT NOT NULL,
  message TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS choices(
  id TEXT PRIMARY KEY,
  complaint_id TEXT NOT NULL REFERENCES complaints(id),
  item_id TEXT NOT NULL REFERENCES garments(id),
  choice TEXT NOT NULL,
  confirmed INTEGER NOT NULL,
  confirmed_by TEXT,
  confirmed_at TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS fulfillments(
  id TEXT PRIMARY KEY,
  complaint_id TEXT NOT NULL REFERENCES complaints(id),
  item_id TEXT NOT NULL REFERENCES garments(id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  reserved_item TEXT REFERENCES garments(id),
  replacement_item TEXT REFERENCES garments(id),
  shipment_id TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS returns(
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES garments(id),
  order_id TEXT REFERENCES orders(id),
  complaint_id TEXT REFERENCES complaints(id),
  condition TEXT NOT NULL,
  received_by TEXT NOT NULL,
  received_at TEXT NOT NULL,
  warehouse_outcome TEXT NOT NULL,
  outcome_reason TEXT NOT NULL,
  restricted_batch INTEGER NOT NULL DEFAULT 0,
  disposition_basis_json TEXT NOT NULL DEFAULT '[]',
  reviewed_by TEXT,
  reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS shipments(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  order_id TEXT REFERENCES orders(id),
  item_id TEXT NOT NULL REFERENCES garments(id),
  batch_id TEXT NOT NULL REFERENCES batches(id),
  carrier TEXT,
  tracking TEXT,
  phase TEXT NOT NULL,
  command_id TEXT,
  picked_up_at TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  blocked_reason TEXT
);

CREATE TABLE IF NOT EXISTS shipment_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shipment_id TEXT NOT NULL REFERENCES shipments(id),
  state TEXT NOT NULL,
  carrier_revision INTEGER,
  picked_up_at TEXT,
  source TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partners(
  command_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  request_json TEXT NOT NULL,
  linked_kind TEXT,
  linked_id TEXT,
  first_status INTEGER,
  state TEXT NOT NULL,
  response_json TEXT,
  last_observation_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments(
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(id),
  item_id TEXT NOT NULL REFERENCES garments(id),
  complaint_id TEXT REFERENCES complaints(id),
  kind TEXT NOT NULL,
  amount INTEGER NOT NULL,
  state TEXT NOT NULL,
  payment_command_id TEXT,
  reservation_id TEXT,
  voucher TEXT,
  reviewed_by TEXT,
  reviewed_at TEXT,
  review_note TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_links(
  id TEXT PRIMARY KEY,
  payment_id TEXT NOT NULL REFERENCES payments(id),
  kind TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations(
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES garments(id),
  complaint_id TEXT NOT NULL REFERENCES complaints(id),
  order_id TEXT REFERENCES orders(id),
  status TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS issues(
  id TEXT PRIMARY KEY,
  complaint_id TEXT REFERENCES complaints(id),
  item_id TEXT REFERENCES garments(id),
  order_id TEXT REFERENCES orders(id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_by TEXT,
  resolved_at TEXT,
  resolution TEXT
);

CREATE TABLE IF NOT EXISTS decision_records(
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  kind TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  prior_decision_id TEXT REFERENCES decision_records(id),
  scope_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrections(
  id TEXT PRIMARY KEY,
  report_id TEXT NOT NULL REFERENCES reports(id),
  old_report_id TEXT NOT NULL REFERENCES reports(id),
  status TEXT NOT NULL,
  affected_decision_ids_json TEXT NOT NULL DEFAULT '[]',
  actions_json TEXT NOT NULL DEFAULT '[]',
  note TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  complaint_id TEXT REFERENCES complaints(id),
  item_id TEXT REFERENCES garments(id),
  order_id TEXT REFERENCES orders(id),
  action TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  actor TEXT NOT NULL,
  role TEXT NOT NULL,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT,
  role TEXT,
  action TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS idempotency(
  key TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  status INTEGER NOT NULL,
  body_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_garments_batch ON garments(batch_id);
CREATE INDEX IF NOT EXISTS idx_order_lines_item ON order_lines(item_id);
CREATE INDEX IF NOT EXISTS idx_dispositions_type ON dispositions(type, status);
CREATE INDEX IF NOT EXISTS idx_shipments_item ON shipments(item_id);
CREATE INDEX IF NOT EXISTS idx_complaints_order ON complaints(order_id);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id, item_id);
CREATE INDEX IF NOT EXISTS idx_report_batches_batch ON report_batches(batch_id);
CREATE INDEX IF NOT EXISTS idx_disposition_batches_batch ON disposition_batches(batch_id);
CREATE INDEX IF NOT EXISTS idx_complaint_targets_batch ON complaint_targets(batch_id);
CREATE INDEX IF NOT EXISTS idx_returns_item ON returns(item_id);
"""


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class Store:
    """线程内复用连接；事务可嵌套，外层提交，内层不提前结束。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._memory_connection: sqlite3.Connection | None = None
        self._memory_lock = threading.RLock()

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=20000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")

    def raw_connect(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            with self._memory_lock:
                if self._memory_connection is None:
                    conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=20)
                    self._configure(conn)
                    self._memory_connection = conn
                return self._memory_connection
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=20)
            self._configure(conn)
            self._local.conn = conn
        return conn

    def initialize(self) -> None:
        conn = self.raw_connect()
        conn.executescript(SCHEMA)
        self._migrate(conn)
        conn.commit()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """给早期演练状态文件补齐新增列，避免重启后查询失败。"""
        additions = {
            "shipments": {"picked_up_at": "TEXT"},
            "dispositions": {"basis_retracted": "INTEGER NOT NULL DEFAULT 0",
                             "basis_retracted_at": "TEXT",
                             "basis_retracted_note": "TEXT"},
            "reports": {"withdrawn_by": "TEXT", "withdrawn_at": "TEXT", "withdraw_reason": "TEXT"},
            "complaint_targets": {"status": "TEXT NOT NULL DEFAULT 'open'", "resolution": "TEXT",
                                  "resolved_at": "TEXT"},
        }
        for table, columns in additions.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.raw_connect()
        depth = getattr(self._local, "depth", 0)
        guard = self._memory_lock if self.path == ":memory:" else None
        if guard:
            guard.acquire()
        try:
            if depth == 0:
                conn.execute("BEGIN IMMEDIATE")
            self._local.depth = depth + 1
            try:
                yield conn
            except Exception:
                self._local.depth = depth
                if depth == 0:
                    conn.rollback()
                raise
            self._local.depth = depth
            if depth == 0:
                conn.commit()
        finally:
            if guard:
                guard.release()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        yield self.raw_connect()

    # -- 查询助手 -------------------------------------------------------
    def one(self, conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = conn.execute(sql, params).fetchone()
        return _row_to_dict(row) if row else None

    def all(self, conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]

    def get_meta(self, conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))
