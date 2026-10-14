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

VISIT_TYPES = [("SETUP", "Set up"), ("TROUBLESHOOT", "Troubleshoot"), ("SURVEY", "Survey")]
VISIT_NAMES = dict(VISIT_TYPES)
DURATIONS = [60, 90, 120]                 # minutes the booker can pick
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


async def cmd_specialists(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/specialists — On-Site Specialists by country · /specialists @name — set theirs."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not is_admin(update.effective_user.id):
        return
    target = " ".join(context.args).strip()
    if target:
        row = find_agent(target)
        if not row or "ONSITE" not in depts_of(row["user_id"]):
            await update.message.reply_text(
                "I can't find an On-Site Specialist by that name. Add them to the "
                "On-Site team first with /departments @username.")
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
    unset = q("SELECT a.* FROM agents a JOIN memberships m ON m.user_id=a.user_id "
              "WHERE m.dept='ONSITE' AND a.status='active' AND a.country IS NULL")
    if unset:
        lines += ["", "No country yet: " + ", ".join(esc(r["display_name"] or r["name"])
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
    return f"{start.strftime('%a %-d %b, %H:%M')}–{end.strftime('%H:%M')}"


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
    if vtype not in VISIT_NAMES:
        return None, "Pick the visit type."
    try:
        duration = int(f.get("duration_min") or 120)
    except (TypeError, ValueError):
        duration = 120
    if duration not in DURATIONS:
        return None, "Pick how long it takes."
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
        return None, f"The specialist already has {busy}. Pick another time."
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
    who = display_name_of(v["created_by"], "SH")
    await tell(bot, v["specialist_id"],
               visit_text(v, "📅 New on-site visit") + f"\n\nBooked by {who}.")


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
        return False, f"The specialist already has {busy} then."
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
        "bookedBy": display_name_of(v["created_by"], ""), "proposed": proposed,
        "canChange": role == "booker" and v["status"] == "SCHEDULED",
    }


def visits_payload(user_id: int, week: str = "", country: str = "", spec: str = "") -> dict:
    role = visit_role(user_id)
    countries = [{"code": c, "name": COUNTRY_NAMES[c],
                  "specialists": [{"id": r["user_id"], "name": r["display_name"] or r["name"]}
                                  for r in specialists_in(c)]} for c, _, _, _ in COUNTRIES]
    if role == "specialist":
        me = q1("SELECT country FROM agents WHERE user_id=?", (user_id,))
        country = (me["country"] if me else "") or "SG"
        spec_id = user_id
    else:
        country = country if country in COUNTRY_NAMES else "SG"
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
        for b in q("SELECT * FROM specialist_blocks WHERE user_id=?", (spec_id,)):
            s = datetime.fromisoformat(b["starts_at"]).astimezone(tz)
            e = datetime.fromisoformat(b["ends_at"]).astimezone(tz)
            if s.date().isoformat() in by_day:
                by_day[s.date().isoformat()]["items"].append({
                    "kind": "block", "id": b["id"], "start": s.strftime("%H:%M"),
                    "end": e.strftime("%H:%M"), "note": b["note"] or "",
                    "canRemove": role == "specialist"})
    for d in days:
        d["items"].sort(key=lambda it: it["start"])
    return {
        "role": role, "countries": countries, "country": country, "specId": spec_id,
        "types": [{"key": k, "name": n} for k, n in VISIT_TYPES], "durations": DURATIONS,
        "weekStart": first.isoformat(), "prev": (first - timedelta(days=7)).isoformat(),
        "next": (first + timedelta(days=7)).isoformat(),
        "weekLabel": f"{first.strftime('%-d %b')} – {(first + timedelta(days=6)).strftime('%-d %b')}",
        "today": today.isoformat(), "days": days,
    }
