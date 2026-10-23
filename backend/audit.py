"""Append-only audit trail, agent-run traces and token-usage log.

Stored in data/audit.db, separate from the operating database, so Reset Demo
never erases it. SQLite triggers reject UPDATE and DELETE on audit_events, and
each event carries a SHA-256 hash chained to the previous event, so tampering is
detectable (verify_chain). No agent tool can write to this module except through
the `log` function, and no function here deletes anything.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    demo_epoch INTEGER NOT NULL,
    event_type TEXT NOT NULL,      -- tool_call | agent | approval | outbox | safety_block | mutation | system
    actor TEXT NOT NULL,
    run_id TEXT,
    subject TEXT,
    details TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    demo_epoch INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    kind TEXT NOT NULL,            -- ask | morning_review | case_review | benchmark | safety | evaluation
    mode TEXT NOT NULL,            -- optimized | naive | weak
    requested_by TEXT NOT NULL,
    query TEXT NOT NULL,
    status TEXT NOT NULL,
    stop_reason TEXT,
    latency_ms INTEGER,
    model_requests INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    cached_input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    tool_calls INTEGER DEFAULT 0,
    steps TEXT NOT NULL DEFAULT '[]',
    answer TEXT
);

CREATE TABLE IF NOT EXISTS usage_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT,
    agent TEXT NOT NULL,
    mode TEXT NOT NULL,
    model TEXT NOT NULL,
    model_requests INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    label TEXT
);
"""

_lock = threading.RLock()
_initialized_paths: set[str] = set()


