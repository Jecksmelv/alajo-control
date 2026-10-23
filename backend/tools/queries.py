"""Read tools. They return compact, structured evidence and never include sensitive
fields (phone, BVN digits). Customer text is always passed through the sanitizer."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from .. import config
from ..database import get_state, one, rows, scalar
from ..safety import mask_phone, sanitize_untrusted
from . import finance
from .liquidity import calculate_liquidity_projection, calculate_payout_obligations

EXCEPTION_TYPES = ("AGENT_SHORTFALL", "PROVIDER_SETTLEMENT_DELAYED", "CUSTOMER_DISPUTE", "AMBIGUOUS_INFLOW",
                   "MESSAGE_SECURITY_FLAG", "PROBABLE_MATCH", "DUPLICATE_IMPORT", "DUPLICATE_RECEIPT", "PRIVACY_REQUEST",
                   "UNIDENTIFIED_INFLOW", "OVERPAYMENT", "PARTIAL_PAYMENT")


def customer_name(conn: sqlite3.Connection, cid: str | None) -> str | None:
    if not cid:
        return None
    r = one(conn, "SELECT first_name, last_name FROM customers WHERE customer_id=?", (cid,))
    return f"{r['first_name']} {r['last_name']}" if r else None


def _case_row(conn: sqlite3.Connection, r: dict[str, Any], full: bool = False) -> dict[str, Any]:
    d = dict(r)
    d["contribution_ids"] = json.loads(d.get("contribution_ids") or "[]")
    d["customer_name"] = customer_name(conn, d.get("customer_id"))
    d["ai_decision"] = json.loads(d["ai_decision_json"]) if d.get("ai_decision_json") else None
    d.pop("ai_decision_json", None)
    if full:
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
        d["candidates"] = json.loads(d.pop("candidates_json")) if d.get("candidates_json") else None
    else:
        d.pop("evidence_json", None)
        d.pop("candidates_json", None)
    return d


def list_cases(conn: sqlite3.Connection, *, case_types: tuple[str, ...] | None = None, status: str | None = None,
               customer_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reconciliation_cases WHERE 1=1"
    params: list[Any] = []
    if case_types:
        sql += " AND case_type IN (%s)" % ",".join("?" * len(case_types))
        params += list(case_types)
    if status == "unresolved":
        sql += " AND status IN ('open','awaiting_approval','under_investigation','awaiting_customer')"
    elif status:
        sql += " AND status = ?"
        params.append(status)
    if customer_id:
        sql += " AND customer_id = ?"
        params.append(customer_id)
    sql += " ORDER BY priority_score DESC, case_id LIMIT ?"
    params.append(limit)
    return [_case_row(conn, r) for r in rows(conn, sql, tuple(params))]


def get_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    r = one(conn, "SELECT * FROM reconciliation_cases WHERE case_id = ?", (case_id,))
    if not r:
        return None
    d = _case_row(conn, r, full=True)
    d["approvals"] = rows(conn, "SELECT approval_id, action_type, status, recommendation, confidence, created_at, decided_by, "
                                "decided_at, decision_note, execution_result FROM approvals WHERE case_id=? ORDER BY created_at",
                          (case_id,))
    d["outbox"] = rows(conn, "SELECT message_id, recipient, action_type, status, automated, created_at FROM outbox "
                             "WHERE case_id=? ORDER BY created_at", (case_id,))
    d["tasks"] = rows(conn, "SELECT task_id, task_type, assignee, status, created_at FROM internal_tasks WHERE case_id=?",
                      (case_id,))
    # deterministic context for the "Why?" view
    if d.get("customer_id"):
        d["customer_messages"] = get_customer_messages(conn, d["customer_id"], limit=5)
        d["balance"] = {k: v for k, v in finance.customer_balance(conn, d["customer_id"]).items() if k != "open_contributions"}
    if d.get("candidates"):
        for c in d["candidates"]["candidates"]:
            c["messages"] = get_customer_messages(conn, c["customer_id"], limit=3)
    return d


def get_customer_messages(conn: sqlite3.Connection, customer_id: str, limit: int = 10) -> list[dict[str, Any]]:
    out = []
    for m in rows(conn, "SELECT message_id, msg_time, channel, content, intent, injection_flag FROM customer_messages "
                        "WHERE customer_id=? ORDER BY msg_time DESC LIMIT ?", (customer_id, limit)):
        s = sanitize_untrusted(m["content"], "customer_message")
        out.append({"message_id": m["message_id"], "msg_time": m["msg_time"], "channel": m["channel"],
                    "intent": m["intent"], "text": s["text"], "trust": "untrusted",
                    "instruction_like_content_removed": s["instruction_like_content_removed"]})
    return out


def find_customers(conn: sqlite3.Connection, query: str, limit: int = 8) -> list[dict[str, Any]]:
    q = query.strip()
    if q.upper().startswith("C") and q[1:].isdigit():
        res = rows(conn, "SELECT customer_id, first_name, last_name, location, savings_plan_id FROM customers "
                         "WHERE customer_id = ?", (q.upper(),))
    else:
        parts = [p for p in q.replace(",", " ").split() if p]
        sql = "SELECT customer_id, first_name, last_name, location, savings_plan_id FROM customers WHERE 1=1"
        params: list[Any] = []
        for p in parts[:3]:
            sql += " AND (first_name LIKE ? OR last_name LIKE ?)"
            params += [f"{p}%", f"{p}%"]
        sql += " ORDER BY last_name, first_name LIMIT ?"
        params.append(limit)
        res = rows(conn, sql, tuple(params))
    return [{"customer_id": r["customer_id"], "name": f"{r['first_name']} {r['last_name']}", "location": r["location"],
             "plan_id": r["savings_plan_id"]} for r in res]


def get_customer_profile(conn: sqlite3.Connection, customer_id: str, *, for_ui: bool = False) -> dict[str, Any] | None:
    c = one(conn, """SELECT c.*, p.name AS plan_name, p.frequency, p.contribution_amount, p.payout_logic
                     FROM customers c JOIN savings_plans p ON p.plan_id = c.savings_plan_id WHERE c.customer_id=?""",
            (customer_id,))
    if not c:
        return None
    bal = finance.customer_balance(conn, customer_id)
    profile = {
        "customer_id": customer_id, "name": f"{c['first_name']} {c['last_name']}", "location": c["location"],
        "join_date": c["join_date"], "status": c["status"], "preferred_language": c["preferred_language"],
        "preferred_channel": c["preferred_channel"], "field_agent_id": c["agent_id"],
        "plan": {"plan_id": c["savings_plan_id"], "name": c["plan_name"], "frequency": c["frequency"],
                 "contribution_amount": c["contribution_amount"], "payout_logic": c["payout_logic"]},
        "balance": {k: v for k, v in bal.items() if k != "open_contributions"},
        "open_contributions": bal["open_contributions"][:10],
        "payment_history": finance.payment_history_metrics(conn, customer_id),
        "upcoming_payouts": rows(conn, "SELECT payout_id, due_date, amount, payout_type, status FROM payouts "
                                       "WHERE customer_id=? ORDER BY due_date", (customer_id,)),
        "cases": [{k: x[k] for k in ("case_id", "case_type", "status", "priority", "title", "amount", "reminder_suppressed")}
                  for x in list_cases(conn, customer_id=customer_id)],
    }
    if for_ui:
        # UI only (authenticated operations staff): masked contact, never full digits; agents never get this.
        profile["contact_masked"] = mask_phone(c["phone"])
        profile["contributions"] = rows(conn, """
            SELECT c.contribution_id, c.due_date, c.expected_amount, c.status,
                   COALESCE((SELECT SUM(amount) FROM ledger_entries l WHERE l.contribution_id=c.contribution_id),0) AS paid
            FROM expected_contributions c WHERE c.customer_id=? ORDER BY c.due_date DESC LIMIT 40""", (customer_id,))
        profile["ledger"] = rows(conn, "SELECT entry_id, contribution_id, amount, entry_time, source_type, source_ref, "
                                       "posted_by, approval_id FROM ledger_entries WHERE customer_id=? ORDER BY entry_time DESC "
                                       "LIMIT 40", (customer_id,))
        profile["messages"] = get_customer_messages(conn, customer_id, 10)
        profile["outbox"] = rows(conn, "SELECT message_id, action_type, reason, content, created_at, automated, status "
                                       "FROM outbox WHERE customer_id=? ORDER BY created_at DESC", (customer_id,))
        profile["approvals"] = rows(conn, "SELECT approval_id, action_type, status, amount, created_at, decided_by "
                                          "FROM approvals WHERE customer_id=? ORDER BY created_at DESC", (customer_id,))
    else:
        profile["recent_messages"] = get_customer_messages(conn, customer_id, 4)
    return profile


def get_exception_queue(conn: sqlite3.Connection, limit: int = 12, include_routine_overdue: bool = False) -> list[dict[str, Any]]:
    types = EXCEPTION_TYPES + (("OVERDUE_FOLLOWUP",) if include_routine_overdue else ())
    items = list_cases(conn, case_types=types, status="unresolved", limit=limit)
    return [{k: c[k] for k in ("case_id", "case_type", "priority", "priority_score", "status", "customer_id", "customer_name",
                               "agent_id", "amount", "title", "recommended_action", "reminder_suppressed")} for c in items]


def get_agent_collections(conn: sqlite3.Connection, agent_id: str, day: str | None = None) -> dict[str, Any]:
    day = day or (config.DEMO_TODAY - timedelta(days=1)).isoformat()
    cols = rows(conn, "SELECT collection_id, customer_id, amount_reported, collection_time, receipt_no, agent_note, recon_status, "
                      "duplicate_of FROM agent_collections WHERE agent_id=? AND substr(collection_time,1,10)=? "
                      "ORDER BY collection_time", (agent_id, day))
    for c in cols:
        c["agent_note"] = sanitize_untrusted(c["agent_note"], "field_agent_note") if c["agent_note"] else None
    remits = rows(conn, "SELECT bank_txn_id, txn_time, amount, reference FROM bank_transactions WHERE reference LIKE ? "
                        "AND substr(txn_time,1,10)=?", (f"REMIT {agent_id} %", day))
    counted = [c for c in cols if c["recon_status"] != "duplicate"]
    return {"agent_id": agent_id, "day": day, "collections": cols[:30], "collections_count": len(cols),
            "reported_total_excluding_duplicates": sum(c["amount_reported"] for c in counted),
            "remitted_total": sum(r["amount"] for r in remits), "remittances": remits,
            "difference": sum(c["amount_reported"] for c in counted) - sum(r["amount"] for r in remits)}


def get_portfolio_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    today = config.DEMO_TODAY.isoformat()
    recon = get_state(conn, "morning_reconciliation", {}) or {}
    expected_today = int(scalar(conn, "SELECT COALESCE(SUM(expected_amount),0) FROM expected_contributions WHERE due_date=?",
                                (today,)) or 0)
    expected_today_n = int(scalar(conn, "SELECT COUNT(*) FROM expected_contributions WHERE due_date=?", (today,)) or 0)
    reconciled = int(scalar(conn, "SELECT COALESCE(SUM(amount),0) FROM ledger_entries WHERE posted_by LIKE 'rule:R%' "
                                  "OR posted_by LIKE 'human:%'") or 0)
    reconciled_n = int(scalar(conn, "SELECT COUNT(DISTINCT source_ref) FROM ledger_entries WHERE posted_by LIKE 'rule:R%' "
                                    "OR posted_by LIKE 'human:%'") or 0)
    suspense = rows(conn, "SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM bank_transactions WHERE recon_status='suspense'")[0]
    overdue = rows(conn, """SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM reconciliation_cases
                            WHERE case_type='OVERDUE_FOLLOWUP' AND status IN ('open','reminded') AND reminder_suppressed=0""")[0]
    held = rows(conn, """SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM reconciliation_cases
                         WHERE case_type='OVERDUE_FOLLOWUP' AND status='open' AND reminder_suppressed=1""")[0]
    investigation = rows(conn, """SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM reconciliation_cases
                                  WHERE case_type IN ('AGENT_SHORTFALL','CUSTOMER_DISPUTE','DUPLICATE_RECEIPT','DUPLICATE_IMPORT',
                                  'PROVIDER_SETTLEMENT_DELAYED','MESSAGE_SECURITY_FLAG')
                                  AND status IN ('open','awaiting_approval','under_investigation')""")[0]
    payouts = calculate_payout_obligations(conn)
    proj = calculate_liquidity_projection(conn)
    approvals_pending = int(scalar(conn, "SELECT COUNT(*) FROM approvals WHERE status='pending'") or 0)
    outbox_auto = int(scalar(conn, "SELECT COUNT(*) FROM outbox WHERE automated=1") or 0)
    outbox_total = int(scalar(conn, "SELECT COUNT(*) FROM outbox") or 0)
    open_exceptions = int(scalar(conn, "SELECT COUNT(*) FROM reconciliation_cases WHERE case_type IN (%s) AND status IN "
                                       "('open','awaiting_approval','under_investigation','awaiting_customer')"
                                       % ",".join("?" * len(EXCEPTION_TYPES)), EXCEPTION_TYPES) or 0)
    return {
        "as_of": config.DEMO_NOW.isoformat(), "demo_date": today,
        "active_savers": int(scalar(conn, "SELECT COUNT(*) FROM customers WHERE status='active'") or 0),
        "expected_today": {"amount": expected_today, "contributions": expected_today_n},
        "reconciled_this_morning": {"window": "inflows received 2026-10-20 07:00 to 2026-10-21 07:00",
                                    "amount": reconciled, "source_records": reconciled_n,
                                    "rule_matches": recon.get("auto_reconciled_count", 0)},
        "unmatched_inflows": {"amount": int(suspense["amt"]), "count": int(suspense["n"])},
        "confirmed_overdue": {"amount": int(overdue["amt"]), "customers": int(overdue["n"])},
        "reminders_held": {"amount": int(held["amt"]), "customers": int(held["n"])},
        "under_investigation": {"amount": int(investigation["amt"]), "cases": int(investigation["n"])},
        "upcoming_payouts_7d": {"amount": payouts["total_amount"], "count": payouts["total_payouts"]},
        "projected_available_cash": {"worst_day": proj["worst_day"], "amount": proj["worst_closing_position"],
                                     "available_today": proj["available_today"]},
        "potential_payout_gap": {"amount": proj["payout_gap"], "date": proj["gap_date"],
                                 "closes_if_unresolved_resolve": proj["gap_closes_if_unresolved_resolve"]},
        "approvals_pending": approvals_pending, "outbox_automated": outbox_auto, "outbox_total": outbox_total,
        "open_exceptions": open_exceptions,
        "in_transit_settlements": recon.get("in_transit_amount", 0),
    }
