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
    ("TH_EMSPHERE", "TH", "Sonos Store EMSPHERE"),
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
    return admin_for(user_id, "SH", "FS", "ONSITE") or bool(depts_of(user_id) & {"SH", "FS"})


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
    if not admin_for(query.from_user.id, "ONSITE"):
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
    if query.from_user.id != uid and not admin_for(query.from_user.id, "FS"):
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
    if "FS" not in depts_of(uid) and not admin_for(uid, "FS"):
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
    if not admin_for(update.effective_user.id, "ONSITE", "FS"):
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


# Reminders for the specialist, as they work: confirm the date with the customer
# a couple of days before, and tell them you're on the way about an hour before.
CONFIRM_DAYS, CONFIRM_HOUR, ETA_MIN = 2, 10, 60


def reminder_due(v, at: datetime) -> tuple:
    """(kind, when it's due) for each reminder this visit gets: none for one
    booked after that reminder's time."""
    start = span_of(v)[0]
    confirm = datetime.combine(start.date() - timedelta(days=CONFIRM_DAYS),
                               time(CONFIRM_HOUR), tzinfo=start.tzinfo)
    # When it got its time: a handover is created first and booked later.
    row = q1("SELECT created_at FROM appointment_log WHERE appt_id=? AND action='booked' "
             "ORDER BY id DESC LIMIT 1", (v["id"],))
    booked = datetime.fromisoformat(row["created_at"] if row else v["created_at"])
    out = []
    if booked < confirm <= at < start - timedelta(hours=12):
        out.append(("confirm", confirm))
    eta = start - timedelta(minutes=ETA_MIN)
    if booked < eta <= at < start:
        out.append(("eta", eta))
    return out


def reminder_text(v, kind: str) -> str:
    start = span_of(v)[0]
    if kind == "confirm":
        head = (f"📞 Visit on {start.strftime('%a %-d %b, %H:%M')}: please confirm the date "
                "and time with the customer.")
    else:
        head = (f"🚗 Visit at {start.strftime('%H:%M')}, in about an hour: let the customer "
                "know you're on the way.")
    return visit_text(v, head)