def _conn() -> sqlite3.Connection:
    p = Path(config.AUDIT_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    if str(p) not in _initialized_paths:
        conn.executescript(AUDIT_SCHEMA)
        _initialized_paths.add(str(p))
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def current_epoch() -> int:
    with _lock:
        conn = _conn()
        try:
            r = conn.execute("SELECT COALESCE(MAX(demo_epoch), 0) FROM audit_events WHERE event_type = 'system' "
                             "AND subject = 'demo_reset'").fetchone()
            return int(r[0] or 0)
        finally:
            conn.close()


def _compact(obj: Any, limit: int = 4000) -> str:
    s = json.dumps(obj, default=str, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + '..."[truncated]"'


def log(event_type: str, actor: str, details: dict[str, Any], *, run_id: str | None = None,
        subject: str | None = None, epoch: int | None = None) -> str:
    """Append one audit event (hash-chained)."""
    with _lock:
        conn = _conn()
        try:
            if epoch is None:
                r = conn.execute("SELECT COALESCE(MAX(demo_epoch), 0) FROM audit_events WHERE event_type='system' "
                                 "AND subject='demo_reset'").fetchone()
                epoch = int(r[0] or 0)
            prev = conn.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev[0] if prev else "GENESIS"
            event_id = "EV-" + uuid.uuid4().hex[:12]
            ts = now_iso()
            det = _compact(details)
            h = hashlib.sha256("|".join([prev_hash, event_id, ts, str(epoch), event_type, actor, run_id or "",
                                         subject or "", det]).encode()).hexdigest()
            conn.execute("INSERT INTO audit_events(event_id, ts, demo_epoch, event_type, actor, run_id, subject, details, "
                         "prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (event_id, ts, epoch, event_type, actor, run_id, subject, det, prev_hash, h))
            conn.commit()
            return event_id
        finally:
            conn.close()


def verify_chain() -> dict[str, Any]:
    with _lock:
        conn = _conn()
        try:
            prev_hash = "GENESIS"
            n = 0
            for r in conn.execute("SELECT * FROM audit_events ORDER BY seq"):
                h = hashlib.sha256("|".join([r["prev_hash"], r["event_id"], r["ts"], str(r["demo_epoch"]),
                                             r["event_type"], r["actor"], r["run_id"] or "", r["subject"] or "",
                                             r["details"]]).encode()).hexdigest()
                if r["prev_hash"] != prev_hash or h != r["hash"]:
                    return {"valid": False, "broken_at_seq": r["seq"], "events_checked": n}
                prev_hash = r["hash"]
                n += 1
            return {"valid": True, "events_checked": n}
        finally:
            conn.close()


def list_events(limit: int = 200, event_type: str | None = None, epoch_only: bool = True,
                subject: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        conn = _conn()
        try:
            sql = "SELECT * FROM audit_events WHERE 1=1"
            params: list[Any] = []
            if epoch_only:
                sql += " AND demo_epoch = (SELECT COALESCE(MAX(demo_epoch),0) FROM audit_events WHERE event_type='system' AND subject='demo_reset')"
            if event_type:
                sql += " AND event_type = ?"
                params.append(event_type)
            if subject:
                sql += " AND subject = ?"
                params.append(subject)
            sql += " ORDER BY seq DESC LIMIT ?"
            params.append(limit)
            out = []
            for r in conn.execute(sql, params):
                d = dict(r)
                d["details"] = json.loads(d["details"]) if d["details"].startswith("{") else d["details"]
                out.append(d)
            return out
        except json.JSONDecodeError:
            return []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Agent runs (traces)
# ---------------------------------------------------------------------------

def start_run(kind: str, mode: str, requested_by: str, query: str) -> str:
    run_id = "RUN-" + datetime.now().strftime("%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    with _lock:
        conn = _conn()
        try:
            conn.execute("INSERT INTO agent_runs(run_id, demo_epoch, started_at, kind, mode, requested_by, query, status) "
                         "VALUES (?,?,?,?,?,?,?,?)", (run_id, current_epoch(), now_iso(), kind, mode, requested_by,
                                                      query[:2000], "running"))
            conn.commit()
        finally:
            conn.close()
    log("agent", requested_by, {"phase": "run_started", "kind": kind, "mode": mode, "query": query[:300]}, run_id=run_id)
    return run_id


def finish_run(run_id: str, *, status: str, stop_reason: str, latency_ms: int, steps: list[dict], totals: dict,
               answer: str | None) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "UPDATE agent_runs SET finished_at=?, status=?, stop_reason=?, latency_ms=?, model_requests=?, "
                "input_tokens=?, cached_input_tokens=?, output_tokens=?, cost_usd=?, tool_calls=?, steps=?, answer=? "
                "WHERE run_id=?",
                (now_iso(), status, stop_reason, latency_ms, totals.get("model_requests", 0), totals.get("input_tokens", 0),
                 totals.get("cached_input_tokens", 0), totals.get("output_tokens", 0), totals.get("cost_usd", 0.0),
                 totals.get("tool_calls", 0), json.dumps(steps, default=str), (answer or "")[:8000], run_id))
            conn.commit()
        finally:
            conn.close()
    log("agent", "system", {"phase": "run_finished", "status": status, "stop_reason": stop_reason,
                            "latency_ms": latency_ms, **{k: totals.get(k) for k in
                                                          ("model_requests", "input_tokens", "cached_input_tokens",
                                                           "output_tokens", "cost_usd", "tool_calls")}}, run_id=run_id)


def get_run(run_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = _conn()
        try:
            r = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["steps"] = json.loads(d["steps"] or "[]")
            return d
        finally:
            conn.close()


def list_runs(limit: int = 50, kinds: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    with _lock:
        conn = _conn()
        try:
            sql = "SELECT run_id, demo_epoch, started_at, finished_at, kind, mode, requested_by, query, status, stop_reason, " \
                  "latency_ms, model_requests, input_tokens, cached_input_tokens, output_tokens, cost_usd, tool_calls FROM agent_runs"
            params: list[Any] = []
            if kinds:
                sql += " WHERE kind IN (%s)" % ",".join("?" * len(kinds))
                params.extend(kinds)
            sql += " ORDER BY started_at DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Usage events
# ---------------------------------------------------------------------------

def record_usage(*, run_id: str | None, agent: str, mode: str, model: str, model_requests: int, input_tokens: int,
                 cached_input_tokens: int, output_tokens: int, latency_ms: int, cost_usd: float,
                 label: str | None = None) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("INSERT INTO usage_events(ts, run_id, agent, mode, model, model_requests, input_tokens, "
                         "cached_input_tokens, output_tokens, latency_ms, cost_usd, label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (now_iso(), run_id, agent, mode, model, model_requests, input_tokens, cached_input_tokens,
                          output_tokens, latency_ms, cost_usd, label))
            conn.commit()
        finally:
            conn.close()


def usage_events(limit: int = 5000) -> list[dict[str, Any]]:
    with _lock:
        conn = _conn()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM usage_events ORDER BY seq DESC LIMIT ?", (limit,))]
        finally:
            conn.close()


def usage_totals() -> dict[str, Any]:
    with _lock:
        conn = _conn()
        try:
            r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(model_requests),0) req, COALESCE(SUM(input_tokens),0) inp, "
                             "COALESCE(SUM(cached_input_tokens),0) cached, COALESCE(SUM(output_tokens),0) outp, "
                             "COALESCE(SUM(cost_usd),0) cost FROM usage_events").fetchone()
            by_agent = [dict(x) for x in conn.execute(
                "SELECT agent, mode, COUNT(*) calls, SUM(model_requests) model_requests, SUM(input_tokens) input_tokens, "
                "SUM(cached_input_tokens) cached_input_tokens, SUM(output_tokens) output_tokens, SUM(cost_usd) cost_usd, "
                "AVG(latency_ms) avg_latency_ms FROM usage_events GROUP BY agent, mode ORDER BY cost_usd DESC")]
            return {"agent_invocations": r["n"], "model_requests": r["req"], "input_tokens": r["inp"],
                    "cached_input_tokens": r["cached"], "output_tokens": r["outp"], "cost_usd": r["cost"],
                    "by_agent": by_agent}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Exports for graders (valid JSON files under output/)
# ---------------------------------------------------------------------------

def export_files() -> dict[str, str]:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = _conn()
        try:
            events = []
            for r in conn.execute("SELECT * FROM audit_events ORDER BY seq"):
                d = dict(r)
                try:
                    d["details"] = json.loads(d["details"])
                except json.JSONDecodeError:
                    pass
                events.append(d)
            usage = [dict(r) for r in conn.execute("SELECT * FROM usage_events ORDER BY seq")]
        finally:
            conn.close()
    p1 = config.OUTPUT_DIR / "audit_trail.json"
    p2 = config.OUTPUT_DIR / "usage_log.json"
    p1.write_text(json.dumps({"chain": verify_chain(), "events": events}, indent=1, ensure_ascii=False), encoding="utf-8")
    p2.write_text(json.dumps({"totals": usage_totals(), "events": usage}, indent=1), encoding="utf-8")
    return {"audit_trail": str(p1), "usage_log": str(p2)}
