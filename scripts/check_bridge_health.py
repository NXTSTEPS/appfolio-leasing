"""Report bridge conditions that otherwise fail silently.

Every problem on 2026-09-05/06 was individually invisible:

  * a hung ``sync_appfolio.py`` held the sync mutex for 35 hours while Task
    Scheduler reported success every 15 minutes;
  * the AppFolio session expired unnoticed, so the next approved bill failed;
  * RR-2026-000007 sat at ``approved`` with no bill and nothing driving it,
    because the poll worker takes work from the n8n queue rather than from
    local rows.

None of those raise on their own. This does, so a scheduled run turns each of
them into a visible non-zero exit instead of a quiet outage.

Read-only: it opens the bridge database and the encrypted session, and writes
nothing.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # the leasing-only edition ships without the bills code
    from app.reimbursement_poll_worker import (  # noqa: E402
        MAX_PREPOST_RETRY_AGE,
        MAX_PREPOST_RETRY_ATTEMPTS,
    )
    BILLS_AVAILABLE = True
except ImportError:
    MAX_PREPOST_RETRY_AGE = MAX_PREPOST_RETRY_ATTEMPTS = None
    BILLS_AVAILABLE = False
from app.session_vault import load_session  # noqa: E402
from app.store import store  # noqa: E402
from app import settings as _settings

try:
    from zoneinfo import ZoneInfo

    EASTERN = _settings.TIMEZONE
except Exception:  # pragma: no cover - tzdata missing on this host
    EASTERN = UTC

SESSION_PATH = ROOT / "work" / "appfolio-session.enc"
SKIP_STATE_PATH = ROOT / "work" / "bridge-sync-skips.txt"
ALERT_STATE_PATH = ROOT / "work" / "bridge-health-alert-state.json"
ALERT_URL_ENV = "BRIDGE_HEALTH_ALERT_WEBHOOK_URL"
ALERT_TOKEN_ENV = "BRIDGE_HEALTH_ALERT_TOKEN"
TENANT_HOST_FRAGMENT = _settings.APPFOLIO_BASE_URL.split("//", 1)[-1].split(".", 1)[0]
# The cookie that actually authenticates the tenant app. Its absence is what
# made every bill fail with "AppFolio redirected outside the configured
# account" -- the SSO cookies on account.appfolio.com were still valid.
TENANT_SESSION_COOKIE = "_property_session"
# Cookies AppFolio re-issues on demand. password_property_jwt lives only about
# four hours and is handed back by the remote_search 401 handshake, so its
# expiry is routine, not a fault -- alerting on it would fire several times a
# day and train the reader to ignore this check.
SELF_RENEWING_COOKIES = frozenset({"password_property_jwt", "_sp_ses", "_dd_s"})


def _utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def check_stranded_requests(
    now: datetime, max_age_minutes: int
) -> list[tuple[str, str]]:
    """Approved rows that no longer have anything driving them to AppFolio."""

    if not BILLS_AVAILABLE:
        return []
    problems: list[tuple[str, str]] = []
    cutoff = now - timedelta(minutes=max_age_minutes)
    rows = store.connection.execute(
        "SELECT request_id, status, approved_at, posting_started_at "
        "FROM reimbursement_bill_requests "
        "WHERE status = 'approved' AND appfolio_bill_id IS NULL"
    ).fetchall()
    for row in rows:
        approved_at = _utc(row["approved_at"])
        if approved_at is None or approved_at > cutoff:
            continue
        age = int((now - approved_at).total_seconds() // 60)
        message = (
            f"{row['request_id']} has been approved for {age} minutes with no "
            f"AppFolio bill (posting_started_at={row['posting_started_at']!r})"
        )
        retry = _latest_prepost_retry(row["request_id"])
        if retry is not None:
            message += "\n" + _describe_retry(retry)
        problems.append((f"stranded:{row['request_id']}", message))
    return problems


def _latest_prepost_retry(request_id: str):
    """The worker's newest pre-submission failure for one request, if any."""

    try:
        columns = {
            item[1]
            for item in store.connection.execute(
                "PRAGMA table_info(reimbursement_prepost_retries)"
            )
        }
    except Exception:
        return None
    if not columns:
        return None
    detail = "last_error_detail" if "last_error_detail" in columns else "NULL"
    return store.connection.execute(
        f"SELECT attempts, first_failed_at, last_error_type, {detail} AS detail "
        "FROM reimbursement_prepost_retries WHERE request_id = ? "
        "ORDER BY first_failed_at DESC LIMIT 1",
        (request_id,),
    ).fetchone()


def _describe_retry(retry) -> str:
    """Say why the worker is stuck and when it gives up, in plain words."""

    reason = " ".join(str(retry["detail"] or "").split()) or (
        f"{retry['last_error_type']} (no detail recorded; older worker run)"
    )
    lines = [f"Why: {reason}"]
    attempts = int(retry["attempts"])
    first_failed = _utc(retry["first_failed_at"])
    if first_failed is not None:
        give_up = (first_failed + MAX_PREPOST_RETRY_AGE).astimezone(EASTERN)
        lines.append(
            f"Tried {attempts} of {MAX_PREPOST_RETRY_ATTEMPTS} times. The bot "
            f"stops retrying at {give_up.hour % 12 or 12}:{give_up:%M %p} "
            f"on {give_up:%b} {give_up.day} "
            "(or sooner if it reaches the attempt limit); after that the row "
            "needs manual correction and re-approval."
        )
    return "\n".join(lines)