async def job_visit_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every few minutes: send the reminders that are due. Each is sent once
    per visit time, so a visit that moves gets them again for its new time."""
    at = now()
    for v in q("SELECT * FROM appointments WHERE status='SCHEDULED' AND specialist_id IS NOT NULL"):
        for kind, _ in reminder_due(v, at):
            key = f"visit_reminded:{v['id']}:{kind}"
            if setting(key, "") == v["starts_at"]:
                continue
            async with write_lock:
                set_setting(key, v["starts_at"])
            await tell(context.bot, v["specialist_id"], reminder_text(v, kind),
                       visit_buttons(v, v["specialist_id"]))


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
    if action == "ra" and setting(f"visit_pin:{appt_id}", ""):
        await pin_next_visit(context.bot, appt_id)


def cancel_visit(appt_id: int, user_id: int, reason: str = "") -> tuple:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["status"] not in ("SCHEDULED",) + OPEN_STATUSES:
        return False, "Only a visit that hasn't started can be cancelled."
    run("UPDATE appointments SET status='CANCELLED', proposed_starts_at=NULL, "
        "proposed_by=NULL WHERE id=?", (appt_id,))
    log_visit(appt_id, user_id, "cancelled", reason)
    return True, None


async def notify_cancelled(bot, appt_id: int, reason: str = "") -> None:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    who = display_name_of(q1("SELECT user_id FROM appointment_log WHERE appt_id=? "
                             "AND action='cancelled' ORDER BY id DESC LIMIT 1",
                             (appt_id,))["user_id"], "SH")
    await close_handover_msgs(bot, appt_id, "🚫 Cancelled")
    await unpin_visit(bot, appt_id)
    await tell(bot, v["specialist_id"],
               f"🚫 {who} cancelled a visit" + (f": {reason}" if reason else "")
               + "\n\n" + (visit_text(v) if has_time(appt_id) else handover_text(v, True)))


# --------------------------------------------------------------------------
# Handovers: SH or FS pass a customer to On-Site with no date. Whichever
# specialist is free takes it, agrees the time with the customer and sets it.
# No database change: two more statuses on appointments, and until a time is
# set, starts_at holds when it was handed over.
# --------------------------------------------------------------------------

OPEN_STATUSES = ("WAITING", "ARRANGING")


def has_time(appt_id: int) -> bool:
    """Whether a real date and time was ever set (booked directly, or set later)."""
    return bool(q1("SELECT 1 FROM appointment_log WHERE appt_id=? AND action='booked' LIMIT 1",
                   (appt_id,)))


def handover_note(appt_id: int) -> str:
    row = q1("SELECT note FROM appointment_log WHERE appt_id=? AND action='handover' "
             "ORDER BY id LIMIT 1", (appt_id,))
    return (row["note"] if row else "") or ""


def handover_text(v, full: bool) -> str:
    """A handover as a message. Without full: no phone or address (for
    specialists who haven't taken it)."""
    lines = [f"{COUNTRY_NAMES[v['country']]} · {VISIT_NAMES.get(v['visit_type'], v['visit_type'])}",
             f"Customer: {v['customer_name']}" + (f" · {v['phone']}" if full and v["phone"] else "")]
    if full:
        lines.append(f"Address: {v['address']}" + (f" {v['postal']}" if v["postal"] else ""))
    elif v["postal"]:
        lines.append(f"Area: {v['postal']}")
    if v["product"]:
        lines.append(f"Product: {v['product']}")
    if v["issue"]:
        lines.append(f"Issue: {v['issue']}")
    note = handover_note(v["id"])
    if note:
        lines.append(f"Note: {note}")
    if v["order_no"]:
        lines.append(f"Order: {v['order_no']}")
    if v["created_by"]:
        lines.append(f"From {booker_name(v['created_by'])}")
    if full:
        lines.append(f"Map: {map_link(v)}")
    return "\n".join(lines)


def create_handover(user_id: int, f: dict) -> tuple:
    """Hand a customer over to On-Site. Returns (id, None) or (None, error)."""
    country = str(f.get("country", "")).upper()
    if country not in COUNTRY_NAMES:
        return None, "Pick a country."
    vtype = str(f.get("visit_type", "")).upper()
    if vtype not in dict(VISIT_TYPES):
        return None, "Pick the visit type."
    clean = {k: str(f.get(k, "") or "").strip()[:300] for k in
             ("customer_name", "phone", "email", "address", "postal", "product",
              "issue", "order_no", "note")}
    need = {"customer_name": "the customer's name", "phone": "a phone number",
            "product": "the product", "issue": "the issue", "address": "the location"}
    for k, label in need.items():
        if len(clean[k]) < 2:
            return None, f"Fill in {label}."
    cur = run(
        "INSERT INTO appointments (country, specialist_id, visit_type, starts_at, duration_min,"
        " customer_name, phone, email, address, postal, product, issue, order_no, status,"
        " created_by, created_at) VALUES (?,NULL,?,?,?,?,?,?,?,?,?,?,?,'WAITING',?,?)",
        (country, vtype, now().isoformat(), RESERVE_MIN, clean["customer_name"], clean["phone"],
         clean["email"] or None, clean["address"], clean["postal"] or None, clean["product"],
         clean["issue"], clean["order_no"] or None, user_id, now().isoformat()))
    log_visit(cur.lastrowid, user_id, "handover", clean["note"])
    return cur.lastrowid, None


def take_kb(appt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🙋 I'll take it", callback_data=f"vz:tk:{appt_id}")]])


def app_kb() -> InlineKeyboardMarkup | None:
    url = app_url()
    return InlineKeyboardMarkup([[InlineKeyboardButton("📱 Open My visits", web_app=WebAppInfo(url=url))]]) if url else None


async def notify_handover(bot, appt_id: int, skip: int = 0) -> None:
    """DM every specialist in that country, with I'll take it."""
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["status"] != "WAITING":
        return
    sent = []
    for r in specialists_in(v["country"]):
        if r["user_id"] == skip:
            continue
        try:
            m = await bot.send_message(r["user_id"], "📋 New On-Site handover\n\n" + handover_text(v, False),
                                       reply_markup=take_kb(appt_id), disable_web_page_preview=True)
            sent.append([r["user_id"], m.message_id])
        except Exception as e:
            log.info("Couldn't tell %s about handover %s: %s", r["user_id"], appt_id, e)
    async with write_lock:
        set_setting(f"handover_msgs:{appt_id}", json.dumps(sent))


async def close_handover_msgs(bot, appt_id: int, line: str, keep: int = 0) -> None:
    """Take the I'll take it button off the specialists' messages."""
    try:
        sent = json.loads(setting(f"handover_msgs:{appt_id}", "") or "[]")
    except ValueError:
        sent = []
    if not sent:
        return
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    for uid, mid in sent:
        if uid == keep:
            continue
        try:
            await bot.edit_message_text(f"{line}\n\n" + handover_text(v, False), chat_id=uid,
                                        message_id=mid, disable_web_page_preview=True)
        except Exception:
            pass
    async with write_lock:
        set_setting(f"handover_msgs:{appt_id}", "")


def take_handover(appt_id: int, user_id: int) -> tuple:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["status"] not in OPEN_STATUSES:
        return False, "That handover is no longer open."
    if not is_specialist(user_id):
        return False, "Only On-Site specialists take handovers."
    me = q1("SELECT country FROM agents WHERE user_id=?", (user_id,))
    if ((me["country"] if me else "") or "SG") != v["country"]:
        return False, f"That one is for {COUNTRY_NAMES[v['country']]}."
    if v["status"] == "ARRANGING":
        return False, ("You already have it." if v["specialist_id"] == user_id
                       else f"{display_name_of(v['specialist_id'], 'Someone')} already took it.")
    cur = run("UPDATE appointments SET status='ARRANGING', specialist_id=? WHERE id=? AND status='WAITING'",
              (user_id, appt_id))
    if not cur.rowcount:
        return False, "Someone just took it."
    log_visit(appt_id, user_id, "taken")
    return True, None


async def after_take(bot, appt_id: int, user_id: int) -> None:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    await close_handover_msgs(bot, appt_id, f"🙋 Taken by {display_name_of(user_id, 'a specialist')}", keep=user_id)
    await tell(bot, user_id, "🙋 It's yours. Call the customer, agree a time, then set it in "
               "the App → My visits.\n\n" + handover_text(v, True), app_kb())


def give_back(appt_id: int, user_id: int) -> tuple:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["status"] != "ARRANGING" or v["specialist_id"] != user_id:
        return False, "Only the specialist who took it can give it back."
    run("UPDATE appointments SET status='WAITING', specialist_id=NULL WHERE id=?", (appt_id,))
    log_visit(appt_id, user_id, "given back")
    return True, None


def set_time(appt_id: int, user_id: int, day: str, hhmm: str) -> tuple:
    """The time agreed with the customer: the handover becomes a booked visit."""
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["status"] != "ARRANGING":
        return False, "That one isn't waiting for a time."
    if v["specialist_id"] != user_id and not admin_for(user_id, "ONSITE"):
        return False, "Only the specialist who took it sets the time."
    try:
        start = local_start(v["country"], day, hhmm)
    except (ValueError, TypeError):
        return False, "Pick the date and time."
    if start < now():
        return False, "That time has already passed."
    busy = clash(v["specialist_id"], start, start + timedelta(minutes=v["duration_min"]), exclude_id=appt_id)
    if busy:
        return False, f"That clashes: {clash_phrase(busy)}."
    run("UPDATE appointments SET status='SCHEDULED', starts_at=? WHERE id=?", (start.isoformat(), appt_id))
    log_visit(appt_id, user_id, "booked")
    return True, None


def onsite_fyi_ids() -> list:
    """Who gets the Booked FYI: admins looking after On-Site (e.g. Zoe), not
    the owner or Admin for all teams."""
    return [r["user_id"] for r in q("SELECT user_id FROM agents WHERE role='admin' AND status='active'")
            if not is_owner(r["user_id"]) and not is_full_admin(r["user_id"])
            and "ONSITE" in admin_teams(r["user_id"])]


async def notify_booked_fyi(bot, appt_id: int) -> None:
    """One line to On-Site admins, and to whoever handed it over."""
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    start = span_of(v)[0].astimezone(COUNTRY_TZ[v["country"]])
    text = (f"✅ Booked · {COUNTRY_NAMES[v['country']].split(' ')[0]} {start.strftime('%a %-d %b, %H:%M')}"
            f" · {VISIT_NAMES.get(v['visit_type'], v['visit_type'])}\n"
            f"{v['customer_name']} · {display_name_of(v['specialist_id'], '')}")
    told = set()
    for uid in onsite_fyi_ids() + ([v["created_by"]] if v["created_by"] else []):
        if uid and uid != v["specialist_id"] and uid not in told:
            told.add(uid)
            await tell(bot, uid, text)


async def on_take_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """vz:tk:<id> — a specialist takes a handover from their DM."""
    query = update.callback_query
    appt_id = int(query.data.split(":")[2])
    uid = update.effective_user.id
    async with write_lock:
        ok, err = take_handover(appt_id, uid)
    if not ok:
        await query.answer(err, show_alert=True)
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        return
    await query.answer("It's yours")
    try:
        await query.edit_message_reply_markup(None)
    except Exception:
        pass
    await after_take(context.bot, appt_id, uid)


HANDOVER_NUDGE_TIME = os.environ.get("HANDOVER_NUDGE_TIME", "").strip() or "11:00"


async def job_handover_nudge(context: ContextTypes.DEFAULT_TYPE) -> None:
    """11am on working days: handovers from earlier days nobody has taken, to
    On-Site admins. Once a day."""
    from ops import is_working_day
    today = now().date()
    if not is_working_day("SG", today) or setting("handover_nudge_done", "") == today.isoformat():
        return
    async with write_lock:
        set_setting("handover_nudge_done", today.isoformat())
    rows = [v for v in q("SELECT * FROM appointments WHERE status='WAITING' ORDER BY created_at")
            if datetime.fromisoformat(v["created_at"]).astimezone(TZ).date() < today]
    if not rows:
        return
    lines = [f"⏳ <b>Not taken yet</b> ({len(rows)})"]
    for v in rows:
        lines.append(f"{COUNTRY_NAMES[v['country']].split(' ')[0]} {esc(v['customer_name'])} · "
                     f"{VISIT_NAMES.get(v['visit_type'], v['visit_type'])} · handed over "
                     f"{datetime.fromisoformat(v['created_at']).astimezone(TZ).strftime('%a %-d %b')}"
                     f" by {esc(booker_name(v['created_by']) if v['created_by'] else 'SH')}")
    for uid in onsite_fyi_ids():
        try:
            await context.bot.send_message(uid, "\n".join(lines), parse_mode="HTML")
        except Exception as e:
            log.info("Couldn't nudge %s about handovers: %s", uid, e)


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
        "followup": URGENCY_NAMES.get(v["urgency"] or "MEDIUM", "") if v["followup_of"] else "",
    }


def open_item(v, user_id: int, role: str) -> dict:
    """A handover not booked yet. A specialist sees phone and address only
    once they've taken it."""
    mine = v["specialist_id"] == user_id
    full = role == "booker" or mine
    item = {
        "id": v["id"], "status": v["status"].lower(), "type": VISIT_NAMES.get(v["visit_type"], v["visit_type"]),
        "country": v["country"], "customer": v["customer_name"], "postal": v["postal"] or "",
        "product": v["product"] or "", "issue": v["issue"] or "", "order": v["order_no"] or "",
        "note": handover_note(v["id"]),
        "from": booker_name(v["created_by"]) if v["created_by"] else "",
        "handed": datetime.fromisoformat(v["created_at"]).astimezone(COUNTRY_TZ[v["country"]]).strftime("%a %-d %b, %H:%M"),
        "specialist": ("You" if mine else display_name_of(v["specialist_id"], "")) if v["specialist_id"] else "",
        "canTake": role == "specialist" and v["status"] == "WAITING",
        "canSet": v["status"] == "ARRANGING" and (mine or admin_for(user_id, "ONSITE")),
        "canGiveBack": v["status"] == "ARRANGING" and mine,
        "canCancel": role == "booker",
    }
    if full:
        item.update(phone=v["phone"] or "", email=v["email"] or "", address=v["address"], map=map_link(v))
    return item


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
        for v in q("SELECT * FROM appointments WHERE specialist_id=? AND status NOT IN "
                   "('CANCELLED','WAITING','ARRANGING')", (spec_id,)):
            key = datetime.fromisoformat(v["starts_at"]).astimezone(tz).date().isoformat()
            if key in by_day:
                by_day[key]["items"].append(visit_item(v, role))
    for d in days:
        d["items"].sort(key=lambda it: it["start"])
    # Handovers not booked yet: a specialist sees their country's waiting ones
    # and the ones they took; bookers see the country they're looking at.
    if role == "specialist":
        open_rows = q("SELECT * FROM appointments WHERE country=? AND (status='WAITING' OR "
                      "(status='ARRANGING' AND specialist_id=?)) ORDER BY created_at", (country, user_id))
    else:
        open_rows = q("SELECT * FROM appointments WHERE country=? AND status IN ('WAITING','ARRANGING') "
                      "ORDER BY created_at", (country,))
    if role == "specialist":
        fu_rows = q("SELECT * FROM appointments WHERE specialist_id=? AND followup_of IS NOT NULL "
                    "AND status IN ('SCHEDULED','ARRANGING','ONSITE')", (user_id,))
    else:
        fu_rows = q("SELECT * FROM appointments WHERE country=? AND followup_of IS NOT NULL "
                    "AND status IN ('SCHEDULED','ARRANGING','ONSITE')", (country,))
    fu_rows = sorted(fu_rows, key=followup_sort)
    return {
        "myDay": today_payload(user_id, country) if role == "specialist" else None,
        "myRequests": my_onsite_requests(user_id) if role == "specialist" else [],
        "myClaims": my_cab_claims(user_id) if role == "specialist" else [],
        "followups": [followup_item(v, user_id, role) for v in fu_rows],
        "open": [open_item(v, user_id, role) for v in open_rows if not v["followup_of"]],
        "myCountry": country if role == "specialist" else "",
        "role": role, "countries": countries, "country": country, "specId": spec_id,
        "types": [{"key": k, "name": n} for k, n in VISIT_TYPES],
        "weekStart": first.isoformat(), "prev": (first - timedelta(days=7)).isoformat(),
        "next": (first + timedelta(days=7)).isoformat(),
        "weekLabel": f"{first.strftime('%-d %b')} – {(first + timedelta(days=6)).strftime('%-d %b')}",
        "today": today.isoformat(), "days": days,
        "canTrack": bool(tracker_countries(user_id)),
        "me": ({"name": display_name_of(user_id, "You"),
                "teams": [DEPT_NAMES[k] for k, _ in DEPARTMENTS if k in depts_of(user_id)],
                "country": country, "countryName": COUNTRY_NAMES.get(country, country)}
               if role == "specialist" else None),
    }


# --------------------------------------------------------------------------
# The tracker: a week of visits across countries, with counts
# --------------------------------------------------------------------------

def tracker_countries(user_id: int) -> list:
    """Admins and SH see every country; a specialist sees their own, and FS
    their store's country."""
    if admin_for(user_id, "SH", "FS", "ONSITE") or "SH" in depts_of(user_id):
        return [c for c, _, _, _ in COUNTRIES]
    me = q1("SELECT country, store FROM agents WHERE user_id=?", (user_id,))
    mine = set()
    if is_specialist(user_id):
        mine.add((me["country"] if me else "") or "SG")
    if "FS" in depts_of(user_id):
        mine.add(FS_STORE_COUNTRY.get((me["store"] if me else "") or "", "") or "SG")
    return [c for c, _, _, _ in COUNTRIES if c in mine]


def tracker_status(v) -> str:
    if v["status"] in OPEN_STATUSES:
        return v["status"].lower()
    if v["status"] == "CANCELLED":
        return "cancelled"
    if v["status"] == "ONSITE":
        return "onsite"
    if v["status"] == "COMPLETED":
        if v["outcome"] in ("RETURN", "REPLACE") and not v["followup_done"]:
            return "followup"
        return "done"
    return "booked"


def tracker_item(v, user_id: int, all_countries: bool) -> dict:
    tz = COUNTRY_TZ[v["country"]]
    start = span_of(v)[0].astimezone(tz)
    hhmm = lambda iso: datetime.fromisoformat(iso).astimezone(tz).strftime("%H:%M") if iso else ""
    mine = v["specialist_id"] == user_id
    steps = []
    timed = v["status"] not in OPEN_STATUSES and has_time(v["id"])
    handed = q1("SELECT 1 FROM appointment_log WHERE appt_id=? AND action='handover'", (v["id"],))
    if v["created_by"]:
        steps.append(f"{'Handed over' if handed else 'Booked'} by {booker_name(v['created_by'])} · "
                     f"{datetime.fromisoformat(v['created_at']).astimezone(tz).strftime('%a %-d %b')}")
    if handed and v["specialist_id"]:
        steps.append(f"🙋 Taken by {display_name_of(v['specialist_id'], '')}")
    if v["arrived_at"]:
        steps.append(f"🚗 Arrived {hhmm(v['arrived_at'])}")
    if v["left_at"]:
        steps.append(f"🏁 Left {hhmm(v['left_at'])} · {mins_text(visit_minutes(v))}")
    if v["status"] == "COMPLETED":
        if v["outcome"] == "RESOLVED":
            steps.append(f"Paid {money_text(v['amount_cents'], v['currency'])} · "
                         f"{dict(PAY_METHODS).get(v['pay_method'], v['pay_method'] or '')}"
                         if v["amount_cents"] else "No charge")
        elif v["followup_done"]:
            steps.append("Follow-up done")
    why = ""
    if v["status"] == "CANCELLED":
        row = q1("SELECT note FROM appointment_log WHERE appt_id=? AND action='cancelled' "
                 "ORDER BY id DESC LIMIT 1", (v["id"],))
        why = (row["note"] if row else "") or ""
        steps.append("Cancelled" + (f": {why}" if why else ""))
    item = {
        "id": v["id"], "country": v["country"], "date": start.date().isoformat(),
        "start": start.strftime("%H:%M") if timed else "",
        "when": start.strftime("%a %-d %b · %H:%M") if timed else "Not booked yet",
        "type": VISIT_NAMES.get(v["visit_type"], v["visit_type"]),
        "status": tracker_status(v),
        "outcome": OUTCOME_NAMES.get(v["outcome"], "")[2:] if v["outcome"] else "",
        "specialist": "You" if mine else display_name_of(v["specialist_id"], "") if v["specialist_id"] else "",
        "since": hhmm(v["arrived_at"]) if v["status"] == "ONSITE" else "", "why": why,
        "steps": steps, "product": v["product"] or "", "issue": v["issue"] or "",
        "note": handover_note(v["id"]),
        "order": v["order_no"] or "",
    }
    # Everyone sees the customer's name, order and area, to spot double
    # bookings. Phone, email and address only for admins and SH (all
    # countries), whoever booked it, and the specialist doing it.
    item.update(customer=v["customer_name"], postal=v["postal"] or "")
    if all_countries or mine or v["created_by"] == user_id:
        item.update(phone=v["phone"] or "", email=v["email"] or "",
                    address=v["address"], map=map_link(v))
    return item


def tracker_payload(user_id: int, week: str = "") -> dict:
    countries = tracker_countries(user_id)
    if not countries:
        return {"countries": [], "items": [], "days": []}
    all_countries = len(countries) == len(COUNTRIES)
    today = datetime.now(COUNTRY_TZ[countries[0]] if not all_countries else TZ).date()
    try:
        first = date.fromisoformat(week)
    except (TypeError, ValueError):
        first = today
    first = first - timedelta(days=first.weekday())
    last = first + timedelta(days=6)
    # A loose window on the stored time, then each visit by its own country's date.
    lo = (first - timedelta(days=2)).isoformat()
    hi = (last + timedelta(days=2)).isoformat() + "T99"
    marks = ",".join("?" * len(countries))
    rows = q(f"SELECT * FROM appointments WHERE country IN ({marks}) AND starts_at >= ? "
             f"AND starts_at <= ? AND status NOT IN ('WAITING','ARRANGING') ORDER BY starts_at",
             (*countries, lo, hi))
    # Not booked yet: every open handover, whatever the week.
    open_items = [tracker_item(v, user_id, all_countries) for v in
                  q(f"SELECT * FROM appointments WHERE country IN ({marks}) AND status IN "
                    f"('WAITING','ARRANGING') ORDER BY created_at", tuple(countries))]
    items = []
    for v in rows:
        it = tracker_item(v, user_id, all_countries)
        if first.isoformat() <= it["date"] <= last.isoformat():
            items.append(it)
    items.sort(key=lambda it: (it["date"], it["start"]))
    counted = [it for it in items if it["status"] != "cancelled"]
    labels = {(first + timedelta(days=i)).isoformat(): (first + timedelta(days=i)).strftime("%a %-d %b")
              for i in range(7)}
    return {
        "countries": [{"code": c, "name": COUNTRY_NAMES[c],
                       "count": sum(1 for it in counted if it["country"] == c)} for c in countries],
        "allCountries": all_countries,
        "total": len(counted),
        "statuses": {**{k: sum(1 for it in open_items if it["status"] == k) for k in ("waiting", "arranging")},
                     **{k: sum(1 for it in items if it["status"] == k)
                        for k in ("booked", "onsite", "done", "followup", "cancelled")}},
        "items": items, "open": open_items, "dayLabels": labels,
        "weekStart": first.isoformat(), "prev": (first - timedelta(days=7)).isoformat(),
        "next": (first + timedelta(days=7)).isoformat(),
        "weekLabel": f"{first.strftime('%a %-d %b')} – {last.strftime('%a %-d %b')}",
    }


# --------------------------------------------------------------------------
# The specialist's day in the bot: /visits, Arrived, Leave, payment, report
# --------------------------------------------------------------------------

OUTCOMES = [("RESOLVED", "✅ Resolved"), ("RETURN", "🔁 Not finished, need to come back"),
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
        rows.append([InlineKeyboardButton("🚗 Arrived", callback_data=f"vz:ar:{v['id']}")])
    if mine and v["status"] == "ONSITE":
        rows.append([InlineKeyboardButton("📝 Open the visit form", url=VISIT_FORM_URL)])
        rows.append([InlineKeyboardButton("🏁 Leave", callback_data=f"vz:lv:{v['id']}")])
    if mine and v["status"] in ("ONSITE", "COMPLETED"):
        rows.append([InlineKeyboardButton("📦 Replacement / payment request", callback_data=f"oq:n:{v['id']}")])
        rows.append([InlineKeyboardButton("🚕 Cab claim", callback_data=f"cb:n:{v['id']}")])
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
    elif v["outcome"] == "RETURN" and next_visit_of(v["id"]):
        nxt = next_visit_of(v["id"])
        lines.append("No charge for this visit.")
        why = nf_reason_text(v["id"])
        if why:
            lines.append(f"Why: {why}")
        lines.append(f"Next visit: {when_text(nxt)}" if nxt["status"] != "ARRANGING"
                     else "Next visit: the customer will confirm the date")
        lines.append(f"Urgency: {URGENCY_NAMES.get(nxt['urgency'] or 'MEDIUM')}")
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
    found = sorted(found, key=followup_sort)
    for v in found[:40]:
        s = datetime.fromisoformat(v["starts_at"])
        mark = "📍 " if v["status"] == "ONSITE" else ""
        if v["followup_of"]:
            mark += f"🔴 FOLLOW-UP · {URGENCY_NAMES.get(v['urgency'] or 'MEDIUM', '').upper()} · "
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
        if outcome == "RETURN":
            await nf_step(query, v["id"], "r")
            return
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
        query, f"How did they pay {money_text(v['amount_cents'], v['currency'])}?", pay_method_kb(appt_id))


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
        reply_markup=pay_method_kb(appt_id))
    return ConversationHandler.END


