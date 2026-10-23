"""Deterministic morning reconciliation engine.

Order of rules (each step only sees records the previous steps left open):
  1. Duplicate bank-import detection         -> DUPLICATE_IMPORT cases, duplicates quarantined
  2. Duplicate field-agent receipt detection -> DUPLICATE_RECEIPT cases, duplicates excluded
  3. Field-agent remittance balancing        -> balanced batches posted (rule R3) or AGENT_SHORTFALL
  4. Exact bank reference match (ALJ-Cxxxxx) -> posted FIFO (rule R1); PARTIAL_PAYMENT / OVERPAYMENT cases
  5. Provider settlements                    -> settled posted (rule R2); pending marked in transit;
                                                past-SLA batches -> PROVIDER_SETTLEMENT_DELAYED
  6. Remaining bank transfers                -> UNMATCHED_INFLOW with deterministic candidate signals
  7. Customer messages                       -> intents, injection flags, DISPUTE / SECURITY / PRIVACY cases
  8. Overdue detection                       -> OVERDUE_FOLLOWUP per customer, with reminder suppression
                                                whenever any payment evidence or open question exists

Only exact, single-interpretation evidence is posted automatically. Everything
else becomes a case. The engine never calls a model.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from .. import config
from ..database import one, rows, scalar
from ..safety import detect_injection, sanitize_untrusted
from . import finance

REF_RE = re.compile(r"\bALJ-(C\d{5})\b", re.IGNORECASE)
REMIT_RE = re.compile(r"^REMIT (AG-\d{2}) (\d{2})([A-Z]{3})$")

TYPE_BASE = {
    "PROVIDER_SETTLEMENT_DELAYED": 72, "AGENT_SHORTFALL": 74, "CUSTOMER_DISPUTE": 62, "AMBIGUOUS_INFLOW": 60,
    "MESSAGE_SECURITY_FLAG": 58, "PROBABLE_MATCH": 52, "DUPLICATE_IMPORT": 50, "DUPLICATE_RECEIPT": 46,
    "PRIVACY_REQUEST": 44, "UNIDENTIFIED_INFLOW": 38, "OVERPAYMENT": 34, "PARTIAL_PAYMENT": 30,
    "OVERDUE_FOLLOWUP": 18, "PROVIDER_SETTLEMENT_PENDING": 8,
}
TYPE_ORDER = list(TYPE_BASE)


def _priority(case_type: str, amount: int, payout_sensitive: bool, days: int = 0) -> tuple[int, str]:
    s = TYPE_BASE[case_type]
    s += min(15, int((amount or 0) / 100_000 * 5))
    if payout_sensitive:
        s += 12
    if case_type == "OVERDUE_FOLLOWUP":
        s += min(24, days * 3)
    s = min(100, s)
    label = "critical" if s >= 80 else "high" if s >= 60 else "medium" if s >= 40 else "low"
    return s, label


def _tokens(name: str) -> list[str]:
    return [t for t in re.sub(r"[^A-Z ]", " ", name.upper()).split() if t]


def name_signal(sender: str, first: str, last: str) -> str:
    toks = _tokens(sender)
    f, l = first.upper(), last.upper()
    if f in toks and l in toks:
        return "full_name"
    if l in toks and any(len(t) == 1 and t == f[0] for t in toks):
        return "surname_and_initial"
    if l in toks:
        return "surname_only"
    if f in toks:
        return "first_name_only"
    return "none"


NAME_POINTS = {"full_name": 60, "surname_and_initial": 35, "surname_only": 20, "first_name_only": 10, "none": 0}


def classify_message(content: str) -> str:
    t = content.lower()
    if detect_injection(content):
        return "embedded_instruction"
    if re.search(r"(phone number|how much (she|he) has saved|account details|balance of|send me [a-z]+ [a-z]+'s)", t):
        return "third_party_data_request"
    if ("cash" in t or "gave" in t) and "agent" in t:
        return "cash_to_agent_claim"
    if re.search(r"\b(have paid|i paid|i don pay|paid this|transferred|used my .* account)\b", t):
        return "payment_claim"
    if "payout" in t or "money go land" in t or "cycle" in t:
        return "payout_question"
    if "will pay" in t:
        return "promise_to_pay"
    return "general"


def find_candidate_payment_matches(conn: sqlite3.Connection, bank_txn_id: str,
                                   blocked_contributions: set[str] | None = None) -> dict[str, Any]:
    """Deterministic candidate generation for an unreferenced transfer: same outstanding amount,
    due within the window, ranked by name and message signals."""
    t = one(conn, "SELECT * FROM bank_transactions WHERE bank_txn_id = ?", (bank_txn_id,))
    if not t:
        raise ValueError(f"Unknown bank transaction {bank_txn_id}")
    tdate = date.fromisoformat(t["txn_time"][:10])
    lo = (tdate - timedelta(days=config.CANDIDATE_WINDOW_BEFORE)).isoformat()
    hi = (tdate + timedelta(days=config.CANDIDATE_WINDOW_AFTER)).isoformat()
    cands = rows(conn, """
        SELECT c.contribution_id, c.customer_id, c.due_date, c.expected_amount,
               COALESCE((SELECT SUM(amount) FROM ledger_entries l WHERE l.contribution_id = c.contribution_id),0) AS paid,
               cu.first_name, cu.last_name, cu.location, cu.savings_plan_id
        FROM expected_contributions c JOIN customers cu ON cu.customer_id = c.customer_id
        WHERE c.status != 'paid' AND c.due_date BETWEEN ? AND ?""", (lo, hi))
    blocked = blocked_contributions or set()
    sender_toks = [x for x in _tokens(t["sender_name"]) if len(x) > 2]
    out = []
    for c in cands:
        outstanding = c["expected_amount"] - c["paid"]
        if outstanding != t["amount"] or c["contribution_id"] in blocked:
            continue
        sig = name_signal(t["sender_name"], c["first_name"], c["last_name"])
        msgs = rows(conn, "SELECT message_id, msg_time, content, intent FROM customer_messages WHERE customer_id = ? "
                          "AND msg_time >= ? ORDER BY msg_time", (c["customer_id"], (tdate - timedelta(days=2)).isoformat()))
        names_sender = None
        claim = None
        for m in msgs:
            up = m["content"].upper()
            if sender_toks and all(tok in up for tok in sender_toks) and not detect_injection(m["content"]):
                names_sender = m["message_id"]
            if classify_message(m["content"]) in ("payment_claim",):
                claim = m["message_id"]
        gap = (tdate - date.fromisoformat(c["due_date"])).days
        score = NAME_POINTS[sig] + (50 if names_sender else 0) + (10 if claim else 0) + (10 if 0 <= gap <= 2 else 0)
        out.append({
            "contribution_id": c["contribution_id"], "customer_id": c["customer_id"],
            "customer_name": f"{c['first_name']} {c['last_name']}", "due_date": c["due_date"],
            "outstanding": outstanding, "amount_matches_outstanding": True,
            "sender_name_signal": sig, "message_names_sender": names_sender, "payment_claim_message": claim,
            "days_between_due_and_transfer": gap, "signal_score": score,
        })
    out.sort(key=lambda x: (-x["signal_score"], x["due_date"], x["customer_id"]))
    with_signal = [c for c in out if c["signal_score"] >= 30]
    if not with_signal:
        assessment = "unidentified"
    elif len(with_signal) == 1 or with_signal[0]["signal_score"] - with_signal[1]["signal_score"] >= 30:
        assessment = "single_supported_candidate"
    else:
        assessment = "multiple_equally_supported_candidates"
    return {"bank_txn": {k: t[k] for k in ("bank_txn_id", "txn_time", "sender_name", "amount", "reference", "import_batch")},
            "candidate_count_same_amount": len(out), "candidates": out[: config.MAX_CANDIDATES],
            "candidates_with_supporting_signal": len(with_signal), "deterministic_assessment": assessment}


def run_morning_reconciliation(conn: sqlite3.Connection) -> dict[str, Any]:
    today = config.DEMO_TODAY
    run_time = config.DEMO_NOW.strftime("%Y-%m-%dT%H:%M:%S")
    cases: list[dict[str, Any]] = []
    suppress: dict[str, list[str]] = defaultdict(list)      # customer -> reasons not to auto-remind
    blocked_contribs: set[str] = set()                      # covered by in-transit/held evidence
    stats = defaultdict(int)
    payout_customers = {r["customer_id"] for r in rows(conn, "SELECT DISTINCT customer_id FROM payouts WHERE status='scheduled'")}
    cust = {r["customer_id"]: r for r in rows(conn, "SELECT customer_id, first_name, last_name, location, agent_id, "
                                                    "savings_plan_id, preferred_language FROM customers")}

    def name(cid: str | None) -> str:
        return f"{cust[cid]['first_name']} {cust[cid]['last_name']}" if cid and cid in cust else "Unknown"

    def add_case(case_type: str, *, title: str, summary: str, evidence: dict, customer_id: str | None = None,
                 contribution_ids: list[str] | None = None, bank_txn_id: str | None = None, agent_id: str | None = None,
                 amount: int | None = None, expected_amount: int | None = None, received_amount: int | None = None,
                 source: str | None = None, candidates: dict | None = None, needs_judgment: bool = False,
                 recommended_action: str = "", status: str = "open", days: int = 0, sort_key: str = "") -> None:
        score, label = _priority(case_type, amount or 0, bool(customer_id and customer_id in payout_customers), days)
        cases.append(dict(case_type=case_type, status=status, priority_score=score, priority=label,
                          customer_id=customer_id, contribution_ids=json.dumps(contribution_ids or []),
                          bank_txn_id=bank_txn_id, agent_id=agent_id, amount=amount, expected_amount=expected_amount,
                          received_amount=received_amount, source=source, title=title, summary=summary,
                          evidence_json=json.dumps(evidence, default=str),
                          candidates_json=json.dumps(candidates, default=str) if candidates else None,
                          needs_judgment=int(needs_judgment), recommended_action=recommended_action,
                          _sort=(TYPE_ORDER.index(case_type), sort_key or (customer_id or bank_txn_id or agent_id or ""))))

    # 1. duplicate bank imports ------------------------------------------------------------
    for g in rows(conn, """SELECT sender_name, amount, reference, txn_time, GROUP_CONCAT(bank_txn_id) AS ids, COUNT(*) AS n
                           FROM bank_transactions WHERE recon_status = 'new'
                           GROUP BY sender_name, amount, reference, txn_time HAVING n > 1"""):
        ids = sorted(g["ids"].split(","))
        keep, dups = ids[0], ids[1:]
        recs = rows(conn, "SELECT bank_txn_id, txn_time, sender_name, amount, reference, import_batch FROM bank_transactions "
                          "WHERE bank_txn_id IN (%s)" % ",".join("?" * len(ids)), tuple(ids))
        for d in dups:
            conn.execute("UPDATE bank_transactions SET recon_status='duplicate', duplicate_of=? WHERE bank_txn_id=?", (keep, d))
        m = REF_RE.search(g["reference"])
        cid = m.group(1).upper() if m else None
        stats["duplicate_imports"] += len(dups)
        add_case("DUPLICATE_IMPORT", customer_id=cid, bank_txn_id=dups[0], amount=g["amount"] * len(dups),
                 received_amount=g["amount"], source="bank import",
                 title=f"Duplicate bank import: {g['sender_name'].title()} N{g['amount']:,}",
                 summary=f"The same transfer appears in {len(ids)} import files. {keep} is kept and posted once; "
                         f"{', '.join(dups)} is quarantined so the customer is not double-credited.",
                 evidence={"records": recs, "identical_fields": ["sender_name", "amount", "reference", "txn_time"],
                           "kept_record": keep, "quarantined_records": dups},
                 recommended_action="Confirm quarantine of the duplicate import record (no credit).", status="resolved"
                 if False else "open")

    # 2. duplicate field-agent receipts ----------------------------------------------------------
    for g in rows(conn, """SELECT agent_id, customer_id, receipt_no, amount_reported, GROUP_CONCAT(collection_id) AS ids,
                                  COUNT(*) AS n FROM agent_collections WHERE recon_status = 'new'
                           GROUP BY agent_id, customer_id, receipt_no, amount_reported HAVING n > 1"""):
        ids = sorted(g["ids"].split(","))
        keep, dups = ids[0], ids[1:]
        recs = rows(conn, "SELECT collection_id, agent_id, customer_id, amount_reported, collection_time, receipt_no, agent_note "
                          "FROM agent_collections WHERE collection_id IN (%s)" % ",".join("?" * len(ids)), tuple(ids))
        for r in recs:
            r["agent_note"] = sanitize_untrusted(r["agent_note"], "field_agent_note")
        for d in dups:
            conn.execute("UPDATE agent_collections SET recon_status='duplicate', duplicate_of=? WHERE collection_id=?", (keep, d))
        stats["duplicate_receipts"] += len(dups)
        add_case("DUPLICATE_RECEIPT", customer_id=g["customer_id"], agent_id=g["agent_id"],
                 amount=g["amount_reported"] * len(dups), received_amount=g["amount_reported"], source="field agent",
                 title=f"Duplicate receipt {g['receipt_no']} from {g['agent_id']}",
                 summary=f"Receipt {g['receipt_no']} for {name(g['customer_id'])} was submitted {len(ids)} times. "
                         f"Only {keep} counts toward the agent batch; the duplicate is excluded so the contribution "
                         f"is credited once.",
                 evidence={"records": recs, "kept_record": keep, "excluded_records": dups},
                 recommended_action="Ask the agent to confirm the resubmission; no extra credit.", sort_key=g["receipt_no"])

    # 3. field-agent remittances ---------------------------------------------------------------
    agent_names = {r["agent_id"]: r["full_name"] for r in rows(conn, "SELECT agent_id, full_name FROM field_agents")}
    for t in rows(conn, "SELECT * FROM bank_transactions WHERE recon_status='new' AND reference LIKE 'REMIT AG-%' ORDER BY txn_time"):
        m = REMIT_RE.match(t["reference"].strip())
        if not m:
            continue
        agent, day = m.group(1), t["txn_time"][:10]
        conn.execute("UPDATE bank_transactions SET recon_status='remittance' WHERE bank_txn_id=?", (t["bank_txn_id"],))
        cols = rows(conn, "SELECT * FROM agent_collections WHERE agent_id=? AND substr(collection_time,1,10)=? "
                          "AND recon_status='new' ORDER BY collection_time", (agent, day))
        reported = sum(c["amount_reported"] for c in cols)
        if reported == t["amount"]:
            for c in cols:
                allocs, _ = finance.fifo_allocation(conn, c["customer_id"], c["amount_reported"], date.fromisoformat(day))
                finance.post_rule_entries(conn, c["customer_id"], allocs, when=run_time, source_type="agent_cash",
                                          source_ref=c["collection_id"], rule="R3-agent-batch-balanced")
                conn.execute("UPDATE agent_collections SET recon_status='matched' WHERE collection_id=?", (c["collection_id"],))
                stats["agent_collections_posted"] += 1
                stats["auto_reconciled_amount"] += c["amount_reported"]
            stats["agent_batches_balanced"] += 1
        else:
            short = reported - t["amount"]
            items = []
            for c in cols:
                conn.execute("UPDATE agent_collections SET recon_status='held' WHERE collection_id=?", (c["collection_id"],))
                note = sanitize_untrusted(c["agent_note"], "field_agent_note") if c["agent_note"] else None
                items.append({"collection_id": c["collection_id"], "customer_id": c["customer_id"],
                              "customer_name": name(c["customer_id"]), "amount_reported": c["amount_reported"],
                              "collection_time": c["collection_time"], "receipt_no": c["receipt_no"], "agent_note": note})
                suppress[c["customer_id"]].append(f"Cash collected by {agent} is held in an open agent-shortfall investigation")
                allocs, _ = finance.fifo_allocation(conn, c["customer_id"], c["amount_reported"], date.fromisoformat(day))
                blocked_contribs.update(a["contribution_id"] for a in allocs)
            flagged = [i["collection_id"] for i in items if i["agent_note"] and i["agent_note"]["instruction_like_content_removed"]]
            stats["agent_shortfalls"] += 1
            add_case("AGENT_SHORTFALL", agent_id=agent, amount=short, expected_amount=reported, received_amount=t["amount"],
                     bank_txn_id=t["bank_txn_id"], source="field agent",
                     title=f"{agent} ({agent_names.get(agent, '')}) remitted N{t['amount']:,} against N{reported:,} reported",
                     summary=f"Field agent {agent} reported {len(cols)} cash collections totalling N{reported:,} on {day} but "
                             f"the remittance {t['bank_txn_id']} was N{t['amount']:,}. Shortfall N{short:,}. Collections are "
                             f"held (not credited) pending investigation; affected customers will not be chased."
                             + (" An agent note contained instructions addressed to the system and was neutralized."
                                if flagged else ""),
                     evidence={"agent_id": agent, "agent_name": agent_names.get(agent), "day": day,
                               "collections": items, "reported_total": reported,
                               "remittance": {k: t[k] for k in ("bank_txn_id", "txn_time", "sender_name", "amount", "reference")},
                               "shortfall": short, "notes_with_embedded_instructions": flagged},
                     recommended_action="Open a field-agent cash investigation; do not credit or close without review.")

    # 4. exact bank reference ---------------------------------------------------------------------
    for t in rows(conn, "SELECT * FROM bank_transactions WHERE recon_status='new' ORDER BY txn_time, bank_txn_id"):
        m = REF_RE.search(t["reference"])
        if not m or m.group(1).upper() not in cust:
            continue
        cid = m.group(1).upper()
        tdate = date.fromisoformat(t["txn_time"][:10])
        allocs, left = finance.fifo_allocation(conn, cid, t["amount"], tdate + timedelta(days=1))
        if not allocs:
            continue
        finance.post_rule_entries(conn, cid, allocs, when=run_time, source_type="bank_transfer",
                                  source_ref=t["bank_txn_id"], rule="R1-exact-reference")
        conn.execute("UPDATE bank_transactions SET recon_status='matched' WHERE bank_txn_id=?", (t["bank_txn_id"],))
        stats["exact_reference_matches"] += 1
        stats["auto_reconciled_amount"] += t["amount"] - left
        last = allocs[-1]
        if last["outstanding_after"] > 0:
            stats["partial_payments"] += 1
            add_case("PARTIAL_PAYMENT", customer_id=cid, contribution_ids=[last["contribution_id"]], bank_txn_id=t["bank_txn_id"],
                     amount=last["outstanding_after"], expected_amount=last["expected_amount"], received_amount=last["amount"],
                     source="bank transfer",
                     title=f"Partial payment: {name(cid)} paid N{last['amount']:,} of N{last['expected_amount']:,}",
                     summary=f"Transfer {t['bank_txn_id']} carried the exact reference and was posted. It covers "
                             f"N{last['amount']:,} of the N{last['expected_amount']:,} due on {last['due_date']}; "
                             f"N{last['outstanding_after']:,} remains outstanding (computed from the ledger).",
                     evidence={"bank_txn": {k: t[k] for k in ("bank_txn_id", "txn_time", "sender_name", "amount", "reference")},
                               "allocation": allocs, "outstanding_after": last["outstanding_after"]},
                     recommended_action="Send payment acknowledgment that states the remaining balance.")
            suppress[cid].append("Partial payment acknowledged with remaining balance (no separate overdue reminder)")
        if left > 0:
            add_case("OVERPAYMENT", customer_id=cid, bank_txn_id=t["bank_txn_id"], amount=left, received_amount=t["amount"],
                     source="bank transfer", title=f"Unallocated excess N{left:,} from {name(cid)}",
                     summary="Transfer exceeds all open contributions; excess held unallocated.",
                     evidence={"allocation": allocs, "unallocated": left},
                     recommended_action="Confirm with customer whether excess is an advance contribution.")

    # 5. provider settlements -------------------------------------------------------------------
    for s in rows(conn, "SELECT * FROM provider_settlements WHERE recon_status='new' AND status='settled' ORDER BY payment_time"):
        allocs, left = finance.fifo_allocation(conn, s["customer_id"], s["amount"],
                                               date.fromisoformat(s["payment_time"][:10]) + timedelta(days=1))
        finance.post_rule_entries(conn, s["customer_id"], allocs, when=run_time, source_type="provider",
                                  source_ref=s["settlement_id"], rule="R2-provider-settled")
        conn.execute("UPDATE provider_settlements SET recon_status='matched' WHERE settlement_id=?", (s["settlement_id"],))
        stats["provider_settled_posted"] += 1
        stats["auto_reconciled_amount"] += s["amount"] - left
    batches: dict[str, list[dict]] = defaultdict(list)
    for s in rows(conn, "SELECT * FROM provider_settlements WHERE recon_status='new' AND status='pending' ORDER BY payment_time"):
        within_sla = s["expected_settlement_date"] >= today.isoformat()
        conn.execute("UPDATE provider_settlements SET recon_status=? WHERE settlement_id=?",
                     ("in_transit" if within_sla else "delayed", s["settlement_id"]))
        allocs, _ = finance.fifo_allocation(conn, s["customer_id"], s["amount"],
                                            date.fromisoformat(s["payment_time"][:10]) + timedelta(days=1))
        blocked_contribs.update(a["contribution_id"] for a in allocs)
        s["covers"] = [a["contribution_id"] for a in allocs]
        batches[s["batch_id"]].append(s)
        suppress[s["customer_id"]].append(
            f"Provider payment {s['settlement_id']} ({s['batch_id']}) is "
            + ("pending settlement within the T+1 window" if within_sla else "confirmed by the provider but settlement is late"))
    for batch_id, items in sorted(batches.items()):
        total = sum(i["amount"] for i in items)
        exp = items[0]["expected_settlement_date"]
        within = exp >= today.isoformat()
        evidence = {"batch_id": batch_id, "provider": items[0]["provider"], "expected_settlement_date": exp,
                    "payments": len(items), "total": total, "customers_affected": len({i["customer_id"] for i in items}),
                    "sample": [{k: i[k] for k in ("settlement_id", "customer_id", "payment_time", "amount", "customer_ref")}
                               for i in items[:8]]}
        if within:
            stats["in_transit_amount"] += total
            add_case("PROVIDER_SETTLEMENT_PENDING", amount=total, received_amount=total, source="payment provider",
                     title=f"{batch_id}: {len(items)} card/USSD payments settling {exp}",
                     summary=f"{len(items)} customer payments (N{total:,}) are confirmed by the provider and due to settle on "
                             f"{exp}, within the T+1 window. Reminders to these customers are suppressed.",
                     evidence=evidence, status="info", recommended_action="No action; re-check after settlement.",
                     sort_key=batch_id)
        else:
            stats["delayed_settlement_amount"] += total
            add_case("PROVIDER_SETTLEMENT_DELAYED", amount=total, received_amount=0, source="payment provider",
                     title=f"{batch_id}: N{total:,} settlement overdue since {exp}",
                     summary=f"{len(items)} provider-confirmed payments (N{total:,}) were due to settle on {exp} and have "
                             f"not arrived. Customers paid; their ledger lags. This cash is excluded from available funds "
                             f"until it settles.",
                     evidence=evidence, recommended_action="Escalate with the provider; do not remind affected customers.",
                     sort_key=batch_id)

    # 6. unmatched bank transfers -------------------------------------------------------------------
    for t in rows(conn, "SELECT * FROM bank_transactions WHERE recon_status='new' ORDER BY txn_time, bank_txn_id"):
        conn.execute("UPDATE bank_transactions SET recon_status='suspense' WHERE bank_txn_id=?", (t["bank_txn_id"],))
        stats["suspense_amount"] += t["amount"]
        stats["unmatched_inflows"] += 1
        cand = find_candidate_payment_matches(conn, t["bank_txn_id"], blocked_contribs)
        a = cand["deterministic_assessment"]
        supported = [c for c in cand["candidates"] if c["signal_score"] >= 30]
        for c in supported:
            suppress[c["customer_id"]].append(f"Possible payment {t['bank_txn_id']} awaiting a match decision")
        base = {"bank_txn": cand["bank_txn"], "deterministic_assessment": a}
        if a == "unidentified":
            add_case("UNIDENTIFIED_INFLOW", bank_txn_id=t["bank_txn_id"], amount=t["amount"], received_amount=t["amount"],
                     source="bank transfer", candidates=cand,
                     title=f"Unidentified transfer N{t['amount']:,} from {t['sender_name'].title()}",
                     summary=f"No open contribution of N{t['amount']:,} has any name or message link to sender "
                             f"'{t['sender_name']}'. Held in suspense.",
                     evidence=base, recommended_action="Hold in suspense; internal follow-up to identify the sender.")
        elif a == "single_supported_candidate":
            top = supported[0]
            add_case("PROBABLE_MATCH", customer_id=top["customer_id"], contribution_ids=[top["contribution_id"]],
                     bank_txn_id=t["bank_txn_id"], amount=t["amount"], expected_amount=top["outstanding"],
                     received_amount=t["amount"], source="bank transfer", candidates=cand, needs_judgment=True,
                     title=f"Transfer from {t['sender_name'].title()} may be {top['customer_name']}'s contribution",
                     summary=f"Transfer {t['bank_txn_id']} (N{t['amount']:,}, reference '{t['reference']}') has no customer "
                             f"reference. One open contribution has supporting evidence: {top['customer_name']} "
                             f"(due {top['due_date']}).",
                     evidence=base, recommended_action="Money Match review; matching requires human approval.")
        else:
            ids = [c["customer_id"] for c in supported]
            add_case("AMBIGUOUS_INFLOW", customer_id=None, contribution_ids=[c["contribution_id"] for c in supported],
                     bank_txn_id=t["bank_txn_id"], amount=t["amount"], received_amount=t["amount"], source="bank transfer",
                     candidates=cand, needs_judgment=True,
                     title=f"Transfer from {t['sender_name'].title()} could belong to {len(supported)} customers",
                     summary=f"Transfer {t['bank_txn_id']} (N{t['amount']:,}) matches open contributions of "
                             f"{', '.join(name(i) for i in ids)} equally well.",
                     evidence=base, recommended_action="Do not guess. Request clarification; human decides.")

    # 7. customer messages ------------------------------------------------------------------------
    for m in rows(conn, "SELECT * FROM customer_messages ORDER BY msg_time"):
        intent = classify_message(m["content"])
        inj = bool(detect_injection(m["content"]))
        conn.execute("UPDATE customer_messages SET intent=?, injection_flag=? WHERE message_id=?",
                     (intent, int(inj), m["message_id"]))
        cid = m["customer_id"]
        safe = sanitize_untrusted(m["content"], "customer_message")
        if intent == "embedded_instruction":
            stats["injection_messages"] += 1
            suppress[cid].append("Message contained instructions addressed to the system; staff review first")
            add_case("MESSAGE_SECURITY_FLAG", customer_id=cid, source="customer message",
                     title=f"Instruction-like message from {name(cid)}",
                     summary="A customer message tried to instruct the system to change account records. It is "
                             "treated as untrusted content only; no record was changed.",
                     evidence={"message": {"message_id": m["message_id"], "msg_time": m["msg_time"], "channel": m["channel"],
                                           **safe}, "patterns_detected": len(detect_injection(m["content"])),
                               "ledger_changed": False},
                     recommended_action="Staff review of the message; no automated account change is possible.",
                     sort_key=m["message_id"])
        elif intent == "third_party_data_request":
            add_case("PRIVACY_REQUEST", customer_id=cid, source="customer message",
                     title=f"{name(cid)} asked for another customer's information",
                     summary="Request for another customer's contact or savings details. Policy: decline; never disclose.",
                     evidence={"message": {"message_id": m["message_id"], "msg_time": m["msg_time"], "channel": m["channel"],
                                           **safe}},
                     recommended_action="Send a policy reply declining disclosure.", sort_key=m["message_id"])
        elif intent == "cash_to_agent_claim":
            c = cust.get(cid)
            agent = c["agent_id"] if c else None
            opens = [o for o in finance.open_contributions(conn, cid) if o["contribution_id"] not in blocked_contribs]
            if not opens:
                continue
            recent = rows(conn, "SELECT collection_id, agent_id, customer_id, amount_reported, collection_time, receipt_no "
                                "FROM agent_collections WHERE customer_id=? AND collection_time >= ? ORDER BY collection_time",
                          (cid, (today - timedelta(days=3)).isoformat()))
            day_records = rows(conn, "SELECT substr(collection_time,1,10) AS day, COUNT(*) AS n, SUM(amount_reported) AS total "
                                     "FROM agent_collections WHERE agent_id=? AND collection_time >= ? GROUP BY day",
                               (agent, (today - timedelta(days=3)).isoformat())) if agent else []
            suppress[cid].append("Customer reports cash paid to a field agent; dispute open")
            blocked_contribs.update(o["contribution_id"] for o in opens if o["days_late"] > config.GRACE_DAYS)
            disputed = [o for o in opens if o["days_late"] > config.GRACE_DAYS] or opens[:1]
            stats["disputes"] += 1
            add_case("CUSTOMER_DISPUTE", customer_id=cid, agent_id=agent, contribution_ids=[o["contribution_id"] for o in disputed],
                     amount=sum(o["outstanding"] for o in disputed), expected_amount=sum(o["expected_amount"] for o in disputed),
                     received_amount=0, source="customer message + field agent records",
                     title=f"{name(cid)} says cash was given to agent {agent}; no agent record",
                     summary=f"Customer reports paying cash to the field agent, but {agent}'s collection records contain no "
                             f"entry for this customer on the disputed date and the agent's remittances balance.",
                     evidence={"message": {"message_id": m["message_id"], "msg_time": m["msg_time"], "channel": m["channel"],
                                           **safe},
                               "disputed_contributions": disputed, "customer_agent_records_last_3_days": recent,
                               "agent_daily_records": day_records},
                     recommended_action="Human review: contact agent for receipt book; do not remind the customer.")
        elif intent == "payment_claim":
            suppress[cid].append(f"Customer says payment was made ({m['message_id']}); verify before reminding")

    # 8. overdue detection -------------------------------------------------------------------------
    cutoff = (today - timedelta(days=config.GRACE_DAYS + 1)).isoformat()
    overdue_rows = rows(conn, """
        SELECT c.customer_id, c.contribution_id, c.due_date, c.expected_amount,
               COALESCE((SELECT SUM(amount) FROM ledger_entries l WHERE l.contribution_id = c.contribution_id),0) AS paid
        FROM expected_contributions c WHERE c.status != 'paid' AND c.due_date <= ? ORDER BY c.customer_id, c.due_date""",
                        (cutoff,))
    by_cust: dict[str, list[dict]] = defaultdict(list)
    for r in overdue_rows:
        r["outstanding"] = r["expected_amount"] - r["paid"]
        r["days_late"] = finance.days_late(r["due_date"])
        by_cust[r["customer_id"]].append(r)
    for cid, items in sorted(by_cust.items()):
        reasons = list(dict.fromkeys(suppress.get(cid, [])))
        covered = [i for i in items if i["contribution_id"] in blocked_contribs]
        if covered and not reasons:
            reasons.append("Payment evidence covers this contribution")
        outstanding = sum(i["outstanding"] for i in items)
        maxd = max(i["days_late"] for i in items)
        hist = finance.payment_history_metrics(conn, cid)
        bank_hits = scalar(conn, "SELECT COUNT(*) FROM bank_transactions WHERE recon_status='suspense' AND "
                                 "amount IN (%s)" % ",".join(str(i["outstanding"]) for i in items)) or 0
        verified = not reasons
        stats["overdue_customers"] += 1
        stats["overdue_amount"] += outstanding
        if verified:
            stats["confirmed_overdue_customers"] += 1
            stats["confirmed_overdue_amount"] += outstanding
        else:
            stats["reminders_suppressed"] += 1
        add_case("OVERDUE_FOLLOWUP", customer_id=cid, contribution_ids=[i["contribution_id"] for i in items],
                 amount=outstanding, expected_amount=sum(i["expected_amount"] for i in items),
                 received_amount=sum(i["paid"] for i in items), source="internal ledger", days=maxd,
                 title=(f"{name(cid)}: N{outstanding:,} overdue ({maxd} days)" if verified else
                        f"{name(cid)}: ledger shows N{outstanding:,} unpaid - reminder held"),
                 summary=("Verified overdue: no bank match, no pending settlement, no agent record and no open dispute. "
                          "Eligible for a routine reminder." if verified else
                          "Ledger shows unpaid, but there is payment evidence or an open question, so no reminder is sent: "
                          + "; ".join(reasons) + "."),
                 evidence={"overdue_contributions": items, "checks": {
                     "exact_reference_match": False, "pending_provider_settlement": any("Provider" in r for r in reasons),
                     "agent_collection_on_record": any("agent" in r.lower() for r in reasons),
                     "candidate_unmatched_transfer": any("Possible payment" in r for r in reasons),
                     "open_dispute": any("dispute" in r for r in reasons),
                     "suspense_transfers_same_amount": bank_hits},
                     "suppression_reasons": reasons, "payment_history": hist},
                 recommended_action=("Send routine reminder (automation allowed)." if verified else
                                     "Do not remind; resolve the linked evidence first."))
        if not verified:
            cases[-1]["reminder_suppressed"] = 1

    # persist cases in deterministic order --------------------------------------------------------
    cases.sort(key=lambda c: c["_sort"])
    for i, c in enumerate(cases, start=1):
        c.pop("_sort")
        c.setdefault("reminder_suppressed", 0)
        c["case_id"] = f"RC-{1000 + i}"
        c["created_at"] = run_time
        c["updated_at"] = run_time
        conn.execute("""INSERT INTO reconciliation_cases(case_id, case_type, status, priority_score, priority, customer_id,
                        contribution_ids, bank_txn_id, agent_id, amount, expected_amount, received_amount, source, title, summary,
                        evidence_json, candidates_json, needs_judgment, reminder_suppressed, recommended_action, created_at,
                        updated_at) VALUES (:case_id,:case_type,:status,:priority_score,:priority,:customer_id,:contribution_ids,
                        :bank_txn_id,:agent_id,:amount,:expected_amount,:received_amount,:source,:title,:summary,:evidence_json,
                        :candidates_json,:needs_judgment,:reminder_suppressed,:recommended_action,:created_at,:updated_at)""", c)
    stats["cases_created"] = len(cases)
    stats["auto_reconciled_count"] = (stats["exact_reference_matches"] + stats["provider_settled_posted"]
                                      + stats["agent_collections_posted"])
    return dict(stats)
