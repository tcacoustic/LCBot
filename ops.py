"""Ops: special requests from SH, Livechat, ON and FS (change a delivery's date,
address or product, stop it, or anything else).

Each request posts in its country's Ops chat (or topic). One Ops person takes
it (🙋 I'm on it), then marks it Done or Can't do, and can send the sender a
short note; the sender hears each step by DM. Every request is saved here and
copied to the Ops Google Sheet. Requests after the 2pm cut-off still go
through, tagged, and the ones still open are posted again the next working
morning.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from functools import lru_cache

import holidays

from core import *

REQUEST_COUNTRIES = [("SG", "🇸🇬 Singapore"), ("MY", "🇲🇾 Malaysia"),
                     ("HK", "🇭🇰 Hong Kong"), ("TH", "🇹🇭 Thailand")]
REQUEST_COUNTRY_NAMES = dict(REQUEST_COUNTRIES)
REQUEST_CATEGORIES = [
    ("DATE", "Change delivery date/time"),
    ("ADDRESS", "Change delivery address"),
    ("PRODUCT", "Change product/colour"),
    ("STOP", "Stop delivery"),
    ("OTHER", "Others"),
]
CATEGORY_NAMES = dict(REQUEST_CATEGORIES)
TIMEFRAMES = [("ANY", "Anytime"), ("AM", "10am–2pm"), ("PM", "2pm–6pm"), ("OTHER", "Other")]
TIMEFRAME_NAMES = dict(TIMEFRAMES)
# Shown as buttons when Ops can't do a request, with "Other" for a typed
# reason underneath. Keep the buttons to 4 or fewer. A typed reason is saved as
# it is, so CANT_NAMES.get(reason, reason) shows either kind.
CANT_REASONS = [
    ("SHIPPED", "Already out for delivery"),
    ("INFO", "Need more information"),
]
CANT_NAMES = {**dict(CANT_REASONS), "NOTPOSSIBLE": "Not possible"}   # NOTPOSSIBLE: earlier requests
STATUS_NAMES = {"OPEN": "Open", "TAKEN": "In progress", "DONE": "Done", "CANT": "Can't do"}
LIVE = ("OPEN", "TAKEN")   # still waiting on Ops

# Conversation states: /request, and the note Ops write to the sender
(R_ORDER, R_CAT, R_DETAILS, R_DATE, R_DATE_TYPED, R_TIME, R_TIME_TYPED, R_CONFIRM,
 R_COUNTRY, SR_NOTE, SR_REASON) = range(410, 421)
DATE_CHOICES = 8   # today and the next 7 days as buttons; anything else is typed


SENDER_TEAMS = {"SH", "LIVECHAT", "ON", "FS"}   # who can send Ops special requests


def may_request(user_id: int) -> bool:
    return bool(depts_of(user_id) & SENDER_TEAMS) or admin_for(user_id, *SENDER_TEAMS)


def is_ops(user_id: int) -> bool:
    return "OPS" in depts_of(user_id) or admin_for(user_id, "OPS")


def ops_target(country: str = "") -> dict | None:
    """Where a country's requests go: its own chat or topic, else the shared one."""
    for key in ([f"ops_requests_chat:{country}"] if country else []) + ["ops_requests_chat"]:
        raw = setting(key, "")
        if raw:
            return json.loads(raw)
    return None


# Busy days: a country's requests can go to Ops in rounds (every 30 or 60
# minutes) instead of one message each. Same-day and 🚩 ones never wait.
ROUND_LENGTHS = (30, 60)


def rounds_for(country: str) -> int:
    """Minutes between rounds for this country, or 0 when they're off."""
    raw = setting(f"ops_rounds:{country}", "")
    return int(raw) if raw.isdigit() and int(raw) in ROUND_LENGTHS else 0


def next_round(country: str, when: datetime | None = None) -> datetime | None:
    """When the next round for this country goes out (on the hour, or :30)."""
    every = rounds_for(country)
    if not every:
        return None
    when = when or now()
    start = when.replace(minute=0, second=0, microsecond=0)
    while start <= when:
        start += timedelta(minutes=every)
    return start


def round_time(t: datetime) -> str:
    return t.strftime("%-I:%M%p").lower()


# Public holidays: no courier deliveries and no Ops, so they're skipped like
# weekends. Built-in yearly lists (Malaysia: national holidays only).
@lru_cache(maxsize=32)
def _holidays(country: str, year: int) -> dict:
    try:
        return dict(holidays.country_holidays(country, years=year, language="en_US"))
    except Exception:
        return {}


def holiday_name(country: str, d: date) -> str:
    return _holidays(country, d.year).get(d, "") if country else ""


def is_working_day(country: str, d: date) -> bool:
    return d.weekday() < 5 and not holiday_name(country, d)


def next_working(country: str, d: date) -> date:
    """The next Mon-Fri after d that isn't a public holiday there."""
    d += timedelta(days=1)
    while not is_working_day(country, d):
        d += timedelta(days=1)
    return d


def past_cutoff(when: datetime, country: str = "") -> bool:
    """At or after today's cut-off, or a day off (the cut-off was the last working day's)."""
    return (not is_working_day(country, when.date())
            or when.hour * 60 + when.minute >= parse_time_token(REQUEST_CUTOFF))


def cutoff_kind(when: datetime, delivery: date, country: str = "") -> str:
    """'URGENT' for a delivery today, 'LATE' for one on the next working day (or a
    day off before it) asked for after the cut-off, '' otherwise. The cut-off is
    for next-day deliveries: anything further out is on time whenever it's sent."""
    if delivery <= when.date():
        return "URGENT"
    if past_cutoff(when, country) and delivery <= next_working(country, when.date()):
        return "LATE"
    return ""



