"""Leasing summary and unsigned-lease reminders.

Source of truth for signature status is AppFolio's own Leasing > Leases page
(``/lease_documents``), which lists every e-sign lease as *Out for Signing*,
*Ready to Countersign* or *Printed*. A lease that disappears from *Out for
Signing* has been signed (or cancelled) and its reminders stop.

Everything here except ``LeaseDocumentReader`` is pure logic so it can be unit
tested without AppFolio.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from app import settings as _settings

OUT_FOR_SIGNING = "Out for Signing"
READY_TO_COUNTERSIGN = "Ready to Countersign"
ACTIVE_TENANT_STATUSES = {"current", "future", "notice"}

_ROW = re.compile(r"<tr[^>]*data-href=\"/lease_documents/(\d+)\"[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_SENT = re.compile(r"Sent on\s+(\d{2}/\d{2}/\d{4})", re.I)
_RESENT = re.compile(r"Last resent on\s+(\d{2}/\d{2}/\d{4})", re.I)
_STATUS = re.compile(r"Lease Status:\s*([A-Za-z][A-Za-z ]*?)\s*(?:\n|<|Sent|$)", re.I)
_SIGNATURE_BLOCK = re.compile(r'class="js-lease-signatures', re.I)
_SIGNATURE_BOX = re.compile(r'<div class="signature-box">(.*?)</div>\s*</div>', re.S | re.I)
_LANDLORD_TITLES = {"lessor", "landlord", "owner", "agent", "manager", "property manager"}


# ---------------------------------------------------------------- data shapes

@dataclass
class LeaseDocument:
    document_id: str
    tenant_names: list[str]
    unit: str
    generated_on: date | None
    status: str
    sent_on: date | None = None
    last_resent_on: date | None = None
    tenants: list[dict[str, Any]] = field(default_factory=list)  # matched cache rows
    # One entry per tenant signer, read from the lease's signature boxes:
    # {"name": str, "signed_at": datetime | None}. Empty when unknown.
    signers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def unsigned_names(self) -> list[str]:
        return [s["name"] for s in self.signers if not s.get("signed_at")]

    def signer_summary(self) -> str:
        """'Philip Sample: signed 03/31/2026 · Hannah Whitlock: not signed' (empty when unknown)."""
        parts = []
        for signer in self.signers:
            at = signer.get("signed_at")
            parts.append(f"{signer['name']}: " + (f"signed {at:%m/%d/%Y}" if at else "not signed"))
        return " · ".join(parts)

    @property
    def url(self) -> str:
        return _settings.appfolio_url(f"lease_documents/{self.document_id}")

    @property
    def emails(self) -> list[str]:
        found: list[str] = []
        for tenant in self.tenants:
            for email in split_emails(tenant.get("Emails")):
                if email.lower() not in {f.lower() for f in found}:
                    found.append(email)
        return found

    def days_outstanding(self, today: date) -> int:
        start = self.sent_on or self.generated_on or today
        return max(0, (today - start).days)


@dataclass
class ReminderDecision:
    lease: LeaseDocument
    send: bool
    reason: str
    reminder_number: int = 0


# ---------------------------------------------------------------- parsing

def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return _html.unescape(fragment)


def _date(value: str) -> date | None:
    value = (value or "").strip()
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    match = re.search(r"\d{2}/\d{2}/\d{4}", value)
    return datetime.strptime(match.group(0), "%m/%d/%Y").date() if match else None


def parse_lease_list(page_html: str) -> list[LeaseDocument]:
    """Parse AppFolio's /lease_documents?filter_type=... table."""
    leases: list[LeaseDocument] = []
    for document_id, row in _ROW.findall(page_html):
        cells = [_text(c) for c in _CELL.findall(row)]
        if len(cells) < 4:
            continue
        names = [" ".join(n.split()) for n in cells[0].split("\n") if n.strip()]
        leases.append(LeaseDocument(
            document_id=document_id,
            tenant_names=names,
            unit=" ".join(cells[1].split()),
            generated_on=_date(cells[2]),
            status=" ".join(cells[3].split()),
        ))
    return leases


def parse_lease_detail(page_text: str) -> dict[str, Any]:
    """Pull sent / last-resent dates, status and per-signer status from a lease document page."""
    text = _text(page_text) if "<" in page_text else page_text
    sent = _SENT.search(text)
    resent = _RESENT.search(text)
    status = _STATUS.search(text)
    return {
        "sent_on": _date(sent.group(1)) if sent else None,
        "last_resent_on": _date(resent.group(1)) if resent else None,
        "status": status.group(1).strip() if status else "",
        "signers": parse_signers(page_text) if "<" in page_text else [],
    }


