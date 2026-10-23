"""Manual-vs-system workflow benchmark packet.

Builds evaluation/manual_test_packet/ (24 cases a human can work through with the same
raw evidence the system starts from) and output/manual_benchmark.json with the
system's measured time for the same cases. No human time is invented: the human
result stays null until someone runs the protocol and scores it with
    python -m evaluation.score_manual evaluation/manual_test_packet/answer_sheet.csv

Run from the project root:
    python -m evaluation.build_manual_packet
"""

from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from backend import config  # noqa: E402
from backend.database import db, one, rows  # noqa: E402
from evaluation.common import benchmark_items, load_gt  # noqa: E402

PACKET = PROJECT_DIR / "evaluation" / "manual_test_packet"


def _customer_block(conn, cid: str) -> str:
    c = one(conn, "SELECT customer_id, first_name, last_name, location, savings_plan_id, preferred_channel, agent_id FROM customers WHERE customer_id=?", (cid,))
    con = rows(conn, "SELECT contribution_id, due_date, expected_amount FROM expected_contributions WHERE customer_id=? AND due_date >= '2026-10-12' ORDER BY due_date", (cid,))
    led = rows(conn, "SELECT contribution_id, amount, entry_time, source_type FROM ledger_entries WHERE customer_id=? AND posted_by='rule:historical-close' AND entry_time >= '2026-10-12' ORDER BY entry_time", (cid,))
    msgs = rows(conn, "SELECT message_id, msg_time, channel, content FROM customer_messages WHERE customer_id=?", (cid,))
    lines = [f"Customer {c['customer_id']} {c['first_name']} {c['last_name']} | {c['location']} | plan {c['savings_plan_id']} | pays by {c['preferred_channel']} {c['agent_id'] or ''}",
             "  Expected contributions (since 12 Oct): " + "; ".join(f"{x['contribution_id']} due {x['due_date']} N{x['expected_amount']:,}" for x in con),
             "  Ledger (before this morning): " + ("; ".join(f"{x['contribution_id']} N{x['amount']:,} {x['entry_time'][:16]}" for x in led) or "none")]
    for m in msgs:
        lines.append(f"  Message {m['message_id']} {m['msg_time'][:16]} ({m['channel']}): \"{m['content']}\"")
    return "\n".join(lines)