def clock_text(token: str) -> str:
    m = parse_time_token(token)
    t = datetime(2000, 1, 1, m // 60, m % 60)
    return (t.strftime("%-I%p") if m % 60 == 0 else t.strftime("%-I:%M%p")).lower()


def cutoff_text() -> str:
    return clock_text(REQUEST_CUTOFF)


def team_of(user_id: int) -> str:
    names = [DEPT_NAMES[k] for k, _ in DEPARTMENTS if k in depts_of(user_id) and k in SENDER_TEAMS]
    return " + ".join(names) or ("Admin" if is_admin(user_id) else "")


def timeframe_text(r) -> str:
    if r["timeframe"] == "OTHER":
        return r["timeframe_other"] or "Other"
    return TIMEFRAME_NAMES.get(r["timeframe"], r["timeframe"])


def stamp(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%a %-d %b, %-I:%M%p") if iso else ""


def request_text(r, head: str = "") -> str:
    """The request as Ops see it in their chat (HTML)."""
    d = date.fromisoformat(r["delivery_date"])
    lines = [head] if head else []
    tag = "🔴 <b>URGENT · SAME DAY</b> · " if r["urgent"] else "⏰ <b>AFTER CUT-OFF</b> · " if r["after_cutoff"] else ""
    if is_flagged(r["id"]):
        tag = "🚩 <b>ESCALATION</b> · " + tag
    lines += [
        f"{tag}📦 <b>Special request #{r['id']}</b> · {esc(REQUEST_COUNTRY_NAMES.get(r['country'] or '', ''))}",
        f"<b>{esc(CATEGORY_NAMES[r['category']])}</b>",
        "",
        f"Order: <code>{esc(r['order_no'])}</code>",
        f"Delivery: {date_label(d)} · {esc(timeframe_text(r))}",
        "",
        esc(r["details"]),
        "",
        f"<i>From {esc(sender_name(r))}"
        f"{' (' + esc(team_of(r['created_by'])) + ')' if r['created_by'] and team_of(r['created_by']) else ''}"
        f"{' · via Paperform' if r['source'] == 'paperform' else ''}"
        f" · {stamp(r['created_at'])}</i>",
    ]
    if r["status"] == "TAKEN":
        lines += ["", f"🙋 <b>{esc(display_name_of(r['taken_by'], 'Ops'))} is on it</b> · {stamp(r['taken_at'])}"]
    elif r["status"] == "DONE":
        by = "ticked in the Ops sheet" if r["handled_by"] == 0 else "by " + display_name_of(r["handled_by"], "Ops")
        lines += ["", f"✅ <b>Done</b> {esc(by)} · {stamp(r['handled_at'])}"]
    elif r["status"] == "CANT":
        lines += ["", f"🚫 <b>Can't do</b>: {esc(CANT_NAMES.get(r['reason'], r['reason'] or ''))}"
                  f" · {esc(display_name_of(r['handled_by'], 'Ops'))} · {stamp(r['handled_at'])}"]
    if r["ops_note"]:
        lines.append(f"📝 <i>{esc(r['ops_note'])}</i>")
    return "\n".join(lines)


def ops_buttons(r, bot_username: str = "") -> InlineKeyboardMarkup | None:
    rid, rows = r["id"], []
    if r["status"] == "OPEN":
        rows.append([InlineKeyboardButton("🙋 I'm on it", callback_data=f"sr:tk:{rid}")])
    if r["status"] in LIVE:
        rows.append([InlineKeyboardButton("✅ Done", callback_data=f"sr:ok:{rid}"),
                     InlineKeyboardButton("🚫 Can't do", callback_data=f"sr:no:{rid}")])
    if bot_username:
        # Notes are written in a private chat with the bot, then sent to the sender.
        rows.append([InlineKeyboardButton("📝 Note to sender",
                                          url=f"https://t.me/{bot_username}?start=srn_{rid}")])
    return InlineKeyboardMarkup(rows) if rows else None


def cant_buttons(rid: int, bot_username: str = "") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(name, callback_data=f"sr:nr:{rid}:{key}")] for key, name in CANT_REASONS]
    if bot_username:
        # Typed in a private chat with the bot (a group button can't take text).
        rows.append([InlineKeyboardButton("✏️ Other reason (type it)",
                                          url=f"https://t.me/{bot_username}?start=srx_{rid}")])
    rows.append([InlineKeyboardButton("‹ Back", callback_data=f"sr:bk:{rid}")])
    return InlineKeyboardMarkup(rows)


def get_request(rid: int):
    return q1("SELECT * FROM special_requests WHERE id=?", (rid,))


# 🚩 Escalation: shown to Ops in Telegram and the App only, never in the
# sheet. Kept as one line per request in the settings table (red_flag:<id>).
def is_flagged(rid: int) -> bool:
    return setting(f"red_flag:{rid}", "") == "yes"


def in_ops_chat(r) -> bool:
    """Which requests Ops see in Telegram: after the cut-off, same-day or 🚩.
    The rest (on time) go only to the Special Req sheet; Ops work them there."""
    return bool(r["after_cutoff"] or r["urgent"] or is_flagged(r["id"]))


def flagged_first(rows: list) -> list:
    """The same order, with 🚩 requests moved to the top."""
    return sorted(rows, key=lambda r: not is_flagged(r["id"]))


# --------------------------------------------------------------------------
# Google Sheet: one row per request, kept up to date by request number
# --------------------------------------------------------------------------

# The Ops team's "Special Req" tab, filled the same way their Paperform does:
# A person · B timestamp (yyyy-mm-dd hh:mm:ss) · C PO # · D category · E description · F delivery date
# · G delivery time. H (Ops tick) and J (remarks) are filled when Ops answer
# here; I, K and L stay Ops' own.
SHEET_CATEGORY = {
    "DATE": "Change Delivery Date/Time",
    "ADDRESS": "Change Delivery Address",
    "PRODUCT": "Change Product/Colour",
    "STOP": "Stop Delivery",
    "OTHER": "Others",
}
SHEET_TIME = {"ANY": "Anytime", "AM": "10am-2pm", "PM": "2pm-6pm"}


def sender_name(r) -> str:
    return r["submitted_name"] or display_name_of(r["created_by"], "someone")


def sheet_values(r) -> list:
    """Columns A to G, as Paperform writes them."""
    return [
        sender_name(r),
        datetime.fromisoformat(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S"),
        r["order_no"],
        SHEET_CATEGORY[r["category"]],
        r["details"],
        date.fromisoformat(r["delivery_date"]).strftime("%d-%b-%Y"),
        SHEET_TIME.get(r["timeframe"]) or (r["timeframe_other"] or "Other"),
    ]


def sheet_remarks(r) -> str:
    """What the bot adds to Remarks (J) once Ops answer."""
    bits = []
    if r["status"] == "CANT":
        bits.append("Can't do: " + CANT_NAMES.get(r["reason"], r["reason"] or ""))
    if r["ops_note"]:
        bits.append("Ops note: " + r["ops_note"])
    return " · ".join(bits)


def sheet_payload(r) -> dict:
    """A new row for the bot's own requests; Paperform already wrote its row,
    so those are only ever updated (found by PO # and category)."""
    if not r["sheet_row"] and r["source"] != "paperform":
        return {"secret": REQUEST_SHEET_SECRET, "action": "add", "values": sheet_values(r)}
    return {"secret": REQUEST_SHEET_SECRET, "action": "update", "row": r["sheet_row"] or 0,
            "po": r["order_no"], "timestamp": "" if r["source"] == "paperform" else sheet_values(r)[1],
            "category": SHEET_CATEGORY[r["category"]],
            "ticked": r["status"] in ("DONE", "CANT"), "remarks": sheet_remarks(r)}


def _post_to_sheet(payload: dict) -> dict:
    req = urllib.request.Request(
        REQUEST_SHEET_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode() or "{}")


_sheet_queue: list = []


def sheet_turn() -> asyncio.Lock:
    """One write to the sheet at a time. Its script writes one row at a time
    and gives up after 20s waiting, so a burst sent all at once (e.g. 11.11)
    timed out, and a row that went in late could be added again on the retry."""
    if not _sheet_queue:
        _sheet_queue.append(asyncio.Lock())
    return _sheet_queue[0]


async def sync_to_sheet(rid: int) -> None:
    """Add this request's row, or bring its tick and remarks up to date.
    Left marked for the retry job if the sheet can't be reached."""
    if not (REQUEST_SHEET_URL and REQUEST_SHEET_SECRET):
        return
    async with sheet_turn():
        await _sync_to_sheet(rid)


async def _sync_to_sheet(rid: int) -> None:
    r = get_request(rid)
    if not r:
        return
    try:
        out = await asyncio.to_thread(_post_to_sheet, sheet_payload(r))
    except Exception as e:
        log.info("Ops sheet: couldn't write request %s: %s", rid, e)
        return
    if not out.get("ok"):
        log.info("Ops sheet refused request %s: %s", rid, out.get("error"))
        return
    async with write_lock:
        if out.get("row"):
            run("UPDATE special_requests SET sheet_row=? WHERE id=?", (int(out["row"]), rid))
        run("UPDATE special_requests SET sheet_dirty=0 WHERE id=?", (rid,))
    # A new row and an answer that came before it reached the sheet: send that too.
    r = get_request(rid)
    if out.get("row") and (r["status"] in ("DONE", "CANT") or r["ops_note"]):
        async with write_lock:
            run("UPDATE special_requests SET sheet_dirty=1 WHERE id=?", (rid,))
        await _sync_to_sheet(rid)


async def job_sheet_retry(context: ContextTypes.DEFAULT_TYPE) -> None:
    for r in q("SELECT id FROM special_requests WHERE sheet_dirty=1 ORDER BY id LIMIT 50"):
        await sync_to_sheet(r["id"])


async def mark_dirty_and_sync(rid: int) -> None:
    async with write_lock:
        run("UPDATE special_requests SET sheet_dirty=1 WHERE id=?", (rid,))
    in_background(sync_to_sheet(rid))


# --------------------------------------------------------------------------
# Sending a request: /request
# --------------------------------------------------------------------------
# Every step after the order number has a ‹ Back button. Each step's prompt is
# built in one place (step_prompt) so going forward and back look the same.

BACK = InlineKeyboardButton("‹ Back", callback_data="sr:back")
CATEGORY_HINTS = {
    "DATE": "e.g. Customer wants it moved to Saturday afternoon.",
    "ADDRESS": "Write the full new address, with unit number and postal code.",
    "PRODUCT": "Which item, and what it should change to (model and colour).",
    "STOP": "Why it should be stopped, and what happens next (refund, cancel).",
    "OTHER": "What Ops need to do.",
}
# Where ‹ Back goes from each step.
PREVIOUS = {R_COUNTRY: R_ORDER, R_CAT: R_COUNTRY, R_DETAILS: R_CAT, R_DATE: R_DETAILS,
            R_DATE_TYPED: R_DATE, R_TIME: R_DATE, R_TIME_TYPED: R_TIME, R_CONFIRM: R_TIME}


def rows_with_back(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows + [[BACK]])


def date_label(d: date) -> str:
    """One way of writing a date everywhere in requests: Fri 16 Oct."""
    return f"{d:%a %-d %b}"


def confirm_buttons(flagged: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚩 Escalation: " + ("Yes" if flagged else "No"), callback_data="sr:flag")],
        [InlineKeyboardButton("📤 Send to Ops", callback_data="sr:send"),
         InlineKeyboardButton("✖️ Cancel", callback_data="sr:stop")],
        [BACK],
    ])


