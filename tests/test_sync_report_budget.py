"""AppFolio allows 500 report runs a day per login, shared by every PC on it.

On 2026-11-24 an install syncing all 7 reports every 15 minutes (672 a day) used
up the limit by mid-afternoon, and a second leasing-only install on the same
login could not even finish setup. A sync now reads only what the enabled
modules use, and the default interval is an hour.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import settings  # noqa: E402


def _config(modules=None, sync_reports=None, every=None):
    cfg = {"modules": {name: {"enabled": True} for name in (modules or [])}, "appfolio": {}, "schedule": {}}
    if sync_reports is not None:
        cfg["appfolio"]["sync_reports"] = sync_reports
    if every is not None:
        cfg["schedule"]["sync_every_minutes"] = every
    return mock.patch.object(settings, "config", lambda: cfg)


class SyncResourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        os.environ.pop("APPFOLIO_RESOURCES", None)
        self.addCleanup(patcher.stop)

    def test_leasing_only_reads_three_reports(self) -> None:
        with _config(["leasing"]):
            self.assertEqual(settings.sync_resources(), ("tenants", "vacancies", "rental-applications"))
            self.assertEqual(settings.daily_report_runs(), 3 * 24)

    def test_modules_combine_without_duplicates_in_a_stable_order(self) -> None:
        with _config(["leasing", "delinquency", "bills"]):
            self.assertEqual(
                settings.sync_resources(),
                ("properties", "tenants", "rent-roll", "vacancies", "vendors", "rental-applications"),
            )

    def test_no_module_reads_everything(self) -> None:
        with _config([]):
            self.assertEqual(settings.sync_resources(), settings.ALL_SYNC_RESOURCES)

    def test_config_list_wins_over_modules(self) -> None:
        with _config(["leasing"], sync_reports=["tenants", "vendors"]):
            self.assertEqual(settings.sync_resources(), ("tenants", "vendors"))

    def test_environment_wins_over_config(self) -> None:
        os.environ["APPFOLIO_RESOURCES"] = "vacancies, tenants"
        with _config(["bills"], sync_reports=["vendors"]):
            self.assertEqual(settings.sync_resources(), ("vacancies", "tenants"))

    def test_default_interval_is_an_hour(self) -> None:
        with _config([]):
            self.assertEqual(settings.sync_every_minutes(), 60)
            self.assertEqual(settings.daily_report_runs(), 7 * 24)
        with _config([], every=15):
            self.assertEqual(settings.daily_report_runs(), 672, "the old default blew the 500 limit")


class HealthWatchesASyncedReportTest(unittest.TestCase):
    def test_leasing_only_install_is_not_reported_stale_for_vendors(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("check_bridge_health_budget", ROOT / "scripts" / "check_bridge_health.py")
        health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(health)
        now = datetime(2026, 11, 25, 12, tzinfo=UTC)
        asked = []

        def get_cache(_project, resource):
            asked.append(resource)
            return [], (now - timedelta(hours=1)).isoformat()

        os.environ.pop("APPFOLIO_RESOURCES", None)
        with _config(["leasing"]), \
                mock.patch.object(health.store, "default_project", lambda: {"id": "p"}), \
                mock.patch.object(health.store, "get_cache", get_cache):
            self.assertEqual(health.check_sync_freshness(now, 6), [])
        self.assertEqual(asked, ["tenants"])


if __name__ == "__main__":
    unittest.main()
