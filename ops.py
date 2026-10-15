"""Ops: special requests from SH, Livechat and FS (change a delivery's date,
address or product, stop it, or anything else).

Each request posts in the Ops group with Done / Can't do buttons, the sender
hears back by DM, and every request becomes a row in the Ops Google Sheet.
Requests after the 2pm cut-off still go through, tagged, and the ones still
open are posted again at the top of the next working morning.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request

from core import *

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
CANT_REASONS = [
    ("SHIPPED", "Already out for delivery"),
    ("INFO", "Need more information"),
    ("NOTPOSSIBLE", "Not possible"),
]
CANT_NAMES = dict(CANT_REASONS)
STATUS_NAMES = {"OPEN": "Open", "DONE": "Done", "CANT": "Can't do"}

# Conversation states for /request
R_ORDER, R_CAT, R_DETAILS, R_DATE, R_DATE_TYPED, R_TIME, R_TIME_TYPED, R_CONFIRM = range(410, 418)
DATE_CHOICES = 8   # today and the next 7 days as buttons; anything else is typed


def may_request(user_id: int) -> bool:
    return is_admin(user_id) or bool(depts_of(user_id) & {"SH", "LIVECHAT", "FS"})


def is_ops(user_id: int) -> bool:
    return is_admin(user_id) or "OPS" in depts_of(user_id)


def ops_target() -> dict | None:
    raw = setting("ops_requests_chat", "")
    return json.loads(raw) if raw else None


def after_cutoff(when: datetime) -> bool:
    """Sent at or after the cut-off, or on a weekend: Ops pick it up next working day."""
    return when.weekday() >= 5 or when.hour * 60 + when.minute >= parse_time_token(REQUEST_CUTOFF)


def cutoff_text() -> str:
    m = parse_time_token(REQUEST_CUTOFF)
    return datetime(2000, 1, 1, m // 60, m % 60).strftime("%-I%p").lower() if m % 60 == 0 \
        else datetime(2000, 1, 1, m // 60, m % 60).strftime("%-I:%M%p").lower()


def team_of(user_id: int) -> str:
    names = [DEPT_NAMES[k] for k, _ in DEPARTMENTS if k in depts_of(user_id) and k in ("SH", "LIVECHAT", "FS")]
    return " + ".join(names) or ("Admin" if is_admin(user_id) else "")


def timeframe_text(r) -> str:
    if r["timeframe"] == "OTHER":
        return r["timeframe_other"] or "Other"
    return TIMEFRAME_NAMES.get(r["timeframe"], r["timeframe"])


def request_text(r, head: str = "") -> str:
    """The request as Ops see it in the group (HTML)."""
    when = datetime.fromisoformat(r["created_at"])
    d = date.fromisoformat(r["delivery_date"])
    lines = []
    if head:
        lines.append(head)
    tag = "⏰ <b>AFTER CUT-OFF</b> · " if r["after_cutoff"] else ""
    lines += [
        f"{tag}📦 <b>Special request #{r['id']}</b>",
        f"<b>{esc(CATEGORY_NAMES[r['category']])}</b>",
        "",
        f"Order: <code>{esc(r['order_no'])}</code>",
        f"Delivery: {d:%a %-d %b} · {esc(timeframe_text(r))}",
        "",
        esc(r["details"]),
        "",
        f"<i>From {esc(display_name_of(r['created_by'], 'someone'))}"
        f"{' (' + esc(team_of(r['created_by'])) + ')' if team_of(r['created_by']) else ''}"
        f" · {when:%a %-d %b, %-I:%M%p}</i>",
    ]
    if r["status"] == "DONE":
        lines += ["", f"✅ <b>Done</b> by {esc(display_name_of(r['handled_by'], 'Ops'))}"
                  f" · {datetime.fromisoformat(r['handled_at']):%a %-d %b, %-I:%M%p}"]
    elif r["status"] == "CANT":
        lines += ["", f"🚫 <b>Can't do</b>: {esc(CANT_NAMES.get(r['reason'], r['reason'] or ''))}"
                  f" · {esc(display_name_of(r['handled_by'], 'Ops'))}"
                  f" · {datetime.fromisoformat(r['handled_at']):%a %-d %b, %-I:%M%p}"]
    return "\n".join(lines)


def ops_buttons(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done", callback_data=f"sr:ok:{rid}"),
        InlineKeyboardButton("🚫 Can't do", callback_data=f"sr:no:{rid}"),
    ]])


def cant_buttons(rid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(name, callback_data=f"sr:nr:{rid}:{key}")] for key, name in CANT_REASONS]
    rows.append([InlineKeyboardButton("‹ Back", callback_data=f"sr:bk:{rid}")])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------
# Google Sheet: one row per request, kept up to date by request number
# --------------------------------------------------------------------------

def sheet_row(r) -> dict:
    return {
        "id": r["id"],
        "submitted_at": datetime.fromisoformat(r["created_at"]).strftime("%Y-%m-%d %H:%M"),
        "submitted_by": display_name_of(r["created_by"], ""),
        "team": team_of(r["created_by"]),
        "order_no": r["order_no"],
        "category": CATEGORY_NAMES[r["category"]],
        "details": r["details"],
        "delivery_date": r["delivery_date"],
        "timeframe": timeframe_text(r),
        "after_cutoff": "Yes" if r["after_cutoff"] else "No",
        "status": STATUS_NAMES.get(r["status"], r["status"]),
        "handled_by": display_name_of(r["handled_by"], "") if r["handled_by"] else "",
        "handled_at": datetime.fromisoformat(r["handled_at"]).strftime("%Y-%m-%d %H:%M") if r["handled_at"] else "",
        "reason": CANT_NAMES.get(r["reason"], r["reason"] or ""),
    }


def _post_to_sheet(payload: dict) -> bool:
    req = urllib.request.Request(
        REQUEST_SHEET_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        out = json.loads(resp.read().decode() or "{}")
    return bool(out.get("ok"))


async def sync_to_sheet(rid: int) -> None:
    """Add or update this request's row. Left marked for the retry job if it fails."""
    if not (REQUEST_SHEET_URL and REQUEST_SHEET_SECRET):
        return
    r = q1("SELECT * FROM special_requests WHERE id=?", (rid,))
    if not r:
        return
    try:
        ok = await asyncio.to_thread(_post_to_sheet, {"secret": REQUEST_SHEET_SECRET, "row": sheet_row(r)})
    except Exception as e:
        log.info("Ops sheet: couldn't write request %s: %s", rid, e)
        ok = False
    if ok:
        async with write_lock:
            run("UPDATE special_requests SET sheet_dirty=0 WHERE id=?", (rid,))


