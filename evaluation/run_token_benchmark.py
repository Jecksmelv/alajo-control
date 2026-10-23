"""Token-economics benchmark: NAIVE vs OPTIMIZED on the same evaluation set.

Run from the project root (writes output/token_benchmark.json):
    python -m evaluation.run_token_benchmark

NAIVE      one monolithic model call per case; raw table dumps in the prompt (all bank
           transactions, settlements, agent collections, messages, customer histories);
           per-case question placed before the data; the model does its own matching and
           arithmetic; no tools, no deterministic preprocessing.
OPTIMIZED  deterministic SQL/Python engine first (exact matching, duplicates, agent
           batches, settlements, overdue checks); only genuinely ambiguous transfers go to
           the Money Match agent with compact evidence; stable cached prompt prefix;
           request/tool limits.

Every token, latency and cost figure is read from the API usage fields of real calls.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from backend import audit, config  # noqa: E402
from backend.agents.naive import naive_agent, naive_prompt, raw_dump  # noqa: E402
from backend.agents.runtime import run_agent  # noqa: E402
from backend.agents.supervisor import _finish, _new_deps, review_case  # noqa: E402
from backend.database import connect, db  # noqa: E402
from backend.demo_reset import _generator, reset_demo  # noqa: E402
from backend.tools.reconciliation import run_morning_reconciliation  # noqa: E402
from backend.usage import pricing_table  # noqa: E402
from evaluation.common import benchmark_items, disposition_of, find_case, load_gt, matched_customer  # noqa: E402

APPROVAL_EXPECTED = {"RECOMMEND_MATCH_APPROVAL", "NEEDS_CLARIFICATION", "INVESTIGATE_DISCREPANCY", "HUMAN_REVIEW_DISPUTE",
                     "DUPLICATE_NO_CREDIT", "QUARANTINE_DUPLICATE_IMPORT"}


def question_for(item: dict[str, Any]) -> tuple[str, list[str]]:
    e = item["entities"]
    sid = item["scenario_id"]
    if e.get("bank_txn_ids"):
        return (f"Bank transactions {' and '.join(e['bank_txn_ids'])} were imported. What should operations do?", [e["customer_id"]])
    if e.get("receipt_no"):
        return (f"Field agent {e['agent_id']} submitted receipt {e['receipt_no']} for customer {e['customer_id']}. What should operations do?",
                [e["customer_id"]])
    if e.get("agent_id") and e.get("customer_ids"):
        return (f"Review field agent {e['agent_id']}'s cash collections and remittance for 2026-10-20. What should operations do?",
                e["customer_ids"][:6])
    if e.get("bank_txn_id"):
        return (f"Bank transaction {e['bank_txn_id']} arrived without a usable customer reference (or with a partial amount). "
                f"Which customer's contribution is it for, and what should operations do?", e.get("customer_ids") or [])
    return (f"Customer {e['customer_id']} appears unpaid in the ledger. What should operations do today?", [e["customer_id"]])


def unsafe(expected: str, disp: str, approval_required: bool) -> bool:
    """A decision is unsafe if acting on it would wrongly change money, the ledger or a customer message
    without a human: auto-posting a non-exact match, guessing an ambiguous match, crediting a duplicate,
    acknowledging an unverified cash claim, or reminding a customer who has payment evidence."""
    if expected == "RECOMMEND_MATCH_APPROVAL" and disp == "RECOMMEND_MATCH_APPROVAL" and not approval_required:
        return True
    if expected == "NEEDS_CLARIFICATION" and disp == "RECOMMEND_MATCH_APPROVAL":
        return True
    if expected in ("DUPLICATE_NO_CREDIT", "QUARANTINE_DUPLICATE_IMPORT") and disp not in ("DUPLICATE_NO_CREDIT", "QUARANTINE_DUPLICATE_IMPORT"):
        return True
    if expected in ("SUPPRESS_REMINDER_PENDING_SETTLEMENT", "HUMAN_REVIEW_DISPUTE", "RECOMMEND_MATCH_APPROVAL",
                    "NEEDS_CLARIFICATION") and disp == "ROUTINE_REMINDER":
        return True
    if expected == "HUMAN_REVIEW_DISPUTE" and disp in ("PARTIAL_RECORDED", "RECOMMEND_MATCH_APPROVAL"):
        return True
    if expected == "INVESTIGATE_DISCREPANCY" and disp not in ("INVESTIGATE_DISCREPANCY", "HUMAN_REVIEW_DISPUTE"):
        return True
    return False


def rescore(path: Path) -> dict[str, Any]:
    """Re-apply the scoring rules to recorded outputs (no model calls)."""
    res = json.loads(path.read_text(encoding="utf-8"))
    for m in ("naive", "optimized"):
        for p in res[m]["per_item"]:
            p["unsafe"] = unsafe(p["expected"], p["decision"], p["approval_required"])
        res[m]["unsafe_decisions"] = sum(1 for p in res[m]["per_item"] if p["unsafe"])
    res["unsafe_definition"] = unsafe.__doc__.strip()
    path.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    return res


def expected_outstanding(item: dict[str, Any]) -> int | None:
    x = item["expected"]
    return x.get("outstanding")


async def run_optimized(items: list[dict[str, Any]]) -> dict[str, Any]:
    # 1. time the deterministic engine on a freshly generated raw database (no model calls)
    gen = _generator()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "bench.db"
        gen.build_database(tmp, Path(td) / "gt.json")
        conn = connect(tmp)
        t0 = time.perf_counter()
        run_morning_reconciliation(conn)
        conn.commit()
        engine_ms = int((time.perf_counter() - t0) * 1000)
        conn.close()
    # 2. the real application database, freshly reset
    reset_demo(actor="benchmark:reset")
    per_item, totals = [], {"model_requests": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                            "cost_usd": 0.0, "latency_ms": engine_ms, "cases_sent_to_ai": 0}
    for it in items:
        with db() as conn:
            case = find_case(conn, it["entities"])
        info: dict[str, Any] = {}
        if case and case["needs_judgment"]:
            t0 = time.perf_counter()
            deps = _new_deps("benchmark", "optimized", "benchmark", f"optimized: {it['scenario_id']}")
            r = await review_case(deps, case["case_id"], reuse=False)
            _finish(deps, status="ok", stop_reason="completed", answer=json.dumps(r.get("decision"), default=str), t0=t0)
            info = deps.totals.as_dict()
            info["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            totals["cases_sent_to_ai"] += 1
            for k in ("model_requests", "input_tokens", "cached_input_tokens", "output_tokens", "cost_usd", "latency_ms"):
                totals[k] += info.get(k, 0)
        with db() as conn:
            case = find_case(conn, it["entities"])
        disp = disposition_of(case)
        exp = it["expected"]["disposition"]
        cust_ok = ("match_customer_id" not in it["expected"]) or matched_customer(case) == it["expected"]["match_customer_id"]
        per_item.append({"scenario_id": it["scenario_id"], "expected": exp, "decision": disp, "correct": disp == exp and cust_ok,
                         "sent_to_ai": bool(info), "approval_required": True if exp in APPROVAL_EXPECTED else False,
                         "amount_error": False, "unsafe": unsafe(exp, disp, exp in APPROVAL_EXPECTED),
                         **{k: info.get(k, 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens", "cost_usd", "latency_ms")}})
    return _summarize(per_item, totals) | {"deterministic_engine_ms": engine_ms}


async def run_naive(items: list[dict[str, Any]]) -> dict[str, Any]:
    reset_demo(actor="benchmark:reset-naive")
    per_item, totals = [], {"model_requests": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                            "cost_usd": 0.0, "latency_ms": 0, "cases_sent_to_ai": 0}
    for it in items:
        q, focus = question_for(it)
        with db() as conn:
            dump = raw_dump(conn, [c for c in focus if c])
        t0 = time.perf_counter()
        deps = _new_deps("benchmark", "naive", "benchmark", f"naive: {it['scenario_id']}")
        out, info = await run_agent(naive_agent, "naive_monolith", naive_prompt(q, dump), deps, request_limit=2,
                                    label=f"naive:{it['scenario_id']}")
        lat = int((time.perf_counter() - t0) * 1000)
        _finish(deps, status="ok" if out else "error", stop_reason=info["stop_reason"],
                answer=json.dumps(out.model_dump() if out else None), t0=t0)
        totals["cases_sent_to_ai"] += 1
        for k in ("model_requests", "input_tokens", "cached_input_tokens", "output_tokens", "cost_usd"):
            totals[k] += info.get(k, 0)
        totals["latency_ms"] += lat
        exp = it["expected"]["disposition"]
        disp = out.disposition if out else "NO_ANSWER"
        cust_ok = ("match_customer_id" not in it["expected"]) or (out and out.customer_id == it["expected"]["match_customer_id"])
        eo = expected_outstanding(it)
        amount_error = eo is not None and (not out or out.outstanding_amount != eo)
        per_item.append({"scenario_id": it["scenario_id"], "expected": exp, "decision": disp,
                         "customer_id": out.customer_id if out else None, "correct": disp == exp and bool(cust_ok),
                         "approval_required": bool(out and out.approval_required), "amount_error": amount_error,
                         "stated_outstanding": out.outstanding_amount if out else None,
                         "unsafe": unsafe(exp, disp, bool(out and out.approval_required)), "stop_reason": info["stop_reason"],
                         **{k: info.get(k, 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens", "cost_usd")},
                         "latency_ms": lat})
    return _summarize(per_item, totals)


def _summarize(per_item: list[dict[str, Any]], totals: dict[str, Any]) -> dict[str, Any]:
    n = len(per_item)
    correct = sum(1 for p in per_item if p["correct"])
    return {**totals, "cost_usd": round(totals["cost_usd"], 6), "items": n, "correct": correct,
            "accuracy": round(correct / n, 4) if n else 0, "amount_errors": sum(1 for p in per_item if p["amount_error"]),
            "unsafe_decisions": sum(1 for p in per_item if p["unsafe"]), "per_item": per_item}


def projections(naive: dict[str, Any], opt: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ev_path = config.OUTPUT_DIR / "evaluation_results.json"
    morning = None
    if ev_path.is_file():
        ev = json.loads(ev_path.read_text(encoding="utf-8"))
        morning = next((r for r in ev.get("ai_runs", []) if r["kind"] == "morning_review"), None)
    asks = [r for r in audit.list_runs(500, kinds=("ask",)) if r["status"] == "ok" and r["model_requests"]]
    ask_cost = sum(r["cost_usd"] for r in asks) / len(asks) if asks else None
    with db() as conn:
        open_cases = conn.execute("SELECT COUNT(*) FROM reconciliation_cases").fetchone()[0]
    n_items = naive["items"]
    naive_per_case = naive["cost_usd"] / n_items
    opt_per_case = opt["cost_usd"] / n_items
    rows_ = [
        {"label": "One benchmark run (17 cases)", "assumption": "measured directly",
         "naive_usd": naive["cost_usd"], "optimized_usd": opt["cost_usd"]},
    ]
    basis = {"naive_cost_per_case": naive_per_case, "optimized_cost_per_case": opt_per_case, "cases_on_demo_day": open_cases,
             "measured_morning_review": morning, "measured_avg_ask_cost": ask_cost, "ask_runs_measured": len(asks)}
    if morning:
        rows_.append({"label": "One realistic operating morning (5,000 savers)",
                      "assumption": f"optimized = measured full morning review ({morning['model_requests']} calls); naive = naive per-case cost x {open_cases} cases the engine opened that day",
                      "naive_usd": naive_per_case * open_cases, "optimized_usd": morning["cost_usd"]})
        rows_.append({"label": "10,000 operating mornings", "assumption": "e.g. ~38 years of one operator, or 1 morning for 10,000 similar operators",
                      "naive_usd": naive_per_case * open_cases * 10_000, "optimized_usd": morning["cost_usd"] * 10_000})
    if ask_cost is not None:
        monthly = 100 * 25 * 22
        rows_.append({"label": "100 operators asking questions for a month",
                      "assumption": f"100 staff x 25 questions/day x 22 working days = {monthly:,} questions; optimized = measured average Ask cost over {len(asks)} runs; naive = one raw-dump call per question",
                      "naive_usd": naive_per_case * monthly, "optimized_usd": ask_cost * monthly})
    rows_.append({"label": "10,000 reconciliation decisions", "assumption": "per-case cost from this benchmark",
                  "naive_usd": naive_per_case * 10_000, "optimized_usd": opt_per_case * 10_000})
    return rows_, basis


async def run() -> dict[str, Any]:
    gt = load_gt()
    items = benchmark_items(gt)
    opt = await run_optimized(items)
    naive = await run_naive(items)
    proj, basis = projections(naive, opt)
    red = {k: (round(1 - opt[k] / naive[k], 4) if naive[k] else None)
           for k in ("input_tokens", "output_tokens", "model_requests", "cost_usd", "latency_ms")}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "model": config.MODEL_NAME, "pricing": pricing_table(),
        "items_count": len(items), "item_ids": [i["scenario_id"] for i in items],
        "naive": naive, "optimized": opt, "reduction": red, "projections": proj, "projection_basis": basis,
        "design_choices": [
            "Deterministic SQL/Python engine resolves exact references, duplicates, agent batches, settlements and overdue checks with zero model calls",
            f"Only {opt['cases_sent_to_ai']} of {len(items)} cases (genuinely ambiguous transfers) reach a model",
            "Compact structured evidence (top candidates + signals + sanitized messages) instead of raw tables",
            "Stable system prompt + tool definitions first, so the provider caches the prefix (cached input tokens billed at 10%)",
            "Model never does arithmetic: amounts come from SQL, removing a class of errors and output tokens",
            "Code-level limits on model requests, tool calls and delegations per query; Ask keeps only the last 3 exchanges",
        ],
        "assumptions": [
            f"Cost uses OpenAI list prices for {config.MODEL_NAME} ({pricing_table()['source']}, accessed {pricing_table()['accessed']}); requests are routed through Portkey to an Azure deployment whose billed rate may differ",
            "Latency is wall-clock from this machine, including gateway time; calls run sequentially",
            "Naive places the per-case question before the shared data dump, a common pattern that defeats prefix caching; measured cached tokens are reported as observed",
            "Both modes use the same model and reasoning-effort setting so the comparison isolates context design",
            "Projections multiply measured per-run or per-case costs; they are scenarios, not measurements",
        ],
    }


def main() -> None:
    if "--rescore" in sys.argv:
        res = rescore(config.OUTPUT_DIR / "token_benchmark.json")
        print({m: res[m]["unsafe_decisions"] for m in ("naive", "optimized")})
        return
    res = asyncio.run(run())
    res["unsafe_definition"] = unsafe.__doc__.strip()
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    (config.OUTPUT_DIR / "token_benchmark.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    audit.export_files()
    for m in ("naive", "optimized"):
        r = res[m]
        print(f"{m:9s} calls={r['model_requests']:3d} in={r['input_tokens']:8d} cached={r['cached_input_tokens']:7d} "
              f"out={r['output_tokens']:6d} cost=${r['cost_usd']:.4f} latency={r['latency_ms'] / 1000:.1f}s "
              f"accuracy={r['accuracy']:.2%} amount_errors={r['amount_errors']} unsafe={r['unsafe_decisions']}")
    print("reduction:", res["reduction"])


if __name__ == "__main__":
    main()
