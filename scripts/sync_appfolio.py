"""Run a staged, all-or-nothing AppFolio cache sync."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.appfolio_reports import BufferedReportAdapter, REPORT_ROUTES
from app.normalization import derive_leases, normalize_rows, report_rows
from app.session_vault import load_session, save_session
from app.store import store
from app import settings as _settings

SESSION_PATH = Path("work/appfolio-session.enc")
RUNS_PATH = Path("work/appfolio-runs")
GROUPS_PATH = Path("work/appfolio-property-groups.json")
DEFAULT_RESOURCES = _settings.ALL_SYNC_RESOURCES


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2))


def load_property_groups() -> list[dict]:
    if not GROUPS_PATH.exists():
        # Property groups are optional; run scripts/resolve_property_groups.py
        # to enable group-scoped features (move-out filters, group reports).
        return []
    payload = json.loads(GROUPS_PATH.read_text())
    groups = payload.get("groups") or []
    expected = os.environ.get("APPFOLIO_EXPECTED_GROUP_COUNT")
    if payload.get("count") != len(groups) or (expected and len(groups) != int(expected)):
        raise ValueError(
            f"Property-group file is inconsistent: count={payload.get('count')}, groups={len(groups)}"
        )
    scopes: set[str] = set()
    normalized_names: set[str] = set()
    result: list[dict] = []
    for group in groups:
        scope_id = str(group.get("scopeId") or "")
        name = " ".join(str(group.get("name") or "").split())
        if not re.fullmatch(r"l_\d+", scope_id):
            raise ValueError(f"Invalid property-group scope: {scope_id!r}")
        if scope_id in scopes or name.casefold() in normalized_names:
            raise ValueError("Duplicate AppFolio property-group mapping")
        scopes.add(scope_id)
        normalized_names.add(name.casefold())
        result.append({
            "appfolioId": scope_id.removeprefix("l_"),
            "scopeId": scope_id,
            "name": name,
            "resultType": "property_list",
        })
    return sorted(result, key=lambda item: item["name"].casefold())


def main() -> None:
    data_key = os.environ.get("BRIDGE_DATA_KEY", "")
    scope_id = os.environ.get("APPFOLIO_SCOPE_ID", "all").strip()
    if scope_id != "all" and not re.fullmatch(r"l_\d+", scope_id):
        raise SystemExit("APPFOLIO_SCOPE_ID must be 'all' or an l_N group scope")
    resources = _settings.sync_resources()
    unsupported = [resource for resource in resources if resource not in REPORT_ROUTES]
    if unsupported:
        raise SystemExit(f"Unknown AppFolio resources: {', '.join(unsupported)}")

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    manifest_path = RUNS_PATH / f"{run_id}.json"
    manifest: dict = {
        "runId": run_id,
        "scopeId": scope_id,
        "resources": list(resources),
        "status": "collecting",
        "startedAt": datetime.now(UTC).isoformat(),
        "reports": {},
    }
    write_manifest(manifest_path, manifest)

    state = load_session(SESSION_PATH, data_key)
    property_groups = load_property_groups()
    staged: dict[str, list[dict]] = {"property-groups": property_groups}
    manifest["propertyGroupCount"] = len(property_groups)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(storage_state=state)
            try:
                adapter = BufferedReportAdapter(context)
                for resource in resources:
                    print(f"Collecting {resource} for {scope_id}…")
                    report = adapter.fetch(resource, scope_id)
                    output = (
                        Path("work/appfolio-data")
                        / scope_id
                        / resource
                        / f"{run_id}.enc"
                    )
                    save_session(output, report, data_key)
                    cache_resource = (
                        "financials"
                        if resource == "income-statement"
                        else resource
                    )
                    normalized = normalize_rows(
                        cache_resource,
                        report_rows(report),
                    )
                    staged[cache_resource] = normalized
                    derived: dict[str, int] = {}
                    if resource == "tenants":
                        leases = derive_leases(normalized)
                        staged["leases"] = leases
                        derived["leases"] = len(leases)
                    elif resource == "rent-roll":
                        units = [dict(row) for row in normalized]
                        staged["units"] = units
                        derived["units"] = len(units)
                    manifest["reports"][resource] = {
                        "status": "complete",
                        "rowCount": len(normalized),
                        "totalRows": report["meta"].get("totalRows"),
                        "pageCount": report["meta"].get("pageCount", 1),
                        "encryptedFile": str(output),
                        "cacheResource": cache_resource,
                        "derived": derived,
                    }
                    write_manifest(manifest_path, manifest)
                    print(f"Staged {len(normalized)} {resource} records")
                    for derived_resource, count in derived.items():
                        print(f"Derived {count} {derived_resource} records")
                save_session(SESSION_PATH, context.storage_state(), data_key)
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["finishedAt"] = datetime.now(UTC).isoformat()
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        write_manifest(manifest_path, manifest)
        raise

    service_email = os.environ.get(
        "APPFOLIO_SERVICE_EMAIL",
        _settings.SERVICE_EMAIL,
    )
    project = store.project_by_email(service_email)
    if project is None:
        manifest["status"] = "failed"
        manifest["error"] = "Bridge project not found"
        write_manifest(manifest_path, manifest)
        raise SystemExit("Bridge project not found")

    establish_baseline = os.environ.get("BRIDGE_ESTABLISH_BASELINE") == "1"
    updated = store.put_caches(
        project["id"],
        staged,
        record_events=not establish_baseline,
    )
    manifest["status"] = "published"
    manifest["mode"] = "baseline" if establish_baseline else "poll"
    manifest["publishedAt"] = next(iter(updated.values()))
    manifest["finishedAt"] = datetime.now(UTC).isoformat()
    write_manifest(manifest_path, manifest)
    counts = ", ".join(
        f"{resource}={len(rows)}"
        for resource, rows in staged.items()
    )
    print(f"Published AppFolio sync atomically: {counts}")
    print(f"Run manifest: {manifest_path}")


if __name__ == "__main__":
    main()