async def job_sheet_retry(context: ContextTypes.DEFAULT_TYPE) -> None:
    for r in q("SELECT id FROM special_requests WHERE sheet_dirty=1 ORDER BY id LIMIT 50"):
        await sync_to_sheet(r["id"])


# --------------------------------------------------------------------------
# Sending a request: /request
# --------------------------------------------------------------------------

def date_buttons() -> InlineKeyboardMarkup:
    today = now().date()
    btns = []
    for i in range(DATE_CHOICES):
        d = today + timedelta(days=i)
        label = "Today" if i == 0 else "Tomorrow" if i == 1 else f"{d:%a %-d %b}"
        btns.append(InlineKeyboardButton(label, callback_data=f"sr:d:{d.isoformat()}"))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton("Another date…", callback_data="sr:d:typed")])
    return InlineKeyboardMarkup(rows)


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


def draft_text(d: dict) -> str:
    lines = ["📦 <b>Check your special request</b>", ""]
    lines.append(f"Order: <code>{esc(d['order_no'])}</code>")
    lines.append(f"Request: <b>{esc(CATEGORY_NAMES[d['category']])}</b>")
    dd = date.fromisoformat(d["delivery_date"])
    tf = d.get("timeframe_other") if d["timeframe"] == "OTHER" else TIMEFRAME_NAMES[d["timeframe"]]
    lines.append(f"Delivery: {dd:%a %-d %b} · {esc(tf)}")
    lines += ["", esc(d["details"])]
    if after_cutoff(now()):
        lines += ["", f"⏰ <b>It's past the {cutoff_text()} cut-off.</b> Ops will still see it now, "
                  "marked AFTER CUT-OFF, and it goes to the top of their list next working day."]
    return "\n".join(lines)


