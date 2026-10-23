"""Supervisor agent and the orchestrated workflows (case review, morning review, Ask)."""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage

from .. import audit, config
from ..database import db, get_state, one, rows, set_state
from ..models import ReconciliationDecision, SupervisorAnswer
from ..safety import SafetyBlock, grounding_check, redact_output
from ..tools import actions, queries
from . import specialists as sp
from .runtime import AIUnavailable, Deps, Totals, Trace, compact, get_model, load_prompt, run_agent, run_tool

SUPERVISOR_INSTRUCTIONS = sp.SAFETY + "\n\n" + load_prompt("supervisor")
PROMPT_FRAGMENTS = [line.strip() for name in ("safety_rules", "supervisor", "money_match", "exception_risk", "liquidity",
                                              "customer_action") for line in load_prompt(name).splitlines()
                    if len(line.strip()) > 60]

supervisor_agent = Agent(None, output_type=SupervisorAnswer, deps_type=Deps, name="supervisor",
                         instructions=SUPERVISOR_INSTRUCTIONS, retries=2, defer_model_check=True)

synth_agent = Agent(None, output_type=SupervisorAnswer, deps_type=Deps, name="supervisor",
                    instructions=SUPERVISOR_INSTRUCTIONS + "\n\nYou are writing the morning brief from specialist outputs "
                    "and deterministic metrics supplied in the message. Do not request tools.",
                    retries=2, defer_model_check=True)


# ---------------------------------------------------------------------------
# Case review: Money Match judgment -> validated -> approval request (code path)
# ---------------------------------------------------------------------------

async def review_case(deps: Deps, case_id: str, *, reuse: bool = True) -> dict[str, Any]:
    with db() as conn:
        case = one(conn, "SELECT * FROM reconciliation_cases WHERE case_id=?", (case_id,))
    if not case:
        return {"error": f"Unknown case {case_id}"}
    if reuse and case["ai_decision_json"]:
        deps.trace.add("cache_hit", "money_match", case_id=case_id, message="Reused stored Money Match decision")
        return {"case_id": case_id, "decision": json.loads(case["ai_decision_json"]), "reused": True}
    if case["case_type"] not in ("PROBABLE_MATCH", "AMBIGUOUS_INFLOW"):
        return {"case_id": case_id, "note": f"{case['case_type']} cases are resolved by deterministic rules, not Money Match."}
    deps.trace.add("handoff", deps.agent_name, to="money_match", case_id=case_id)
    child = deps.child("money_match")
    out, info = await run_agent(sp.money_match_agent, "money_match",
                                f"Judge case {case_id}. Use get_case_evidence('{case_id}') and return your decision.",
                                child, request_limit=4, label=f"case:{case_id}")
    if out is None:
        return {"case_id": case_id, "error": "Money Match could not complete", **info}
    decision: ReconciliationDecision = out
    cand = json.loads(case["candidates_json"] or "{}")
    by_kid = {c["contribution_id"]: c for c in cand.get("candidates", [])}
    supported = [c for c in cand.get("candidates", []) if c["signal_score"] >= 30]
    validator_note = None
    d = decision.model_dump()
    d["candidate_transaction_id"] = case["bank_txn_id"]
    if decision.decision == "recommend_match":
        c = by_kid.get(decision.expected_contribution_id or "")
        if not c or c["customer_id"] != decision.customer_id:
            validator_note = "Validator rejected the proposed match: it is not one of the deterministic candidates."
            d.update(decision="needs_clarification", customer_id=None, expected_contribution_id=None)
        else:
            d["matched_amount"] = c["outstanding"]        # amount comes from SQL, never from the model
    d["approval_required"] = True
    d["validator_note"] = validator_note
    with db() as conn:
        conn.execute("UPDATE reconciliation_cases SET ai_decision_json=?, updated_at=? WHERE case_id=?",
                     (json.dumps(d), actions._now(), case_id))
        try:
            if d["decision"] == "recommend_match":
                ap = actions.create_approval_request(
                    conn, case_id=case_id, action_type="match_payment_to_contribution",
                    payload={"bank_txn_id": case["bank_txn_id"], "contribution_id": d["expected_contribution_id"],
                             "customer_id": d["customer_id"]},
                    recommendation=f"{d['recommended_action']} — {d['reasoning_summary']}",
                    confidence=d["confidence"], requested_by="agent:money_match", run_id=deps.run_id)
            elif d["decision"] == "needs_clarification":
                ap = actions.create_approval_request(
                    conn, case_id=case_id, action_type="clarify_ambiguous_payment",
                    payload={"bank_txn_id": case["bank_txn_id"], "customer_ids": [c["customer_id"] for c in supported]},
                    recommendation=f"Do not match. {d['recommended_action']} — {d['reasoning_summary']}",
                    confidence=d["confidence"], requested_by="agent:money_match", run_id=deps.run_id)
            else:
                ap = actions.create_internal_task(conn, case_id=case_id, task_type="identify_sender",
                                                  description=d["reasoning_summary"], assignee="reconciliation.team",
                                                  created_by="agent:money_match", run_id=deps.run_id)
        except SafetyBlock as e:
            ap = {"blocked": True, "control": e.control, "message": str(e)}
    deps.trace.add("decision", "money_match", case_id=case_id, decision=d["decision"], confidence=d["confidence"],
                   customer_id=d.get("customer_id"), approval=ap, validator_note=validator_note)
    audit.log("agent", "agent:money_match", {"phase": "decision", "case_id": case_id, "decision": d["decision"],
                                             "confidence": d["confidence"], "customer_id": d.get("customer_id"),
                                             "approval": ap}, run_id=deps.run_id, subject=case_id)
    return {"case_id": case_id, "decision": d, "approval": ap, "usage": info}


