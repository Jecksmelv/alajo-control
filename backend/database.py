"""SQLite data layer for the operating database (data/alajo.db).

The operating database is rebuilt deterministically by Reset Demo. The audit and
usage store lives in a separate database (data/audit.db, see audit.py) so that a
demo reset never erases audit history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE savings_plans (
    plan_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    frequency TEXT NOT NULL CHECK (frequency IN ('daily','weekly','monthly')),
    contribution_amount INTEGER NOT NULL,
    cycle_length INTEGER NOT NULL,          -- contributions per cycle
    fee_contributions INTEGER NOT NULL,     -- contributions retained as service fee per cycle
    payout_logic TEXT NOT NULL
);

CREATE TABLE field_agents (
    agent_id TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    location TEXT NOT NULL,
    phone_masked TEXT NOT NULL
);

CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    location TEXT NOT NULL,
    join_date TEXT NOT NULL,
    savings_plan_id TEXT NOT NULL REFERENCES savings_plans(plan_id),
    status TEXT NOT NULL,
    due_weekday INTEGER,                    -- weekly plans: 0=Mon .. 6=Sun
    due_day_of_month INTEGER,               -- monthly plans
    agent_id TEXT REFERENCES field_agents(agent_id),
    preferred_channel TEXT NOT NULL,        -- transfer | card | agent_cash
    preferred_language TEXT NOT NULL,       -- en | pcm (Nigerian Pidgin)
    phone TEXT NOT NULL,                    -- sensitive: never sent to agents
    bvn_last4 TEXT NOT NULL                 -- sensitive: never sent to agents
);

CREATE TABLE expected_contributions (
    contribution_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    due_date TEXT NOT NULL,
    expected_amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unpaid'   -- unpaid | partial | paid (derived from ledger)
);
CREATE INDEX ix_contrib_customer ON expected_contributions(customer_id, due_date);
CREATE INDEX ix_contrib_due ON expected_contributions(due_date, status);

-- Internal savings ledger. Append-only: corrections are new reversing entries.
CREATE TABLE ledger_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    contribution_id TEXT NOT NULL REFERENCES expected_contributions(contribution_id),
    amount INTEGER NOT NULL,
    entry_time TEXT NOT NULL,
    source_type TEXT NOT NULL,              -- bank_transfer | provider | agent_cash | approved_match
    source_ref TEXT,
    posted_by TEXT NOT NULL,                -- rule:<id> or human:<user> (via approval)
    approval_id TEXT
);
CREATE INDEX ix_ledger_contrib ON ledger_entries(contribution_id);
CREATE INDEX ix_ledger_customer ON ledger_entries(customer_id);
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger_entries is append-only'); END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger_entries is append-only'); END;
CREATE TRIGGER ledger_manual_needs_approval BEFORE INSERT ON ledger_entries
WHEN NEW.source_type = 'approved_match' AND NEW.approval_id IS NULL
BEGIN SELECT RAISE(ABORT, 'manual ledger posting requires an approval_id'); END;

CREATE TABLE bank_transactions (
    bank_txn_id TEXT PRIMARY KEY,
    txn_time TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    sender_account_masked TEXT NOT NULL,
    amount INTEGER NOT NULL,
    reference TEXT NOT NULL,
    settlement_status TEXT NOT NULL,        -- settled
    import_batch TEXT NOT NULL,
    recon_status TEXT NOT NULL DEFAULT 'new', -- new | matched | suspense | duplicate | remittance | approved_match
    duplicate_of TEXT
);

CREATE TABLE provider_settlements (
    settlement_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    payment_time TEXT NOT NULL,
    amount INTEGER NOT NULL,
    customer_ref TEXT NOT NULL,
    expected_settlement_date TEXT NOT NULL,
    status TEXT NOT NULL,                   -- settled | pending
    recon_status TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE agent_collections (
    collection_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES field_agents(agent_id),
    customer_id TEXT,
    amount_reported INTEGER NOT NULL,
    collection_time TEXT NOT NULL,
    receipt_no TEXT NOT NULL,
    agent_note TEXT,                        -- free text from the field: untrusted
    recon_status TEXT NOT NULL DEFAULT 'new', -- new | matched | duplicate | held
    duplicate_of TEXT
);

CREATE TABLE customer_messages (
    message_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    msg_time TEXT NOT NULL,
    channel TEXT NOT NULL,                  -- whatsapp | sms | call_note
    content TEXT NOT NULL,                  -- untrusted customer content
    intent TEXT,                            -- deterministic classifier output
    injection_flag INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE payouts (
    payout_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    due_date TEXT NOT NULL,
    amount INTEGER NOT NULL,
    payout_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled'
);

CREATE TABLE treasury_accounts (
    account_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cleared_balance INTEGER NOT NULL,
    as_of TEXT NOT NULL
);

CREATE TABLE reconciliation_cases (
    case_id TEXT PRIMARY KEY,
    case_type TEXT NOT NULL,
    status TEXT NOT NULL,                   -- open | awaiting_approval | resolved | auto_resolved | info
    priority_score INTEGER NOT NULL,
    priority TEXT NOT NULL,                 -- critical | high | medium | low
    customer_id TEXT,
    contribution_ids TEXT,                  -- JSON list
    bank_txn_id TEXT,
    agent_id TEXT,
    amount INTEGER,
    expected_amount INTEGER,
    received_amount INTEGER,
    source TEXT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,            -- deterministic evidence (facts)
    candidates_json TEXT,                   -- candidate matches with deterministic signals
    needs_judgment INTEGER NOT NULL DEFAULT 0,
    reminder_suppressed INTEGER NOT NULL DEFAULT 0,
    recommended_action TEXT,
    ai_decision_json TEXT,                  -- Money Match recommendation (never a mutation)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolution_note TEXT
);
CREATE INDEX ix_case_customer ON reconciliation_cases(customer_id);

CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    case_id TEXT,
    action_type TEXT NOT NULL,
    customer_id TEXT,
    amount INTEGER,
    payload_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence REAL,
    reason_required TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
    requested_by TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    execution_result TEXT
);

CREATE TABLE outbox (
    message_id TEXT PRIMARY KEY,
    customer_id TEXT,
    recipient TEXT NOT NULL,
    channel TEXT NOT NULL,
    action_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    content TEXT NOT NULL,
    language TEXT NOT NULL,
    case_id TEXT,
    approval_id TEXT,
    automated INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued (simulated send)
    created_by TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE internal_tasks (
    task_id TEXT PRIMARY KEY,
    case_id TEXT,
    assignee TEXT NOT NULL,
    task_type TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    automated INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE demo_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_lock = threading.RLock()


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Serialized connection: SQLite writes from concurrent requests are queued."""
    with _lock:
        conn = connect(path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def rows(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def scalar(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> Any:
    r = conn.execute(sql, params).fetchone()
    return r[0] if r else None


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    v = scalar(conn, "SELECT value FROM demo_state WHERE key = ?", (key,))
    return json.loads(v) if v is not None else default


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO demo_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def db_exists() -> bool:
    if not config.DB_PATH.is_file():
        return False
    try:
        with db() as conn:
            return bool(get_state(conn, "initialized", False))
    except sqlite3.DatabaseError:
        return False


def ensure_initialized() -> None:
    """Build the deterministic demo day on first start (fresh clone friendly)."""
    if not db_exists():
        from .demo_reset import reset_demo

        reset_demo(actor="system:first-start")