async def finish_visit(bot, query, appt_id: int, uid: int) -> None:
    """Mark it Completed and send the visit report to SH (whoever booked it,
    and the visit report chat if one is set with /visitchat)."""
    async with write_lock:
        run("UPDATE appointments SET status='COMPLETED', left_at=COALESCE(left_at, ?) "
            "WHERE id=?", (now().isoformat(), appt_id))
        v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
        log_visit(appt_id, uid, "completed", OUTCOME_NAMES.get(v["outcome"], "")[2:])
    await unpin_visit(bot, appt_id)
    report = "🏁 Visit completed\n\n" + visit_text(v) + "\n\n" + report_lines(v)
    done = "✅ Done, the report has gone to SH. Thank you!\n\n" + report_lines(v)
    kb = None
    if v["outcome"] == "REPLACE":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📦 Send a replacement / payment request",
                                                         callback_data=f"oq:n:{appt_id}")]])
    if query is not None and not getattr(query.message, "photo", None):
        await edit_or_send(query, done, kb)
    else:
        if query is not None:
            try:
                await query.edit_message_reply_markup(None)
            except Exception:
                pass
        await tell(bot, uid, done, kb)
    if v["outcome"] == "RESOLVED":
        await send_review_qr(bot, uid)
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
    if not admin_for(update.effective_user.id, "SH"):
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


# --------------------------------------------------------------------------
# Not finished: no charge, say why, and book the next visit before leaving.
# The next visit is a new appointment (followup_of = this one) for the same
# specialist, pinned in their chat with the bot until it's done.
# --------------------------------------------------------------------------

NF_REASONS = [("PART", "Need a part"), ("TIME", "Out of time"),
              ("NOTREADY", "Customer not ready"), ("OTHER", "Other")]
NF_REASON_NAMES = dict(NF_REASONS)
URGENCY = [("HIGH", "High"), ("MEDIUM", "Medium"), ("LOW", "Low")]
URGENCY_NAMES = dict(URGENCY)
URGENCY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
# Quick picks for the next visit's time; ✏️ Type a time takes anything else.
NEXT_TIMES = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]


def t12(hm: str) -> str:
    """'14:00' -> '2pm', '10:30' -> '10:30am'."""
    h, m = int(hm[:2]), hm[3:5]
    return f"{h % 12 or 12}{'' if m == '00' else ':' + m}{'am' if h < 12 else 'pm'}"


def read_any_time(text: str) -> str | None:
    """Whatever someone types for a time -> 'HH:MM', or None. '7.15pm', '1930',
    '230', '2:30', '10' all work. With no am/pm, 10-11 is morning and 12 or
    1-9 is afternoon/evening (visits run from about 10am to 6pm)."""
    t = text.strip().lower().replace(" ", "").replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm|a|p)?", t)
    if not m:
        return None
    h, mins, mer = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "")[:1]
    if mins > 59 or h > 23:
        return None
    if mer:
        if h < 1 or h > 12:
            return None
        h = (h % 12) + (12 if mer == "p" else 0)
    elif 1 <= h <= 9:
        h += 12
    return f"{h:02d}:{mins:02d}"
NEXT_DAYS = 14
ONS_TEXT, ONS_PHOTO = 290, 291      # typed notes / amounts, and photos, in chat


def followup_sort(v) -> tuple:
    """Follow-ups first, most urgent first, then by date and time."""
    if not v["followup_of"]:
        return (1, 9, v["starts_at"])
    return (0, URGENCY_RANK.get(v["urgency"] or "MEDIUM", 1), v["starts_at"])


def next_visit_of(appt_id: int):
    return q1("SELECT * FROM appointments WHERE followup_of=? AND status!='CANCELLED' "
              "ORDER BY id DESC LIMIT 1", (appt_id,))


def nf_reason_text(appt_id: int) -> str:
    row = q1("SELECT note FROM appointment_log WHERE appt_id=? AND action='not finished' "
             "ORDER BY id DESC LIMIT 1", (appt_id,))
    return (row["note"] if row else "") or ""


def nf_draft(appt_id: int) -> dict:
    try:
        return json.loads(setting(f"nf_draft:{appt_id}", "") or "{}")
    except ValueError:
        return {}


