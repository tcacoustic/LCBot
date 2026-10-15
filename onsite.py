"""On-Site: home visits by On-Site Specialists — booking, clashes, reschedules.

Part of the SH bot. Shared helpers live in core.py. SH and FS book visits;
the specialist for the visit's country is told, and can block out times
they aren't free. Reschedules are requests the specialist accepts.
"""

from __future__ import annotations

import math
import urllib.parse

from core import *  # noqa: F401,F403

COUNTRIES = [
    ("SG", "🇸🇬", "Singapore", "Asia/Singapore"),
    ("MY", "🇲🇾", "Malaysia", "Asia/Kuala_Lumpur"),
    ("HK", "🇭🇰", "Hong Kong", "Asia/Hong_Kong"),
    ("TH", "🇹🇭", "Thailand", "Asia/Bangkok"),
]
COUNTRY_NAMES = {c: f"{flag} {name}" for c, flag, name, _ in COUNTRIES}
COUNTRY_TZ = {c: ZoneInfo(tz) for c, _, _, tz in COUNTRIES}
# FS stores: (code, country, name). FS staff pick theirs when they join.
FS_STORES = [
    ("SG_SONOS", "SG", "Sonos Flagship Store"),
    ("SG_ADELPHI", "SG", "TC Acoustic @ The Adelphi"),
    ("MY_SONOS", "MY", "Sonos Concept Store by TC Acoustic"),
    ("HK_EXPCENTRE", "HK", "TC Experience Centre HK"),
    ("TH_EMSPHERE", "TH", "Sonos Store EmSphere"),
]
FS_STORE_NAMES = {code: name for code, _, name in FS_STORES}
FS_STORE_COUNTRY = {code: c for code, c, _ in FS_STORES}

VISIT_TYPES = [("SETUP", "Set up"), ("TROUBLESHOOT", "Troubleshoot")]
# Survey is no longer offered; the name stays so any old survey still shows.
VISIT_NAMES = {**dict(VISIT_TYPES), "SURVEY": "Survey"}
# Bookers only pick a start time: a visit takes 1 to 2 hours and only the
# specialist knows, so 2 hours are kept free after it for clash checks.
RESERVE_MIN = 120
LIVE_STATUSES = ("SCHEDULED", "ONSITE")   # visits that take up the specialist's time

# What a visit costs, by country: first hour, each started hour after, currency.
# A few minutes over an hour aren't charged (GRACE_MIN). Countries not here:
# the specialist types the amount.
PRICING = {"SG": (10000, 8000, "SGD")}
GRACE_MIN = 10


def visit_charge(country: str, minutes: int):
    """(cents, currency) the bot suggests for a resolved visit, or None."""
    if country not in PRICING or minutes <= 0:
        return None
    first, after, currency = PRICING[country]
    extra = max(0, minutes - 60 - GRACE_MIN)
    return first + after * math.ceil(extra / 60), currency


def money_text(cents: int, currency: str) -> str:
    return f"{currency} {cents / 100:,.2f}"


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------

def may_book(user_id: int) -> bool:
    """SH, FS and admins book visits; specialists work them."""
    return is_admin(user_id) or bool(depts_of(user_id) & {"SH", "FS"})


def is_specialist(user_id: int) -> bool:
    return "ONSITE" in depts_of(user_id)


def specialists_in(country: str) -> list:
    return q("SELECT a.* FROM agents a JOIN memberships m ON m.user_id=a.user_id "
             "WHERE m.dept='ONSITE' AND a.status='active' AND a.country=? "
             "ORDER BY a.name", (country,))


def country_picker(user_id: int) -> InlineKeyboardMarkup:
    row = q1("SELECT country FROM agents WHERE user_id=?", (user_id,))
    have = row["country"] if row else None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{'✅' if c == have else '⬜️'} {flag} {name}",
                               callback_data=f"vz:cy:{user_id}:{c}")]
         for c, flag, name, _ in COUNTRIES])


async def ask_country(bot, admin_id: int, user_id: int, shown: str) -> None:
    await bot.send_message(
        admin_id, f"<b>Which country does {esc(shown)} do visits in?</b>",
        parse_mode=constants.ParseMode.HTML, reply_markup=country_picker(user_id))


