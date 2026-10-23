"""Reset Demo: rebuild the exact deterministic demo day.

An explicit administrator action. It rebuilds data/alajo.db from the seeded
generator, re-runs the deterministic morning reconciliation and calibrates the
payout account. It does NOT erase the audit trail: it appends a `demo_reset`
event that opens a new demo epoch, and screens filter to the current epoch.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from typing import Any

from . import audit, config
from .database import db, scalar, set_state
from .tools.liquidity import calculate_liquidity_projection
from .tools.reconciliation import run_morning_reconciliation

GAP_SHARE_OF_UNRESOLVED = 0.6   # the Friday gap is set below what resolving open inflows would recover


def _generator():
    path = config.PROJECT_DIR / "scripts" / "generate_demo_data.py"
    spec = importlib.util.spec_from_file_location("alajo_generate_demo_data", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def reset_demo(actor: str = "admin:demo-reset") -> dict[str, Any]:
    t0 = time.perf_counter()
    epoch = audit.current_epoch() + 1
    gen = _generator()
    counts = gen.build_database(config.DB_PATH)
    with db() as conn:
        recon = run_morning_reconciliation(conn)
        # Calibrate the payout account so the planted Friday gap exists (CASE_014). The balance is a
        # synthetic starting condition; the gap itself is then computed by the projection, not stored.
        base = calculate_liquidity_projection(conn, opening_override=0)
        friday = next(d for d in base["days"] if d["date"] == "2026-10-23")
        target_gap = int(round(base["unresolved_inflows_total"] * GAP_SHARE_OF_UNRESOLVED, -4))
        cleared = int(round(base["suspense_restricted"] - friday["closing_position"] - target_gap, -3))
        conn.execute("UPDATE treasury_accounts SET cleared_balance = ? WHERE account_id='PAYOUT-OPS'", (cleared,))
        proj = calculate_liquidity_projection(conn)
        set_state(conn, "initialized", True)
        set_state(conn, "morning_reconciliation", recon)
        set_state(conn, "ai_review", {"status": "not_run"})
        set_state(conn, "demo_epoch", epoch)
        ledger_rows = scalar(conn, "SELECT COUNT(*) FROM ledger_entries")
    summary = {"epoch": epoch, "counts": counts, "ledger_rows_after_rules": ledger_rows, "reconciliation": recon,
               "payout_account_cleared_balance": cleared, "payout_gap": proj["payout_gap"], "gap_date": proj["gap_date"],
               "seconds": round(time.perf_counter() - t0, 2)}
    audit.log("system", actor, {"action": "demo_reset", **{k: summary[k] for k in ("epoch", "counts", "payout_gap",
                                                                                     "gap_date", "seconds")}},
              subject="demo_reset", epoch=epoch)
    audit.log("mutation", "rule-engine", {"action": "morning_reconciliation", **recon}, subject="reconciliation")
    return json.loads(json.dumps(summary, default=str))
