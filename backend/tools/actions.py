"""Action layer: the only code that writes outbox messages, internal tasks, approval
requests and approved ledger postings.

Controls enforced here, in code:
  * every action type is checked against safety.ACTION_POLICY;
  * a routine reminder is refused unless the case is a verified overdue with no
    suppression reason (no false reminders);
  * material actions can only be *requested* by agents; execution happens in
    decide_approval(), which the HTTP approval endpoint calls with a human actor;
  * the approved posting re-checks the deterministic facts before writing.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from .. import audit, config
from ..database import one, rows, scalar
from ..safety import Gate, OUTBOX_ACTIONS, SafetyBlock, gate_for, mask_phone
from . import finance

FINAL_CASE_STATUSES = ("resolved", "auto_resolved", "info")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _naira(n: int | None) -> str:
    return f"N{int(n or 0):,}"


TEMPLATES = {
    ("routine_overdue_reminder", "en"):
        "Hello {first}, this is Alajo. Our records show your {plan} contribution of {amount} due on {due} has not been "
        "received yet (outstanding: {outstanding}). If you have already paid, please reply with your transfer reference "
        "so we can update your record. Thank you for saving with us.",
    ("routine_overdue_reminder", "pcm"):
        "Hello {first}, na Alajo. We never see your {plan} contribution of {amount} wey suppose land on {due} "
        "(balance: {outstanding}). If you don pay already, abeg reply with your transfer reference make we update am. "
        "Thank you as you dey save with us.",
    ("payment_acknowledgment", "en"):
        "Hello {first}, we have received {received} toward your {plan} contribution due on {due}. {balance_line} "
        "Thank you for saving with Alajo.",
    ("payment_acknowledgment", "pcm"):
        "Hello {first}, we don receive {received} for your {plan} contribution of {due}. {balance_line} "
        "Thank you as you dey save with Alajo.",
    ("request_transfer_reference", "en"):
        "Hello {first}, we received a transfer of {amount} on {date} that we could not link to an account. If you sent "
        "it, please reply with the sender's name and the transfer reference so we can credit the right account.",
    ("request_transfer_reference", "pcm"):
        "Hello {first}, we receive transfer of {amount} for {date} but we no fit link am to any account. If na you send "
        "am, abeg reply with the sender name and transfer reference make we credit the correct account.",
    ("request_documentation", "en"):
        "Hello {first}, thank you for telling us about your cash payment. Please send a photo of the receipt your field "
        "agent gave you (with the receipt number if available). We are checking with the agent and will update you.",
    ("request_documentation", "pcm"):
        "Hello {first}, thank you as you tell us about the cash wey you pay. Abeg send picture of the receipt wey your "
        "agent give you (with receipt number if e dey). We dey check with the agent and we go update you.",
    ("policy_reply", "en"):
        "Hello {first}, for everyone's privacy, Alajo cannot share another customer's contact details or savings "
        "information. If you have a question about your own account, we are happy to help.",
    ("policy_reply", "pcm"):
        "Hello {first}, to protect everybody privacy, Alajo no fit share another customer contact or savings details. "
        "If you get question about your own account, we dey here to help.",
    ("field_agent_info_request", "en"):
        "Hello {agent_name}, please confirm your collections for {day}: reported {reported}, remitted {remitted} "
        "(difference {difference}). Send photos of the receipt book pages and any outstanding remittance reference today.",
}


def _lang(conn: sqlite3.Connection, customer_id: str | None, style: str) -> str:
    if style in ("en", "pcm"):
        return style
    if not customer_id:
        return "en"
    return scalar(conn, "SELECT preferred_language FROM customers WHERE customer_id=?", (customer_id,)) or "en"


def _render(action_type: str, lang: str, **kw: Any) -> str:
    tpl = TEMPLATES.get((action_type, lang)) or TEMPLATES[(action_type, "en")]
    return tpl.format(**kw)


def _customer(conn: sqlite3.Connection, cid: str) -> dict[str, Any]:
    c = one(conn, """SELECT c.customer_id, c.first_name, c.last_name, c.phone, p.name AS plan_name
                     FROM customers c JOIN savings_plans p ON p.plan_id=c.savings_plan_id WHERE c.customer_id=?""", (cid,))
    if not c:
        raise SafetyBlock("validation", f"Unknown customer {cid}")
    return c


# ---------------------------------------------------------------------------
# Low-risk actions (AUTO_ALLOWED)
# ---------------------------------------------------------------------------

def create_outbox_message(conn: sqlite3.Connection, *, action_type: str, case_id: str, created_by: str,
                          customer_id: str | None = None, style: str = "auto", automated: bool = True,
                          approval_id: str | None = None, run_id: str | None = None,
                          extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if action_type not in OUTBOX_ACTIONS:
        raise SafetyBlock("action_policy", f"'{action_type}' is not an outbox action.")
    if automated and gate_for(action_type) != Gate.AUTO_ALLOWED:
        raise SafetyBlock("action_policy", f"'{action_type}' cannot be sent automatically.")
    case = one(conn, "SELECT * FROM reconciliation_cases WHERE case_id=?", (case_id,))
    if not case:
        raise SafetyBlock("validation", f"Unknown case {case_id}")
    cid = customer_id or case["customer_id"]
    # Eligibility checks: the reason a message may be sent must be true in the data.
    if action_type == "routine_overdue_reminder":
        if case["case_type"] != "OVERDUE_FOLLOWUP":
            raise SafetyBlock("reminder_eligibility", f"{case_id} is a {case['case_type']} case, not a verified overdue.")
        if case["reminder_suppressed"]:
            ev = json.loads(case["evidence_json"])
            raise SafetyBlock("reminder_eligibility",
                              f"Reminder blocked for {case_id}: " + "; ".join(ev.get("suppression_reasons", [])),
                              {"case_id": case_id})
        if case["status"] != "open":
            raise SafetyBlock("reminder_eligibility", f"{case_id} is {case['status']}; reminder not needed.")
        if cid != case["customer_id"]:
            raise SafetyBlock("privacy_scope", "Reminder recipient must be the case customer.")
        bal = finance.customer_balance(conn, cid)
        if bal["overdue_outstanding"] <= 0:
            raise SafetyBlock("reminder_eligibility", "Ledger shows nothing overdue now.")
    if action_type == "request_transfer_reference":
        if case["case_type"] not in ("AMBIGUOUS_INFLOW", "PROBABLE_MATCH", "UNIDENTIFIED_INFLOW"):
            raise SafetyBlock("reminder_eligibility", "Reference requests are only for unmatched transfers.")
        cands = json.loads(case["candidates_json"] or "{}").get("candidates", [])
        if cid not in {c["customer_id"] for c in cands if c["signal_score"] >= 30}:
            raise SafetyBlock("privacy_scope", "Recipient is not a supported candidate for this transfer.")
    if action_type == "request_documentation" and case["case_type"] != "CUSTOMER_DISPUTE":
        raise SafetyBlock("reminder_eligibility", "Documentation requests are for disputes.")
    if action_type == "policy_reply" and case["case_type"] != "PRIVACY_REQUEST":
        raise SafetyBlock("reminder_eligibility", "Policy replies are for privacy requests.")
    if action_type == "payment_acknowledgment" and not approval_id and case["case_type"] != "PARTIAL_PAYMENT":
        raise SafetyBlock("reminder_eligibility", "Acknowledgments confirm money already recorded in the ledger.")

    dup = one(conn, "SELECT message_id FROM outbox WHERE case_id=? AND action_type=? AND COALESCE(customer_id,'')=?",
              (case_id, action_type, cid or ""))
    if dup:
        return {"message_id": dup["message_id"], "status": "already_queued"}

    lang = _lang(conn, cid, style)
    if action_type == "field_agent_info_request":
        ev = json.loads(case["evidence_json"])
        agent = one(conn, "SELECT full_name, phone_masked FROM field_agents WHERE agent_id=?", (case["agent_id"],))
        content = _render(action_type, "en", agent_name=agent["full_name"], day=ev.get("day"),
                          reported=_naira(ev.get("reported_total")), remitted=_naira(case["received_amount"]),
                          difference=_naira(ev.get("shortfall")))
        recipient, channel, lang = f"{agent['full_name']} ({case['agent_id']}) {agent['phone_masked']}", "field_app", "en"
        cid = None
    else:
        c = _customer(conn, cid)
        recipient, channel = f"{c['first_name']} {c['last_name']} ({cid}) {mask_phone(c['phone'])}", "whatsapp"
        kw: dict[str, Any] = {"first": c["first_name"], "plan": c["plan_name"]}
        if action_type == "routine_overdue_reminder":
            opens = [o for o in finance.open_contributions(conn, cid) if o["days_late"] > config.GRACE_DAYS]
            kw.update(amount=_naira(opens[0]["expected_amount"]), due=opens[0]["due_date"],
                      outstanding=_naira(sum(o["outstanding"] for o in opens)))
        elif action_type == "payment_acknowledgment":
            info = extra or {}
            kid = info.get("contribution_id") or (json.loads(case["contribution_ids"]) or [None])[0]
            due = scalar(conn, "SELECT due_date FROM expected_contributions WHERE contribution_id=?", (kid,))
            out = finance.contribution_outstanding(conn, kid)
            received = info.get("received", case["received_amount"])
            bl = (f"Remaining balance for that contribution: {_naira(out)}." if out > 0 else
                  "That contribution is now fully recorded.") if lang == "en" else (
                f"Balance wey remain: {_naira(out)}." if out > 0 else "That contribution don complete.")
            kw.update(received=_naira(received), due=due, balance_line=bl)
        elif action_type == "request_transfer_reference":
            t = one(conn, "SELECT amount, txn_time FROM bank_transactions WHERE bank_txn_id=?", (case["bank_txn_id"],))
            kw.update(amount=_naira(t["amount"]), date=t["txn_time"][:10])
        content = _render(action_type, lang, **kw)
    mid = _id("OB")
    reason = {"routine_overdue_reminder": "Verified overdue contribution",
              "payment_acknowledgment": "Payment recorded in ledger",
              "request_transfer_reference": "Unmatched transfer needs sender confirmation",
              "request_documentation": "Customer dispute needs receipt evidence",
              "policy_reply": "Declining request for another customer's information",
              "field_agent_info_request": "Agent cash shortfall investigation"}[action_type]
    conn.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (mid, cid, recipient, channel, action_type, reason, content, lang, case_id, approval_id, int(automated),
                  "queued", created_by, run_id, _now()))
    if action_type == "routine_overdue_reminder":
        conn.execute("UPDATE reconciliation_cases SET status='reminded', updated_at=? WHERE case_id=?", (_now(), case_id))
    audit.log("outbox", created_by, {"message_id": mid, "action_type": action_type, "case_id": case_id,
                                     "customer_id": cid, "automated": automated, "language": lang,
                                     "approval_id": approval_id}, run_id=run_id, subject=case_id)
    return {"message_id": mid, "status": "queued", "action_type": action_type, "recipient": recipient, "language": lang,
            "content": content}


def queue_routine_reminders(conn: sqlite3.Connection, *, created_by: str, style: str = "auto", run_id: str | None = None,
                            case_ids: list[str] | None = None, limit: int = 1000) -> dict[str, Any]:
    """Batch tool: send reminders for every verified overdue case (or the listed ones). Suppressed cases are refused."""
    if case_ids:
        targets = case_ids[:limit]
    else:
        targets = [r["case_id"] for r in rows(conn, "SELECT case_id FROM reconciliation_cases WHERE case_type='OVERDUE_FOLLOWUP' "
                                                    "AND status='open' AND reminder_suppressed=0 ORDER BY priority_score DESC, "
                                                    "case_id LIMIT ?", (limit,))]
    sent, blocked = [], []
    for cid in targets:
        try:
            r = create_outbox_message(conn, action_type="routine_overdue_reminder", case_id=cid, created_by=created_by,
                                      style=style, run_id=run_id)
            sent.append(r["message_id"])
        except SafetyBlock as e:
            blocked.append({"case_id": cid, "reason": str(e)})
            audit.log("safety_block", created_by, {"control": e.control, "case_id": cid, "reason": str(e)},
                      run_id=run_id, subject=cid)
    held = int(scalar(conn, "SELECT COUNT(*) FROM reconciliation_cases WHERE case_type='OVERDUE_FOLLOWUP' AND status='open' "
                            "AND reminder_suppressed=1") or 0)
    return {"reminders_queued": len(sent), "message_ids_sample": sent[:5], "blocked": blocked[:10],
            "blocked_count": len(blocked), "reminders_held_for_evidence": held}


def create_internal_task(conn: sqlite3.Connection, *, case_id: str, task_type: str, description: str, assignee: str,
                         created_by: str, automated: bool = True, run_id: str | None = None) -> dict[str, Any]:
    if not one(conn, "SELECT 1 FROM reconciliation_cases WHERE case_id=?", (case_id,)):
        raise SafetyBlock("validation", f"Unknown case {case_id}")
    dup = one(conn, "SELECT task_id FROM internal_tasks WHERE case_id=? AND task_type=?", (case_id, task_type))
    if dup:
        return {"task_id": dup["task_id"], "status": "already_open"}
    tid = _id("TK")
    conn.execute("INSERT INTO internal_tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (tid, case_id, assignee[:60], task_type[:60], description[:500], "open", int(automated), created_by, run_id,
                  _now()))
    audit.log("outbox", created_by, {"task_id": tid, "case_id": case_id, "task_type": task_type, "assignee": assignee},
              run_id=run_id, subject=case_id)
    return {"task_id": tid, "status": "open"}


# ---------------------------------------------------------------------------
# Material actions: request only (APPROVAL_REQUIRED)
# ---------------------------------------------------------------------------

def create_approval_request(conn: sqlite3.Connection, *, case_id: str, action_type: str, payload: dict[str, Any],
                            recommendation: str, requested_by: str, confidence: float | None = None,
                            run_id: str | None = None) -> dict[str, Any]:
    if gate_for(action_type) == Gate.FORBIDDEN:
        raise SafetyBlock("action_policy", f"'{action_type}' is forbidden in this system.")
    if gate_for(action_type) != Gate.APPROVAL_REQUIRED:
        raise SafetyBlock("action_policy", f"'{action_type}' does not use the approval queue.")
    case = one(conn, "SELECT * FROM reconciliation_cases WHERE case_id=?", (case_id,))
    if not case:
        raise SafetyBlock("validation", f"Unknown case {case_id}")
    existing = one(conn, "SELECT approval_id FROM approvals WHERE case_id=? AND action_type=? AND status='pending'",
                   (case_id, action_type))
    if existing:
        return {"approval_id": existing["approval_id"], "status": "already_pending"}
    # Deterministic facts attached by code, never by the model.
    evidence = {"case_title": case["title"], "case_summary": case["summary"],
                "deterministic_evidence": json.loads(case["evidence_json"])}
    amount = case["amount"]
    if action_type == "match_payment_to_contribution":
        kid = payload.get("contribution_id")
        t = one(conn, "SELECT * FROM bank_transactions WHERE bank_txn_id=?", (payload.get("bank_txn_id"),))
        if not t or t["recon_status"] != "suspense":
            raise SafetyBlock("validation", "Transfer is not in suspense.")
        cands = json.loads(case["candidates_json"] or "{}").get("candidates", [])
        if kid not in {c["contribution_id"] for c in cands}:
            raise SafetyBlock("validation", "Proposed contribution is not a deterministic candidate for this transfer.")
        if finance.contribution_outstanding(conn, kid) != t["amount"]:
            raise SafetyBlock("validation", "Transfer amount does not equal the contribution's outstanding amount.")
        payload = {"bank_txn_id": t["bank_txn_id"], "contribution_id": kid, "customer_id": payload.get("customer_id"),
                   "amount": t["amount"]}
        amount = t["amount"]
    reason = {
        "match_payment_to_contribution": "Matching a transfer without an exact reference changes a customer's balance.",
        "clarify_ambiguous_payment": "Evidence supports more than one customer; a human decides how to resolve.",
        "resolve_agent_discrepancy": "Field-agent cash discrepancies need a human investigation.",
        "resolve_customer_dispute": "Customer and agent records conflict about money received.",
        "quarantine_duplicate_record": "Excluding a source record from the books is a financial control decision.",
        "adjust_customer_balance": "Direct balance changes always require human approval.",
        "mark_contribution_satisfied": "Changing a contribution's status changes the customer's balance.",
    }.get(action_type, "Material financial or account-changing action.")
    aid = _id("AP")
    conn.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (aid, case_id, action_type, payload.get("customer_id") or case["customer_id"], amount,
                  json.dumps(payload), json.dumps(evidence, default=str), recommendation[:600], confidence, reason, "pending",
                  requested_by, run_id, _now(), None, None, None, None))
    if case["status"] == "open":
        conn.execute("UPDATE reconciliation_cases SET status='awaiting_approval', updated_at=? WHERE case_id=?",
                     (_now(), case_id))
    audit.log("approval", requested_by, {"phase": "requested", "approval_id": aid, "action_type": action_type,
                                         "case_id": case_id, "amount": amount, "confidence": confidence},
              run_id=run_id, subject=case_id)
    return {"approval_id": aid, "status": "pending", "reason_required": reason}


def apply_ledger_change(conn: sqlite3.Connection, *, actor: str, customer_id: str, amount: int | None = None,
                        approval_id: str | None = None, run_id: str | None = None, reason: str = "") -> dict[str, Any]:
    """The only ledger-change entry point reachable by an agent. It refuses unless a human has
    already approved the exact change; approved changes are executed by decide_approval()."""
    if not approval_id:
        raise SafetyBlock("approval_gate", "Ledger changes require an approval_id approved by a human. "
                          "Create an approval request instead.", {"customer_id": customer_id, "amount": amount})
    a = one(conn, "SELECT * FROM approvals WHERE approval_id=?", (approval_id,))
    if not a or a["status"] != "approved" or not (a["decided_by"] or "").startswith("human:"):
        raise SafetyBlock("approval_gate", f"Approval {approval_id} is not approved by a human.",
                          {"approval_id": approval_id})
    if a["customer_id"] != customer_id:
        raise SafetyBlock("approval_gate", "Approval does not cover this customer.")
    return {"status": "already_executed", "execution_result": a["execution_result"]}


# ---------------------------------------------------------------------------
# Human decision (called only by the approval HTTP endpoint)
# ---------------------------------------------------------------------------

def decide_approval(conn: sqlite3.Connection, approval_id: str, *, decision: str, decided_by: str,
                    note: str | None = None) -> dict[str, Any]:
    if not decided_by.startswith("human:"):
        raise SafetyBlock("approval_gate", "Only a human operator can decide approvals.")
    a = one(conn, "SELECT * FROM approvals WHERE approval_id=?", (approval_id,))
    if not a:
        raise SafetyBlock("validation", f"Unknown approval {approval_id}")
    if a["status"] != "pending":
        raise SafetyBlock("validation", f"Approval {approval_id} is already {a['status']}.")
    now = _now()
    result: dict[str, Any] = {}
    if decision == "reject":
        conn.execute("UPDATE approvals SET status='rejected', decided_by=?, decided_at=?, decision_note=? WHERE approval_id=?",
                     (decided_by, now, note, approval_id))
        conn.execute("UPDATE reconciliation_cases SET status='open', updated_at=?, resolution_note=? WHERE case_id=? "
                     "AND status='awaiting_approval'", (now, f"Approval {approval_id} rejected: {note or ''}", a["case_id"]))
        result = {"status": "rejected"}
    else:
        result = _execute(conn, a, decided_by)
        conn.execute("UPDATE approvals SET status='approved', decided_by=?, decided_at=?, decision_note=?, execution_result=? "
                     "WHERE approval_id=?", (decided_by, now, note, json.dumps(result), approval_id))
    audit.log("approval", decided_by, {"phase": "decided", "approval_id": approval_id, "decision": decision,
                                       "action_type": a["action_type"], "case_id": a["case_id"], "amount": a["amount"],
                                       "note": note, "result": result}, subject=a["case_id"])
    return {"approval_id": approval_id, "decision": decision, "result": result}


def _execute(conn: sqlite3.Connection, a: dict[str, Any], decided_by: str) -> dict[str, Any]:
    payload = json.loads(a["payload_json"])
    case_id = a["case_id"]
    now = _now()
    at = a["action_type"]
    if at == "match_payment_to_contribution":
        t = one(conn, "SELECT * FROM bank_transactions WHERE bank_txn_id=?", (payload["bank_txn_id"],))
        kid = payload["contribution_id"]
        # re-verify deterministic facts at execution time
        if not t or t["recon_status"] != "suspense":
            raise SafetyBlock("validation", "Transfer is no longer in suspense; nothing posted.")
        out = finance.contribution_outstanding(conn, kid)
        if out != t["amount"]:
            raise SafetyBlock("validation", f"Outstanding is now {out}, transfer is {t['amount']}; nothing posted.")
        cid = scalar(conn, "SELECT customer_id FROM expected_contributions WHERE contribution_id=?", (kid,))
        conn.execute("INSERT INTO ledger_entries(customer_id, contribution_id, amount, entry_time, source_type, source_ref, "
                     "posted_by, approval_id) VALUES (?,?,?,?,?,?,?,?)",
                     (cid, kid, t["amount"], now, "approved_match", t["bank_txn_id"], decided_by, a["approval_id"]))
        finance.refresh_status(conn, kid)
        conn.execute("UPDATE bank_transactions SET recon_status='approved_match' WHERE bank_txn_id=?", (t["bank_txn_id"],))
        conn.execute("UPDATE reconciliation_cases SET status='resolved', updated_at=?, resolution_note=? WHERE case_id=?",
                     (now, f"Matched {t['bank_txn_id']} to {kid} (approval {a['approval_id']})", case_id))
        audit.log("mutation", decided_by, {"action": "ledger_post", "customer_id": cid, "contribution_id": kid,
                                           "amount": t["amount"], "source_ref": t["bank_txn_id"],
                                           "approval_id": a["approval_id"]}, subject=case_id)
        # close the customer's overdue case if nothing remains overdue
        bal = finance.customer_balance(conn, cid)
        for oc in rows(conn, "SELECT case_id FROM reconciliation_cases WHERE customer_id=? AND case_type='OVERDUE_FOLLOWUP' "
                             "AND status IN ('open','reminded')", (cid,)):
            if bal["overdue_outstanding"] == 0:
                conn.execute("UPDATE reconciliation_cases SET status='resolved', updated_at=?, resolution_note=? WHERE case_id=?",
                             (now, f"Paid via approved match {a['approval_id']}", oc["case_id"]))
        ack = create_outbox_message(conn, action_type="payment_acknowledgment", case_id=case_id, customer_id=cid,
                                    created_by=decided_by, automated=False, approval_id=a["approval_id"],
                                    extra={"contribution_id": kid, "received": t["amount"]})
        return {"posted": True, "ledger_amount": t["amount"], "contribution_id": kid, "customer_id": cid,
                "contribution_status": scalar(conn, "SELECT status FROM expected_contributions WHERE contribution_id=?", (kid,)),
                "acknowledgment_message_id": ack["message_id"]}
    if at == "clarify_ambiguous_payment":
        sent = []
        for cid in payload.get("customer_ids", []):
            r = create_outbox_message(conn, action_type="request_transfer_reference", case_id=case_id, customer_id=cid,
                                      created_by=decided_by, automated=False, approval_id=a["approval_id"])
            sent.append(r["message_id"])
        conn.execute("UPDATE reconciliation_cases SET status='awaiting_customer', updated_at=? WHERE case_id=?", (now, case_id))
        return {"posted": False, "clarification_messages": sent, "funds": "remain in suspense"}
    if at in ("resolve_agent_discrepancy", "resolve_customer_dispute"):
        t = create_internal_task(conn, case_id=case_id, task_type="investigation", assignee="field.operations.lead",
                                 description=f"Investigate per approval {a['approval_id']}: {a['recommendation'][:300]}",
                                 created_by=decided_by, automated=False)
        conn.execute("UPDATE reconciliation_cases SET status='under_investigation', updated_at=? WHERE case_id=?", (now, case_id))
        return {"posted": False, "investigation_task": t["task_id"]}
    if at == "quarantine_duplicate_record":
        conn.execute("UPDATE reconciliation_cases SET status='resolved', updated_at=?, resolution_note=? WHERE case_id=?",
                     (now, f"Duplicate quarantine confirmed (approval {a['approval_id']})", case_id))
        return {"posted": False, "duplicate_quarantine_confirmed": True}
    if at in ("adjust_customer_balance", "mark_contribution_satisfied"):
        kid = payload.get("contribution_id")
        out = finance.contribution_outstanding(conn, kid) if kid else 0
        amt = int(payload.get("amount") or out)
        if not kid or amt <= 0 or amt > out:
            raise SafetyBlock("validation", "Adjustment must target an open contribution and not exceed its outstanding amount.")
        cid = scalar(conn, "SELECT customer_id FROM expected_contributions WHERE contribution_id=?", (kid,))
        conn.execute("INSERT INTO ledger_entries(customer_id, contribution_id, amount, entry_time, source_type, source_ref, "
                     "posted_by, approval_id) VALUES (?,?,?,?,?,?,?,?)",
                     (cid, kid, amt, now, "approved_match", "manual-adjustment", decided_by, a["approval_id"]))
        finance.refresh_status(conn, kid)
        audit.log("mutation", decided_by, {"action": "ledger_adjustment", "contribution_id": kid, "amount": amt,
                                           "approval_id": a["approval_id"]}, subject=case_id)
        return {"posted": True, "ledger_amount": amt, "contribution_id": kid}
    raise SafetyBlock("action_policy", f"No execution path for '{at}'.")
