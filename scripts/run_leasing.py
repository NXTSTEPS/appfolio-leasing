"""Leasing summary + unsigned-lease reminders.

Default is a safe PREVIEW: it reads AppFolio, writes HTML previews under
work/leasing/, prints what it WOULD send, and changes nothing.

    python scripts/run_leasing.py                      # preview only
    python scripts/run_leasing.py --send-summary       # email staff the summary
    python scripts/run_leasing.py --send-reminders     # email tenants who are due
    python scripts/run_leasing.py --send-summary --send-reminders --test
                                                       # everything goes to email.test_recipient
    python scripts/run_leasing.py --sync-only          # Apps Script: refresh the sheet, no email

With ``delivery.method: apps_script`` (the default) the bridge only reads
AppFolio and pushes the lists to the Google Sheet; the Apps Script keeps the
Lease Tracker and sends reminders and the summary using the sheet's Settings.

Tenant reminders also require ``modules.leasing.send_tenant_reminders: true``.
Run through scripts/run_with_bridge_key.ps1 so the session key is available.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import leasing  # noqa: E402
from app import apps_script  # noqa: E402
from app import mailer  # noqa: E402
from app import settings as _settings  # noqa: E402
from app.session_vault import load_session  # noqa: E402
from app.store import store  # noqa: E402

WORK = _settings.WORK_DIR
STATE_PATH = WORK / "leasing-state.json"
LOCK_PATH = WORK / "leasing.lock"
PREVIEW_DIR = WORK / "leasing"


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"version": 1, "leases": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


class RunLock:
    """Single-runner lock so two schedules never double-send."""

    def __enter__(self):
        WORK.mkdir(parents=True, exist_ok=True)
        if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime > 2 * 3600:
            LOCK_PATH.unlink(missing_ok=True)  # stale lock from a crashed run
        try:
            self.fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SystemExit("Another leasing run is in progress; skipping.")
        return self

    def __exit__(self, *_):
        os.close(self.fd)
        LOCK_PATH.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send-summary", action="store_true", help="email the staff leasing summary")
    parser.add_argument("--send-reminders", action="store_true", help="email tenants whose reminder is due")
    parser.add_argument("--test", action="store_true", help="send everything to email.test_recipient")
    parser.add_argument("--today", help="override today's date (YYYY-MM-DD) for testing")
    parser.add_argument("--sync-only", action="store_true",
                        help="Apps Script mode: update the sheet without sending any email")
    args = parser.parse_args()

    cfg = _settings.module("leasing")
    if not cfg.get("enabled"):
        print("Leasing module is disabled in config/company.json (modules.leasing.enabled).")
        return 0
    sending = args.send_summary or args.send_reminders
    if args.send_reminders and not cfg.get("send_tenant_reminders") and not args.test:
        print("Tenant reminders are OFF (modules.leasing.send_tenant_reminders=false). "
              "Showing who WOULD be reminded; nothing will be emailed to tenants.")
    today = date.fromisoformat(args.today) if args.today else datetime.now(_settings.TIMEZONE).date()
    now_iso = datetime.now(UTC).isoformat()

    project = store.project_by_email(_settings.SERVICE_EMAIL)
    if project is None:
        raise SystemExit("Bridge project not found - finish setup (scripts/setup.ps1) first.")
    tenants, tenants_updated = store.get_cache(project["id"], "tenants")
    if not tenants:
        raise SystemExit("Tenant cache is empty - run a sync first.")
    vacancies, _ = store.get_cache(project["id"], "vacancies")
    applications, _ = store.get_cache(project["id"], "rental-applications")

    data_key = os.environ.get("BRIDGE_DATA_KEY", "")
    session = load_session(WORK / "appfolio-session.enc", data_key)

    with RunLock():
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(storage_state=session)
            try:
                reader = leasing.LeaseDocumentReader(context)
                pending = [reader.enrich(lease) for lease in reader.list(leasing.OUT_FOR_SIGNING)]
                countersign = reader.list(leasing.READY_TO_COUNTERSIGN)
            finally:
                context.close()
                browser.close()
                # Never save the browser session here: the scheduled sync owns it.

        for lease in pending + countersign:
            lease.tenants = leasing.match_tenants(lease, tenants)

        if apps_script.enabled():
            payload = leasing.apps_script_payload(
                today=today, pending=pending, countersign=countersign, tenants=tenants,
                vacancies=vacancies if cfg.get("summary_include_vacancies", True) else None,
                applications=applications if cfg.get("summary_include_applications_days") else None,
                cfg=cfg, run_reminders=args.send_reminders, run_summary=args.send_summary,
                test=args.test,
            )
            PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
            (PREVIEW_DIR / "apps-script-payload.json").write_text(
                json.dumps(payload, indent=1, default=str), encoding="utf-8")
            print(f"Out for signing: {len(pending)}   Ready to countersign: {len(countersign)}   "
                  f"No email matched: {sum(1 for l in pending if not l.emails)}")
            if not (sending or args.sync_only):
                print("Preview only: nothing sent to the Google Sheet. "
                      f"Payload: {PREVIEW_DIR / 'apps-script-payload.json'}")
                return 0
            answer = apps_script.send("leasing.sync", payload)
            for line in answer.get("lines", [])[:60]:
                print("  " + str(line))
            return 0

        real_state = load_state()
        state = real_state if sending else copy.deepcopy(real_state)
        leasing.reconcile_state(state, pending, countersign, now_iso)
        decisions = leasing.decide_reminders(pending, state, today, cfg)
        by_id = {d.lease.document_id: d for d in decisions}
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

        # --- tenant reminders -------------------------------------------------
        tenant_sending_allowed = args.send_reminders and (cfg.get("send_tenant_reminders") or args.test)
        due = [d for d in decisions if d.send]
        for decision in due:
            lease = decision.lease
            subject, html_body, text = leasing.render_tenant_reminder(lease, decision.reminder_number, cfg)
            preview = PREVIEW_DIR / f"reminder-{lease.document_id}.html"
            preview.write_text(f"<p><b>To:</b> {', '.join(lease.emails)}<br><b>Subject:</b> {subject}</p>" + html_body, encoding="utf-8")
            if not tenant_sending_allowed:
                print(f"  WOULD REMIND  #{decision.reminder_number}  {lease.unit}  -> {', '.join(lease.emails)}")
                continue
            entry = state["leases"][lease.document_id]
            record = {"at": now_iso, "to": lease.emails, "number": decision.reminder_number, "status": "pending"}
            entry["reminders"].append(record)
            save_state(state)  # reserve before sending so a crash can't double-send
            try:
                message_id = mailer.send(mailer.Email(
                    to=lease.emails,
                    cc=cfg.get("staff_recipients", []) if cfg.get("cc_staff_on_tenant_reminders") else [],
                    subject=subject, html=html_body, text=text,
                ), test_mode=args.test)
                record.update(status="sent", message_id=message_id)
                decision.reason = f"reminder #{decision.reminder_number} sent today"
                print(f"  SENT REMINDER #{decision.reminder_number}  {lease.unit}  -> {', '.join(lease.emails)}")
            except Exception as exc:  # keep going; staff sees the failure in the summary
                record.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
                decision.reason = f"reminder FAILED: {type(exc).__name__}"
                print(f"  FAILED        {lease.unit}: {exc}")
            save_state(state)

        # --- staff summary ----------------------------------------------------
        pending_units = {leasing._street_key(lease.unit) for lease in pending}
        summary_html = leasing.render_summary(
            today=today, pending=pending, countersign=countersign, decisions=by_id, state=state,
            closed=leasing.unreported_closed(state),
            expiring=leasing.expiring_leases(tenants, today, int(cfg.get("summary_include_expiring_days", 90)), pending_units),
            mtm=leasing.month_to_month(tenants, today),
            notices=leasing.notice_tenants(tenants),
            vacancies=leasing.vacancy_rows(vacancies) if cfg.get("summary_include_vacancies", True) else None,
            apps=leasing.application_summary(applications, today, int(cfg.get("summary_include_applications_days", 30)))
            if cfg.get("summary_include_applications_days") else None,
            cfg=cfg,
        )
        (PREVIEW_DIR / "summary-preview.html").write_text(summary_html, encoding="utf-8")
        if args.send_summary:
            mailer.send(mailer.Email(
                to=cfg.get("staff_recipients", []),
                subject=f"Leasing summary - {len(pending)} unsigned, {len(countersign)} to countersign ({today:%b %d})",
                html=summary_html,
            ), test_mode=args.test)
            for entry in state["leases"].values():
                if entry.get("closed_at"):
                    entry["closed_reported"] = True
            state["last_summary_at"] = now_iso
            print("Summary emailed to " + ", ".join(cfg.get("staff_recipients", [])) + (" (TEST MODE)" if args.test else ""))
        if sending:
            save_state(state)

    print(f"\nOut for signing: {len(pending)}   Ready to countersign: {len(countersign)}   "
          f"Reminders due: {len(due)}   No email matched: {sum(1 for l in pending if not l.emails)}")
    print(f"Tenant cache last updated: {tenants_updated}")
    print(f"Preview: {PREVIEW_DIR / 'summary-preview.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