def save_nf_draft(appt_id: int, d: dict) -> None:
    set_setting(f"nf_draft:{appt_id}", json.dumps(d))


def followup_item(v, user_id: int, role: str) -> dict:
    tz = COUNTRY_TZ[v["country"]]
    mine = v["specialist_id"] == user_id
    item = {
        "id": v["id"], "of": v["followup_of"], "status": v["status"], "country": v["country"],
        "type": VISIT_NAMES.get(v["visit_type"], v["visit_type"]),
        "urgency": URGENCY_NAMES.get(v["urgency"] or "MEDIUM", "Medium"),
        "customer": v["customer_name"], "postal": v["postal"] or "", "product": v["product"] or "",
        "when": (span_of(v)[0].astimezone(tz).strftime("%a %-d %b, %H:%M")
                 if v["status"] != "ARRANGING" else "Customer will confirm the date"),
        "why": nf_reason_text(v["followup_of"]),
        "specialist": "You" if mine else display_name_of(v["specialist_id"], ""),
        "canSet": v["status"] == "ARRANGING" and (mine or admin_for(user_id, "ONSITE")),
    }
    if role == "booker" or mine:
        item.update(phone=v["phone"] or "", address=v["address"], map=map_link(v))
    return item


def book_followup(appt_id: int, user_id: int, d: dict) -> tuple:
    """Close a visit that didn't finish (no charge) and make the next one.
    d: reason, note, urgency, and date + time, or later=True.
    Returns (new id, None) or (None, error)."""
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["status"] != "ONSITE" or v["specialist_id"] != user_id:
        return None, "That visit isn't open on your side any more."
    reason = str(d.get("reason") or "")
    if reason not in NF_REASON_NAMES:
        return None, "Pick why it didn't finish."
    urgency = str(d.get("urgency") or "MEDIUM")
    if urgency not in URGENCY_NAMES:
        urgency = "MEDIUM"
    note = str(d.get("note") or "").strip()[:300]
    later = bool(d.get("later"))
    start = now()
    if not later:
        try:
            start = local_start(v["country"], str(d.get("date", "")), str(d.get("time", "")))
        except (ValueError, TypeError):
            return None, "Pick the date and time for the next visit."
        if start < now():
            return None, "That time has already passed."
        busy = clash(user_id, start, start + timedelta(minutes=v["duration_min"]), exclude_id=appt_id)
        if busy:
            return None, f"That clashes: you {clash_phrase(busy)}. Pick another time."
    cur = run(
        "INSERT INTO appointments (country, specialist_id, visit_type, starts_at, duration_min,"
        " customer_name, phone, email, address, postal, product, issue, order_no, status,"
        " created_by, created_at, urgency, followup_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (v["country"], user_id, v["visit_type"], start.isoformat(), v["duration_min"],
         v["customer_name"], v["phone"], v["email"], v["address"], v["postal"], v["product"],
         v["issue"], v["order_no"], "ARRANGING" if later else "SCHEDULED",
         v["created_by"] or user_id, now().isoformat(), urgency, appt_id))
    new_id = cur.lastrowid
    why = NF_REASON_NAMES[reason] + (f": {note}" if note else "")
    log_visit(new_id, user_id, "follow-up", f"of #{appt_id} · {why}")
    if not later:
        log_visit(new_id, user_id, "booked")
    run("UPDATE appointments SET outcome='RETURN', left_at=COALESCE(left_at, ?), amount_cents=0, "
        "currency=NULL, pay_method=NULL, followup_done=1 WHERE id=?", (now().isoformat(), appt_id))
    log_visit(appt_id, user_id, "not finished", why)
    set_setting(f"nf_draft:{appt_id}", "")
    return new_id, None


async def pin_next_visit(bot, appt_id: int) -> None:
    """Pin the next visit at the top of the specialist's chat with the bot."""
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["status"] != "SCHEDULED" or not v["specialist_id"]:
        return
    await unpin_visit(bot, appt_id)
    tz = COUNTRY_TZ[v["country"]]
    when = span_of(v)[0].astimezone(tz).strftime("%a %-d %b, %H:%M")
    lines = [f"📌 Next visit #{v['id']} · {when}",
             f"{COUNTRY_NAMES[v['country']]} · {VISIT_NAMES.get(v['visit_type'], v['visit_type'])}",
             f"{v['customer_name']}" + (f" · {v['postal']}" if v["postal"] else ""),
             f"🔴 Follow-up · {URGENCY_NAMES.get(v['urgency'] or 'MEDIUM')}"]
    if v["product"]:
        lines.append(f"Product: {v['product']}")
    why = nf_reason_text(v["followup_of"]) if v["followup_of"] else ""
    if why:
        lines.append(f"Last time: {why}")
    try:
        m = await bot.send_message(v["specialist_id"], "\n".join(lines),
                                   reply_markup=visit_buttons(v, v["specialist_id"]),
                                   disable_web_page_preview=True)
        await bot.pin_chat_message(v["specialist_id"], m.message_id, disable_notification=True)
        async with write_lock:
            set_setting(f"visit_pin:{appt_id}", f"{v['specialist_id']}:{m.message_id}")
    except Exception as e:
        log.info("Couldn't pin visit %s: %s", appt_id, e)


async def unpin_visit(bot, appt_id: int) -> None:
    raw = setting(f"visit_pin:{appt_id}", "")
    if not raw:
        return
    try:
        uid, mid = (int(x) for x in raw.split(":"))
        await bot.unpin_chat_message(chat_id=uid, message_id=mid)
    except Exception as e:
        log.info("Couldn't unpin visit %s: %s", appt_id, e)
    async with write_lock:
        set_setting(f"visit_pin:{appt_id}", "")


def nf_summary(v, d: dict) -> str:
    lines = [f"🔁 Not finished · {v['customer_name']} (#{v['id']})", "No charge for this visit."]
    if d.get("reason"):
        lines.append(f"Why: {NF_REASON_NAMES[d['reason']]}" + (f" · {d['note']}" if d.get("note") else ""))
    if d.get("urgency"):
        lines.append(f"Urgency: {URGENCY_NAMES[d['urgency']]}")
    if d.get("later"):
        lines.append("Next visit: the customer will confirm the date")
    elif d.get("date"):
        day = date.fromisoformat(d["date"]).strftime("%a %-d %b")
        lines.append(f"Next visit: {day}" + (f", {t12(d['time'])}" if d.get("time") else ""))
    return "\n".join(lines)


def nf_keyboard(v, step: str, d: dict) -> InlineKeyboardMarkup:
    vid = v["id"]
    back = {"r": f"vz:lv:{vid}", "u": f"vn:b:{vid}:r", "d": f"vn:b:{vid}:u",
            "t": f"vn:b:{vid}:d", "k": f"vn:b:{vid}:{'d' if d.get('later') else 't'}"}[step]
    rows = []
    if step == "r":
        rows = [[InlineKeyboardButton(("✅ " if d.get("reason") == k else "") + n,
                                      callback_data=f"vn:r:{vid}:{k}")] for k, n in NF_REASONS]
    elif step == "u":
        rows = [[InlineKeyboardButton(("✅ " if d.get("urgency") == k else "") + n,
                                      callback_data=f"vn:u:{vid}:{k}") for k, n in URGENCY]]
        rows.append([InlineKeyboardButton("✏️ Add a note" if not d.get("note") else "✏️ Change the note",
                                          callback_data=f"vn:n:{vid}")])
    elif step == "d":
        today = datetime.now(COUNTRY_TZ[v["country"]]).date()
        days = [today + timedelta(days=i) for i in range(NEXT_DAYS)]
        btns = [InlineKeyboardButton(x.strftime("%a %-d %b"), callback_data=f"vn:d:{vid}:{x.strftime('%Y%m%d')}")
                for x in days]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        rows.append([InlineKeyboardButton("🕓 Customer will confirm later", callback_data=f"vn:l:{vid}")])
    elif step == "t":
        times = NEXT_TIMES
        day = d.get("date", "")
        if day == datetime.now(COUNTRY_TZ[v["country"]]).date().isoformat():
            times = [t for t in NEXT_TIMES if local_start(v["country"], day, t) > now()]
        btns = [InlineKeyboardButton(t12(t), callback_data=f"vn:t:{vid}:{t.replace(':', '')}") for t in times]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        rows.append([InlineKeyboardButton("✏️ Type a time", callback_data=f"vn:x:{vid}")])
    elif step == "k":
        rows = [[InlineKeyboardButton("✅ Done, close this visit" if d.get("later")
                                      else "✅ Book the next visit", callback_data=f"vn:k:{vid}")]]
    rows.append([InlineKeyboardButton("‹ Back", callback_data=back)])
    return InlineKeyboardMarkup(rows)


NF_QUESTIONS = {"r": "Why didn't it finish?", "u": "How urgent is the next visit?",
                "d": "When is the next visit? Agree it with the customer before you leave.",
                "t": "What time?", "k": "All good?"}


async def nf_step(query, appt_id: int, step: str) -> None:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    d = nf_draft(appt_id)
    await edit_or_send(query, nf_summary(v, d) + "\n\n" + NF_QUESTIONS[step], nf_keyboard(v, step, d))


async def on_nf_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """vn:r reason · vn:u urgency · vn:d date · vn:t time · vn:l later ·
    vn:b back · vn:k book — the not-finished steps in /visits."""
    query = update.callback_query
    parts = query.data.split(":")
    action, appt_id = parts[1], int(parts[2])
    uid = query.from_user.id
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["specialist_id"] != uid or v["status"] != "ONSITE":
        await query.answer("That step is already done.", show_alert=True)
        return
    d = nf_draft(appt_id)
    nxt = None
    if action == "r" and parts[3] in NF_REASON_NAMES:
        d["reason"], nxt = parts[3], "u"
    elif action == "u" and parts[3] in URGENCY_NAMES:
        d["urgency"], nxt = parts[3], "d"
    elif action == "d":
        d["date"] = f"{parts[3][:4]}-{parts[3][4:6]}-{parts[3][6:]}"
        d.pop("later", None)
        nxt = "t"
    elif action == "t":
        d["time"] = f"{parts[3][:2]}:{parts[3][2:]}"
        nxt = "k"
    elif action == "l":
        d["later"] = True
        d.pop("date", None), d.pop("time", None)
        nxt = "k"
    elif action == "b":
        nxt = parts[3]
    elif action == "k":
        async with write_lock:
            new_id, err = book_followup(appt_id, uid, d)
        if err:
            await query.answer(err, show_alert=True)
            return
        await query.answer("Booked" if not d.get("later") else "Done")
        await finish_visit(context.bot, query, appt_id, uid)
        await pin_next_visit(context.bot, new_id)
        return
    if nxt is None:
        await query.answer()
        return
    async with write_lock:
        save_nf_draft(appt_id, d)
    await query.answer()
    await nf_step(query, appt_id, nxt)


