/**
 * AppFolio Assistant - Google Sheet + Apps Script.
 *
 * Works with the AppFolio Bridge on a Windows PC:
 *
 *   AppFolio --(bridge reads it)--> PC bridge --(HTTPS POST)--> this script --> emails
 *                                                                       \--> this sheet
 *
 * The bridge does only what needs AppFolio: read data, download ledger PDFs and
 * enter approved bills. This script does everything else: keeps the Lease
 * Tracker, sends tenant reminders and staff emails, and holds bill approvals.
 * Who gets what is set in the Settings tab - no code changes needed.
 *
 * Without a PC the sheet can still run on AppFolio's scheduled report emails
 * or the AppFolio Reports API ("Data source" setting), with fewer features.
 *
 * Install: apps-script/README.md. Version history: see VERSION below.
 */

var VERSION = '2.3.3';
var AI_FALLBACK_MODEL = 'gemini-3.5-flash-lite';   // used when the chosen model is overloaded
// One run (a sync or a menu click) is one script execution, so these reset every time.
var RUN_STARTED_ = Date.now();
var AI_BUDGET_MS_ = 200000;  // the PC bridge waits 330s for an answer and Apps Script stops at 360s: stop asking Gemini after 200s
var aiDown_ = '';            // set once Gemini hangs or is overloaded; the rest of the run skips it

// ================================================================ menu

function onOpen() {
  var ui = SpreadsheetApp.getUi();
  ui.createMenu('AppFolio Tools')
    .addItem('1. Set up this spreadsheet', 'setup')
    .addItem('2. Connect the PC bridge', 'connectBridge')
    .addItem('Connection status', 'showStatus')
    .addSeparator()
    .addItem('Preview reminders (no email)', 'previewReminders')
    .addItem('Send due reminders now', 'sendDueReminders')
    .addItem('Email me a sample reminder for the selected row', 'sampleReminder')
    .addItem('Send leasing summary now', 'sendSummary')
    .addItem('Save Gemini API key (AI wording)', 'promptGeminiKey')
    .addSeparator()
    .addItem('Turn on automation', 'installTriggers')
    .addItem('Turn off automation', 'removeTriggers')
    .addSubMenu(ui.createMenu('Without the PC bridge')
      .addItem('Refresh AppFolio data now (email/API)', 'refreshData')
      .addItem('Save AppFolio API keys (API mode)', 'promptApiKeys'))
    .addToUi();
}

// ================================================================ tabs and settings

var TAB = {
  settings: 'Settings',
  tracker: 'Lease Tracker',
  log: 'Reminder Log',
  bridgeLog: 'Bridge Log',
  tenants: 'Data: Tenants',
  vacancies: 'Data: Vacancies',
  applications: 'Data: Applications',
};

// [name, default, help]. A row whose name starts with "##" is a section heading.
var DEFAULT_SETTINGS = [
  ['## General', '', ''],
  ['Company name', 'Your Property Management Company', 'Shown in emails'],
  ['Reply-to email', '', 'Replies to any email go here'],
  ['Test mode email', '', 'While filled, EVERY email goes only to this address, marked [TEST]'],
  ['Alert emails', '', 'Who hears about problems: PC bridge offline, a failed job, an AppFolio sign-in that expired'],
  ['Data source', 'bridge', 'bridge (PC bridge, recommended), email (AppFolio report emails) or api (AppFolio Reports API)'],
  ['## Leasing', '', ''],
  ['Staff emails', '', 'Get the leasing summary and are CC\'d on tenant reminders. Comma-separated'],
  ['Tenant portal link', 'https://yourcompany.appfolio.com/connect', 'Your AppFolio resident portal'],
  ['Send tenant reminders', 'NO', 'YES to email tenants. Leave NO until the preview looks right'],
  ['First reminder after (days)', '2', 'Days after the lease was sent'],
  ['Remind every (days)', '3', 'An AppFolio "Resend" also counts as a reminder'],
  ['Maximum reminders', '6', 'After this the lease is flagged for a phone call'],
  ['Escalate after (days)', '14', 'Marked OVERDUE in the summary'],
  ['Stale after (days)', '60', 'Older unsigned leases are never emailed automatically: confirm or cancel them in AppFolio'],
  ['Leases ending within (days)', '90', 'Added to the tracker as Renewal due'],
  ['Reminder subject', 'Reminder: your lease for {unit} is waiting for your signature', '{unit} is replaced with the address'],
  ['Reminder subject (Spanish)', 'Recordatorio: su contrato de arrendamiento de {unit} está esperando su firma', 'Used for Lease Tracker rows whose Language is Spanish'],
  ['Reminders signed by', '', 'Your first name, e.g. Dana. Reminder emails and texts are signed with it and the company name'],
  ['AI reminder wording', 'YES', 'YES = Gemini writes each reminder email and text so they read personally (needs AppFolio Tools > Save Gemini API key). Falls back to the standard wording if Gemini is unavailable'],
  ['AI model', 'gemini-3.5-flash', 'Gemini model name. gemini-3.5-flash is reliable on free keys (Nov 2026); 3.8-flash often answers 503 or hangs. If this one is busy, gemini-3.5-flash-lite is tried'],
  ['## Without the PC bridge (Data source email or api)', '', ''],
  ['AppFolio address', 'yourcompany', 'The part before .appfolio.com (API mode)'],
  ['Tenants report search', 'from:appfolio subject:"Tenant Directory" has:attachment newer_than:3d', 'Gmail search for the scheduled Tenant Directory CSV'],
  ['Vacancy report search', 'from:appfolio subject:"Unit Vacancy" has:attachment newer_than:3d', 'Optional'],
  ['Applications report search', 'from:appfolio subject:"Rental Applications" has:attachment newer_than:3d', 'Optional'],
  ['Tenants API report', 'tenant_directory', 'API mode'],
  ['Vacancy API report', 'unit_vacancy', 'API mode'],
  ['Applications API report', 'rental_applications', 'API mode'],
];

function settings_() {
  var sheet = SpreadsheetApp.getActive().getSheetByName(TAB.settings);
  if (!sheet) throw new Error('Run "AppFolio Tools > 1. Set up this spreadsheet" first.');
  var values = sheet.getRange(2, 1, Math.max(sheet.getLastRow() - 1, 1), 2).getValues();
  var s = {};
  values.forEach(function (row) { if (row[0]) s[String(row[0]).trim()] = String(row[1] == null ? '' : row[1]).trim(); });
  var num = function (name, dflt) { var n = Number(s[name]); return isFinite(n) && s[name] !== '' ? n : dflt; };
  return {
    raw: s,
    company: s['Company name'] || '',
    replyTo: s['Reply-to email'] || '',
    testEmail: s['Test mode email'] || '',
    alerts: splitEmails_(s['Alert emails']),
    source: (s['Data source'] || 'bridge').toLowerCase(),
    staff: splitEmails_(s['Staff emails']),
    portal: s['Tenant portal link'] || '',
    sendTenant: /^y/i.test(s['Send tenant reminders'] || ''),
    signedBy: s['Reminders signed by'] || '',
    ai: !/^n/i.test(s['AI reminder wording'] || 'YES'),
    aiModel: s['AI model'] || 'gemini-3.5-flash',
    firstAfter: num('First reminder after (days)', 2),
    every: num('Remind every (days)', 3),
    max: num('Maximum reminders', 6),
    escalate: num('Escalate after (days)', 14),
    stale: num('Stale after (days)', 60),
    expiringDays: num('Leases ending within (days)', 90),
    reminderSubject: s['Reminder subject'] || 'Reminder: your lease for {unit} is waiting for your signature',
    reminderSubjectEs: s['Reminder subject (Spanish)'] || 'Recordatorio: su contrato de arrendamiento de {unit} está esperando su firma',
    subdomain: s['AppFolio address'],
    searches: { tenants: s['Tenants report search'], vacancies: s['Vacancy report search'],
                applications: s['Applications report search'],
              },
    apiReports: { tenants: s['Tenants API report'], vacancies: s['Vacancy API report'],
                  applications: s['Applications API report'],
                },
    forceTest: false
  };
}

/** Everyone who should hear about a problem, never empty. */
function alertTo_(cfg) {
  if (cfg.alerts.length) return cfg.alerts;
  if (cfg.staff.length) return cfg.staff;
  var owner = Session.getEffectiveUser().getEmail();
  return owner ? [owner] : [];
}

// ================================================================ setup

var TRACKER_HEADERS = ['Status', 'Unit / Property', 'Tenant names', 'Tenant emails', 'Lease sent on',
  'New lease starts', 'Old lease ends', 'Reminders sent', 'Last reminder', 'Signed on', 'Notes',
  'AppFolio resent on', 'AppFolio lease ID', 'Signature status', 'Reminders go to', 'Text to send', 'Reminder facts',
  'Language'];
var C = { status: 0, unit: 1, names: 2, emails: 3, sent: 4, newStart: 5, oldEnd: 6, count: 7, last: 8,
          signed: 9, notes: 10, resent: 11, docId: 12, signers: 13, remind: 14, text: 15, facts: 16, lang: 17 };
var LANGUAGES = ['English', 'Spanish'];
var TRACKER_STATUSES = ['Out for signing', 'Ready to countersign', 'Renewal due', 'Signed', 'Paused', 'Cancelled'];


