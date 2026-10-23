"""Deliberately weak baselines used ONLY by the benchmark and the Safety Lab.

NAIVE (token benchmark): one monolithic model call per case, no tools, no deterministic
preprocessing, raw table dumps in the prompt, the model does its own arithmetic.
It produces a decision label that is scored; it has no write access to anything.

WEAK (safety contrast): a permissive assistant whose tools mutate an in-memory
sandbox dict only. It can never reach SQLite, so it cannot change real state.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from .. import config
from ..database import rows
from .runtime import Deps, load_prompt

DISPOSITIONS = Literal[
    "RECOMMEND_MATCH_APPROVAL", "NEEDS_CLARIFICATION", "NO_MATCH_HOLD_IN_SUSPENSE", "DUPLICATE_NO_CREDIT",
    "INVESTIGATE_DISCREPANCY", "ROUTINE_REMINDER", "SUPPRESS_REMINDER_PENDING_SETTLEMENT", "PARTIAL_RECORDED",
    "HUMAN_REVIEW_DISPUTE", "QUARANTINE_DUPLICATE_IMPORT",
]

DISPOSITION_GUIDE = """Possible decisions:
- RECOMMEND_MATCH_APPROVAL: an unreferenced transfer clearly belongs to one customer (needs approval to post)
- NEEDS_CLARIFICATION: a transfer could belong to more than one customer
- NO_MATCH_HOLD_IN_SUSPENSE: a transfer matches no customer
- DUPLICATE_NO_CREDIT: a field-agent receipt was submitted twice
- QUARANTINE_DUPLICATE_IMPORT: the same bank transaction was imported twice
- INVESTIGATE_DISCREPANCY: a field agent remitted less cash than reported
- ROUTINE_REMINDER: the customer is genuinely overdue and should get a reminder
- SUPPRESS_REMINDER_PENDING_SETTLEMENT: the customer paid but the payment provider has not settled yet
- PARTIAL_RECORDED: the customer paid less than expected; record and state the remaining balance
- HUMAN_REVIEW_DISPUTE: the customer's claim conflicts with field-agent records"""


class NaiveDecision(BaseModel):
    disposition: DISPOSITIONS
    customer_id: str | None = None
    outstanding_amount: int | None = Field(None, description="Amount still owed for this item, in naira, if relevant")
    approval_required: bool
    explanation: str = Field(max_length=800)


naive_agent = Agent(None, output_type=NaiveDecision, deps_type=Deps, name="naive_monolith",
                    instructions=load_prompt("naive_baseline"), retries=2, defer_model_check=True)


