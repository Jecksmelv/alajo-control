"""Capture REAL screenshots of the running app (used by report.html and slides.html).

Prerequisites: backend on :8000 and frontend on :5173 are running (see README), and
Playwright's Chromium is installed:  python -m playwright install chromium

    python scripts/capture_screenshots.py [--base http://127.0.0.1:5173] [--no-reset]

It follows the 5-minute demo: Reset Demo -> morning AI review (live model) -> spouse
case "Why?" -> approve -> outbox -> liquidity -> Ask -> Agent Trace -> Safety Lab
(live injection test) -> Performance / Evaluation labs. Images go to output/screenshots/.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUT = PROJECT_DIR / "output" / "screenshots"


def api(base: str, path: str, body: dict | None = None, timeout: int = 600) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + "/api" + path, data=data, method="POST" if data is not None or path in
                                 ("/reset", "/morning-review") else "GET", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def shot(page: Page, name: str, full: bool = False) -> None:
    page.wait_for_timeout(700)
    page.screenshot(path=str(OUT / f"{name}.jpg"), type="jpeg", quality=84, full_page=full)
    print("  saved", name)


def go(page: Page, base: str, route: str) -> None:
    page.goto(f"{base}/#/{route}")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(900)


def open_case(page: Page, case_id: str) -> None:
    page.evaluate("(id) => window.alajoOpenCase(id)", case_id)
    page.wait_for_selector(".drawer .money", timeout=30000)
    page.wait_for_timeout(800)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:5173")
    ap.add_argument("--no-reset", action="store_true")
    a = ap.parse_args()
    base = a.base
    OUT.mkdir(parents=True, exist_ok=True)
    if not a.no_reset:
        print("Reset Demo…")
        api(base, "/reset", {})
        print("Morning AI review (live model)…")
        api(base, "/morning-review", {})
        for _ in range(120):
            s = api(base, "/summary")
            if s["ai_review"]["status"] != "running":
                break
            time.sleep(3)
        print("  review status:", s["ai_review"]["status"])
    cases = api(base, "/cases?types=PROBABLE_MATCH&status=all")
    spouse = next(c for c in cases if "Musa" in c["title"])["case_id"]
    overdue = next(c for c in api(base, "/cases?types=OVERDUE_FOLLOWUP&status=all&customer_id=C00977"))["case_id"]
    held = next(c for c in api(base, "/cases?types=OVERDUE_FOLLOWUP&status=all&customer_id=C04021"))["case_id"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1.25)
        go(page, base, "overview")
        shot(page, "01_control_room")
        shot(page, "01b_control_room_full", full=True)
        go(page, base, "reconciliation/unmatched")
        shot(page, "02_reconciliation")
        open_case(page, spouse)
        shot(page, "03_spouse_case_why")
        page.locator(".drawer").evaluate("el => el.scrollTo(0, 650)")
        shot(page, "03b_spouse_case_judgment")
        go(page, base, "approvals")
        shot(page, "04_approvals")
        # approve the spouse match through the UI
        go(page, base, "reconciliation/unmatched")
        open_case(page, spouse)
        page.locator(".drawer").evaluate("el => el.scrollTo(0, 2000)")
        btn = page.locator(".drawer button.btn.approve").first
        if btn.count():
            btn.click()
            page.wait_for_timeout(2500)
            page.locator(".drawer").evaluate("el => el.scrollTo(0, 2000)")
            shot(page, "05_spouse_case_approved")
        go(page, base, "reconciliation/overdue")
        open_case(page, overdue)
        shot(page, "06_genuine_overdue_case")
        go(page, base, "reconciliation/held")
        open_case(page, held)
        shot(page, "06b_reminder_held_settlement")
        go(page, base, "outbox")
        shot(page, "07_outbox")
        go(page, base, "liquidity")
        shot(page, "08_liquidity")
        go(page, base, "exceptions")
        shot(page, "09_exceptions")
        go(page, base, "customers/C01842")
        shot(page, "10_customer_360")
        go(page, base, "ask")
        page.evaluate("sessionStorage.clear()")
        go(page, base, "ask")
        page.fill(".composer input", "What needs my attention today?")
        page.click(".composer button")
        page.wait_for_selector(".msg.bot .msg-foot", timeout=240000)
        shot(page, "11_ask")
        page.click(".msg.bot .msg-foot a")
        page.wait_for_timeout(2500)
        shot(page, "12_agent_trace")
        go(page, base, "labs/safety")
        page.click("text=Run prompt-injection test")
        page.wait_for_selector(".control-box.ok", timeout=240000)
        shot(page, "13_safety_lab")
        go(page, base, "labs/performance")
        shot(page, "14_performance_lab")
        go(page, base, "labs/evaluation")
        shot(page, "15_evaluation_lab")
        go(page, base, "labs/audit")
        shot(page, "16_audit_trail")
        browser.close()
    print("Screenshots written to", OUT)


if __name__ == "__main__":
    sys.exit(main())