function setup() {
  var ss = SpreadsheetApp.getActive();
  var s = ss.getSheetByName(TAB.settings) || ss.insertSheet(TAB.settings, 0);
  if (s.getLastRow() < 1) {
    s.getRange(1, 1, 1, 3).setValues([['Setting', 'Value', 'Help']]).setFontWeight('bold');
    s.setFrozenRows(1);
    s.setColumnWidth(1, 240); s.setColumnWidth(2, 380); s.setColumnWidth(3, 520);
  }
  // Add any settings this version introduced, keeping existing values.
  var existing = {};
  if (s.getLastRow() > 1) s.getRange(2, 1, s.getLastRow() - 1, 1).getValues().forEach(function (r) { existing[String(r[0]).trim()] = true; });
  var owner = Session.getEffectiveUser().getEmail();
  DEFAULT_SETTINGS.forEach(function (row) {
    if (existing[row[0]]) return;
    var value = row[1];
    if (!value && owner && /^(Reply-to email|Alert emails|Staff emails|Bill approvers)$/.test(row[0])) value = owner;
    s.appendRow([row[0], value, row[2]]);
  });

  ensureTab_(TAB.tracker, TRACKER_HEADERS, 1);
  var tracker = ss.getSheetByName(TAB.tracker);
  tracker.getRange(2, 1, 999, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(TRACKER_STATUSES, true).build());
  tracker.hideColumns(C.facts + 1); // machine-readable; "Reminders go to" and "Text to send" are the readable version
  // Blank means English. AppFolio doesn't record a tenant's language, so staff pick Spanish here.
  tracker.getRange(2, C.lang + 1, 999, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(LANGUAGES, true).setAllowInvalid(false).build());
  ensureTab_(TAB.log, ['When', 'Unit', 'To', 'Reminder #', 'Result', 'Detail']);
  ensureTab_(TAB.bridgeLog, ['When', 'Action', 'Delivery ID', 'Result', 'Detail']);
  SpreadsheetApp.getUi().alert('Ready.\n\n1. Fill in the Settings tab (emails are pre-filled with your address).\n' +
    '2. Deploy the web app (README step A4), then AppFolio Tools > 2. Connect the PC bridge.');
}