async def on_country_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """vz:cy:<id>:<CC> — set the country a specialist works in."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Only admins can change this.", show_alert=True)
        return
    _, _, raw, country = query.data.split(":")
    uid = int(raw)
    if country not in COUNTRY_NAMES:
        await query.answer()
        return
    async with write_lock:
        run("UPDATE agents SET country=? WHERE user_id=?", (country, uid))
    await query.answer()
    await query.edit_message_text(
        f"✅ {display_name_of(uid, 'They')} does visits in {COUNTRY_NAMES[country]}.")


# --------------------------------------------------------------------------
# FS stores: country first, then that country's stores
# --------------------------------------------------------------------------

def store_of(user_id: int) -> str:
    row = q1("SELECT store FROM agents WHERE user_id=?", (user_id,))
    return FS_STORE_NAMES.get(row["store"] or "", "") if row else ""


def booker_name(user_id: int) -> str:
    """Who booked a visit; FS staff also show their store."""
    who = display_name_of(user_id, "SH")
    store = store_of(user_id) if "FS" in depts_of(user_id) else ""
    return f"{who} · {store}" if store else who


def store_question(user_id: int, asker_id: int) -> str:
    if user_id == asker_id:
        return "Which store are you at? Pick your country first."
    return f"Which store is {display_name_of(user_id, 'they')} at? Pick the country first."


def store_country_picker(user_id: int) -> InlineKeyboardMarkup:
    have = q1("SELECT country FROM agents WHERE user_id=?", (user_id,))
    have = have["country"] if have else None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{'✅' if c == have else '⬜️'} {flag} {name}",
                               callback_data=f"vz:fc:{user_id}:{c}")]
         for c, flag, name, _ in COUNTRIES if c in FS_STORE_COUNTRY.values()])


def store_picker(user_id: int, country: str) -> InlineKeyboardMarkup:
    row = q1("SELECT store FROM agents WHERE user_id=?", (user_id,))
    have = row["store"] if row else None
    rows = [[InlineKeyboardButton(f"{'✅' if code == have else '⬜️'} {name}",
                                  callback_data=f"vz:fs:{user_id}:{code}")]
            for code, c, name in FS_STORES if c == country]
    rows.append([InlineKeyboardButton("‹ Back", callback_data=f"vz:fc:{user_id}:back")])
    return InlineKeyboardMarkup(rows)


async def ask_store(bot, chat_id: int, user_id: int) -> None:
    """Send the store picker to chat_id (the FS member themselves, or an admin)."""
    try:
        await bot.send_message(chat_id, f"<b>{esc(store_question(user_id, chat_id))}</b>",
                               parse_mode=constants.ParseMode.HTML,
                               reply_markup=store_country_picker(user_id))
    except Exception as e:
        log.info("Couldn't ask %s for a store: %s", chat_id, e)


async def on_store_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """vz:fc:<id>:<CC|back> picks the country · vz:fs:<id>:<STORE> saves the store."""
    query = update.callback_query
    _, step, raw, value = query.data.split(":")
    uid = int(raw)
    if query.from_user.id != uid and not is_admin(query.from_user.id):
        await query.answer("Only admins can change someone else's store.", show_alert=True)
        return
    question = f"<b>{esc(store_question(uid, query.from_user.id))}</b>"
    if step == "fc":
        await query.answer()
        if value == "back":
            await query.edit_message_text(question, parse_mode=constants.ParseMode.HTML,
                                          reply_markup=store_country_picker(uid))
            return
        if value not in COUNTRY_NAMES:
            return
        await query.edit_message_text(f"{question}\n\n{COUNTRY_NAMES[value]}",
                                      parse_mode=constants.ParseMode.HTML,
                                      reply_markup=store_picker(uid, value))
        return
    if value not in FS_STORE_NAMES:
        await query.answer()
        return
    async with write_lock:
        run("UPDATE agents SET store=?, country=? WHERE user_id=?",
            (value, FS_STORE_COUNTRY[value], uid))
    await query.answer()
    who = "You're" if query.from_user.id == uid else f"{display_name_of(uid, 'They')} is"
    await query.edit_message_text(
        f"✅ {who} at {FS_STORE_NAMES[value]} ({COUNTRY_NAMES[FS_STORE_COUNTRY[value]]}).")


async def cmd_store(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/store — FS staff change the store they're at."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    if "FS" not in depts_of(uid) and not is_admin(uid):
        return
    if "FS" not in depts_of(uid):
        await update.message.reply_text("To set an FS member's store: /specialists @username")
        return
    now_at = store_of(uid)
    if now_at:
        await update.message.reply_text(f"You're at {now_at}.")
    await ask_store(context.bot, uid, uid)


async def cmd_specialists(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/specialists — On-Site Specialists and FS by country · /specialists @name — set theirs."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not is_admin(update.effective_user.id):
        return
    target = " ".join(context.args).strip()
    if target:
        row = find_agent(target)
        if not row or not {"ONSITE", "FS"} & depts_of(row["user_id"]):
            await update.message.reply_text(
                "I can't find an On-Site Specialist or FS member by that name. Add them "
                "to the team first with /departments @username.")
            return
        if "ONSITE" not in depts_of(row["user_id"]):
            await ask_store(context.bot, update.effective_user.id, row["user_id"])
            return
        await update.message.reply_text(
            f"<b>Which country does {esc(row['display_name'] or row['name'])} do visits in?</b>",
            parse_mode=constants.ParseMode.HTML, reply_markup=country_picker(row["user_id"]))
        return
    lines = ["<b>On-Site Specialists</b>", ""]
    for c, flag, name, _ in COUNTRIES:
        team = specialists_in(c)
        lines.append(f"{flag} {name}: " + (", ".join(esc(r["display_name"] or r["name"])
                                                    for r in team) or "<i>none</i>"))
    fs = q("SELECT a.* FROM agents a JOIN memberships m ON m.user_id=a.user_id "
           "WHERE m.dept='FS' AND a.status='active' ORDER BY a.name")
    if fs:
        lines += ["", "<b>FS stores</b>"]
        for code, c, name in FS_STORES:
            here = [r for r in fs if r["store"] == code]
            if here:
                lines.append(f"{COUNTRY_NAMES[c]} {esc(name)}: " + ", ".join(
                    esc(r["display_name"] or r["name"]) for r in here))
    unset = q("SELECT DISTINCT a.* FROM agents a JOIN memberships m ON m.user_id=a.user_id "
              "WHERE a.status='active' AND ((m.dept='ONSITE' AND a.country IS NULL) "
              "OR (m.dept='FS' AND a.store IS NULL))")
    if unset:
        lines += ["", "No country or store yet: " + ", ".join(esc(r["display_name"] or r["name"])
                                                      for r in unset)]
    lines += ["", "Set someone's country: <code>/specialists @username</code>"]
    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)


# --------------------------------------------------------------------------
# Times and clashes
# --------------------------------------------------------------------------

def local_start(country: str, day: str, hhmm: str) -> datetime:
    """'2026-10-18' + '14:30' in the visit country's time."""
    h, m = (int(x) for x in hhmm.split(":"))
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=COUNTRY_TZ[country])


def span_of(v) -> tuple:
    start = datetime.fromisoformat(v["starts_at"])
    return start, start + timedelta(minutes=v["duration_min"])


