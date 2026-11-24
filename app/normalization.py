from __future__ import annotations

import math
from typing import Any, Iterable


def report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = (
        (((report.get("data") or {}).get("results") or {}).get("rows"))
        or []
    )
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("AppFolio report rows must be an array of objects")
    return rows


def json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        seen: dict[str, str] = {}
        for key, item in value.items():
            folded = key.casefold()
            previous = seen.get(folded)
            if previous is not None:
                result.pop(previous, None)
            result[key] = json_safe(item)
            seen[folded] = key
        return result
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def _stable_id(resource: str, row: dict[str, Any]) -> object:
    if resource == "properties":
        return row.get("PropertyId") or row.get("id")
    if resource == "tenants":
        return row.get("SelectedTenantId") or row.get("id")
    if resource == "leases":
        return row.get("LeaseUuid") or row.get("occupancy_id") or row.get("id")
    if resource in {"units", "rent-roll", "vacancies"}:
        return row.get("unit_id") or row.get("id")
    if resource == "vendors":
        return row.get("vendor_id") or row.get("id")
    if resource == "rental-applications":
        return row.get("rental_application_id") or row.get("id")
    if resource == "financials":
        return row.get("id") or row.get("TreeNodeId")
    return row.get("id")


def normalize_rows(
    resource: str,
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (
            resource != "financials"
            and row.get("rowType") not in (None, "", "row")
        ):
            continue
        stable_id = _stable_id(resource, row)
        if stable_id in (None, ""):
            raise ValueError(f"{resource}: AppFolio row is missing a stable ID")
        item = json_safe(dict(row))
        item["appfolioId"] = str(stable_id)
        if resource in {"tenants", "leases"}:
            item["moveInOn"] = row.get("MoveIn_raw") or row.get("MoveIn")
            item["moveOutOn"] = row.get("MoveOut_raw") or row.get("MoveOut")
            item["occupancyId"] = (
                str(row.get("occupancy_id"))
                if row.get("occupancy_id") is not None
                else None
            )
            item["propertyId"] = (
                str(row.get("PropertyId"))
                if row.get("PropertyId") is not None
                else None
            )
            item["unitId"] = (
                str(row.get("unit_id"))
                if row.get("unit_id") is not None
                else None
            )
        item = json_safe(item)
        previous = by_id.get(item["appfolioId"])
        if previous is None:
            by_id[item["appfolioId"]] = item
        elif previous != item:
            raise ValueError(
                f"{resource}: conflicting rows for AppFolio ID "
                f"{item['appfolioId']}"
            )
    return [by_id[key] for key in sorted(by_id)]


def derive_leases(tenants: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for tenant in tenants:
        lease_id = (
            tenant.get("LeaseUuid")
            or tenant.get("occupancyId")
            or tenant.get("occupancy_id")
        )
        if lease_id in (None, ""):
            continue
        grouped.setdefault(str(lease_id), []).append(tenant)

    leases: list[dict[str, Any]] = []
    status_priority = {
        "current": 0,
        "notice": 1,
        "future": 2,
        "evict": 3,
        "past": 4,
    }

    def first_value(rows: list[dict[str, Any]], *fields: str) -> Any:
        for row in rows:
            for field in fields:
                if row.get(field) not in (None, ""):
                    return row[field]
        return None

    for lease_id, rows in grouped.items():
        statuses = sorted({
            str(row.get("Status"))
            for row in rows
            if row.get("Status") not in (None, "")
        })
        status = min(
            statuses,
            key=lambda value: status_priority.get(value.casefold(), 99),
            default=None,
        )
        leases.append({
            "appfolioId": lease_id,
            "leaseUuid": first_value(rows, "LeaseUuid"),
            "occupancyId": first_value(rows, "occupancyId", "occupancy_id"),
            "propertyId": first_value(rows, "propertyId", "PropertyId"),
            "unitId": first_value(rows, "unitId", "unit_id"),
            "leaseFromOn": first_value(rows, "LeaseFrom_raw", "LeaseFrom"),
            "leaseToOn": first_value(rows, "LeaseTo_raw", "LeaseTo"),
            "moveInOn": first_value(rows, "moveInOn", "MoveIn_raw", "MoveIn"),
            "moveOutOn": first_value(
                rows,
                "moveOutOn",
                "MoveOut_raw",
                "MoveOut",
            ),
            "status": status,
            "statuses": statuses,
            "rent": first_value(rows, "Rent_raw", "Rent"),
            "tenantIds": sorted({
                str(row["appfolioId"])
                for row in rows
                if row.get("appfolioId") not in (None, "")
            }),
        })
    return sorted(leases, key=lambda item: item["appfolioId"])