def read_date(text: str) -> date | None:
    """'17/10', '17/10/2026', '17-10' or '2026-10-17'."""
    t = text.strip().replace("-", "/").replace(".", "/")
    try:
        if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", t):
            y, m, d = (int(x) for x in t.split("/"))
            return date(y, m, d)
        parts = [int(x) for x in t.split("/")]
        if len(parts) == 2:
            d, m = parts
            today = now().date()
            out = date(today.year, m, d)
            return out if out >= today - timedelta(days=1) else date(today.year + 1, m, d)
        if len(parts) == 3:
            d, m, y = parts
            return date(y + 2000 if y < 100 else y, m, d)
    except ValueError:
        return None
    return None


def cutoff_note(country: str) -> str:
    """Under the date buttons: what today's date and the next working day mean."""
    today = now().date()
    lines = [f"<i>{date_label(today)} is today: a same-day request, marked 🔴 URGENT.</i>"]
    if past_cutoff(now(), country):
        nwd = next_working(country, today)
        lines.append(f"<i>⏰ It's past the {cutoff_text()} cut-off, so deliveries up to "
                     f"{date_label(nwd)} are after cut-off.</i>")
    return "\n\n" + "\n".join(lines)


def cutoff_warning(d: dict) -> str:
    kind = cutoff_kind(now(), date.fromisoformat(d["delivery_date"]), d["country"])
    if kind == "URGENT":
        return "🔴 <b>Same-day delivery.</b> Ops will see it marked URGENT."
    if kind == "LATE":
        day = date.fromisoformat(d["delivery_date"])
        later = next_working(d["country"], max(day, next_working(d["country"], now().date())))
        return (f"⏰ <b>After the {cutoff_text()} cut-off for {date_label(day)}.</b> Ops will try, but it "
                f"may move to {date_label(later)}: check the customer is OK with that.")
    return ""


def draft_text(d: dict) -> str:
    tf = d.get("timeframe_other") if d["timeframe"] == "OTHER" else TIMEFRAME_NAMES[d["timeframe"]]
    lines = ["📦 <b>Check your special request</b>", "",
             f"Order: <code>{esc(d['order_no'])}</code> · {esc(REQUEST_COUNTRY_NAMES[d['country']])}",
             f"Request: <b>{esc(CATEGORY_NAMES[d['category']])}</b>",
             f"Delivery: {date_label(date.fromisoformat(d['delivery_date']))} · {esc(tf)}",
             "", esc(d["details"])]
    if d.get("red_flag"):
        lines += ["", "🚩 <b>Escalation</b>: Ops see it straight away, at the top."]
    warn = cutoff_warning(d)
    if warn:
        lines += ["", warn]
    return "\n".join(lines)


def step_prompt(step: int, d: dict) -> tuple:
    """(text, buttons) for one step of /request."""
    if step == R_ORDER:
        now_order = f"\n\nSo far: <code>{esc(d['order_no'])}</code>. Send it again or a new one." \
            if d.get("order_no") else ""
        return ("📦 <b>Special request to Ops</b>\n\nWhat's the <b>order number</b>?" + now_order
                + "\n\n<i>/cancel to stop</i>"), None
    if step == R_COUNTRY:
        return (f"Order: <code>{esc(d['order_no'])}</code>\n\n<b>Which country is the delivery in?</b>",
                rows_with_back([[InlineKeyboardButton(name, callback_data=f"sr:y:{key}")]
                                for key, name in REQUEST_COUNTRIES]))
    if step == R_CAT:
        return (f"{esc(REQUEST_COUNTRY_NAMES[d['country']])}\n\n<b>What's the request?</b>",
                rows_with_back([[InlineKeyboardButton(name, callback_data=f"sr:c:{key}")]
                                for key, name in REQUEST_CATEGORIES]))
    if step == R_DETAILS:
        return (f"<b>{esc(CATEGORY_NAMES[d['category']])}</b>\n\nNow the <b>details</b>, as fully as you can.\n"
                f"<i>{esc(CATEGORY_HINTS[d['category']])}</i>\n\nPlease don't include phone or ID numbers.",
                rows_with_back([]))
    if step == R_DATE:
        # The next few delivery days: public holidays are left out (no courier).
        days, day = [], now().date()
        while len(days) < DATE_CHOICES:
            if not holiday_name(d["country"], day):
                days.append(day)
            day += timedelta(days=1)
        btns = [InlineKeyboardButton(date_label(x), callback_data=f"sr:d:{x.isoformat()}") for x in days]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([InlineKeyboardButton("Another date…", callback_data="sr:d:typed")])
        label = "the new delivery date" if d.get("category") == "DATE" else "the delivery date"
        return f"<b>What's {label}?</b>" + cutoff_note(d["country"]), rows_with_back(rows)
    if step == R_DATE_TYPED:
        return "Type the date, e.g. <code>24/10</code>.", rows_with_back([])
    if step == R_TIME:
        return (f"Delivery: <b>{date_label(date.fromisoformat(d['delivery_date']))}</b>\n\n<b>What time frame?</b>",
                rows_with_back([[InlineKeyboardButton(name, callback_data=f"sr:t:{key}")]
                                for key, name in TIMEFRAMES]))
    if step == R_TIME_TYPED:
        return "Type the time frame, e.g. <code>after 7pm</code>.", rows_with_back([])
    return draft_text(d), confirm_buttons(bool(d.get("red_flag")))