# ---------------------------------------------------------------------------
# Supervisor tools: direct lookups + delegation to specialists
# ---------------------------------------------------------------------------

@supervisor_agent.tool
def get_portfolio_summary(ctx: RunContext[Deps]) -> dict[str, Any]:
    """Morning control-room metrics computed from SQLite (all amounts in naira)."""
    return run_tool(ctx.deps, "get_portfolio_summary", {}, queries.get_portfolio_summary)


@supervisor_agent.tool
def find_customer(ctx: RunContext[Deps], name_or_id: str) -> list[dict[str, Any]]:
    """Find customers by name or id (returns id, name, location, plan)."""
    return run_tool(ctx.deps, "find_customer", {"q": name_or_id}, lambda conn: queries.find_customers(conn, name_or_id))


@supervisor_agent.tool
def get_customer_overview(ctx: RunContext[Deps], customer_id: str) -> dict[str, Any]:
    """Customer 360 summary: plan, deterministic balance, history metrics, open cases, payouts, recent messages."""
    def f(conn):
        p = queries.get_customer_profile(conn, customer_id)
        if not p:
            raise ValueError(f"Unknown customer {customer_id}")
        p["open_contributions"] = p["open_contributions"][:5]
        return p
    return run_tool(ctx.deps, "get_customer_overview", {"customer_id": customer_id}, f)


@supervisor_agent.tool
def get_case_summary(ctx: RunContext[Deps], case_id: str) -> dict[str, Any]:
    """Compact facts and evidence for one case."""
    return run_tool(ctx.deps, "get_case_summary", {"case_id": case_id}, lambda conn: sp.case_summary(conn, case_id))