function ensureTab_(name, headers, position) {
  var ss = SpreadsheetApp.getActive();
  var sheet = ss.getSheetByName(name) || (position != null ? ss.insertSheet(name, position) : ss.insertSheet(name));
  var width = headers.length;
  // A tab made narrower than this version needs (e.g. by an older install) gets the missing columns first;
  // reading past the last column throws "outside the dimensions of the sheet".
  if (sheet.getMaxColumns() < width) sheet.insertColumnsAfter(sheet.getMaxColumns(), width - sheet.getMaxColumns());
  var current = sheet.getLastRow() >= 1 ? sheet.getRange(1, 1, 1, width).getValues()[0] : [];
  var changed = false;
  for (var i = 0; i < width; i++) {
    if (String(current[i] || '') !== headers[i]) {
      if (current[i]) continue; // never overwrite a column someone renamed or used
      current[i] = headers[i]; changed = true;
    }
  }
  if (changed || sheet.getLastRow() < 1) {
    sheet.getRange(1, 1, 1, width).setValues([current]).setFontWeight('bold');
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function col_(headers, name) { return headers.indexOf(name) + 1; }

// ================================================================ PC bridge connection

function connectBridge() {
  var ui = SpreadsheetApp.getUi();
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('BRIDGE_TOKEN');
  if (token) {
    var keep = ui.alert('Connect the PC bridge', 'This sheet already has a connection key.\n\nYES = show the same code again.\n' +
      'NO = make a NEW key (the PC must then be connected again).', ui.ButtonSet.YES_NO);
    if (keep === ui.Button.NO) token = null;
  }
  if (!token) {
    token = randomKey_();
    props.setProperty('BRIDGE_TOKEN', token);
    props.setProperty('BILL_HMAC_KEY', randomKey_());
  }
  if (!props.getProperty('BILL_HMAC_KEY')) props.setProperty('BILL_HMAC_KEY', randomKey_());
  var detected = '';
  try { detected = ScriptApp.getService().getUrl() || ''; } catch (e) { detected = ''; }
  var answer = ui.prompt('Web app URL',
    'Paste the Web app URL from Deploy > Manage deployments (it ends with /exec).' +
    (/\/exec$/.test(detected) ? '\n\nLeave blank to use:\n' + detected : ''), ui.ButtonSet.OK_CANCEL);
  if (answer.getSelectedButton() !== ui.Button.OK) return;
  var url = answer.getResponseText().replace(/\s+/g, '') || detected;
  url = url.replace(/\/u\/\d+(?=\/)/, '');  // browser account number, not part of the address
  if (!WEB_APP_URL_RE.test(url)) {
    ui.alert('That is not one web app URL:\n\n' + url + '\n\nIt should look like\nhttps://script.google.com/macros/s/AKfy…/exec\n' +
      '(or .../a/macros/yourdomain.com/s/AKfy…/exec). Clear the box, paste the URL once, and try again.');
    return;
  }
  props.setProperty('WEB_APP_URL', url);
  var code = 'AFB1.' + Utilities.base64EncodeWebSafe(JSON.stringify({
    url: url, token: token, billKey: props.getProperty('BILL_HMAC_KEY')
  })).replace(/=+$/, '');
  var html = HtmlService.createHtmlOutput(
    '<div style="font-family:Arial,sans-serif;font-size:13px">' +
    '<p>On the PC run <b>.\\scripts\\connect_apps_script.ps1</b> (setup.ps1 does this for you) and paste this code:</p>' +
    '<textarea id="c" style="width:100%;height:110px;font-family:monospace;font-size:12px" readonly>' + esc_(code) + '</textarea>' +
    '<p><button onclick="var c=document.getElementById(\'c\');c.select();document.execCommand(\'copy\');this.innerText=\'Copied\'">Copy code</button></p>' +
    '<p style="color:#8a5a00">Treat this code like a password: it lets the PC send email from this account and sign bill approvals.</p></div>')
    .setWidth(520).setHeight(300);
  ui.showModalDialog(html, 'Connection code');
}

// Same rule as app/apps_script.py: personal (/macros/s/ID/exec) or Workspace (/a/macros/DOMAIN/s/ID/exec).
var WEB_APP_URL_RE = /^https:\/\/script\.google\.com\/(?:a\/macros\/[A-Za-z0-9.-]+|macros)\/s\/[A-Za-z0-9_-]{20,}\/exec$/;

function randomKey_() { return (Utilities.getUuid() + Utilities.getUuid()).replace(/-/g, ''); }

function showStatus() {
  var props = PropertiesService.getScriptProperties();
  var last = props.getProperty('LAST_BRIDGE_CONTACT');
  var lines = [
    'Script version: ' + VERSION,
    'Connected: ' + (props.getProperty('BRIDGE_TOKEN') ? 'key created' : 'NOT YET (AppFolio Tools > 2)'),
    'Last message from the PC: ' + (last ? new Date(last).toString() : 'never'),
    'PC name: ' + (props.getProperty('BRIDGE_HOST') || '-'),
    'Bridge project: ' + (props.getProperty('BRIDGE_PROJECT_ID') || '-'),
    'Automation triggers: ' + ScriptApp.getProjectTriggers().map(function (t) { return t.getHandlerFunction(); }).join(', ')
  ];
  SpreadsheetApp.getUi().alert(lines.join('\n'));
}

/** A browser visit shows this; the bridge always uses POST. */
function doGet() {
  return json_({ ok: true, app: 'AppFolio Assistant', version: VERSION, note: 'This address is used by the PC bridge.' });
}

var NO_DEDUPE = { 'ping': true, 'bills.queue': true, 'bills.result': true, 'bills.payment': true };

function doPost(e) {
  var request;
  try { request = JSON.parse(e && e.postData ? e.postData.contents : ''); }
  catch (err) { return json_({ ok: false, error: 'Request body is not JSON' }); }
  var props = PropertiesService.getScriptProperties();
  var expected = props.getProperty('BRIDGE_TOKEN');
  if (!expected) return json_({ ok: false, error: 'Not connected yet: AppFolio Tools > 2. Connect the PC bridge' });
  if (!request || !safeEqual_(String(request.token || ''), expected)) {
    bridgeLog_(String(request && request.action || '?'), '', 'REFUSED', 'wrong token');
    return json_({ ok: false, error: 'Wrong connection key. Connect the PC bridge again.' });
  }
  var action = String(request.action || '');
  var handler = HANDLERS[action];
  if (!handler) return json_({ ok: false, error: 'Unknown action ' + action + ' (update Code.gs?)' });
  var deliveryId = String(request.deliveryId || '');
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(120000)) return json_({ ok: false, error: 'Sheet is busy, try again' });
  try {
    props.setProperty('LAST_BRIDGE_CONTACT', new Date().toISOString());
    if (deliveryId && !NO_DEDUPE[action] && alreadyDelivered_(deliveryId)) {
      return json_({ ok: true, duplicate: true, result: 'already delivered' });
    }
    var result = handler(request.payload || {}, request) || {};
    var quiet = (action === 'bills.queue' && !(result.items || []).length) || (action === 'ping' && !(request.payload || {}).sendTestEmail && (request.payload || {}).heartbeat);
    if (!quiet) {
      bridgeLog_(action, deliveryId, 'ok', result.result || '');
    }
    result.ok = true;
    return json_(result);
  } catch (err) {
    bridgeLog_(action, deliveryId, 'ERROR', String(err && err.message || err));
    return json_({ ok: false, error: String(err && err.message || err) });
  } finally {
    lock.releaseLock();
  }
}

var HANDLERS = {
  'ping': handlePing_,
  'leasing.sync': handleLeasingSync_,
  'alert': handleAlert_,
};

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function safeEqual_(a, b) {
  if (a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function bridgeLog_(action, deliveryId, result, detail) {
  var sheet = SpreadsheetApp.getActive().getSheetByName(TAB.bridgeLog) ||
    ensureTab_(TAB.bridgeLog, ['When', 'Action', 'Delivery ID', 'Result', 'Detail']);
  sheet.appendRow([new Date(), action, deliveryId, result, String(detail || '').slice(0, 500)]);
  if (sheet.getLastRow() > 6000) sheet.deleteRows(2, 1000);
}

function alreadyDelivered_(deliveryId) {
  var sheet = SpreadsheetApp.getActive().getSheetByName(TAB.bridgeLog);
  if (!sheet || sheet.getLastRow() < 2) return false;
  var rows = sheet.getRange(2, 3, sheet.getLastRow() - 1, 2).getValues();
  for (var i = rows.length - 1; i >= 0; i--) {
    if (rows[i][0] === deliveryId && rows[i][1] === 'ok') return true;
  }
  return false;
}

function handlePing_(p) {
  var props = PropertiesService.getScriptProperties();
  if (p.projectId) props.setProperty('BRIDGE_PROJECT_ID', String(p.projectId));
  if (p.host) props.setProperty('BRIDGE_HOST', String(p.host));
  var out = { result: 'connected', sheet: SpreadsheetApp.getActive().getName(), version: VERSION,
              projectId: props.getProperty('BRIDGE_PROJECT_ID') || '' };
  if (p.sendTestEmail) {
    var cfg = settings_();
    var to = alertTo_(cfg);
    deliver_(cfg, to, 'AppFolio Assistant: the PC bridge is connected',
      '<p>The PC bridge <b>' + esc_(p.host || '') + '</b> reached this sheet. Emails will come from this account.</p>');
    out.testEmail = cfg.testEmail || to.join(', ');
  }
  return out;
}

// ================================================================ watchdog and automation

function installTriggers() {
  removeTriggers();
  var cfg = settings_();
  var made = [];
  if (cfg.source === 'bridge') {
    ScriptApp.newTrigger('watchdog').timeBased().everyHours(6).create();
    made.push('PC bridge watchdog (every 6 hours)');
  } else {
    ScriptApp.newTrigger('dailyRun').timeBased().everyDays(1).atHour(8).create();
    made.push('daily run at 8-9 AM (data from AppFolio ' + cfg.source + ')');
  }
  SpreadsheetApp.getUi().alert('Automation is on:\n- ' + made.join('\n- ') +
    (cfg.source === 'bridge' ? '\n\nThe PC bridge decides when emails go out (its scheduled tasks).' : ''));
}

function removeTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (tr) { ScriptApp.deleteTrigger(tr); });
}

/** Emails the alert list if the PC bridge has gone quiet (PC off, asleep, sign-in expired). */
function watchdog() {
  var props = PropertiesService.getScriptProperties();
  var last = props.getProperty('LAST_BRIDGE_CONTACT');
  var hours = last ? (Date.now() - new Date(last).getTime()) / 3600000 : 999;
  if (hours < 26) { props.deleteProperty('WATCHDOG_ALERTED'); return; }
  if (props.getProperty('WATCHDOG_ALERTED') === new Date().toDateString()) return;
  var cfg = settings_();
  deliver_(cfg, alertTo_(cfg), 'AppFolio Assistant: the PC bridge has not checked in',
    '<p>The PC bridge last contacted this sheet <b>' + (last ? esc_(new Date(last).toString()) : 'never') + '</b>.</p>' +
    '<p>Until it is back, no reminders, reports or bills are processed. Check that the PC <b>' +
    esc_(props.getProperty('BRIDGE_HOST') || '') + '</b> is on and awake, then on it run ' +
    '<code>.\\scripts\\run_job.ps1 -Job health</code> and <code>.\\scripts\\connect_apps_script.ps1 -TestOnly</code>. ' +
    'If AppFolio\'s sign-in expired, run <code>.\\scripts\\refresh_appfolio_session.ps1</code>.</p>');
  props.setProperty('WATCHDOG_ALERTED', new Date().toDateString());
}

// ================================================================ leasing: data from the PC bridge

function handleLeasingSync_(p) {
  if (p.tenants) writeTable_(TAB.tenants, p.tenants);
  if (p.vacancies) writeTable_(TAB.vacancies, p.vacancies);
  if (p.applications) writeTable_(TAB.applications, p.applications);
  var today = p.today ? asDate_(p.today) : todayDate_();
  var changes = syncTrackerFromBridge_(p.leases || [], today);
  updateTracker_(today);
  var run = p.run || {};
  var lines = changes.slice();
  var cfg = settings_();
  cfg.forceTest = !!run.test;
  if (run.reminders) lines = lines.concat(runReminders_(true, cfg, today));
  if (run.summary) { sendSummary_(cfg, today); lines.push('Leasing summary sent'); }
  var pending = (p.leases || []).filter(function (l) { return l.status === 'Out for signing'; }).length;
  return { result: pending + ' out for signing; ' + (run.reminders || run.summary ? 'emails processed' : 'sheet updated only'),
           lines: lines };
}

/** Make the tracker match AppFolio's live "Out for Signing" / "Ready to Countersign" lists. */
function syncTrackerFromBridge_(leases, today) {
  var cfg = settings_();
  var sheet = ensureTab_(TAB.tracker, TRACKER_HEADERS, 1);
  var rows = trackerRows_(sheet);
  var byDoc = {}, lines = [];
  rows.forEach(function (r, i) { if (r[C.docId]) byDoc[String(r[C.docId])] = i; });
  var live = {};
  leases.forEach(function (l) {
    var id = String(l.documentId || '');
    if (!id) return;
    live[id] = true;
    var i = byDoc[id];
    if (i === undefined) {
      // A row staff typed by hand for the same unit becomes this lease's row.
      for (var k = 0; k < rows.length; k++) {
        if (!rows[k][C.docId] && (rows[k][C.status] === 'Out for signing' || rows[k][C.status] === 'Renewal due') &&
            normalize_(rows[k][C.unit]) === normalize_(l.unit)) { i = k; break; }
      }
    }
    if (i === undefined) {
      rows.push(blankTrackerRow_());
      i = rows.length - 1;
      lines.push(l.unit + ': added (' + l.status + ')');
    }
    var r = rows[i];
    if (r[C.status] !== 'Paused' && r[C.status] !== 'Cancelled') r[C.status] = l.status || 'Out for signing';
    r[C.unit] = r[C.unit] || l.unit;
    if (l.names) r[C.names] = l.names;
    if (l.emails) r[C.emails] = l.emails;
    r[C.signers] = l.signers || '';
    // The language is stored with the facts, so switching a row to Spanish also redrafts its text.
    var facts = l.facts && l.facts.known ? JSON.stringify(langOf_(r) === 'es' ? Object.assign({}, l.facts, { lang: 'es' }) : l.facts) : '';
    if (facts !== String(r[C.facts] || '')) {
      // Who still has to sign changed (or first sync): start a fresh text draft for them.
      r[C.facts] = facts;
      r[C.text] = facts ? templateText_(cfg, r[C.unit], l.facts, langOf_(r)) : '';
    }
    r[C.remind] = facts ? remindLabel_(l.facts) : '';
    if (l.sentOn) r[C.sent] = asDate_(l.sentOn);
    if (l.oldLeaseEnd && !r[C.oldEnd]) r[C.oldEnd] = asDate_(l.oldLeaseEnd);
    r[C.resent] = l.lastResentOn ? asDate_(l.lastResentOn) : r[C.resent];
    r[C.docId] = id;
    if (!r[C.count]) r[C.count] = 0;
  });
  rows.forEach(function (r) {
    if (!r[C.docId] || live[String(r[C.docId])]) return;
    if (r[C.status] === 'Out for signing' || r[C.status] === 'Ready to countersign') {
      r[C.status] = 'Signed'; r[C.signed] = today;
      r[C.notes] = appendNote_(r[C.notes], 'Closed: no longer pending in AppFolio (signed or cancelled there)');
      lines.push(r[C.unit] + ': closed (no longer pending in AppFolio)');
    }
  });
  writeTrackerRows_(sheet, rows);
  return lines;
}

function blankTrackerRow_() { var r = []; for (var i = 0; i < TRACKER_HEADERS.length; i++) r.push(''); r[C.count] = 0; return r; }
function appendNote_(note, text) { return note ? note + ' | ' + text : text; }

function trackerRows_(sheet) {
  sheet = sheet || SpreadsheetApp.getActive().getSheetByName(TAB.tracker);
  if (!sheet || sheet.getLastRow() < 2) return [];
  return sheet.getRange(2, 1, sheet.getLastRow() - 1, TRACKER_HEADERS.length).getValues();
}

function writeTrackerRows_(sheet, rows) {
  if (rows.length) sheet.getRange(2, 1, rows.length, TRACKER_HEADERS.length).setValues(rows);
}

// ================================================================ leasing: data without the PC bridge

function dailyRun() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return;
  try {
    var cfg = settings_();
    refreshData();
    updateTracker_();
    runReminders_(true, cfg);
    sendSummary_(cfg);
  } catch (err) {
    var c = settings_();
    deliver_(c, alertTo_(c), 'AppFolio Assistant: daily run failed',
      '<p>The daily run stopped with this error:</p><pre>' + esc_(err && err.stack || err) + '</pre>');
    throw err;
  } finally {
    lock.releaseLock();
  }
}

function refreshData() {
  var cfg = settings_();
  if (cfg.source === 'bridge') {
    try { SpreadsheetApp.getUi().alert('Data source is "bridge": the PC bridge fills these tabs. Nothing to refresh here.'); } catch (e) {}
    return [];
  }
  var loaded = [];
  var kinds = ['tenants', 'vacancies', 'applications'];
  kinds.forEach(function (kind) {
    var rows = cfg.source === 'api' ? fetchApi_(cfg, cfg.apiReports[kind]) : fetchEmailCsv_(cfg.searches[kind]);
    if (rows === null) return; // not configured / no email today: keep the last copy
    writeTable_(TAB[kind], rows);
    loaded.push(kind + ' (' + Math.max(rows.length - 1, 0) + ')');
  });
  Logger.log('Loaded: ' + loaded.join(', '));
  return loaded;
}