async def show_step(update: Update, context: ContextTypes.DEFAULT_TYPE, step: int) -> int:
    """Show a step: in place when it came from a button, as a new message after typing."""
    context.user_data["sr_step"] = step
    text, kb = step_prompt(step, context.user_data["sr"])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=kb)
    return step


async def on_request_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """sr:back — the step before this one, with what was picked kept."""
    await update.callback_query.answer()
    if "sr" not in context.user_data:
        return ConversationHandler.END
    step = PREVIOUS.get(context.user_data.get("sr_step"), R_ORDER)
    return await show_step(update, context, step)


async def cmd_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/request — send a special request to Ops."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly to send a special request.")
        return ConversationHandler.END
    if not may_request(update.effective_user.id):
        await update.message.reply_text("Special requests are for SH, Livechat, ON and FS.")
        return ConversationHandler.END
    context.user_data["sr"] = {}
    return await show_step(update, context, R_ORDER)


def picked(update: Update, context: ContextTypes.DEFAULT_TYPE, choices: dict) -> str | None:
    """The key from an sr:<x>:<KEY> button, if it's a real choice and a request is going."""
    key = update.callback_query.data.split(":", 2)[2]
    return key if key in choices and "sr" in context.user_data else None


async def on_request_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if len(text) < 3:
        await update.message.reply_text("That looks too short. Send the full order number.")
        return R_ORDER
    context.user_data.setdefault("sr", {})["order_no"] = text[:80]
    return await show_step(update, context, R_COUNTRY)


async def on_request_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    key = picked(update, context, REQUEST_COUNTRY_NAMES)
    if not key:
        return ConversationHandler.END
    context.user_data["sr"]["country"] = key
    return await show_step(update, context, R_CAT)


async def on_request_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    key = picked(update, context, CATEGORY_NAMES)
    if not key:
        return ConversationHandler.END
    context.user_data["sr"]["category"] = key
    return await show_step(update, context, R_DETAILS)


async def on_request_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if len(text) < 5:
        await update.message.reply_text("A bit more detail please, so Ops can act on it.")
        return R_DETAILS
    context.user_data["sr"]["details"] = text[:2000]
    return await show_step(update, context, R_DATE)


async def on_request_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    if "sr" not in context.user_data:
        return ConversationHandler.END
    value = update.callback_query.data.split(":", 2)[2]
    if value == "typed":
        return await show_step(update, context, R_DATE_TYPED)
    context.user_data["sr"]["delivery_date"] = value
    return await show_step(update, context, R_TIME)


async def on_request_date_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = read_date(update.message.text or "")
    if not d or d < now().date():
        await update.message.reply_text("I couldn't read that as a date from today on. Try e.g. 24/10.")
        return R_DATE_TYPED
    country = context.user_data["sr"]["country"]
    if holiday_name(country, d):
        await update.message.reply_text(
            f"{date_label(d)} is a public holiday in {REQUEST_COUNTRY_NAMES[country][3:]} "
            f"({holiday_name(country, d)}): no deliveries. Type another date.")
        return R_DATE_TYPED
    context.user_data["sr"]["delivery_date"] = d.isoformat()
    return await show_step(update, context, R_TIME)


async def on_request_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    key = picked(update, context, TIMEFRAME_NAMES)
    if not key:
        return ConversationHandler.END
    context.user_data["sr"]["timeframe"] = key
    return await show_step(update, context, R_TIME_TYPED if key == "OTHER" else R_CONFIRM)


async def on_request_time_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        return R_TIME_TYPED
    context.user_data["sr"]["timeframe_other"] = text[:80]
    return await show_step(update, context, R_CONFIRM)


async def post_to_ops(bot, rid: int, head: str = "") -> bool:
    """Post (or re-post) a request in its country's Ops chat, with its buttons."""
    r = get_request(rid)
    target = ops_target(r["country"] or "") if r else None
    if not target:
        return False
    kw = {"message_thread_id": target["thread"]} if target.get("thread") else {}
    try:
        msg = await bot.send_message(target["chat"], request_text(r, head),
                                     parse_mode=constants.ParseMode.HTML,
                                     reply_markup=ops_buttons(r, bot.username), **kw)
    except Exception as e:
        log.warning("Couldn't post special request %s to Ops: %s", rid, e)
        return False
    async with write_lock:
        run("UPDATE special_requests SET ops_chat_id=?, ops_msg_id=? WHERE id=?",
            (target["chat"], msg.message_id, rid))
    return True


async def refresh_ops_message(bot, rid: int) -> None:
    """Redraw a request's message in the Ops chat after it changes."""
    r = get_request(rid)
    if not r or not r["ops_msg_id"]:
        return
    try:
        await bot.edit_message_text(request_text(r), chat_id=r["ops_chat_id"], message_id=r["ops_msg_id"],
                                    parse_mode=constants.ParseMode.HTML,
                                    reply_markup=ops_buttons(r, bot.username))
    except BadRequest:
        pass
    except Exception as e:
        log.info("Couldn't update request %s in the Ops chat: %s", rid, e)


async def tell_sender(bot, r, text: str) -> None:
    if not r["created_by"]:
        return   # from the Paperform: nobody to message in Telegram
    try:
        await bot.send_message(r["created_by"], text, parse_mode=constants.ParseMode.HTML)
    except Exception as e:
        log.info("Couldn't tell %s about request %s: %s", r["created_by"], r["id"], e)


def about(r) -> str:
    return (f"request #{r['id']}</b> ({esc(CATEGORY_NAMES[r['category']])}, "
            f"order <code>{esc(r['order_no'])}</code>)")


async def on_request_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "sr:flag" and "sr" in context.user_data:
        d = context.user_data["sr"]
        d["red_flag"] = not d.get("red_flag")
        return await show_step(update, context, R_CONFIRM)
    d = context.user_data.pop("sr", None)
    if query.data == "sr:stop" or not d:
        await query.edit_message_text("Cancelled. Nothing was sent.")
        return ConversationHandler.END
    rid, posted, kind = await create_request(context.bot, d, query.from_user.id)
    await query.edit_message_text(sent_text(rid, posted, kind), parse_mode=constants.ParseMode.HTML)
    return ConversationHandler.END


def sent_text(rid: int, posted, kind: str) -> str:
    late = {"LATE": " It's after the cut-off, so it may move to a later day.",
            "URGENT": " It's same-day, so Ops see it as URGENT."}.get(kind, "")
    if posted == "SHEET":         # on time: the Ops sheet only
        return (f"✅ Sent to Ops as <b>request #{rid}</b>. It's before the cut-off, so it goes "
                "straight into the Ops sheet. I'll message you when it's done.")
    if isinstance(posted, str):   # held for the next round
        return (f"✅ Saved as <b>request #{rid}</b>.{late}\nOps are working in rounds today, so they'll "
                f"see it at <b>{posted}</b>. I'll message you when they pick it up.")
    if posted:
        return f"✅ Sent to Ops as <b>request #{rid}</b>.{late}\nI'll message you when they pick it up."
    return (f"✅ Saved as <b>request #{rid}</b>, but there's no Ops chat for that country yet, "
            "so an admin has been told.")