async def on_nf_note_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """vn:n:<id> — type a note about why it didn't finish."""
    query = update.callback_query
    appt_id = int(query.data.split(":")[2])
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["specialist_id"] != query.from_user.id or v["status"] != "ONSITE":
        await query.answer("That step is already done.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["ons"] = {"kind": "nf_note", "id": appt_id}
    await edit_or_send(query, "Type the note in one message, e.g. bracket too short, bring the long one."
                              "\n/cancel to stop")
    return ONS_TEXT


async def on_nf_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """vn:x:<id> — type any time for the next visit."""
    query = update.callback_query
    appt_id = int(query.data.split(":")[2])
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["specialist_id"] != query.from_user.id or v["status"] != "ONSITE":
        await query.answer("That step is already done.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["ons"] = {"kind": "nf_time", "id": appt_id}
    await edit_or_send(query, "Type the time, e.g. 7pm, 9:30am or 1930.\n/cancel to stop")
    return ONS_TEXT


async def on_ons_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Typed text for the On-Site chat steps."""
    job = context.user_data.pop("ons", None) or {}
    text = (update.message.text or "").strip()
    if job.get("kind") == "nf_time":
        appt_id = job["id"]
        v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
        if not v or v["status"] != "ONSITE":
            return ConversationHandler.END
        hm = read_any_time(text)
        if not hm:
            context.user_data["ons"] = job
            await update.message.reply_text("I couldn't read that time. Try e.g. 7pm, 9:30am or 1930.\n/cancel to stop")
            return ONS_TEXT
        async with write_lock:
            d = nf_draft(appt_id)
            d["time"] = hm
            save_nf_draft(appt_id, d)
        await update.message.reply_text(nf_summary(v, d) + "\n\n" + NF_QUESTIONS["k"],
                                        reply_markup=nf_keyboard(v, "k", d))
        return ConversationHandler.END
    if job.get("kind") == "nf_note":
        appt_id = job["id"]
        v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
        if not v or v["status"] != "ONSITE":
            return ConversationHandler.END
        async with write_lock:
            d = nf_draft(appt_id)
            d["note"] = text[:300]
            save_nf_draft(appt_id, d)
        await update.message.reply_text(nf_summary(v, d) + "\n\n" + NF_QUESTIONS["u"],
                                        reply_markup=nf_keyboard(v, "u", d))
        return ConversationHandler.END
    return await ons_text_more(update, context, job, text)


ONS_TEXT_HANDLERS = {}     # kind -> async fn(update, context, job, text) -> state


async def ons_text_more(update, context, job: dict, text: str) -> int:
    """Other typed steps, by kind (requests, cab claims)."""
    fn = ONS_TEXT_HANDLERS.get(job.get("kind"))
    return await fn(update, context, job, text) if fn else ConversationHandler.END


# --------------------------------------------------------------------------
# The specialist's day in the App: Today, the visit screen (Arrived, a
# checklist, optional photos) and Leave. The checklists are DRAFT steps until
# the specialist confirms them. Ticks and photos are kept without new tables:
# ticks in settings, photos in the visit's log.
# --------------------------------------------------------------------------

CHECKLISTS = {
    "SETUP": [
        "Unbox and check for damage",
        "Place it and connect power (and the TV cable for a soundbar)",
        "Connect to Wi-Fi in the Sonos or Marshall app",
        "Update the firmware, and pair the sub or surrounds if any",
        "Test it with the customer and show them the app",
    ],
    "TROUBLESHOOT": [
        "Check the problem matches what the customer reported",
        "Check power and cables (power, HDMI eARC or optical, Ethernet if used)",
        "Check Wi-Fi: right network, good signal where it sits",
        "Open the Sonos or Marshall app and check it sees every product",
        "Update the firmware and the app",
        "Power cycle the product (unplug, wait, plug back in)",
        "Sub (Sonos or Marshall Sub 200): check it's paired, re-pair if needed",
        "Factory reset and set up again, only if needed",
        "Test it with the customer: TV audio, music, sub with the soundbar",
        "Record the result",
    ],
}
CHECKLIST_DRAFT = True     # flip once the specialist has confirmed the steps


def checklist_of(v) -> list:
    return CHECKLISTS.get(v["visit_type"], CHECKLISTS["SETUP"])


def ticks_of(appt_id: int) -> list:
    try:
        return [int(i) for i in json.loads(setting(f"visit_checks:{appt_id}", "") or "[]")]
    except (ValueError, TypeError):
        return []


def set_tick(appt_id: int, user_id: int, i: int, on: bool) -> tuple:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["specialist_id"] != user_id:
        return False, "That's not your visit."
    if not 0 <= i < len(checklist_of(v)):
        return False, "That step isn't on the list."
    ticks = set(ticks_of(appt_id))
    (ticks.add if on else ticks.discard)(i)
    set_setting(f"visit_checks:{appt_id}", json.dumps(sorted(ticks)))
    return True, None


def photos_of(appt_id: int) -> int:
    return q1("SELECT COUNT(*) AS n FROM appointment_log WHERE appt_id=? AND action='photo'",
              (appt_id,))["n"]


def arrive(appt_id: int, user_id: int) -> tuple:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["specialist_id"] != user_id:
        return False, "That's not your visit."
    if v["status"] != "SCHEDULED":
        return False, "Already marked."
    run("UPDATE appointments SET status='ONSITE', arrived_at=? WHERE id=?", (now().isoformat(), appt_id))
    log_visit(appt_id, user_id, "arrived")
    return True, None


def visit_screen(v, user_id: int) -> dict:
    """Everything the visit screen in the App needs, for the specialist's own visit."""
    tz = COUNTRY_TZ[v["country"]]
    start = span_of(v)[0].astimezone(tz)
    hhmm = lambda iso: datetime.fromisoformat(iso).astimezone(tz).strftime("%H:%M") if iso else ""
    item = {
        "id": v["id"], "status": v["status"], "country": v["country"],
        "type": VISIT_NAMES.get(v["visit_type"], v["visit_type"]),
        "when": start.strftime("%a %-d %b, %H:%M"), "start": start.strftime("%H:%M"),
        "customer": v["customer_name"], "phone": v["phone"] or "", "email": v["email"] or "",
        "address": v["address"], "postal": v["postal"] or "", "product": v["product"] or "",
        "issue": v["issue"] or "", "order": v["order_no"] or "", "map": map_link(v),
        "note": handover_note(v["id"]), "arrived": hhmm(v["arrived_at"]),
        "minutes": visit_minutes(v), "onSite": mins_text(visit_minutes(v)),
        "followup": URGENCY_NAMES.get(v["urgency"] or "MEDIUM", "") if v["followup_of"] else "",
        "why": nf_reason_text(v["followup_of"]) if v["followup_of"] else "",
        "checklist": checklist_of(v), "ticks": ticks_of(v["id"]), "draft": CHECKLIST_DRAFT,
        "photos": photos_of(v["id"]), "formUrl": VISIT_FORM_URL,
        "mine": v["specialist_id"] == user_id,
        "canRequest": may_request(v, user_id), "reviewQr": bool(REVIEW_URL),
        "paynowReady": bool(PAYNOW_UEN) and v["country"] == "SG",
        "canClaim": may_claim(v, user_id),
    }
    if v["status"] == "ONSITE":
        suggest = visit_charge(v["country"], max(1, visit_minutes(v)))
        item["suggest"] = suggest[0] if suggest else None
        item["currency"] = CURRENCY.get(v["country"], "SGD")
    if v["status"] == "COMPLETED":
        item["report"] = report_lines(v)
    return item


def today_payload(user_id: int, country: str) -> dict:
    """The specialist's Today: the next visit on top, then today's timeline."""
    tz = COUNTRY_TZ[country]
    today = datetime.now(tz).date()
    rows = q("SELECT * FROM appointments WHERE specialist_id=? AND status IN "
             "('SCHEDULED','ONSITE','COMPLETED')", (user_id,))
    todays = sorted((v for v in rows if span_of(v)[0].astimezone(tz).date() == today),
                    key=lambda v: v["starts_at"])
    upcoming = sorted((v for v in rows if v["status"] in ("SCHEDULED", "ONSITE")),
                      key=lambda v: (v["status"] != "ONSITE", v["starts_at"]))
    nxt = upcoming[0] if upcoming else None
    return {
        "label": today.strftime("%a %-d %b"),
        "next": visit_screen(nxt, user_id) if nxt else None,
        "items": [{"id": v["id"], "start": span_of(v)[0].astimezone(tz).strftime("%H:%M"),
                   "customer": v["customer_name"], "postal": v["postal"] or "",
                   "type": VISIT_NAMES.get(v["visit_type"], v["visit_type"]), "status": v["status"],
                   "followup": bool(v["followup_of"])} for v in todays],
    }


async def leave_from_app(appt_id: int, user_id: int, b: dict) -> dict:
    """Leave from the App: outcome, then the charge (Resolved), the next visit
    (Not finished) or nothing more (Replacement). Runs on the bot's loop."""
    bot = bot_ref()
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not v or v["specialist_id"] != user_id or v["status"] != "ONSITE":
        return {"ok": False, "error": "That visit isn't open on your side any more."}
    outcome = str(b.get("outcome") or "")
    if outcome not in OUTCOME_NAMES:
        return {"ok": False, "error": "Pick how it went."}
    if outcome == "RETURN":
        async with write_lock:
            run("UPDATE appointments SET left_at=? WHERE id=?", (now().isoformat(), appt_id))
            new_id, err = book_followup(appt_id, user_id, b)
        if err:
            return {"ok": False, "error": err}
        await finish_visit(bot, None, appt_id, user_id)
        await pin_next_visit(bot, new_id)
        return {"ok": True, "next": new_id}
    cents, cur, method = 0, None, None
    if outcome == "RESOLVED" and not b.get("noCharge"):
        try:
            cents = round(float(str(b.get("amount") or "0").replace(",", "")) * 100)
        except ValueError:
            cents = -1
        if cents <= 0 or cents > 10_000_000:
            return {"ok": False, "error": "Type the amount, or pick No charge."}
        method = str(b.get("method") or "")
        if method not in dict(PAY_METHODS):
            return {"ok": False, "error": "Pick how the customer paid."}
        cur = CURRENCY.get(v["country"], "SGD")
    async with write_lock:
        run("UPDATE appointments SET outcome=?, left_at=?, amount_cents=?, currency=?, pay_method=? "
            "WHERE id=?", (outcome, now().isoformat(), cents, cur, method, appt_id))
    await finish_visit(bot, None, appt_id, user_id)
    return {"ok": True}


async def on_visit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo sent to the bot while on site goes with that visit (optional)."""
    if update.effective_chat.type != constants.ChatType.PRIVATE or not update.message.photo:
        return
    uid = update.effective_user.id
    if await photo_for_other(update, context):
        return
    here = q("SELECT * FROM appointments WHERE specialist_id=? AND status='ONSITE'", (uid,))
    if len(here) != 1:
        return
    v = here[0]
    async with write_lock:
        log_visit(v["id"], uid, "photo", update.message.photo[-1].file_id)
    await update.message.reply_text(f"📷 Added to {v['customer_name']}'s visit "
                                    f"({photos_of(v['id'])} so far).")


ONS_PHOTO_HANDLERS = []    # async fn(update, context) -> True if it used the photo


async def photo_for_other(update, context) -> bool:
    """Other uses of a photo sent to the bot (e.g. a cab receipt)."""
    for fn in ONS_PHOTO_HANDLERS:
        if await fn(update, context):
            return True
    return False


# --------------------------------------------------------------------------
# Replacement / payment requests: a fixed format posted to the SH x Ops chat
# (set with /requestchat), with I'll handle it, Done and a Note back to the
# specialist. No customer phone or address in the post.
# --------------------------------------------------------------------------

RQ_NEEDS = [("REPLACEMENT", "Replacement unit"), ("PART", "Part"), ("OTHER", "Other")]
RQ_WARRANTY = [("IN", "In warranty"), ("OUT", "Out of warranty")]
RQ_PAYS = [("LINK", "Payment link"), ("PAYNOW", "PayNow"), ("NONE", "No charge")]
RQ_NEED_NAMES, RQ_WARRANTY_NAMES, RQ_PAYS_NAMES = dict(RQ_NEEDS), dict(RQ_WARRANTY), dict(RQ_PAYS)


def may_request(v, user_id: int) -> bool:
    return bool(v) and v["specialist_id"] == user_id and v["status"] in ("ONSITE", "COMPLETED")


def create_onsite_request(user_id: int, appt_id: int, f: dict) -> tuple:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not may_request(v, user_id):
        return None, "You can send a request for your own visit, on site or after it."
    need, warranty, pays = (str(f.get(k) or "") for k in ("need", "warranty", "pays_by"))
    if need not in RQ_NEED_NAMES:
        return None, "Pick what's needed."
    if warranty not in RQ_WARRANTY_NAMES:
        return None, "Pick in or out of warranty."
    if pays not in RQ_PAYS_NAMES:
        return None, "Pick how the customer pays."
    reason = str(f.get("reason") or "").strip()[:400]
    if len(reason) < 3:
        return None, "Say why in a few words."
    price = None
    raw = str(f.get("price") or "").replace(",", "").strip()
    if raw:
        try:
            price = round(float(re.sub(r"^[A-Za-z$ ]+", "", raw)) * 100)
        except ValueError:
            return None, "Type the price as a number, or leave it empty (to confirm)."
        if price < 0 or price > 10_000_000:
            return None, "That price doesn't look right."
    cur = run("INSERT INTO onsite_requests (appt_id, country, need, reason, warranty, price_cents, "
              "currency, pays_by, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (appt_id, v["country"], need, reason, warranty, price, CURRENCY.get(v["country"], "SGD"),
               pays, user_id, now().isoformat()))
    log_visit(appt_id, user_id, "request", f"#{cur.lastrowid} {RQ_NEED_NAMES[need]}")
    return cur.lastrowid, None


def rq_text(r) -> str:
    v = q1("SELECT * FROM appointments WHERE id=?", (r["appt_id"],))
    tz = COUNTRY_TZ[r["country"]]
    day = span_of(v)[0].astimezone(tz).strftime("%a %-d %b") if v else ""
    price = money_text(r["price_cents"], r["currency"]) if r["price_cents"] is not None else "To confirm"
    lines = [f"📦 <b>Replacement / payment request</b> · {COUNTRY_NAMES[r['country']].split(' ')[0]} {r['country']}",
             f"Visit #{r['appt_id']} · {day}"]
    if v and v["product"]:
        lines.append(f"Product: {esc(v['product'])}")
    lines += [f"Need: {RQ_NEED_NAMES[r['need']]}", f"Reason: {esc(r['reason'])}",
              f"Warranty: {RQ_WARRANTY_NAMES[r['warranty']]}", f"Price: {price}",
              f"Pays by: {RQ_PAYS_NAMES[r['pays_by']]}",
              f"From: {esc(display_name_of(r['created_by'], 'the specialist'))}", ""]
    if r["status"] == "OPEN":
        lines.append("🔸 Open · nobody on it yet")
    elif r["status"] == "TAKEN":
        lines.append(f"🙋 {esc(display_name_of(r['taken_by'], 'Someone'))} is handling it")
    else:
        lines.append(f"✅ Done by {esc(display_name_of(r['done_by'], 'Someone'))}")
    if r["note"]:
        lines.append(f"📝 {esc(r['note'])}")
    return "\n".join(lines)


def rq_buttons(r, bot_username: str = "") -> InlineKeyboardMarkup | None:
    rows = []
    if r["status"] == "OPEN":
        rows.append([InlineKeyboardButton("🙋 I'll handle it", callback_data=f"oq:t:{r['id']}")])
    if r["status"] == "TAKEN":
        rows.append([InlineKeyboardButton("✅ Done", callback_data=f"oq:d:{r['id']}")])
    if r["status"] != "DONE" and bot_username:
        rows.append([InlineKeyboardButton("📝 Note to the specialist",
                                          url=f"https://t.me/{bot_username}?start=oqn_{r['id']}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def post_onsite_request(bot, rid: int) -> bool:
    """Post to the request chat, or DM the On-Site admins if none is set."""
    r = q1("SELECT * FROM onsite_requests WHERE id=?", (rid,))
    where = setting("onsite_request_chat", "")
    if where:
        try:
            target = json.loads(where)
            kw = {"message_thread_id": target["thread"]} if target.get("thread") else {}
            m = await bot.send_message(target["chat"], rq_text(r), parse_mode=constants.ParseMode.HTML,
                                       reply_markup=rq_buttons(r, bot.username), **kw)
            async with write_lock:
                run("UPDATE onsite_requests SET chat_id=?, msg_id=? WHERE id=?", (m.chat_id, m.message_id, rid))
            return True
        except Exception as e:
            log.info("Couldn't post On-Site request %s: %s", rid, e)
    for uid in onsite_fyi_ids() or list(ADMIN_IDS):
        try:
            await bot.send_message(uid, rq_text(r) + "\n\n<i>No request chat is set yet: send "
                                   "/requestchat in the SH x Ops chat.</i>", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass
    return False


async def refresh_onsite_request(bot, rid: int) -> None:
    r = q1("SELECT * FROM onsite_requests WHERE id=?", (rid,))
    if not r or not r["chat_id"]:
        return
    try:
        await bot.edit_message_text(rq_text(r), chat_id=r["chat_id"], message_id=r["msg_id"],
                                    parse_mode=constants.ParseMode.HTML,
                                    reply_markup=rq_buttons(r, bot.username))
    except BadRequest:
        pass


async def on_rq_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """oq:t:<id> I'll handle it · oq:d:<id> Done — in the request chat."""
    query = update.callback_query
    _, action, raw = query.data.split(":")
    uid = query.from_user.id
    if agent_status(uid) != "active" and not is_owner(uid):
        await query.answer("Only SH Bot members can do this.", show_alert=True)
        return
    r = q1("SELECT * FROM onsite_requests WHERE id=?", (int(raw),))
    if not r:
        await query.answer("That request is gone.", show_alert=True)
        return
    who = display_name_of(uid, "Someone")
    async with write_lock:
        if action == "t":
            cur = run("UPDATE onsite_requests SET status='TAKEN', taken_by=?, taken_at=? WHERE id=? AND status='OPEN'",
                      (uid, now().isoformat(), r["id"]))
        else:
            cur = run("UPDATE onsite_requests SET status='DONE', done_by=?, done_at=? WHERE id=? AND status='TAKEN'",
                      (uid, now().isoformat(), r["id"]))
    if not cur.rowcount:
        await query.answer("Someone already did that.")
        await refresh_onsite_request(context.bot, r["id"])
        return
    await query.answer("On it" if action == "t" else "Done")
    await refresh_onsite_request(context.bot, r["id"])
    await tell(context.bot, r["created_by"],
               (f"🙋 {who} is handling your request for visit #{r['appt_id']} "
                f"({RQ_NEED_NAMES[r['need']]})." if action == "t" else
                f"✅ {who} marked your request for visit #{r['appt_id']} done "
                f"({RQ_NEED_NAMES[r['need']]})."))


async def start_rq_note(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str) -> int:
    """/start oqn_<id>, from 📝 Note to the specialist under a request."""
    uid = update.effective_user.id
    r = q1("SELECT * FROM onsite_requests WHERE id=?", (int(raw),)) if raw.isdigit() else None
    if not r or (agent_status(uid) != "active" and not is_owner(uid)):
        await update.message.reply_text("That request is gone.")
        return ConversationHandler.END
    context.user_data["ons"] = {"kind": "rq_note", "id": r["id"]}
    await update.message.reply_text(
        f"📝 Note to {display_name_of(r['created_by'], 'the specialist')} about visit #{r['appt_id']} "
        f"({RQ_NEED_NAMES[r['need']]}). Type it in one message, e.g. unit ships Fri, or payment link sent."
        "\n/cancel to stop")
    return ONS_TEXT


async def _rq_note_text(update, context, job, text) -> int:
    r = q1("SELECT * FROM onsite_requests WHERE id=?", (job["id"],))
    if not r or not text:
        await update.message.reply_text("Nothing was sent.")
        return ConversationHandler.END
    async with write_lock:
        run("UPDATE onsite_requests SET note=? WHERE id=?", (text[:500], r["id"]))
    await tell(context.bot, r["created_by"], f"📝 Note on your request for visit #{r['appt_id']}:\n\n{text[:500]}"
               f"\n— {display_name_of(update.effective_user.id, 'SH x Ops')}")
    await refresh_onsite_request(context.bot, r["id"])
    await update.message.reply_text("✅ Sent to the specialist, and added under the request.")
    return ConversationHandler.END


ONS_TEXT_HANDLERS["rq_note"] = _rq_note_text


async def cmd_requestchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/requestchat (admins, inside a group) — On-Site replacement / payment
    requests are posted here. /requestchat off — stop."""
    if not admin_for(update.effective_user.id, "ONSITE", "SH"):
        return
    chat = update.effective_chat
    if context.args and context.args[0].lower() == "off":
        async with write_lock:
            set_setting("onsite_request_chat", "")
        await update.message.reply_text("On-Site requests now go to the On-Site admins by DM.")
        return
    if chat.type not in (constants.ChatType.GROUP, constants.ChatType.SUPERGROUP):
        await update.message.reply_text("Send this inside the SH x Ops chat where requests should go.")
        return
    thread = update.message.message_thread_id if update.message.is_topic_message else None
    async with write_lock:
        set_setting("onsite_request_chat", json.dumps({"chat": chat.id, "thread": thread}))
    await update.message.reply_text("✅ On-Site replacement / payment requests will be posted here.")


# The request form in /visits: need → warranty → pays by → reason (typed) → send.
RQ_STEPS = [("need", RQ_NEEDS, "What's needed?"), ("warranty", RQ_WARRANTY, "Warranty?"),
            ("pays_by", RQ_PAYS, "How does the customer pay?")]


def rq_draft_key(appt_id: int, uid: int) -> str:
    return f"rq_draft:{appt_id}:{uid}"


def rq_draft(appt_id: int, uid: int) -> dict:
    try:
        return json.loads(setting(rq_draft_key(appt_id, uid), "") or "{}")
    except ValueError:
        return {}


def rq_chat_view(v, d: dict, step: int) -> tuple:
    """(text, keyboard) for step 0-2 (picks), 3 (reason), 4 (send)."""
    head = [f"📦 Replacement / payment request · visit #{v['id']}", f"{v['customer_name']}"
            + (f" · {v['product']}" if v["product"] else "")]
    names = {"need": RQ_NEED_NAMES, "warranty": RQ_WARRANTY_NAMES, "pays_by": RQ_PAYS_NAMES}
    for k, label in (("need", "Need"), ("warranty", "Warranty"), ("pays_by", "Pays by")):
        if d.get(k):
            head.append(f"{label}: {names[k][d[k]]}")
    if d.get("reason"):
        head.append(f"Reason: {d['reason']}")
    head.append("Price: to confirm")
    vid = v["id"]
    back = f"oq:s:{vid}:{step - 1}" if step > 0 else f"vz:v:{vid}"
    if step < 3:
        key, opts, question = RQ_STEPS[step]
        rows = [[InlineKeyboardButton(("✅ " if d.get(key) == k else "") + n, callback_data=f"oq:p:{vid}:{step}:{k}")]
                for k, n in opts]
        text = "\n".join(head) + "\n\n" + question
    elif step == 3:
        rows = [[InlineKeyboardButton("✏️ Type the reason" if not d.get("reason") else "✏️ Change the reason",
                                      callback_data=f"oq:r:{vid}")]]
        if d.get("reason"):
            rows.append([InlineKeyboardButton("Next ›", callback_data=f"oq:s:{vid}:4")])
        text = "\n".join(head) + "\n\nWhy is it needed? One line, e.g. won't power on, fault confirmed on site."
    else:
        rows = [[InlineKeyboardButton("✅ Send to SH x Ops", callback_data=f"oq:k:{vid}")]]
        text = "\n".join(head) + "\n\nSend it?"
    rows.append([InlineKeyboardButton("‹ Back", callback_data=back)])
    return text, InlineKeyboardMarkup(rows)


async def on_rq_form(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """oq:n:<visit> start · oq:p:<visit>:<step>:<key> pick · oq:s:<visit>:<step> go to
    a step · oq:k:<visit> send — the request form in /visits."""
    query = update.callback_query
    parts = query.data.split(":")
    action, vid, uid = parts[1], int(parts[2]), query.from_user.id
    v = q1("SELECT * FROM appointments WHERE id=?", (vid,))
    if not may_request(v, uid):
        await query.answer("That's not open for a request.", show_alert=True)
        return
    d = rq_draft(vid, uid)
    step = 0
    if action == "n":
        d = {}
    elif action == "p":
        step = int(parts[3])
        d[RQ_STEPS[step][0]] = parts[4]
        step += 1
    elif action == "s":
        step = max(0, min(4, int(parts[3])))
    elif action == "k":
        async with write_lock:
            rid, err = create_onsite_request(uid, vid, d)
            if not err:
                set_setting(rq_draft_key(vid, uid), "")
        if err:
            await query.answer(err, show_alert=True)
            return
        await query.answer("Sent")
        posted = await post_onsite_request(context.bot, rid)
        await edit_or_send(query, ("✅ Sent to SH x Ops. You'll get a message when someone picks it up."
                                   if posted else "✅ Sent to the On-Site admins (no request chat is set yet)."),
                           InlineKeyboardMarkup([[InlineKeyboardButton("◀️ All visits", callback_data="vz:lst")]]))
        return
    async with write_lock:
        set_setting(rq_draft_key(vid, uid), json.dumps(d))
    await query.answer()
    text, kb = rq_chat_view(v, d, step)
    await edit_or_send(query, text, kb)


async def on_rq_reason_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """oq:r:<visit> — type the reason."""
    query = update.callback_query
    vid = int(query.data.split(":")[2])
    v = q1("SELECT * FROM appointments WHERE id=?", (vid,))
    if not may_request(v, query.from_user.id):
        await query.answer("That's not open for a request.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["ons"] = {"kind": "rq_reason", "id": vid}
    await edit_or_send(query, "Type why it's needed, in one message.\n/cancel to stop")
    return ONS_TEXT


async def _rq_reason_text(update, context, job, text) -> int:
    uid = update.effective_user.id
    v = q1("SELECT * FROM appointments WHERE id=?", (job["id"],))
    if not may_request(v, uid):
        return ConversationHandler.END
    async with write_lock:
        d = rq_draft(v["id"], uid)
        d["reason"] = text[:400]
        set_setting(rq_draft_key(v["id"], uid), json.dumps(d))
    t, kb = rq_chat_view(v, d, 4)
    await update.message.reply_text(t, reply_markup=kb)
    return ConversationHandler.END


ONS_TEXT_HANDLERS["rq_reason"] = _rq_reason_text


def my_onsite_requests(user_id: int) -> list:
    out = []
    for r in q("SELECT * FROM onsite_requests WHERE created_by=? ORDER BY id DESC LIMIT 30", (user_id,)):
        out.append({"id": r["id"], "visit": r["appt_id"], "need": RQ_NEED_NAMES[r["need"]],
                    "status": r["status"], "reason": r["reason"],
                    "who": display_name_of(r["taken_by"] or r["done_by"], "") if r["taken_by"] else "",
                    "note": r["note"] or "",
                    "when": datetime.fromisoformat(r["created_at"]).astimezone(TZ).strftime("%a %-d %b")})
    return out


# --------------------------------------------------------------------------
# QR codes: PayNow (a real SGQR only when PAYNOW_UEN is set, otherwise a
# clearly marked SAMPLE that isn't a payment code) and the review link.
# --------------------------------------------------------------------------

PAYNOW_UEN = os.environ.get("PAYNOW_UEN", "").strip()
PAYNOW_NAME = os.environ.get("PAYNOW_NAME", "").strip() or "TC Acoustic"
REVIEW_URL = os.environ.get("REVIEW_URL", "").strip()


def _tlv(tag: str, val: str) -> str:
    return f"{tag}{len(val):02d}{val}"


def _crc16(data: str) -> str:
    crc = 0xFFFF
    for ch in data.encode():
        crc ^= ch << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def paynow_payload(cents: int, ref: str) -> str:
    """SGQR / PayNow to a UEN, fixed amount, with a reference."""
    acct = _tlv("00", "SG.PAYNOW") + _tlv("01", "2") + _tlv("02", PAYNOW_UEN) + _tlv("03", "0")
    s = (_tlv("00", "01") + _tlv("01", "12") + _tlv("26", acct) + _tlv("52", "0000") + _tlv("53", "702")
         + _tlv("54", f"{cents / 100:.2f}") + _tlv("58", "SG") + _tlv("59", PAYNOW_NAME[:25])
         + _tlv("60", "Singapore") + _tlv("62", _tlv("01", ref[:25])) + "6304")
    return s + _crc16(s)


def qr_png(text: str) -> bytes | None:
    try:
        import io
        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=8, border=3)
    return buf.getvalue()


def paynow_qr(v, cents: int) -> dict:
    """{'sample': bool, 'png': bytes|None, 'ref': str}. Real only for SG with PAYNOW_UEN."""
    ref = f"V{v['id']}"
    if PAYNOW_UEN and v["country"] == "SG":
        return {"sample": False, "png": qr_png(paynow_payload(cents, ref)), "ref": ref}
    return {"sample": True, "png": qr_png(f"SAMPLE ONLY - NOT A PAYMENT CODE - visit {ref}"), "ref": ref}


def data_uri(png: bytes | None) -> str:
    import base64
    return "data:image/png;base64," + base64.b64encode(png).decode() if png else ""


async def send_paynow_qr(bot, chat_id: int, v) -> None:
    qr = paynow_qr(v, v["amount_cents"] or 0)
    cap = (f"{'⚠️ SAMPLE — not a payment code. PayNow isn’t set up yet.' if qr['sample'] else 'Scan to pay with PayNow'}\n"
           f"{money_text(v['amount_cents'] or 0, v['currency'] or 'SGD')} · Reference {qr['ref']}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Customer has paid", callback_data=f"vz:pm:{v['id']}:PAYNOW")],
                               [InlineKeyboardButton("‹ Back", callback_data=f"vq:b:{v['id']}")]])
    if qr["png"]:
        await bot.send_photo(chat_id, qr["png"], caption=cap, reply_markup=kb)
    else:
        await bot.send_message(chat_id, cap, reply_markup=kb)


async def send_review_qr(bot, chat_id: int) -> None:
    if not REVIEW_URL:
        return
    png = qr_png(REVIEW_URL)
    cap = "⭐ Show this to the customer: scan to leave us a review."
    try:
        if png:
            await bot.send_photo(chat_id, png, caption=cap)
        else:
            await bot.send_message(chat_id, f"{cap}\n{REVIEW_URL}")
    except Exception as e:
        log.info("Couldn't send the review QR: %s", e)


async def on_paynow_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """vq:p:<id> show the PayNow QR · vq:b:<id> back to how they paid."""
    query = update.callback_query
    _, action, raw = query.data.split(":")
    v = q1("SELECT * FROM appointments WHERE id=?", (int(raw),))
    if not v or v["specialist_id"] != query.from_user.id or v["status"] != "ONSITE":
        await query.answer("That step is already done.", show_alert=True)
        return
    await query.answer()
    if action == "p":
        await send_paynow_qr(context.bot, query.from_user.id, v)
    else:
        await context.bot.send_message(query.from_user.id, f"How did they pay {money_text(v['amount_cents'], v['currency'])}?",
                                       reply_markup=pay_method_kb(v["id"]))


def pay_method_kb(appt_id: int) -> InlineKeyboardMarkup:
    """Cash / Other finish straight away; PayNow first shows the QR."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(n, callback_data=(f"vq:p:{appt_id}" if k == "PAYNOW"
                                                                          else f"vz:pm:{appt_id}:{k}"))
                                  for k, n in PAY_METHODS]])


