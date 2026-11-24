"""Send HTML email through SMTP or an n8n webhook, chosen in company.json.

Secrets never live in company.json:
  * SMTP password  -> env SMTP_PASSWORD (scripts/set_secret.ps1 -Name SMTP_PASSWORD)
  * n8n webhook    -> env N8N_EMAIL_WEBHOOK_URL (scripts/set_secret.ps1 -Name N8N_EMAIL_WEBHOOK_URL)

``test_mode=True`` (or env BRIDGE_EMAIL_TEST_MODE=1) rewrites every recipient
to ``email.test_recipient`` so a new installation can be checked safely.
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.request import Request, urlopen

from app import settings as _settings


class EmailNotConfigured(RuntimeError):
    pass


@dataclass
class Email:
    to: list[str]
    subject: str
    html: str
    cc: list[str] = field(default_factory=list)
    text: str = ""
    reply_to: str = ""


def _clean(addresses: list[str]) -> list[str]:
    seen: list[str] = []
    for item in addresses:
        for part in str(item or "").replace(";", ",").split(","):
            part = part.strip()
            if part and "@" in part and part.lower() not in {s.lower() for s in seen}:
                seen.append(part)
    return seen


def _apply_test_mode(message: Email, test_mode: bool) -> Email:
    if not (test_mode or os.getenv("BRIDGE_EMAIL_TEST_MODE") == "1"):
        return message
    target = _settings.get("email.test_recipient", "")
    if not target:
        raise EmailNotConfigured("Test mode needs email.test_recipient in config/company.json")
    original = ", ".join(message.to + message.cc)
    banner = (
        '<div style="background:#fff3cd;border:1px solid #e0c060;padding:8px 12px;'
        'font:13px Arial,sans-serif;margin-bottom:12px">TEST MODE - this would have gone to: '
        f"{original}</div>"
    )
    return Email(to=[target], cc=[], subject="[TEST] " + message.subject,
                 html=banner + message.html, text=message.text, reply_to=message.reply_to)


def send(message: Email, *, test_mode: bool = False) -> str:
    """Send one email and return a provider message id (best effort)."""
    message = _apply_test_mode(message, test_mode)
    message.to = _clean(message.to)
    message.cc = [c for c in _clean(message.cc) if c.lower() not in {t.lower() for t in message.to}]
    if not message.to:
        raise ValueError("Email has no valid recipient")
    method = (_settings.get("email.method", "smtp") or "smtp").lower()
    if method == "smtp":
        return _send_smtp(message)
    if method == "n8n":
        return _send_n8n(message)
    raise EmailNotConfigured(f"Unknown email.method {method!r}; use 'smtp' or 'n8n'")


def _send_smtp(message: Email) -> str:
    host = _settings.get("email.smtp_host", "")
    port = int(_settings.get("email.smtp_port", 587) or 587)
    username = _settings.get("email.smtp_username", "")
    password = os.getenv("SMTP_PASSWORD", "")
    from_address = _settings.get("email.from_address", "") or username
    if not (host and username and password and from_address):
        raise EmailNotConfigured(
            "SMTP needs email.smtp_host / smtp_username / from_address in company.json "
            "and the password saved with: scripts\\set_secret.ps1 -Name SMTP_PASSWORD"
        )
    msg = EmailMessage()
    msg["From"] = formataddr((_settings.get("email.from_name", ""), from_address))
    msg["To"] = ", ".join(message.to)
    if message.cc:
        msg["Cc"] = ", ".join(message.cc)
    reply_to = message.reply_to or _settings.get("email.reply_to", "")
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = message.subject
    message_id = make_msgid(domain=from_address.split("@", 1)[-1])
    msg["Message-ID"] = message_id
    msg.set_content(message.text or "This message is best viewed in an HTML email client.")
    msg.add_alternative(message.html, subtype="html")
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as smtp:
            smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as smtp:
            smtp.starttls(context=context)
            smtp.login(username, password)
            smtp.send_message(msg)
    return message_id


def _send_n8n(message: Email) -> str:
    url = os.getenv("N8N_EMAIL_WEBHOOK_URL", "")
    if not url.startswith("https://"):
        raise EmailNotConfigured(
            "n8n delivery needs an https webhook saved with: "
            "scripts\\set_secret.ps1 -Name N8N_EMAIL_WEBHOOK_URL"
        )
    body = json.dumps({
        "to": ", ".join(message.to),
        "cc": ", ".join(message.cc),
        "replyTo": message.reply_to or _settings.get("email.reply_to", ""),
        "fromName": _settings.get("email.from_name", ""),
        "subject": message.subject,
        "html": message.html,
        "text": message.text,
    }).encode()
    request = Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "AppFolio-Bridge/1.0"})
    token = os.getenv("N8N_EMAIL_WEBHOOK_TOKEN", "")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urlopen(request, timeout=60) as response:  # noqa: S310 - https enforced above
        if response.status >= 300:
            raise RuntimeError(f"n8n email webhook returned HTTP {response.status}")
        return response.headers.get("x-request-id", "") or "n8n-accepted"
