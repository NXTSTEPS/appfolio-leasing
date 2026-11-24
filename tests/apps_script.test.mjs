// Runs apps-script/Code.gs against in-memory fakes of the Google services.
//   node tests/apps_script.test.mjs                 # run the checks
//   node tests/apps_script.test.mjs --emit out.json # also write a signed bill approval
//                                                   # (tests/test_apps_script_contract.py
//                                                   #  validates it with the bridge's Python code)
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import vm from 'node:vm';

// ---------------------------------------------------------------- fakes
class FakeRange {
  constructor(sheet, row, col, rows, cols) { Object.assign(this, { sheet, row, col, rows, cols }); }
  getValues() {
    const out = [];
    for (let r = 0; r < this.rows; r++) {
      const line = [];
      for (let c = 0; c < this.cols; c++) line.push(this.sheet.data[this.row - 1 + r]?.[this.col - 1 + c] ?? '');
      out.push(line);
    }
    return out;
  }
  setValues(values) {
    values.forEach((line, r) => line.forEach((v, c) => {
      const rr = this.row - 1 + r;
      this.sheet.data[rr] = this.sheet.data[rr] || [];
      this.sheet.data[rr][this.col - 1 + c] = v;
    }));
    return this;
  }
  getRow() { return this.row; }
  setFontWeight() { return this; }
  setDataValidation() { return this; }
}
class FakeSheet {
  constructor(name, data = []) { this.name = name; this.data = data; this.activeRow = 2; this.maxCols = 26; }
  getMaxColumns() { return this.maxCols; }
  insertColumnsAfter(after, n) { assert.equal(after, this.maxCols); this.maxCols += n; }
  getName() { return this.name; }
  getLastRow() { while (this.data.length && this.data[this.data.length - 1].every((v) => v === '' || v == null)) this.data.pop(); return this.data.length; }
  getRange(r, c, rows = 1, cols = 1) {
    if (c + cols - 1 > this.maxCols) throw new Error('The coordinates of the range are outside the dimensions of the sheet.');
    return new FakeRange(this, r, c, rows, cols);
  }
  getDataRange() { return new FakeRange(this, 1, 1, this.data.length, Math.max(...this.data.map((d) => d.length))); }
  getActiveRange() { return new FakeRange(this, this.activeRow, 1, 1, 1); }
  appendRow(row) { this.getLastRow(); this.data.push(row.slice()); }
  clearContents() { this.data = []; }
  deleteRows(start, n) { this.data.splice(start - 1, n); }
  setFrozenRows() {} setColumnWidth() {} hideColumns() {}
}
const sheets = {};
let activeSheet = null;
const ss = {
  getSheetByName: (n) => sheets[n] || null,
  insertSheet: (n) => (sheets[n] = new FakeSheet(n)),
  getName: () => 'Test Sheet', getUrl: () => 'https://docs.google.com/spreadsheets/d/test',
};
const sent = [];
const geminiCalls = [];
const sleeps = [];
let geminiReply = () => ({ getResponseCode: () => 500, getContentText: () => '' });
const geminiJson = (obj) => () => ({ getResponseCode: () => 200,
  getContentText: () => JSON.stringify({ candidates: [{ content: { parts: [{ text: JSON.stringify(obj) }] } }] }) });