function fetchEmailCsv_(query) {
  if (!query) return null;
  var threads = GmailApp.search(query, 0, 5);
  for (var i = 0; i < threads.length; i++) {
    var messages = threads[i].getMessages();
    for (var m = messages.length - 1; m >= 0; m--) {
      var atts = messages[m].getAttachments();
      for (var a = 0; a < atts.length; a++) {
        if (/\.csv$/i.test(atts[a].getName())) return Utilities.parseCsv(atts[a].getDataAsString());
      }
    }
  }
  return null;
}

function promptApiKeys() {
  var ui = SpreadsheetApp.getUi();
  var id = ui.prompt('AppFolio Reports API Client ID').getResponseText().trim();
  var secret = ui.prompt('AppFolio Reports API Client Secret').getResponseText().trim();
  if (!id || !secret) return ui.alert('Nothing saved.');
  PropertiesService.getScriptProperties().setProperties({ APPFOLIO_CLIENT_ID: id, APPFOLIO_CLIENT_SECRET: secret });
  ui.alert('Saved in Script Properties (not visible in the sheet).');
}

function fetchApi_(cfg, report) {
  if (!report) return null;
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('APPFOLIO_CLIENT_ID'), secret = props.getProperty('APPFOLIO_CLIENT_SECRET');
  if (!id || !secret) throw new Error('API mode needs keys: AppFolio Tools > Without the PC bridge > Save AppFolio API keys.');
  var url = 'https://' + cfg.subdomain + '.appfolio.com/api/v1/reports/' + report + '.json';
  var auth = { Authorization: 'Basic ' + Utilities.base64Encode(id + ':' + secret) };
  var all = [];
  for (var page = 0; url && page < 50; page++) {
    var res = UrlFetchApp.fetch(url, { headers: auth, muteHttpExceptions: true });
    if (res.getResponseCode() !== 200) throw new Error('AppFolio API ' + report + ' HTTP ' + res.getResponseCode() + ': ' + res.getContentText().slice(0, 200));
    var body = JSON.parse(res.getContentText());
    all = all.concat(Array.isArray(body) ? body : (body.results || []));
    url = body.next_page_url || null;
  }
  if (!all.length) return [[]];
  var headers = Object.keys(all[0]);
  return [headers].concat(all.map(function (r) { return headers.map(function (h) { return r[h] == null ? '' : r[h]; }); }));
}

function writeTable_(tabName, rows) {
  var ss = SpreadsheetApp.getActive();
  var sheet = ss.getSheetByName(tabName) || ss.insertSheet(tabName);
  sheet.clearContents();
  if (!rows || !rows.length || !rows[0].length) return;
  var width = rows[0].length;
  rows = rows.map(function (r) { r = r.slice(0, width); while (r.length < width) r.push(''); return r; });
  sheet.getRange(1, 1, rows.length, width).setValues(rows);
  sheet.getRange(1, 1, 1, width).setFontWeight('bold');
  sheet.setFrozenRows(1);
}

/** Read a tab as objects, tolerant of AppFolio's varying column titles. */
function table_(tabName) {
  var sheet = SpreadsheetApp.getActive().getSheetByName(tabName);
  if (!sheet || sheet.getLastRow() < 2) return [];
  var values = sheet.getDataRange().getValues();
  var headers = values[0].map(function (h) { return key_(h); });
  return values.slice(1).map(function (row) {
    var o = {}; headers.forEach(function (h, i) { o[h] = row[i]; }); return o;
  });
}

function key_(h) { return String(h).toLowerCase().replace(/[^a-z0-9]/g, ''); }

function pick_(obj, names) {
  for (var i = 0; i < names.length; i++) {
    var v = obj[key_(names[i])];
    if (v !== undefined && v !== '') return v;
  }
  return '';
}

// ================================================================ lease tracker rules

/** Close rows once AppFolio shows new lease dates; add renewals for leases ending soon. */
function updateTracker_(today) {
  var cfg = settings_();
  var sheet = ensureTab_(TAB.tracker, TRACKER_HEADERS, 1);
  var tenants = table_(TAB.tenants);
  var rows = trackerRows_(sheet);
  today = today || todayDate_();
  var byUnit = {};
  tenants.forEach(function (t) {
    var unit = normalize_(pick_(t, ['Unit', 'Property', 'Property Name', 'Address']));
    (byUnit[unit] = byUnit[unit] || []).push(t);
  });

  rows.forEach(function (r) {
    if (r[C.status] !== 'Out for signing' && r[C.status] !== 'Renewal due') return;
    // Rows from the PC bridge close when AppFolio's pending list drops them; date matching is for hand-typed rows.
    if (r[C.docId] && r[C.status] === 'Out for signing') return;
    (byUnit[normalize_(r[C.unit])] || []).forEach(function (t) {
      var leaseFrom = asDate_(pick_(t, ['Lease From', 'Lease Start', 'LeaseFrom']));
      var leaseTo = asDate_(pick_(t, ['Lease To', 'Lease End', 'LeaseTo', 'Lease Expiration']));
      var newStart = asDate_(r[C.newStart]), oldEnd = asDate_(r[C.oldEnd]);
      var signed = (newStart && leaseFrom && leaseFrom.getTime() >= newStart.getTime()) ||
                   (oldEnd && leaseTo && leaseTo.getTime() > oldEnd.getTime());
      if (signed && r[C.status] !== 'Signed') {
        r[C.status] = 'Signed'; r[C.signed] = today;
        r[C.notes] = appendNote_(r[C.notes], 'Auto-closed: AppFolio shows new lease dates');
      }
    });
  });

  var tracked = {};
  rows.forEach(function (r) { if (r[C.status] !== 'Signed' && r[C.status] !== 'Cancelled') tracked[normalize_(r[C.unit])] = true; });
  var horizon = new Date(today.getTime() + cfg.expiringDays * 86400000);
  tenants.forEach(function (t) {
    var status = String(pick_(t, ['Status', 'Tenant Status'])).toLowerCase();
    if (status && status !== 'current') return;
    var leaseTo = asDate_(pick_(t, ['Lease To', 'Lease End', 'LeaseTo', 'Lease Expiration']));
    var unitRaw = pick_(t, ['Unit', 'Property', 'Property Name', 'Address']);
    if (!leaseTo || leaseTo < today || leaseTo > horizon || tracked[normalize_(unitRaw)]) return;
    tracked[normalize_(unitRaw)] = true;
    var r = blankTrackerRow_();
    r[C.status] = 'Renewal due'; r[C.unit] = unitRaw;
    r[C.names] = pick_(t, ['Tenant', 'Name', 'Tenant Name']);
    r[C.emails] = pick_(t, ['Emails', 'Email', 'Email Address']);
    r[C.oldEnd] = leaseTo; r[C.notes] = 'Added automatically: lease ends soon';
    rows.push(r);
  });
  writeTrackerRows_(sheet, rows);
}

// ================================================================ reminders

function previewReminders() {
  var lines = runReminders_(false, settings_());
  SpreadsheetApp.getUi().alert(lines.join('\n') || 'Nothing is out for signing.');
  return lines;
}

function sendDueReminders() { return runReminders_(true, settings_()); }

/**
 * Sends YOU the reminder the selected Lease Tracker row would get today, written the same way
 * (Gemini or standard). Nothing goes to the tenant and the reminder count does not change.
 */
function sampleReminder() {
  var ui = SpreadsheetApp.getUi();
  var sheet = SpreadsheetApp.getActiveSheet();
  if (sheet.getName() !== TAB.tracker || sheet.getActiveRange().getRow() < 2) {
    ui.alert('Click a row in the Lease Tracker first.'); return;
  }
  var cfg = settings_();
  var rowIndex = sheet.getActiveRange().getRow();
  var r = sheet.getRange(rowIndex, 1, 1, TRACKER_HEADERS.length).getValues()[0];
  var today = todayDate_();
  var d = decideReminder_(r, cfg, today);
  var facts = factsOf_(r);
  var to = cfg.testEmail || Session.getActiveUser().getEmail() || alertTo_(cfg)[0];
  var recipients = d.emails || (facts ? unsignedEmails_(facts) : splitEmails_(r[C.emails]));
  if (!recipients.length) { ui.alert('No email to remind for this row: ' + d.reason); return; }
  var msg = buildReminder_(cfg, r, { number: d.number || Number(r[C.count] || 0) + 1, emails: recipients, facts: facts }, today);
  var note = '<p style="background:#e7f0fd;padding:8px;font-family:Arial,sans-serif">SAMPLE - this would go to: ' +
    esc_(recipients.join(', ')) + (cfg.staff.length ? ' (CC ' + esc_(cfg.staff.join(', ')) + ')' : '') +
    '<br>Today: ' + esc_(d.send ? 'reminder #' + d.number + ' is due' : 'not sent today (' + d.reason + ')') +
    '<br>Wording: ' + esc_(msg.by) + (msg.sms ? '<br>Text to send: ' + esc_(msg.sms) : '') + '</p>';
  var options = { htmlBody: note + msg.html, name: cfg.company || 'AppFolio Assistant' };
  if (cfg.replyTo) options.replyTo = cfg.replyTo;
  GmailApp.sendEmail(to, '[SAMPLE] ' + reminderSubject_(cfg, r),
    'This message is best viewed in an HTML email client.', options);
  ui.alert('Sample sent to ' + to + '.');
}

/**
 * Same rules, in the same order, as the PC bridge's own reminder logic:
 * not out for signing > max reached > no email > stale > too soon > contacted recently > due.
 */