async def cmd_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/request — send a special request to Ops."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly to send a special request.")
        return ConversationHandler.END
    uid = update.effective_user.id
    if not may_request(uid):
        await update.message.reply_text("Special requests are for SH, Livechat and FS.")
        return ConversationHandler.END
    context.user_data["sr"] = {}
    note = ""
    if after_cutoff(now()):
        note = (f"\n\n⏰ <i>It's past the {cutoff_text()} cut-off: Ops will still see it, "
                "and it's at the top of their list next working day.</i>")
    await update.message.reply_text(
        "📦 <b>Special request to Ops</b>\n\nWhat's the <b>order number</b>?" + note
        + "\n\n<i>/cancel to stop</i>", parse_mode=constants.ParseMode.HTML)
    return R_ORDER


async def on_request_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if len(text) < 3:
        await update.message.reply_text("That looks too short. Send the full order number.")
        return R_ORDER
    context.user_data.setdefault("sr", {})["order_no"] = text[:80]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(name, callback_data=f"sr:c:{key}")]
                               for key, name in REQUEST_CATEGORIES])
    await update.message.reply_text("<b>What's the request?</b>", parse_mode=constants.ParseMode.HTML,
                                    reply_markup=kb)
    return R_CAT


async def on_request_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    key = query.data.split(":")[2]
    await query.answer()
    if key not in CATEGORY_NAMES or "sr" not in context.user_data:
        return ConversationHandler.END
    context.user_data["sr"]["category"] = key
    hint = {
        "DATE": "e.g. Customer wants it moved to Saturday afternoon.",
        "ADDRESS": "Write the full new address, with unit number and postal code.",
        "PRODUCT": "Which item, and what it should change to (model and colour).",
        "STOP": "Why it should be stopped, and what happens next (refund, cancel).",
        "OTHER": "What Ops need to do.",
    }[key]
    await query.edit_message_text(
        f"<b>{esc(CATEGORY_NAMES[key])}</b>\n\nNow the <b>details</b>, as fully as you can.\n"
        f"<i>{esc(hint)}</i>\n\nPlease don't include phone or ID numbers.",
        parse_mode=constants.ParseMode.HTML)
    return R_DETAILS


async def on_request_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if len(text) < 5:
        await update.message.reply_text("A bit more detail please, so Ops can act on it.")
        return R_DETAILS
    context.user_data["sr"]["details"] = text[:2000]
    label = "the new delivery date" if context.user_data["sr"].get("category") == "DATE" \
        else "the delivery date"
    await update.message.reply_text(f"<b>What's {label}?</b>", parse_mode=constants.ParseMode.HTML,
                                    reply_markup=date_buttons())
    return R_DATE


def time_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(name, callback_data=f"sr:t:{key}")]
                                 for key, name in TIMEFRAMES])


async def on_request_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    value = query.data.split(":", 2)[2]
    await query.answer()
    if "sr" not in context.user_data:
        return ConversationHandler.END
    if value == "typed":
        await query.edit_message_text("Type the date, e.g. <code>24/10</code>.",
                                      parse_mode=constants.ParseMode.HTML)
        return R_DATE_TYPED
    context.user_data["sr"]["delivery_date"] = value
    await query.edit_message_text(f"Delivery: <b>{date.fromisoformat(value):%a %-d %b}</b>\n\n"
                                  "<b>What time frame?</b>", parse_mode=constants.ParseMode.HTML,
                                  reply_markup=time_buttons())
    return R_TIME


async def on_request_date_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = read_date(update.message.text or "")
    if not d or d < now().date():
        await update.message.reply_text("I couldn't read that as a date from today on. Try e.g. 24/10.")
        return R_DATE_TYPED
    context.user_data["sr"]["delivery_date"] = d.isoformat()
    await update.message.reply_text(f"Delivery: <b>{d:%a %-d %b}</b>\n\n<b>What time frame?</b>",
                                    parse_mode=constants.ParseMode.HTML, reply_markup=time_buttons())
    return R_TIME


def confirm_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Send to Ops", callback_data="sr:send"),
        InlineKeyboardButton("✖️ Cancel", callback_data="sr:stop"),
    ]])