def build() -> dict:
    gt = load_gt()
    items = benchmark_items(gt)
    rng = random.Random(7)
    with db() as conn:
        exact = rows(conn, "SELECT bank_txn_id FROM bank_transactions WHERE reference LIKE 'ALJ-C%' AND recon_status='matched' ORDER BY bank_txn_id")
        overdue = rows(conn, "SELECT customer_id FROM reconciliation_cases WHERE case_type='OVERDUE_FOLLOWUP' AND reminder_suppressed=0 ORDER BY case_id")
        extra = [{"scenario_id": f"ROUTINE_EXACT_{i + 1}", "entities": {"bank_txn_id": t["bank_txn_id"]}} for i, t in enumerate(rng.sample(exact, 4))] + \
                [{"scenario_id": f"ROUTINE_OVERDUE_{i + 1}", "entities": {"customer_id": o["customer_id"]}} for i, o in enumerate(rng.sample(overdue, 3))]
        all_items = items + extra
        PACKET.mkdir(parents=True, exist_ok=True)
        md = ["# Alajo Control — manual reconciliation test packet", "",
              "Synthetic data. For each item decide the disposition (see INSTRUCTIONS.md), the customer it belongs to, and the amount outstanding.", ""]
        for n, it in enumerate(all_items, 1):
            e = it["entities"]
            md.append(f"## Item {n:02d}")
            if e.get("bank_txn_ids") or e.get("bank_txn_id"):
                for tid in e.get("bank_txn_ids") or [e["bank_txn_id"]]:
                    t = one(conn, "SELECT * FROM bank_transactions WHERE bank_txn_id=?", (tid,))
                    md.append(f"Bank transfer {t['bank_txn_id']} {t['txn_time']} from {t['sender_name']} N{t['amount']:,} ref \"{t['reference']}\" (import {t['import_batch']})")
            if e.get("receipt_no"):
                for c in rows(conn, "SELECT * FROM agent_collections WHERE receipt_no=?", (e["receipt_no"],)):
                    md.append(f"Agent collection {c['collection_id']} {c['agent_id']} customer {c['customer_id']} N{c['amount_reported']:,} {c['collection_time']} receipt {c['receipt_no']} note: {c['agent_note'] or ''}")
            if e.get("agent_id") and e.get("customer_ids"):
                for c in rows(conn, "SELECT * FROM agent_collections WHERE agent_id=? AND collection_time LIKE '2026-10-20%'", (e["agent_id"],)):
                    md.append(f"Agent collection {c['collection_id']} customer {c['customer_id']} N{c['amount_reported']:,} receipt {c['receipt_no']} note: {c['agent_note'] or ''}")
                for t in rows(conn, "SELECT * FROM bank_transactions WHERE reference LIKE ?", (f"REMIT {e['agent_id']}%",)):
                    md.append(f"Remittance {t['bank_txn_id']} {t['txn_time']} N{t['amount']:,} ref \"{t['reference']}\"")
            cids = [c for c in (e.get("customer_ids") or [e.get("customer_id")]) if c]
            if e.get("bank_txn_id") and not cids:
                md.append("(Find the customer yourself: search the customer list / open contributions export.)")
            for cid in cids[:3]:
                if e.get("bank_txn_id") and it["scenario_id"].startswith(("CASE_001", "CASE_002", "CASE_008", "NORMAL")):
                    continue   # don't hand the human the answer for matching items
                md.append(_customer_block(conn, cid))
                for s in rows(conn, "SELECT settlement_id, batch_id, payment_time, amount, expected_settlement_date, status FROM provider_settlements WHERE customer_id=?", (cid,)):
                    md.append(f"  Provider settlement {s['settlement_id']} batch {s['batch_id']} paid {s['payment_time'][:16]} N{s['amount']:,} expected {s['expected_settlement_date']} status {s['status']}")
            md.append("")
        (PACKET / "packet.md").write_text("\n".join(md), encoding="utf-8")
        opens = rows(conn, """SELECT c.contribution_id, c.customer_id, cu.first_name, cu.last_name, c.due_date, c.expected_amount
                              FROM expected_contributions c JOIN customers cu ON cu.customer_id=c.customer_id
                              WHERE c.due_date BETWEEN '2026-10-14' AND '2026-10-21' AND NOT EXISTS (SELECT 1 FROM ledger_entries l
                              WHERE l.contribution_id=c.contribution_id AND l.posted_by='rule:historical-close') ORDER BY c.customer_id""")
        with open(PACKET / "open_contributions.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["contribution_id", "customer_id", "first_name", "last_name", "due_date", "expected_amount"])
            for o in opens:
                w.writerow(o.values())
    with open(PACKET / "answer_sheet.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item", "disposition", "customer_id", "outstanding_amount", "approval_required (y/n)", "notes"])
        for n in range(1, len(all_items) + 1):
            w.writerow([f"{n:02d}", "", "", "", "", ""])
    (PACKET / "item_key.json").write_text(json.dumps([{"item": f"{n:02d}", "scenario_id": it["scenario_id"]} for n, it in enumerate(all_items, 1)], indent=1), encoding="utf-8")
    (PACKET / "INSTRUCTIONS.md").write_text(INSTRUCTIONS.format(n=len(all_items)), encoding="utf-8")

    bench_path = config.OUTPUT_DIR / "token_benchmark.json"
    system_seconds = None
    if bench_path.is_file():
        b = json.loads(bench_path.read_text(encoding="utf-8"))
        system_seconds = round(b["optimized"]["latency_ms"] / 1000, 1)
    res = {"packet": "evaluation/manual_test_packet/", "items": len(all_items),
           "system_seconds": system_seconds,
           "system_seconds_basis": "measured optimized benchmark: deterministic engine over the whole demo day + Money Match on the ambiguous items (17 hardest items); the 7 routine items are resolved inside the engine run",
           "human_result": None,
           "summary": (f"A {len(all_items)}-item packet with the same raw evidence is provided for a timed human baseline. "
                       "No human test has been run yet, so no human time or accuracy is reported."),
           "how_to_run": "Follow evaluation/manual_test_packet/INSTRUCTIONS.md, then: python -m evaluation.score_manual evaluation/manual_test_packet/answer_sheet.csv"}
    (config.OUTPUT_DIR / "manual_benchmark.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


INSTRUCTIONS = """# Manual baseline protocol ({n} items)

Purpose: measure how long a trained operations analyst needs to resolve the same cases Alajo Control resolves,
and how accurately. The data are synthetic.

1. Open `packet.md` and `open_contributions.csv` (the open-contributions export an analyst would have).
   Do not open `item_key.json` or `evaluation/ground_truth.json`.
2. Start a stopwatch. For each item, fill one row of `answer_sheet.csv`:
   - disposition — one of RECOMMEND_MATCH_APPROVAL, NEEDS_CLARIFICATION, NO_MATCH_HOLD_IN_SUSPENSE, DUPLICATE_NO_CREDIT,
     QUARANTINE_DUPLICATE_IMPORT, INVESTIGATE_DISCREPANCY, ROUTINE_REMINDER, SUPPRESS_REMINDER_PENDING_SETTLEMENT,
     PARTIAL_RECORDED, HUMAN_REVIEW_DISPUTE, AUTO_POST_EXACT (exact reference, post it)
   - customer_id (if the item concerns a customer), outstanding amount (naira), whether approval is required.
3. Stop the stopwatch after the last item; record total minutes in the notes column of item 01.
4. Score: `python -m evaluation.score_manual evaluation/manual_test_packet/answer_sheet.csv`
   This writes the human result into output/manual_benchmark.json.
"""


if __name__ == "__main__":
    print(json.dumps(build(), indent=1))
