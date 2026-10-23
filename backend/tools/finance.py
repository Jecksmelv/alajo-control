"""Deterministic financial calculations. No model ever performs this arithmetic.

All money is integer naira. Allocation of a payment to contributions is FIFO
(oldest open contribution first), the convention used for arrears.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from .. import config
from ..database import one, rows, scalar


def contribution_paid(conn: sqlite3.Connection, contribution_id: str) -> int:
    return int(scalar(conn, "SELECT COALESCE(SUM(amount),0) FROM ledger_entries WHERE contribution_id = ?",
                      (contribution_id,)) or 0)


def contribution_outstanding(conn: sqlite3.Connection, contribution_id: str) -> int:
    exp = scalar(conn, "SELECT expected_amount FROM expected_contributions WHERE contribution_id = ?", (contribution_id,))
    if exp is None:
        raise ValueError(f"Unknown contribution {contribution_id}")
    return max(0, int(exp) - contribution_paid(conn, contribution_id))


def days_late(due_date: str | date, today: date | None = None) -> int:
    d = date.fromisoformat(due_date) if isinstance(due_date, str) else due_date
    return max(0, ((today or config.DEMO_TODAY) - d).days)


def is_confirmed_overdue_date(due_date: str | date) -> bool:
    return days_late(due_date) > config.GRACE_DAYS


def refresh_status(conn: sqlite3.Connection, contribution_id: str) -> str:
    exp = int(scalar(conn, "SELECT expected_amount FROM expected_contributions WHERE contribution_id = ?",
                     (contribution_id,)))
    paid = contribution_paid(conn, contribution_id)
    status = "paid" if paid >= exp else ("partial" if paid > 0 else "unpaid")
    conn.execute("UPDATE expected_contributions SET status = ? WHERE contribution_id = ?", (status, contribution_id))
    return status


def open_contributions(conn: sqlite3.Connection, customer_id: str, through: date | None = None) -> list[dict[str, Any]]:
    """Unpaid/partial contributions, oldest first, with deterministic outstanding."""
    through = through or (config.DEMO_TODAY)
    out = rows(conn, """
        SELECT c.contribution_id, c.customer_id, c.due_date, c.expected_amount, c.status,
               COALESCE((SELECT SUM(amount) FROM ledger_entries l WHERE l.contribution_id = c.contribution_id), 0) AS paid
        FROM expected_contributions c
        WHERE c.customer_id = ? AND c.status != 'paid' AND c.due_date <= ?
        ORDER BY c.due_date""", (customer_id, through.isoformat()))
    for r in out:
        r["outstanding"] = int(r["expected_amount"]) - int(r["paid"])
        r["days_late"] = days_late(r["due_date"])
    return [r for r in out if r["outstanding"] > 0]


def fifo_allocation(conn: sqlite3.Connection, customer_id: str, amount: int, through: date) -> tuple[list[dict], int]:
    """Plan (without writing) how `amount` would be allocated FIFO. Returns (allocations, unallocated)."""
    remaining = int(amount)
    allocations = []
    for c in open_contributions(conn, customer_id, through):
        if remaining <= 0:
            break
        take = min(remaining, c["outstanding"])
        allocations.append({"contribution_id": c["contribution_id"], "due_date": c["due_date"], "amount": take,
                            "expected_amount": c["expected_amount"], "outstanding_before": c["outstanding"],
                            "outstanding_after": c["outstanding"] - take})
        remaining -= take
    return allocations, remaining


def post_rule_entries(conn: sqlite3.Connection, customer_id: str, allocations: list[dict], *, when: str,
                      source_type: str, source_ref: str, rule: str) -> None:
    """Deterministic straight-through posting (exact reference / settled provider / balanced agent batch).
    Only the reconciliation engine calls this; no agent tool can reach it."""
    for a in allocations:
        conn.execute("INSERT INTO ledger_entries(customer_id, contribution_id, amount, entry_time, source_type, source_ref, "
                     "posted_by, approval_id) VALUES (?,?,?,?,?,?,?,NULL)",
                     (customer_id, a["contribution_id"], a["amount"], when, source_type, source_ref, f"rule:{rule}"))
        refresh_status(conn, a["contribution_id"])


def customer_balance(conn: sqlite3.Connection, customer_id: str) -> dict[str, Any]:
    """Savings balance and arrears, all from SQL."""
    saved = int(scalar(conn, "SELECT COALESCE(SUM(amount),0) FROM ledger_entries WHERE customer_id = ?", (customer_id,)) or 0)
    due_to_date = int(scalar(conn, "SELECT COALESCE(SUM(expected_amount),0) FROM expected_contributions "
                                   "WHERE customer_id = ? AND due_date <= ?",
                             (customer_id, config.DEMO_TODAY.isoformat())) or 0)
    opens = open_contributions(conn, customer_id)
    overdue = [c for c in opens if c["days_late"] > config.GRACE_DAYS]
    in_grace = [c for c in opens if c["days_late"] <= config.GRACE_DAYS]
    return {
        "customer_id": customer_id,
        "recorded_savings_in_window": saved,
        "expected_to_date_in_window": due_to_date,
        "outstanding_total": sum(c["outstanding"] for c in opens),
        "overdue_outstanding": sum(c["outstanding"] for c in overdue),
        "overdue_contributions": len(overdue),
        "max_days_late": max((c["days_late"] for c in overdue), default=0),
        "in_grace_outstanding": sum(c["outstanding"] for c in in_grace),
        "open_contributions": opens,
    }


def payment_history_metrics(conn: sqlite3.Connection, customer_id: str) -> dict[str, Any]:
    """On-time / late / missed counts over the 28-day history window (excludes the last 2 days)."""
    hist = rows(conn, """
        SELECT c.contribution_id, c.due_date, c.expected_amount,
               (SELECT MIN(entry_time) FROM ledger_entries l WHERE l.contribution_id = c.contribution_id) AS first_paid,
               COALESCE((SELECT SUM(amount) FROM ledger_entries l WHERE l.contribution_id = c.contribution_id),0) AS paid
        FROM expected_contributions c WHERE c.customer_id = ? AND c.due_date <= ? ORDER BY c.due_date""",
                (customer_id, (config.DEMO_TODAY.fromordinal(config.DEMO_TODAY.toordinal() - 2)).isoformat()))
    on_time = late = missed = 0
    for h in hist:
        if h["paid"] < h["expected_amount"]:
            missed += 1
        elif h["first_paid"] and h["first_paid"][:10] > h["due_date"]:
            late += 1
        else:
            on_time += 1
    n = len(hist)
    return {"contributions_due": n, "on_time": on_time, "late": late, "missed_or_short": missed,
            "on_time_rate": round(on_time / n, 3) if n else None}


def portfolio_collection_rate(conn: sqlite3.Connection) -> float:
    """Share of contributions due in the history window (excluding the last 2 days) paid by due date + 1 day."""
    cutoff = config.DEMO_TODAY.fromordinal(config.DEMO_TODAY.toordinal() - 2).isoformat()
    r = one(conn, """
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN (SELECT MIN(substr(entry_time,1,10)) FROM ledger_entries l
                              WHERE l.contribution_id = c.contribution_id) <= date(c.due_date, '+1 day') THEN 1 ELSE 0 END) AS ok
        FROM expected_contributions c WHERE c.due_date <= ?""", (cutoff,))
    return round((r["ok"] or 0) / r["n"], 4) if r and r["n"] else 0.0


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
