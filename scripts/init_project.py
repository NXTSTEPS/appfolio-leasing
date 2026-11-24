"""Create (or re-key) the bridge project for this company. No AppFolio access needed.

Called by bootstrap_bridge.ps1 with BRIDGE_BOOTSTRAP_API_KEY set.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import settings as _settings  # noqa: E402
from app.store import store  # noqa: E402


def main() -> None:
    api_key = os.environ.get("BRIDGE_BOOTSTRAP_API_KEY", "")
    if not api_key:
        raise SystemExit("BRIDGE_BOOTSTRAP_API_KEY is not set; run scripts\\bootstrap_bridge.ps1")
    project = store.project_by_email(_settings.SERVICE_EMAIL)
    if project is None:
        store.create_project(_settings.COMPANY_NAME, _settings.COMPANY_DOMAIN, _settings.SERVICE_EMAIL, api_key=api_key)
        print(f"Created bridge project for {_settings.COMPANY_NAME} ({_settings.SERVICE_EMAIL}).")
    else:
        store.set_api_key(project["id"], api_key)
        print("Bridge project already existed; its local API key was rotated.")


if __name__ == "__main__":
    main()