# --------------------------------------------------------------------------
# Cab claims: the specialist claims a ride to or from a visit with the amount
# and a receipt photo; On-Site admins approve or reject with a reason.
# --------------------------------------------------------------------------

CAB_TRIPS = [("TO", "To the visit"), ("FROM", "From the visit"), ("BOTH", "Both ways")]
CAB_TRIP_NAMES = dict(CAB_TRIPS)
CAB_REJECT = [("RECEIPT", "Receipt unclear"), ("LIMIT", "Over the limit"),
              ("NOTVISIT", "Not for a visit"), ("OTHER", "Other")]
CAB_REJECT_NAMES = dict(CAB_REJECT)
CAB_STATUS = {"RECEIPT": "Waiting for the receipt", "PENDING": "Waiting for approval",
              "APPROVED": "Approved", "REJECTED": "Rejected"}


def may_claim(v, user_id: int) -> bool:
    return bool(v) and v["specialist_id"] == user_id and v["status"] in ("ONSITE", "COMPLETED")


def create_cab_claim(user_id: int, appt_id: int, trip: str, amount: str) -> tuple:
    v = q1("SELECT * FROM appointments WHERE id=?", (appt_id,))
    if not may_claim(v, user_id):
        return None, "Claim against your own visit, once you've arrived."
    if trip not in CAB_TRIP_NAMES:
        return None, "Pick the trip."
    try:
        cents = round(float(re.sub(r"^[A-Za-z$ ]+", "", str(amount or "").replace(",", "").strip())) * 100)
    except ValueError:
        cents = 0
    if cents <= 0 or cents > 100_000:
        return None, "Type the fare, e.g. 18.40."
    # One claim waits for its receipt at a time: an older one still waiting is dropped.
    run("DELETE FROM cab_claims WHERE user_id=? AND status='RECEIPT'", (user_id,))
    cur = run("INSERT INTO cab_claims (appt_id, user_id, trip, amount_cents, currency, created_at) "
              "VALUES (?,?,?,?,?,?)", (appt_id, user_id, trip, cents, CURRENCY.get(v["country"], "SGD"),
                                       now().isoformat()))
    return cur.lastrowid, None


