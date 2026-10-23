# Alajo Control

**AI operations control tower for community savings.**

Most digital savings products solve the digitization problem. Alajo Control solves the *truth-and-action* problem that emerges after digitization: it reconciles money across imperfect sources (internal ledger, bank transfers, payment-provider settlements, field-agent cash collections, customer messages), decides which customers genuinely need follow-up, shows whether upcoming payouts are covered, explains the evidence behind every recommendation, automates only low-risk administrative actions, and puts every financial or account-changing action behind a code-enforced human approval gate — with a complete audit trail and measured AI cost.

It is an internal tool for Alajo's operations and finance team (not a customer app and not a chatbot). It runs on a synthetic but realistic operating day: **5,000 active savers**, 14 Nigerian locations, 8 ajo/esusu-style plans, 47,677 expected contributions, 1,210 bank transfers, 461 provider settlements, 533 field-agent collections and 14 planted ground-truth scenarios.

> The prototype uses a synthetic operating dataset modeled on realistic Nigerian community-savings workflows; no real customer data are included.

Deliverables: [`report.html`](report.html) (written report) · [`slides.html`](slides.html) (10-slide deck) · measured evidence in [`output/`](output/).

---

## Measured results (from `output/*.json`)

| | Result |
|---|---|
| Ground-truth scenarios (14 planted + 7 additional) | **21 / 21 passed** · reconciliation decisions 17 / 17 · false matches 0 · escalations 9 / 9 |
| False reminders (all 5,000 savers) | ledger-only rule would wrongly chase **108**; system sent **0** → 108 prevented · precision 100.0% · recall 94.0% |
| Safety Lab | **10 / 10 passed** (incl. weak-vs-hardened contrast; weak config compromised only in its sandbox) |
| Token benchmark (same 17 cases) | naive 4,383,369 input tokens, $1.1024, accuracy 82% → optimized 18,420 tokens (10,125 cached), $0.0043, accuracy 100% |
| Full optimized morning review | 19 model calls · $0.0070 · 95 s |
| Human baseline | not yet run — protocol in `evaluation/manual_test_packet/` |

## Architecture

```
React + Vite + TypeScript (frontend/)            FastAPI (backend/main.py)
  Control Room · Reconciliation · Exceptions  ──▶  /api/*  ──▶  deterministic tools (backend/tools/)
  Liquidity · Customer 360 · Approvals              │          finance.py  reconciliation.py  liquidity.py
  Outbox · Ask · Agent Trace · Labs                 │          queries.py  actions.py (the only write path)
                                                    ▼
                                    PydanticAI agents (backend/agents/) via Portkey
             Supervisor ──▶ Money Match · Exception & Risk · Liquidity & Payout · Customer Action
                                                    │
        SQLite: data/alajo.db (operating day, rebuilt by Reset Demo)   data/audit.db (append-only, survives reset)
```

* **Deterministic first.** A rule engine (`backend/tools/reconciliation.py`) posts only exact, single-interpretation evidence (exact `ALJ-Cxxxxx` reference, settled provider payments, balanced field-agent batches), detects duplicates, agent shortfalls, late settlements, partial payments and disputes, and verifies overdue status before any reminder. All money arithmetic is SQL/Python.
* **AI only where judgment is needed.** Money Match receives compact evidence for the few genuinely ambiguous transfers; Exception & Risk triages; Liquidity explains a deterministic 7-day projection; Customer Action chooses safe next steps. The Supervisor delegates and synthesizes. Every agent has its own prompt, tools and structured output.
* **Code-level controls.** `backend/safety.py` classifies every action as auto-allowed, approval-required or forbidden; `backend/tools/actions.py` refuses ineligible reminders and executes material actions only inside the human approval endpoint; SQLite triggers make the ledger and audit log append-only. Customer text is sanitized and treated as data; sensitive fields never reach a model; naira figures in answers are checked against tool outputs.

## Prerequisites

* Python **3.11+** (tested with 3.12)
* Node.js **20+** and npm (tested with Node 24)
* A Portkey API key with access to `gpt-5.6-luna` (the course key). Without a key, all deterministic features still work; AI buttons report that the key is missing.

## Setup

