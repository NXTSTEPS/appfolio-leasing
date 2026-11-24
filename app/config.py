from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("BRIDGE_DB_PATH", ROOT / "work" / "appfolio-bridge.sqlite3"))
ADMIN_TOKEN = os.getenv("BRIDGE_ADMIN_TOKEN", "")
DATA_KEY = os.getenv("BRIDGE_DATA_KEY", "")
VENDOR_RESOLUTION_WEBHOOK_URL = os.getenv("N8N_VENDOR_RESOLUTION_WEBHOOK_URL", "")
VENDOR_RESOLUTION_WEBHOOK_TOKEN = os.getenv("N8N_VENDOR_RESOLUTION_WEBHOOK_TOKEN", "")

RESOURCES = {
    "property-groups", "properties", "units", "owners", "tenants", "leases",
    "financials", "vendors", "rent-roll", "vacancies", "work-orders",
    "guest-cards", "rental-applications", "emails", "sms", "attachments",
}