async def create_request(bot, d: dict, created_by: int, source: str = "bot",
                         submitted_name: str = "") -> tuple:
    """Save a request, post it to Ops and copy it to the sheet. The same for the
    bot chat, the App form and the Paperform. Returns (id, posted, cut-off kind);
    posted is "SHEET" for an on-time request (sheet only, no Ops chat post), the
    round's time ("3:00pm") when it waits for the next round, or whether there's
    an Ops chat to post it in."""
    created = now()
    kind = cutoff_kind(created, date.fromisoformat(d["delivery_date"]), d.get("country") or "")
    async with write_lock:
        rid = run(
            "INSERT INTO special_requests (order_no, country, category, details, delivery_date, timeframe,"
            " timeframe_other, created_by, created_at, after_cutoff, urgent, status, sheet_dirty,"
            " source, submitted_name) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'OPEN', ?, ?, ?)",
            (d["order_no"], d.get("country") or "", d["category"], d["details"], d["delivery_date"],
             d["timeframe"], d.get("timeframe_other"), created_by, created.isoformat(),
             1 if kind == "LATE" else 0, 1 if kind == "URGENT" else 0,
             0 if source == "paperform" else 1, source, submitted_name or None)).lastrowid
        if d.get("red_flag"):
            set_setting(f"red_flag:{rid}", "yes")
    # Saved: the sender is answered now, and the Ops chat and the sheet are done
    # just after. On a busy day either can take a while, and a slow answer made
    # people press Send again.
    sheet = source != "paperform"
    if not kind and not d.get("red_flag"):
        # On time: the sheet only, the Ops chat is for after the cut-off.
        in_background(deliver_request(bot, rid, post=False, sheet=sheet))
        return rid, "SHEET", kind
    country = d.get("country") or ""
    due = next_round(country, created)
    if due and kind != "URGENT" and not d.get("red_flag") and ops_target(country):
        in_background(deliver_request(bot, rid, post=False, sheet=sheet))
        return rid, round_time(due), kind
    in_background(deliver_request(bot, rid, post=True, sheet=sheet))
    return rid, bool(ops_target(country)), kind


_background: set = set()


def in_background(coro) -> None:
    """Run it on the bot's loop without waiting (kept referenced until done).
    Sheet writes go this way: they queue (sheet_turn), and the bot answers
    one tap at a time, so waiting on them would hold everyone up."""
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def deliver_request(bot, rid: int, post: bool, sheet: bool) -> None:
    if post and not await post_to_ops(bot, rid):
        r = get_request(rid)
        country = r["country"] or ""
        why = (f"link their chat with /opschat {country} inside it" if not ops_target(country)
               else "Telegram refused it. It's in /requests and the sheet")
        for admin in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin, f"⚠️ Special request #{rid} ({country or 'no country'}) couldn't "
                    f"reach Ops: {why}.")
            except Exception:
                pass
    if sheet:
        await sync_to_sheet(rid)


# --------------------------------------------------------------------------
# Ops answering in their chat
# --------------------------------------------------------------------------

async def on_ops_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """sr:tk (take) / sr:ok / sr:no / sr:nr:<reason> / sr:bk on a request in an Ops chat."""
    query = update.callback_query
    parts = query.data.split(":")
    action, rid, uid = parts[1], int(parts[2]), query.from_user.id
    if not is_ops(uid):
        await query.answer("Only the Ops team can answer requests.", show_alert=True)
        return
    r = get_request(rid)
    if not r:
        await query.answer("That request is gone.", show_alert=True)
        return
    if r["status"] not in LIVE:
        await query.answer(f"Already answered: {STATUS_NAMES[r['status']]}.", show_alert=True)
        return
    if r["status"] == "TAKEN" and r["taken_by"] != uid and not is_admin(uid):
        await query.answer(f"{display_name_of(r['taken_by'], 'Someone')} is on this one.", show_alert=True)
        return

    if action == "tk":
        async with write_lock:
            run("UPDATE special_requests SET status='TAKEN', taken_by=?, taken_at=?, sheet_dirty=1"
                " WHERE id=? AND status='OPEN'", (uid, now().isoformat(), rid))
        r = get_request(rid)
        if r["taken_by"] != uid:
            await query.answer(f"{display_name_of(r['taken_by'], 'Someone')} just took it.", show_alert=True)
            return
        await query.answer("It's yours 🙋")
        await refresh_ops_message(context.bot, rid)
        await tell_sender(context.bot, r, f"🙋 <b>Ops are on {about(r)}.\n"
                                          f"<i>— {esc(display_name_of(uid, 'Ops'))}</i>")
        in_background(sync_to_sheet(rid))
        return
    if action == "no":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=cant_buttons(rid, context.bot.username))
        return
    if action == "bk":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=ops_buttons(r, context.bot.username))
        return

    status, reason = ("DONE", None) if action == "ok" else ("CANT", parts[3] if len(parts) > 3 else None)
    if status == "CANT" and reason not in CANT_NAMES:
        await query.answer()
        return
    async with write_lock:
        run("UPDATE special_requests SET status=?, reason=?, handled_by=?, handled_at=?, sheet_dirty=1,"
            " taken_by=COALESCE(taken_by, ?), taken_at=COALESCE(taken_at, ?)"
            " WHERE id=? AND status IN ('OPEN','TAKEN')",
            (status, reason, uid, now().isoformat(), uid, now().isoformat(), rid))
    r = get_request(rid)
    await query.answer("Done ✅" if status == "DONE" else "Marked can't do")
    await refresh_ops_message(context.bot, rid)
    who = esc(display_name_of(uid, "Ops"))
    if status == "DONE":
        msg = f"✅ <b>Ops have done {about(r)}.\n<i>— {who}</i>"
    else:
        msg = (f"🚫 <b>Ops can't do {about(r)}\nReason: <b>{esc(CANT_NAMES.get(reason, reason))}</b>\n<i>— {who}</i>"
               "\n\nThey may add a note with more detail.")
    await tell_sender(context.bot, r, msg)
    in_background(sync_to_sheet(rid))


async def start_cant_reason(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str) -> int:
    """/start srx_<id>, from ✏️ Other reason: Ops type why it can't be done."""
    uid = update.effective_user.id
    r = get_request(int(raw)) if raw.isdigit() else None
    if not r or not is_ops(uid):
        await update.message.reply_text("That request is gone, or this is for the Ops team only.")
        return ConversationHandler.END
    if r["status"] not in LIVE:
        await update.message.reply_text(f"Request #{r['id']} is already answered: {STATUS_NAMES[r['status']]}.")
        return ConversationHandler.END
    if r["status"] == "TAKEN" and r["taken_by"] != uid and not is_admin(uid):
        await update.message.reply_text(f"{display_name_of(r['taken_by'], 'Someone')} is on that one.")
        return ConversationHandler.END
    context.user_data["sr_reason"] = r["id"]
    await update.message.reply_text(
        f"🚫 <b>Can't do {about(r).replace('</b>', '', 1)}\n\nWhy can't it be done? Type the reason; "
        f"{esc(sender_name(r))} will see it.\n\n<i>/cancel to stop</i>", parse_mode=constants.ParseMode.HTML)
    return SR_REASON