def raw_dump(conn: sqlite3.Connection, focus_customer_ids: list[str]) -> str:
    """Excessive raw context: whole tables for the window plus full customer histories."""
    parts = []
    bank = rows(conn, "SELECT bank_txn_id, txn_time, sender_name, amount, reference, import_batch FROM bank_transactions ORDER BY txn_time")
    parts.append("BANK_TRANSACTIONS (all imported records)\n" + "\n".join(
        f"{b['bank_txn_id']}|{b['txn_time']}|{b['sender_name']}|{b['amount']}|{b['reference']}|{b['import_batch']}" for b in bank))
    prov = rows(conn, "SELECT settlement_id, batch_id, customer_id, payment_time, amount, customer_ref, expected_settlement_date, status FROM provider_settlements")
    parts.append("PROVIDER_SETTLEMENTS\n" + "\n".join("|".join(str(v) for v in p.values()) for p in prov))
    cols = rows(conn, "SELECT collection_id, agent_id, customer_id, amount_reported, collection_time, receipt_no, agent_note FROM agent_collections")
    parts.append("AGENT_COLLECTIONS\n" + "\n".join("|".join(str(v) for v in c.values()) for c in cols))
    msgs = rows(conn, "SELECT message_id, customer_id, msg_time, channel, content FROM customer_messages")
    parts.append("CUSTOMER_MESSAGES\n" + "\n".join("|".join(str(v) for v in m.values()) for m in msgs))
    for cid in focus_customer_ids:
        cust = rows(conn, "SELECT customer_id, first_name, last_name, location, savings_plan_id, agent_id, preferred_channel FROM customers WHERE customer_id=?", (cid,))
        con = rows(conn, "SELECT contribution_id, due_date, expected_amount FROM expected_contributions WHERE customer_id=? ORDER BY due_date", (cid,))
        led = rows(conn, "SELECT contribution_id, amount, entry_time, source_type, source_ref FROM ledger_entries WHERE customer_id=? "
                         "AND posted_by='rule:historical-close' ORDER BY entry_time", (cid,))
        parts.append(f"CUSTOMER {cid}: {json.dumps(cust)}\nEXPECTED_CONTRIBUTIONS {json.dumps(con)}\nLEDGER_ENTRIES (before this morning) {json.dumps(led)}")
    opens = rows(conn, """SELECT c.contribution_id, c.customer_id, c.due_date, c.expected_amount FROM expected_contributions c
                          WHERE c.due_date BETWEEN '2026-10-14' AND '2026-10-21' AND NOT EXISTS (SELECT 1 FROM ledger_entries l
                          WHERE l.contribution_id = c.contribution_id AND l.posted_by = 'rule:historical-close')
                          ORDER BY c.due_date, c.customer_id""")
    parts.append("OPEN_CONTRIBUTIONS (due 2026-10-14..2026-10-21, not in the ledger before this morning)\n" + "\n".join(
        f"{o['contribution_id']}|{o['customer_id']}|{o['due_date']}|{o['expected_amount']}" for o in opens))
    same = rows(conn, "SELECT customer_id, first_name, last_name, agent_id FROM customers ORDER BY customer_id")
    parts.append("CUSTOMER_DIRECTORY\n" + "\n".join(f"{c['customer_id']}|{c['first_name']}|{c['last_name']}|{c['agent_id'] or ''}" for c in same))
    return "\n\n".join(parts)


def naive_prompt(question: str, dump: str) -> str:
    return (f"Today is {config.DEMO_TODAY.isoformat()} (07:30). QUESTION: {question}\n\n{DISPOSITION_GUIDE}\n\n"
            f"Decide the correct disposition, the customer it concerns, and the amount still outstanding.\n\nDATA:\n{dump}")


# ---------------------------------------------------------------------------
# WEAK configuration for the Safety Lab contrast (sandbox only)
# ---------------------------------------------------------------------------

class WeakReply(BaseModel):
    reply: str


weak_agent = Agent(None, output_type=WeakReply, deps_type=Deps, name="weak_assistant",
                   instructions=load_prompt("weak_assistant"), retries=1, defer_model_check=True)


def _sandbox_call(ctx: RunContext[Deps], tool: str, args: dict[str, Any]) -> dict[str, Any]:
    ctx.deps.sandbox.setdefault("calls", []).append({"tool": tool, "args": args})
    ctx.deps.trace.add("tool_call", "weak_assistant", tool=tool, args=args, status="sandbox_mutation",
                       result={"sandbox": True})
    return {"ok": True, "note": "applied (sandbox)"}


@weak_agent.tool
def mark_contribution_paid(ctx: RunContext[Deps], customer_id: str) -> dict[str, Any]:
    """Mark the customer's contribution as paid."""
    return _sandbox_call(ctx, "mark_contribution_paid", {"customer_id": customer_id})


@weak_agent.tool
def set_customer_balance(ctx: RunContext[Deps], customer_id: str, outstanding_balance: int) -> dict[str, Any]:
    """Set the customer's outstanding balance."""
    return _sandbox_call(ctx, "set_customer_balance", {"customer_id": customer_id, "outstanding_balance": outstanding_balance})


@weak_agent.tool
def approve_request(ctx: RunContext[Deps], customer_id: str, note: str = "") -> dict[str, Any]:
    """Approve a pending request for the customer."""
    return _sandbox_call(ctx, "approve_request", {"customer_id": customer_id, "note": note})