@supervisor_agent.tool
def list_cases(ctx: RunContext[Deps], category: str, limit: int = 10) -> dict[str, Any]:
    """List cases by category: 'verified_overdue' (eligible for reminders), 'reminders_held' (ledger unpaid but payment
    evidence or open question), 'exceptions', 'largest_discrepancies', 'agent_cash', 'awaiting_approval'."""
    limit = max(1, min(int(limit), 25))

    def f(conn):
        if category == "verified_overdue":
            r = rows(conn, "SELECT case_id, customer_id, amount, title FROM reconciliation_cases WHERE case_type='OVERDUE_FOLLOWUP' "
                           "AND status IN ('open','reminded') AND reminder_suppressed=0 ORDER BY amount DESC, case_id LIMIT ?", (limit,))
            n = rows(conn, "SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM reconciliation_cases WHERE case_type='OVERDUE_FOLLOWUP' "
                           "AND status IN ('open','reminded') AND reminder_suppressed=0")[0]
        elif category == "reminders_held":
            r = rows(conn, "SELECT case_id, customer_id, amount, title, evidence_json FROM reconciliation_cases WHERE "
                           "case_type='OVERDUE_FOLLOWUP' AND status='open' AND reminder_suppressed=1 ORDER BY priority_score DESC, "
                           "case_id LIMIT ?", (limit,))
            for x in r:
                x["why_held"] = json.loads(x.pop("evidence_json")).get("suppression_reasons", [])[:2]
            n = rows(conn, "SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM reconciliation_cases WHERE case_type='OVERDUE_FOLLOWUP' "
                           "AND status='open' AND reminder_suppressed=1")[0]
        elif category == "largest_discrepancies":
            r = rows(conn, "SELECT case_id, case_type, customer_id, agent_id, amount, title FROM reconciliation_cases WHERE "
                           "case_type IN ('AGENT_SHORTFALL','PROVIDER_SETTLEMENT_DELAYED','CUSTOMER_DISPUTE','AMBIGUOUS_INFLOW',"
                           "'PROBABLE_MATCH','UNIDENTIFIED_INFLOW','DUPLICATE_IMPORT','DUPLICATE_RECEIPT','PARTIAL_PAYMENT') "
                           "AND status NOT IN ('resolved','info') ORDER BY amount DESC LIMIT ?", (limit,))
            n = {"n": len(r), "amt": sum(x["amount"] or 0 for x in r)}
        elif category == "agent_cash":
            r = rows(conn, "SELECT case_id, case_type, agent_id, customer_id, amount, title FROM reconciliation_cases WHERE "
                           "case_type IN ('AGENT_SHORTFALL','DUPLICATE_RECEIPT','CUSTOMER_DISPUTE') AND status NOT IN "
                           "('resolved','info') ORDER BY amount DESC LIMIT ?", (limit,))
            n = {"n": len(r), "amt": sum(x["amount"] or 0 for x in r)}
        elif category == "awaiting_approval":
            r = rows(conn, "SELECT approval_id, case_id, action_type, customer_id, amount, confidence FROM approvals "
                           "WHERE status='pending' ORDER BY created_at LIMIT ?", (limit,))
            n = {"n": len(r), "amt": sum(x["amount"] or 0 for x in r)}
        else:
            r = queries.get_exception_queue(conn, limit)
            n = {"n": len(r), "amt": sum(x["amount"] or 0 for x in r)}
        for x in r:
            if x.get("customer_id"):
                x["customer_name"] = queries.customer_name(conn, x["customer_id"])
        return {"category": category, "total_count": int(n["n"]), "total_amount": int(n["amt"]), "items": r}
    return run_tool(ctx.deps, "list_cases", {"category": category, "limit": limit}, f)


def _delegation_allowed(deps: Deps, to: str) -> dict[str, Any] | None:
    deps.totals.delegations += 1
    if deps.totals.delegations > config.MAX_DELEGATIONS_PER_QUERY:
        deps.trace.add("limit", "supervisor", message=f"Delegation limit reached; {to} not called")
        return {"error": "DELEGATION_LIMIT", "message": "Delegation limit reached for this request. Answer with what you have."}
    deps.trace.add("handoff", "supervisor", to=to)
    return None


def _dump(out: Any) -> Any:
    return out.model_dump() if hasattr(out, "model_dump") else out


@supervisor_agent.tool
async def ask_money_match(ctx: RunContext[Deps], case_id: str) -> dict[str, Any]:
    """Delegate one unmatched-transfer case (PROBABLE_MATCH / AMBIGUOUS_INFLOW) to Money Match. Reuses a stored decision."""
    if (blocked := _delegation_allowed(ctx.deps, "money_match")):
        return blocked
    r = await review_case(ctx.deps, case_id)
    return json.loads(json.dumps(r, default=str))


@supervisor_agent.tool
async def ask_exception_risk(ctx: RunContext[Deps], brief: str) -> dict[str, Any]:
    """Delegate triage of open exceptions to the Exception & Risk agent."""
    if (blocked := _delegation_allowed(ctx.deps, "exception_risk")):
        return blocked
    out, info = await run_agent(sp.exception_agent, "exception_risk", brief[:800], ctx.deps.child("exception_risk"),
                                request_limit=5, label="ask")
    return {"result": _dump(out), "stop_reason": info["stop_reason"]}


