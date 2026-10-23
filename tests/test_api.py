"""HTTP API tests (FastAPI TestClient). No model calls."""

from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)


def test_health_and_summary():
    h = client.get("/api/health").json()
    assert h["ok"] and h["model"]
    s = client.get("/api/summary").json()
    assert s["active_savers"] == 5000
    assert s["expected_today"]["amount"] > 0
    assert s["potential_payout_gap"]["date"] == "2026-10-23"


def test_cases_and_case_detail():
    cases = client.get("/api/cases?types=PROBABLE_MATCH,AMBIGUOUS_INFLOW").json()
    assert cases and all(c["case_type"] in ("PROBABLE_MATCH", "AMBIGUOUS_INFLOW") for c in cases)
    d = client.get(f"/api/cases/{cases[0]['case_id']}").json()
    assert d["evidence"] and d["candidates"]["candidates"]


def test_blocked_reminder_returns_409():
    held = [c for c in client.get("/api/cases?types=OVERDUE_FOLLOWUP").json() if c["reminder_suppressed"]]
    r = client.post(f"/api/cases/{held[0]['case_id']}/remind", json={"style": "auto"})
    assert r.status_code == 409 and r.json()["blocked"]


def test_liquidity_and_customer_endpoints():
    p = client.get("/api/liquidity").json()
    assert len(p["days"]) == 7
    c = client.get("/api/customers/C01842").json()
    assert c["name"] == "Aisha Bello" and "*" in c["contact_masked"]


def test_approval_endpoint_records_human_actor():
    cases = client.get("/api/cases?types=PROBABLE_MATCH").json()
    from backend.database import db
    from backend.tools import actions
    case = client.get(f"/api/cases/{cases[0]['case_id']}").json()
    top = case["candidates"]["candidates"][0]
    with db() as conn:
        ap = actions.create_approval_request(conn, case_id=case["case_id"], action_type="match_payment_to_contribution",
                                             payload={"bank_txn_id": case["bank_txn_id"], "contribution_id": top["contribution_id"],
                                                      "customer_id": top["customer_id"]},
                                             recommendation="api test", requested_by="agent:money_match")
    r = client.post(f"/api/approvals/{ap['approval_id']}/decision", json={"decision": "reject", "note": "test"}).json()
    assert r["decision"] == "reject"
    rec = [a for a in client.get("/api/approvals?status=all").json() if a["approval_id"] == ap["approval_id"]][0]
    assert rec["status"] == "rejected" and rec["decided_by"].startswith("human:")
    audit = client.get("/api/audit?event_type=approval").json()
    assert audit["chain"]["valid"]