def check_session(
    now: datetime, warn_within_hours: int
) -> list[tuple[str, str]]:
    """The tenant session, not the SSO session, is what bills actually need."""

    if not SESSION_PATH.is_file():
        return [("session:file_missing", "The encrypted AppFolio session file is missing")]
    data_key = os.environ.get("BRIDGE_DATA_KEY", "")
    if len(data_key) < 24:
        return [("session:no_key", "BRIDGE_DATA_KEY is unavailable; session health was not checked")]
    try:
        state = load_session(SESSION_PATH, data_key)
    except Exception as exc:  # noqa: BLE001 - report, never crash the check
        return [("session:undecryptable", f"The AppFolio session could not be decrypted ({type(exc).__name__})")]

    problems: list[tuple[str, str]] = []
    cookies = [
        cookie
        for cookie in state.get("cookies", [])
        if TENANT_HOST_FRAGMENT in str(cookie.get("domain", ""))
    ]
    if not cookies:
        return [("session:no_tenant_cookies", "The AppFolio session has no tenant cookies at all")]
    if not any(
        str(cookie.get("name")) == TENANT_SESSION_COOKIE for cookie in cookies
    ):
        problems.append((
            "session:missing_tenant_cookie",
            f"The AppFolio session is missing {TENANT_SESSION_COOKIE}; "
            "bills will be redirected to the sign-in host",
        ))

    # Deliberately NOT warning on "expires soon". password_property_jwt lives
    # only about 4 hours, so any useful warning horizon would fire on every run
    # and train the reader to ignore this check. Age of the whole session is the
    # signal that actually tracks breakage: the 2026-09-05 session was written
    # at 07:46 and was failing bills by the next evening.
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        if any(name.startswith(prefix) for prefix in SELF_RENEWING_COOKIES):
            continue
        expires = cookie.get("expires")
        if not isinstance(expires, (int, float)) or expires <= 0:
            continue  # a browser-session cookie carries no expiry
        when = datetime.fromtimestamp(expires, UTC)
        if when <= now:
            problems.append((
                f"session:expired:{name}",
                f"AppFolio cookie {name} expired at "
                f"{when.isoformat()}; refresh the session",
            ))

    written = datetime.fromtimestamp(SESSION_PATH.stat().st_mtime, UTC)
    age_hours = (now - written).total_seconds() / 3600
    if age_hours > warn_within_hours:
        problems.append((
            "session:stale",
            f"The AppFolio session is {age_hours:.1f}h old "
            f"(threshold {warn_within_hours}h); refresh it with "
            "scripts/refresh_appfolio_session.ps1",
        ))
    return problems


def check_sync_freshness(
    now: datetime, max_age_hours: int
) -> list[tuple[str, str]]:
    """A stale cache means no sync completed, whatever the log says.

    Checks a report this install actually syncs (a leasing-only install never
    reads vendors, so its vendor cache would look stale forever)."""

    project = store.default_project()
    if not project:
        return [("sync:no_project", "No AppFolio project is configured")]
    synced = _settings.sync_resources()
    resource = "vendors" if "vendors" in synced else synced[0]
    if resource == "income-statement":
        resource = "financials"
    label = resource.replace("-", " ")
    _, updated_at = store.get_cache(project["id"], resource)
    cached = _utc(updated_at)
    if cached is None:
        return [("sync:never_written", f"The AppFolio {label} cache has never been written")]
    age_hours = (now - cached).total_seconds() / 3600
    if age_hours > max_age_hours:
        return [(
            "sync:stale",
            f"The AppFolio {label} cache is {age_hours:.1f}h old "
            f"(threshold {max_age_hours}h); the sync is probably not running",
        )]
    return []


def check_sync_lockouts(max_consecutive: int) -> list[tuple[str, str]]:
    if not SKIP_STATE_PATH.is_file():
        return []
    raw = SKIP_STATE_PATH.read_text(encoding="utf-8-sig").strip()
    if not raw.isdigit():
        return []
    skips = int(raw)
    if skips >= max_consecutive:
        return [(
            "sync:lockout",
            f"The sync has been locked out {skips} consecutive runs; a previous "
            "sync_appfolio.py is probably hung and holding the mutex",
        )]
    return []