async def on_request_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    key = query.data.split(":")[2]
    await query.answer()
    if key not in TIMEFRAME_NAMES or "sr" not in context.user_data:
        return ConversationHandler.END
    context.user_data["sr"]["timeframe"] = key
    if key == "OTHER":
        await query.edit_message_text("Type the time frame, e.g. <code>after 7pm</code>.",
                                      parse_mode=constants.ParseMode.HTML)
        return R_TIME_TYPED
    await query.edit_message_text(draft_text(context.user_data["sr"]),
                                  parse_mode=constants.ParseMode.HTML, reply_markup=confirm_buttons())
    return R_CONFIRM


async def on_request_time_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        return R_TIME_TYPED
    context.user_data["sr"]["timeframe_other"] = text[:80]
    await update.message.reply_text(draft_text(context.user_data["sr"]),
                                    parse_mode=constants.ParseMode.HTML, reply_markup=confirm_buttons())
    return R_CONFIRM


async def post_to_ops(bot, rid: int, head: str = "") -> bool:
    """Post (or re-post) a request in the Ops group, with its buttons."""
    target = ops_target()
    r = q1("SELECT * FROM special_requests WHERE id=?", (rid,))
    if not target or not r:
        return False
    kw = {"message_thread_id": target["thread"]} if target.get("thread") else {}
    try:
        msg = await bot.send_message(target["chat"], request_text(r, head),
                                     parse_mode=constants.ParseMode.HTML,
                                     reply_markup=ops_buttons(rid) if r["status"] == "OPEN" else None, **kw)
    except Exception as e:
        log.warning("Couldn't post special request %s to Ops: %s", rid, e)
        return False
    async with write_lock:
        run("UPDATE special_requests SET ops_chat_id=?, ops_msg_id=? WHERE id=?",
            (target["chat"], msg.message_id, rid))
    return True


async def on_request_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.pop("sr", None)
    if query.data == "sr:stop" or not d:
        await query.edit_message_text("Cancelled. Nothing was sent.")
        return ConversationHandler.END
    created = now()
    async with write_lock:
        rid = run(
            "INSERT INTO special_requests (order_no, category, details, delivery_date, timeframe,"
            " timeframe_other, created_by, created_at, after_cutoff, status, sheet_dirty)"
            " VALUES (?,?,?,?,?,?,?,?,?, 'OPEN', 1)",
            (d["order_no"], d["category"], d["details"], d["delivery_date"], d["timeframe"],
             d.get("timeframe_other"), query.from_user.id, created.isoformat(),
             1 if after_cutoff(created) else 0)).lastrowid
    posted = await post_to_ops(context.bot, rid)
    late = " It's after the cut-off, so it's marked for Ops' next working day." if after_cutoff(created) else ""
    if posted:
        await query.edit_message_text(
            f"✅ Sent to Ops as <b>request #{rid}</b>.{late}\nI'll message you when they answer.",
            parse_mode=constants.ParseMode.HTML)
    else:
        await query.edit_message_text(
            f"✅ Saved as <b>request #{rid}</b>, but the Ops group isn't linked yet, so an admin "
            "has been told.", parse_mode=constants.ParseMode.HTML)
        for admin in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin, f"⚠️ Special request #{rid} couldn't reach Ops: link the Ops group "
                    "with /opschat inside it.")
            except Exception:
                pass
    await sync_to_sheet(rid)
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Ops answering in the group
# --------------------------------------------------------------------------

