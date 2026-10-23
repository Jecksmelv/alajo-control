"""Shared helpers for the evaluation, benchmark and manual-packet scripts.

Ground truth is read ONLY here (evaluation side). Mapping a scenario to the system's
case uses entity ids (transaction, customer, agent) - never the expected label.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend import config
from backend.database import one, rows

GT_PATH = Path(__file__).resolve().parent / "ground_truth.json"

RECON_SCENARIOS = ["CASE_001_SPOUSE_ACCOUNT_PAYMENT", "CASE_002_MISSING_REFERENCE", "CASE_003_DUPLICATE_RECEIPT",
                   "CASE_004_AGENT_SETTLEMENT_SHORT", "CASE_005_GENUINE_OVERDUE", "CASE_006_DELAYED_SETTLEMENT",
                   "CASE_007_PARTIAL_PAYMENT", "CASE_008_AMBIGUOUS_SAME_AMOUNT", "CASE_009_CUSTOMER_DISPUTE",
                   "CASE_010_DUPLICATE_BANK_IMPORT"]


def load_gt() -> dict[str, Any]:
    return json.loads(GT_PATH.read_text(encoding="utf-8"))


def all_scenarios(gt: dict[str, Any]) -> list[dict[str, Any]]:
    return gt["scenarios"] + gt["additional_cases"]


def benchmark_items(gt: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {s["scenario_id"]: s for s in all_scenarios(gt)}
    return [by_id[s] for s in RECON_SCENARIOS] + gt["additional_cases"]


def find_case(conn: sqlite3.Connection, e: dict[str, Any]) -> dict[str, Any] | None:
    if e.get("bank_txn_ids"):
        ids = e["bank_txn_ids"]
        return one(conn, "SELECT * FROM reconciliation_cases WHERE bank_txn_id IN (%s) AND case_type='DUPLICATE_IMPORT'"
                   % ",".join("?" * len(ids)), tuple(ids))
    if e.get("bank_txn_id"):
        return one(conn, "SELECT * FROM reconciliation_cases WHERE bank_txn_id=? AND case_type IN "
                         "('PROBABLE_MATCH','AMBIGUOUS_INFLOW','UNIDENTIFIED_INFLOW','PARTIAL_PAYMENT')", (e["bank_txn_id"],))
    if e.get("receipt_no"):
        return one(conn, "SELECT * FROM reconciliation_cases WHERE case_type='DUPLICATE_RECEIPT' AND customer_id=?",
                   (e["customer_id"],))
    if e.get("agent_id") and e.get("customer_ids"):
        return one(conn, "SELECT * FROM reconciliation_cases WHERE case_type='AGENT_SHORTFALL' AND agent_id=?", (e["agent_id"],))
    if e.get("customer_id"):
        c = one(conn, "SELECT * FROM reconciliation_cases WHERE customer_id=? AND case_type NOT IN "
                      "('OVERDUE_FOLLOWUP','PROVIDER_SETTLEMENT_PENDING') ORDER BY priority_score DESC LIMIT 1", (e["customer_id"],))
        return c or one(conn, "SELECT * FROM reconciliation_cases WHERE customer_id=? AND case_type='OVERDUE_FOLLOWUP'",
                        (e["customer_id"],))
    return None


def disposition_of(case: dict[str, Any] | None) -> str:
    if not case:
        return "NOT_DETECTED"
    t = case["case_type"]
    if t in ("PROBABLE_MATCH", "AMBIGUOUS_INFLOW"):
        d = json.loads(case["ai_decision_json"]) if case.get("ai_decision_json") else None
        if not d:
            return "NOT_REVIEWED"
        return {"recommend_match": "RECOMMEND_MATCH_APPROVAL", "needs_clarification": "NEEDS_CLARIFICATION",
                "no_match": "NO_MATCH_HOLD_IN_SUSPENSE"}[d["decision"]]
    if t == "OVERDUE_FOLLOWUP":
        if not case["reminder_suppressed"]:
            return "ROUTINE_REMINDER"
        reasons = json.loads(case["evidence_json"]).get("suppression_reasons", [])
        return "SUPPRESS_REMINDER_PENDING_SETTLEMENT" if any("Provider" in r for r in reasons) else "REMINDER_HELD"
    return {"UNIDENTIFIED_INFLOW": "NO_MATCH_HOLD_IN_SUSPENSE", "DUPLICATE_RECEIPT": "DUPLICATE_NO_CREDIT",
            "DUPLICATE_IMPORT": "QUARANTINE_DUPLICATE_IMPORT", "AGENT_SHORTFALL": "INVESTIGATE_DISCREPANCY",
            "PARTIAL_PAYMENT": "PARTIAL_RECORDED", "CUSTOMER_DISPUTE": "HUMAN_REVIEW_DISPUTE",
            "MESSAGE_SECURITY_FLAG": "INJECTION_NEUTRALIZED", "PRIVACY_REQUEST": "PRIVACY_DENIED"}.get(t, t)


def matched_customer(case: dict[str, Any] | None) -> str | None:
    if not case or not case.get("ai_decision_json"):
        return None
    return json.loads(case["ai_decision_json"]).get("customer_id")


def contribution_paid(conn: sqlite3.Connection, kid: str) -> int:
    r = one(conn, "SELECT COALESCE(SUM(amount),0) AS s FROM ledger_entries WHERE contribution_id=?", (kid,))
    return int(r["s"])


def reminders_to(conn: sqlite3.Connection, cid: str) -> list[dict[str, Any]]:
    return rows(conn, "SELECT * FROM outbox WHERE customer_id=? AND action_type='routine_overdue_reminder'", (cid,))


def reminder_population(conn: sqlite3.Connection, gt: dict[str, Any]) -> dict[str, Any]:
    """Across all customers: who truly owes (money terms, FIFO) vs. who the ledger alone would chase."""
    truth_paid = set(gt["truth_paid_contribution_ids"])
    cutoff = (config.DEMO_TODAY.fromordinal(config.DEMO_TODAY.toordinal() - config.GRACE_DAYS - 1)).isoformat()
    contribs = rows(conn, "SELECT contribution_id, customer_id, due_date, expected_amount FROM expected_contributions")
    due_by: dict[str, int] = {}
    paid_by: dict[str, int] = {}
    for c in contribs:
        if c["due_date"] <= cutoff:
            due_by[c["customer_id"]] = due_by.get(c["customer_id"], 0) + c["expected_amount"]
        if c["contribution_id"] in truth_paid:
            paid_by[c["customer_id"]] = paid_by.get(c["customer_id"], 0) + c["expected_amount"]
    # partial payment (Emeka) is real money even though the contribution is not fully paid
    for s in gt["scenarios"]:
        if s["scenario_id"] == "CASE_007_PARTIAL_PAYMENT":
            cid = s["entities"]["customer_id"]
            paid_by[cid] = paid_by.get(cid, 0) + s["expected"]["received"]
    truly_owes = {c for c, due in due_by.items() if due > paid_by.get(c, 0)}
    ledger_overdue = {r["customer_id"] for r in rows(conn, "SELECT DISTINCT customer_id FROM reconciliation_cases WHERE case_type='OVERDUE_FOLLOWUP'")}
    reminded = {r["customer_id"] for r in rows(conn, "SELECT DISTINCT customer_id FROM outbox WHERE action_type='routine_overdue_reminder'")}
    # policy holds: open questions where the correct action is to hold, whatever the money truth
    holds = set()
    for s in gt["scenarios"]:
        if s["scenario_id"] in ("CASE_008_AMBIGUOUS_SAME_AMOUNT", "CASE_009_CUSTOMER_DISPUTE", "CASE_011_PROMPT_INJECTION",
                                "CASE_007_PARTIAL_PAYMENT"):
            holds.update(s["entities"].get("customer_ids") or [s["entities"].get("customer_id")])
        if s["scenario_id"] == "CASE_004_AGENT_SETTLEMENT_SHORT":
            holds.update(s["entities"]["customer_ids"])
    baseline_false = sorted((ledger_overdue - truly_owes) - holds)
    system_false = sorted((reminded - truly_owes) - holds)
    should = truly_owes & ledger_overdue - holds
    hit = reminded & should
    return {
        "customers_ledger_shows_overdue": len(ledger_overdue),
        "customers_truly_owing": len(truly_owes & ledger_overdue),
        "policy_hold_customers": len(holds & ledger_overdue),
        "ledger_only_false_reminders": len(baseline_false),
        "system_false_reminders": len(system_false),
        "false_reminders_prevented": len(baseline_false) - len(system_false),
        "reminders_sent": len(reminded),
        "genuine_overdue_reminded": len(hit),
        "genuine_overdue_recall_rate": round(len(hit) / len(should), 4) if should else None,
        "reminder_precision_rate": round(len(hit) / len(reminded), 4) if reminded else None,
        "reminders_to_policy_holds": len(reminded & holds),
        "system_false_reminder_ids": system_false[:20],
    }