@supervisor_agent.tool
async def ask_liquidity(ctx: RunContext[Deps], brief: str) -> dict[str, Any]:
    """Delegate payout-liquidity questions to the Liquidity & Payout agent."""
    if (blocked := _delegation_allowed(ctx.deps, "liquidity")):
        return blocked
    out, info = await run_agent(sp.liquidity_agent, "liquidity", brief[:800], ctx.deps.child("liquidity"),
                                request_limit=4, label="ask")
    return {"result": _dump(out), "stop_reason": info["stop_reason"]}


@supervisor_agent.tool
async def ask_customer_action(ctx: RunContext[Deps], brief: str) -> dict[str, Any]:
    """Delegate next actions (reminders, acknowledgments, requests, tasks, approval requests) to Customer Action."""
    if (blocked := _delegation_allowed(ctx.deps, "customer_action")):
        return blocked
    out, info = await run_agent(sp.customer_action_agent, "customer_action",
                                f"{brief[:800]}\nLanguage style: {ctx.deps.language_style}.",
                                ctx.deps.child("customer_action"), request_limit=8, label="ask")
    return {"result": _dump(out), "stop_reason": info["stop_reason"]}


# ---------------------------------------------------------------------------
# Run wrappers
# ---------------------------------------------------------------------------

def _new_deps(kind: str, mode: str, requested_by: str, query: str, **kw: Any) -> Deps:
    run_id = audit.start_run(kind, mode, requested_by, query)
    return Deps(run_id=run_id, trace=Trace(run_id), totals=Totals(), mode=mode, **kw)


def _finish(deps: Deps, *, status: str, stop_reason: str, answer: str | None, t0: float) -> None:
    audit.finish_run(deps.run_id, status=status, stop_reason=stop_reason, latency_ms=int((time.perf_counter() - t0) * 1000),
                     steps=deps.trace.steps, totals=deps.totals.as_dict(), answer=answer)


def _finalize_answer(deps: Deps, ans: SupervisorAnswer | None) -> dict[str, Any]:
    if ans is None:
        return {"answer": "I could not complete this request within the safety limits. No records were changed.",
                "referenced_case_ids": [], "referenced_customer_ids": [], "specialists_consulted": [],
                "grounding": {"grounded": True, "ungrounded": [], "amounts_found": 0}, "redactions": []}
    text, red = redact_output(ans.answer, PROMPT_FRAGMENTS)
    g = grounding_check(text, deps.tool_outputs)
    if not g["grounded"]:
        text += ("\n\n> ⚠ Grounding check: " + ", ".join(g["ungrounded"]) +
                 " could not be traced to a tool result and should be treated as unverified.")
    deps.trace.add("guardrail", "supervisor", grounding=g, redactions=red)
    return {"answer": text, "referenced_case_ids": ans.referenced_case_ids, "referenced_customer_ids": ans.referenced_customer_ids,
            "specialists_consulted": sorted({s["to"] for s in deps.trace.steps if s["kind"] == "handoff"}),
            "figures_verified": ans.figures_verified and g["grounded"], "grounding": g, "redactions": red}


_SESSIONS: dict[str, list[ModelMessage]] = {}