def when_text(v) -> str:
    start, end = span_of(v)
    return start.strftime('%a %-d %b, %H:%M')


def clash_phrase(busy: str) -> str:
    """'another visit (…)' → 'has another visit (…)'; 'marked …' stays."""
    return busy if busy.startswith("marked") else f"has {busy}"


def clash(specialist_id: int, start: datetime, end: datetime,
          exclude_id: int | None = None) -> str | None:
    """What the specialist is already doing then, or None if they're free."""
    marks = ",".join("?" * len(LIVE_STATUSES))
    for v in q(f"SELECT * FROM appointments WHERE specialist_id=? AND status IN ({marks})",
               (specialist_id, *LIVE_STATUSES)):
        if v["id"] == exclude_id:
            continue
        s, e = span_of(v)
        if s < end and start < e:
            return f"another visit ({VISIT_NAMES[v['visit_type']]}, {when_text(v)})"
    for b in q("SELECT * FROM specialist_blocks WHERE user_id=?", (specialist_id,)):
        s, e = datetime.fromisoformat(b["starts_at"]), datetime.fromisoformat(b["ends_at"])
        if s < end and start < e:
            if b["part"]:
                half = {"AM": "10am–2pm", "PM": "2pm–6pm", "FD": "all day"}[b["part"]]
                return f"marked themselves not free {half} ({s.strftime('%a %-d %b')})"
            return f"a time they aren't free ({s.strftime('%a %-d %b, %H:%M')}–{e.strftime('%H:%M')})"
    return None


# --------------------------------------------------------------------------
# Visits
# --------------------------------------------------------------------------

def log_visit(appt_id: int, user_id: int | None, action: str, note: str = "") -> None:
    run("INSERT INTO appointment_log (appt_id, user_id, action, note, created_at) "
        "VALUES (?,?,?,?,?)", (appt_id, user_id, action, note or None, now().isoformat()))


def create_visit(user_id: int, f: dict) -> tuple:
    """Book a visit from the booking form. Returns (id, None) or (None, error)."""
    country = str(f.get("country", "")).upper()
    if country not in COUNTRY_NAMES:
        return None, "Pick a country."
    vtype = str(f.get("visit_type", "")).upper()
    if vtype not in dict(VISIT_TYPES):
        return None, "Pick the visit type."
    duration = RESERVE_MIN
    team = specialists_in(country)
    if not team:
        return None, f"There's no On-Site Specialist in {COUNTRY_NAMES[country]} yet."
    try:
        spec = int(f.get("specialist_id") or (team[0]["user_id"] if len(team) == 1 else 0))
    except (TypeError, ValueError):
        spec = 0
    if spec not in {r["user_id"] for r in team}:
        return None, "Pick the specialist."
    try:
        start = local_start(country, str(f.get("date", "")), str(f.get("time", "")))
    except (ValueError, TypeError):
        return None, "Pick the date and time."
    if start < now():
        return None, "That time has already passed."
    need = {"customer_name": "the customer's name", "phone": "a phone number",
            "address": "the address"}
    clean = {k: str(f.get(k, "") or "").strip()[:300] for k in
             ("customer_name", "phone", "email", "address", "postal", "product",
              "issue", "order_no")}
    for k, label in need.items():
        if len(clean[k]) < 2:
            return None, f"Fill in {label}."
    busy = clash(spec, start, start + timedelta(minutes=duration))
    if busy:
        return None, f"That clashes: the specialist {clash_phrase(busy)}. Pick another time."
    cur = run(
        "INSERT INTO appointments (country, specialist_id, visit_type, starts_at, duration_min,"
        " customer_name, phone, email, address, postal, product, issue, order_no,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (country, spec, vtype, start.isoformat(), duration, clean["customer_name"],
         clean["phone"], clean["email"] or None, clean["address"], clean["postal"] or None,
         clean["product"] or None, clean["issue"] or None, clean["order_no"] or None,
         user_id, now().isoformat()))
    log_visit(cur.lastrowid, user_id, "booked")
    return cur.lastrowid, None


def map_link(v) -> str:
    where = ", ".join(x for x in (v["address"], v["postal"],
                                  COUNTRY_NAMES[v["country"]].split(" ", 1)[1]) if x)
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(where)


def visit_text(v, head: str = "") -> str:
    """A visit as plain text, for messages."""
    lines = [head] if head else []
    lines += [
        f"{COUNTRY_NAMES[v['country']]} · {VISIT_NAMES[v['visit_type']]} · {when_text(v)}",
        f"Customer: {v['customer_name']}" + (f" · {v['phone']}" if v["phone"] else ""),
        f"Address: {v['address']}" + (f" {v['postal']}" if v["postal"] else ""),
    ]
    if v["product"]:
        lines.append(f"Product: {v['product']}")
    if v["issue"]:
        lines.append(f"Issue: {v['issue']}")
    if v["order_no"]:
        lines.append(f"Order: {v['order_no']}")
    lines.append(f"Map: {map_link(v)}")
    return "\n".join(lines)


async def tell(bot, user_id: int | None, text: str, kb=None) -> None:
    if not user_id:
        return
    try:
        await bot.send_message(user_id, text, reply_markup=kb,
                               disable_web_page_preview=True)
    except Exception as e:
        log.info("Couldn't message %s about a visit: %s", user_id, e)


async def notify_booked(bot, appt_id: int) -> None:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    await tell(bot, v["specialist_id"],
               visit_text(v, "📅 New on-site visit") + f"\n\nBooked by {booker_name(v['created_by'])}.")


