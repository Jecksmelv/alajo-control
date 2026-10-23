"""The four specialist agents. Each has its own prompt, its own tool set and its own
structured output; none of them has a tool that can write to the ledger directly."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic_ai import Agent, RunContext

from ..database import one, rows
from ..models import ActionPlanResult, LiquidityBrief, ReconciliationDecision, TriageReport
from ..safety import Gate, SafetyBlock, check_customer_scope, gate_for, sanitize_untrusted
from ..tools import actions, finance, liquidity, queries
from .runtime import Deps, load_prompt, run_tool

SAFETY = load_prompt("safety_rules")


def _instructions(role: str) -> str:
    return SAFETY + "\n\n" + load_prompt(role)


# ---------------------------------------------------------------------------
# Compact evidence builders (minimum necessary data per agent)
# ---------------------------------------------------------------------------

def money_match_evidence(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    case = one(conn, "SELECT * FROM reconciliation_cases WHERE case_id=?", (case_id,))
    if not case:
        raise ValueError(f"Unknown case {case_id}")
    if case["case_type"] not in ("PROBABLE_MATCH", "AMBIGUOUS_INFLOW", "UNIDENTIFIED_INFLOW"):
        raise ValueError(f"{case_id} is a {case['case_type']} case; Money Match only judges unmatched transfers.")
    cand = json.loads(case["candidates_json"] or "{}")
    out_cands = []
    for c in cand.get("candidates", [])[:4]:
        supported = c["signal_score"] >= 30
        item = {k: c[k] for k in ("customer_id", "customer_name", "contribution_id", "due_date", "outstanding",
                                  "sender_name_signal", "days_between_due_and_transfer", "signal_score")}
        item["message_names_sender"] = bool(c["message_names_sender"])
        item["payment_claim_message"] = bool(c["payment_claim_message"])
        if supported:
            item["recent_messages"] = [{"message_id": m["message_id"], "msg_time": m["msg_time"], "text": m["text"],
                                        "trust": "untrusted"} for m in queries.get_customer_messages(conn, c["customer_id"], 3)]
            h = finance.payment_history_metrics(conn, c["customer_id"])
            item["payment_history_28d"] = {k: h[k] for k in ("contributions_due", "on_time", "late", "missed_or_short")}
        out_cands.append(item)
    t = cand.get("bank_txn", {})
    return {"case_id": case_id, "transfer": {"bank_txn_id": t.get("bank_txn_id"), "time": t.get("txn_time"),
                                             "sender_name": sanitize_untrusted(t.get("sender_name"), "bank_sender")["text"],
                                             "reference": sanitize_untrusted(t.get("reference"), "bank_reference")["text"],
                                             "amount": t.get("amount")},
            "deterministic_assessment": cand.get("deterministic_assessment"),
            "open_contributions_with_same_outstanding_amount": cand.get("candidate_count_same_amount"),
            "candidates_with_supporting_signal": cand.get("candidates_with_supporting_signal"),
            "top_candidates": out_cands}


def case_summary(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    c = queries.get_case(conn, case_id)
    if not c:
        raise ValueError(f"Unknown case {case_id}")
    ev = c.get("evidence") or {}
    keep = {}
    for k, v in ev.items():
        if k in ("records", "collections", "sample", "overdue_contributions", "disputed_contributions", "allocation"):
            keep[k] = v[:6] if isinstance(v, list) else v
        elif k not in ("payment_history",):
            keep[k] = v
    return {"case_id": case_id, "case_type": c["case_type"], "status": c["status"], "priority": c["priority"],
            "priority_score": c["priority_score"], "customer_id": c["customer_id"], "customer_name": c["customer_name"],
            "agent_id": c["agent_id"], "amount": c["amount"], "expected_amount": c["expected_amount"],
            "received_amount": c["received_amount"], "title": c["title"], "summary": c["summary"],
            "recommended_action": c["recommended_action"], "evidence": json.loads(json.dumps(keep, default=str)[:3500])
            if len(json.dumps(keep, default=str)) <= 3500 else {"note": "evidence truncated", "keys": list(keep)},
            "money_match_decision": c["ai_decision"],
            "approvals": [{k: a[k] for k in ("approval_id", "action_type", "status")} for a in c["approvals"]],
            "outbox": [{k: o[k] for k in ("message_id", "action_type", "status")} for o in c["outbox"]]}


AUTO_BY_TYPE = {
    "PARTIAL_PAYMENT": (["payment_acknowledgment"], []),
    "CUSTOMER_DISPUTE": (["request_documentation", "internal_followup_task"], ["resolve_customer_dispute"]),
    "PRIVACY_REQUEST": (["policy_reply"], []),
    "AGENT_SHORTFALL": (["field_agent_info_request", "internal_followup_task"], ["resolve_agent_discrepancy"]),
    "DUPLICATE_RECEIPT": (["internal_followup_task"], ["quarantine_duplicate_record"]),
    "DUPLICATE_IMPORT": (["internal_followup_task"], ["quarantine_duplicate_record"]),
    "MESSAGE_SECURITY_FLAG": (["internal_followup_task"], []),
    "UNIDENTIFIED_INFLOW": (["internal_followup_task"], []),
    "PROVIDER_SETTLEMENT_DELAYED": (["internal_followup_task"], []),
}


def action_queue(conn: sqlite3.Connection) -> dict[str, Any]:
    verified = rows(conn, "SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM reconciliation_cases WHERE "
                          "case_type='OVERDUE_FOLLOWUP' AND status='open' AND reminder_suppressed=0")[0]
    held = rows(conn, "SELECT COUNT(*) n FROM reconciliation_cases WHERE case_type='OVERDUE_FOLLOWUP' AND status='open' "
                      "AND reminder_suppressed=1")[0]
    items = []
    for c in queries.list_cases(conn, case_types=tuple(AUTO_BY_TYPE), status="unresolved", limit=40):
        auto, appr = AUTO_BY_TYPE[c["case_type"]]
        done = [o["action_type"] for o in rows(conn, "SELECT action_type FROM outbox WHERE case_id=?", (c["case_id"],))]
        done += [t["task_type"] for t in rows(conn, "SELECT task_type FROM internal_tasks WHERE case_id=?", (c["case_id"],))]
        done += [a["action_type"] + ":" + a["status"] for a in rows(conn, "SELECT action_type, status FROM approvals "
                                                                        "WHERE case_id=?", (c["case_id"],))]
        items.append({"case_id": c["case_id"], "case_type": c["case_type"], "customer_id": c["customer_id"],
                      "agent_id": c["agent_id"], "title": c["title"], "status": c["status"],
                      "allowed_automatic_actions": auto, "approval_required_actions": appr, "already_done": done})
    return {"verified_overdue_cases": int(verified["n"]), "verified_overdue_amount": int(verified["amt"]),
            "overdue_cases_with_reminder_held": int(held["n"]),
            "note": "Routine reminders: call queue_routine_reminders once; held cases are refused automatically.",
            "cases": items}


# ---------------------------------------------------------------------------
# Money Match
# ---------------------------------------------------------------------------
money_match_agent = Agent(None, output_type=ReconciliationDecision, deps_type=Deps, name="money_match",
                          instructions=_instructions("money_match"), retries=2, defer_model_check=True)


@money_match_agent.tool
def get_case_evidence(ctx: RunContext[Deps], case_id: str) -> dict[str, Any]:
    """Compact deterministic evidence for one unmatched-transfer case: the transfer, candidate contributions with
    equal outstanding amount, name/message/timing signals, sanitized messages."""
    return run_tool(ctx.deps, "get_case_evidence", {"case_id": case_id}, lambda conn: money_match_evidence(conn, case_id))


@money_match_agent.tool
def get_customer_payment_history(ctx: RunContext[Deps], customer_id: str) -> dict[str, Any]:
    """On-time / late / missed counts for a customer over the 28-day window (deterministic)."""
    def f(conn):
        check_customer_scope(ctx.deps.scope_customer_id, customer_id)
        return {"customer_id": customer_id, **finance.payment_history_metrics(conn, customer_id)}
    return run_tool(ctx.deps, "get_customer_payment_history", {"customer_id": customer_id}, f)


# ---------------------------------------------------------------------------
# Exception & Risk
# ---------------------------------------------------------------------------
exception_agent = Agent(None, output_type=TriageReport, deps_type=Deps, name="exception_risk",
                        instructions=_instructions("exception_risk"), retries=2, defer_model_check=True)


@exception_agent.tool
def get_exception_queue(ctx: RunContext[Deps], limit: int = 10) -> list[dict[str, Any]]:
    """Unresolved exception cases ranked by deterministic priority score (routine overdue excluded)."""
    limit = max(1, min(int(limit), 15))
    return run_tool(ctx.deps, "get_exception_queue", {"limit": limit}, lambda conn: queries.get_exception_queue(conn, limit))


@exception_agent.tool
def get_case_summary(ctx: RunContext[Deps], case_id: str) -> dict[str, Any]:
    """Compact facts and evidence for one case."""
    return run_tool(ctx.deps, "get_case_summary", {"case_id": case_id}, lambda conn: case_summary(conn, case_id))


@exception_agent.tool
def get_agent_collections(ctx: RunContext[Deps], agent_id: str, day: str | None = None) -> dict[str, Any]:
    """A field agent's reported collections vs bank remittances for a day (default: yesterday)."""
    return run_tool(ctx.deps, "get_agent_collections", {"agent_id": agent_id, "day": day},
                    lambda conn: queries.get_agent_collections(conn, agent_id, day))