> Windows tip: clone to a short path (e.g. `C:/work/alajo-control`). Very long paths can exceed Windows' 260-character limit and break `pip`/`venv`.

### 1. Environment file

Alajo Control loads `.env` from **the project folder first, then one folder above it**. A grader can therefore keep one `.env` in a parent submissions directory without touching the project.

```bash
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
# then set PORTKEY_API_KEY=...   (or place that line in ../.env instead)
```

### 2. Python backend

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Windows
.venv/bin/python -m pip install -r requirements.txt         # macOS / Linux
```

### 3. Frontend

```bash
cd frontend
npm install
cd ..
```

### 4. Synthetic data (optional — happens automatically on first start)

```bash
.venv/Scripts/python scripts/generate_demo_data.py          # Windows
.venv/bin/python scripts/generate_demo_data.py              # macOS / Linux
```

This rebuilds `data/alajo.db` from a fixed seed (about 4 seconds), runs the deterministic morning reconciliation, writes `evaluation/ground_truth.json` (evaluation only, never read by agents) and exports the raw source files to `demo_data/*.csv`.

## Run

Terminal 1 — backend (from the project root):

```bash
.venv/Scripts/python -m uvicorn backend.main:app --port 8000     # Windows
.venv/bin/python -m uvicorn backend.main:app --port 8000         # macOS / Linux
```

Terminal 2 — frontend:

```bash
cd frontend
npm run dev
```

Open **http://localhost:5173**. The API is at http://localhost:8000/api (e.g. `/api/health`, `/api/summary`).
Single-port alternative: `cd frontend && npm run build`, then the backend also serves the built UI at http://localhost:8000.

## 5-Minute Demo

1. **Morning Control Room** — real SQLite figures: expected today, reconciled this morning, suspense, confirmed overdue, under investigation, 7-day payouts, projected low point and the **Friday 23 Oct payout gap**. Click **Run morning AI review** (about 1 minute; the Supervisor runs Money Match on every ambiguous transfer, then triage, liquidity, customer actions and a grounded brief).
2. **Reconciliation → Unmatched transfers** → open *“Transfer from Musa Ibrahim may be Aisha Bello's contribution”*.
3. Read the **Why?** panel: the unpaid ledger contribution, the transfer from Musa Ibrahim, Aisha's own WhatsApp message naming her husband's account, timing, the conflict check, and the Money Match judgment with its confidence.
4. Click **Approve**. The ledger posts ₦20,000 (re-verified against SQL), Aisha's overdue case closes, an acknowledgment is queued in the Outbox, and the decision is logged (Labs → Audit trail).
5. **Reconciliation → Verified overdue** → open *Tunde Bakare* and click **Send safe reminder** (or see it already queued in **Outbox**). Then open **Reminders held** → *Halima Yusuf*: the ledger says unpaid, but a provider settlement is in transit — the reminder gate refuses it.
6. **Liquidity & Payouts** — the deterministic projection, the Friday shortfall, and which unresolved inflows close it (the delayed PayLink batch). Click **Explain position** for the Liquidity agent's grounded explanation.
7. **Ask Alajo Control** → “What needs my attention today?” (watch the specialists consulted, figures-grounded badge, tokens and cost).
8. **Agent Trace** — handoffs, tool calls with compact arguments/results, decisions, guardrails, tokens, cost and latency for that run.
9. **Labs → Safety Lab** → **Run prompt-injection test** — the same attack against a deliberately weak configuration (sandbox only) and the hardened system.
10. **Labs → Performance Lab** — measured naive vs optimized tokens, latency, cost and accuracy; **Impact & Evaluation** — ground-truth results.

**Reset Demo** (top bar, or `scripts/generate_demo_data.py`) rebuilds the exact deterministic day. It is an explicit administrator action: the audit trail is **not** erased; a new demo epoch begins and screens filter to it.

## Evaluation, safety and benchmark commands

All write machine-readable results to `output/` and use the live model (they need the key). Run from the project root with the venv interpreter (`.venv/Scripts/python` on Windows, `.venv/bin/python` elsewhere):

| What | Command | Output |
|---|---|---|
| Ground-truth evaluation (planted scenarios, false reminders across all 5,000 savers, approval path) | `python -m evaluation.run_evaluation` | `output/evaluation_results.json` |
| Token benchmark, naive vs optimized on the same 17 cases | `python -m evaluation.run_token_benchmark` | `output/token_benchmark.json` |
| Safety Lab red-team suite (9 tests incl. weak-vs-hardened) | `python -m evaluation.safety_suite` | `output/safety_results.json` |
| Manual-vs-system test packet (24 items, human protocol) | `python -m evaluation.build_manual_packet` | `evaluation/manual_test_packet/`, `output/manual_benchmark.json` |
| Export audit trail + usage log | `curl -X POST localhost:8000/api/export` (or any suite above) | `output/audit_trail.json`, `output/usage_log.json` |
| Real screenshots (app running; `python -m playwright install chromium` once) | `python scripts/capture_screenshots.py` | `output/screenshots/` |

The evaluation and benchmark scripts reset the demo database before they run.

## Tests

```bash
.venv/Scripts/python -m pytest -q        # Windows   (41 deterministic tests, no model calls, isolated temp DB)
.venv/bin/python -m pytest -q            # macOS / Linux
cd frontend && npm run build             # type-check + production build
```

Tests cover financial calculations, exact reconciliation, duplicate detection, reminder gating, approval gating, privacy scoping, prohibited ledger mutation (code + DB triggers), sanitization, grounding, liquidity determinism, audit append-only/hash chain, deterministic reset and the API.

## Project structure

```
alajo-control/
├── README.md  report.html  slides.html  requirements.txt  .env.example  .gitignore
├── backend/
│   ├── main.py            FastAPI routes (approval endpoint = only path that executes material actions)
│   ├── config.py          paths, .env lookup (project dir, then parent), model + policy limits
│   ├── database.py        SQLite schema (append-only ledger triggers), helpers
│   ├── models.py          Pydantic models (agent outputs, API payloads)
│   ├── safety.py          action policy, sanitizer, privacy scope, output guard, numeric grounding
│   ├── audit.py           append-only hash-chained audit trail, agent-run traces, usage log
│   ├── usage.py           cited pricing + cost calculation
│   ├── demo_reset.py      Reset Demo (keeps audit history)
│   ├── agents/            runtime.py · supervisor.py · specialists.py · naive.py (benchmark/safety baselines)
│   ├── tools/             finance.py · reconciliation.py · liquidity.py · queries.py · actions.py
│   └── prompts/           system prompts (shared safety rules + one per agent)
├── frontend/              React + Vite + TypeScript UI
├── scripts/               generate_demo_data.py · capture_screenshots.py
├── demo_data/             raw source files of the demo day (CSV export)
├── evaluation/            ground_truth.json (eval only) · run_evaluation.py · run_token_benchmark.py ·
│                          safety_suite.py · build_manual_packet.py · score_manual.py · manual_test_packet/
├── tests/                 pytest suite
├── output/                evaluation_results.json · safety_results.json · token_benchmark.json ·
│                          usage_log.json · audit_trail.json · manual_benchmark.json · screenshots/
└── docs/research_notes.md sources used in the report
```

## Configuration and policy

* Model: `gpt-5.6-luna` via `https://api.portkey.ai/v1` (`x-portkey-api-key` header). Override with `ALAJO_MODEL`.
* Demo clock: Wednesday 21 Oct 2026, 07:30 WAT. Grace period 1 day; provider settlement SLA T+1; operating buffer ₦2,000,000.
* Limits per query: 30 model requests, 6 delegations, 8 requests / 12 tool calls per agent loop, 2,500 output tokens per response; Ask keeps the last 3 exchanges.
* Portkey's response replay cache is bypassed (`x-portkey-cache-force-refresh`) so every measured call is real; provider prompt caching still applies and is measured.

## Limitations

Synthetic data; simulated WhatsApp/SMS outbox and simulated payment-provider feed; heuristic candidate generation for unreferenced transfers; no real money movement or bank access; single-operator demo without authentication; cost uses OpenAI list prices while the course gateway routes to an Azure deployment; scale beyond one demo day is modeled, not load-tested. See `report.html` §11.
