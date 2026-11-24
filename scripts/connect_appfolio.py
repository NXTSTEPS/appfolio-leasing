"""Create an encrypted browser session after a human completes AppFolio login.

This script opens a normal visible browser. It does not enter a password,
intercept MFA, defeat CAPTCHA, or submit a login automatically.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Error, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.session_vault import save_session


def main() -> None:
    login_url = os.environ.get("APPFOLIO_LOGIN_URL", "")
    service_email = os.environ.get("APPFOLIO_SERVICE_EMAIL", "")
    data_key = os.environ.get("BRIDGE_DATA_KEY", "")
    if not login_url or not service_email or not data_key:
        raise SystemExit("Set APPFOLIO_LOGIN_URL, APPFOLIO_SERVICE_EMAIL, and BRIDGE_DATA_KEY first.")
    if len(data_key) < 24:
        raise SystemExit("BRIDGE_DATA_KEY must be at least 24 characters long. Choose a new key and rerun before logging in.")

    output = Path(os.environ.get("APPFOLIO_SESSION_PATH", "work/appfolio-session.enc"))
    print(f"Opening AppFolio sign-in for {service_email}. Complete login and MFA in the browser.")
    print("Do not enter credentials into this terminal. Return here only after you reach AppFolio.")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            try:
                page = browser.new_page()
                page.goto(login_url, wait_until="domcontentloaded")
                input("After you are signed into AppFolio, press Enter here to save the encrypted session: ")
                save_session(output, page.context.storage_state(), data_key)
            finally:
                browser.close()
    except Error as exc:
        raise SystemExit(
            "Could not start the managed browser. Install the approved Playwright Chromium runtime, then rerun. "
            f"Details: {exc}"
        ) from exc
    print(f"Encrypted session saved to {output}. Keep this file private and do not commit it.")


if __name__ == "__main__":
    main()