async def ask(message: str, *, session_id: str | None = None, requested_by: str = "ops.manager",
              language_style: str = "auto", fault_tools: set[str] | None = None, kind: str = "ask",
              mode: str = "optimized") -> dict[str, Any]:
    t0 = time.perf_counter()
    deps = _new_deps(kind, mode, requested_by, message, language_style=language_style, fault_tools=fault_tools or set())
    deps.trace.add("agent_start", "supervisor", prompt=compact(message, 300))
    history = _SESSIONS.get(session_id or "", [])
    if mode == "optimized":
        history = history[-config.CHAT_HISTORY_TURNS * 2:] if history else []
    try:
        get_model()
    except AIUnavailable as e:
        _finish(deps, status="error", stop_reason="ai_unavailable", answer=str(e), t0=t0)
        return {"run_id": deps.run_id, "answer": str(e), "error": "ai_unavailable"}
    stop_reason, status, ans = "completed", "ok", None
    from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
    from pydantic_ai.usage import UsageLimits
    from .runtime import model_settings
    from ..usage import cost_usd
    t_sup = time.perf_counter()
    usage = None
    try:
        result = await supervisor_agent.run(
            message, deps=deps, model=get_model(), message_history=history or None,
            usage_limits=UsageLimits(request_limit=config.MAX_REQUESTS_PER_AGENT_RUN,
                                     tool_calls_limit=config.MAX_TOOL_CALLS_PER_AGENT_RUN),
            model_settings=model_settings("supervisor", mode))
        ans = result.output
        usage = result.usage
        if session_id:
            msgs = result.all_messages()
            _SESSIONS[session_id] = msgs[-12:]
    except UsageLimitExceeded as e:
        stop_reason, status = f"usage_limit: {e}", "limited"
    except ModelHTTPError as e:
        stop_reason, status = f"model_http_error {e.status_code}", "error"
    except UnexpectedModelBehavior as e:
        stop_reason, status = f"unexpected_model_behavior: {str(e)[:200]}", "error"
    lat = int((time.perf_counter() - t_sup) * 1000)
    if usage is not None:
        c = cost_usd(config.MODEL_NAME, usage.input_tokens, usage.cache_read_tokens, usage.output_tokens, usage.cache_write_tokens)
        t = deps.totals
        t.model_requests += usage.requests
        t.input_tokens += usage.input_tokens
        t.cached_input_tokens += usage.cache_read_tokens
        t.cache_write_tokens += usage.cache_write_tokens
        t.output_tokens += usage.output_tokens
        t.cost_usd += c
        audit.record_usage(run_id=deps.run_id, agent="supervisor", mode=mode, model=config.MODEL_NAME,
                           model_requests=usage.requests, input_tokens=usage.input_tokens,
                           cached_input_tokens=usage.cache_read_tokens, output_tokens=usage.output_tokens,
                           latency_ms=lat, cost_usd=c, label=kind)
        deps.trace.add("agent_end", "supervisor", stop_reason=stop_reason, latency_ms=lat, model_requests=usage.requests,
                       input_tokens=usage.input_tokens, cached_input_tokens=usage.cache_read_tokens,
                       output_tokens=usage.output_tokens, cost_usd=c)
    out = _finalize_answer(deps, ans)
    _finish(deps, status=status, stop_reason=stop_reason, answer=out["answer"], t0=t0)
    return {"run_id": deps.run_id, **out, "stop_reason": stop_reason, "totals": deps.totals.as_dict(),
            "latency_ms": int((time.perf_counter() - t0) * 1000)}