function decideReminder_(r, cfg, today) {
  if (r[C.status] !== 'Out for signing') return { send: false, reason: 'status ' + r[C.status] };
  var facts = factsOf_(r);
  // When we know who has signed, only the people who haven't are emailed.
  var emails = facts ? unsignedEmails_(facts) : splitEmails_(r[C.emails]);
  var sentOn = asDate_(r[C.sent]);
  var count = Number(r[C.count] || 0);
  if (facts && facts.issue) return { send: false, reason: facts.issue };
  if (count >= cfg.max) return { send: false, reason: 'max reminders reached - call the tenant' };
  if (!emails.length) {
    return { send: false, reason: facts ? 'no email for ' + joinNames_(facts.toSign.map(function (t) { return t.first; })) +
      ' - text or call' : 'no tenant email' };
  }
  if (!sentOn) return { send: false, reason: 'fill in "Lease sent on"' };
  var age = days_(sentOn, today);
  if (cfg.stale && count === 0 && age > cfg.stale) return { send: false, reason: 'stale (' + age + ' days) - confirm or cancel in AppFolio' };
  if (age < cfg.firstAfter) return { send: false, reason: 'sent too recently' };
  var last = latest_([asDate_(r[C.last]), asDate_(r[C.resent])]);
  if (last && days_(last, today) < cfg.every) {
    var ago = days_(last, today);
    return { send: false, reason: ago === 0 ? 'reminded today' : 'contacted ' + ago + (ago === 1 ? ' day ago' : ' days ago') };
  }
  return { send: true, reason: 'due', number: count + 1, emails: emails, facts: facts };
}

function runReminders_(send, cfg, today) {
  var sheet = SpreadsheetApp.getActive().getSheetByName(TAB.tracker);
  if (!sheet || sheet.getLastRow() < 2) return [];
  var rows = trackerRows_(sheet);
  today = today || todayDate_();
  var allowed = send && (cfg.sendTenant || cfg.testEmail || cfg.forceTest);
  var lines = [];
  rows.forEach(function (r, i) {
    if (r[C.status] !== 'Out for signing') return;
    var d = decideReminder_(r, cfg, today);
    if (!d.send) { lines.push(r[C.unit] + ': skip (' + d.reason + ')'); return; }
    lines.push(r[C.unit] + ': ' + (allowed ? 'SENDING' : 'would send') + ' reminder #' + d.number + ' to ' + d.emails.join(', '));
    if (!allowed) return;
    try {
      var msg = buildReminder_(cfg, r, d, today);
      deliver_(cfg, d.emails, reminderSubject_(cfg, r), msg.html, cfg.staff);
      rows[i][C.count] = d.number; rows[i][C.last] = today;
      if (msg.sms) rows[i][C.text] = msg.sms;
      log_([new Date(), r[C.unit], d.emails.join(', '), d.number, isTest_(cfg) ? 'sent (TEST)' : 'sent',
            'Wording: ' + msg.by + (msg.sms ? ' | Text: ' + msg.sms : '')]);
    } catch (err) {
      log_([new Date(), r[C.unit], d.emails.join(', '), d.number, 'FAILED', String(err)]);
    }
  });
  // Test sends do not advance the real reminder count.
  if (allowed && !isTest_(cfg)) writeTrackerRows_(sheet, rows);
  return lines;
}

// ---------------------------------------------------------------- reminder wording

function factsOf_(r) {
  try {
    var f = r[C.facts] ? JSON.parse(r[C.facts]) : null;
    return f && f.known ? f : null;
  } catch (e) { return null; }
}

function unsignedEmails_(facts) {
  var out = [];
  facts.toSign.forEach(function (t) {
    (t.emails || []).forEach(function (e) { if (out.indexOf(e) < 0) out.push(e); });
  });
  return out;
}

function portalOff_(facts) {
  return !!facts && facts.toSign.some(function (t) { return /^no/i.test(t.portal || ''); });
}

/** What staff read in "Reminders go to": who still has to sign, their email and the number to text. */
function remindLabel_(facts) {
  if (facts.issue) return 'CHECK IN APPFOLIO: ' + facts.issue;
  if (!facts.toSign.length) return 'Everyone has signed';
  return facts.toSign.map(function (t) {
    return t.first + ': ' + ((t.emails || []).join(', ') || 'no email') + (t.phone ? ' | text ' + t.phone : '') +
      (/^no/i.test(t.portal || '') ? ' | portal not activated' : '');
  }).join('\n');
}

function joinNames_(names, lang) {
  names = (names || []).filter(String);
  return names.length <= 1 ? (names[0] || '') :
    names.slice(0, -1).join(', ') + (lang === 'es' ? ' y ' : ' and ') + names[names.length - 1];
}

function signOff_(cfg) { return (cfg.signedBy ? cfg.signedBy + ', ' : '') + (cfg.company || ''); }

/** 'es' when the row's Language is Spanish; otherwise 'en'. */
function langOf_(r) { return /^(span|esp)/i.test(String((r && r[C.lang]) || '')) ? 'es' : 'en'; }

function reminderSubject_(cfg, r) {
  return (langOf_(r) === 'es' ? cfg.reminderSubjectEs : cfg.reminderSubject).replace('{unit}', r[C.unit]);
}

/** Fixed wording around the message (email layout and the standard text), in each language. */
var WORDS_ = {
  en: {
    banner: 'Your lease is ready for your signature', status: 'Signing status', signed: '&#10003; Signed',
    waiting: 'Waiting for your signature', howTo: 'How to sign', button: 'Open my resident portal', thanks: 'Thank you,',
    footer: 'Questions or trouble signing? Just reply to this email.', leaseFor: 'Lease for ',
    stepsOff: ['Open the email from AppFolio inviting you to your resident portal, and activate your account.',
               'Sign in. A yellow notice on the home page says a document needs your signature.',
               'Open it, review the lease and sign. That\'s it.'],
    stepsOn: ['Sign in to your resident portal with the button below.',
              'A yellow notice on the home page says a document needs your signature.',
              'Open it, review the lease and sign. That\'s it.']
  },
  es: {
    banner: 'Su contrato está listo para firmar', status: 'Estado de las firmas', signed: '&#10003; Firmó',
    waiting: 'Falta su firma', howTo: 'Cómo firmar', button: 'Abrir mi portal de residente', thanks: 'Gracias,',
    footer: '¿Preguntas o problemas para firmar? Solo responda a este correo.', leaseFor: 'Contrato de ',
    stepsOff: ['Abra el correo de AppFolio que lo invita a su portal de residente y active su cuenta.',
               'Inicie sesión. Un aviso amarillo en la página principal dice que un documento necesita su firma.',
               'Ábralo, revise el contrato y fírmelo. Eso es todo.'],
    stepsOn: ['Entre a su portal de residente con el botón de abajo.',
              'Un aviso amarillo en la página principal dice que un documento necesita su firma.',
              'Ábralo, revise el contrato y fírmelo. Eso es todo.']
  }
};

/** The standard text message, in the style staff already use. */
function templateText_(cfg, unit, facts, lang) {
  if (!facts || facts.issue || !facts.toSign.length) return '';
  var who = joinNames_(facts.toSign.map(function (t) { return t.first; }), lang);
  var signed = joinNames_(facts.signed, lang);
  if (lang === 'es') {
    return 'Hola ' + who + ', por favor firme su parte del contrato de ' + unit + '.' +
      (signed ? ' ' + signed + (facts.signed.length > 1 ? ' ya firmaron.' : ' ya firmó.') : '') +
      (portalOff_(facts)
        ? ' Primero active su portal en línea desde la invitación de AppFolio y luego verá el contrato para firmar en la página principal.'
        : ' Entre a su portal en línea y verá un aviso de que hay un documento para firmar.') +
      ' ¡Gracias! - ' + signOff_(cfg);
  }
  return 'Hi ' + who + ', please sign your part of the lease for ' + unit + '.' +
    (signed ? ' ' + signed + (facts.signed.length > 1 ? ' have' : ' has') + ' already signed.' : '') +
    (portalOff_(facts)
      ? ' First activate your online portal from the AppFolio invite, then you\'ll see the lease to sign on the home page.'
      : ' Open your online portal and you\'ll see a notice that a document needs to be signed.') +
    ' Thank you! - ' + signOff_(cfg);
}

function templateParagraphs_(cfg, r, facts) {
  var lang = langOf_(r);
  var who = facts ? joinNames_(facts.toSign.map(function (t) { return t.first; }), lang)
                  : (String(r[C.names]).split(/[,&\n]/)[0] || (lang === 'es' ? '' : 'there'));
  var signed = facts ? joinNames_(facts.signed, lang) : '';
  var many = facts && facts.signed.length > 1;
  if (lang === 'es') {
    var es = ['Hola' + (who ? ' ' + who : '') + ',',
      'Su contrato de arrendamiento para ' + r[C.unit] + ' está listo y esperando su firma electrónica' +
        (r[C.sent] ? ' (enviado el ' + Utilities.formatDate(asDate_(r[C.sent]), Session.getScriptTimeZone(), 'd/M/yyyy') + ')' : '') + '.'];
    if (signed) es.push(signed + (many ? ' ya firmaron' : ' ya firmó') + ', así que solo falta su parte.');
    else if (!facts) es.push('Todas las personas en el contrato deben firmar. Si usted ya firmó, gracias; estamos esperando a los demás.');
    es.push('Solo toma unos minutos. Si algo le impide firmar, responda a este correo y le ayudamos.');
    return es;
  }
  var paras = ['Hi ' + who + ',',
    'Your lease for ' + r[C.unit] + ' is ready and waiting for your electronic signature' +
      (r[C.sent] ? ' (sent ' + fmt_(r[C.sent]) + ')' : '') + '.'];
  if (signed) paras.push(signed + (many ? ' have' : ' has') + ' already signed, so we only need your part to finish.');
  else if (!facts) paras.push('Every person on the lease needs to sign. If you already did, thank you - we are waiting on the other signers.');
  paras.push('It only takes a few minutes. If anything is holding you up, just reply to this email and we will help.');
  return paras;
}

