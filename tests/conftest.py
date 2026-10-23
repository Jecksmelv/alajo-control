"""Test setup: an isolated temporary database and audit store (never the demo data)."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="alajo-tests-"))
os.environ["ALAJO_DB_PATH"] = str(_TMP / "alajo_test.db")
os.environ["ALAJO_AUDIT_DB_PATH"] = str(_TMP / "audit_test.db")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def demo_db():
    from backend.demo_reset import reset_demo

    return reset_demo(actor="pytest")
