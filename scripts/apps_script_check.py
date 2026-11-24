"""Test (or decode) the connection between this PC and the Google Sheet.

    python scripts/apps_script_check.py            # ping the Apps Script web app
    python scripts/apps_script_check.py --send-test-email

The ping also tells the script which bridge project it serves; bill approvals
are signed for that project only. Normally run by connect_apps_script.ps1.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import apps_script  # noqa: E402
from app import settings as _settings  # noqa: E402


def project_id() -> str:
    try:
        from app.store import store

        project = store.project_by_email(_settings.SERVICE_EMAIL) or store.default_project()
        return str(project["id"]) if project else ""
    except Exception:  # the database may not exist yet during first setup
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decode", action="store_true",
                        help="decode APPS_SCRIPT_CONNECTION_CODE from the environment and print JSON")
    parser.add_argument("--send-test-email", action="store_true")
    args = parser.parse_args()

    if args.decode:
        try:
            print(json.dumps(apps_script.decode_connection_code(os.environ.get("APPS_SCRIPT_CONNECTION_CODE", ""))))
        except apps_script.AppsScriptError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        return 0

    try:
        answer = apps_script.call("ping", {
            "projectId": project_id(),
            "company": _settings.COMPANY_NAME,
            "appfolioUrl": _settings.APPFOLIO_BASE_URL,
            "host": os.environ.get("COMPUTERNAME", ""),
            "sendTestEmail": bool(args.send_test_email),
        })
    except apps_script.AppsScriptError as exc:
        print(f"NOT CONNECTED: {exc}")
        return 2
    print(f"CONNECTED to '{answer.get('sheet')}' (script version {answer.get('version')}).")
    if answer.get("projectId"):
        print(f"Bill approvals are signed for bridge project {answer['projectId']}.")
    if answer.get("testEmail"):
        print(f"Test email sent to {answer['testEmail']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