/**
 * The tenant reminder email: a 600px card built from tables with inline styles, so Gmail, Outlook and
 * phone mail apps all show it the same way. paras is the message (Gemini's or the standard wording);
 * the signing status, the steps and the button come from the facts, never from the AI.
 */
function reminderEmailHtml_(cfg, paras, r, facts) {
  var navy = '#1f3864', ink = '#1f2933', muted = '#5f6b7a', line = '#e3e8ef', font = 'Arial,Helvetica,sans-serif';
  var td = function (style, html) { return '<tr><td style="' + style + '">' + html + '</td></tr>'; };
  var unit = r ? String(r[C.unit] || '') : '';
  var W = WORDS_[langOf_(r)];

  var status = '';
  if (facts && (facts.toSign.length || facts.signed.length)) {
    var rows = facts.signed.map(function (n) {
      return '<tr><td style="padding:6px 0;font:14px ' + font + ';color:' + ink + '">' + esc_(n) + '</td>' +
        '<td align="right" style="padding:6px 0;font:bold 13px ' + font + ';color:#2f7d32">' + W.signed + '</td></tr>';
    }).concat(facts.toSign.map(function (t) {
      return '<tr><td style="padding:6px 0;font:bold 14px ' + font + ';color:' + ink + '">' + esc_(t.first) + '</td>' +
        '<td align="right" style="padding:6px 0;font:bold 13px ' + font + ';color:#b45309">' + esc_(W.waiting) + '</td></tr>';
    }));
    status = td('padding:4px 32px 8px',
      '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid ' + line +
      ';border-radius:8px;background:#f8fafc"><tr><td style="padding:12px 16px">' +
      '<div style="font:bold 11px ' + font + ';letter-spacing:.06em;text-transform:uppercase;color:' + muted + ';padding-bottom:4px">' +
      esc_(W.status) + (unit ? ' &middot; ' + esc_(unit) : '') + '</div>' +
      '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">' + rows.join('') + '</table></td></tr></table>');
  }

  var steps = portalOff_(facts) ? W.stepsOff : W.stepsOn;
  var stepRows = steps.map(function (text, i) {
    return '<tr><td width="34" valign="top" style="padding:6px 0"><div style="width:24px;height:24px;line-height:24px;border-radius:12px;' +
      'background:' + navy + ';color:#fff;text-align:center;font:bold 12px ' + font + '">' + (i + 1) + '</div></td>' +
      '<td style="padding:8px 0 6px;font:14px/1.5 ' + font + ';color:' + ink + '">' + esc_(text) + '</td></tr>';
  }).join('');

  var button = cfg.portal ? td('padding:8px 32px 24px',
    '<table role="presentation" cellpadding="0" cellspacing="0"><tr><td bgcolor="' + navy + '" style="border-radius:6px">' +
    '<a href="' + esc_(cfg.portal) + '" style="display:inline-block;padding:13px 28px;font:bold 15px ' + font +
    ';color:#ffffff;text-decoration:none;border-radius:6px">' + esc_(W.button) + '</a></td></tr></table>') : '';

  return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#eef1f5" style="background:#eef1f5">' +
    '<tr><td align="center" style="padding:24px 12px">' +
    '<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:#ffffff;' +
    'border-radius:10px;overflow:hidden;border:1px solid ' + line + '">' +
    td('background:' + navy + ';padding:22px 32px',
      '<div style="font:bold 20px ' + font + ';color:#ffffff">' + esc_(cfg.company || 'Your lease') + '</div>' +
      '<div style="font:14px ' + font + ';color:#c9d3e6;padding-top:4px">' + esc_(W.banner) + '</div>') +
    td('padding:26px 32px 8px;font:15px/1.6 ' + font + ';color:' + ink,
      paras.map(function (p, i) { return '<p style="margin:0 0 14px' + (i === 0 ? ';font-weight:bold' : '') + '">' + esc_(p) + '</p>'; }).join('')) +
    status +
    td('padding:12px 32px 4px',
      '<div style="font:bold 11px ' + font + ';letter-spacing:.06em;text-transform:uppercase;color:' + muted + '">' + esc_(W.howTo) + '</div>' +
      '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">' + stepRows + '</table>') +
    button +
    td('padding:0 32px 26px;font:15px/1.6 ' + font + ';color:' + ink,
      esc_(W.thanks) + '<br><b>' + esc_(cfg.signedBy || cfg.company || '') + '</b>' + (cfg.signedBy && cfg.company ? '<br>' + esc_(cfg.company) : '')) +
    td('background:#f8fafc;border-top:1px solid ' + line + ';padding:14px 32px;font:12px/1.5 ' + font + ';color:' + muted,
      esc_(W.footer) + (unit ? '<br>' + esc_(W.leaseFor) + esc_(unit) : '')) +
    '</table></td></tr></table>';
}

/**
 * The reminder email and text for one lease. Gemini writes them when AI wording is on and a key is
 * saved; otherwise, or if its answer fails the checks in aiReminder_, the standard wording is used.
 */
function buildReminder_(cfg, r, d, today) {
  var facts = d.facts;
  var fallback = { html: reminderEmailHtml_(cfg, templateParagraphs_(cfg, r, facts), r, facts),
                   sms: facts ? templateText_(cfg, r[C.unit], facts, langOf_(r)) : '', by: 'standard' };
  if (!cfg.ai) return fallback;
  var key = PropertiesService.getScriptProperties().getProperty('GEMINI_API_KEY');
  if (!key) { fallback.by = 'standard (no Gemini key saved)'; return fallback; }
  if (aiDown_) { fallback.by = 'standard (Gemini unavailable this run: ' + aiDown_ + ')'; return fallback; }
  if (Date.now() - RUN_STARTED_ > AI_BUDGET_MS_) { fallback.by = 'standard (out of time for Gemini this run)'; return fallback; }
  try {
    var ai = aiReminder_(cfg, key, r, d, today);
    return { html: reminderEmailHtml_(cfg, ai.paragraphs, r, facts), sms: ai.sms, by: 'Gemini (' + ai.model + ')' };
  } catch (err) {
    var why = String(err.message || err).split(key).join('...').replace(/key=[^&\s)]+/g, 'key=...');
    // Overloaded, hung, or the key was refused: the next lease would fail the same way, so stop asking this run.
    if (err.unavailable || /timeout|timed out|HTTP 40[123]\b/i.test(why)) aiDown_ = why.slice(0, 80);
    fallback.by = 'standard (Gemini failed: ' + why.slice(0, 160) + ')';
    return fallback;
  }
}