# ---------------------------------------------------------------------------
# Liquidity & Payout
# ---------------------------------------------------------------------------
liquidity_agent = Agent(None, output_type=LiquidityBrief, deps_type=Deps, name="liquidity",
                        instructions=_instructions("liquidity"), retries=2, defer_model_check=True)


def _projection_compact(conn: sqlite3.Connection) -> dict[str, Any]:
    p = liquidity.calculate_liquidity_projection(conn)
    p["unresolved_inflows"] = p["unresolved_inflows"][:8]
    p["days"] = [{k: d[k] for k in ("date", "settlements_in_transit", "forecast_contributions", "scheduled_payouts",
                                    "payout_count", "closing_position", "closing_position_if_unresolved_resolve")}
                 for d in p["days"]]
    return p


@liquidity_agent.tool
def calculate_liquidity_projection(ctx: RunContext[Deps]) -> dict[str, Any]:
    """Deterministic 7-day payout-account projection (base case and if unresolved inflows resolve)."""
    return run_tool(ctx.deps, "calculate_liquidity_projection", {}, _projection_compact)


@liquidity_agent.tool
def get_unresolved_inflows(ctx: RunContext[Deps]) -> list[dict[str, Any]]:
    """Open cases holding money that could change the forecast (suspense, late settlements, agent shortfalls)."""
    return run_tool(ctx.deps, "get_unresolved_inflows", {}, liquidity.get_unresolved_inflows)


