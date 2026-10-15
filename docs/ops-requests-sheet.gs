/**
 * Ops special requests: the bot sends each request here, and this adds a row
 * to the "Requests" tab (or updates it when Ops answer).
 *
 * Setup (once), in the Ops Google Sheet:
 *   1. Extensions > Apps Script, paste this file, Save.
 *   2. Project Settings > Script Properties > Add property:
 *        SECRET = a long random password (the same value goes in the bot's
 *        REQUEST_SHEET_SECRET setting on Railway; never paste it in a chat).
 *   3. Deploy > New deployment > Web app.
 *        Execute as: Me.  Who has access: Anyone.
 *      (Anyone can reach the address, but nothing is written without SECRET.)
 *   4. Copy the Web app URL into the bot's REQUEST_SHEET_URL setting on Railway.
 */

const SHEET_NAME = 'Requests';
const COLUMNS = [
  ['id', 'Request #'],
  ['submitted_at', 'Submitted at'],
  ['submitted_by', 'Submitted by'],
  ['team', 'Team'],
  ['order_no', 'Order number'],
  ['category', 'Request'],
  ['details', 'Details'],
  ['delivery_date', 'Delivery date'],
  ['timeframe', 'Time frame'],
  ['after_cutoff', 'After cut-off'],
  ['status', 'Status'],
  ['handled_by', 'Handled by'],
  ['handled_at', 'Handled at'],
  ['reason', "Can't do reason"],
];

function doPost(e) {
  const secret = PropertiesService.getScriptProperties().getProperty('SECRET');
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply({ ok: false, error: 'bad json' });
  }
  if (!secret || body.secret !== secret) {
    return reply({ ok: false, error: 'not allowed' });
  }
  const row = body.row || {};
  if (!row.id) return reply({ ok: false, error: 'no id' });

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sheet = getSheet();
    const values = COLUMNS.map(([key]) => (row[key] === undefined ? '' : String(row[key])));
    // Find this request's row by its number in column A; add a new line if it's new.
    const last = sheet.getLastRow();
    let at = 0;
    if (last > 1) {
      const ids = sheet.getRange(2, 1, last - 1, 1).getValues();
      for (let i = 0; i < ids.length; i++) {
        if (String(ids[i][0]) === String(row.id)) { at = i + 2; break; }
      }
    }
    if (at) {
      sheet.getRange(at, 1, 1, values.length).setValues([values]);
    } else {
      sheet.appendRow(values);
    }
    return reply({ ok: true });
  } finally {
    lock.releaseLock();
  }
}

function getSheet() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = book.getSheetByName(SHEET_NAME);
  if (!sheet) sheet = book.insertSheet(SHEET_NAME);
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(COLUMNS.map(([, title]) => title));
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, COLUMNS.length).setFontWeight('bold');
    // Keep order numbers and dates as typed, not turned into numbers.
    sheet.getRange('A:N').setNumberFormat('@');
  }
  return sheet;
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