def request_reschedule(appt_id: int, user_id: int, day: str, hhmm: str) -> tuple:
    """SH or FS ask to move a visit; the specialist accepts or declines."""
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["status"] != "SCHEDULED":
        return False, "Only a scheduled visit can be moved."
    try:
        start = local_start(v["country"], day, hhmm)
    except (ValueError, TypeError):
        return False, "Pick the new date and time."
    if start < now():
        return False, "That time has already passed."
    busy = clash(v["specialist_id"], start, start + timedelta(minutes=v["duration_min"]),
                 exclude_id=appt_id)
    if busy:
        return False, f"That clashes: the specialist {clash_phrase(busy)}."
    run("UPDATE appointments SET proposed_starts_at=?, proposed_by=? WHERE id=?",
        (start.isoformat(), user_id, appt_id))
    log_visit(appt_id, user_id, "asked to move", start.strftime("%a %-d %b, %H:%M"))
    return True, None


async def notify_reschedule(bot, appt_id: int) -> None:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    new = datetime.fromisoformat(v["proposed_starts_at"])
    who = display_name_of(v["proposed_by"], "SH")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"vz:ra:{appt_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"vz:rd:{appt_id}")]])
    await tell(bot, v["specialist_id"],
               f"🔁 {who} asks to move a visit to {new.strftime('%a %-d %b, %H:%M')}\n\n"
               + visit_text(v), kb)


async def on_reschedule_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """vz:ra:<id> accept · vz:rd:<id> decline — the specialist answers."""
    query = update.callback_query
    _, action, raw = query.data.split(":")
    appt_id = int(raw)
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["specialist_id"] != query.from_user.id:
        await query.answer("That's not your visit.", show_alert=True)
        return
    if not v["proposed_starts_at"]:
        await query.answer("Already answered.")
        return
    new = datetime.fromisoformat(v["proposed_starts_at"])
    asker = v["proposed_by"]
    if action == "ra":
        busy = clash(v["specialist_id"], new, new + timedelta(minutes=v["duration_min"]),
                     exclude_id=appt_id)
        if busy:
            await query.answer(f"You now have {busy} then — decline instead.", show_alert=True)
            return
        async with write_lock:
            run("UPDATE appointments SET starts_at=?, proposed_starts_at=NULL, "
                "proposed_by=NULL WHERE id=?", (new.isoformat(), appt_id))
            log_visit(appt_id, query.from_user.id, "accepted the move",
                      new.strftime("%a %-d %b, %H:%M"))
        answer = f"✅ {display_name_of(query.from_user.id, 'The specialist')} accepted"
    else:
        async with write_lock:
            run("UPDATE appointments SET proposed_starts_at=NULL, proposed_by=NULL "
                "WHERE id=?", (appt_id,))
            log_visit(appt_id, query.from_user.id, "declined the move")
        answer = f"❌ {display_name_of(query.from_user.id, 'The specialist')} declined"
    await query.answer()
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    await query.edit_message_text(
        ("✅ Moved." if action == "ra" else "❌ Kept the original time.") + "\n\n" + visit_text(v),
        disable_web_page_preview=True)
    await tell(context.bot, asker,
               f"{answer} moving {v['customer_name']}'s visit.\n\n" + visit_text(v))


def cancel_visit(appt_id: int, user_id: int, reason: str = "") -> tuple:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["status"] != "SCHEDULED":
        return False, "Only a scheduled visit can be cancelled."
    run("UPDATE appointments SET status='CANCELLED', proposed_starts_at=NULL, "
        "proposed_by=NULL WHERE id=?", (appt_id,))
    log_visit(appt_id, user_id, "cancelled", reason)
    return True, None


async def notify_cancelled(bot, appt_id: int, reason: str = "") -> None:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    who = display_name_of(q1("SELECT user_id FROM appointment_log WHERE appt_id=? "
                             "AND action='cancelled' ORDER BY id DESC LIMIT 1",
                             (appt_id,))["user_id"], "SH")
    await tell(bot, v["specialist_id"],
               f"🚫 {who} cancelled a visit" + (f": {reason}" if reason else "")
               + "\n\n" + visit_text(v))


# --------------------------------------------------------------------------
# Times the specialist isn't free
# --------------------------------------------------------------------------

# The specialist marks days by halves: not free 10am–2pm (AM), 2pm–6pm (PM)
# or all day (FD).
DAY_PARTS = {"AM": ("10:00", "14:00"), "PM": ("14:00", "18:00"), "FD": ("00:00", "23:59")}
PART_NAMES = {"AM": "Not free AM", "PM": "Not free PM", "FD": "Not free all day"}
AVAIL_DAYS = 5
WORK_DAY = ("10:00", "18:00")    # the hours availability is shown for
AVAIL_TEXT = {"": "Available 10am–6pm", "AM": "Available 2pm–6pm",
              "PM": "Available 10am–2pm", "FD": "Not available"}


def hm(t: datetime) -> str:
    """10am, 12:30pm."""
    h = t.hour % 12 or 12
    return f"{h}{'' if t.minute == 0 else f':{t.minute:02d}'}{'am' if t.hour < 12 else 'pm'}"


def free_windows(spec_id: int, country: str, day: str) -> list:
    """The open stretches of the working day: blocked times and booked visits
    taken off. Gaps under 30 minutes don't count."""
    ws, we = local_start(country, day, WORK_DAY[0]), local_start(country, day, WORK_DAY[1])
    busy = []
    marks = ",".join("?" * len(LIVE_STATUSES))
    for v in q(f"SELECT * FROM appointments WHERE specialist_id=? AND status IN ({marks})",
               (spec_id, *LIVE_STATUSES)):
        busy.append(span_of(v))
    for b in q("SELECT * FROM specialist_blocks WHERE user_id=?", (spec_id,)):
        busy.append((datetime.fromisoformat(b["starts_at"]), datetime.fromisoformat(b["ends_at"])))
    busy = sorted((max(s, ws), min(e, we)) for s, e in busy if s < we and ws < e)
    out, cursor = [], ws
    for s, e in busy:
        if s > cursor:
            out.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < we:
        out.append((cursor, we))
    return [(s, e) for s, e in out if (e - s) >= timedelta(minutes=30)]


