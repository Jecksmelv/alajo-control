"""Deterministic 7-day payout liquidity projection.

Base case counts only money the business can rely on:
  available today = cleared balance - suspense (unallocated transfers are restricted until matched)
  + provider settlements still inside their T+1 window, on their expected date
  + forecast contributions = outstanding due that day x trailing 28-day collection rate
  - scheduled payouts
Unresolved inflows are reported separately (suspense transfers, provider batches past
their settlement date, field-agent cash shortfalls); the "resolved" scenario adds them
on day 0 to show which ones close the gap.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from .. import config
from ..database import one, rows, scalar
from .finance import portfolio_collection_rate


def get_unresolved_inflows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for c in rows(conn, """SELECT case_id, case_type, title, amount, bank_txn_id, customer_id, agent_id, status, priority
                           FROM reconciliation_cases WHERE status IN ('open','awaiting_approval')
                           AND case_type IN ('PROBABLE_MATCH','AMBIGUOUS_INFLOW','UNIDENTIFIED_INFLOW',
                                             'PROVIDER_SETTLEMENT_DELAYED','AGENT_SHORTFALL')
                           ORDER BY amount DESC"""):
        kind = {"PROVIDER_SETTLEMENT_DELAYED": "provider settlement past due",
                "AGENT_SHORTFALL": "field-agent cash not remitted",
                "UNIDENTIFIED_INFLOW": "suspense transfer (sender unknown)",
                "PROBABLE_MATCH": "suspense transfer (probable match pending approval)",
                "AMBIGUOUS_INFLOW": "suspense transfer (ambiguous, needs clarification)"}[c["case_type"]]
        out.append({"case_id": c["case_id"], "case_type": c["case_type"], "kind": kind, "title": c["title"],
                    "amount": int(c["amount"] or 0), "status": c["status"], "priority": c["priority"],
                    "customer_id": c["customer_id"], "agent_id": c["agent_id"]})
    return out


def calculate_payout_obligations(conn: sqlite3.Connection, days: int = config.LIQUIDITY_HORIZON_DAYS) -> dict[str, Any]:
    start = config.DEMO_TODAY
    end = start + timedelta(days=days - 1)
    by_day = rows(conn, """SELECT due_date, COUNT(*) AS payouts, SUM(amount) AS amount FROM payouts
                           WHERE status='scheduled' AND due_date BETWEEN ? AND ? GROUP BY due_date ORDER BY due_date""",
                  (start.isoformat(), end.isoformat()))
    by_type = rows(conn, """SELECT due_date, payout_type, COUNT(*) AS payouts, SUM(amount) AS amount FROM payouts
                            WHERE status='scheduled' AND due_date BETWEEN ? AND ? GROUP BY due_date, payout_type""",
                   (start.isoformat(), end.isoformat()))
    return {"horizon_start": start.isoformat(), "horizon_end": end.isoformat(),
            "total_payouts": sum(r["payouts"] for r in by_day), "total_amount": sum(r["amount"] for r in by_day),
            "by_day": by_day, "by_day_and_type": by_type}


def calculate_liquidity_projection(conn: sqlite3.Connection, *, opening_override: int | None = None) -> dict[str, Any]:
    acct = one(conn, "SELECT * FROM treasury_accounts WHERE account_id='PAYOUT-OPS'")
    cleared = int(opening_override if opening_override is not None else acct["cleared_balance"])
    suspense = int(scalar(conn, "SELECT COALESCE(SUM(amount),0) FROM bank_transactions WHERE recon_status='suspense'") or 0)
    rate = portfolio_collection_rate(conn)
    unresolved = get_unresolved_inflows(conn)
    unresolved_total = sum(u["amount"] for u in unresolved)
    available = cleared - suspense
    days_out = []
    running = available
    running_resolved = available + unresolved_total
    for i in range(config.LIQUIDITY_HORIZON_DAYS):
        d = (config.DEMO_TODAY + timedelta(days=i)).isoformat()
        in_transit = int(scalar(conn, "SELECT COALESCE(SUM(amount),0) FROM provider_settlements "
                                      "WHERE recon_status='in_transit' AND expected_settlement_date = ?", (d,)) or 0)
        due_outstanding = int(scalar(conn, """SELECT COALESCE(SUM(c.expected_amount - COALESCE((SELECT SUM(amount)
                                  FROM ledger_entries l WHERE l.contribution_id = c.contribution_id),0)),0)
                                  FROM expected_contributions c WHERE c.due_date = ?""", (d,)) or 0)
        if i > 0:
            # future contributions are generated on the fly from customer plans (not stored ahead)
            due_outstanding = scheduled_contributions_for(conn, d)
        forecast = int(round(due_outstanding * rate))
        payouts = int(scalar(conn, "SELECT COALESCE(SUM(amount),0) FROM payouts WHERE status='scheduled' AND due_date=?",
                             (d,)) or 0)
        n_payouts = int(scalar(conn, "SELECT COUNT(*) FROM payouts WHERE status='scheduled' AND due_date=?", (d,)) or 0)
        opening = running
        running = running + in_transit + forecast - payouts
        running_resolved = running_resolved + in_transit + forecast - payouts
        days_out.append({"date": d, "opening_position": opening, "settlements_in_transit": in_transit,
                         "contributions_due": due_outstanding, "forecast_contributions": forecast,
                         "scheduled_payouts": payouts, "payout_count": n_payouts, "closing_position": running,
                         "closing_position_if_unresolved_resolve": running_resolved,
                         "below_buffer": running < config.OPERATING_BUFFER_NGN, "shortfall": max(0, -running)})
    worst = min(days_out, key=lambda x: x["closing_position"])
    gap = max(0, -worst["closing_position"])
    worst_resolved = min(x["closing_position_if_unresolved_resolve"] for x in days_out)
    # smallest set of unresolved inflows (largest first) that closes the gap
    closers, acc = [], 0
    for u in sorted(unresolved, key=lambda x: -x["amount"]):
        if acc >= gap:
            break
        closers.append(u["case_id"])
        acc += u["amount"]
    return {
        "as_of": acct["as_of"], "cleared_balance": cleared, "suspense_restricted": suspense,
        "available_today": available, "collection_rate_28d": rate, "operating_buffer": config.OPERATING_BUFFER_NGN,
        "days": days_out, "worst_day": worst["date"], "worst_closing_position": worst["closing_position"],
        "payout_gap": gap, "gap_date": worst["date"] if gap > 0 else None,
        "unresolved_inflows_total": unresolved_total, "unresolved_inflows": unresolved[:15],
        "worst_position_if_unresolved_resolve": worst_resolved,
        "gap_closes_if_unresolved_resolve": gap > 0 and worst_resolved >= 0,
        "minimum_cases_to_close_gap": closers if gap > 0 and acc >= gap else [],
        "method": "base = cleared - suspense + in-transit settlements + outstanding due x 28-day collection rate - "
                  "scheduled payouts; unresolved inflows excluded until resolved",
    }


def scheduled_contributions_for(conn: sqlite3.Connection, day_iso: str) -> int:
    """Contributions scheduled on a future day, derived from each active customer's plan."""
    from datetime import date

    d = date.fromisoformat(day_iso)
    daily = int(scalar(conn, """SELECT COALESCE(SUM(p.contribution_amount),0) FROM customers c
                                JOIN savings_plans p ON p.plan_id = c.savings_plan_id
                                WHERE c.status='active' AND p.frequency='daily'""") or 0)
    weekly = int(scalar(conn, """SELECT COALESCE(SUM(p.contribution_amount),0) FROM customers c
                                 JOIN savings_plans p ON p.plan_id = c.savings_plan_id
                                 WHERE c.status='active' AND p.frequency='weekly' AND c.due_weekday = ?""",
                        (d.weekday(),)) or 0)
    monthly = int(scalar(conn, """SELECT COALESCE(SUM(p.contribution_amount),0) FROM customers c
                                  JOIN savings_plans p ON p.plan_id = c.savings_plan_id
                                  WHERE c.status='active' AND p.frequency='monthly' AND c.due_day_of_month = ?""",
                         (d.day,)) or 0)
    return daily + weekly + monthly