function aiReminder_(cfg, key, r, d, today) {
  var facts = d.facts;
  var toSign = facts ? facts.toSign.map(function (t) { return t.first; }) : [String(r[C.names]).split(/[,&\n]/)[0].trim()];
  var sentOn = asDate_(r[C.sent]), lastOn = asDate_(r[C.last]);
  var spanish = langOf_(r) === 'es';
  var waited = sentOn ? days_(sentOn, today) : null;
  var brief = {
    company: cfg.company, signed_by: cfg.signedBy || '', property: String(r[C.unit]),
    language: spanish ? 'Spanish' : 'English',
    still_to_sign: toSign,
    already_signed: signedDetail_(r, facts, today),
    lease_sent_how_long_ago: waited === null ? 'unknown' : howLong_(waited),
    days_waiting: waited === null ? '' : waited,
    reminders_already_sent: Math.max(0, d.number - 1),
    days_since_last_reminder: lastOn ? days_(lastOn, today) : 'none sent yet',
    reminder_number: d.number, max_reminders: cfg.max,
    tone: toneFor_(d.number, cfg.max),
    portal_activated: facts ? !portalOff_(facts) : 'unknown'
  };
  var prompt = [
    'You write lease-signing reminders for a property manager, as if she typed them herself to this tenant.',
    'Her voice: short, warm and plain, first names, no corporate phrases. Examples of her own texts ' +
      '(match the tone, do NOT copy them):',
    '"Hi, please sign your part of the lease. Edith has already signed her part."',
    '"Hi, I see you were able to activate your online account. Thank you! Now go to the homepage of your portal and you will see a yellow notification that a document needs to be signed."',
    '"Do you need help signing the lease?"',
    '',
    'This tenant\'s real situation (use only these facts; never invent amounts, deadlines, fees or consequences):',
    JSON.stringify(brief),
    '',
    'Make it about THIS situation, so it does not read like a form letter:',
    '- Do not open with "please sign your part". Open with the situation: how long it has been waiting ' +
      '(say it naturally, e.g. "a few weeks now", "since the spring"), a check-in, or a question.',
    '- If someone in already_signed signed a while ago, say so naturally (you may use the month name they signed; never write numeric dates).',
    '- Follow the tone field: "first nudge" = light and friendly; "follow-up" = ask if anything is holding them up and offer help; ' +
      '"firm" = clearly say the lease still needs their signature and ask them to sign this week or reply; ' +
      '"last reminder" = say this is the last email reminder and that you will call them. Never threatening, no legal language.',
    '- If reminders_already_sent is more than 0, acknowledge you have reached out before.',
    '- Address only the people in still_to_sign, by first name.',
    '- The email already shows step-by-step signing instructions and a button below your text, so do not list the steps. ' +
      'If portal_activated is false, say in one sentence that activating the portal comes first.',
    '- The text message has no steps under it: there, say briefly how to sign (portal home page, or activate the portal first if portal_activated is false).',
    spanish
      ? '- Write BOTH the email and the text in Spanish (Latin American, using "usted"). The greeting is "Hola <name>,".'
      : '- Write in English.',
    '- No links, no numeric dates, no money amounts. Do not add a sign-off to the email; it is added automatically.',
    'Return JSON: {"email_paragraphs": [2 to 4 short paragraphs; the first is just the greeting, e.g. "' + (spanish ? 'Hola Ana,' : 'Hi Ana,') + '"], ' +
      '"sms": "one text message under 300 characters that ends with - ' + signOff_(cfg) + '"}'
  ].join('\n');
  var request = {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    headers: { 'x-goog-api-key': key },   // never in the URL: errors quote the URL
    payload: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }],
                              generationConfig: { responseMimeType: 'application/json', temperature: 1.0 } })
  };
  // Google answers 503/429/500 when a model is overloaded (gemini-3.8-flash does this constantly for many
  // free-tier keys, Nov 2026). Wait and retry, then try the fallback model before giving up.
  var models = [cfg.aiModel];
  if (AI_FALLBACK_MODEL !== cfg.aiModel) models.push(AI_FALLBACK_MODEL);
  var busy = [429, 500, 503], res, model, failures = [];
  for (var m = 0; m < models.length; m++) {
    model = models[m];
    var url = 'https://generativelanguage.googleapis.com/v1beta/models/' + encodeURIComponent(model) + ':generateContent';
    var waits = m === 0 ? [2000, 4000] : [2000];
    for (var attempt = 0; ; attempt++) {
      res = UrlFetchApp.fetch(url, request);
      var code = res.getResponseCode();
      if (code === 200 || attempt >= waits.length || busy.indexOf(code) < 0) break;
      Utilities.sleep(waits[attempt]);
    }
    if (res.getResponseCode() === 200) break;
    var why = '';
    try { why = String((JSON.parse(res.getContentText()).error || {}).message || ''); } catch (e) {}
    failures.push(model + ' HTTP ' + res.getResponseCode() + (why ? ' ' + why.replace(/key=[^&\s]+/g, 'key=...').slice(0, 100) : ''));
    if (busy.indexOf(res.getResponseCode()) < 0) break;   // a real error (bad key, bad model): don't try another
  }
  if (res.getResponseCode() !== 200) {
    var err = new Error(failures.join('; '));
    err.unavailable = busy.indexOf(res.getResponseCode()) >= 0;
    throw err;
  }
  var body = JSON.parse(res.getContentText());
  var out = JSON.parse(body.candidates[0].content.parts[0].text);
  var paras = (out.email_paragraphs || []).map(function (p) { return String(p).trim(); }).filter(String);
  var sms = String(out.sms || '').trim();
  // Checks before anything reaches a tenant: sensible size, no links, money or dates, and it greets the right people.
  var bad = /https?:|www\.|\$|\b\d{1,2}\/\d{1,2}(\/\d{2,4})?\b/i;
  if (paras.length < 2 || paras.length > 5 || paras.some(function (p) { return p.length > 700 || bad.test(p); })) {
    throw new Error('email did not pass checks');
  }
  if (!sms || sms.length > 400 || bad.test(sms)) throw new Error('text did not pass checks');
  // Every first name must appear. "Mark & Laurie" counts as Mark and Laurie, so "Hi Mark and Laurie" passes.
  var greeting = (paras[0] + ' ' + sms).toLowerCase();
  var names = [];
  toSign.forEach(function (n) {
    String(n || '').split(/\s*(?:&|,|\band\b|\by\b)\s*/i).forEach(function (part) { if (part.trim()) names.push(part.trim().toLowerCase()); });
  });
  if (!names.every(function (n) { return greeting.indexOf(n) >= 0; })) {
    throw new Error('did not address ' + toSign.join(' & '));
  }
  return { paragraphs: paras, sms: sms, model: model };
}

/** "about 3 weeks", "about 8 months": how long, in words Gemini can use without writing a date. */
function howLong_(days) {
  if (days < 2) return 'a day';
  if (days < 14) return days + ' days';
  if (days < 60) return 'about ' + Math.round(days / 7) + ' weeks';
  return 'about ' + Math.round(days / 30.4) + ' months';
}

/** How firm the reminder should be, from how many have gone out. */
function toneFor_(number, max) {
  if (number >= max) return 'last reminder';
  if (number <= 1) return 'first nudge';
  if (number <= 3) return 'follow-up';
  return 'firm';
}

/**
 * Who already signed and when, from "Signature status" ('Peter MacDonald III: signed 03/31/2026 · ...').
 * Gemini gets the month name and how long ago, never the numeric date.
 */
function signedDetail_(r, facts, today) {
  if (!facts || !facts.signed.length) return [];
  var months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  var when = {};
  String(r[C.signers] || '').split(/\s*·\s*/).forEach(function (part) {
    var m = part.match(/^(.+?):\s*signed (\d{1,2})\/(\d{1,2})\/(\d{4})/);
    if (m) when[m[1].trim().split(/\s+/)[0].toLowerCase()] = new Date(Number(m[4]), Number(m[2]) - 1, Number(m[3]));
  });
  return facts.signed.map(function (first) {
    var at = when[String(first).split(/\s+/)[0].toLowerCase()];
    return at ? { name: first, signed_in: months[at.getMonth()], how_long_ago: howLong_(Math.max(0, days_(at, today))) }
              : { name: first };
  });
}

// ================================================================ leasing summary

function sendSummary() { sendSummary_(settings_()); }

function sendSummary_(cfg, today) {
  var rows = trackerRows_();
  today = today || todayDate_();
  var out = rows.filter(function (r) { return r[C.status] === 'Out for signing'; });
  var counter = rows.filter(function (r) { return r[C.status] === 'Ready to countersign'; });
  var due = rows.filter(function (r) { return r[C.status] === 'Renewal due'; });
  var signedRecently = rows.filter(function (r) { return r[C.status] === 'Signed' && asDate_(r[C.signed]) && days_(asDate_(r[C.signed]), today) <= 7; });
  var vac = table_(TAB.vacancies);
  var apps = table_(TAB.applications);
  var kpiList = [['Waiting for signature', out.length], ['To countersign', counter.length], ['Renewals to start', due.length],
                 ['Signed this week', signedRecently.length], ['Vacant units', vac.length]];
  var extra = '';
  var html = header_('Leasing summary', cfg, today) +
    kpis_(kpiList) +
    section_('Waiting for tenant signature', ['Unit', 'Tenants', 'Sent', 'Days', 'Reminders', 'Next step'], out.map(function (r) {
      var age = r[C.sent] ? days_(asDate_(r[C.sent]), today) : '';
      var d = decideReminder_(r, cfg, today);
      var flag = age !== '' && cfg.stale && age > cfg.stale && !Number(r[C.count]) ? ' <b style="color:#854f0b">STALE - confirm or cancel</b>'
        : ((age !== '' && age >= cfg.escalate) || Number(r[C.count]) >= cfg.max ? ' <b style="color:#b42318">OVERDUE</b>' : '');
      return [esc_(r[C.unit]) + flag, esc_(r[C.signers] || r[C.names]), esc_(fmt_(r[C.sent])), esc_(age), esc_(r[C.count] || 0),
              esc_(d.send ? 'reminder #' + d.number + ' due' : d.reason)];
    }), true) +
    section_('Waiting for your countersignature', ['Unit', 'Tenants'], counter.map(function (r) { return [r[C.unit], r[C.names]]; })) +
    section_('Renewals to start (lease ending within ' + cfg.expiringDays + ' days)', ['Unit', 'Tenants', 'Lease ends'],
      due.map(function (r) { return [r[C.unit], r[C.names], fmt_(r[C.oldEnd])]; })) +
    section_('Signed in the last 7 days', ['Unit', 'Tenants', 'Signed'], signedRecently.map(function (r) { return [r[C.unit], r[C.names], fmt_(r[C.signed])]; })) +
    section_('Vacant units', ['Unit', 'Days vacant', 'Rent ready', 'Advertised rent'], vac.slice(0, 40).map(function (v) {
      return [pick_(v, ['Unit', 'Address', 'Property']), pick_(v, ['Days Vacant']), pick_(v, ['Rent Ready']), pick_(v, ['Advertised Rent', 'Rent'])];
    })) +
    section_('Open rental applications', ['Applicant', 'Unit', 'Received', 'Status'], apps.slice(0, 25).map(function (a) {
      return [pick_(a, ['Applicant', 'Applicants', 'Name']), pick_(a, ['Unit', 'Unit Name', 'Property']), fmt_(pick_(a, ['Received', 'Received On'])), pick_(a, ['Status'])];
    })) + extra + footer_('Leasing summary') + '</div></div>';
  var to = cfg.staff.length ? cfg.staff : alertTo_(cfg);
  deliver_(cfg, to, 'Leasing summary - ' + out.length + ' unsigned, ' + due.length + ' renewals to start (' +
    Utilities.formatDate(today, Session.getScriptTimeZone(), 'MMM d') + ')', html);
}


// ================================================================ alerts from the PC

function handleAlert_(p) {
  var cfg = settings_();
  var html = '<div style="font-family:Arial,sans-serif;color:#101828;max-width:680px">' +
    '<h2 style="margin:0 0 8px;color:#b42318">' + esc_(p.summary || 'Problem on the PC bridge') + '</h2>' +
    '<table role="presentation" style="border-collapse:collapse;width:100%;margin-bottom:12px">' +
    row_('Part', p.component) + row_('PC', p.host) + row_('When', p.generatedAt || new Date().toISOString()) + '</table>' +
    '<pre style="background:#f2f4f7;padding:12px;border-radius:6px;white-space:pre-wrap;font-size:12px">' + esc_(p.detail || '') + '</pre>' +
    '<p style="font-size:13px">Common fixes: if AppFolio\'s sign-in expired run <code>.\\scripts\\refresh_appfolio_session.ps1</code>; ' +
    'see the log in <code>work\\logs</code> on the PC. Docs: TROUBLESHOOTING in the README.</p>' + footer_('Bridge alert') + '</div>';
  deliver_(cfg, alertTo_(cfg), 'AppFolio Bridge alert: ' + (p.component || '') + ' - ' + (p.summary || ''), html);
  return { result: 'alert emailed' };
}


