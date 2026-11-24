"""Report whether the saved AppFolio session is still authorized.

Navigates to one buffered report and checks where it lands. Read-only: it opens
a page and reads it, and never clicks, submits, or types a credential.

Exit code 0 means authorized, 1 means the session needs to be refreshed.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from app.appfolio_reports import REPORT_ROUTES
from app.session_vault import load_session
from app import settings as _settings

BASE_URL = os.environ.get("APPFOLIO_BASE_URL", _settings.APPFOLIO_BASE_URL)
PROBE_RESOURCE = "properties"


def main() -> int:
    data_key = os.environ.get("BRIDGE_DATA_KEY", "")
    if not data_key:
        raise SystemExit(
            "BRIDGE_DATA_KEY is not set. Run this through scripts\\run_with_bridge_key.ps1."
        )

    session_path = Path(os.environ.get("APPFOLIO_SESSION_PATH", "work/appfolio-session.enc"))
    state = load_session(ROOT / session_path, data_key)

    now = dt.datetime.now(dt.timezone.utc).timestamp()
    expired = [
        cookie["name"]
        for cookie in state.get("cookies", [])
        if isinstance(cookie.get("expires"), (int, float)) and 0 < cookie["expires"] < now
    ]
    age_hours = (now - (ROOT / session_path).stat().st_mtime) / 3600
    print(f"session file:    {session_path} ({age_hours:.1f} hours old)")
    print(f"expired cookies: {', '.join(expired) if expired else 'none'}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_context(storage_state=state).new_page()
            page.goto(BASE_URL + REPORT_ROUTES[PROBE_RESOURCE], wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(4_000)
            landed_on_login = page.evaluate(
                "() => !!document.querySelector('input[type=password]')"
            )
            print(f"landed on:       {page.url}")
        finally:
            browser.close()

    if landed_on_login or "account.appfolio.com" in page.url:
        print("\nRESULT: NOT authorized. AppFolio bounced the session to its login page.")
        print("Refresh it with scripts\\refresh_appfolio_session.ps1 (keeps the existing key).")
        return 1

    print("\nRESULT: authorized. The collector can run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