@liquidity_agent.tool
def calculate_payout_obligations(ctx: RunContext[Deps]) -> dict[str, Any]:
    """Scheduled payouts over the 7-day horizon by day and payout type."""
    return run_tool(ctx.deps, "calculate_payout_obligations", {}, lambda conn: liquidity.calculate_payout_obligations(conn))


# ---------------------------------------------------------------------------
# Customer Action
# ---------------------------------------------------------------------------
customer_action_agent = Agent(None, output_type=ActionPlanResult, deps_type=Deps, name="customer_action",
                              instructions=_instructions("customer_action"), retries=2, defer_model_check=True)


@customer_action_agent.tool
def get_action_queue(ctx: RunContext[Deps]) -> dict[str, Any]:
    """Cases needing action, with the actions policy allows automatically vs only with approval."""
    return run_tool(ctx.deps, "get_action_queue", {}, action_queue)


@customer_action_agent.tool
def get_customer_messages(ctx: RunContext[Deps], customer_id: str) -> list[dict[str, Any]]:
    """Recent messages from one customer (sanitized, untrusted content). Respects the run's customer scope."""
    def f(conn):
        check_customer_scope(ctx.deps.scope_customer_id, customer_id)
        return queries.get_customer_messages(conn, customer_id, 5)
    return run_tool(ctx.deps, "get_customer_messages", {"customer_id": customer_id}, f)