def free_text(windows: list) -> str:
    if not windows:
        return "Not available"
    return "Available " + ", ".join(f"{hm(s)}–{hm(e)}" for s, e in windows)


def day_part(user_id: int, country: str, day: str) -> str:
    """'AM', 'PM', 'FD' or '' for a day the specialist marked."""
    tz = COUNTRY_TZ[country]
    for b in q("SELECT * FROM specialist_blocks WHERE user_id=? AND part IS NOT NULL", (user_id,)):
        if datetime.fromisoformat(b["starts_at"]).astimezone(tz).date().isoformat() == day:
            return b["part"]
    return ""


def set_day_part(user_id: int, country: str, day: str, part: str) -> tuple:
    """Mark a day AM / PM / FD, or FREE to clear it. Refused over a booked visit."""
    if part not in DAY_PARTS and part != "FREE":
        return False, "Pick AM, PM, FD or Free."
    tz = COUNTRY_TZ[country]
    old = [b for b in q("SELECT * FROM specialist_blocks WHERE user_id=? AND part IS NOT NULL",
                        (user_id,))
           if datetime.fromisoformat(b["starts_at"]).astimezone(tz).date().isoformat() == day]
    if part != "FREE":
        s, e = DAY_PARTS[part]
        try:
            start, end = local_start(country, day, s), local_start(country, day, e)
        except (ValueError, TypeError):
            return False, "Pick a day."
        marks = ",".join("?" * len(LIVE_STATUSES))
        for v in q(f"SELECT * FROM appointments WHERE specialist_id=? AND status IN ({marks})",
                   (user_id, *LIVE_STATUSES)):
            vs, ve = span_of(v)
            if vs < end and start < ve:
                return False, (f"You have a visit then ({when_text(v)}). Ask SH to move it "
                               "first.")
    for b in old:
        run("DELETE FROM specialist_blocks WHERE id=?", (b["id"],))
    if part != "FREE":
        run("INSERT INTO specialist_blocks (user_id, starts_at, ends_at, note, part, created_at) "
            "VALUES (?,?,?,?,?,?)", (user_id, start.isoformat(), end.isoformat(),
                                     None, part, now().isoformat()))
    return True, None


def add_block(user_id: int, country: str, day: str, start_hhmm: str, end_hhmm: str,
              note: str = "") -> tuple:
    try:
        s = local_start(country, day, start_hhmm)
        e = local_start(country, day, end_hhmm)
    except (ValueError, TypeError):
        return None, "Pick the date and times."
    if e <= s:
        return None, "The end has to be after the start."
    marks = ",".join("?" * len(LIVE_STATUSES))
    for v in q(f"SELECT * FROM appointments WHERE specialist_id=? AND status IN ({marks})",
               (user_id, *LIVE_STATUSES)):
        vs, ve = span_of(v)
        if vs < e and s < ve:
            return None, (f"You have a visit then ({when_text(v)}). Ask SH to move it "
                          "before blocking this time.")
    cur = run("INSERT INTO specialist_blocks (user_id, starts_at, ends_at, note, created_at) "
              "VALUES (?,?,?,?,?)", (user_id, s.isoformat(), e.isoformat(),
                                     (note or "").strip()[:200] or None, now().isoformat()))
    return cur.lastrowid, None


def remove_block(user_id: int, block_id: int) -> bool:
    return run("DELETE FROM specialist_blocks WHERE id=? AND user_id=?",
               (block_id, user_id)).rowcount > 0


# --------------------------------------------------------------------------
# The App: a week of visits and blocked times, for bookers and specialists
# --------------------------------------------------------------------------

def visit_role(user_id: int) -> str:
    """'booker' (SH, FS, admins), 'specialist', or '' (no On-Site screens)."""
    if may_book(user_id):
        return "booker"
    return "specialist" if is_specialist(user_id) else ""


def visit_item(v, role: str) -> dict:
    s, e = span_of(v)
    proposed = ""
    if v["proposed_starts_at"]:
        proposed = datetime.fromisoformat(v["proposed_starts_at"]).strftime("%a %-d %b, %H:%M")
    return {
        "kind": "visit", "id": v["id"], "start": s.strftime("%H:%M"), "end": e.strftime("%H:%M"),
        "type": VISIT_NAMES[v["visit_type"]], "status": v["status"],
        "customer": v["customer_name"], "phone": v["phone"] or "", "email": v["email"] or "",
        "address": v["address"], "postal": v["postal"] or "", "product": v["product"] or "",
        "issue": v["issue"] or "", "order": v["order_no"] or "", "map": map_link(v),
        "specialist": display_name_of(v["specialist_id"], ""),
        "bookedBy": booker_name(v["created_by"]) if v["created_by"] else "", "proposed": proposed,
        "canChange": role == "booker" and v["status"] == "SCHEDULED",
    }


