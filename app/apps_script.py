"""Send bridge data to the customer's Google Apps Script web app.

The Apps Script (``apps-script/Code.gs``) lives in a Google Sheet the customer
owns. It does everything n8n used to do: builds and sends the emails, keeps the
lease tracker and reminder log, and holds the bill-approval queue. The bridge
keeps doing only what needs AppFolio: reading data, downloading ledger PDFs and
writing approved bills.

Secrets (saved with ``scripts/connect_apps_script.ps1``, Windows DPAPI):

* ``APPS_SCRIPT_URL``   - the web app's ``https://script.google.com/macros/s/.../exec`` URL
* ``APPS_SCRIPT_TOKEN`` - shared secret the script checks on every call

Every call is a JSON POST ``{token, action, deliveryId, payload}``. Google
answers a web-app POST with a redirect to ``script.googleusercontent.com`` that
must be fetched with GET; only that host is followed. The script always answers
``{"ok": true|false, ...}`` and de-duplicates on ``deliveryId``, so a retry
after a lost response never sends the same email twice.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
import uuid
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from app import settings as _settings

URL_ENV = "APPS_SCRIPT_URL"
TOKEN_ENV = "APPS_SCRIPT_TOKEN"
CONNECTION_PREFIX = "AFB1."
PROTOCOL = "appfolio-bridge/1"
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 330  # leasing.sync may spend up to 200s on Gemini wording; Apps Script stops at 360s
MAX_RESPONSE_BYTES = 5_000_000
# Personal accounts: /macros/s/ID/exec. Google Workspace: /a/macros/DOMAIN/s/ID/exec.
_SCRIPT_URL = re.compile(
    r"^https://script\.google\.com/(?:a/macros/[A-Za-z0-9.-]+|macros)/s/[A-Za-z0-9_-]{20,}/exec$"
)
_REDIRECT_HOSTS = {"script.googleusercontent.com"}


class AppsScriptError(RuntimeError):
    """The Apps Script call failed or answered ``ok: false``."""


def delivery_method() -> str:
    """``apps_script`` (default for new installs) or ``n8n`` (older installs)."""

    value = os.environ.get("BRIDGE_DELIVERY") or _settings.get("delivery.method") or ""
    value = str(value).strip().lower()
    if value in {"apps_script", "n8n"}:
        return value
    # Installs made before the delivery setting existed used n8n.
    return "apps_script" if _settings.get("delivery") is not None else "n8n"


def enabled() -> bool:
    return delivery_method() == "apps_script"


def webhook_or_apps_script(env_name: str) -> str:
    """Return the n8n webhook URL, or a marker when Apps Script handles delivery.

    Senders keep their existing ``post_payload(payload, url)`` shape; the marker
    tells them to call :func:`call` instead.
    """

    if enabled():
        return APPS_SCRIPT_MARKER
    return os.environ.get(env_name, "").strip()


APPS_SCRIPT_MARKER = "apps-script:"


def is_marker(url: str) -> bool:
    return str(url or "").startswith(APPS_SCRIPT_MARKER)


def _url_shape(value: str) -> str:
    """The URL with long IDs hidden, safe to print in an error."""

    return re.sub(r"[A-Za-z0-9_-]{20,}", "<ID>", value)[:200]


def _validated_url(value: str) -> str:
    value = str(value or "").strip()
    # A browser signed in to several Google accounts copies /u/<n>/ into the
    # URL; that part only picks the browser account, so drop it.
    value = re.sub(r"/u/\d+(?=/)", "", value)
    if not _SCRIPT_URL.match(value):
        raise AppsScriptError(
            f"{URL_ENV} must be the web app URL that ends in /exec "
            f"(Apps Script > Deploy > Manage deployments). Got: {_url_shape(value) or '(empty)'}"
        )
    return value


def _validated_token(value: str) -> str:
    value = str(value or "").strip()
    if len(value) < 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AppsScriptError(f"{TOKEN_ENV} is missing or invalid; run connect_apps_script.ps1")
    return value


class _GoogleRedirectOnly(HTTPRedirectHandler):
    """Follow the one redirect Google uses for web-app answers, nothing else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parts = urlsplit(newurl)
        if parts.scheme != "https" or parts.hostname not in _REDIRECT_HOSTS:
            raise AppsScriptError(f"Apps Script redirected to an unexpected host ({parts.hostname})")
        # urllib turns the POST into a GET without a body for 302/303, which is
        # exactly what Google expects for the answer URL.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def decode_connection_code(code: str) -> dict[str, str]:
    """Decode the one-line code shown by AppFolio Tools > Connect the PC bridge."""

    code = "".join(str(code or "").split())
    if not code.startswith(CONNECTION_PREFIX):
        raise AppsScriptError("That is not a connection code (it should start with AFB1.)")
    raw = code[len(CONNECTION_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise AppsScriptError("The connection code is damaged; copy it again") from exc
    if not isinstance(data, dict):
        raise AppsScriptError("The connection code is damaged; copy it again")
    result = {
        URL_ENV: _validated_url(data.get("url", "")),
        TOKEN_ENV: _validated_token(data.get("token", "")),
    }
    hmac_key = str(data.get("billKey") or "")
    if hmac_key:
        if len(hmac_key.encode("utf-8")) < 32:
            raise AppsScriptError("The bill-approval key in the code is too short")
        result["REIMBURSEMENT_INTAKE_HMAC_KEY"] = hmac_key
    return result


def call(
    action: str,
    payload: Mapping[str, Any] | None = None,
    *,
    delivery_id: str | None = None,
    url: str | None = None,
    token: str | None = None,
    opener: Any = None,
    attempts: int = MAX_ATTEMPTS,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """POST one action to the Apps Script web app and return its JSON answer."""

    if not re.fullmatch(r"[a-z][a-z0-9_.]{1,60}", action):
        raise AppsScriptError(f"Invalid Apps Script action {action!r}")
    endpoint = _validated_url(url if url is not None else os.environ.get(URL_ENV, ""))
    secret = _validated_token(token if token is not None else os.environ.get(TOKEN_ENV, ""))
    body = json.dumps(
        {
            "protocol": PROTOCOL,
            "token": secret,
            "action": action,
            "deliveryId": delivery_id or f"{action}:{uuid.uuid4()}",
            "payload": dict(payload or {}),
        },
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    client = opener or build_opener(ProxyHandler({}), _GoogleRedirectOnly())
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "AppFolio-Bridge/2 (+apps-script)",
            },
        )
        try:
            with client.open(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise AppsScriptError("Apps Script answer was too large")
            try:
                answer = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                snippet = raw[:200].decode("utf-8", errors="replace")
                if "<html" in snippet.lower():
                    raise AppsScriptError(
                        "Apps Script answered with a web page, not data. Redeploy the web app "
                        "with 'Who has access: Anyone' and use the /exec URL."
                    ) from exc
                raise AppsScriptError("Apps Script answer was not JSON") from exc
            if not isinstance(answer, dict):
                raise AppsScriptError("Apps Script answer was not a JSON object")
            if answer.get("ok") is not True:
                # A refusal (bad token, bad data) is final: retrying cannot help.
                raise AppsScriptError(str(answer.get("error") or "Apps Script refused the request")[:500])
            return answer
        except AppsScriptError:
            raise
        except HTTPError as exc:
            last_error = AppsScriptError(f"Apps Script returned HTTP {exc.code}")
            if exc.code not in {408, 425, 429} and exc.code < 500:
                break
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt < attempts:
            sleep(2 ** attempt)
    raise AppsScriptError(f"Apps Script could not be reached: {last_error}") from last_error


def send(action: str, payload: Mapping[str, Any], *, delivery_id: str | None = None) -> dict[str, Any]:
    """Deliver a notification payload; prints the script's one-line result."""

    answer = call(action, payload, delivery_id=delivery_id)
    note = answer.get("result") or ("already delivered" if answer.get("duplicate") else "accepted")
    print(f"Apps Script {action}: {note}")
    return answer