@customer_action_agent.tool
def get_customer_cases(ctx: RunContext[Deps], customer_id: str) -> list[dict[str, Any]]:
    """Open cases for one customer (respects the run's customer scope)."""
    def f(conn):
        check_customer_scope(ctx.deps.scope_customer_id, customer_id)
        return [{k: c[k] for k in ("case_id", "case_type", "status", "title", "amount", "reminder_suppressed")}
                for c in queries.list_cases(conn, customer_id=customer_id, status="unresolved")]
    return run_tool(ctx.deps, "get_customer_cases", {"customer_id": customer_id}, f)


@customer_action_agent.tool
def queue_routine_reminders(ctx: RunContext[Deps], style: str = "auto") -> dict[str, Any]:
    """Queue routine reminders for every verified-overdue case. Cases with payment evidence are refused by code."""
    style = style if style in ("auto", "en", "pcm") else "auto"
    return run_tool(ctx.deps, "queue_routine_reminders", {"style": style},
                    lambda conn: actions.queue_routine_reminders(conn, created_by="agent:customer_action", style=style,
                                                                 run_id=ctx.deps.run_id), write=True)


@customer_action_agent.tool
def send_customer_message(ctx: RunContext[Deps], case_id: str, action_type: str, customer_id: str | None = None,
                          style: str = "auto") -> dict[str, Any]:
    """Queue one low-risk templated message (payment_acknowledgment, request_transfer_reference, request_documentation,
    policy_reply, field_agent_info_request) for a case."""
    def f(conn):
        if gate_for(action_type) != Gate.AUTO_ALLOWED:
            raise SafetyBlock("action_policy", f"'{action_type}' needs human approval or is forbidden.")
        check_customer_scope(ctx.deps.scope_customer_id, customer_id)
        r = actions.create_outbox_message(conn, action_type=action_type, case_id=case_id, customer_id=customer_id,
                                          created_by="agent:customer_action", style=style, run_id=ctx.deps.run_id)
        r.pop("content", None)
        return r
    return run_tool(ctx.deps, "send_customer_message", {"case_id": case_id, "action_type": action_type,
                                                        "customer_id": customer_id, "style": style}, f, write=True)


@customer_action_agent.tool
def create_internal_task(ctx: RunContext[Deps], case_id: str, task_type: str, description: str,
                         assignee: str = "operations.team") -> dict[str, Any]:
    """Create an internal follow-up task for operations staff."""
    return run_tool(ctx.deps, "create_internal_task", {"case_id": case_id, "task_type": task_type},
                    lambda conn: actions.create_internal_task(conn, case_id=case_id, task_type=task_type,
                                                              description=description, assignee=assignee,
                                                              created_by="agent:customer_action", run_id=ctx.deps.run_id),
                    write=True)


@customer_action_agent.tool
def request_approval(ctx: RunContext[Deps], case_id: str, action_type: str, recommendation: str) -> dict[str, Any]:
    """Put a material action in the human approval queue (does not execute it)."""
    return run_tool(ctx.deps, "request_approval", {"case_id": case_id, "action_type": action_type},
                    lambda conn: actions.create_approval_request(conn, case_id=case_id, action_type=action_type,
                                                                 payload={}, recommendation=recommendation,
                                                                 requested_by="agent:customer_action",
                                                                 run_id=ctx.deps.run_id), write=True)


@customer_action_agent.tool
def apply_ledger_change(ctx: RunContext[Deps], customer_id: str, amount: int | None = None,
                        approval_id: str | None = None, reason: str = "") -> dict[str, Any]:
    """Ledger change entry point. Refused by code unless a human has approved the exact change."""
    return run_tool(ctx.deps, "apply_ledger_change", {"customer_id": customer_id, "amount": amount,
                                                      "approval_id": approval_id},
                    lambda conn: actions.apply_ledger_change(conn, actor="agent:customer_action", customer_id=customer_id,
                                                             amount=amount, approval_id=approval_id,
                                                             run_id=ctx.deps.run_id, reason=reason), write=True)
