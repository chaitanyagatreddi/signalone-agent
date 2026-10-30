// SignalOne → Google Sheet. Paste into the Sheet: Extensions → Apps Script.
// Triggers (set once with setupTriggers): refresh all brands daily at 1 AM; write the Sheet every 10 minutes.
// The 10-minute call also keeps the free Render app awake, so its saved reviews aren't wiped by sleeping.
const API = 'https://signalone-agent.onrender.com';

// Starts a fresh fetch + scoring of all 4 brands on the agent (runs in the background there).
function startRefresh() {
  const res = UrlFetchApp.fetch(API + '/refresh', { method: 'post', muteHttpExceptions: true });
  Logger.log('refresh: ' + res.getResponseCode() + ' ' + res.getContentText().slice(0, 300));
}

// Merges the agent's saved reviews into the "Reviews" tab. Never deletes rows: the Sheet is the
// permanent record, because the free Render app loses its own copy on every redeploy.
function writeSheet() {
  const res = UrlFetchApp.fetch(API + '/sheet-rows', { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) throw new Error('sheet-rows failed: ' + res.getResponseCode() + ' ' + res.getContentText().slice(0, 300));
  const data = JSON.parse(res.getContentText());
  const n = data.columns.length;
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName('Reviews') || ss.insertSheet('Reviews');
  const key = (r) => r[0] + '|' + r[1] + '|' + r[5]; // brand | source | text
  const last = sheet.getLastRow();
  const existing = last > 1 ? sheet.getRange(2, 1, last - 1, n).getValues() : [];
  const index = new Map(existing.map((r, i) => [key(r), i]));
  let added = 0, updated = 0;
  data.rows.forEach((r) => {
    if (index.has(key(r))) { existing[index.get(key(r))] = r; updated++; }
    else { index.set(key(r), existing.length); existing.push(r); added++; }
  });
  sheet.getRange(1, 1, 1, n).setValues([data.columns]).setFontWeight('bold');
  if (existing.length) sheet.getRange(2, 1, existing.length, n).setValues(existing);
  sheet.getRange(1, n + 2).setValue('updated_at: ' + data.updated_at);
  Logger.log('added ' + added + ', updated ' + updated + ', total ' + existing.length + ' rows');
}

// Run once by hand. Creates the two daily triggers; after this nobody needs to touch it.
function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('startRefresh').timeBased().everyDays(1).atHour(1).create();
  ScriptApp.newTrigger('writeSheet').timeBased().everyMinutes(10).create();
}
