// SignalOne → Google Sheet. Paste into the Sheet: Extensions → Apps Script.
// Triggers (set once with setupTriggers): refresh all brands daily at 1 AM; write the Sheet every 10 minutes.
// The 10-minute call also keeps the free Render app awake, so its saved reviews aren't wiped by sleeping.
const API = 'PASTE_YOUR_RENDER_URL_HERE'; // e.g. https://signalone-agent.onrender.com

// Starts a fresh fetch + scoring of all 4 brands on the agent (runs in the background there).
function startRefresh() {
  const res = UrlFetchApp.fetch(API + '/refresh', { method: 'post', muteHttpExceptions: true });
  Logger.log('refresh: ' + res.getResponseCode() + ' ' + res.getContentText().slice(0, 300));
}

// Copies every saved review into the "Reviews" tab and stamps the update time.
function writeSheet() {
  const res = UrlFetchApp.fetch(API + '/sheet-rows', { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) throw new Error('sheet-rows failed: ' + res.getResponseCode() + ' ' + res.getContentText().slice(0, 300));
  const data = JSON.parse(res.getContentText());
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName('Reviews') || ss.insertSheet('Reviews');
  sheet.clear();
  sheet.getRange(1, 1, 1, data.columns.length).setValues([data.columns]).setFontWeight('bold');
  if (data.rows.length) sheet.getRange(2, 1, data.rows.length, data.columns.length).setValues(data.rows);
  sheet.getRange(1, data.columns.length + 2).setValue('updated_at: ' + data.updated_at);
  Logger.log('wrote ' + data.rows.length + ' rows');
}

// Run once by hand. Creates the two daily triggers; after this nobody needs to touch it.
function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('startRefresh').timeBased().everyDays(1).atHour(1).create();
  ScriptApp.newTrigger('writeSheet').timeBased().everyMinutes(10).create();
}