def cab_text(c) -> str:
    v = q1("SELECT * FROM appointments WHERE id=?", (c["appt_id"],))
    tz = COUNTRY_TZ[v["country"]] if v else TZ
    day = span_of(v)[0].astimezone(tz).strftime("%a %-d %b") if v else ""
    flag = COUNTRY_NAMES[v["country"]].split(" ")[0] + " " + v["country"] if v else ""
    lines = [f"🚕 Cab claim · {flag}", f"From: {display_name_of(c['user_id'], 'a specialist')}",
             f"Visit #{c['appt_id']} · {day}" + (f" · {v['postal']}" if v and v["postal"] else ""),
             f"Trip: {CAB_TRIP_NAMES[c['trip']]}", f"Amount: {money_text(c['amount_cents'], c['currency'])}"]
    if c["status"] == "APPROVED":
        lines.append(f"\n✅ Approved by {display_name_of(c['decided_by'], 'an admin')}")
    elif c["status"] == "REJECTED":
        lines.append(f"\n❌ Rejected by {display_name_of(c['decided_by'], 'an admin')}: {c['reason']}")
    return "\n".join(lines)


def cab_admins() -> list:
    return onsite_fyi_ids() or list(ADMIN_IDS)


def cab_kb(cid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"cb:a:{cid}"),
                                  InlineKeyboardButton("❌ Reject", callback_data=f"cb:r:{cid}")]])


