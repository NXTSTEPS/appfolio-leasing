# AppFolio Lease Reminders

Chases unsigned AppFolio leases until they're signed, and emails your team a daily leasing
summary. A Windows PC reads AppFolio; a Google Sheet you own sends the emails. You don't need
n8n, Zapier or a special AppFolio plan.

> **Setting it up?** Download the setup guide, **`AppFolio-Lease-Reminders-Setup.pdf`**, from the
> [latest release](../../releases/latest). It takes about an hour, once.
> **Setting it up with an AI agent?** Point it at [`AGENTS.md`](AGENTS.md).

## Install on a Windows PC (one command)

Open **PowerShell** and paste this. It downloads the latest release to `C:\appfolio-leasing` and
starts setup, which asks its questions one at a time:

```powershell
$z="$env:TEMP\appfolio-leasing.zip"; Invoke-WebRequest https://github.com/JerryWest1/appfolio-leasing/releases/latest/download/appfolio-leasing.zip -OutFile $z; Expand-Archive $z C:\ -Force; cd C:\appfolio-leasing; Get-ChildItem -Recurse | Unblock-File; powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

Running it again later updates the code and keeps your settings (`config\company.json` and `work\`
are never in the zip).

## What it does

| Feature | What happens | When |
|---|---|---|
| **Unsigned-lease reminders** | Reads AppFolio's *Out for Signing* list and emails only the tenants who haven't signed yet, every few days, until the lease is signed. Optional AI wording (Google Gemini) writes each reminder from the lease's real situation, in English or Spanish | Weekdays 9:00 |
| **Text drafts** | A ready-to-send text message for each reminder, in the Lease Tracker's *Text to send* column | With each reminder |
| **Leasing summary** | Unsigned leases with days outstanding and OVERDUE / STALE flags, leases to countersign, renewals to start, leases signed this week, vacant units, open applications | Weekdays 9:00 |
| **Problem alerts** | AppFolio sign-in expired, sync stuck, the PC silent for a day | Hourly / 6-hourly |

Test mode is on until you turn it off: every email goes only to you, marked `[TEST]`.

## AppFolio's daily report limit

> [!IMPORTANT]
> AppFolio allows **500 report runs a day per login**, resetting at midnight Eastern. Every PC and
> install signed in with the **same AppFolio login shares those 500**. Past the limit, every sync
> fails with `run_limit_reached` until midnight.
>
> This install reads 3 reports (tenants, vacancies, rental applications) once an hour: **72 runs a
> day**. Before you sync more often or run another copy on the same login, add up
> *reports × syncs a day* across every install and keep the total under about 250. The interval is
> `schedule.sync_every_minutes` in `config\company.json`.

## What you need

- **AppFolio:** a login with access to reports and leasing (a dedicated user is best). You sign in
  yourself, including MFA; the password is never stored.
- **Google:** a Gmail or Google Workspace account to send from (a shared one such as
  `leasing@yourcompany.com` works best).
- **A Windows 10/11 PC** that is on during business hours.
- *Optional:* a **Gemini API key** from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
  for AI wording.

## How it works

```
 AppFolio ──(the PC reads it, read-only)──▶ PC ── HTTPS + secret key ──▶ Google Sheet ──▶ tenants, staff
```

The PC never sends email and never changes anything in AppFolio. The sheet never connects to
AppFolio. AppFolio is a trademark of AppFolio, Inc.; this project is not affiliated with it.
