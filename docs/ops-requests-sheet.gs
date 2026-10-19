/**
 * Ops special requests from the Telegram bot, written into the "Special Req"
 * tab the same way the Paperform does, so both can be used at once.
 *
 * New request  -> a new row, columns A-G:
 *   A Person Submitted · B Timestamp · C PO # · D Category · E Description
 *   · F Delivery Date · G Delivery Time
 * Ops answer in Telegram -> that row's H checkbox (Ops tick) is ticked, and the
 *   reason or note is added to J (Remarks). Anything already in J is kept.
 *   Paperform rows are found by PO # and Category (the bot never adds those).
 * Ops tick H in the sheet -> the bot marks that request done in Telegram too
 *   (needs the trigger in step 5).
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
 *   5. Ticks back to the bot: Script Properties > add BOT_URL = the bot's App
 *      address (its PUBLIC_URL, e.g. https://xxx.up.railway.app). Then
 *      Triggers (clock icon) > Add Trigger > function onTickEdit, event
 *      source From spreadsheet, event type On edit > Save, and allow access.
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
  const row = body.timestamp
    ? findRow(sheet, body.row, body.po, body.timestamp)
    : findByCategory(sheet, body.po, body.category);
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

// A Paperform row: the newest with this PO # and Category.
function findByCategory(sheet, po, category) {
  const last = lastFilledRow(sheet);
  if (last < 2) return 0;
  const data = sheet.getRange(2, COL_PO, last - 1, 2).getDisplayValues();   // C and D
  for (let i = data.length - 1; i >= 0; i--) {
    if (String(data[i][0]) === String(po)
        && String(data[i][1]).toLowerCase() === String(category).toLowerCase()) return i + 2;
  }
  return 0;
}

// Installable "On edit" trigger (setup step 5): when someone ticks H, tell the bot.
function onTickEdit(e) {
  const range = e && e.range;
  if (!range || range.getSheet().getName() !== SHEET_NAME) return;
  if (range.getColumn() > COL_TICK || range.getColumn() + range.getNumColumns() - 1 < COL_TICK) return;
  const props = PropertiesService.getScriptProperties();
  const url = props.getProperty('BOT_URL'), secret = props.getProperty('SECRET');
  if (!url || !secret) return;
  const sheet = range.getSheet();
  for (let r = range.getRow(); r < range.getRow() + range.getNumRows(); r++) {
    if (r < 2 || sheet.getRange(r, COL_TICK).getValue() !== true) continue;
    const row = sheet.getRange(r, 1, 1, 4).getDisplayValues()[0];   // A-D
    UrlFetchApp.fetch(url.replace(/\/$/, '') + '/api/ops/sheet-tick', {
      method: 'post', contentType: 'application/json', muteHttpExceptions: true,
      payload: JSON.stringify({ secret: secret, timestamp: row[1], po: row[2], category: row[3] }),
    });
  }
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