const allSent = [];
const props = {};
let currentUser = 'owner@acme.test';
let uiAnswer = 'YES';
const validation = () => { const v = { requireValueInList: () => v, requireCheckbox: () => v, requireValueInRange: () => v, setAllowInvalid: () => v, build: () => ({}) }; return v; };
const signed = (buf) => Array.from(buf).map((b) => (b > 127 ? b - 256 : b));
const pad = (n) => String(n).padStart(2, '0');
const ctx = {
  SpreadsheetApp: {
    getActive: () => ss, getActiveSheet: () => activeSheet, newDataValidation: validation,
    getUi: () => ({ alert: () => uiAnswer, ButtonSet: { YES_NO: 'YES_NO', OK_CANCEL: 'OK_CANCEL' }, Button: { YES: 'YES', NO: 'NO', OK: 'OK' } }),
  },
  GmailApp: { sendEmail: (to, subject, body, opts) => { sent.push({ to, subject, opts }); allSent.push({ to, subject, opts }); } },
  Utilities: {
    formatDate: (d, tz, fmt) => fmt === 'yyyy-MM-dd' ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` : d.toDateString(),
    parseCsv: () => [],
    sleep: (ms) => { sleeps.push(ms); },
    DigestAlgorithm: { SHA_256: 'sha256' }, Charset: { UTF_8: 'utf8' },
    computeDigest: (alg, text) => signed(crypto.createHash('sha256').update(text, 'utf8').digest()),
    computeHmacSha256Signature: (text, key) => signed(crypto.createHmac('sha256', Buffer.from(key, 'utf8')).update(text, 'utf8').digest()),
    base64EncodeWebSafe: (s) => Buffer.from(s, 'utf8').toString('base64url'),
    base64Decode: (s) => Array.from(Buffer.from(s, 'base64')),
    newBlob: (bytes, type, name) => ({ bytes, type, name }),
    getUuid: () => crypto.randomUUID(),
  },
  Session: { getScriptTimeZone: () => 'America/New_York', getActiveUser: () => ({ getEmail: () => currentUser }), getEffectiveUser: () => ({ getEmail: () => 'owner@acme.test' }) },
  PropertiesService: { getScriptProperties: () => ({
    getProperty: (k) => (k in props ? props[k] : null), setProperty: (k, v) => { props[k] = String(v); },
    deleteProperty: (k) => { delete props[k]; }, setProperties: (o) => Object.assign(props, o) }) },
  ContentService: { MimeType: { JSON: 'json' }, createTextOutput: (s) => ({ body: s, setMimeType() { return this; } }) },
  LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock() {} }) },
  Logger: { log() {} },
  UrlFetchApp: { fetch: (url, opts) => { geminiCalls.push({ url, opts }); return geminiReply(url, opts); } },
  console,
};
vm.createContext(ctx);
vm.runInContext(readFileSync(new URL('../apps-script/Code.gs', import.meta.url), 'utf8'), ctx);

const day = (offset) => { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() + offset); return d; };
const iso = (offset) => ctx.Utilities.formatDate(day(offset), '', 'yyyy-MM-dd');
const TOKEN = 'x'.repeat(40);
const post = (action, payload, deliveryId = '') =>
  JSON.parse(ctx.doPost({ postData: { contents: JSON.stringify({ token: TOKEN, action, deliveryId, payload }) } }).body);

// ---------------------------------------------------------------- setup + settings
ctx.setup();
const settings = sheets['Settings'];
const setSetting = (k, v) => { const row = settings.data.find((r) => r[0] === k); assert.ok(row, 'setting ' + k); row[1] = v; };
setSetting('Company name', 'Acme PM');
setSetting('Staff emails', 'staff@acme.test');
setSetting('Alert emails', 'alerts@acme.test');
assert.equal(sheets['Settings'].data.find((r) => r[0] === 'Reply-to email')[1], 'owner@acme.test', 'owner pre-filled');

// ---------------------------------------------------------------- connection
assert.equal(JSON.parse(ctx.doPost({ postData: { contents: '{}' } }).body).ok, false, 'refuses before connect');
props.BRIDGE_TOKEN = TOKEN;
props.BILL_HMAC_KEY = 'k'.repeat(64);
assert.match(JSON.parse(ctx.doPost({ postData: { contents: JSON.stringify({ token: 'bad', action: 'ping' }) } }).body).error, /Wrong connection key/);
const ping = post('ping', { projectId: 'proj_123', host: 'PC-1' });
assert.equal(ping.ok, true);
assert.equal(props.BRIDGE_PROJECT_ID, 'proj_123');
assert.ok(props.LAST_BRIDGE_CONTACT, 'contact time recorded');
assert.equal(post('nope', {}).ok, false, 'unknown action refused');

// ---------------------------------------------------------------- leasing from the bridge
const tracker = sheets['Lease Tracker'];
tracker.data.push(['Out for signing', '5 Elm Avenue', 'Bo Ray', '', day(-10), '', '', 0, '', '', 'typed by staff']);
const leasingPayload = (leases, run) => ({
  today: iso(0),
  leases,
  tenants: [['Unit', 'Tenant', 'Emails', 'Status', 'Lease From', 'Lease To'],
            ['12 Oak Street', 'Lee, Ann', 'ann@x.test', 'Current', iso(-300), iso(30)]],
  vacancies: [['Unit', 'Days Vacant', 'Rent Ready', 'Advertised Rent', 'Next Move In'], ['9 Pine Rd', 12, 'Yes', 1500, '']],
  applications: null,
  run,
});
const leases = [
  { documentId: '101', status: 'Out for signing', unit: '5 Elm Ave', names: 'Bo Ray', emails: 'bo@x.test', sentOn: iso(-10), lastResentOn: '', oldLeaseEnd: iso(5) },
  { documentId: '102', status: 'Out for signing', unit: '7 Birch Ln', names: 'Di Fox', emails: 'di@x.test', sentOn: iso(-1), lastResentOn: '', oldLeaseEnd: '' },
  { documentId: '103', status: 'Out for signing', unit: '3 Old Rd', names: 'Al Old', emails: 'al@x.test', sentOn: iso(-90), lastResentOn: '', oldLeaseEnd: '' },
  { documentId: '104', status: 'Out for signing', unit: '8 Ash Ct', names: 'Ed Fu', emails: 'ed@x.test', sentOn: iso(-9), lastResentOn: iso(-1), oldLeaseEnd: '' },
  { documentId: '105', status: 'Ready to countersign', unit: '2 Fir Pl', names: 'Cy Kim', emails: 'cy@x.test', sentOn: iso(-4), lastResentOn: '', oldLeaseEnd: '' },
];
let res = post('leasing.sync', leasingPayload(leases, { reminders: true, summary: true }), 'leasing:1');
assert.equal(res.ok, true, res.error);
const rowFor = (unit) => tracker.data.slice(1).find((r) => r[1] === unit);
assert.equal(rowFor('5 Elm Avenue')[12], '101', 'hand-typed row adopted the AppFolio lease ID');
assert.equal(rowFor('5 Elm Avenue')[3], 'bo@x.test', 'emails filled from AppFolio');
assert.equal(rowFor('2 Fir Pl')[0], 'Ready to countersign');
assert.equal(rowFor('12 Oak Street')[0], 'Renewal due', 'lease ending soon added');
// Send tenant reminders is NO: nothing to tenants, summary to staff only.
assert.equal(sent.filter((m) => /Reminder/.test(m.subject)).length, 0);
assert.equal(sent.length, 1);
assert.match(sent[0].subject, /^Leasing summary - 4 unsigned/);
assert.equal(sent[0].to, 'staff@acme.test');
assert.match(sent[0].opts.htmlBody, /STALE - confirm or cancel/);
assert.match(sent[0].opts.htmlBody, /To countersign/);

// Duplicate delivery ID: nothing happens twice.
assert.equal(post('leasing.sync', leasingPayload(leases, { reminders: true, summary: true }), 'leasing:1').duplicate, true);
assert.equal(sent.length, 1);

// Reminders on: only 5 Elm (10 days, never contacted). 7 Birch too new, 3 Old stale, 8 Ash resent yesterday.
setSetting('Send tenant reminders', 'YES');
sent.length = 0;
res = post('leasing.sync', leasingPayload(leases, { reminders: true, summary: false }), 'leasing:2');
assert.deepEqual(sent.map((m) => m.to), ['bo@x.test']);
assert.equal(sent[0].opts.cc, 'staff@acme.test');
assert.equal(sent[0].opts.replyTo, 'owner@acme.test');
assert.equal(rowFor('5 Elm Avenue')[7], 1, 'reminder count advanced');
assert.ok(res.lines.some((l) => /3 Old Rd: skip \(stale/.test(l)), res.lines.join('\n'));
assert.ok(res.lines.some((l) => /8 Ash Ct: skip \(contacted 1 day ago/.test(l)));
// Same day again: interval blocks it.
sent.length = 0;
post('leasing.sync', leasingPayload(leases, { reminders: true, summary: false }), 'leasing:3');
assert.equal(sent.length, 0);

// Test run from the PC (-Test): goes to the alert address, counts do not move.
sent.length = 0;
const later = leasingPayload(leases, { reminders: true, summary: true, test: true });
later.today = iso(4);
post('leasing.sync', later, 'leasing:4');
assert.ok(sent.length >= 2 && sent.every((m) => m.to === 'alerts@acme.test' && /^\[TEST\]/.test(m.subject)), JSON.stringify(sent.map((m) => [m.to, m.subject])));
assert.equal(rowFor('5 Elm Avenue')[7], 1, 'test run does not advance the count');

// Lease gone from AppFolio's list -> closed.
post('leasing.sync', leasingPayload(leases.filter((l) => l.documentId !== '102'), {}), 'leasing:5');
assert.equal(rowFor('7 Birch Ln')[0], 'Signed');


// ---------------------------------------------------------------- only the tenant who hasn't signed
const factsPartly = { known: true, issue: '', signed: ['Philip'],
  toSign: [{ name: 'Hannah E. Whitlock', first: 'Hannah', emails: ['hannah@x.test'], phone: '(732) 555-0202', portal: 'No', matched: true }] };
const partly = [{ documentId: '201', status: 'Out for signing', unit: '1253 Juniper Street', names: 'Philip Sample, Hannah E. Whitlock',
  emails: 'philip@x.test, hannah@x.test', signers: 'Hannah E. Whitlock: not signed · Philip Sample: signed 03/31/2026',
  facts: factsPartly, sentOn: iso(-10), lastResentOn: '', oldLeaseEnd: '' }];
setSetting('Reminders signed by', 'Dana');
sent.length = 0;
res = post('leasing.sync', leasingPayload(partly, { reminders: true, summary: false }), 'leasing:partly1');
assert.equal(res.ok, true, res.error);
assert.deepEqual(sent.map((m) => m.to), ['hannah@x.test'], 'only the unsigned tenant is emailed');
assert.match(sent[0].opts.htmlBody, /Hi Hannah,/);
assert.match(sent[0].opts.htmlBody, /Philip has already signed/);
assert.match(sent[0].opts.htmlBody, /inviting you to your resident portal, and activate your account/, 'portal not activated -> activation step');
assert.match(sent[0].opts.htmlBody, /Thank you,<br><b>Dana<\/b><br>Acme PM/);
assert.equal(geminiCalls.length, 0, 'no key saved -> no Gemini call');
assert.match(rowFor('1253 Juniper Street')[14], /^Hannah: hannah@x\.test \| text \(732\) 555-0202 \| portal not activated$/);
assert.match(rowFor('1253 Juniper Street')[15], /^Hi Hannah, please sign your part of the lease for 1253 Juniper Street\. Philip has already signed\./);
assert.match(sheets['Reminder Log'].data.at(-1)[5], /Wording: standard \(no Gemini key saved\)/);

// Gemini writes it when a key is saved; its text lands in "Text to send".
props.GEMINI_API_KEY = 'test-key';
geminiReply = geminiJson({ email_paragraphs: ['Hi Hannah,', 'Philip already signed, so we just need you.', 'Activate your portal first, then sign from the home page.'],
  sms: 'Hi Hannah! Philip already signed the lease. Can you activate your portal and sign? - Dana, Acme PM' });
// Google is busy (503) on the first try: the script waits and retries instead of falling back.
{ const good = geminiReply; let tries = 0;
  geminiReply = (...args) => (++tries === 1 ? { getResponseCode: () => 503, getContentText: () => '' } : good(...args)); }
const later2 = leasingPayload(partly, { reminders: true, summary: false }); later2.today = iso(4);
sent.length = 0;
post('leasing.sync', later2, 'leasing:partly2');
assert.equal(sent.length, 1);
assert.match(sent[0].opts.htmlBody, /Philip already signed, so we just need you\./);
assert.match(geminiCalls.at(-1).url, /models\/gemini-3\.5-flash:generateContent$/);
assert.equal(geminiCalls.at(-1).opts.headers['x-goog-api-key'], 'test-key', 'key goes in a header, not the URL');
assert.match(rowFor('1253 Juniper Street')[15], /^Hi Hannah! Philip already signed/);
assert.match(sheets['Reminder Log'].data.at(-1)[5], /Wording: Gemini \(gemini-3\.5-flash\)/);
assert.deepEqual(sleeps, [2000], 'one wait, then the retry worked');

// The chosen model stays overloaded (503 on every try): the fallback model writes it instead.
{ const good = geminiJson({ email_paragraphs: ['Hi Hannah,', 'Philip signed already; only your part is left.'],
    sms: 'Hi Hannah, Philip already signed. Please sign from your portal home page. - Dana' });
  geminiReply = (url, ...rest) => (/gemini-3\.5-flash:/.test(url)
    ? { getResponseCode: () => 503, getContentText: () => JSON.stringify({ error: { message: 'This model is currently experiencing high demand.' } }) }
    : good(url, ...rest)); }
geminiCalls.length = 0; sleeps.length = 0;
const later2b = leasingPayload(partly, { reminders: true, summary: false }); later2b.today = iso(8);
sent.length = 0;
post('leasing.sync', later2b, 'leasing:partly2b');
assert.match(sent[0].opts.htmlBody, /only your part is left/);
assert.equal(geminiCalls.length, 4, '3 tries on the chosen model, then the fallback answers');
assert.match(geminiCalls.at(-1).url, /models\/gemini-3\.5-flash-lite:generateContent$/);
assert.deepEqual(sleeps, [2000, 4000]);
assert.match(sheets['Reminder Log'].data.at(-1)[5], /Wording: Gemini \(gemini-3\.5-flash-lite\)/);

// Both models overloaded: standard wording, the log says why, and the rest of the run skips Gemini.
geminiReply = () => ({ getResponseCode: () => 503, getContentText: () => '{"error":{"message":"high demand"}}' });
const later2c = leasingPayload(partly, { reminders: true, summary: false }); later2c.today = iso(12);
sent.length = 0;
post('leasing.sync', later2c, 'leasing:partly2c');
assert.match(sent[0].opts.htmlBody, /Hi Hannah,/);
assert.match(sheets['Reminder Log'].data.at(-1)[5], /Gemini failed: gemini-3\.5-flash HTTP 503 high demand; gemini-3\.5-flash-lite HTTP 503/);
assert.match(ctx.aiDown_, /HTTP 503/, 'Gemini is marked down for the rest of this run');
const callsBefore = geminiCalls.length;
assert.match(ctx.buildReminder_(ctx.settings_(), rowFor('1253 Juniper Street'), { number: 2, facts: null }, new Date()).by,
  /Gemini unavailable this run/);
assert.equal(geminiCalls.length, callsBefore, 'no further Gemini calls in this run');

// A hung call (UrlFetchApp throws a timeout) is reported without the key and also stops Gemini for the run.
ctx.aiDown_ = '';
geminiReply = () => { throw new Error('Timeout: https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?key=test-key'); };
const hung = ctx.buildReminder_(ctx.settings_(), rowFor('1253 Juniper Street'), { number: 2, facts: null }, new Date());
assert.match(hung.by, /Gemini failed: Timeout/);
assert.doesNotMatch(hung.by, /test-key/, 'the key never reaches the log');
assert.ok(ctx.aiDown_);
ctx.aiDown_ = '';

// Past the time budget (the bridge waits 330s, Apps Script stops at 360s), Gemini isn't asked at all.
ctx.RUN_STARTED_ = Date.now() - 201000;
assert.match(ctx.buildReminder_(ctx.settings_(), rowFor('1253 Juniper Street'), { number: 2, facts: null }, new Date()).by,
  /out of time for Gemini/);
ctx.RUN_STARTED_ = Date.now();

// Gemini gets the real situation: how long it has waited, when Philip signed, how many reminders went out, the tone.
{
  let prompt = '';
  geminiReply = (url, opts) => { prompt = JSON.parse(opts.payload).contents[0].parts[0].text;
    return geminiJson({ email_paragraphs: ['Hi Noah and Robin,', 'It has been a few weeks now.'],
      sms: 'Hi Noah and Robin, checking in on the lease. - Dana' })(); };
  const row = rowFor('1253 Juniper Street').slice();
  const facts = { known: true, issue: '', signed: ['Philip'], toSign: [{ name: 'Noah & Robin Calloway', first: 'Noah & Robin', emails: ['m@x.test'], portal: 'yes' }] };
  row[ctx.C.facts] = JSON.stringify(facts);
  const out = ctx.buildReminder_(ctx.settings_(), row, { number: 3, emails: ['m@x.test'], facts }, day(40));
  assert.match(out.by, /^Gemini/, '"Noah and Robin" counts as addressing "Noah & Robin"');
  assert.match(prompt, /"signed_in":"March"/);
  assert.match(prompt, /"reminders_already_sent":2/);
  assert.match(prompt, /"tone":"follow-up"/);
  assert.match(prompt, /"lease_sent_how_long_ago":"/);
  assert.doesNotMatch(prompt, /03\/31\/2026/, 'Gemini never gets numeric dates');
  assert.match(prompt, /Write in English/);
}
assert.equal(ctx.toneFor_(1, 6), 'first nudge');
assert.equal(ctx.toneFor_(4, 6), 'firm');
assert.equal(ctx.toneFor_(6, 6), 'last reminder');
assert.equal(ctx.howLong_(250), 'about 8 months');

// Language = Spanish: the subject, layout, standard wording and Gemini's instructions are all Spanish.
{
  let prompt = '';
  geminiReply = (url, opts) => { prompt = JSON.parse(opts.payload).contents[0].parts[0].text;
    return geminiJson({ email_paragraphs: ['Hola Hannah,', 'Ya pasaron unas semanas desde que le enviamos el contrato.'],
      sms: 'Hola Hannah, Philip ya firmó. Puede firmar desde su portal. - Dana' })(); };
  const row = rowFor('1253 Juniper Street').slice();
  row[ctx.C.lang] = 'Spanish';
  const facts = JSON.parse(row[ctx.C.facts]);
  const cfg = ctx.settings_();
  assert.match(ctx.reminderSubject_(cfg, row), /^Recordatorio: su contrato de arrendamiento de 1253 Juniper Street/);
  const ai = ctx.buildReminder_(cfg, row, { number: 2, emails: ['hannah@x.test'], facts }, day(20));
  assert.match(prompt, /Write BOTH the email and the text in Spanish/);
  assert.match(ai.html, /Estado de las firmas/);
  assert.match(ai.html, /Cómo firmar/);
  assert.match(ai.html, /Ya pasaron unas semanas/);
  ctx.aiDown_ = 'test';   // standard wording path
  const std = ctx.buildReminder_(cfg, row, { number: 2, emails: ['hannah@x.test'], facts }, day(20));
  assert.match(std.html, /Hola Hannah,/);
  assert.match(std.html, /Philip ya firmó, así que solo falta su parte/);
  assert.match(std.sms, /^Hola Hannah, por favor firme su parte del contrato de 1253 Juniper Street\. Philip ya firmó\./);
  ctx.aiDown_ = '';
  assert.equal(ctx.langOf_(row), 'es');
  row[ctx.C.lang] = '';
  assert.equal(ctx.langOf_(row), 'en', 'blank means English');
}
// A Gemini answer with a link (or addressed to the wrong person) is thrown away for the standard wording.
geminiReply = geminiJson({ email_paragraphs: ['Hi Philip,', 'Sign at https://evil.test now.'], sms: 'Hi Philip - Dana' });
const later3 = leasingPayload(partly, { reminders: true, summary: false }); later3.today = iso(16);
sent.length = 0;
post('leasing.sync', later3, 'leasing:partly3');
assert.match(sent[0].opts.htmlBody, /Hi Hannah,/);
assert.doesNotMatch(sent[0].opts.htmlBody, /evil/);
assert.match(sheets['Reminder Log'].data.at(-1)[5], /Gemini failed: email did not pass checks/);
delete props.GEMINI_API_KEY;

// The same tenant shows as signed and unsigned: no reminder, the tracker says to check AppFolio.
const mixed = [{ ...partly[0], documentId: '202', unit: '4 Quiet Street',
  facts: { ...factsPartly, issue: 'the same tenant shows as signed and not signed - check who signed in AppFolio' } }];
sent.length = 0;
res = post('leasing.sync', leasingPayload(mixed, { reminders: true, summary: false }), 'leasing:mixed');
assert.equal(sent.length, 0);
assert.match(rowFor('4 Quiet Street')[14], /^CHECK IN APPFOLIO/);
assert.ok(res.lines.some((l) => /4 Quiet Street: skip \(the same tenant/.test(l)), res.lines.join('\n'));
// Sample for the selected row: goes to whoever asked, never the tenant, count unchanged.
activeSheet = tracker;
tracker.activeRow = tracker.data.findIndex((r) => r[1] === '1253 Juniper Street') + 1;
const countBefore = rowFor('1253 Juniper Street')[7];
sent.length = 0;
ctx.sampleReminder();
assert.equal(sent.length, 1);
assert.equal(sent[0].to, 'owner@acme.test');
assert.match(sent[0].subject, /^\[SAMPLE\]/);
assert.match(sent[0].opts.htmlBody, /this would go to: hannah@x\.test/);
assert.equal(rowFor('1253 Juniper Street')[7], countBefore, 'sample does not count');
setSetting('Reminders signed by', '');

// Multi-unit building: a new lease for Upstairs must not close First Floor's hand-typed row.
assert.equal(ctx.normalize_('24 Hawthorn Ave, - First Floor'), '24 hawthorn|first floor');
assert.equal(ctx.normalize_('24 Hawthorn Avenue'), '24 hawthorn');
tracker.data.push(['Out for signing', '24 Hawthorn Ave, - First Floor', 'Navarro', 'g@x.test', day(-5), '', day(-30), 0, '', '', '']);
sheets['Data: Tenants'] = new FakeSheet('Data: Tenants', [['Unit', 'Tenant', 'Emails', 'Status', 'Lease From', 'Lease To'],
  ['24 Hawthorn Ave, - Upstairs', 'Castillo', 'v@x.test', 'Current', day(-1), day(364)]]);
ctx.updateTracker_(day(0));
assert.equal(rowFor('24 Hawthorn Ave, - First Floor')[0], 'Out for signing', 'another unit in the building did not close this row');


// Test mode email redirects everything.
setSetting('Test mode email', 'me@acme.test');
sent.length = 0;
ctx.sendSummary();
assert.equal(sent[0].to, 'me@acme.test');
assert.match(sent[0].subject, /^\[TEST\]/);

// Watchdog: quiet PC -> one alert per day.
setSetting('Test mode email', '');
props.LAST_BRIDGE_CONTACT = new Date(Date.now() - 30 * 3600000).toISOString();
sent.length = 0;
ctx.watchdog(); ctx.watchdog();
assert.equal(sent.length, 1);
assert.match(sent[0].subject, /has not checked in/);

const dump = process.argv.indexOf('--dump-emails');
if (dump > 0) {
  mkdirSync(process.argv[dump + 1], { recursive: true });
  allSent.forEach((m, i) => writeFileSync(`${process.argv[dump + 1]}/${String(i).padStart(2, '0')}.html`,
    `<!-- to: ${m.to} | subject: ${m.subject} -->
` + m.opts.htmlBody));
}
console.log('apps-script tests passed');

// A Lease Tracker tab narrower than the headers (an older install made it 17 columns wide) is widened, not an error.
{
  const narrow = sheets['Lease Tracker'];
  narrow.maxCols = 17;
  ctx.ensureTab_('Lease Tracker', ctx.TRACKER_HEADERS, 1);
  assert.equal(narrow.getMaxColumns(), ctx.TRACKER_HEADERS.length);
  assert.equal(narrow.data[0][ctx.C.lang], 'Language');
}
console.log('narrow-tab test passed');