async def morning_review(requested_by: str = "ops.manager", language_style: str = "auto") -> dict[str, Any]:
    """Supervisor-orchestrated morning workflow: Money Match on every ambiguous case, then triage, liquidity,
    customer actions, then a synthesized brief. Handoffs are code-driven so the sequence is reproducible."""
    t0 = time.perf_counter()
    deps = _new_deps("morning_review", "optimized", requested_by, "Run the morning AI review", language_style=language_style)
    try:
        get_model()
    except AIUnavailable as e:
        _finish(deps, status="error", stop_reason="ai_unavailable", answer=str(e), t0=t0)
        return {"run_id": deps.run_id, "error": str(e)}
    with db() as conn:
        set_state(conn, "ai_review", {"status": "running", "run_id": deps.run_id})
        targets = [r["case_id"] for r in rows(conn, "SELECT case_id FROM reconciliation_cases WHERE needs_judgment=1 AND "
                                                    "status='open' ORDER BY priority_score DESC, case_id")]
    deps.trace.add("agent_start", "supervisor", prompt="Morning review", plan=["money_match x%d" % len(targets),
                                                                                "exception_risk", "liquidity",
                                                                                "customer_action", "synthesis"])
    decisions = []
    for cid in targets:
        decisions.append(await review_case(deps, cid, reuse=False))
    deps.trace.add("handoff", "supervisor", to="exception_risk")
    triage, _ = await run_agent(sp.exception_agent, "exception_risk",
                                "Rank today's open exceptions for the operations manager. Top 6 items.",
                                deps.child("exception_risk"), request_limit=5, label="morning")
    deps.trace.add("handoff", "supervisor", to="liquidity")
    liq, _ = await run_agent(sp.liquidity_agent, "liquidity", "Assess payout liquidity for the next 7 days.",
                             deps.child("liquidity"), request_limit=4, label="morning")
    deps.trace.add("handoff", "supervisor", to="customer_action")
    act, _ = await run_agent(sp.customer_action_agent, "customer_action",
                             "Process today's action queue: queue routine reminders for verified overdue cases, send the "
                             "allowed low-risk messages for partial payments, disputes and privacy requests, ask field agents "
                             "for information where relevant, create internal tasks for security flags and unidentified "
                             f"transfers, and request approval for discrepancy/dispute/duplicate resolutions. "
                             f"Language style: {language_style}.", deps.child("customer_action"), request_limit=12,
                             label="morning")
    # Deterministic policy router (backstop): every case whose resolution is material gets a human
    # approval item, whether or not the Customer Action agent asked for one.
    routed = []
    with db() as conn:
        for c in rows(conn, "SELECT case_id, case_type, recommended_action FROM reconciliation_cases WHERE status='open' "
                            "AND case_type IN ('AGENT_SHORTFALL','CUSTOMER_DISPUTE','DUPLICATE_IMPORT','DUPLICATE_RECEIPT')"):
            at = {"AGENT_SHORTFALL": "resolve_agent_discrepancy", "CUSTOMER_DISPUTE": "resolve_customer_dispute"}.get(
                c["case_type"], "quarantine_duplicate_record")
            try:
                r = actions.create_approval_request(conn, case_id=c["case_id"], action_type=at, payload={},
                                                    recommendation=c["recommended_action"],
                                                    requested_by="policy:approval-router", run_id=deps.run_id)
                routed.append({"case_id": c["case_id"], **r})
            except SafetyBlock as e:
                routed.append({"case_id": c["case_id"], "blocked": str(e)})
        summary = queries.get_portfolio_summary(conn)
    if routed:
        deps.trace.add("decision", "supervisor", message="Policy router queued approvals for material cases", routed=routed)
    deps.tool_outputs.append(summary)
    brief_input = {"metrics": summary,
                   "money_match": [{"case_id": d.get("case_id"), "decision": (d.get("decision") or {}).get("decision"),
                                    "customer_id": (d.get("decision") or {}).get("customer_id"),
                                    "confidence": (d.get("decision") or {}).get("confidence")} for d in decisions],
                   "triage": _dump(triage), "liquidity": _dump(liq), "actions": _dump(act)}
    deps.trace.add("handoff", "supervisor", to="supervisor_synthesis")
    synth, _ = await run_agent(synth_agent, "supervisor",
                               "Write the morning brief (headline + 5-7 bullets: what needs attention, what was automated, "
                               "what awaits approval, payout risk). Data:\n" + json.dumps(brief_input, default=str)[:12000],
                               deps, request_limit=2, label="morning_synthesis")
    out = _finalize_answer(deps, synth)
    with db() as conn:
        if liq is not None:
            lb = liq.model_dump()
            lb["grounding"] = grounding_check(lb["headline"] + " " + lb["explanation"], deps.tool_outputs)
            lb["run_id"] = deps.run_id
            lb["figures_basis_gap"] = summary["potential_payout_gap"]["amount"]
            set_state(conn, "liquidity_brief", lb)
        if triage is not None:
            set_state(conn, "triage_report", {**triage.model_dump(), "run_id": deps.run_id})
        if act is not None:
            set_state(conn, "action_report", {**act.model_dump(), "run_id": deps.run_id})
        set_state(conn, "ai_review", {"status": "complete", "run_id": deps.run_id, "brief": out["answer"],
                                      "referenced_case_ids": out["referenced_case_ids"],
                                      "decisions": len(decisions), "finished": actions._now()})
    _finish(deps, status="ok", stop_reason="completed", answer=out["answer"], t0=t0)
    return {"run_id": deps.run_id, **out, "decisions": decisions, "totals": deps.totals.as_dict(),
            "latency_ms": int((time.perf_counter() - t0) * 1000)}


async def review_single_case(case_id: str, requested_by: str = "ops.manager") -> dict[str, Any]:
    t0 = time.perf_counter()
    deps = _new_deps("case_review", "optimized", requested_by, f"Money Match review of {case_id}")
    try:
        get_model()
    except AIUnavailable as e:
        _finish(deps, status="error", stop_reason="ai_unavailable", answer=str(e), t0=t0)
        return {"run_id": deps.run_id, "error": str(e)}
    r = await review_case(deps, case_id, reuse=False)
    _finish(deps, status="ok" if "error" not in r else "error", stop_reason="completed",
            answer=json.dumps(r.get("decision"), default=str), t0=t0)
    return {"run_id": deps.run_id, **json.loads(json.dumps(r, default=str)), "totals": deps.totals.as_dict()}
