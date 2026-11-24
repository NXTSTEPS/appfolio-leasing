# AGENTS.md: for AI agents installing or changing AppFolio Lease Reminders

Two parts: the **PC bridge** (this repo, Windows, Python + Playwright) reads AppFolio, and the
**Google Sheet** (`apps-script/Code.gs`) sends every email and holds the settings and the Lease Tracker.

## Rules

- **Never** ask for, type or store the AppFolio password, MFA codes, the encryption passphrase,
  the Gemini key or the `AFB1.` connection code. The user enters them in the browser or the hidden
  prompts the scripts open.
- **Never** commit, copy or print anything from `work/` or `config/company.json`.
- **Never** clear *Test mode email* or set *Send tenant reminders* to YES without the user's
  explicit OK after they've seen the test emails.
- If AppFolio shows a login page, ask the user to run `.\scripts\refresh_appfolio_session.ps1`.
- If AppFolio's layout changed ("did not look like expected"), stop and report. Don't guess new
  selectors against a live account.

## AppFolio: 500 report runs a day per login (read before changing the sync)

- AppFolio shows *"Daily report run limit reached (500 per day)"*; the bridge sees
  `run_limit_reached` and the sync stops. It resets at **midnight Eastern**.
- `scripts/sync_appfolio.py` runs **one report per resource per sync** (`app/settings.py:
  sync_resources()`; leasing reads tenants, vacancies and rental applications).
- **Every PC and install on the same AppFolio login shares the 500.**
- Budget: `reports × (1440 / schedule.sync_every_minutes)`, summed over every install on the login.
  Keep it under about 250. Defaults: 3 reports every 60 minutes = 72 a day.
  `install_schedules.ps1` prints the total and warns above 250.
- If you make the leasing job read a new cached report, add it to `MODULE_SYNC_RESOURCES` in
  `app/settings.py`, or it never refreshes.

## Install steps (the human version is the setup PDF in the latest release)

1. **Sheet:** the user creates a Google Sheet, pastes `apps-script/Code.gs` into Extensions › Apps
   Script, runs *AppFolio Tools › 1. Set up this spreadsheet*, fills Settings (keep *Test mode email*
   filled), deploys a Web app (Execute as: Me, Access: Anyone) and runs *2. Connect the PC bridge* to
   get the `AFB1.` code.
2. **PC:** `powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1`. It installs packages,
   asks the company questions, opens AppFolio for the user to sign in, reads AppFolio once, asks for
   the `AFB1.` code, fills the sheet without sending email, and installs the scheduled tasks.
3. **Test:** `.\scripts\run_job.ps1 -Job leasing -Test` sends `[TEST]` emails to the test address only.
4. **Go live** only on the user's OK: clear *Test mode email*, set *Send tenant reminders* = YES,
   then *AppFolio Tools › Turn on automation*.

## Gemini (optional AI wording)

- Key saved with *AppFolio Tools › Save Gemini API key*. It goes in the `x-goog-api-key` header,
  never in a URL.
- Default model `gemini-3.5-flash`, fallback `gemini-3.5-flash-lite`. A run stops calling Gemini
  after one busy/timeout/401/402/403 failure and after 200 s, and falls back to standard wording.
- HTTP 402 "prepayment credits are depleted" means the key's Google project has no credit.

## Changing the sheet code

- Bump `VERSION` in `Code.gs`. The sheet owner deploys a **new version** (Deploy › Manage
  deployments › edit › New version) and runs *1. Set up this spreadsheet*.
- `setup.ps1` keeps an existing `config/company.json`; new defaults don't reach existing installs.

## Tests

`python -m unittest discover -s tests -t .`