async def on_cant_reason_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    rid = context.user_data.pop("sr_reason", None)
    text = (update.message.text or "").strip()[:300]
    uid = update.effective_user.id
    r = get_request(rid) if rid else None
    if not r or not text:
        await update.message.reply_text("Nothing was changed.")
        return ConversationHandler.END
    async with write_lock:
        run("UPDATE special_requests SET status='CANT', reason=?, handled_by=?, handled_at=?, sheet_dirty=1,"
            " taken_by=COALESCE(taken_by, ?), taken_at=COALESCE(taken_at, ?)"
            " WHERE id=? AND status IN ('OPEN','TAKEN')",
            (text, uid, now().isoformat(), uid, now().isoformat(), rid))
    r = get_request(rid)
    if r["status"] != "CANT" or r["reason"] != text:
        await update.message.reply_text(f"Request #{rid} was answered meanwhile: {STATUS_NAMES[r['status']]}.")
        return ConversationHandler.END
    await refresh_ops_message(context.bot, rid)
    await tell_sender(context.bot, r, f"🚫 <b>Ops can't do {about(r)}\nReason: <b>{esc(text)}</b>\n"
                                      f"<i>— {esc(display_name_of(uid, 'Ops'))}</i>")
    in_background(sync_to_sheet(rid))
    await update.message.reply_text(f"✅ Marked can't do, and {sender_name(r)} has been told why.")
    return ConversationHandler.END


async def start_ops_note(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str) -> int:
    """/start srn_<id>, from 📝 Note to sender: Ops write the note here, privately."""
    uid = update.effective_user.id
    r = get_request(int(raw)) if raw.isdigit() else None
    if not r or not is_ops(uid):
        await update.message.reply_text("That request is gone, or notes are for the Ops team only.")
        return ConversationHandler.END
    context.user_data["sr_note"] = r["id"]
    await update.message.reply_text(
        f"📝 <b>Note to {esc(display_name_of(r['created_by'], 'the sender'))}</b> about "
        f"{about(r).replace('</b>', '', 1)}\n\nWhat should they know? (e.g. moved to Sat 2pm–6pm, "
        "or the driver has already left)\n\n<i>/cancel to stop</i>",
        parse_mode=constants.ParseMode.HTML)
    return SR_NOTE


async def on_ops_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    rid = context.user_data.pop("sr_note", None)
    text = (update.message.text or "").strip()[:500]
    r = get_request(rid) if rid else None
    if not r or not text:
        await update.message.reply_text("Nothing was sent.")
        return ConversationHandler.END
    async with write_lock:
        run("UPDATE special_requests SET ops_note=? WHERE id=?", (text, rid))
    r = get_request(rid)
    await tell_sender(context.bot, r, f"📝 <b>Note from Ops on {about(r)}\n\n{esc(text)}\n"
                                      f"<i>— {esc(display_name_of(update.effective_user.id, 'Ops'))}</i>")
    await refresh_ops_message(context.bot, rid)
    await mark_dirty_and_sync(rid)
    await update.message.reply_text(f"✅ Sent to {display_name_of(r['created_by'], 'the sender')}, "
                                    "and added under the request in the Ops chat.")
    return ConversationHandler.END


async def post_round(bot, country: str, label: str) -> int:
    """Post every request for this country still waiting for a round, under one
    heading. Returns how many went."""
    rows = [r for r in q("SELECT * FROM special_requests WHERE country=? AND status IN ('OPEN','TAKEN')"
                         " AND ops_msg_id IS NULL ORDER BY delivery_date, id", (country,)) if in_ops_chat(r)]
    target = ops_target(country)
    if not rows or not target:
        return 0
    kw = {"message_thread_id": target["thread"]} if target.get("thread") else {}
    try:
        await bot.send_message(target["chat"], f"🕒 <b>{esc(label)} · {REQUEST_COUNTRY_NAMES[country]}"
                               f" · {len(rows)} request{'' if len(rows) == 1 else 's'}</b>\nSoonest delivery first.",
                               parse_mode=constants.ParseMode.HTML, **kw)
    except Exception as e:
        log.warning("Couldn't post the %s round: %s", country, e)
        return 0
    sent = 0
    for r in rows:
        sent += await post_to_ops(bot, r["id"])
    return sent


async def job_ops_rounds(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every half hour: post the rounds that are due (60-minute rounds on the hour)."""
    t = now()
    on_the_hour = t.minute < 5
    for country in REQUEST_COUNTRY_NAMES:
        every = rounds_for(country)
        if every and (on_the_hour or (every == 30 and 30 <= t.minute < 35)):
            slot = t.replace(minute=0 if on_the_hour else 30, second=0, microsecond=0)
            await post_round(context.bot, country, f"Round {round_time(slot)}")


async def cmd_opsrounds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/opsrounds — busy days: requests to Ops in rounds, per country.
    /opsrounds SG 30 · /opsrounds MY 60 · /opsrounds SG off"""
    if not admin_for(update.effective_user.id, "OPS"):
        return
    args = [a.upper() for a in context.args]
    country = next((a for a in args if a in REQUEST_COUNTRY_NAMES), "")
    choice = next((a for a in args if a in ("30", "60", "OFF")), "")
    if not country or not choice:
        lines = ["🕒 <b>Ops rounds</b>", "On a busy day, a country's requests can go to Ops every 30 or 60 "
                 "minutes in one batch, instead of one message each. 🔴 Same-day and 🚩 escalated "
                 "requests always go straight away.", ""]
        for cc, name in REQUEST_COUNTRIES:
            every = rounds_for(cc)
            lines.append(f"{name}: " + (f"<b>every {every} min</b>, next at {round_time(next_round(cc))}"
                                        if every else "off (one message each)"))
        lines += ["", "<code>/opsrounds SG 30</code> · <code>/opsrounds MY 60</code> · <code>/opsrounds SG off</code>"]
        await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)
        return
    async with write_lock:
        set_setting(f"ops_rounds:{country}", "" if choice == "OFF" else choice)
    name = REQUEST_COUNTRY_NAMES[country]
    if choice == "OFF":
        # Anything still waiting goes now, so nothing is left behind.
        n = await post_round(context.bot, country, "Rounds off: waiting requests")
        await update.message.reply_text(f"✅ {name}: rounds off. Requests go to Ops one by one again"
                                        + (f" ({n} waiting just went)." if n else "."))
        return
    await update.message.reply_text(f"✅ {name}: requests go to Ops every {choice} minutes, next at "
                                    f"{round_time(next_round(country))}. 🔴 Same-day and 🚩 ones still go "
                                    "straight away. Turn off with /opsrounds {country} off.".replace("{country}", country))


