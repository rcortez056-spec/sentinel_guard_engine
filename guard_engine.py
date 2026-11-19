#!/usr/bin/env python3
"""
Sentinel Guard Engine
=====================

Guards every agent action before it runs.

    1. INGEST       Assign a unique transaction ID and record the request
    2. STATE QUERY  Block if the same idempotency key is locked or already succeeded
    3. RULES        Block malformed requests before anything executes
    4. LOCK         Atomically claim the key (enforced by a partial unique index)
    5. EXECUTE      Run the registered action (email, CRM update, LLM call, ...)
    6. RELEASE      Mark SUCCESS or ERROR, which releases the lock
    7. LOG          Every step is written to the audit log with a UTC timestamp

Failures are never retried automatically. They go to a manual review queue.

Standard library only.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class Status(str, Enum):
    INCOMING = "INCOMING"
    LOCKED = "LOCKED"
    SUCCESS = "SUCCESS"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    tx_id           TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    status          TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    result_json     TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- The lock: at most one LOCKED transaction per idempotency key.
CREATE UNIQUE INDEX IF NOT EXISTS one_lock_per_key
    ON transactions(idempotency_key) WHERE status = 'LOCKED';

CREATE INDEX IF NOT EXISTS idx_key_status
    ON transactions(idempotency_key, status);

CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id  TEXT NOT NULL,
    ts     TEXT NOT NULL,
    step   TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS manual_review (
    tx_id      TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, default=str)


class StateStore:
    """SQLite-backed transaction state. Pass a file path to persist across runs."""

    def __init__(self, path: str = ":memory:") -> None:
        self._mutex = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    # -- writes -------------------------------------------------------------

    def insert(self, tx_id: str, key: str, request: Any, status: Status) -> None:
        now = _utc_now()
        with self._mutex, self._conn:
            self._conn.execute(
                "INSERT INTO transactions "
                "(tx_id, idempotency_key, status, request_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tx_id, key, status.value, _dumps(request), now, now),
            )

    def try_lock(self, tx_id: str) -> bool:
        """Return True if this transaction now holds the key lock."""
        try:
            with self._mutex, self._conn:
                self._conn.execute(
                    "UPDATE transactions SET status = ?, updated_at = ? WHERE tx_id = ?",
                    (Status.LOCKED.value, _utc_now(), tx_id),
                )
            return True
        except sqlite3.IntegrityError:
            # Another transaction holds the lock for this key.
            return False

    def release(
        self,
        tx_id: str,
        status: Status,
        result: Any = None,
        error: Optional[str] = None,
    ) -> None:
        with self._mutex, self._conn:
            self._conn.execute(
                "UPDATE transactions SET status = ?, result_json = ?, error = ?, updated_at = ? "
                "WHERE tx_id = ?",
                (status.value, _dumps(result), error, _utc_now(), tx_id),
            )

    def audit(self, tx_id: str, step: str, status: Any, detail: Any = None) -> None:
        status_value = status.value if isinstance(status, Status) else str(status)
        with self._mutex, self._conn:
            self._conn.execute(
                "INSERT INTO audit_log (tx_id, ts, step, status, detail) VALUES (?, ?, ?, ?, ?)",
                (tx_id, _utc_now(), step, status_value, _dumps(detail)),
            )

    def add_manual_review(self, tx_id: str, reason: str) -> None:
        with self._mutex, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO manual_review (tx_id, reason, created_at) VALUES (?, ?, ?)",
                (tx_id, reason, _utc_now()),
            )

    # -- reads --------------------------------------------------------------

    def find_active(self, key: str, exclude_tx_id: str) -> Optional[str]:
        """Return the ID of another LOCKED or SUCCESS transaction for this key, if any."""
        if not key:
            return None
        with self._mutex:
            row = self._conn.execute(
                "SELECT tx_id FROM transactions "
                "WHERE idempotency_key = ? AND tx_id <> ? AND status IN (?, ?) LIMIT 1",
                (key, exclude_tx_id, Status.LOCKED.value, Status.SUCCESS.value),
            ).fetchone()
        return row["tx_id"] if row else None

    def get_transaction(self, tx_id: str) -> Optional[Dict[str, Any]]:
        with self._mutex:
            row = self._conn.execute(
                "SELECT * FROM transactions WHERE tx_id = ?", (tx_id,)
            ).fetchone()
        return dict(row) if row else None

    def audit_trail(self, tx_id: str) -> List[Dict[str, Any]]:
        with self._mutex:
            rows = self._conn.execute(
                "SELECT ts, step, status, detail FROM audit_log WHERE tx_id = ? ORDER BY id",
                (tx_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def manual_review_queue(self) -> List[Dict[str, Any]]:
        with self._mutex:
            rows = self._conn.execute(
                "SELECT * FROM manual_review ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def status_counts(self) -> Dict[str, int]:
        with self._mutex:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM transactions GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}


ActionFn = Callable[[Dict[str, Any]], Any]


class GuardEngine:
    """
    Runs each request through the guard sequence.

    `actions` maps an action name to a callable that receives the request's
    `payload` dict and returns a JSON-serialisable result. Raise an exception
    to signal failure; the engine records it and does not retry.
    """

    def __init__(self, store: StateStore, actions: Dict[str, ActionFn]) -> None:
        self.store = store
        self.actions = dict(actions)
        self._counter = itertools.count(1)
        self._counter_lock = threading.Lock()

    def submit(self, request: Any) -> Dict[str, Any]:
        tx_id = self._new_tx_id()
        req = request if isinstance(request, dict) else {}
        key = str(req.get("idempotency_key") or "").strip()

        # 1. INGEST
        self.store.insert(tx_id, key, request, Status.INCOMING)
        self.store.audit(tx_id, "INGEST", Status.INCOMING, {"action": req.get("action")})

        # 2. STATE QUERY: is this key already in flight or already done?
        other = self.store.find_active(key, exclude_tx_id=tx_id)
        if other:
            return self._finish(
                tx_id, Status.BLOCKED, "STATE_QUERY",
                error=f"Duplicate: key already locked or completed by {other}",
            )

        # 3. RULES: reject malformed requests before any work happens
        problem = self._validate(req, key)
        if problem:
            return self._finish(tx_id, Status.BLOCKED, "RULES", error=problem)

        # 4. LOCK
        if not self.store.try_lock(tx_id):
            return self._finish(
                tx_id, Status.BLOCKED, "LOCK",
                error="State lock held by another transaction",
            )
        self.store.audit(tx_id, "LOCK", Status.LOCKED, {"key": key})

        # 5. EXECUTE
        try:
            result = self.actions[req["action"]](req["payload"])
        except Exception as exc:  # noqa: BLE001 - every failure is recorded
            message = f"{type(exc).__name__}: {exc}"
            self.store.add_manual_review(tx_id, message)
            return self._finish(tx_id, Status.ERROR, "EXECUTE", error=message)

        # 6. RELEASE (status change releases the lock) + 7. LOG
        return self._finish(tx_id, Status.SUCCESS, "EXECUTE", result=result)

    # -- internals ----------------------------------------------------------

    def _new_tx_id(self) -> str:
        with self._counter_lock:
            seq = next(self._counter)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        return f"TX-{stamp}-{seq:04d}"

    def _validate(self, req: Dict[str, Any], key: str) -> Optional[str]:
        if not req:
            return "Request must be a JSON object"
        if not key:
            return "Missing or empty 'idempotency_key'"
        if req.get("action") not in self.actions:
            return f"Unknown action '{req.get('action')}'"
        if not isinstance(req.get("payload"), dict):
            return "'payload' must be an object"
        return None

    def _finish(
        self,
        tx_id: str,
        status: Status,
        step: str,
        result: Any = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.store.audit(tx_id, step, status, {"error": error} if error else {"result": result})
        self.store.release(tx_id, status, result=result, error=error)
        self.store.audit(tx_id, "RELEASE", status)
        return {"tx_id": tx_id, "status": status.value, "result": result, "error": error}


# ---------------------------------------------------------------------------
# Demo actions (replace with real integrations; no credentials are included)
# ---------------------------------------------------------------------------

def send_email(payload: Dict[str, Any]) -> Dict[str, Any]:
    time.sleep(0.02)
    if payload.get("simulate_failure"):
        raise ConnectionError("SMTP 421: service temporarily unavailable")
    return {"delivered_to": payload["to"], "message_id": f"MSG-{abs(hash(payload['to'])) % 10**6}"}


def crm_update(payload: Dict[str, Any]) -> Dict[str, Any]:
    time.sleep(0.02)
    return {"crm_record": payload["lead_id"], "stage": payload.get("stage", "contacted")}


DEMO_ACTIONS: Dict[str, ActionFn] = {
    "send_email": send_email,
    "crm_update": crm_update,
}


def _print_result(label: str, result: Dict[str, Any]) -> None:
    detail = result["error"] or result["result"]
    print(f"{label:<34} {result['status']:<8} {result['tx_id']}")
    print(f"{'':<34} -> {detail}")


def main() -> None:
    store = StateStore()
    engine = GuardEngine(store, DEMO_ACTIONS)

    print("SENTINEL GUARD ENGINE - DEMO RUN")
    print("-" * 78)

    good = {
        "idempotency_key": "lead-101:send_email",
        "action": "send_email",
        "payload": {"to": "sarah@techstartup.com", "subject": "Quick question"},
    }
    r1 = engine.submit(good)
    _print_result("1. Normal request", r1)

    r2 = engine.submit(good)
    _print_result("2. Duplicate of request 1", r2)

    r3 = engine.submit({"idempotency_key": "lead-102:crm", "action": "crm_update"})
    _print_result("3. Malformed (no payload)", r3)

    r4 = engine.submit({
        "idempotency_key": "lead-103:send_email",
        "action": "send_email",
        "payload": {"to": "mia@example.com", "simulate_failure": True},
    })
    _print_result("4. Executor failure", r4)

    print("-" * 78)
    print("Status counts:", store.status_counts())
    print("Manual review queue:")
    for item in store.manual_review_queue():
        print(f"  {item['tx_id']}  {item['reason']}")
    print(f"\nAudit trail for {r1['tx_id']}:")
    for entry in store.audit_trail(r1["tx_id"]):
        print(f"  {entry['ts']}  {entry['step']:<10} {entry['status']}")


if __name__ == "__main__":
    main()
