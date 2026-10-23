"""Alajo Control API (FastAPI).

Run from the project root:
    .venv/Scripts/python -m uvicorn backend.main:app --port 8000      (Windows)
    .venv/bin/python -m uvicorn backend.main:app --port 8000          (macOS/Linux)
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import audit, config
from .database import db, ensure_initialized, get_state, one, rows, scalar, set_state
from .models import ApprovalDecisionRequest, AskRequest, ReminderRequest
from .safety import ACTION_POLICY, SafetyBlock
from .tools import actions, liquidity, queries
from .usage import pricing_table

_background: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_initialized()
    yield


app = FastAPI(title="Alajo Control API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(SafetyBlock)
async def _safety_handler(_, exc: SafetyBlock):
    audit.log("safety_block", "api", {"control": exc.control, "message": str(exc), **exc.details})
    return JSONResponse(status_code=409, content={"blocked": True, "control": exc.control, "message": str(exc)})


def _json(v: str | None) -> Any:
    return json.loads(v) if v else None


# ---------------------------------------------------------------------------
# Status / overview
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "ai_configured": config.ai_configured(), "model": config.MODEL_NAME,
            "env_files_loaded": config.ENV_FILES_LOADED, "demo_date": config.DEMO_TODAY.isoformat(),
            "demo_epoch": audit.current_epoch()}


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    with db() as conn:
        s = queries.get_portfolio_summary(conn)
        s["ai_review"] = get_state(conn, "ai_review", {"status": "not_run"})
        s["reconciliation_run"] = get_state(conn, "morning_reconciliation", {})
        s["action_queue"] = queries.get_exception_queue(conn, 8)
        s["approvals"] = rows(conn, "SELECT approval_id, case_id, action_type, customer_id, amount, confidence, created_at "
                                    "FROM approvals WHERE status='pending' ORDER BY created_at DESC LIMIT 6")
        for a in s["approvals"]:
            a["customer_name"] = queries.customer_name(conn, a["customer_id"])
        s["automated_actions"] = rows(conn, "SELECT action_type, COUNT(*) AS n FROM outbox WHERE automated=1 GROUP BY action_type")
        s["tasks_open"] = scalar(conn, "SELECT COUNT(*) FROM internal_tasks WHERE status='open'")
        proj = liquidity.calculate_liquidity_projection(conn)
        s["liquidity_days"] = [{k: d[k] for k in ("date", "closing_position", "closing_position_if_unresolved_resolve",
                                                   "scheduled_payouts")} for d in proj["days"]]
        s["liquidity_top_unresolved"] = proj["unresolved_inflows"][:3]
    s["recent_runs"] = audit.list_runs(6)
    return s


# ---------------------------------------------------------------------------
# Reconciliation / cases
# ---------------------------------------------------------------------------

@app.get("/api/cases")
def list_cases(types: str | None = None, status: str = "unresolved", customer_id: str | None = None,
               limit: int = Query(300, le=1000)) -> list[dict[str, Any]]:
    tt = tuple(t for t in (types or "").split(",") if t) or None
    with db() as conn:
        return queries.list_cases(conn, case_types=tt, status=status if status != "all" else None,
                                  customer_id=customer_id, limit=limit)


@app.get("/api/cases/{case_id}")
def get_case(case_id: str) -> dict[str, Any]:
    with db() as conn:
        c = queries.get_case(conn, case_id)
    if not c:
        raise HTTPException(404, "Case not found")
    return c


@app.post("/api/cases/{case_id}/review")
async def review_case(case_id: str) -> dict[str, Any]:
    from .agents.supervisor import review_single_case
    return await review_single_case(case_id)


@app.post("/api/cases/{case_id}/remind")
def remind(case_id: str, req: ReminderRequest | None = None) -> dict[str, Any]:
    """Operator-triggered routine reminder: same eligibility checks as automation."""
    with db() as conn:
        return actions.create_outbox_message(conn, action_type="routine_overdue_reminder", case_id=case_id,
                                             created_by="human:ops.manager", style=(req.style if req else "auto"),
                                             automated=True)


@app.post("/api/reminders/run")
def run_reminders(req: ReminderRequest | None = None) -> dict[str, Any]:
    with db() as conn:
        return actions.queue_routine_reminders(conn, created_by="human:ops.manager", style=(req.style if req else "auto"))


# ---------------------------------------------------------------------------
# Liquidity
# ---------------------------------------------------------------------------

@app.get("/api/liquidity")
def get_liquidity() -> dict[str, Any]:
    with db() as conn:
        p = liquidity.calculate_liquidity_projection(conn)
        p["obligations"] = liquidity.calculate_payout_obligations(conn)
        p["brief"] = (get_state(conn, "liquidity_brief") or None)
    return p


@app.post("/api/liquidity/explain")
async def explain_liquidity() -> dict[str, Any]:
    from .agents import specialists as sp
    from .agents.supervisor import _finish, _new_deps
    import time
    t0 = time.perf_counter()
    deps = _new_deps("liquidity_brief", "optimized", "ops.manager", "Explain the 7-day payout position")
    from .agents.runtime import run_agent
    out, info = await run_agent(sp.liquidity_agent, "liquidity", "Assess payout liquidity for the next 7 days.",
                                deps.child("liquidity"), request_limit=4, label="liquidity_page")
    brief = out.model_dump() if out else None
    if brief:
        from .safety import grounding_check
        brief["grounding"] = grounding_check(brief["headline"] + " " + brief["explanation"], deps.tool_outputs)
        brief["run_id"] = deps.run_id
        with db() as conn:
            brief["figures_basis_gap"] = liquidity.calculate_liquidity_projection(conn)["payout_gap"]
        with db() as conn:
            set_state(conn, "liquidity_brief", brief)
    _finish(deps, status="ok" if brief else "error", stop_reason=info["stop_reason"], answer=json.dumps(brief), t0=t0)
    return {"run_id": deps.run_id, "brief": brief, **info}


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@app.get("/api/customers")
def customers(q: str | None = None) -> list[dict[str, Any]]:
    with db() as conn:
        if q:
            res = queries.find_customers(conn, q, limit=25)
        else:
            res = [{"customer_id": r["customer_id"], "name": queries.customer_name(conn, r["customer_id"]),
                    "flag": r["title"], "case_id": r["case_id"]}
                   for r in rows(conn, "SELECT customer_id, title, case_id FROM reconciliation_cases WHERE customer_id IS NOT "
                                       "NULL AND case_type != 'OVERDUE_FOLLOWUP' ORDER BY priority_score DESC LIMIT 25")]
        return res


@app.get("/api/customers/{customer_id}")
def customer(customer_id: str) -> dict[str, Any]:
    with db() as conn:
        p = queries.get_customer_profile(conn, customer_id, for_ui=True)
    if not p:
        raise HTTPException(404, "Customer not found")
    return p


# ---------------------------------------------------------------------------
# Approvals / outbox / tasks
# ---------------------------------------------------------------------------

@app.get("/api/approvals")
def approvals(status: str = "pending") -> list[dict[str, Any]]:
    with db() as conn:
        sql = "SELECT * FROM approvals" + ("" if status == "all" else " WHERE status = ?") + " ORDER BY created_at DESC"
        res = rows(conn, sql, () if status == "all" else (status,))
        for a in res:
            a["payload"] = _json(a.pop("payload_json"))
            a["evidence"] = _json(a.pop("evidence_json"))
            a["execution_result"] = _json(a["execution_result"])
            a["customer_name"] = queries.customer_name(conn, a["customer_id"])
            case = one(conn, "SELECT case_type, title, ai_decision_json FROM reconciliation_cases WHERE case_id=?", (a["case_id"],))
            a["case_type"] = case["case_type"] if case else None
            a["case_title"] = case["title"] if case else None
            a["ai_decision"] = _json(case["ai_decision_json"]) if case else None
        return res


@app.post("/api/approvals/{approval_id}/decision")
def decide(approval_id: str, req: ApprovalDecisionRequest) -> dict[str, Any]:
    # The only route that executes a material action. Actor is always recorded as a human operator.
    with db() as conn:
        return actions.decide_approval(conn, approval_id, decision=req.decision, decided_by=f"human:{req.decided_by}",
                                       note=req.note)


@app.get("/api/outbox")
def outbox() -> list[dict[str, Any]]:
    with db() as conn:
        return rows(conn, "SELECT * FROM outbox ORDER BY created_at DESC, message_id LIMIT 600")


@app.get("/api/tasks")
def tasks() -> list[dict[str, Any]]:
    with db() as conn:
        return rows(conn, "SELECT * FROM internal_tasks ORDER BY created_at DESC LIMIT 200")


@app.get("/api/state/{key}")
def state(key: str) -> Any:
    if key not in ("triage_report", "liquidity_brief", "action_report", "ai_review"):
        raise HTTPException(404, "Unknown state key")
    with db() as conn:
        return get_state(conn, key)


@app.get("/api/policy")
def policy() -> dict[str, Any]:
    return {k: {"gate": v[0].value, "why": v[1]} for k, v in ACTION_POLICY.items()}


# ---------------------------------------------------------------------------
# Agents: Ask, morning review, traces
# ---------------------------------------------------------------------------

@app.post("/api/ask")
async def ask(req: AskRequest) -> dict[str, Any]:
    from .agents.supervisor import ask as run_ask
    return await run_ask(req.message, session_id=req.session_id, language_style=req.language or "auto")


async def _morning() -> None:
    from .agents.supervisor import morning_review
    try:
        r = await morning_review()
        with db() as conn:
            if r.get("decisions") is not None:
                pass
    except Exception as e:  # surface failures in the UI instead of hanging
        with db() as conn:
            set_state(conn, "ai_review", {"status": "error", "error": str(e)[:300]})


@app.post("/api/morning-review")
async def start_morning_review() -> dict[str, Any]:
    t = _background.get("morning")
    if t and not t.done():
        return {"status": "running"}
    with db() as conn:
        set_state(conn, "ai_review", {"status": "running"})
    _background["morning"] = asyncio.create_task(_morning())
    return {"status": "started"}


@app.get("/api/runs")
def runs(limit: int = 60) -> list[dict[str, Any]]:
    return audit.list_runs(limit)


@app.get("/api/runs/{run_id}")
def run(run_id: str) -> dict[str, Any]:
    r = audit.get_run(run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    return r


@app.get("/api/audit")
def audit_events(event_type: str | None = None, limit: int = 300, all_epochs: bool = False) -> dict[str, Any]:
    return {"chain": audit.verify_chain(), "events": audit.list_events(limit, event_type, epoch_only=not all_epochs)}


@app.get("/api/usage")
def usage() -> dict[str, Any]:
    return {"totals": audit.usage_totals(), "pricing": pricing_table()}


# ---------------------------------------------------------------------------
# Labs (results produced by the evaluation scripts) + live safety test
# ---------------------------------------------------------------------------

def _read_output(name: str) -> Any:
    p = config.OUTPUT_DIR / name
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


@app.get("/api/lab/{name}")
def lab(name: str) -> Any:
    files = {"evaluation": "evaluation_results.json", "safety": "safety_results.json", "performance": "token_benchmark.json",
             "manual": "manual_benchmark.json"}
    if name not in files:
        raise HTTPException(404, "Unknown lab")
    return {"result": _read_output(files[name]), "usage": audit.usage_totals(), "pricing": pricing_table()}


@app.post("/api/lab/safety/run/{test_id}")
async def run_safety_test(test_id: str) -> dict[str, Any]:
    from evaluation.safety_suite import run_single_test
    return await run_single_test(test_id)


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    from .agents import supervisor
    from .demo_reset import reset_demo
    supervisor._SESSIONS.clear()
    return reset_demo(actor="admin:ui-reset")


@app.post("/api/export")
def export() -> dict[str, str]:
    return audit.export_files()


# ---------------------------------------------------------------------------
# Built frontend (optional single-port mode): serve frontend/dist if present
# ---------------------------------------------------------------------------
if config.FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        f = config.FRONTEND_DIST / path
        if path and f.is_file():
            return FileResponse(f)
        return FileResponse(config.FRONTEND_DIST / "index.html")
