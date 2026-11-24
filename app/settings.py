"""One place for every company-specific value.

Values come from ``config/company.json`` (copied from
``config/company.example.json`` during setup). Environment variables with the
same meaning still win, so existing scheduled tasks keep working.

Nothing secret belongs in company.json. Passwords, API keys and webhook URLs
are stored with Windows DPAPI under ``work/`` (see scripts/set_secret.ps1).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.getenv("BRIDGE_CONFIG", ROOT / "config" / "company.json"))
EXAMPLE_PATH = ROOT / "config" / "company.example.json"
WORK_DIR = Path(os.getenv("BRIDGE_WORK_DIR", ROOT / "work"))


@lru_cache(maxsize=1)
def config() -> dict[str, Any]:
    """Load company.json, falling back to the example so tests and imports work."""
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    return json.loads(path.read_text(encoding="utf-8-sig"))


def get(dotted: str, default: Any = None) -> Any:
    """Read ``"modules.leasing.enabled"``-style keys."""
    node: Any = config()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def is_configured() -> bool:
    return CONFIG_PATH.exists()


def module(name: str) -> dict[str, Any]:
    return dict(get(f"modules.{name}", {}) or {})


def module_enabled(name: str) -> bool:
    return bool(module(name).get("enabled"))


# Frequently used values -----------------------------------------------------

APPFOLIO_BASE_URL: str = (
    os.getenv("APPFOLIO_BASE_URL")
    or os.getenv("APPFOLIO_APP_URL")
    or get("appfolio.base_url", "https://yourcompany.appfolio.com")
).rstrip("/")
APPFOLIO_LOGIN_URL: str = os.getenv("APPFOLIO_LOGIN_URL") or APPFOLIO_BASE_URL + "/"
SERVICE_EMAIL: str = os.getenv("APPFOLIO_SERVICE_EMAIL") or get(
    "appfolio.service_email", "appfolio-bot@yourcompany.com"
)
COMPANY_NAME: str = get("company.name", "Your Property Management Company")
COMPANY_DOMAIN: str = get("company.domain", "yourcompany.com")
TIMEZONE_NAME: str = get("company.timezone", "America/New_York")
TIMEZONE = ZoneInfo(TIMEZONE_NAME)
TASK_PREFIX: str = get("windows.task_prefix", "AppFolio Bridge")
N8N_BASE_URL: str = (os.getenv("N8N_BASE_URL") or get("n8n.base_url", "") or "").rstrip("/")
ADMIN_EMAIL: str = get("company.admin_email", "") or SERVICE_EMAIL


# AppFolio sync reports --------------------------------------------------------
#
# AppFolio allows 500 report runs a day (resets at midnight Eastern) and every
# sync runs one report per resource. Syncing all 7 every 15 minutes is 672 runs
# a day, so by default a sync reads only what the enabled modules use.

ALL_SYNC_RESOURCES = (
    "properties",
    "tenants",
    "rent-roll",
    "vacancies",
    "vendors",
    "rental-applications",
    "income-statement",
)
MODULE_SYNC_RESOURCES: dict[str, tuple[str, ...]] = {
    "leasing": ("tenants", "vacancies", "rental-applications"),
    "delinquency": ("rent-roll", "tenants"),
    "moveout_reminder": ("properties", "rent-roll"),
    "moveout_ledger_review": ("properties", "rent-roll"),
    "bills": ("properties", "vendors"),
    "sale_je": ("properties",),
    "vendor_payment_email": ("vendors",),
}
DEFAULT_SYNC_EVERY_MINUTES = 60
DAILY_REPORT_RUN_LIMIT = 500


def sync_resources() -> tuple[str, ...]:
    """Reports each sync runs: APPFOLIO_RESOURCES, then appfolio.sync_reports,
    then whatever the enabled modules read (all of them if none is enabled)."""
    configured = os.environ.get("APPFOLIO_RESOURCES", "")
    explicit = [item.strip() for item in configured.split(",") if item.strip()]
    if not explicit:
        listed = get("appfolio.sync_reports") or []
        if isinstance(listed, str):
            listed = listed.split(",")
        explicit = [str(item).strip() for item in listed if str(item).strip()]
    if explicit:
        return tuple(dict.fromkeys(explicit))
    needed = {
        resource
        for name, resources in MODULE_SYNC_RESOURCES.items()
        if module_enabled(name)
        for resource in resources
    }
    if not needed:
        return ALL_SYNC_RESOURCES
    return tuple(r for r in ALL_SYNC_RESOURCES if r in needed)


def sync_every_minutes() -> int:
    try:
        minutes = int(get("schedule.sync_every_minutes") or DEFAULT_SYNC_EVERY_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_SYNC_EVERY_MINUTES
    return max(5, minutes)


def daily_report_runs(
    resources: tuple[str, ...] | None = None, every_minutes: int | None = None
) -> int:
    resources = sync_resources() if resources is None else resources
    every = sync_every_minutes() if every_minutes is None else every_minutes
    return len(resources) * (24 * 60 // max(1, every))


def appfolio_url(path: str = "") -> str:
    if not path:
        return APPFOLIO_BASE_URL
    return APPFOLIO_BASE_URL + "/" + path.lstrip("/")