def _alert_signature(problems: list[tuple[str, str]]) -> str:
    """Hash the stable KEYS, never the messages.

    Messages carry a live figure -- "the vendor cache is 6.6h old" -- which
    ticks every run. Hashing the message text meant the signature changed
    hourly, the repeat window never matched, and one ongoing outage produced an
    alert every hour on 2026-09-07. Keys identify the problem, not its age.
    """

    keys = sorted(key for key, _ in problems)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _read_alert_state() -> dict[str, str]:
    if not ALERT_STATE_PATH.is_file():
        return {}
    try:
        payload = json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def send_alert(
    problems: list[tuple[str, str]], now: datetime, *, repeat_after_hours: int
) -> str:
    """Post problems to the private n8n health webhook, rate limited.

    Re-sends only when the problem set CHANGES or the repeat window elapses, so
    an ongoing outage does not produce an hourly Slack message that gets muted
    -- which would recreate the exact blindness this is meant to remove.
    """

    from app import apps_script

    use_apps_script = apps_script.enabled()
    url = os.environ.get(ALERT_URL_ENV, "").strip()
    token = os.environ.get(ALERT_TOKEN_ENV, "").strip()
    if not use_apps_script:
        if not url or not token:
            return "not configured"
        if urlparse(url).scheme != "https":
            return "refused: webhook must be HTTPS"

    signature = _alert_signature(problems)
    state = _read_alert_state()
    last_sent = _utc(state.get("lastSentAt"))
    if state.get("lastSignature") == signature and last_sent is not None:
        if now - last_sent < timedelta(hours=repeat_after_hours):
            return "suppressed (unchanged, within repeat window)"

    if use_apps_script:
        try:
            apps_script.send(
                "alert",
                {
                    "component": "Bridge health check",
                    "generatedAt": now.isoformat(),
                    "host": platform.node(),
                    "summary": f"{len(problems)} problem(s) need attention on the bridge PC",
                    "detail": chr(10).join(message for _, message in problems),
                },
                delivery_id=f"health:{signature[:16]}:{now:%Y%m%d%H}",
            )
        except apps_script.AppsScriptError as exc:
            return f"delivery failed: {exc}"
        ALERT_STATE_PATH.write_text(
            json.dumps({"lastSignature": signature, "lastSentAt": now.isoformat()}),
            encoding="utf-8",
        )
        return "sent"

    body = json.dumps(
        {
            "checkedAt": now.isoformat(),
            "host": platform.node(),
            "problems": [message for _, message in problems],
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "AppFolio-BridgeHealth/1.0",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            response.read(2048)
            status = response.status
    except (HTTPError, URLError) as exc:
        return f"delivery failed: {type(exc).__name__}"
    if not 200 <= status < 300:
        return f"delivery failed: HTTP {status}"

    ALERT_STATE_PATH.write_text(
        json.dumps({"lastSignature": signature, "lastSentAt": now.isoformat()}),
        encoding="utf-8",
    )
    return "sent"


def heartbeat(now: datetime) -> None:
    """Tell the Google Sheet this PC is alive, so its watchdog stays quiet.

    Without it a PC that only runs weekday jobs is silent all weekend and the
    sheet would report it offline. Never fails the health check.
    """

    from app import apps_script

    if not apps_script.enabled():
        return
    try:
        apps_script.call("ping", {"host": platform.node(), "heartbeat": now.isoformat()}, attempts=1)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        print(f"heartbeat to the Google Sheet failed: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stranded-after-minutes", type=int, default=60)
    parser.add_argument("--session-warn-hours", type=int, default=20)
    parser.add_argument("--sync-stale-hours", type=int, default=6)
    parser.add_argument("--max-sync-skips", type=int, default=4)
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable output"
    )
    parser.add_argument(
        "--alert",
        action="store_true",
        help="Post any problems to the private n8n health webhook",
    )
    parser.add_argument(
        "--alert-repeat-hours",
        type=int,
        default=6,
        help="Re-send an unchanged problem set only this often",
    )
    args = parser.parse_args()

    now = datetime.now(UTC)
    checks = {
        "strandedRequests": check_stranded_requests(
            now, args.stranded_after_minutes
        ),
        "session": check_session(now, args.session_warn_hours),
        "syncFreshness": check_sync_freshness(now, args.sync_stale_hours),
        "syncLockouts": check_sync_lockouts(args.max_sync_skips),
    }
    problems = [item for group in checks.values() for item in group]
    messages = {name: [message for _, message in group] for name, group in checks.items()}

    alert_outcome = "not requested"
    if args.alert:
        heartbeat(now)
    if args.alert and problems:
        alert_outcome = send_alert(
            problems, now, repeat_after_hours=args.alert_repeat_hours
        )
    elif args.alert:
        # Healthy: clear the state so the next real problem alerts immediately
        # instead of being suppressed as "unchanged".
        ALERT_STATE_PATH.unlink(missing_ok=True)
        alert_outcome = "healthy, nothing to send"

    if args.json:
        print(
            json.dumps(
                {
                    "checkedAt": now.isoformat(),
                    "healthy": not problems,
                    "checks": messages,
                    "alert": alert_outcome,
                },
                indent=2,
            )
        )
    else:
        print(f"Bridge health at {now.isoformat()}")
        for name, items in messages.items():
            if items:
                for item in items:
                    print(f"  PROBLEM  [{name}] {item}")
            else:
                print(f"  ok       [{name}]")
        print()
        print("HEALTHY" if not problems else f"{len(problems)} PROBLEM(S) FOUND")
        print(f"alert: {alert_outcome}")

    # Non-zero so a scheduled run surfaces as a failure rather than silence.
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