async def on_ops_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """sr:ok / sr:no / sr:nr:<reason> / sr:bk on a request in the Ops group."""
    query = update.callback_query
    parts = query.data.split(":")
    action, rid = parts[1], int(parts[2])
    if not is_ops(query.from_user.id):
        await query.answer("Only the Ops team can answer requests.", show_alert=True)
        return
    r = q1("SELECT * FROM special_requests WHERE id=?", (rid,))
    if not r:
        await query.answer("That request is gone.", show_alert=True)
        return
    if r["status"] != "OPEN":
        await query.answer(f"Already answered: {STATUS_NAMES[r['status']]}.", show_alert=True)
        return
    if action == "no":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=cant_buttons(rid))
        return
    if action == "bk":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=ops_buttons(rid))
        return
    status, reason = ("DONE", None) if action == "ok" else ("CANT", parts[3] if len(parts) > 3 else None)
    if status == "CANT" and reason not in CANT_NAMES:
        await query.answer()
        return
    async with write_lock:
        run("UPDATE special_requests SET status=?, reason=?, handled_by=?, handled_at=?, sheet_dirty=1"
            " WHERE id=? AND status='OPEN'",
            (status, reason, query.from_user.id, now().isoformat(), rid))
    r = q1("SELECT * FROM special_requests WHERE id=?", (rid,))
    await query.answer("Done ✅" if status == "DONE" else "Marked can't do")
    try:
        await query.edit_message_text(request_text(r), parse_mode=constants.ParseMode.HTML)
    except BadRequest:
        pass
    who = display_name_of(query.from_user.id, "Ops")
    if status == "DONE":
        msg = (f"✅ <b>Ops have done request #{rid}</b> ({esc(CATEGORY_NAMES[r['category']])}, "
               f"order <code>{esc(r['order_no'])}</code>).\n<i>— {esc(who)}</i>")
    else:
        msg = (f"🚫 <b>Ops can't do request #{rid}</b> ({esc(CATEGORY_NAMES[r['category']])}, "
               f"order <code>{esc(r['order_no'])}</code>)\nReason: <b>{esc(CANT_NAMES[reason])}</b>\n"
               f"<i>— {esc(who)}</i>")
    try:
        await context.bot.send_message(r["created_by"], msg, parse_mode=constants.ParseMode.HTML)
    except Exception as e:
        log.info("Couldn't tell %s about request %s: %s", r["created_by"], rid, e)
    await sync_to_sheet(rid)


async def job_carry_over(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Working mornings: re-post after-cut-off requests still open, at the top of the day."""
    today = now()
    if today.weekday() >= 5 or not ops_target():
        return
    start = today.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = q("SELECT * FROM special_requests WHERE status='OPEN' AND after_cutoff=1 AND created_at < ?"
             " ORDER BY delivery_date, id", (start,))
    if not rows:
        return
    target = ops_target()
    kw = {"message_thread_id": target["thread"]} if target.get("thread") else {}
    await context.bot.send_message(
        target["chat"], f"📌 <b>After the last cut-off: {len(rows)} request"
        f"{'' if len(rows) == 1 else 's'} still open</b>\nSoonest delivery first.",
        parse_mode=constants.ParseMode.HTML, **kw)
    for r in rows:
        old_chat, old_msg = r["ops_chat_id"], r["ops_msg_id"]
        if await post_to_ops(context.bot, r["id"], head="↪️ <i>Carried over</i>") and old_msg:
            try:
                await context.bot.edit_message_reply_markup(old_chat, old_msg, reply_markup=None)
            except Exception:
                pass


async def cmd_opschat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/opschat (admins, inside the Ops group) — special requests post here."""
    if not is_admin(update.effective_user.id):
        return
    chat = update.effective_chat
    if chat.type not in (constants.ChatType.GROUP, constants.ChatType.SUPERGROUP):
        await update.message.reply_text("Send /opschat inside the Ops group where requests should go.")
        return
    thread = update.message.message_thread_id if update.message.is_topic_message else None
    async with write_lock:
        set_setting("ops_requests_chat", json.dumps({"chat": chat.id, "thread": thread}))
    await update.message.reply_text(
        f"✅ Special requests will be posted here. Requests after {cutoff_text()} are marked "
        "AFTER CUT-OFF and come back at the top of the next working morning.")


async def cmd_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/requests — Ops and admins: the special requests still open."""
    if update.effective_chat.type != constants.ChatType.PRIVATE or not is_ops(update.effective_user.id):
        return
    rows = q("SELECT * FROM special_requests WHERE status='OPEN' ORDER BY after_cutoff, delivery_date, id")
    if not rows:
        await update.message.reply_text("No special requests open. 🎉")
        return
    lines = [f"📦 <b>Open special requests: {len(rows)}</b>", ""]
    for r in rows:
        late = "⏰ " if r["after_cutoff"] else ""
        lines.append(f"{late}<b>#{r['id']}</b> {esc(CATEGORY_NAMES[r['category']])} · "
                     f"<code>{esc(r['order_no'])}</code> · "
                     f"{date.fromisoformat(r['delivery_date']):%a %-d %b}, {esc(timeframe_text(r))}")
    lines += ["", "Answer them in the Ops group."]
    await reply_long(update.message, "\n".join(lines), parse_mode=constants.ParseMode.HTML)