def _span(box: str, css_class: str) -> str:
    match = re.search(r'class="' + re.escape(css_class) + r'"[^>]*>(.*?)</span>', box, re.S | re.I)
    if not match:
        return ""
    value = _html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))
    return " ".join(value.replace(chr(0x200B), "").split())


def _signed_at(value: str) -> datetime | None:
    match = re.match(r"(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2}\s*[ap]m)", value, re.I)
    if not match:
        return None
    return datetime.strptime(f"{match.group(1)} {match.group(2).replace(' ', '').upper()}", "%m/%d/%Y %I:%M%p")


def parse_signers(page_html: str) -> list[dict[str, Any]]:
    """Who on the lease has signed, from AppFolio's signature boxes.

    A lease page has one signature block per signed page, each with one box per
    signer in the same order, then the landlord's box. A signed box shows the
    signature, an IP address and a timestamp; an unsigned box shows only the
    signer's name where the timestamp goes. A tenant counts as signed only when
    every one of their boxes is signed. If the blocks don't line up, the result
    is empty (unknown) so nobody is targeted on a guess.
    """
    blocks: list[list[dict[str, Any]]] = []
    starts = [m.start() for m in _SIGNATURE_BLOCK.finditer(page_html)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(page_html)
        boxes = []
        for box in _SIGNATURE_BOX.findall(page_html[start:end]):
            if _span(box, "signer-title").lower() in _LANDLORD_TITLES:
                continue
            signature = _span(box, "signature-box__signature")
            stamp = _span(box, "signature-box__timestamp")
            signed_at = _signed_at(stamp) if signature else None
            boxes.append({"name": signature if signed_at else stamp, "signed_at": signed_at})
        if boxes:
            blocks.append(boxes)
    if not blocks or len({len(b) for b in blocks}) != 1:
        return []
    signers = []
    for position in range(len(blocks[0])):
        boxes = [block[position] for block in blocks]
        unsigned = [b for b in boxes if not b["signed_at"]]
        name = (unsigned[0]["name"] if unsigned else boxes[0]["name"]) or f"Signer {position + 1}"
        signed_at = None if unsigned else max(b["signed_at"] for b in boxes)
        signers.append({"name": name, "signed_at": signed_at})
    return signers


def split_emails(value: Any) -> list[str]:
    return [e.strip() for e in re.split(r"[,;\s]+", str(value or "")) if "@" in e]


# ---------------------------------------------------------------- matching

_ABBREVIATIONS = {
    "avenue": "ave", "av": "ave", "street": "st", "road": "rd", "drive": "dr", "court": "ct",
    "lane": "ln", "boulevard": "blvd", "place": "pl", "terrace": "ter", "circle": "cir",
    "parkway": "pkwy", "highway": "hwy", "north": "n", "south": "s", "east": "e", "west": "w",
    "apartment": "apt", "unit": "apt", "#": "apt", "first": "1st", "second": "2nd", "third": "3rd",
}


def normalize_address(value: Any) -> str:
    words = re.sub(r"[^a-z0-9# ]+", " ", str(value or "").lower()).split()
    words = [_ABBREVIATIONS.get(w, w) for w in words if w not in {"floor", "fl"}]
    return " ".join(words)


def _street_key(value: Any) -> str:
    """House number + first street word, e.g. '24 hawthorn'. Robust to suffix noise."""
    words = normalize_address(value).split()
    return " ".join(words[:2])


def _name_tokens(names: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for name in names:
        for token in re.split(r"[^a-z]+", name.lower()):
            if len(token) > 1 and token not in {"jr", "sr", "ii", "iii", "iv"}:
                tokens.add(token)
    return tokens


def match_tenants(lease: LeaseDocument, tenants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return active cache tenant rows on this lease (same address + name match)."""
    lease_full = normalize_address(lease.unit)
    lease_key = _street_key(lease.unit)
    name_tokens = _name_tokens(lease.tenant_names)
    matches: list[dict[str, Any]] = []
    for row in tenants:
        if str(row.get("Status", "")).strip().lower() not in ACTIVE_TENANT_STATUSES:
            continue
        unit_full = normalize_address(row.get("Unit") or row.get("PropertyStreet"))
        same_place = (
            unit_full == lease_full
            or _street_key(row.get("PropertyStreet")) == lease_key
            or _street_key(row.get("Unit")) == lease_key
        )
        if not same_place:
            continue
        last = str(row.get("LastName") or "").lower()
        last_tokens = {t for t in re.split(r"[^a-z]+", last) if len(t) > 1}
        if last_tokens and last_tokens & name_tokens:
            matches.append(row)
    return matches



def _tokens(value: Any) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", str(value or "").lower()) if len(t) > 1 and t not in {"jr", "sr", "ii", "iii", "iv"}}


def _people(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per person. A tenant renewing has a Current and a Future row; keep the one with most contact info."""
    best: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = (" ".join(sorted(_tokens(row.get("FirstName")))), " ".join(sorted(_tokens(row.get("LastName")))))
        score = (bool(split_emails(row.get("Emails"))), bool(_mobile(row.get("PhoneNumbers"))))
        if key not in best or score > best[key][0]:
            best[key] = (score, row)
    return [row for _, row in best.values()]


def _signer_row(name: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The tenant row for a signer's name: last name must match; first name breaks ties."""
    tokens = _tokens(name)
    people = _people(rows)
    # Exact full name first: a household can list "Victor Rivera" and "Victor Rivera-Jones" as different people.
    exact = [r for r in people if (_tokens(r.get("FirstName")) | _tokens(r.get("LastName"))) == tokens]
    if len(exact) == 1:
        return exact[0]
    by_last = [r for r in people if _tokens(r.get("LastName")) & tokens]
    if len(by_last) <= 1:
        return by_last[0] if by_last else None
    by_first = [r for r in by_last if _tokens(r.get("FirstName")) & tokens]
    return by_first[0] if len(by_first) == 1 else None


def _mobile(value: Any) -> str:
    """The best number to text from AppFolio's 'Mobile: …, Home: …' phone list."""
    parts = [p.strip() for p in re.split(r"[,;\n]+", str(value or "")) if re.search(r"\d{3}", p)]
    for label in ("mobile", "cell"):
        for part in parts:
            if label in part.lower():
                return part.split(":", 1)[-1].strip()
    return parts[0].split(":", 1)[-1].strip() if parts else ""


def _first_name(row: dict[str, Any] | None, fallback: str) -> str:
    first = str((row or {}).get("FirstName") or "").strip()
    if not first:
        words = fallback.split()
        first = words[0] if words else ""
    # Drop middle initials: "Priya R." -> "Priya" (keeps "Maria Jose" and "Noah & Robin").
    first = " ".join(w for w in first.split() if not re.fullmatch(r"[A-Za-z]\.?", w))
    return first.title()


def reminder_facts(lease: LeaseDocument) -> dict[str, Any]:
    """Who still has to sign, how to reach them, and who already signed.

    Used to remind only the people who haven't signed. ``known`` is False when
    the signature boxes couldn't be read; then reminders fall back to everyone
    on the lease. ``issue`` is set when the page contradicts itself (the same
    tenant shows as signed in one box and unsigned in another), and no
    reminder is sent until someone checks the lease in AppFolio.
    """
    if not lease.signers:
        return {"known": False, "toSign": [], "signed": [], "issue": ""}
    rows = lease.tenants
    to_sign, signed, issue = [], [], ""
    unsigned_ids, signed_ids = set(), set()
    for signer in lease.signers:
        row = _signer_row(signer["name"], rows)
        key = id(row) if row is not None else None
        if signer.get("signed_at"):
            signed.append(_first_name(row, signer["name"]))
            if key is not None:
                signed_ids.add(key)
        else:
            if key is not None:
                unsigned_ids.add(key)
            to_sign.append({
                "name": signer["name"],
                "first": _first_name(row, signer["name"]),
                "emails": split_emails((row or {}).get("Emails")),
                "phone": _mobile((row or {}).get("PhoneNumbers")),
                "portal": str((row or {}).get("TenantPortalActivated") or ""),
                "matched": row is not None,
            })
    if unsigned_ids & signed_ids:
        issue = "the same tenant shows as signed and not signed - check who signed in AppFolio"
    return {"known": True, "toSign": to_sign, "signed": signed, "issue": issue}


# ---------------------------------------------------------------- reminder policy

def decide_reminders(
    leases: list[LeaseDocument],
    state: dict[str, Any],
    today: date,
    cfg: dict[str, Any] | None = None,
) -> list[ReminderDecision]:
    cfg = cfg if cfg is not None else _settings.module("leasing")
    first_after = int(cfg.get("first_reminder_after_days", 2))
    every = int(cfg.get("reminder_every_days", 3))
    max_count = int(cfg.get("max_reminders", 6))
    stale_after = int(cfg.get("stale_after_days", 60) or 0)
    decisions: list[ReminderDecision] = []
    for lease in leases:
        if lease.status != OUT_FOR_SIGNING:
            decisions.append(ReminderDecision(lease, False, "not out for signing"))
            continue
        history = state.get("leases", {}).get(lease.document_id, {})
        sent_reminders = [r for r in history.get("reminders", []) if r.get("status") == "sent"]
        if len(sent_reminders) >= max_count:
            decisions.append(ReminderDecision(lease, False, f"reached max {max_count} reminders - staff follow-up"))
            continue
        if not lease.emails:
            decisions.append(ReminderDecision(lease, False, "no tenant email matched - staff follow-up"))
            continue
        if stale_after and lease.days_outstanding(today) > stale_after and not sent_reminders:
            decisions.append(ReminderDecision(
                lease, False, f"stale (over {stale_after} days) - confirm or cancel in AppFolio"))
            continue
        if lease.days_outstanding(today) < first_after:
            decisions.append(ReminderDecision(lease, False, f"sent less than {first_after} days ago"))
            continue
        last_contact_dates = [d for d in (lease.sent_on, lease.last_resent_on) if d]
        last_contact_dates += [date.fromisoformat(r["at"][:10]) for r in sent_reminders]
        last_contact = max(last_contact_dates) if last_contact_dates else None
        if last_contact and (today - last_contact).days < every:
            decisions.append(ReminderDecision(lease, False, f"contacted {(today - last_contact).days} day(s) ago"))
            continue
        decisions.append(ReminderDecision(lease, True, "due", reminder_number=len(sent_reminders) + 1))
    return decisions


def reconcile_state(
    state: dict[str, Any],
    pending: list[LeaseDocument],
    countersign: list[LeaseDocument],
    now_iso: str,
) -> list[dict[str, Any]]:
    """Track first-seen / closed leases. Returns leases closed since last run."""
    leases = state.setdefault("leases", {})
    open_ids = {lease.document_id for lease in pending + countersign}
    for lease in pending + countersign:
        entry = leases.setdefault(lease.document_id, {"first_seen": now_iso, "reminders": []})
        entry.update({
            "status": lease.status, "unit": lease.unit, "tenants": lease.tenant_names,
            "closed_at": None, "last_seen": now_iso,
        })
    closed: list[dict[str, Any]] = []
    for document_id, entry in leases.items():
        if document_id not in open_ids and not entry.get("closed_at"):
            entry["closed_at"] = now_iso
            entry["closed_reported"] = False
            closed.append({"document_id": document_id, **entry})
    return closed


def unreported_closed(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"document_id": document_id, **entry}
        for document_id, entry in state.get("leases", {}).items()
        if entry.get("closed_at") and not entry.get("closed_reported", True)
    ]


# ---------------------------------------------------------------- summary data from cache

def _parse_iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def expiring_leases(tenants: list[dict[str, Any]], today: date, days: int,
                    pending_units: set[str]) -> list[dict[str, Any]]:
    horizon = today + timedelta(days=days)
    rows = []
    for t in tenants:
        if str(t.get("Status", "")).lower() != "current" or str(t.get("PrimaryTenant", "Yes")) != "Yes":
            continue
        lease_to = _parse_iso(t.get("LeaseTo_raw"))
        if not lease_to or not (today <= lease_to <= horizon):
            continue
        key = _street_key(t.get("Unit") or t.get("PropertyStreet"))
        rows.append({
            "tenant": t.get("Tenant") or f"{t.get('LastName', '')}, {t.get('FirstName', '')}",
            "unit": t.get("Unit") or t.get("PropertyStreet"),
            "lease_to": lease_to,
            "days_left": (lease_to - today).days,
            "rent": t.get("Rent"),
            "renewal_out": key in pending_units,
            "occupancy_id": t.get("occupancyId") or t.get("occupancy_id"),
        })
    return sorted(rows, key=lambda r: r["lease_to"])


def month_to_month(tenants: list[dict[str, Any]], today: date) -> int:
    return sum(
        1 for t in tenants
        if str(t.get("Status", "")).lower() == "current"
        and str(t.get("PrimaryTenant", "Yes")) == "Yes"
        and (_parse_iso(t.get("LeaseTo_raw")) or today) < today
    )


def notice_tenants(tenants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"tenant": t.get("Tenant"), "unit": t.get("Unit"), "move_out": _parse_iso(t.get("MoveOut_raw"))}
        for t in tenants
        if str(t.get("Status", "")).lower() == "notice" and str(t.get("PrimaryTenant", "Yes")) == "Yes"
    ]


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def vacancy_rows(vacancies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rent-ready vacant units only, with the property's full address split into columns."""
    rows = [{
        "street": _clean(" ".join(filter(None, [v.get("PropertyStreet") or v.get("Street"), v.get("PropertyStreet2")]))),
        "unit": _clean(v.get("Unit")),
        "city": _clean(v.get("PropertyCity") or v.get("City")),
        "state": _clean(v.get("PropertyState") or v.get("State")),
        "zip": _clean(v.get("PropertyZip") or v.get("Zip")),
        "days_vacant": v.get("DaysVacant_raw"),
        "rent_ready": v.get("RentReady"),
        "rent": v.get("AdvertisedRent") or v.get("SchdRent"),
        "next_move_in": _parse_iso(v.get("NextMoveIn_raw")),
        "posted": v.get("PostedToInternet"),
    } for v in vacancies if str(v.get("RentReady") or "").strip().lower() == "yes"]
    return sorted(rows, key=lambda r: -(r["days_vacant"] or 0))


def application_summary(apps: list[dict[str, Any]], today: date, days: int) -> dict[str, Any]:
    since = today - timedelta(days=days)
    recent = [a for a in apps if (_parse_iso(a.get("Received_raw")) or date.min) >= since]
    by_status: dict[str, int] = {}
    for a in recent:
        status = str(a.get("Status") or a.get("ApplicationStatus") or "Unknown")
        by_status[status] = by_status.get(status, 0) + 1
    open_apps = [
        {"applicant": a.get("Applicants"), "unit": a.get("UnitName") or a.get("PropertyName"),
         "received": _parse_iso(a.get("Received_raw")), "status": a.get("Status")}
        for a in recent
        if str(a.get("Status", "")).lower() not in {"converted", "canceled", "cancelled", "denied", "decision made"}
    ]
    return {"total": len(recent), "by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
            "open": sorted(open_apps, key=lambda r: r["received"] or date.min, reverse=True)}


# ---------------------------------------------------------------- HTML

_CSS = (
    "font-family:Arial,Helvetica,sans-serif;color:#1f2933;font-size:14px;line-height:1.45"
)
_TH = "text-align:left;padding:6px 8px;background:#eef2f6;border-bottom:1px solid #d5dde5;font-size:12px"
_TD = "padding:6px 8px;border-bottom:1px solid #eef2f6;vertical-align:top"


def _e(value: Any) -> str:
    return _html.escape("" if value is None else str(value))


def _fmt(d: date | None) -> str:
    return d.strftime("%b %d, %Y") if d else "-"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<p style="color:#61707d;margin:4px 0 14px">None.</p>'
    head = "".join(f'<th style="{_TH}">{h}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f'<td style="{_TD}">{c}</td>' for c in r) + "</tr>" for r in rows)
    return f'<table style="border-collapse:collapse;width:100%;margin:4px 0 18px">{head}{body}</table>'


def _h2(text: str, count: int | None = None) -> str:
    badge = f' <span style="color:#61707d;font-weight:normal">({count})</span>' if count is not None else ""
    return f'<h2 style="font-size:16px;margin:22px 0 6px;color:#12324a">{text}{badge}</h2>'


def render_summary(*, today: date, pending: list[LeaseDocument], countersign: list[LeaseDocument],
                   decisions: dict[str, ReminderDecision], state: dict[str, Any],
                   closed: list[dict[str, Any]], expiring: list[dict[str, Any]], mtm: int,
                   notices: list[dict[str, Any]], vacancies: list[dict[str, Any]] | None,
                   apps: dict[str, Any] | None, cfg: dict[str, Any]) -> str:
    escalate = int(cfg.get("escalate_after_days", 14))
    link = lambda url, text: f'<a href="{_e(url)}" style="color:#0b6bcb">{_e(text)}</a>'  # noqa: E731

    pending_rows = []
    for lease in sorted(pending, key=lambda l: -l.days_outstanding(today)):
        days = lease.days_outstanding(today)
        history = state.get("leases", {}).get(lease.document_id, {})
        sent_count = len([r for r in history.get("reminders", []) if r.get("status") == "sent"])
        decision = decisions.get(lease.document_id)
        flag = ' <b style="color:#b42318">OVERDUE</b>' if days >= escalate else ""
        emails = ", ".join(lease.emails) or '<b style="color:#b42318">no email matched</b>'
        pending_rows.append([
            link(lease.url, lease.unit) + flag,
            "<br>".join(_e(part) for part in lease.signer_summary().split(" · ")) if lease.signers
            else "<br>".join(_e(n) for n in lease.tenant_names),
            f"{_fmt(lease.sent_on or lease.generated_on)}<br><span style='color:#61707d'>{days} day{"" if days == 1 else "s"}</span>",
            _fmt(lease.last_resent_on),
            str(sent_count),
            _e(decision.reason if decision else ""),
            emails,
        ])

    parts = [
        f'<div style="{_CSS}">',
        f'<h1 style="font-size:20px;margin:0 0 4px;color:#12324a">Leasing summary</h1>',
        f'<p style="margin:0 0 14px;color:#61707d">{_e(_settings.COMPANY_NAME)} - {today.strftime("%A, %B %d, %Y")}</p>',
        '<table style="border-collapse:collapse;margin-bottom:6px"><tr>',
    ]
    for label, value, color in (
        ("Waiting for tenant signature", len(pending), "#b42318" if pending else "#12324a"),
        ("Ready for you to countersign", len(countersign), "#b54708" if countersign else "#12324a"),
        (f"Leases ending in {cfg.get('summary_include_expiring_days', 90)} days", len(expiring), "#12324a"),
        ("Vacant units", len(vacancies or []), "#12324a"),
    ):
        parts.append(
            f'<td style="padding:10px 14px;border:1px solid #d5dde5;text-align:center;min-width:120px">'
            f'<div style="font-size:22px;font-weight:bold;color:{color}">{value}</div>'
            f'<div style="font-size:12px;color:#61707d">{label}</div></td>'
        )
    parts.append("</tr></table>")

    parts.append(_h2("Waiting for tenant signature", len(pending)))
    parts.append(_table(["Unit", "Tenants", "Sent", "AppFolio resent", "Our reminders", "Today", "Emails on file"], pending_rows))

    parts.append(_h2("Ready for you to countersign", len(countersign)))
    parts.append(_table(["Unit", "Tenants", "Generated"], [
        [link(l.url, l.unit), "<br>".join(_e(n) for n in l.tenant_names), _fmt(l.generated_on)] for l in countersign
    ]))

    if closed:
        parts.append(_h2("Signed or closed since the last summary", len(closed)))
        parts.append(_table(["Unit", "Tenants", "Reminders sent"], [
            [_e(c.get("unit")), "<br>".join(_e(n) for n in c.get("tenants", [])),
             str(len([r for r in c.get("reminders", []) if r.get("status") == "sent"]))] for c in closed
        ]))

    parts.append(_h2(f"Leases ending in the next {cfg.get('summary_include_expiring_days', 90)} days", len(expiring)))
    parts.append(_table(["Unit", "Tenant", "Lease ends", "Rent", "Renewal sent?"], [
        [_e(r["unit"]), _e(r["tenant"]), f"{_fmt(r['lease_to'])}<br><span style='color:#61707d'>{r['days_left']} day{"" if r['days_left'] == 1 else "s"}</span>",
         _e(r["rent"]), "Yes - out for signing" if r["renewal_out"] else '<b style="color:#b54708">Not yet</b>']
        for r in expiring
    ]))
    if mtm:
        parts.append(f'<p style="margin:0 0 14px">Month-to-month (lease end date passed): <b>{mtm}</b></p>')
    if notices:
        parts.append(_h2("Tenants on notice", len(notices)))
        parts.append(_table(["Unit", "Tenant", "Move-out"], [[_e(n["unit"]), _e(n["tenant"]), _fmt(n["move_out"])] for n in notices]))
    if vacancies is not None:
        parts.append(_h2("Vacant units, rent ready", len(vacancies)))
        parts.append(_table(["Street", "Unit", "City", "State", "Zip", "Days vacant", "Advertised rent", "Next move-in",
                             "Posted online"], [
            [_e(v["street"]), _e(v["unit"]), _e(v["city"]), _e(v["state"]), _e(v["zip"]), _e(v["days_vacant"]),
             _e(v["rent"]), _fmt(v["next_move_in"]), _e(v["posted"])]
            for v in vacancies
        ]))
    if apps is not None:
        parts.append(_h2(f"Rental applications, last {cfg.get('summary_include_applications_days', 30)} days", apps["total"]))
        if apps["by_status"]:
            parts.append("<p style='margin:0 0 8px'>" + " &middot; ".join(f"{_e(k)}: <b>{v}</b>" for k, v in apps["by_status"].items()) + "</p>")
        parts.append(_table(["Applicant", "Unit", "Received", "Status"], [
            [_e(a["applicant"]), _e(a["unit"]), _fmt(a["received"]), _e(a["status"])] for a in apps["open"][:25]
        ]))
    parts.append('<p style="color:#8a96a3;font-size:12px;margin-top:20px">Generated automatically from AppFolio by the AppFolio Bridge.</p></div>')
    return "".join(parts)


def render_tenant_reminder(lease: LeaseDocument, reminder_number: int, cfg: dict[str, Any]) -> tuple[str, str, str]:
    """Return (subject, html, text) for one tenant reminder."""
    first_names = []
    for tenant in lease.tenants:
        name = str(tenant.get("FirstName") or "").strip()
        if name and name not in first_names:
            first_names.append(name)
    greeting = "Hi " + (" and ".join(first_names) if first_names else "there") + ","
    portal = _settings.get("appfolio.tenant_portal_url", "") or _settings.appfolio_url("connect")
    subject = str(cfg.get("reminder_subject", "Reminder: your lease for {unit} is waiting for your signature")).format(unit=lease.unit)
    intro = cfg.get("reminder_intro", "Your lease is ready and waiting for your electronic signature.")
    sent = _fmt(lease.sent_on or lease.generated_on)
    company = _settings.COMPANY_NAME
    reply_to = _settings.get("email.reply_to", "") or _settings.get("email.from_address", "")
    html_body = f"""<div style="{_CSS};max-width:560px">
<p>{_e(greeting)}</p>
<p>{_e(intro)}</p>
<table style="border-collapse:collapse;margin:10px 0 16px">
<tr><td style="{_TD};color:#61707d">Property</td><td style="{_TD}"><b>{_e(lease.unit)}</b></td></tr>
<tr><td style="{_TD};color:#61707d">Sent</td><td style="{_TD}">{_e(sent)}</td></tr>
</table>
<p><b>How to sign:</b> open the email from AppFolio with the subject about your lease and click the signing link,
or sign in to your resident portal:</p>
<p><a href="{_e(portal)}" style="display:inline-block;background:#0b6bcb;color:#fff;padding:10px 18px;border-radius:4px;text-decoration:none">Open resident portal</a></p>
<p>Every person on the lease needs to sign. If you already signed, thank you - we're waiting on the other signers.
If you have questions or need the lease sent again, just reply to this email{(' (' + _e(reply_to) + ')') if reply_to else ''}.</p>
<p>Thank you,<br>{_e(company)}</p>
</div>"""
    text = (f"{greeting}\n\n{intro}\nProperty: {lease.unit}\nSent: {sent}\n\n"
            f"Open the AppFolio signing email, or sign in at {portal}\n\n"
            f"Every person on the lease needs to sign. Reply to this email with questions.\n\n{company}")
    return subject, html_body, text


# ---------------------------------------------------------------- AppFolio reader

class LeaseDocumentReader:
    """Read-only access to AppFolio's lease e-sign lists via an authorized browser context."""

    def __init__(self, context: Any, base_url: str | None = None) -> None:
        self.context = context
        self.base_url = (base_url or _settings.APPFOLIO_BASE_URL).rstrip("/")

    def _get(self, path: str) -> str:
        response = self.context.request.get(
            self.base_url + path,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=60_000,
            max_redirects=0,
        )
        if response.status in (301, 302, 303, 307, 308) or "sign_in" in response.url:
            raise RuntimeError("AppFolio session expired - refresh it with scripts\\refresh_appfolio_session.ps1")
        if not response.ok:
            raise RuntimeError(f"AppFolio returned HTTP {response.status} for {path}")
        return response.text()

    def list(self, filter_type: str) -> list[LeaseDocument]:
        html_page = self._get("/lease_documents?filter_type=" + filter_type.replace(" ", "+"))
        if "js-lease-document-row" not in html_page and "There are currently no lease documents" not in html_page:
            raise RuntimeError("AppFolio lease list did not look like expected; the page layout may have changed")
        return [lease for lease in parse_lease_list(html_page) if lease.status == filter_type]

    def enrich(self, lease: LeaseDocument) -> LeaseDocument:
        detail = parse_lease_detail(self._get(f"/lease_documents/{lease.document_id}"))
        lease.sent_on = detail["sent_on"]
        lease.last_resent_on = detail["last_resent_on"]
        lease.signers = detail["signers"]
        return lease


# ---------------------------------------------------------------- Apps Script payload

def _iso(value: date | None) -> str:
    return value.isoformat() if value else ""


def _lease_row(lease: LeaseDocument, status: str) -> dict[str, Any]:
    old_end = max((str(t.get("LeaseTo") or "") for t in lease.tenants), default="")
    new_start = max((str(t.get("LeaseFrom") or "") for t in lease.tenants), default="")
    return {
        "documentId": lease.document_id,
        "status": status,
        "unit": lease.unit,
        "names": ", ".join(lease.tenant_names),
        "emails": ", ".join(lease.emails),
        "signers": lease.signer_summary(),
        "facts": reminder_facts(lease),
        "sentOn": _iso(lease.sent_on or lease.generated_on),
        "lastResentOn": _iso(lease.last_resent_on),
        "oldLeaseEnd": old_end,
        "currentLeaseStart": new_start,
        "url": lease.url,
    }


def unit_label(tenant: dict[str, Any]) -> str:
    """The unit as AppFolio's lease list writes it.

    Some units are named without an address ("Upstairs", "First Floor"). Those
    become "24 Hawthorn Ave, - First Floor", the same form as the lease list, so a
    multi-unit building's units stay apart in the sheet.
    """

    unit = str(tenant.get("Unit") or "").strip()
    street = str(tenant.get("PropertyStreet") or tenant.get("PropertyName") or "").strip()
    if unit and street and not re.search(r"\d", unit):
        return f"{street} - {unit}"
    return unit or street


def apps_script_payload(
    *,
    today: date,
    pending: list[LeaseDocument],
    countersign: list[LeaseDocument],
    tenants: list[dict[str, Any]],
    vacancies: list[dict[str, Any]] | None,
    applications: list[dict[str, Any]] | None,
    cfg: dict[str, Any],
    run_reminders: bool,
    run_summary: bool,
    test: bool,
) -> dict[str, Any]:
    """Everything the Apps Script needs to keep the Lease Tracker and send email.

    The bridge reads AppFolio; the script owns the tracker, reminder schedule,
    reminder log and the emails. Only the columns the script uses are sent.
    """

    tenant_rows = [["Unit", "Tenant", "Emails", "Status", "Lease From", "Lease To", "Rent", "Move Out"]]
    for t in tenants:
        if str(t.get("Status", "")).lower() not in ACTIVE_TENANT_STATUSES:
            continue
        tenant_rows.append([
            unit_label(t),
            t.get("Tenant") or "", t.get("Emails") or "", t.get("Status") or "",
            t.get("LeaseFrom") or "", t.get("LeaseTo") or "", t.get("Rent") or "",
            t.get("MoveOut") or "",
        ])
    vacancy_table = None
    if vacancies is not None:
        vacancy_table = [["Street", "Unit", "City", "State", "Zip", "Days Vacant", "Advertised Rent",
                          "Next Move In"]] + [
            # A leading apostrophe keeps Sheets from turning zip 08724 into the number 8724.
            [r["street"], r["unit"], r["city"], r["state"], ("'" + r["zip"]) if r["zip"] else "",
             r["days_vacant"] or "", r["rent"] or "",
             _iso(r["next_move_in"])]
            for r in vacancy_rows(vacancies)
        ]
    application_table = None
    if applications is not None:
        days = int(cfg.get("summary_include_applications_days", 30) or 30)
        summary = application_summary(applications, today, days)
        application_table = [["Applicant", "Unit", "Received", "Status"]] + [
            [a["applicant"] or "", a["unit"] or "", _iso(a["received"]), a["status"] or ""]
            for a in summary["open"]
        ]
    return {
        "schemaVersion": 1,
        "today": today.isoformat(),
        "leases": [_lease_row(l, "Out for signing") for l in pending]
        + [_lease_row(l, "Ready to countersign") for l in countersign],
        "tenants": tenant_rows,
        "vacancies": vacancy_table,
        "applications": application_table,
        "run": {"reminders": bool(run_reminders), "summary": bool(run_summary), "test": bool(test)},
    }
