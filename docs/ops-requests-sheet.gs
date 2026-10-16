/**
 * Ops special requests from the Telegram bot, written into the "Special Req"
 * tab the same way the Paperform does, so both can be used at once.
 *
 * New request  -> a new row, columns A-G:
 *   A Person Submitted · B Timestamp · C PO # · D Category · E Description
 *   · F Delivery Date · G Delivery Time
 * Ops answer in Telegram -> that row's H checkbox (Ops tick) is ticked, and the
 *   reason or note is added to J (Remarks). Anything already in J is kept.
 * Columns I (SO #), K (Pending?) and L (Appears in Cancellation?) are never
 * touched, and nothing outside this tab is read or changed.
 *
 * Setup (once), by someone with edit access to the Ops sheet:
 *   1. Extensions > Apps Script, paste this file, Save.
 *   2. Project Settings > Script Properties > Add property:
 *        SECRET = a long random password. The same value goes in the bot's
 *        REQUEST_SHEET_SECRET setting on Railway. Never paste it in a chat.
 *   3. Deploy > New deployment > Web app.
 *        Execute as: Me.  Who has access: Anyone.
 *      (Anyone can reach the address, but nothing is written without SECRET.)
 *   4. Copy the Web app URL into the bot's REQUEST_SHEET_URL setting on Railway.
 */

const SHEET_NAME = 'Special Req';
const COL_PERSON = 1, COL_TIMESTAMP = 2, COL_PO = 3, COL_TICK = 8, COL_REMARKS = 10;

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
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  if (!sheet) return reply({ ok: false, error: 'no tab called ' + SHEET_NAME });

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    if (body.action === 'add') return reply(addRow(sheet, body.values));
    if (body.action === 'update') return reply(updateRow(sheet, body));
    return reply({ ok: false, error: 'unknown action' });
  } finally {
    lock.releaseLock();
  }
}

function addRow(sheet, values) {
  if (!Array.isArray(values) || values.length !== 7) return { ok: false, error: 'need 7 values' };
  const row = lastFilledRow(sheet) + 1;
  const range = sheet.getRange(row, 1, 1, 7);
  range.setNumberFormat('@');            // keep PO #, dates and times exactly as sent
  range.setValues([values.map(String)]);
  return { ok: true, row: row };
}

function updateRow(sheet, body) {
  const row = findRow(sheet, body.row, body.po, body.timestamp);
  if (!row) return { ok: false, error: 'row not found' };
  if (body.ticked) {
    const tick = sheet.getRange(row, COL_TICK);
    if (tick.isChecked() !== null) tick.check();   // a checkbox column
    else if (!tick.getValue()) tick.setValue('Done');
  }
  if (body.remarks) {
    const cell = sheet.getRange(row, COL_REMARKS);
    const now = String(cell.getValue() || '');
    if (now.indexOf(body.remarks) === -1) cell.setValue(now ? now + ' | ' + body.remarks : body.remarks);
  }
  return { ok: true };
}

// The bot remembers each request's row, but rows can move (sorting, deleting),
// so it's checked against the PO # and timestamp, and searched for if it moved.
function findRow(sheet, hint, po, timestamp) {
  const matches = r => String(sheet.getRange(r, COL_PO).getDisplayValue()) === String(po)
    && String(sheet.getRange(r, COL_TIMESTAMP).getDisplayValue()) === String(timestamp);
  const last = lastFilledRow(sheet);
  if (hint && hint <= last && matches(hint)) return hint;
  if (last < 2) return 0;
  const data = sheet.getRange(2, COL_TIMESTAMP, last - 1, 2).getDisplayValues();
  for (let i = data.length - 1; i >= 0; i--) {
    if (String(data[i][0]) === String(timestamp) && String(data[i][1]) === String(po)) return i + 2;
  }
  return 0;
}

// The last row with something in column A or B: checkboxes further down
// don't count as filled.
function lastFilledRow(sheet) {
  const last = sheet.getLastRow();
  if (last < 1) return 0;
  const ab = sheet.getRange(1, COL_PERSON, last, 2).getDisplayValues();
  for (let i = ab.length - 1; i >= 0; i--) {
    if (ab[i][0] !== '' || ab[i][1] !== '') return i + 1;
  }
  return 0;
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
