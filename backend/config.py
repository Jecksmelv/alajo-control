"""Central configuration: paths, environment loading, model settings and policy limits.

The .env file is looked up in the project directory first and then one directory
above it (graders may keep a single .env in a parent submissions folder). Values
are never printed or logged.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"            # generated runtime state (gitignored)
OUTPUT_DIR = PROJECT_DIR / "output"        # evidence files for graders
EVAL_DIR = PROJECT_DIR / "evaluation"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"

ENV_SEARCH_PATHS = [PROJECT_DIR / ".env", PROJECT_DIR.parent / ".env"]
ENV_FILES_LOADED: list[str] = []
for _env_path in ENV_SEARCH_PATHS:
    if _env_path.is_file():
        # Earlier files win: a project-level .env overrides a parent-level one.
        load_dotenv(_env_path, override=False)
        ENV_FILES_LOADED.append("project" if _env_path.parent == PROJECT_DIR else "parent")

DB_PATH = Path(os.environ.get("ALAJO_DB_PATH", DATA_DIR / "alajo.db"))
AUDIT_DB_PATH = Path(os.environ.get("ALAJO_AUDIT_DB_PATH", DATA_DIR / "audit.db"))

# ---------------------------------------------------------------------------
# Model wiring (Portkey gateway, OpenAI-compatible Responses API)
# ---------------------------------------------------------------------------
MODEL_NAME = os.environ.get("ALAJO_MODEL", "gpt-6-luna")   # course requirement: gpt-6-luna only
PORTKEY_BASE_URL = os.environ.get("PORTKEY_BASE_URL", "https://api.portkey.ai/v1")
PORTKEY_PROVIDER = os.environ.get("PORTKEY_PROVIDER", "openai")


def portkey_key() -> str:
    return os.environ.get("PORTKEY_API_KEY", "").strip()


def ai_configured() -> bool:
    return bool(portkey_key())


# ---------------------------------------------------------------------------
# Demo clock. The synthetic operating day is fixed so every run is reproducible.
# ---------------------------------------------------------------------------
DEMO_TODAY = date(2026, 10, 21)              # Wednesday
DEMO_NOW = datetime(2026, 10, 21, 7, 30)     # morning control-room snapshot
RANDOM_SEED = 409

# ---------------------------------------------------------------------------
# Operating policy (deterministic; documented in README)
# ---------------------------------------------------------------------------
GRACE_DAYS = 1                 # a contribution is "confirmed overdue" only after this grace
PROVIDER_SLA_DAYS = 1          # provider settlements are expected T+1
CANDIDATE_WINDOW_BEFORE = 7    # days before a transfer a candidate contribution may fall due
CANDIDATE_WINDOW_AFTER = 2
MAX_CANDIDATES = 5
LIQUIDITY_HORIZON_DAYS = 7
OPERATING_BUFFER_NGN = 2_000_000   # treasury policy minimum (warning line, not a hard stop)

# ---------------------------------------------------------------------------
# Agent limits (code-level, enforced by PydanticAI UsageLimits + our counters)
# ---------------------------------------------------------------------------
MAX_REQUESTS_PER_AGENT_RUN = 8
MAX_TOOL_CALLS_PER_AGENT_RUN = 12
MAX_DELEGATIONS_PER_QUERY = 6
MAX_MODEL_REQUESTS_PER_QUERY = 30
MAX_OUTPUT_TOKENS = 2_500
CHAT_HISTORY_TURNS = 3         # optimized mode keeps only the last 3 exchanges