def visits_payload(user_id: int, week: str = "", country: str = "", spec: str = "",
                   avail: str = "") -> dict:
    role = visit_role(user_id)
    countries = [{"code": c, "name": COUNTRY_NAMES[c],
                  "specialists": [{"id": r["user_id"], "name": r["display_name"] or r["name"]}
                                  for r in specialists_in(c)]} for c, _, _, _ in COUNTRIES]
    if role == "specialist":
        me = q1("SELECT country FROM agents WHERE user_id=?", (user_id,))
        country = (me["country"] if me else "") or "SG"
        spec_id = user_id
    else:
        if country not in COUNTRY_NAMES:
            # FS open on their own store's country; SH and admins on SG.
            me = q1("SELECT country FROM agents WHERE user_id=?", (user_id,))
            country = (me["country"] if me else "") or "SG"
        team = next(c["specialists"] for c in countries if c["code"] == country)
        try:
            spec_id = int(spec)
        except (TypeError, ValueError):
            spec_id = 0
        if spec_id not in {s["id"] for s in team}:
            spec_id = team[0]["id"] if team else 0
    tz = COUNTRY_TZ[country]
    today = datetime.now(tz).date()
    try:
        first = date.fromisoformat(week)
    except (TypeError, ValueError):
        first = today
    first = first - timedelta(days=first.weekday())
    days = [{"date": (first + timedelta(days=i)).isoformat(),
             "label": (first + timedelta(days=i)).strftime("%a %-d %b"),
             "today": first + timedelta(days=i) == today, "items": []} for i in range(7)]
    by_day = {d["date"]: d for d in days}
    if spec_id:
        for v in q("SELECT * FROM appointments WHERE specialist_id=? AND status<>'CANCELLED'",
                   (spec_id,)):
            key = datetime.fromisoformat(v["starts_at"]).astimezone(tz).date().isoformat()
            if key in by_day:
                by_day[key]["items"].append(visit_item(v, role))
        for d in days:
            d["free"] = free_text(free_windows(spec_id, country, d["date"]))
    for d in days:
        d["items"].sort(key=lambda it: it["start"])
    availability, a_prev, a_next = [], "", ""
    if role == "specialist":
        try:
            a0 = max(date.fromisoformat(avail), today)
        except (TypeError, ValueError):
            a0 = today
        for i in range(AVAIL_DAYS):
            d = a0 + timedelta(days=i)
            part = day_part(user_id, country, d.isoformat())
            availability.append({"date": d.isoformat(), "label": d.strftime("%a %-d %b"),
                                 "part": part, "text": AVAIL_TEXT[part]})
        a_prev = max(today, a0 - timedelta(days=AVAIL_DAYS)).isoformat() if a0 > today else ""
        a_next = (a0 + timedelta(days=AVAIL_DAYS)).isoformat()
    return {
        "availability": availability, "availPrev": a_prev, "availNext": a_next,
        "role": role, "countries": countries, "country": country, "specId": spec_id,
        "types": [{"key": k, "name": n} for k, n in VISIT_TYPES],
        "weekStart": first.isoformat(), "prev": (first - timedelta(days=7)).isoformat(),
        "next": (first + timedelta(days=7)).isoformat(),
        "weekLabel": f"{first.strftime('%-d %b')} – {(first + timedelta(days=6)).strftime('%-d %b')}",
        "today": today.isoformat(), "days": days,
    }


# --------------------------------------------------------------------------
# The specialist's day in the bot: /visits, Arrived, Leave, payment, report
# --------------------------------------------------------------------------

OUTCOMES = [("RESOLVED", "✅ Resolved"), ("RETURN", "🔁 Needs a return visit"),
            ("REPLACE", "📦 Needs a replacement unit")]
OUTCOME_NAMES = dict(OUTCOMES)
PAY_METHODS = [("CASH", "Cash"), ("PAYNOW", "PayNow"), ("OTHER", "Other")]
CURRENCY = {"SG": "SGD", "MY": "MYR", "HK": "HKD", "TH": "THB"}
VISIT_AMOUNT = 280
# The customer sign-off form the specialist fills in on site (Zoho).
VISIT_FORM_URL = (os.environ.get("ONSITE_FORM_URL", "").strip() or
                  "https://forms.zohopublic.com/tatchuanacoustic/form/test/formperma/"
                  "iNsu3uIgPk9pCmF7VejBqy1sgbvkZisp4s3IqxD4hcM")