async def send_cab_to_admins(bot, cid: int) -> None:
    c = q1("SELECT * FROM cab_claims WHERE id=?", (cid,))
    sent = []
    for uid in cab_admins():
        try:
            m = await bot.send_photo(uid, c["receipt"], caption=cab_text(c), reply_markup=cab_kb(cid))
            sent.append([uid, m.message_id])
        except Exception as e:
            log.info("Couldn't send cab claim %s to %s: %s", cid, uid, e)
    async with write_lock:
        set_setting(f"cab_msgs:{cid}", json.dumps(sent))


async def _cab_receipt_photo(update, context) -> bool:
    """A photo while a cab claim waits for its receipt is that receipt."""
    uid = update.effective_user.id
    c = q1("SELECT * FROM cab_claims WHERE user_id=? AND status='RECEIPT' ORDER BY id DESC LIMIT 1", (uid,))
    if not c:
        return False
    async with write_lock:
        run("UPDATE cab_claims SET receipt=?, status='PENDING' WHERE id=?",
            (update.message.photo[-1].file_id, c["id"]))
    await update.message.reply_text(f"✅ Cab claim sent for approval: "
                                    f"{money_text(c['amount_cents'], c['currency'])}, "
                                    f"{CAB_TRIP_NAMES[c['trip']].lower()}. You'll hear back here.")
    await send_cab_to_admins(context.bot, c["id"])
    return True


ONS_PHOTO_HANDLERS.append(_cab_receipt_photo)


async def on_cab_form(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cb:n:<visit> start (pick the trip) in /visits."""
    query = update.callback_query
    vid = int(query.data.split(":")[2])
    v = q1("SELECT * FROM appointments WHERE id=?", (vid,))
    if not may_claim(v, query.from_user.id):
        await query.answer("Claim against your own visit, once you've arrived.", show_alert=True)
        return
    await query.answer()
    rows = [[InlineKeyboardButton(n, callback_data=f"cb:p:{vid}:{k}")] for k, n in CAB_TRIPS]
    rows.append([InlineKeyboardButton("‹ Back", callback_data=f"vz:v:{vid}")])
    await edit_or_send(query, f"🚕 Cab claim · visit #{vid} · {v['customer_name']}\n\nWhich trip?",
                       InlineKeyboardMarkup(rows))


async def on_cab_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """cb:p:<visit>:<TRIP> — then type the fare."""
    query = update.callback_query
    _, _, raw, trip = query.data.split(":")
    v = q1("SELECT * FROM appointments WHERE id=?", (int(raw),))
    if not may_claim(v, query.from_user.id) or trip not in CAB_TRIP_NAMES:
        await query.answer("That step is already done.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["ons"] = {"kind": "cab_amount", "id": v["id"], "trip": trip}
    await edit_or_send(query, f"🚕 {CAB_TRIP_NAMES[trip]} · visit #{v['id']}\n\n"
                              f"Type the fare in {CURRENCY.get(v['country'], 'SGD')}, e.g. 18.40.\n/cancel to stop",
                       InlineKeyboardMarkup([[InlineKeyboardButton("‹ Back", callback_data=f"cb:n:{v['id']}")]]))
    return ONS_TEXT


async def _cab_amount_text(update, context, job, text) -> int:
    uid = update.effective_user.id
    async with write_lock:
        cid, err = create_cab_claim(uid, job["id"], job["trip"], text)
    if err:
        context.user_data["ons"] = job
        await update.message.reply_text(err + " Or /cancel.")
        return ONS_TEXT
    await update.message.reply_text("📷 Now send a photo of the receipt here.")
    return ConversationHandler.END


ONS_TEXT_HANDLERS["cab_amount"] = _cab_amount_text


async def on_cab_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cb:a:<id> approve · cb:r:<id> reject (pick why) · cb:x:<id>:<KEY> reject
    with that reason · cb:b:<id> back."""
    query = update.callback_query
    parts = query.data.split(":")
    action, cid, uid = parts[1], int(parts[2]), query.from_user.id
    if not admin_for(uid, "ONSITE"):
        await query.answer("Only On-Site admins approve cab claims.", show_alert=True)
        return
    c = q1("SELECT * FROM cab_claims WHERE id=?", (cid,))
    if not c or c["status"] != "PENDING":
        await query.answer("Already decided.")
        return
    if action == "r":
        await query.answer()
        rows = [[InlineKeyboardButton(n, callback_data=f"cb:x:{cid}:{k}")] for k, n in CAB_REJECT]
        rows.append([InlineKeyboardButton("‹ Back", callback_data=f"cb:b:{cid}")])
        await query.edit_message_reply_markup(InlineKeyboardMarkup(rows))
        return
    if action == "b":
        await query.answer()
        await query.edit_message_reply_markup(cab_kb(cid))
        return
    if action == "x" and parts[3] in CAB_REJECT_NAMES:
        status, reason = "REJECTED", CAB_REJECT_NAMES[parts[3]]
    elif action == "a":
        status, reason = "APPROVED", None
    else:
        await query.answer()
        return
    async with write_lock:
        cur = run("UPDATE cab_claims SET status=?, reason=?, decided_by=?, decided_at=? WHERE id=? AND status='PENDING'",
                  (status, reason, uid, now().isoformat(), cid))
    if not cur.rowcount:
        await query.answer("Already decided.")
        return
    await query.answer("Approved" if status == "APPROVED" else "Rejected")
    c = q1("SELECT * FROM cab_claims WHERE id=?", (cid,))
    try:
        sent = json.loads(setting(f"cab_msgs:{cid}", "") or "[]")
    except ValueError:
        sent = []
    for chat_id, mid in sent:
        try:
            await context.bot.edit_message_caption(chat_id=chat_id, message_id=mid, caption=cab_text(c), reply_markup=None)
        except Exception:
            pass
    await tell(context.bot, c["user_id"],
               (f"✅ Your cab claim was approved: {money_text(c['amount_cents'], c['currency'])} "
                f"for visit #{c['appt_id']}." if status == "APPROVED" else
                f"❌ Your cab claim of {money_text(c['amount_cents'], c['currency'])} for visit "
                f"#{c['appt_id']} was rejected: {reason}. Ask {display_name_of(uid, 'the admin')} if unsure."))


def my_cab_claims(user_id: int) -> list:
    return [{"id": c["id"], "visit": c["appt_id"], "trip": CAB_TRIP_NAMES[c["trip"]],
             "amount": money_text(c["amount_cents"], c["currency"]), "status": c["status"],
             "statusName": CAB_STATUS[c["status"]], "reason": c["reason"] or "",
             "when": datetime.fromisoformat(c["created_at"]).astimezone(TZ).strftime("%a %-d %b")}
            for c in q("SELECT * FROM cab_claims WHERE user_id=? ORDER BY id DESC LIMIT 30", (user_id,))]


async def cab_from_app(user_id: int, appt_id: int, trip: str, amount: str) -> dict:
    async with write_lock:
        cid, err = create_cab_claim(user_id, appt_id, trip, amount)
    if err:
        return {"ok": False, "error": err}
    await tell(bot_ref(), user_id, f"📷 Send a photo of the receipt here for your cab claim (visit #{appt_id}).")
    return {"ok": True}