async def job_carry_over(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Working mornings: re-post after-cut-off requests still open, per Ops chat."""
    today = now()
    if today.weekday() >= 5:
        return
    start = today.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = q("SELECT * FROM special_requests WHERE status IN ('OPEN','TAKEN') AND after_cutoff=1"
             " AND created_at < ? ORDER BY delivery_date, id", (start,))
    groups: dict = {}
    for r in rows:
        if not is_working_day(r["country"] or "", today.date()):
            continue   # a public holiday there: they come back on the next working morning
        target = ops_target(r["country"] or "")
        if target:
            groups.setdefault(json.dumps(target, sort_keys=True), []).append(r)
    for key, items in groups.items():
        target = json.loads(key)
        kw = {"message_thread_id": target["thread"]} if target.get("thread") else {}
        try:
            await context.bot.send_message(
                target["chat"], f"📌 <b>After the last cut-off: {len(items)} request"
                f"{'' if len(items) == 1 else 's'} still open</b>\nSoonest delivery first.",
                parse_mode=constants.ParseMode.HTML, **kw)
        except Exception as e:
            log.warning("Couldn't post the carry-over list: %s", e)
            continue
        for r in items:
            old_chat, old_msg = r["ops_chat_id"], r["ops_msg_id"]
            if await post_to_ops(context.bot, r["id"], head="↪️ <i>Carried over</i>") and old_msg:
                try:
                    await context.bot.edit_message_reply_markup(old_chat, old_msg, reply_markup=None)
                except Exception:
                    pass


def ops_chat_lines() -> list:
    lines = []
    shared = setting("ops_requests_chat", "")
    for key, name in REQUEST_COUNTRIES:
        own = setting(f"ops_requests_chat:{key}", "")
        lines.append(f"{name}: " + ("its own chat" if own else "the shared chat" if shared else "<i>not linked</i>"))
    return lines


async def cmd_opschat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/opschat [SG|MY|HK|TH] (admins, inside an Ops chat or topic) — requests post here.
    No country: the shared chat for any country without its own. /opschat off [CC] stops."""
    if not admin_for(update.effective_user.id, "OPS"):
        return
    chat = update.effective_chat
    args = [a.upper() for a in context.args]
    off = bool(args) and args[0] == "OFF"
    country = next((a for a in args if a in REQUEST_COUNTRY_NAMES), "")
    key = f"ops_requests_chat:{country}" if country else "ops_requests_chat"
    if chat.type == constants.ChatType.PRIVATE and not off:
        await update.message.reply_text(
            "<b>Where special requests go</b>\n\n" + "\n".join(ops_chat_lines())
            + "\n\nTo link one, send <code>/opschat SG</code> (or MY, HK, TH) inside that Ops chat or "
            "topic. <code>/opschat</code> on its own sets the shared chat for the rest.",
            parse_mode=constants.ParseMode.HTML)
        return
    if off:
        async with write_lock:
            set_setting(key, "")
        await update.message.reply_text(f"Unlinked {REQUEST_COUNTRY_NAMES.get(country, 'the shared chat')}.")
        return
    if chat.type not in (constants.ChatType.GROUP, constants.ChatType.SUPERGROUP):
        await update.message.reply_text("Send this inside the Ops chat where requests should go.")
        return
    thread = update.message.message_thread_id if update.message.is_topic_message else None
    async with write_lock:
        set_setting(key, json.dumps({"chat": chat.id, "thread": thread}))
    where = REQUEST_COUNTRY_NAMES[country] if country else "any country without its own chat"
    await update.message.reply_text(
        f"✅ Special requests for {where} will be posted here"
        f"{' (this topic)' if thread else ''}. Next-working-day deliveries asked for after "
        f"{cutoff_text()} are marked ⏰ AFTER CUT-OFF and come back at {clock_text(REQUEST_CARRY_TIME)} "
        "the next working day; same-day ones are marked 🔴 URGENT.")


async def cmd_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/requests — Ops and admins: the special requests still open."""
    if update.effective_chat.type != constants.ChatType.PRIVATE or not is_ops(update.effective_user.id):
        return
    rows = q("SELECT * FROM special_requests WHERE status IN ('OPEN','TAKEN')"
             " ORDER BY country, urgent DESC, after_cutoff DESC, delivery_date, id")
    rows = sorted(rows, key=lambda r: (r["country"] or "", not is_flagged(r["id"])))
    if not rows:
        await update.message.reply_text("No special requests open. 🎉")
        return
    lines = [f"📦 <b>Open special requests: {len(rows)}</b>"]
    last = None
    for r in rows:
        if r["country"] != last:
            last = r["country"]
            lines += ["", f"<b>{esc(REQUEST_COUNTRY_NAMES.get(last or '', 'No country'))}</b>"]
        flags = ("🚩 " if is_flagged(r["id"]) else "") + ("🔴 " if r["urgent"] else "⏰ " if r["after_cutoff"] else "") \
            + ("🙋 " if r["status"] == "TAKEN" else "") \
            + ("" if r["ops_msg_id"] else "🕒 " if in_ops_chat(r) else "📄 ")
        lines.append(f"{flags}<b>#{r['id']}</b> {esc(CATEGORY_NAMES[r['category']])} · "
                     f"<code>{esc(r['order_no'])}</code> · "
                     f"{date_label(date.fromisoformat(r['delivery_date']))}, {esc(timeframe_text(r))}"
                     + (f" · {esc(display_name_of(r['taken_by'], ''))}" if r["status"] == "TAKEN" else ""))
    lines += ["", "🔴 same day · ⏰ after cut-off · 🙋 someone's on it. Answer them in the Ops chat."]
    await reply_long(update.message, "\n".join(lines), parse_mode=constants.ParseMode.HTML)


# --------------------------------------------------------------------------
# From outside Telegram: the Paperform, and ticks made in the Ops sheet.
# Both arrive at the App's web server (webapp.py) and run on its thread.
# --------------------------------------------------------------------------

PO_COUNTRY = (("TCSG", "SG"), ("TCMY", "MY"), ("TCHK", "HK"), ("TCTH", "TH"))


def country_from_po(po: str) -> str:
    """TCSG-12345 is a Singapore order, and so on; '' when the PO doesn't say."""
    up = po.upper()
    return next((c for prefix, c in PO_COUNTRY if prefix in up), "")


def category_from_text(text: str) -> str:
    t = text.lower()
    for key, name in SHEET_CATEGORY.items():
        if t == name.lower():
            return key
    if "address" in t:
        return "ADDRESS"
    if "date" in t or "time" in t:
        return "DATE"
    if "product" in t or "colour" in t or "color" in t:
        return "PRODUCT"
    if "stop" in t:
        return "STOP"
    return "OTHER"


def timeframe_from_text(text: str) -> tuple:
    t = text.lower().replace(" ", "").replace("–", "-")
    for key, name in SHEET_TIME.items():
        if t == name.lower():
            return key, None
    return "OTHER", (text.strip() or "Other")


def paperform_request(payload: dict) -> tuple:
    """(request dict, the name typed on the form) from a Paperform webhook, or
    (None, why). Questions are matched by their titles, like the sheet's columns."""
    answers = []
    for item in payload.get("data") or []:
        value = item.get("value")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        answers.append((str(item.get("title") or item.get("key") or "").lower(),
                        "" if value is None else str(value).strip()))

    def answer(*words: str) -> str:
        for title, value in answers:
            if all(re.search(rf"\b{w}", title) for w in words) and value:
                return value
        return ""

    po = answer("po") or answer("order")
    if not po:
        return None, "no PO number"
    day = read_day(answer("delivery", "date") or answer("date")) or now().date()
    timeframe, other = timeframe_from_text(answer("delivery", "time") or answer("time"))
    details = answer("description") or answer("detail") or "(no description)"
    return {
        "order_no": po[:80], "country": country_from_po(po),
        "category": category_from_text(answer("category") or answer("request")),
        "details": details[:2000], "delivery_date": day.isoformat(),
        "timeframe": timeframe, "timeframe_other": other,
    }, (answer("name") or answer("person") or answer("submitted"))[:80]


def web_paperform(payload: dict) -> dict:
    d, name = paperform_request(payload)
    if not d:
        return {"ok": False, "error": name}
    out = on_bot_loop(create_request(bot_ref(), d, 0, "paperform", name), timeout=20)
    return {"ok": bool(out), "id": out[0] if out else None}


def find_sheet_request(po: str, timestamp: str, category: str):
    """The open request a ticked sheet row belongs to: the bot's own rows match
    on PO # and timestamp, Paperform's on PO # and request type."""
    live = q("SELECT * FROM special_requests WHERE order_no=? AND status IN ('OPEN','TAKEN')"
             " ORDER BY id DESC", (po,))
    for r in live:
        if r["source"] != "paperform" and sheet_values(r)[1] == timestamp:
            return r
    for r in live:
        if r["source"] == "paperform" and SHEET_CATEGORY[r["category"]].lower() == category.lower():
            return r
    return None


async def after_sheet_tick(bot, rid: int) -> None:
    r = get_request(rid)
    await refresh_ops_message(bot, rid)
    await tell_sender(bot, r, f"✅ <b>Ops have done {about(r)}.\n<i>— ticked in the Ops sheet</i>")


def web_sheet_tick(body: dict) -> dict:
    """Column H ticked in the sheet: that request is done in Telegram too."""
    r = find_sheet_request(str(body.get("po") or ""), str(body.get("timestamp") or ""),
                           str(body.get("category") or ""))
    if not r:
        return {"ok": True, "matched": False}
    run("UPDATE special_requests SET status='DONE', handled_by=0, handled_at=?, sheet_dirty=0"
        " WHERE id=? AND status IN ('OPEN','TAKEN')", (now().isoformat(), r["id"]))
    on_bot_loop(after_sheet_tick(bot_ref(), r["id"]))
    return {"ok": True, "matched": True, "id": r["id"]}


# --------------------------------------------------------------------------
# The App's Special request form (webapp.py: GET /api/requests, POST /api/request)
# --------------------------------------------------------------------------

def request_form_payload(user_id: int, country: str = "") -> dict:
    """The form's choices for one country, and this person's recent requests."""
    if country not in REQUEST_COUNTRY_NAMES:
        me = q1("SELECT country FROM agents WHERE user_id=?", (user_id,))
        country = (me["country"] if me and me["country"] in REQUEST_COUNTRY_NAMES else "") or "SG"
    days, day, when = [], now().date(), now()
    while len(days) < DATE_CHOICES:
        if not holiday_name(country, day):
            days.append({"iso": day.isoformat(), "label": date_label(day), "kind": cutoff_kind(when, day, country)})
        day += timedelta(days=1)
    return {
        "country": country,
        "countries": [{"key": k, "name": n} for k, n in REQUEST_COUNTRIES],
        "categories": [{"key": k, "name": n, "hint": CATEGORY_HINTS[k]} for k, n in REQUEST_CATEGORIES],
        "timeframes": [{"key": k, "name": n} for k, n in TIMEFRAMES],
        "days": days,
        "cutoff": cutoff_text(),
        "mine": my_requests(user_id, 8),
    }


def my_requests(user_id: int, limit: int) -> list:
    """This person's latest special requests, as the App lists them."""
    return [{
        "id": r["id"], "order": r["order_no"], "category": CATEGORY_NAMES[r["category"]],
        "delivery": f"{date_label(date.fromisoformat(r['delivery_date']))} · {timeframe_text(r)}",
        "status": STATUS_NAMES.get(r["status"], r["status"]), "statusKey": r["status"],
        "urgent": bool(r["urgent"]), "late": bool(r["after_cutoff"]), "redFlag": is_flagged(r["id"]),
        "takenBy": display_name_of(r["taken_by"], "") if r["status"] == "TAKEN" and r["taken_by"] else "",
        "reason": CANT_NAMES.get(r["reason"], r["reason"] or "") if r["status"] == "CANT" else "",
        "note": r["ops_note"] or "",
    } for r in q("SELECT * FROM special_requests WHERE created_by=? ORDER BY id DESC LIMIT ?",
                 (user_id, limit))]


def ops_home(user_id: int) -> dict:
    """Ops's Home: what's open, the urgent ones first, each linking to its
    message in the Ops chat (that's where it's answered)."""
    rows = flagged_first(q("SELECT * FROM special_requests WHERE status IN ('OPEN','TAKEN')"
                           " ORDER BY urgent DESC, after_cutoff DESC, delivery_date, id"))
    def item(r):
        chat = str(r["ops_chat_id"] or "")
        link = (f"https://t.me/c/{chat[4:]}/{r['ops_msg_id']}"
                if chat.startswith("-100") and r["ops_msg_id"] else "")
        taker = display_name_of(r["taken_by"], "") if r["status"] == "TAKEN" and r["taken_by"] else ""
        return {"id": r["id"], "order": r["order_no"], "category": CATEGORY_NAMES[r["category"]],
                "flag": REQUEST_COUNTRY_NAMES.get(r["country"] or "", "")[:2],
                "delivery": date_label(date.fromisoformat(r["delivery_date"])),
                "from": sender_name(r),
                "at": datetime.fromisoformat(r["created_at"]).strftime("%-I:%M%p").lower(),
                "urgent": bool(r["urgent"]), "late": bool(r["after_cutoff"]), "redFlag": is_flagged(r["id"]),
                "round": "" if r["ops_msg_id"] or not in_ops_chat(r) else round_time(next_round(r["country"] or "") or now()),
                "sheetOnly": not in_ops_chat(r),
                "takenBy": "You" if r["taken_by"] == user_id and taker else taker, "link": link}
    today = now().date().isoformat()
    return {
        "open": len(rows),
        "urgent": sum(1 for r in rows if r["urgent"]),
        "late": sum(1 for r in rows if r["after_cutoff"] and not r["urgent"]),
        "mine": sum(1 for r in rows if r["status"] == "TAKEN" and r["taken_by"] == user_id),
        "doneToday": q1("SELECT COUNT(*) AS n FROM special_requests WHERE status IN ('DONE','CANT')"
                        " AND handled_at >= ?", (today,))["n"],
        "requests": [item(r) for r in rows[:30]],
    }


def web_request_add(user_id: int, body: dict) -> dict:
    """The App form: checked the same way as the bot's steps, then sent the same way."""
    if not may_request(user_id):
        return {"ok": False, "error": "Special requests are for SH, Livechat, ON and FS."}
    get = lambda k: str(body.get(k) or "").strip()
    d = {"order_no": get("order_no")[:80], "country": get("country"), "category": get("category"),
         "details": get("details")[:2000], "delivery_date": get("delivery_date"),
         "timeframe": get("timeframe"), "timeframe_other": get("timeframe_other")[:80] or None,
         "red_flag": body.get("red_flag") is True}
    if len(d["order_no"]) < 3:
        return {"ok": False, "error": "Add the full order number."}
    if d["country"] not in REQUEST_COUNTRY_NAMES:
        return {"ok": False, "error": "Pick the country."}
    if d["category"] not in CATEGORY_NAMES:
        return {"ok": False, "error": "Pick what the request is."}
    if len(d["details"]) < 5:
        return {"ok": False, "error": "A bit more detail please, so Ops can act on it."}
    try:
        day = date.fromisoformat(d["delivery_date"])
    except ValueError:
        return {"ok": False, "error": "Pick the delivery date."}
    if day < now().date():
        return {"ok": False, "error": "That delivery date has passed."}
    if holiday_name(d["country"], day):
        return {"ok": False, "error": f"{date_label(day)} is a public holiday there: no deliveries."}
    if d["timeframe"] not in TIMEFRAME_NAMES:
        return {"ok": False, "error": "Pick the time frame."}
    if d["timeframe"] == "OTHER" and not d["timeframe_other"]:
        return {"ok": False, "error": "Type the time frame."}
    out = on_bot_loop(create_request(bot_ref(), d, user_id), timeout=20)
    if not out:
        return {"ok": False, "error": "Couldn't send that just now. Try again in a minute."}
    rid, posted, kind = out
    return {"ok": True, "id": rid, "message": re.sub(r"<[^>]+>", "", sent_text(rid, posted, kind))}