def mins_text(minutes: int) -> str:
    h, m = divmod(max(0, minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def visit_minutes(v) -> int:
    if not v["arrived_at"]:
        return 0
    end = datetime.fromisoformat(v["left_at"]) if v["left_at"] else now()
    return int((end - datetime.fromisoformat(v["arrived_at"])).total_seconds() // 60)


def visit_buttons(v, user_id: int) -> InlineKeyboardMarkup:
    rows = []
    mine = v["specialist_id"] == user_id
    if mine and v["status"] == "SCHEDULED":
        rows.append([InlineKeyboardButton("📍 Arrived", callback_data=f"vz:ar:{v['id']}")])
    if mine and v["status"] == "ONSITE":
        rows.append([InlineKeyboardButton("📝 Open the visit form", url=VISIT_FORM_URL)])
        rows.append([InlineKeyboardButton("🏁 Leave", callback_data=f"vz:lv:{v['id']}")])
    if (v["status"] == "COMPLETED" and v["outcome"] in ("RETURN", "REPLACE")
            and not v["followup_done"] and may_book(user_id)):
        rows.append([InlineKeyboardButton("✅ Follow-up done", callback_data=f"vz:fd:{v['id']}")])
    rows.append([InlineKeyboardButton("◀️ All visits", callback_data="vz:lst")])
    return InlineKeyboardMarkup(rows)


def visit_detail(v) -> str:
    text = visit_text(v)
    if v["status"] == "ONSITE":
        text += f"\n\n📍 On-Site since {datetime.fromisoformat(v['arrived_at']).strftime('%H:%M')}"
    if v["status"] == "COMPLETED":
        text += "\n\n" + report_lines(v)
    return text


def report_lines(v) -> str:
    lines = [OUTCOME_NAMES.get(v["outcome"], "Completed")]
    if v["arrived_at"] and v["left_at"]:
        a = datetime.fromisoformat(v["arrived_at"]).strftime("%H:%M")
        b = datetime.fromisoformat(v["left_at"]).strftime("%H:%M")
        lines.append(f"Arrived {a} · Left {b} ({mins_text(visit_minutes(v))})")
    if v["outcome"] == "RESOLVED":
        lines.append(f"Paid: {money_text(v['amount_cents'], v['currency'])} by "
                     f"{dict(PAY_METHODS).get(v['pay_method'], v['pay_method'])}"
                     if v["amount_cents"] else "No charge")
    elif v["followup_done"]:
        lines.append("Follow-up: done")
    else:
        need = "the return visit" if v["outcome"] == "RETURN" else "the replacement unit"
        lines.append(f"Follow-up for SH: arrange {need}.")
    lines.append(f"Specialist: {display_name_of(v['specialist_id'], '')}")
    return "\n".join(lines)


def visits_list(user_id: int) -> tuple:
    """The specialist's visits from today on; for bookers, everyone's, plus
    open follow-ups."""
    rows = []
    if is_specialist(user_id) and not may_book(user_id):
        found = q("SELECT * FROM appointments WHERE specialist_id=? AND status IN "
                  "('SCHEDULED','ONSITE') ORDER BY starts_at", (user_id,))
        title = "📍 My visits"
    else:
        found = q("SELECT * FROM appointments WHERE status IN ('SCHEDULED','ONSITE') "
                  "ORDER BY starts_at")
        title = "📍 On-Site visits"
        follow = q("SELECT * FROM appointments WHERE status='COMPLETED' AND "
                   "outcome IN ('RETURN','REPLACE') AND followup_done=0 ORDER BY left_at")
        for v in follow:
            rows.append([InlineKeyboardButton(
                f"⚠️ Follow-up · {v['customer_name']} · {OUTCOME_NAMES[v['outcome']][2:]}"[:60],
                callback_data=f"vz:v:{v['id']}")])
    for v in found[:40]:
        s = datetime.fromisoformat(v["starts_at"])
        mark = "📍 " if v["status"] == "ONSITE" else ""
        rows.append([InlineKeyboardButton(
            f"{mark}{s.strftime('%a %-d %b %H:%M')} · {v['customer_name']}"[:60],
            callback_data=f"vz:v:{v['id']}")])
    if not rows:
        return f"{title}\n\nNothing coming up.", None
    return (f"{title}\n\nTap one for the details." +
            ("\nBook or move visits in the App." if may_book(user_id) else "")), InlineKeyboardMarkup(rows)


async def cmd_visits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    if not visit_role(uid):
        return
    text, kb = visits_list(uid)
    await update.message.reply_text(text, reply_markup=kb)


async def edit_or_send(query, text: str, kb=None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=kb, disable_web_page_preview=True)
    except BadRequest:
        pass


async def on_visit_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """vz:lst · vz:v view · vz:ar arrived · vz:lv leave ·
    vz:oc outcome · vz:ok suggested amount · vz:nc no charge · vz:pm method ·
    vz:fd follow-up done."""
    query = update.callback_query
    uid = query.from_user.id
    parts = query.data.split(":")
    action = parts[1]
    if action == "lst":
        await query.answer()
        text, kb = visits_list(uid)
        await edit_or_send(query, text, kb)
        return
    v = q1("SELECT * FROM appointments WHERE id=?", (int(parts[2]),))
    if not v or not visit_role(uid):
        await query.answer("That visit is gone.", show_alert=True)
        return
    mine = v["specialist_id"] == uid
    if action == "v":
        await query.answer()
        await edit_or_send(query, visit_detail(v), visit_buttons(v, uid))
        return
    if action == "fd":
        if not may_book(uid):
            await query.answer("For SH, FS and admins.", show_alert=True)
            return
        async with write_lock:
            run("UPDATE appointments SET followup_done=1 WHERE id=?", (v["id"],))
            log_visit(v["id"], uid, "follow-up done")
        await query.answer("Marked done ✓")
        v = q1("SELECT * FROM appointments WHERE id=?", (v["id"],))
        await edit_or_send(query, visit_detail(v), visit_buttons(v, uid))
        return
    if not mine:
        await query.answer("That's another specialist's visit.", show_alert=True)
        return

    if action == "ar" and v["status"] == "SCHEDULED":
        async with write_lock:
            run("UPDATE appointments SET status='ONSITE', arrived_at=? WHERE id=?",
                (now().isoformat(), v["id"]))
            log_visit(v["id"], uid, "arrived")
        await query.answer("Arrived ✓")
        v = q1("SELECT * FROM appointments WHERE id=?", (v["id"],))
        await edit_or_send(query, "✅ Arrived. Please check the customer's details:\n\n"
                           + visit_detail(v), visit_buttons(v, uid))
        return
    if action == "lv" and v["status"] == "ONSITE":
        await query.answer()
        await edit_or_send(query, f"🏁 Leaving {v['customer_name']} after "
                           f"{mins_text(visit_minutes(v))}. How did it go?",
                           InlineKeyboardMarkup(
                               [[InlineKeyboardButton(n, callback_data=f"vz:oc:{v['id']}:{k}")]
                                for k, n in OUTCOMES]))
        return
    if action == "oc" and v["status"] == "ONSITE" and parts[3] in OUTCOME_NAMES:
        outcome = parts[3]
        async with write_lock:
            run("UPDATE appointments SET outcome=?, left_at=? WHERE id=?",
                (outcome, now().isoformat(), v["id"]))
        await query.answer()
        if outcome != "RESOLVED":
            await finish_visit(context.bot, query, v["id"], uid)
            return
        v = q1("SELECT * FROM appointments WHERE id=?", (v["id"],))
        minutes = visit_minutes(v)
        suggest = visit_charge(v["country"], minutes)
        rows = []
        if suggest:
            cents, cur = suggest
            async with write_lock:
                run("UPDATE appointments SET amount_cents=?, currency=? WHERE id=?",
                    (cents, cur, v["id"]))
            rows.append([InlineKeyboardButton(f"✅ Charge {money_text(cents, cur)}",
                                              callback_data=f"vz:ok:{v['id']}")])
            head = f"Time on site {mins_text(minutes)}, so the charge is {money_text(cents, cur)}."
        else:
            head = (f"Time on site {mins_text(minutes)}. Pricing for "
                    f"{COUNTRY_NAMES[v['country']]} isn't set yet, so type the amount.")
        rows.append([InlineKeyboardButton("✏️ A different amount" if suggest else "✏️ Type the amount",
                                          callback_data=f"vz:am:{v['id']}")])
        rows.append([InlineKeyboardButton("No charge", callback_data=f"vz:nc:{v['id']}")])
        await edit_or_send(query, head, InlineKeyboardMarkup(rows))
        return
    if action == "ok" and v["status"] == "ONSITE":
        await query.answer()
        await ask_method(query, v["id"])
        return
    if action == "nc" and v["status"] == "ONSITE":
        async with write_lock:
            run("UPDATE appointments SET amount_cents=0, currency=NULL, pay_method=NULL "
                "WHERE id=?", (v["id"],))
        await query.answer()
        await finish_visit(context.bot, query, v["id"], uid)
        return
    if action == "pm" and v["status"] == "ONSITE" and parts[3] in dict(PAY_METHODS):
        async with write_lock:
            run("UPDATE appointments SET pay_method=? WHERE id=?", (parts[3], v["id"]))
        await query.answer()
        await finish_visit(context.bot, query, v["id"], uid)
        return
    await query.answer("That step is already done.")


async def ask_method(query, appt_id: int) -> None:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    await edit_or_send(
        query, f"How did they pay {money_text(v['amount_cents'], v['currency'])}?",
        InlineKeyboardMarkup([[InlineKeyboardButton(n, callback_data=f"vz:pm:{appt_id}:{k}")
                               for k, n in PAY_METHODS]]))


async def on_amount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """vz:am:<id> — type the amount instead of the suggested one."""
    query = update.callback_query
    v = q1("SELECT * FROM appointments WHERE id=?", (int(query.data.split(":")[2]),))
    if not v or v["specialist_id"] != query.from_user.id or v["status"] != "ONSITE":
        await query.answer("That step is already done.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["visit_amount"] = v["id"]
    cur = CURRENCY.get(v["country"], "SGD")
    await edit_or_send(query, f"Type the amount in {cur}, e.g. 150 or 99.50.\n/cancel to stop")
    return VISIT_AMOUNT


async def on_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    appt_id = context.user_data.get("visit_amount")
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,)) if appt_id else None
    if not v:
        return ConversationHandler.END
    raw = update.message.text.strip().replace(",", "")
    raw = re.sub(r"^[A-Za-z$ ]+", "", raw)
    try:
        cents = round(float(raw) * 100)
    except ValueError:
        cents = -1
    if cents < 0 or cents > 10_000_000:
        await update.message.reply_text("Type just the number, e.g. 150. Or /cancel.")
        return VISIT_AMOUNT
    context.user_data.pop("visit_amount", None)
    cur = CURRENCY.get(v["country"], "SGD")
    async with write_lock:
        run("UPDATE appointments SET amount_cents=?, currency=? WHERE id=?", (cents, cur, appt_id))
    if cents == 0:
        await update.message.reply_text("No charge noted.")
        await finish_visit(context.bot, None, appt_id, update.effective_user.id)
        return ConversationHandler.END
    await update.message.reply_text(
        f"How did they pay {money_text(cents, cur)}?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            n, callback_data=f"vz:pm:{appt_id}:{k}") for k, n in PAY_METHODS]]))
    return ConversationHandler.END


async def finish_visit(bot, query, appt_id: int, uid: int) -> None:
    """Mark it Completed and send the visit report to SH (whoever booked it,
    and the visit report chat if one is set with /visitchat)."""
    async with write_lock:
        run("UPDATE appointments SET status='COMPLETED', left_at=COALESCE(left_at, ?) "
            "WHERE id=?", (now().isoformat(), appt_id))
        v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
        log_visit(appt_id, uid, "completed", OUTCOME_NAMES.get(v["outcome"], "")[2:])
    report = "🏁 Visit completed\n\n" + visit_text(v) + "\n\n" + report_lines(v)
    done = "✅ Done, the report has gone to SH. Thank you!\n\n" + report_lines(v)
    if query is not None:
        await edit_or_send(query, done)
    else:
        await tell(bot, uid, done)
    await tell(bot, v["created_by"], report)
    where = setting("visit_report_chat", "")
    if where:
        try:
            target = json.loads(where)
            kw = {"message_thread_id": target["thread"]} if target.get("thread") else {}
            await bot.send_message(target["chat"], report, disable_web_page_preview=True, **kw)
        except Exception as e:
            log.info("Couldn't post the visit report: %s", e)


async def cmd_visitchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/visitchat (admins, inside a group) — post visit reports here as well.
    /visitchat off — stop."""
    if not is_admin(update.effective_user.id):
        return
    chat = update.effective_chat
    if context.args and context.args[0].lower() == "off":
        async with write_lock:
            set_setting("visit_report_chat", "")
        await update.message.reply_text("Visit reports now go only to whoever booked the visit.")
        return
    if chat.type not in (constants.ChatType.GROUP, constants.ChatType.SUPERGROUP):
        await update.message.reply_text("Send this inside the SH group where reports should go.")
        return
    thread = update.message.message_thread_id if update.message.is_topic_message else None
    async with write_lock:
        set_setting("visit_report_chat", json.dumps({"chat": chat.id, "thread": thread}))
    await update.message.reply_text("✅ On-Site visit reports will be posted here too.")
