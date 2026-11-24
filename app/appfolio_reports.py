from __future__ import annotations
from app import settings as _settings

import json
import time
from typing import Any

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def _is_login_page(page: Page) -> bool:
    """True when AppFolio has redirected to its identity provider."""
    if "account.appfolio.com" in page.url:
        return True
    try:
        return bool(page.evaluate("() => !!document.querySelector('input[type=password]')"))
    except Exception:
        return False


REPORT_ROUTES = {
    "property-groups": "/buffered_reports/property_group_directory?customize=true",
    "properties": "/buffered_reports/property_directory?customize=true",
    "tenants": "/buffered_reports/tenant_directory?customize=true",
    "leases": "/buffered_reports/lease_expiration_detail?customize=true",
    "rent-roll": "/buffered_reports/rent_roll?customize=true",
    "income-statement": "/buffered_reports/income_statement?customize=true",
    "vendors": "/buffered_reports/vendor_directory?customize=true",
    "rental-applications": "/buffered_reports/rental_applications?customize=true",
    "vacancies": "/buffered_reports/unit_vacancy?customize=true",
}


class BufferedReportAdapter:
    """Read an AppFolio buffered report through its authenticated JSON job API."""

    def __init__(self, context: BrowserContext, base_url: str = _settings.APPFOLIO_BASE_URL) -> None:
        self.context = context
        self.base_url = base_url.rstrip("/")

    def fetch(self, resource: str, scope_id: str, timeout_seconds: int = 120) -> dict[str, Any]:
        route = REPORT_ROUTES[resource]
        page = self.context.new_page()
        metadata: dict[str, Any] = {"resource": resource, "scopeId": scope_id}
        try:
            page.goto(self.base_url + route, wait_until="commit", timeout=30_000)
            requires_scope = resource != "vendors"
            try:
                page.wait_for_function(
                    """requiresScope => {
                      const hidden = document.querySelector(
                        'input[type="hidden"][name="filters[properties_ids]"]'
                      );
                      const button = [...document.querySelectorAll('[role=dialog] button')]
                        .find(e => (e.innerText || '').trim() === 'Update');
                      return !!button && (!requiresScope || !!hidden);
                    }""",
                    arg=requires_scope,
                    timeout=30_000,
                )
            except PlaywrightTimeoutError:
                # An expired session is bounced to the identity provider, where the
                # report controls never appear. Say so, instead of reporting a bare
                # timeout that looks identical to a page-structure change.
                if _is_login_page(page):
                    raise RuntimeError(
                        "AppFolio signed the bridge out. Refresh the saved session with "
                        "scripts\\refresh_appfolio_session.ps1, then rerun."
                    ) from None
                raise
            report_scope = "" if scope_id == "all" else scope_id
            prepared = page.evaluate("""({scope, resource, requiresScope}) => {
              const hidden = document.querySelector(
                'input[type="hidden"][name="filters[properties_ids]"]'
              );
              const button = [...document.querySelectorAll('[role=dialog] button')]
                .find(e => (e.innerText || '').trim() === 'Update');
              if (!button || (requiresScope && !hidden)) return false;
              if (hidden) hidden.value = scope;
              const visibility = document.querySelector('select[name="filters[property_visibility]"]');
              if (visibility) visibility.value = 'active';
              if (resource === 'tenants') {
                const tenantVisibility = document.querySelector(
                  'select[name="filters[tenant_visibility]"]'
                );
                if (tenantVisibility) tenantVisibility.value = 'all';
                document.querySelectorAll(
                  'input[type="checkbox"][name="filters[tenant_statuses][]"]'
                ).forEach(input => {
                  input.checked = input.value !== 'all';
                });
                document.querySelectorAll(
                  'input[type="checkbox"][name="filters[tenant_types][]"]'
                ).forEach(input => {
                  input.checked = input.value === 'all';
                });
              }
              return true;
            }""", {
                "scope": report_scope,
                "resource": resource,
                "requiresScope": requires_scope,
            })
            if not prepared:
                raise RuntimeError(f"{resource}: AppFolio report controls did not render")

            captured: dict[str, Any] = {}
            captured_requests: dict[str, Any] = {}
            rewritten_filters: dict[str, Any] = {}
            rewrite_errors: list[str] = []

            def capture(response) -> None:
                if (
                    "/reporting/" not in response.url
                    or not response.ok
                    or not (
                        response.url.endswith("/ag_results")
                        or response.url.endswith("/summary")
                    )
                ):
                    return
                try:
                    captured[response.url] = response.json()
                    captured_requests[response.url] = response.request.post_data_json
                except Exception as exc:
                    metadata.setdefault("captureErrors", []).append(str(exc))

            page.on("response", capture)
            submit_url = self.base_url + route.split("?", 1)[0]

            def rewrite_submission(route) -> None:
                try:
                    payload = route.request.post_data_json
                    if not isinstance(payload, dict):
                        raise RuntimeError("report submission was not a JSON object")
                    filters = payload.setdefault("filters", {})
                    if not isinstance(filters, dict):
                        raise RuntimeError("report filters were not a JSON object")
                    if requires_scope:
                        filters["properties_ids"] = [] if scope_id == "all" else [scope_id]
                    if "property_visibility" in filters:
                        filters["property_visibility"] = "active"
                    if resource == "tenants":
                        filters["tenant_visibility"] = "all"
                        filters["tenant_statuses"] = ["2", "0", "4", "1", "3"]
                        filters["tenant_types"] = ["all"]
                    if resource == "rental-applications":
                        filters["rental_application_statuses"] = ["all"]
                        filters["received_on_from"] = "01/01/2000"
                        filters["received_on_to"] = time.strftime("%m/%d/%Y")
                        filters["received_on_range"] = "last_30"
                        filters["float_received_on"] = "0"
                    if resource in {
                        "rent-roll",
                        "vacancies",
                        "rental-applications",
                    }:
                        columns = filters.get("columns")
                        if isinstance(columns, dict):
                            columns["group"] = [{"by": ""}]
                    rewritten_filters.update({
                        key: filters.get(key)
                        for key in (
                            "properties_ids",
                            "property_visibility",
                            "tenant_visibility",
                            "tenant_statuses",
                            "tenant_types",
                        )
                        if key in filters
                    })
                    headers = dict(route.request.headers)
                    headers["content-type"] = "application/json"
                    route.continue_(headers=headers, post_data=json.dumps(payload))
                except Exception as exc:
                    rewrite_errors.append(str(exc))
                    route.continue_()

            page.route(submit_url, rewrite_submission)
            with page.expect_response(
                lambda response: (
                    response.request.method == "PUT"
                    and response.url.split("?", 1)[0] == submit_url
                ),
                timeout=30_000,
            ) as pending:
                clicked = page.evaluate("""() => {
                  const button = [...document.querySelectorAll('[role=dialog] button')]
                    .find(e => (e.innerText || '').trim() === 'Update');
                  if (!button) return false;
                  button.click();
                    return true;
                }""")
            page.unroute(submit_url, rewrite_submission)
            if not clicked:
                raise RuntimeError(f"{resource}: AppFolio Update button was unavailable")
            if rewrite_errors:
                raise RuntimeError(
                    f"{resource}: could not apply report filters: {rewrite_errors[0]}"
                )
            if requires_scope and rewritten_filters.get("properties_ids") != (
                [] if scope_id == "all" else [scope_id]
            ):
                raise RuntimeError(f"{resource}: requested scope was not submitted")
            metadata["submittedFilters"] = rewritten_filters

            submission = pending.value
            submission_payload = submission.json()
            if submission_payload.get("status") != "success":
                detail = (
                    submission_payload.get("error")
                    or submission_payload.get("errors")
                    or submission_payload.get("message")
                    or submission_payload.get("status")
                )
                if "run_limit" in str(detail):
                    raise RuntimeError(
                        f"{resource}: AppFolio's daily report limit is used up "
                        "(AppFolio allows 500 report runs a day; it resets at midnight "
                        "Eastern). Every PC syncing with this AppFolio login counts toward it: "
                        "sync less often or fewer reports (config/company.json > "
                        "schedule.sync_every_minutes and appfolio.sync_reports), then "
                        "try again after midnight. (run_limit_reached)"
                    )
                raise RuntimeError(
                    f"{resource}: AppFolio rejected the report submission: "
                    f"{str(detail)[:300]}"
                )
            urls = submission_payload.get("urls") or {}
            results_path = urls.get("ag_results_url")
            summary_path = urls.get("summary_url")
            if not isinstance(results_path, str):
                raise RuntimeError(f"{resource}: AppFolio did not return a results URL")
            results_url = self.base_url + results_path
            summary_url = self.base_url + summary_path if isinstance(summary_path, str) else None
            metadata["resultsUrlPath"] = results_path
            if summary_path:
                metadata["summaryUrlPath"] = summary_path
            job = submission_payload.get("job") or {}
            if isinstance(job.get("id"), str):
                metadata["jobId"] = job["id"]

            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline and results_url not in captured:
                page.wait_for_timeout(250)
            if results_url not in captured:
                raise TimeoutError(f"{resource}: AppFolio JSON report job did not complete within {timeout_seconds}s")

            result_payload = captured[results_url]
            result = result_payload.get("results") if isinstance(result_payload, dict) else None
            rows = result.get("rows") if isinstance(result, dict) else None
            total_rows = result.get("total_rows") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise RuntimeError(f"{resource}: AppFolio returned an invalid row payload")
            if isinstance(total_rows, int) and len(rows) < total_rows:
                grid_request = captured_requests.get(results_url)
                if (
                    not isinstance(grid_request, dict)
                    or not isinstance(grid_request.get("startRow"), int)
                    or not isinstance(grid_request.get("endRow"), int)
                ):
                    raise RuntimeError(
                        f"{resource}: AppFolio returned a partial result without "
                        "a usable paging contract"
                    )
                page_size = max(
                    1,
                    grid_request["endRow"] - grid_request["startRow"],
                )
                combined_rows = list(rows)
                while len(combined_rows) < total_rows:
                    start = len(combined_rows)
                    request_body = dict(grid_request)
                    request_body["startRow"] = start
                    request_body["endRow"] = min(start + page_size, total_rows)
                    page_result = page.evaluate(
                        """async ({url, body}) => {
                          const response = await fetch(url, {
                            method: 'POST',
                            credentials: 'same-origin',
                            headers: {
                              'Accept': 'application/json',
                              'Content-Type': 'application/json'
                            },
                            body: JSON.stringify(body)
                          });
                          return {
                            status: response.status,
                            text: await response.text()
                          };
                        }""",
                        {"url": results_url, "body": request_body},
                    )
                    if page_result["status"] < 200 or page_result["status"] >= 300:
                        raise RuntimeError(
                            f"{resource}: AppFolio paging request failed "
                            f"with HTTP {page_result['status']}"
                        )
                    page_payload = json.loads(page_result["text"])
                    page_results = (
                        page_payload.get("results")
                        if isinstance(page_payload, dict)
                        else None
                    )
                    page_rows = (
                        page_results.get("rows")
                        if isinstance(page_results, dict)
                        else None
                    )
                    if not isinstance(page_rows, list) or not page_rows:
                        raise RuntimeError(
                            f"{resource}: AppFolio paging stopped at "
                            f"{len(combined_rows)}/{total_rows} rows"
                        )
                    combined_rows.extend(page_rows)
                rows = combined_rows
                result["rows"] = rows
                metadata["pageCount"] = (
                    len(rows) + page_size - 1
                ) // page_size
            if isinstance(total_rows, int) and len(rows) != total_rows:
                raise RuntimeError(
                    f"{resource}: incomplete AppFolio result "
                    f"({len(rows)} rows received; {total_rows} expected)"
                )
            metadata["rowCount"] = len(rows)
            metadata["totalRows"] = total_rows
            metadata["pageUrl"] = page.url
            return {
                "meta": metadata,
                "data": result_payload,
                "summary": captured.get(summary_url) if summary_url else None,
            }
        finally:
            page.close()