function promptGeminiKey() {
  var ui = SpreadsheetApp.getUi();
  var props = PropertiesService.getScriptProperties();
  var has = !!props.getProperty('GEMINI_API_KEY');
  var key = ui.prompt('Gemini API key',
    'Paste the key from aistudio.google.com/apikey (it starts with AIza). Gemini then writes the tenant ' +
    'reminder emails and texts' + (settings_().raw['Bill email search'] ? ' and pre-fills new bill emails' : '') + '.\n\n' +
    (has ? 'A key is already saved. Paste a new one to replace it, or leave blank and click OK to remove it.'
         : 'No key is saved yet.'), ui.ButtonSet.OK_CANCEL);
  if (key.getSelectedButton() !== ui.Button.OK) return;
  if (key.getResponseText().trim()) props.setProperty('GEMINI_API_KEY', key.getResponseText().trim());
  else props.deleteProperty('GEMINI_API_KEY');
  ui.alert(key.getResponseText().trim() ? 'Gemini key saved. Reminders will now use AI wording.' : 'Gemini key removed.');
}


// ================================================================ email

function isTest_(cfg) { return !!(cfg.testEmail || cfg.forceTest); }

function deliver_(cfg, to, subject, html, cc, attachments) {
  to = (to || []).filter(String); cc = (cc || []).filter(String);
  var options = { htmlBody: html, name: cfg.company || 'AppFolio Assistant' };
  if (cfg.replyTo) options.replyTo = cfg.replyTo;
  if (attachments && attachments.length) options.attachments = attachments;
  if (isTest_(cfg)) {
    var testTo = cfg.testEmail || alertTo_(cfg)[0];
    options.htmlBody = '<p style="background:#fff3cd;padding:8px;font-family:Arial,sans-serif">TEST MODE - would have gone to: ' +
      esc_(to.concat(cc).join(', ')) + '</p>' + html;
    GmailApp.sendEmail(testTo, '[TEST] ' + subject, 'This message is best viewed in an HTML email client.', options);
    return;
  }
  if (!to.length) throw new Error('No recipient for "' + subject + '" - fill in the Settings tab');
  if (cc.length) options.cc = cc.join(',');
  GmailApp.sendEmail(to.join(','), subject, 'This message is best viewed in an HTML email client.', options);
}

function header_(title, cfg, day) {
  return '<div style="font-family:Arial,sans-serif;font-size:14px;color:#1f2933;max-width:860px">' +
    '<div style="background:#1f3864;color:#fff;padding:14px 18px;border-radius:6px 6px 0 0"><div style="font-size:20px;font-weight:bold">' +
    esc_(title) + '</div><div style="font-size:12px;opacity:.85">' + esc_(cfg.company) + ' - ' +
    Utilities.formatDate(day || new Date(), Session.getScriptTimeZone(), 'EEEE, MMMM d, yyyy') + '</div></div><div style="padding:6px 4px">';
}

function footer_(what) {
  return '<p style="color:#8a96a3;font-size:12px;margin-top:22px">' + esc_(what) + ' - sent by the AppFolio Assistant (Google Sheet).</p>';
}

function kpis_(items) {
  return '<table role="presentation" style="border-collapse:separate;border-spacing:6px;margin:8px 0 10px"><tr>' + items.map(function (k) {
    return '<td style="background:#eaf3de;border-radius:6px;padding:8px 14px;text-align:center"><div style="font-size:20px;font-weight:bold;color:#1f3864">' +
      esc_(k[1]) + '</div><div style="font-size:12px;color:#61707d">' + esc_(k[0]) + '</div></td>';
  }).join('') + '</tr></table>';
}

/** Cells are plain text and escaped here; pass isHtml when the caller already built (escaped) HTML cells. */
function section_(title, headers, rows, isHtml) {
  var h = '<h3 style="color:#1f3864;margin:18px 0 6px">' + esc_(title) + ' (' + rows.length + ')</h3>';
  if (!rows.length) return h + '<p style="color:#61707d">None.</p>';
  return h + '<table style="border-collapse:collapse;width:100%"><tr>' + headers.map(function (x) {
    return '<th style="text-align:left;background:#eef2f6;padding:5px 8px;font-size:12px">' + esc_(x) + '</th>';
  }).join('') + '</tr>' + rows.map(function (r) {
    return '<tr>' + r.map(function (c) {
      return '<td style="padding:5px 8px;border-bottom:1px solid #eef2f6;vertical-align:top">' + (isHtml ? c : esc_(c)) + '</td>';
    }).join('') + '</tr>';
  }).join('') + '</table>';
}

function row_(label, value, raw) {
  if (value === undefined || value === null || value === '') return '';
  return '<tr><td style="padding:7px 10px;background:#f2f4f7;width:170px"><strong>' + esc_(label) +
    '</strong></td><td style="padding:7px 10px;border-bottom:1px solid #eaecf0">' + (raw ? value : esc_(value)) + '</td></tr>';
}

function linkOrText_(url) { return /^https:\/\/[^\s"<>]+$/.test(url || '') ? '<a href="' + esc_(url) + '" style="color:#155eef">' + esc_(url) + '</a>' : ''; }

// ================================================================ helpers

function log_(row) { (SpreadsheetApp.getActive().getSheetByName(TAB.log) || ensureTab_(TAB.log, ['When', 'Unit', 'To', 'Reminder #', 'Result', 'Detail'])).appendRow(row); }
function splitEmails_(v) { return String(v || '').split(/[,;\s]+/).filter(function (e) { return e.indexOf('@') > 0; }); }
function collapse_(v) { return String(v == null ? '' : v).replace(/\s+/g, ' ').trim(); }
/** "24 Hawthorn Ave, - First Floor" -> "24 hawthorn|first floor": house number + street word, plus the unit if there is one. */
function normalize_(v) {
  var words = function (s) {
    return String(s || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ')
      .replace(/\bavenue\b/g, 'ave').replace(/\bstreet\b/g, 'st').replace(/\broad\b/g, 'rd').replace(/\bdrive\b/g, 'dr')
      .replace(/\bcourt\b/g, 'ct').replace(/\blane\b/g, 'ln').split(/\s+/).filter(String);
  };
  var parts = String(v || '').split(/\s+-\s+/);
  var key = words(parts[0]).slice(0, 2).join(' ');
  var unit = words(parts.slice(1).join(' ')).join(' ');
  return unit ? key + '|' + unit : key;
}
function isDate_(v) { return Object.prototype.toString.call(v) === '[object Date]'; }
function todayDate_() { var d = new Date(); d.setHours(0, 0, 0, 0); return d; }
function asDate_(v) {
  if (!v) return null;
  if (isDate_(v)) return v;
  var m = String(v).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  var d = new Date(v); return isNaN(d.getTime()) ? null : d;
}
function latest_(dates) { return dates.filter(Boolean).sort(function (a, b) { return b - a; })[0] || null; }
function days_(a, b) { return Math.floor((b.getTime() - a.getTime()) / 86400000); }
function fmt_(v) { var d = asDate_(v); return d ? Utilities.formatDate(d, Session.getScriptTimeZone(), 'MMM d, yyyy') : ''; }
function money_(v) { var s = String(v == null ? '0' : v); var neg = /^\(.*\)$/.test(s.trim()); var n = Number(s.replace(/[$,()\s]/g, '')) || 0; return neg ? -n : n; }
function usd_(n) { n = Number(n || 0); return (n < 0 ? '-$' : '$') + Math.abs(n).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
function esc_(v) {
  return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function amountCents_(v) {
  var text = typeof v === 'number' ? String(v) : String(v == null ? '' : v).replace(/[$,\s]/g, '');
  var m = text.match(/^(\d+)(?:\.(\d{1,2}))?$/);
  if (!m) return null;
  var cents = Number(m[1]) * 100 + Number(((m[2] || '') + '00').slice(0, 2));
  return cents > 0 && cents <= 10000000000 ? cents : null;
}

function isoDate_(v) {
  if (isDate_(v)) return isNaN(v.getTime()) ? '' : Utilities.formatDate(v, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  var text = collapse_(v), y, mo, d, m;
  if ((m = text.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/))) { y = +m[1]; mo = +m[2]; d = +m[3]; }
  else if ((m = text.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4}|\d{2})$/))) { mo = +m[1]; d = +m[2]; y = +m[3]; if (y < 100) y += y < 69 ? 2000 : 1900; }
  else return '';
  var date = new Date(Date.UTC(y, mo - 1, d));
  if (date.getUTCFullYear() !== y || date.getUTCMonth() !== mo - 1 || date.getUTCDate() !== d) return '';
  return ('000' + y).slice(-4) + '-' + ('0' + mo).slice(-2) + '-' + ('0' + d).slice(-2);
}

function addDaysIso_(iso, n) {
  var p = iso.split('-');
  var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2] + Number(n || 0)));
  return d.toISOString().slice(0, 10);
}

/** JSON with sorted keys and no spaces - the same bytes as Python json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False). */
function canonicalJson_(obj) {
  return '{' + Object.keys(obj).sort().map(function (k) { return JSON.stringify(k) + ':' + JSON.stringify(obj[k]); }).join(',') + '}';
}

function hex_(bytes) {
  return bytes.map(function (b) { return ('0' + ((b + 256) % 256).toString(16)).slice(-2); }).join('');
}

function sha256Hex_(text) {
  return hex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8));
}

function hmacHex_(text, key) {
  return hex_(Utilities.computeHmacSha256Signature(text, key, Utilities.Charset.UTF_8));
}
