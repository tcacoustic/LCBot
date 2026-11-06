"""Claiming slots, and drop / pickup / swap requests.

Part of the LC avails bot. Shared helpers live in core.py.
"""

from core import *  # noqa: F401,F403

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("DM me /plan to fill in your whole week at once 🙂")
        return
    if not await gate(update):
        return
    w = open_week()
    if not w:
        await update.message.reply_text("No week is open right now.")
        return
    touch_agent(update.effective_user, dm_ok=1)
    text, kb = render_plan(update.effective_user.id, w["id"])
    await update.message.reply_text(
        text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
    )


async def on_plan_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await gate_cb(query):
        return
    raw = query.data.split(":", 1)[1]
    if raw == "noop":
        await query.answer()
        return

    slot_id = int(raw)
    user = query.from_user
    row = q1(
        """SELECT s.id, s.label, d.id AS day_id, d.name AS day_name,
                  w.id AS week_id, w.status
           FROM slots s JOIN days d ON d.id = s.day_id
           JOIN weeks w ON w.id = d.week_id WHERE s.id=?""",
        (slot_id,),
    )
    if not row or row["status"] != "open":
        await query.answer("This week is closed.", show_alert=True)
        return
    if is_locked(row["week_id"], user.id):
        await query.answer(
            "🔒 Your week is confirmed. Tap 🔓 Slots to change it.",
            show_alert=True,
        )
        return

    async with write_lock:
        touch_agent(user, dm_ok=1)
        mine = q1(
            "SELECT 1 FROM signups WHERE slot_id=? AND user_id=?", (slot_id, user.id)
        )
        if not mine:
            holder = slot_taken_by(slot_id, user.id)
            if holder:
                await query.answer(
                    f"{row['day_name']} {row['label']} is already taken by {holder}.",
                    show_alert=True,
                )
                return
        if mine:
            run("DELETE FROM signups WHERE slot_id=? AND user_id=?", (slot_id, user.id))
            note = f"Dropped {row['day_name']} {row['label']}"
        else:
            run(
                "INSERT INTO signups (slot_id, user_id, name, ts) VALUES (?,?,?,?)",
                (slot_id, user.id, display_name_of(user.id, user.full_name),
                 now().isoformat()),
            )
            note = f"✅ {row['day_name']} {row['label']}"

    await query.answer(note)
    await refresh_plan(query, user.id, row["week_id"], row["day_id"])
    await refresh_group(context, row["week_id"], row["day_id"])


async def on_plan_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move between the day list and a single day."""
    query = update.callback_query
    if not await gate_cb(query):
        return
    raw = query.data.split(":", 1)[1]
    user = query.from_user
    w = open_week()
    if not w:
        await query.answer("No week is open.", show_alert=True)
        return
    await query.answer()
    if raw == "list":
        text, kb = render_plan(user.id, w["id"])
    else:
        text, kb = render_plan_day(user.id, int(raw))
    try:
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode=constants.ParseMode.HTML
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Plan nav failed: %s", e)


async def on_plan_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await gate_cb(query):
        return
    action = query.data.split(":", 1)[1]
    user = query.from_user
    w = open_week()
    if not w:
        await query.answer("No week is open.", show_alert=True)
        return
    week_id = w["id"]

    if action in {"clear", "last"} and is_locked(week_id, user.id):
        await query.answer(
            "🔒 Your week is confirmed. Tap 🔓 Slots first.",
            show_alert=True,
        )
        return

    async with write_lock:
        touch_agent(user, dm_ok=1)

        if action == "clear":
            run(
                """DELETE FROM signups WHERE user_id=? AND slot_id IN
                   (SELECT s.id FROM slots s JOIN days d ON d.id=s.day_id WHERE d.week_id=?)""",
                (user.id, week_id),
            )
            note = "Cleared your whole week."

        elif action == "last":
            prev = q1(
                "SELECT * FROM weeks WHERE id < ? ORDER BY id DESC LIMIT 1", (week_id,)
            )
            if not prev:
                await query.answer("No previous week to copy from.", show_alert=True)
                return
            previous = q(
                """SELECT d.name AS day_name, s.label FROM signups su
                   JOIN slots s ON s.id = su.slot_id JOIN days d ON d.id = s.day_id
                   WHERE d.week_id=? AND su.user_id=?""",
                (prev["id"], user.id),
            )
            if not previous:
                await query.answer(
                    "You had nothing booked last week — nothing to copy.", show_alert=True
                )
                return
            copied, skipped, taken = 0, 0, 0
            for p in previous:
                target = q1(
                    """SELECT s.id FROM slots s JOIN days d ON d.id = s.day_id
                       WHERE d.week_id=? AND d.name=? AND s.label=?""",
                    (week_id, p["day_name"], p["label"]),
                )
                if not target:
                    skipped += 1
                    continue
                if slot_taken_by(target["id"], user.id):
                    taken += 1
                    continue
                cur = run(
                    "INSERT OR IGNORE INTO signups (slot_id, user_id, name, ts) VALUES (?,?,?,?)",
                    (target["id"], user.id,
                     display_name_of(user.id, user.full_name), now().isoformat()),
                )
                copied += cur.rowcount
            note = f"Copied {copied} slot(s) from {prev['label']}."
            if skipped:
                note += f" {skipped} not in this week's timings."
            if taken:
                note += f" {taken} already taken by someone else."
            note += " Now adjust whatever's changed."

        elif action == "confirm":
            if is_locked(week_id, user.id):
                run(
                    "DELETE FROM confirmations WHERE week_id=? AND user_id=?",
                    (week_id, user.id),
                )
                note = "🔓 Unlocked. Change your slots, then confirm again."
            else:
                run(
                    "INSERT INTO confirmations (week_id, user_id, ts) VALUES (?,?,?)",
                    (week_id, user.id, now().isoformat()),
                )
                note = "✅ Confirmed and locked. Thanks!"
        else:
            await query.answer()
            return

    await query.answer(note, show_alert=(action in {"last", "confirm"}))
    await refresh_plan(query, user.id, week_id)
    if BOARD_MODE == "single":
        await refresh_board(context, week_id)
    else:
        if action in {"clear", "last"}:
            for d in q("SELECT id FROM days WHERE week_id=? ORDER BY idx", (week_id,)):
                await refresh_day(context, d["id"])
        await refresh_header(context, week_id)


async def cmd_dropshift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask to be taken off a shift you can no longer work."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly for that 🙂")
        return ConversationHandler.END
    if not await gate(update):
        return ConversationHandler.END

    user = update.effective_user
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return ConversationHandler.END

    today = now().date().isoformat()
    mine = q(
        """SELECT s.id, s.label, d.name AS day_name, d.the_date
           FROM signups su JOIN slots s ON s.id = su.slot_id
           JOIN days d ON d.id = s.day_id
           WHERE d.week_id=? AND su.user_id=? AND d.the_date >= ?
           ORDER BY d.idx, s.idx""",
        (w["id"], user.id, today),
    )
    if not mine:
        await update.message.reply_text(
            "You have no upcoming shifts this week."
        )
        return ConversationHandler.END

    pending = {
        r["slot_id"] for r in q(
            "SELECT slot_id FROM drop_requests WHERE agent_id=? AND status='pending'",
            (user.id,),
        )
    }
    rows = []
    for m in mine:
        if m["id"] in pending:
            continue
        rows.append([InlineKeyboardButton(
            f"{m['day_name']} {m['label']}", callback_data=f"ds:{m['id']}"
        )])
    if not rows:
        await update.message.reply_text(
            "You've already asked to drop all of your upcoming shifts. "
            "Your manager will get back to you."
        )
        return ConversationHandler.END

    rows.append([InlineKeyboardButton("Cancel", callback_data="ds:cancel")])
    await update.message.reply_text(
        "<b>Which shift can't you work?</b>\n\n"
        "<i>Your manager has to approve it, so don't assume you're off "
        "until they do.</i>",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return DROP_PICK


async def on_drop_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    raw = query.data.split(":", 1)[1]
    await query.answer()
    if raw == "cancel":
        await query.edit_message_text("No problem — nothing changed.")
        return ConversationHandler.END

    slot = q1(
        """SELECT s.id, s.label, d.name AS day_name FROM slots s
           JOIN days d ON d.id = s.day_id WHERE s.id=?""",
        (int(raw),),
    )
    if not slot:
        await query.edit_message_text("That shift no longer exists.")
        return ConversationHandler.END

    context.user_data["drop_slot"] = slot["id"]
    await query.edit_message_text(
        f"<b>{slot['day_name']} {esc(slot['label'])}</b>\n\n"
        "Why can't you work it? A short reason is fine — your manager sees this.",
        parse_mode=constants.ParseMode.HTML,
    )
    return DROP_REASON


async def got_drop_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    slot_id = context.user_data.pop("drop_slot", None)
    if not slot_id:
        await update.message.reply_text("That request expired — send /dropshift again.")
        return ConversationHandler.END

    reason = " ".join(update.message.text.split()).strip()
    if len(reason) < 3:
        await update.message.reply_text("Give a little more detail than that.")
        context.user_data["drop_slot"] = slot_id
        return DROP_REASON

    slot = q1(
        """SELECT s.id, s.label, d.name AS day_name, d.the_date FROM slots s
           JOIN days d ON d.id = s.day_id WHERE s.id=?""",
        (slot_id,),
    )
    async with write_lock:
        cur = run(
            "INSERT INTO drop_requests (agent_id, slot_id, reason, requested_at) "
            "VALUES (?,?,?,?)",
            (user.id, slot_id, reason, now().isoformat()),
        )
        req_id = cur.lastrowid

    nm = display_name_of(user.id, user.full_name)
    await update.message.reply_text(
        "✅ Sent to your manager.\n\n"
        f"<b>{slot['day_name']} {esc(slot['label'])}</b>\n"
        f"<i>{esc(reason)}</i>\n\n"
        "You're still on the shift until they approve it.",
        parse_mode=constants.ParseMode.HTML,
    )

    text = (
        "🙋 <b>Shift drop request</b>\n\n"
        f"<b>{esc(nm)}</b> can't work "
        f"<b>{slot['day_name']} {esc(slot['label'])}</b>\n"
        f"<i>{esc(reason)}</i>"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"dq:a:{req_id}"),
        InlineKeyboardButton("🚫 Decline", callback_data=f"dq:d:{req_id}"),
    ]])
    sent = await notify_admins(context.bot, text, kb)
    run("UPDATE drop_requests SET notified=? WHERE id=?",
        (json.dumps(sent), req_id))
    return ConversationHandler.END


async def cmd_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask to take an open shift, including after the deadline."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly for that 🙂")
        return ConversationHandler.END
    if not await gate(update):
        return ConversationHandler.END

    user = update.effective_user
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return ConversationHandler.END

    today = now().date().isoformat()
    slots = q(
        """SELECT s.id, s.label, s.capacity, d.name AS day_name
           FROM slots s JOIN days d ON d.id = s.day_id
           WHERE d.week_id=? AND d.the_date >= ?
           ORDER BY d.idx, s.idx""",
        (w["id"], today),
    )
    pending = {
        r["slot_id"] for r in q(
            "SELECT slot_id FROM drop_requests "
            "WHERE agent_id=? AND kind='pickup' AND status='pending'",
            (user.id,),
        )
    }

    rows = []
    for sl in slots:
        holders = slot_holders(sl["id"])
        if any(h["user_id"] == user.id for h in holders):
            continue
        if len(holders) >= (sl["capacity"] or SLOT_CAPACITY):
            continue
        if sl["id"] in pending:
            continue
        rows.append([InlineKeyboardButton(
            f"{sl['day_name']} {sl['label']}", callback_data=f"pu:{sl['id']}"
        )])

    if not rows:
        await update.message.reply_text(
            "Nothing open at the moment — every upcoming slot is taken, "
            "or you've already asked for the ones that are free."
        )
        return ConversationHandler.END

    rows.append([InlineKeyboardButton("Cancel", callback_data="pu:cancel")])
    await update.message.reply_text(
        "<b>Which shift would you like to take?</b>\n\n"
        "<i>Your manager has to approve it, so don't count on it "
        "until they do.</i>",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return PICKUP_PICK


async def on_pickup_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    raw = query.data.split(":", 1)[1]
    await query.answer()
    if raw == "cancel":
        await query.edit_message_text("No problem — nothing changed.")
        return ConversationHandler.END

    slot = q1(
        """SELECT s.id, s.label, d.name AS day_name FROM slots s
           JOIN days d ON d.id = s.day_id WHERE s.id=?""",
        (int(raw),),
    )
    if not slot:
        await query.edit_message_text("That shift no longer exists.")
        return ConversationHandler.END

    context.user_data["pickup_slot"] = slot["id"]
    await query.edit_message_text(
        f"<b>{slot['day_name']} {esc(slot['label'])}</b>\n\n"
        "Anything your manager should know? Send a short note, "
        "or just say <code>-</code>.",
        parse_mode=constants.ParseMode.HTML,
    )
    return PICKUP_REASON


async def got_pickup_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    slot_id = context.user_data.pop("pickup_slot", None)
    if not slot_id:
        await update.message.reply_text("That request expired — send /pickup again.")
        return ConversationHandler.END

    note = " ".join(update.message.text.split()).strip()
    if note == "-":
        note = ""

    slot = q1(
        """SELECT s.id, s.label, d.name AS day_name FROM slots s
           JOIN days d ON d.id = s.day_id WHERE s.id=?""",
        (slot_id,),
    )
    async with write_lock:
        cur = run(
            "INSERT INTO drop_requests (kind, agent_id, slot_id, reason, requested_at) "
            "VALUES ('pickup',?,?,?,?)",
            (user.id, slot_id, note, now().isoformat()),
        )
        req_id = cur.lastrowid

    nm = display_name_of(user.id, user.full_name)
    await update.message.reply_text(
        "✅ Sent to your manager.\n\n"
        f"<b>{slot['day_name']} {esc(slot['label'])}</b>\n\n"
        "You're not on it until they approve.",
        parse_mode=constants.ParseMode.HTML,
    )

    text = (
        "🙌 <b>Shift pickup request</b>\n\n"
        f"<b>{esc(nm)}</b> would like to take "
        f"<b>{slot['day_name']} {esc(slot['label'])}</b>"
    )
    if note:
        text += f"\n<i>{esc(note)}</i>"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"dq:a:{req_id}"),
        InlineKeyboardButton("🚫 Decline", callback_data=f"dq:d:{req_id}"),
    ]])
    sent = await notify_admins(context.bot, text, kb)
    run("UPDATE drop_requests SET notified=? WHERE id=?",
        (json.dumps(sent), req_id))
    return ConversationHandler.END


async def cmd_swap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Hand a shift straight to a named colleague."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly for that 🙂")
        return ConversationHandler.END
    if not await gate(update):
        return ConversationHandler.END

    user = update.effective_user
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return ConversationHandler.END

    today = now().date().isoformat()
    mine = q(
        """SELECT s.id, s.label, d.name AS day_name FROM signups su
           JOIN slots s ON s.id = su.slot_id
           JOIN days d ON d.id = s.day_id
           WHERE d.week_id=? AND su.user_id=? AND d.the_date >= ?
           ORDER BY d.idx, s.idx""",
        (w["id"], user.id, today),
    )
    if not mine:
        await update.message.reply_text("You have no upcoming shifts this week.")
        return ConversationHandler.END

    pending = {
        r["slot_id"] for r in q(
            "SELECT slot_id FROM drop_requests WHERE agent_id=? AND status='pending'",
            (user.id,),
        )
    }
    rows = [
        [InlineKeyboardButton(f"{m['day_name']} {m['label']}",
                              callback_data=f"sw:{m['id']}")]
        for m in mine if m["id"] not in pending
    ]
    if not rows:
        await update.message.reply_text(
            "You've already got requests in for all of those."
        )
        return ConversationHandler.END
    rows.append([InlineKeyboardButton("Cancel", callback_data="sw:cancel")])
    await update.message.reply_text(
        "<b>Which shift are you handing over?</b>",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return SWAP_PICK


async def on_swap_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    raw = query.data.split(":", 1)[1]
    await query.answer()
    if raw == "cancel":
        await query.edit_message_text("No problem — nothing changed.")
        return ConversationHandler.END

    slot = q1(
        """SELECT s.id, s.label, s.capacity, d.name AS day_name FROM slots s
           JOIN days d ON d.id = s.day_id WHERE s.id=?""",
        (int(raw),),
    )
    if not slot:
        await query.edit_message_text("That shift no longer exists.")
        return ConversationHandler.END

    context.user_data["swap_slot"] = slot["id"]
    holders = {h["user_id"] for h in slot_holders(slot["id"])}
    others = [
        a for a in q("SELECT * FROM agents WHERE status='active' AND user_id IN (SELECT user_id FROM memberships WHERE dept='LIVECHAT') ORDER BY name")
        if a["user_id"] != query.from_user.id and a["user_id"] not in holders
    ]
    if not others:
        await query.edit_message_text("There's nobody else to hand it to.")
        return ConversationHandler.END

    rows, row = [], []
    for a in others:
        nm = a["display_name"] or a["name"]
        row.append(InlineKeyboardButton(nm[:20], callback_data=f"sq:{a['user_id']}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Cancel", callback_data="sq:cancel")])

    await query.edit_message_text(
        f"<b>{slot['day_name']} {esc(slot['label'])}</b>\n\n"
        "Who's taking it?",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return SWAP_WHO


async def on_swap_who(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    raw = query.data.split(":", 1)[1]
    await query.answer()
    if raw == "cancel":
        await query.edit_message_text("No problem — nothing changed.")
        return ConversationHandler.END

    context.user_data["swap_to"] = int(raw)
    nm = display_name_of(int(raw), "them")
    await query.edit_message_text(
        f"Handing it to <b>{esc(nm)}</b>.\n\n"
        "Why? A short reason is fine — your manager sees this.",
        parse_mode=constants.ParseMode.HTML,
    )
    return SWAP_REASON


async def got_swap_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    slot_id = context.user_data.pop("swap_slot", None)
    to_id = context.user_data.pop("swap_to", None)
    if not slot_id or not to_id:
        await update.message.reply_text("That expired — send /swap again.")
        return ConversationHandler.END

    reason = " ".join(update.message.text.split()).strip()
    if len(reason) < 3:
        await update.message.reply_text("A little more detail, or /cancel.")
        context.user_data["swap_slot"] = slot_id
        context.user_data["swap_to"] = to_id
        return SWAP_REASON

    slot = q1(
        """SELECT s.label, d.name AS day_name FROM slots s
           JOIN days d ON d.id = s.day_id WHERE s.id=?""",
        (slot_id,),
    )
    async with write_lock:
        cur = run(
            "INSERT INTO drop_requests (kind, agent_id, slot_id, target_id, reason,"
            " requested_at) VALUES ('swap',?,?,?,?,?)",
            (user.id, slot_id, to_id, reason, now().isoformat()),
        )
        req_id = cur.lastrowid

    from_nm = display_name_of(user.id, user.full_name)
    to_nm = display_name_of(to_id, "them")
    where = f"{slot['day_name']} {slot['label']}"

    await update.message.reply_text(
        f"✅ Sent to your manager.\n\n"
        f"<b>{esc(where)}</b> → {esc(to_nm)}\n\n"
        "You're still on it until they approve.",
        parse_mode=constants.ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            to_id,
            f"🔄 {from_nm} has asked to hand you {where}.\n"
            f"Reason: {reason}\n\n"
            "Waiting on a manager to approve it.",
        )
    except Exception:
        pass

    text = (
        "🔄 <b>Shift swap request</b>\n\n"
        f"<b>{esc(from_nm)}</b> → <b>{esc(to_nm)}</b>\n"
        f"{esc(where)}\n"
        f"<i>{esc(reason)}</i>"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"dq:a:{req_id}"),
        InlineKeyboardButton("🚫 Decline", callback_data=f"dq:d:{req_id}"),
    ]])
    sent = await notify_admins(context.bot, text, kb)
    run("UPDATE drop_requests SET notified=? WHERE id=?",
        (json.dumps(sent), req_id))
    return ConversationHandler.END


async def on_drop_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, action, raw = query.data.split(":")
    if not is_admin(query.from_user.id):
        await query.answer("Only admins can decide this.", show_alert=True)
        return

    req = q1("SELECT * FROM drop_requests WHERE id=?", (int(raw),))
    if not req:
        await query.answer("That request has gone.", show_alert=True)
        return
    if req["status"] != "pending":
        await query.answer(f"Already {req['status']}.", show_alert=True)
        return
    if req["kind"] == "exchange":
        await decide_exchange(query, context, req, action == "a")
        return

    slot = q1(
        """SELECT s.id, s.label, d.id AS day_id, d.name AS day_name, d.week_id
           FROM slots s JOIN days d ON d.id = s.day_id WHERE s.id=?""",
        (req["slot_id"],),
    )
    nm = display_name_of(req["agent_id"], str(req["agent_id"]))
    approve = action == "a"
    is_pickup = req["kind"] == "pickup"
    is_swap = req["kind"] == "swap"
    to_nm = display_name_of(req["target_id"], "them") if req["target_id"] else ""
    where = f"{slot['day_name']} {slot['label']}" if slot else "that shift"

    async with write_lock:
        run(
            "UPDATE drop_requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
            ("approved" if approve else "declined",
             query.from_user.id, now().isoformat(), req["id"]),
        )
        if approve and slot:
            if is_swap:
                # One move, so the slot is never open for anyone else to take.
                run(
                    "DELETE FROM signups WHERE slot_id=? AND user_id=?",
                    (slot["id"], req["agent_id"]),
                )
                run(
                    "INSERT OR IGNORE INTO signups (slot_id, user_id, name, ts) "
                    "VALUES (?,?,?,?)",
                    (slot["id"], req["target_id"], to_nm, now().isoformat()),
                )
            elif is_pickup:
                run(
                    "INSERT OR IGNORE INTO signups (slot_id, user_id, name, ts) "
                    "VALUES (?,?,?,?)",
                    (slot["id"], req["agent_id"], nm, now().isoformat()),
                )
            else:
                run(
                    "DELETE FROM signups WHERE slot_id=? AND user_id=?",
                    (slot["id"], req["agent_id"]),
                )

    verdict = "approved ✅" if approve else "declined 🚫"
    await query.answer(f"{nm} {verdict}")
    title = ("Shift swap request" if is_swap
             else "Shift pickup request" if is_pickup else "Shift drop request")
    icon = "🔄" if is_swap else "🙌" if is_pickup else "🙋"
    settled = (
        f"{icon} <b>{title}</b>\n\n"
        f"<b>{esc(nm)}</b> — {esc(where)}\n"
        + (f"<i>{esc(req['reason'])}</i>\n" if req["reason"] else "")
        + f"\n{verdict} by {esc(display_name_of(query.from_user.id, 'admin'))}"
    )
    try:
        sent = json.loads(req["notified"] or "[]")
    except (json.JSONDecodeError, TypeError):
        sent = []
    if sent:
        await settle_admin_messages(context.bot, sent, settled)
    else:
        try:
            await query.edit_message_text(
                settled, parse_mode=constants.ParseMode.HTML
            )
        except BadRequest:
            pass

    if is_swap:
        try:
            if approve:
                await context.bot.send_message(
                    req["agent_id"],
                    f"✅ {to_nm} is taking {where}. You're off it.\n\n"
                    "Check /myshifts.",
                )
                await context.bot.send_message(
                    req["target_id"],
                    f"✅ You're on {where}, taking over from {nm}.\n\n"
                    "Check /myshifts.",
                )
            else:
                await context.bot.send_message(
                    req["agent_id"],
                    f"Your swap for {where} wasn't approved.\n"
                    "You're still on that shift.",
                )
                await context.bot.send_message(
                    req["target_id"],
                    f"The swap for {where} wasn't approved — "
                    f"{nm} is keeping it.",
                )
        except Exception:
            pass
        if approve and slot:
            await refresh_group(context, slot["week_id"], slot["day_id"])
        return

    try:
        if approve and is_pickup:
            msg = (f"✅ You're on {where}.\n\nCheck /myshifts.")
        elif approve:
            msg = (f"✅ You've been taken off {where}.\n\n"
                   "It's open for someone else now. Check /myshifts.")
        elif is_pickup:
            msg = (f"Your request to take {where} wasn't approved.\n\n"
                   "Speak to your manager if you'd still like it.")
        else:
            msg = (f"Your request to drop {where} wasn't approved.\n\n"
                   "You're still on that shift — speak to your manager.")
        await context.bot.send_message(req["agent_id"], msg)
    except Exception:
        pass

    if approve and slot:
        await refresh_group(context, slot["week_id"], slot["day_id"])


async def cmd_dropreqs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shift requests waiting on a decision — both drops and pickups."""
    if not is_admin(update.effective_user.id):
        return
    rows = q(
        "SELECT * FROM drop_requests WHERE status='pending' ORDER BY requested_at"
    )
    if not rows:
        await update.message.reply_text("No shift drop requests waiting.")
        return
    await update.message.reply_text(f"{len(rows)} waiting:")
    for r in rows:
        slot = q1(
            """SELECT s.label, d.name AS day_name FROM slots s
               JOIN days d ON d.id = s.day_id WHERE s.id=?""",
            (r["slot_id"],),
        )
        where = f"{slot['day_name']} {slot['label']}" if slot else "unknown shift"
        nm = display_name_of(r["agent_id"], str(r["agent_id"]))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"dq:a:{r['id']}"),
            InlineKeyboardButton("🚫 Decline", callback_data=f"dq:d:{r['id']}"),
        ]])
        if r["kind"] == "exchange":
            await reply_long(update.message, exchange_text(r), reply_markup=kb,
                             parse_mode=constants.ParseMode.HTML)
            continue
        icon = ("🔄 swap" if r["kind"] == "swap"
                else "🙌 wants" if r["kind"] == "pickup" else "🙋 can't work")
        await reply_long(update.message, 
            f"{icon} — <b>{esc(nm)}</b>, {esc(where)}\n"
            + (f"<i>{esc(r['reason'])}</i>" if r["reason"] else ""),
            reply_markup=kb, parse_mode=constants.ParseMode.HTML,
        )


async def cmd_myshifts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly for that 🙂")
        return
    if not await gate(update):
        return
    wants_next = bool(context.args) and context.args[0].lower() in ("next", "new")
    here = this_week()
    nxt = open_week()
    w = nxt if wants_next and nxt else here
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return
    rows = q(
        """SELECT d.name, d.the_date, s.label FROM signups su
           JOIN slots s ON s.id = su.slot_id JOIN days d ON d.id = s.day_id
           WHERE d.week_id=? AND su.user_id=? ORDER BY d.idx, s.idx""",
        (w["id"], update.effective_user.id),
    )
    hint = ""
    if nxt and here and nxt["id"] != here["id"]:
        hint = (
            f"\n<i>{esc(nxt['label'])} is open — /myshifts next</i>"
            if not wants_next
            else "\n<i>/myshifts for this week</i>"
        )
    if not rows:
        await reply_long(update.message, 
            f"You haven't claimed anything for {esc(w['label'])} yet." + hint,
            parse_mode=constants.ParseMode.HTML,
        )
        return
    lines = [f"<b>Your shifts — {esc(w['label'])}</b>"] + [
        f"{r['name']} {fmt_day(date.fromisoformat(r['the_date']))}: {r['label']}"
        for r in rows
    ]
    lines.append(f"\n{len(rows)} slot(s)")
    await reply_long(update.message, 
        "\n".join(lines) + hint, parse_mode=constants.ParseMode.HTML
    )


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Use /schedule here, or message me directly.")
        return
    if not await gate(update):
        return

    wants_next = bool(context.args) and context.args[0].lower() in ("next", "new")
    here = this_week()
    nxt = open_week()
    w = nxt if wants_next and nxt else here
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return

    tail = ""
    if nxt and here and nxt["id"] != here["id"]:
        tail = (
            f"\n\n<i>Showing {esc(here['label'])}. "
            f"{esc(nxt['label'])} is open for avails — /summary next</i>"
            if not wants_next else
            f"\n\n<i>Showing {esc(nxt['label'])}, open for avails. "
            "/summary for this week</i>"
        )

    if BOARD_MODE == "single":
        text, _ = render_board(w["id"])
        await reply_long(update.message, 
            text + tail, parse_mode=constants.ParseMode.HTML
        )
        return
    lines = [render_header(w["id"])[0], ""]
    for d in q("SELECT * FROM days WHERE week_id=? ORDER BY idx", (w["id"],)):
        day_text, _ = render_day(d["id"])
        lines.append(day_text)
        lines.append("")
    await reply_long(update.message, 
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
    )


async def cmd_dropslot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dropslot @handle MON 10am-12pm — free one slot without touching anything else."""
    if not is_admin(update.effective_user.id):
        return
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return

    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: <code>/dropslot @handle MON 10am-12pm</code>\n\n"
            "Frees that one slot so someone else can take it. "
            "Everything else they've claimed stays.\n"
            "See what they have with <code>/whohas MON 10am-12pm</code>.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    row = find_agent(context.args[0])
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return
    day = norm_day(context.args[1])
    if not day:
        await update.message.reply_text(f"Use one of: {', '.join(DAY_NAMES)}")
        return
    label = " ".join(context.args[2:]).strip()

    slot = q1(
        """SELECT s.id, s.label, d.id AS day_id FROM slots s
           JOIN days d ON d.id = s.day_id
           WHERE d.week_id=? AND d.name=? AND lower(s.label)=lower(?)""",
        (w["id"], day, label),
    )
    if not slot:
        await update.message.reply_text(
            f"No {label!r} slot on {day} this week. Check the wording on the board."
        )
        return

    nm = row["display_name"] or row["name"]
    held = q1(
        "SELECT 1 FROM signups WHERE slot_id=? AND user_id=?",
        (slot["id"], row["user_id"]),
    )
    if not held:
        holders = slot_holders(slot["id"])
        who = ", ".join(h["name"] for h in holders) if holders else "nobody"
        await update.message.reply_text(
            f"<b>{esc(nm)}</b> doesn't hold {day} {esc(slot['label'])}.\n"
            f"Currently: {esc(who)}.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    async with write_lock:
        run(
            "DELETE FROM signups WHERE slot_id=? AND user_id=?",
            (slot["id"], row["user_id"]),
        )

    left = q1(
        """SELECT COUNT(*) c FROM signups su JOIN slots s ON s.id=su.slot_id
           JOIN days d ON d.id=s.day_id WHERE d.week_id=? AND su.user_id=?""",
        (w["id"], row["user_id"]),
    )["c"]

    await update.message.reply_text(
        f"Freed <b>{day} {esc(slot['label'])}</b> from <b>{esc(nm)}</b>.\n"
        f"They still have {left} slot(s) this week.",
        parse_mode=constants.ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            row["user_id"],
            f"Your manager has taken you off {day} {slot['label']} this week.\n\n"
            "Check /myshifts for what you still have.",
        )
    except Exception:
        pass

    await refresh_group(context, w["id"], slot["day_id"])


async def cmd_whohas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whohas MON 10am-12pm — who is on that slot."""
    if not is_admin(update.effective_user.id):
        return
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week posted yet.")
        return
    if len(context.args) < 2:
        await reply_long(update.message, 
            "Usage: <code>/whohas MON 10am-12pm</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    day = norm_day(context.args[0])
    if not day:
        await update.message.reply_text(f"Use one of: {', '.join(DAY_NAMES)}")
        return
    label = " ".join(context.args[1:]).strip()
    slot = q1(
        """SELECT s.id, s.label, s.capacity FROM slots s JOIN days d ON d.id = s.day_id
           WHERE d.week_id=? AND d.name=? AND lower(s.label)=lower(?)""",
        (w["id"], day, label),
    )
    if not slot:
        await update.message.reply_text(f"No {label!r} slot on {day}.")
        return
    holders = slot_holders(slot["id"])
    cap = slot["capacity"] or SLOT_CAPACITY
    if not holders:
        await reply_long(update.message, 
            f"<b>{day} {esc(slot['label'])}</b> — nobody yet (0/{cap})",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    lines = [f"<b>{day} {esc(slot['label'])}</b> — {len(holders)}/{cap}", ""]
    for h in holders:
        a = q1("SELECT username FROM agents WHERE user_id=?", (h["user_id"],))
        handle = f" @{a['username']}" if a and a["username"] else ""
        lines.append(f"{esc(h['name'])}{handle}")
    lines.append(
        f"\n<code>/dropslot @handle {day} {slot['label']}</code> to free one"
    )
    await reply_long(update.message, 
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
    )


async def cmd_applyfixed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fill fixed rosters into the week that's already posted."""
    if not is_admin(update.effective_user.id):
        return
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return
    async with write_lock:
        filled = apply_fixed_slots(w["id"])
    if filled:
        await update.message.reply_text(
            f"Filled <b>{filled}</b> slot(s) into {w['label']} from fixed rosters.",
            parse_mode=constants.ParseMode.HTML,
        )
        await refresh_group(context, w["id"])
    else:
        await update.message.reply_text(
            "Nothing to fill. Either they're already on the board, "
            "someone else holds those slots, or the slot doesn't exist "
            "this week.\n\nCheck with /fixed and /whohas.",
        )


async def cmd_addslot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addslot MON 6pm-8pm — put a slot back on a day of the live week."""
    if not is_admin(update.effective_user.id):
        return
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/addslot MON 6pm-8pm</code>\n\n"
            "Adds that slot to the live week. Use it to undo a holiday or "
            "campaign day that was set by mistake.\n"
            "Several at once: <code>/addslot MON 6pm-8pm, 8pm-10pm</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    day = norm_day(context.args[0])
    if not day:
        await update.message.reply_text(f"Use one of: {', '.join(DAY_NAMES)}")
        return
    labels = [x.strip() for x in " ".join(context.args[1:]).split(",") if x.strip()]

    d = q1(
        "SELECT * FROM days WHERE week_id=? AND name=?", (w["id"], day)
    )
    if not d:
        await update.message.reply_text(
            f"{day} isn't part of {esc(w['label'])}.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    added, skipped = [], []
    async with write_lock:
        for label in labels:
            try:
                start = slot_start_minutes(label)
            except ValueError:
                skipped.append(f"{label} (not a time)")
                continue
            dupe = q1(
                "SELECT 1 FROM slots WHERE day_id=? AND lower(label)=lower(?)",
                (d["id"], label),
            )
            if dupe:
                skipped.append(f"{label} (already there)")
                continue
            run(
                "INSERT INTO slots (day_id, idx, label, start_min) VALUES (?,?,?,?)",
                (d["id"], 999, label, start),
            )
            added.append(label)
        # keep the day in time order
        for i, row in enumerate(
            q("SELECT id FROM slots WHERE day_id=? ORDER BY start_min, id", (d["id"],))
        ):
            run("UPDATE slots SET idx=? WHERE id=?", (i, row["id"]))

    msg = []
    if added:
        msg.append(f"✅ Added to <b>{day}</b>: {esc(', '.join(added))}")
    if skipped:
        msg.append(f"Skipped: {esc(', '.join(skipped))}")
    if added:
        msg.append("\n<i>Agents can claim it if the week is still open. "
                   "If it's closed, they can ask with /pickup.</i>")
    await update.message.reply_text(
        "\n".join(msg), parse_mode=constants.ParseMode.HTML
    )
    if added:
        await refresh_group(context, w["id"], d["id"])


async def cmd_delslot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/delslot MON 6pm-8pm — take a slot off the live week."""
    if not is_admin(update.effective_user.id):
        return
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/delslot MON 6pm-8pm</code>\n\n"
            "Removes that slot from the live week. If someone's on it, "
            "I'll ask you to confirm.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    args = [a for a in context.args if a.upper() != "CONFIRM"]
    forced = len(args) != len(context.args)
    day = norm_day(args[0])
    if not day:
        await update.message.reply_text(f"Use one of: {', '.join(DAY_NAMES)}")
        return
    label = " ".join(args[1:]).strip()

    slot = q1(
        """SELECT s.id, s.label, d.id AS day_id FROM slots s
           JOIN days d ON d.id = s.day_id
           WHERE d.week_id=? AND d.name=? AND lower(s.label)=lower(?)""",
        (w["id"], day, label),
    )
    if not slot:
        await update.message.reply_text(
            f"No {label!r} slot on {day} this week. Check the board's wording."
        )
        return

    holders = slot_holders(slot["id"])
    if holders and not forced:
        who = ", ".join(h["name"] for h in holders)
        await update.message.reply_text(
            f"⚠️ <b>{day} {esc(slot['label'])}</b> is held by "
            f"<b>{esc(who)}</b>.\n\n"
            "Removing it takes them off the shift and tells them.\n\n"
            f"To go ahead: <code>/delslot {day} {slot['label']} CONFIRM</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    async with write_lock:
        run("DELETE FROM signups WHERE slot_id=?", (slot["id"],))
        run("DELETE FROM slots WHERE id=?", (slot["id"],))
        for i, row in enumerate(
            q("SELECT id FROM slots WHERE day_id=? ORDER BY start_min, id",
              (slot["day_id"],))
        ):
            run("UPDATE slots SET idx=? WHERE id=?", (i, row["id"]))

    note = f"🗑 Removed <b>{day} {esc(slot['label'])}</b>."
    if holders:
        note += f"\n{len(holders)} agent(s) taken off it and told."
    await update.message.reply_text(note, parse_mode=constants.ParseMode.HTML)

    for h in holders:
        try:
            await context.bot.send_message(
                h["user_id"],
                f"{day} {slot['label']} has been removed from the schedule, "
                "so you're no longer on it.\n\nCheck /myshifts.",
            )
        except Exception:
            pass
    await refresh_group(context, w["id"], slot["day_id"])


async def cmd_capacity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/capacity 10am-12pm 2 — how many agents a slot takes this week."""
    if not is_admin(update.effective_user.id):
        return
    w = open_week() or latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return

    if not context.args:
        rows = q(
            """SELECT d.name AS day, s.label, s.capacity,
                      (SELECT COUNT(*) FROM signups WHERE slot_id=s.id) AS n
               FROM slots s JOIN days d ON d.id = s.day_id
               WHERE d.week_id=? AND s.capacity IS NOT NULL AND s.capacity <> ?
               ORDER BY d.idx, s.idx""",
            (w["id"], SLOT_CAPACITY),
        )
        lines = [f"<b>Slots taking more than {SLOT_CAPACITY} — {w['label']}</b>", ""]
        if not rows:
            lines.append(f"None. Every slot takes {SLOT_CAPACITY}.")
        for r in rows:
            lines.append(f"{r['day']} {r['label']} — {r['n']}/{r['capacity']}")
        lines += [
            "",
            "<code>/capacity 10am-12pm 2</code> — every 10am-12pm this week",
            "<code>/capacity MON 10am-12pm 2</code> — just that day",
            "<code>/capacity 10am-12pm 1</code> — back to one",
        ]
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    args = list(context.args)
    day = None
    if norm_day(args[0]):
        day = norm_day(args.pop(0))
    if len(args) < 2 or not args[-1].isdigit():
        await update.message.reply_text(
            "Usage: <code>/capacity 10am-12pm 2</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    n = int(args.pop())
    if not 1 <= n <= 10:
        await update.message.reply_text("Pick a number between 1 and 10.")
        return
    label = " ".join(args).strip()

    if day:
        target = q(
            """SELECT s.id FROM slots s JOIN days d ON d.id = s.day_id
               WHERE d.week_id=? AND d.name=? AND lower(s.label)=lower(?)""",
            (w["id"], day, label),
        )
    else:
        target = q(
            """SELECT s.id FROM slots s JOIN days d ON d.id = s.day_id
               WHERE d.week_id=? AND lower(s.label)=lower(?)""",
            (w["id"], label),
        )
    if not target:
        await update.message.reply_text(
            f"No {label!r} slots{' on ' + day if day else ''} this week. "
            "Check the exact wording on the board."
        )
        return

    async with write_lock:
        for t in target:
            run("UPDATE slots SET capacity=? WHERE id=?", (n, t["id"]))

    where = f"on {day}" if day else "every day that has it"
    await update.message.reply_text(
        f"<b>{esc(label)}</b> now takes <b>{n}</b> agent(s), {where}.\n"
        f"{len(target)} slot(s) updated.",
        parse_mode=constants.ParseMode.HTML,
    )
    await refresh_group(context, w["id"])


async def cmd_fixed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Slots someone always works, filled in automatically each week.

    Meant for full-timers whose hours don't change.
    """
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        rows = q(
            """SELECT f.user_id, a.display_name, a.name, COUNT(*) AS n
               FROM fixed_slots f JOIN agents a ON a.user_id = f.user_id
               GROUP BY f.user_id ORDER BY a.name"""
        )
        lines = ["<b>Fixed weekly rosters</b>", ""]
        if not rows:
            lines.append("Nobody has one yet.")
        for r in rows:
            slots = q(
                "SELECT day_name, label FROM fixed_slots WHERE user_id=?",
                (r["user_id"],),
            )
            by_day = {}
            for sl in slots:
                by_day.setdefault(sl["day_name"], []).append(sl["label"])
            detail = "; ".join(
                f"{d} {', '.join(sorted(by_day[d], key=slot_start_minutes))}"
                for d in DAY_NAMES if d in by_day
            )
            lines.append(f"<b>{esc(r['display_name'] or r['name'])}</b> — {detail}")
        lines += [
            "",
            "<code>/fixed @handle MON 10am-12pm</code> — add or remove a slot",
            "<code>/fixed @handle clear</code> — wipe theirs",
            "",
            "<i>These fill in automatically when a week is posted.</i>",
        ]
        await reply_long(update.message, 
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    row = find_agent(context.args[0])
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return
    nm = row["display_name"] or row["name"]

    if len(context.args) == 1:
        slots = q(
            "SELECT day_name, label FROM fixed_slots WHERE user_id=?", (row["user_id"],)
        )
        if not slots:
            await reply_long(update.message, 
                f"<b>{esc(nm)}</b> has no fixed slots.\n\n"
                f"Add one: <code>/fixed {context.args[0]} MON 10am-12pm</code>",
                parse_mode=constants.ParseMode.HTML,
            )
            return
        by_day = {}
        for sl in slots:
            by_day.setdefault(sl["day_name"], []).append(sl["label"])
        lines = [f"<b>{esc(nm)}</b> works these every week:", ""]
        for d in DAY_NAMES:
            if d in by_day:
                lines.append(
                    f"{d}: {', '.join(sorted(by_day[d], key=slot_start_minutes))}"
                )
        await reply_long(update.message, 
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    if context.args[1].lower() == "clear":
        async with write_lock:
            run("DELETE FROM fixed_slots WHERE user_id=?", (row["user_id"],))
        await reply_long(update.message, 
            f"Cleared the fixed roster for <b>{esc(nm)}</b>.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    if len(context.args) < 3:
        await reply_long(update.message, 
            "Usage: <code>/fixed @handle MON 10am-12pm</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    day = norm_day(context.args[1])
    if not day:
        await update.message.reply_text(f"Use one of: {', '.join(DAY_NAMES)}")
        return
    label = " ".join(context.args[2:]).strip()
    try:
        slot_start_minutes(label)
    except ValueError:
        await update.message.reply_text("Give a slot like 10am-12pm.")
        return

    async with write_lock:
        existing = q1(
            "SELECT 1 FROM fixed_slots WHERE user_id=? AND day_name=? AND lower(label)=lower(?)",
            (row["user_id"], day, label),
        )
        if existing:
            run(
                "DELETE FROM fixed_slots WHERE user_id=? AND day_name=? AND lower(label)=lower(?)",
                (row["user_id"], day, label),
            )
            verb = "Removed"
        else:
            run(
                "INSERT INTO fixed_slots (user_id, day_name, label) VALUES (?,?,?)",
                (row["user_id"], day, label),
            )
            verb = "Added"

    total = q1(
        "SELECT COUNT(*) c FROM fixed_slots WHERE user_id=?", (row["user_id"],)
    )["c"]
    await reply_long(update.message, 
        f"{verb} <b>{day} {esc(label)}</b> for <b>{esc(nm)}</b>. "
        f"They now have {total} fixed slot(s).\n\n"
        "<i>Applies automatically to the next week you post. "
        "For the week already up, send /applyfixed.</i>",
        parse_mode=constants.ParseMode.HTML,
    )


# --------------------------------------------------------------------------
# /changeshift — one place to drop, give or exchange one of your shifts.
# /dropshift and /swap open it too. Callbacks: cg:p pick · cg:w what ·
# cg:u who · cg:t their shift · cg:back · cg:x cancel; xa:y|n the other
# person agreeing to an exchange.
# --------------------------------------------------------------------------

CG_PICK, CG_WHAT, CG_WHO, CG_THEIRS, CG_REASON = 290, 291, 292, 293, 294


def shift_name(slot_id: int) -> str:
    s = q1("""SELECT s.label, d.the_date FROM slots s JOIN days d ON d.id = s.day_id
              WHERE s.id=?""", (slot_id,))
    if not s:
        return "that shift"
    return f"{date.fromisoformat(s['the_date']):%a %-d %b} {s['label']}"


def upcoming_shifts(user_id: int) -> list:
    """Someone's shifts from today on, in the open (or latest) week, with no
    request already waiting on them."""
    w = open_week() or latest_week()
    if not w:
        return []
    busy = {r["slot_id"] for r in q(
        "SELECT slot_id FROM drop_requests WHERE agent_id=? AND status IN ('pending','awaiting')",
        (user_id,))}
    rows = q("""SELECT s.id, s.label, s.start_min, d.id AS day_id, d.the_date FROM signups su
                JOIN slots s ON s.id = su.slot_id JOIN days d ON d.id = s.day_id
                WHERE d.week_id=? AND su.user_id=? AND d.the_date >= ?
                ORDER BY d.idx, s.idx""", (w["id"], user_id, now().date().isoformat()))
    return [r for r in rows if r["id"] not in busy]


def clashes(slot, mine: list) -> bool:
    """Whether this shift overlaps one of mine (same day, times cross)."""
    a1 = slot["start_min"]
    a2 = a1 + slot_minutes(slot["label"])
    for m in mine:
        if m["day_id"] != slot["day_id"]:
            continue
        b1 = m["start_min"]
        b2 = b1 + slot_minutes(m["label"])
        if a1 < b2 and b1 < a2:
            return True
    return False


def slot_row(slot_id: int):
    return q1("""SELECT s.id, s.label, s.start_min, d.id AS day_id FROM slots s
                 JOIN days d ON d.id = s.day_id WHERE s.id=?""", (slot_id,))


def exchange_options(me: int, my_slot: int, them: int) -> list:
    """Their shifts I could take for mine, with nobody left on two shifts at
    once: theirs mustn't clash with my other shifts, and mine mustn't clash
    with their other shifts."""
    mine = upcoming_shifts(me)
    keep = [m for m in mine if m["id"] != my_slot]
    given = slot_row(my_slot)
    out = []
    their_all = upcoming_shifts(them)
    for s in their_all:
        if s["id"] in {m["id"] for m in mine} or clashes(s, keep):
            continue
        if given and clashes(given, [t for t in their_all if t["id"] != s["id"]]):
            continue
        out.append(s)
    return out


def cg_back_row() -> list:
    return [InlineKeyboardButton("‹ Back", callback_data="cg:back"),
            InlineKeyboardButton("Cancel", callback_data="cg:x")]


async def cg_show(update: Update, context: ContextTypes.DEFAULT_TYPE, step: int) -> int:
    """Draw one step of /changeshift (in place after a button)."""
    d = context.user_data.setdefault("cg", {})
    d["step"] = step
    uid = update.effective_user.id
    if step == CG_PICK:
        mine = upcoming_shifts(uid)
        if not mine:
            text, kb = ("You have no upcoming shifts to change, or you've already asked about all of them.", None)
        else:
            rows = [[InlineKeyboardButton(shift_name(m["id"]), callback_data=f"cg:p:{m['id']}")] for m in mine]
            rows.append([InlineKeyboardButton("Cancel", callback_data="cg:x")])
            text, kb = ("<b>Which shift do you want to change?</b>", InlineKeyboardMarkup(rows))
    elif step == CG_WHAT:
        text = (f"<b>{esc(shift_name(d['slot']))}</b>\n\nWhat do you want to do?\n\n"
                "<i>Your manager approves it. You stay on the shift until then.</i>")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🙋 Drop it (back on the board)", callback_data="cg:w:drop")],
            [InlineKeyboardButton("➡️ Give it to someone", callback_data="cg:w:give")],
            [InlineKeyboardButton("🔁 Exchange it for one of theirs", callback_data="cg:w:ex")],
            cg_back_row()])
    elif step == CG_WHO:
        holders = {h["user_id"] for h in slot_holders(d["slot"])}
        people = [a for a in q("SELECT * FROM agents WHERE status='active' AND user_id IN "
                               "(SELECT user_id FROM memberships WHERE dept='LIVECHAT') ORDER BY name")
                  if a["user_id"] != uid and a["user_id"] not in holders]
        if d["mode"] == "ex":
            people = [a for a in people if exchange_options(uid, d["slot"], a["user_id"])]
        else:   # give: only people free at that time
            mine_slot = slot_row(d["slot"])
            people = [a for a in people if not clashes(mine_slot, upcoming_shifts(a["user_id"]))]
        btns = [InlineKeyboardButton((a["display_name"] or a["name"])[:20], callback_data=f"cg:u:{a['user_id']}")
                for a in people]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)] + [cg_back_row()]
        verb = "exchange with" if d["mode"] == "ex" else "give it to"
        text = (f"<b>{esc(shift_name(d['slot']))}</b>\n\nWho do you want to {verb}?"
                if people else "There's nobody to " + verb + " for this shift.")
        kb = InlineKeyboardMarkup(rows)
    elif step == CG_THEIRS:
        theirs = exchange_options(uid, d["slot"], d["to"])
        rows = [[InlineKeyboardButton(shift_name(s["id"]), callback_data=f"cg:t:{s['id']}")] for s in theirs]
        rows.append(cg_back_row())
        text = (f"<b>Which of {esc(display_name_of(d['to'], 'their'))}'s shifts are you taking?</b>\n"
                "<i>Only shifts that don't clash with yours.</i>")
        kb = InlineKeyboardMarkup(rows)
    else:  # CG_REASON
        lines = [f"<b>{esc(shift_name(d['slot']))}</b>"]
        if d["mode"] == "give":
            lines.append(f"Giving it to <b>{esc(display_name_of(d['to'], 'them'))}</b>.")
        elif d["mode"] == "ex":
            lines.append(f"Exchanging with <b>{esc(display_name_of(d['to'], 'them'))}</b> for "
                         f"<b>{esc(shift_name(d['theirs']))}</b>.")
        lines += ["", "Why? A short reason is fine. Your manager sees this."]
        text, kb = "\n".join(lines), InlineKeyboardMarkup([cg_back_row()])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=kb)
    if step == CG_PICK and kb is None:
        return ConversationHandler.END
    return step


async def cmd_changeshift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly for that 🙂")
        return ConversationHandler.END
    if not await gate(update):
        return ConversationHandler.END
    if not (open_week() or latest_week()):
        await update.message.reply_text("No week has been posted yet.")
        return ConversationHandler.END
    context.user_data["cg"] = {}
    return await cg_show(update, context, CG_PICK)


CG_BACK = {CG_WHAT: CG_PICK, CG_WHO: CG_WHAT, CG_THEIRS: CG_WHO}


async def on_cg_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("cg")
    parts = query.data.split(":")
    if parts[1] == "x" or d is None:
        context.user_data.pop("cg", None)
        await query.edit_message_text("No problem. Nothing changed.")
        return ConversationHandler.END
    if parts[1] == "back":
        step = d.get("step")
        if step == CG_REASON:
            back = {"drop": CG_WHAT, "give": CG_WHO, "ex": CG_THEIRS}[d["mode"]]
        else:
            back = CG_BACK.get(step, CG_PICK)
        return await cg_show(update, context, back)
    kind, val = parts[1], parts[2]
    if kind == "p":
        d["slot"] = int(val)
        return await cg_show(update, context, CG_WHAT)
    if kind == "w":
        d["mode"] = val
        return await cg_show(update, context, CG_REASON if val == "drop" else CG_WHO)
    if kind == "u":
        d["to"] = int(val)
        return await cg_show(update, context, CG_THEIRS if d["mode"] == "ex" else CG_REASON)
    if kind == "t":
        d["theirs"] = int(val)
        return await cg_show(update, context, CG_REASON)
    return d.get("step", CG_PICK)


async def got_cg_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data.get("cg") or {}
    if "slot" not in d or "mode" not in d:
        await update.message.reply_text("That expired. Send /changeshift again.")
        return ConversationHandler.END
    reason = " ".join((update.message.text or "").split()).strip()
    if len(reason) < 3:
        await update.message.reply_text("A little more detail, or /cancel.")
        return CG_REASON
    user = update.effective_user
    me = display_name_of(user.id, user.full_name)
    mine = shift_name(d["slot"])
    mode = d["mode"]
    kind = {"drop": "drop", "give": "swap", "ex": "exchange"}[mode]
    async with write_lock:
        req_id = run(
            "INSERT INTO drop_requests (kind, agent_id, slot_id, target_id, reason, requested_at, status)"
            " VALUES (?,?,?,?,?,?,?)",
            (kind, user.id, d["slot"], d.get("to"), reason, now().isoformat(),
             "awaiting" if mode == "ex" else "pending")).lastrowid
        if mode == "ex":
            set_setting(f"exchange_back:{req_id}", str(d["theirs"]))
    context.user_data.pop("cg", None)

    if mode == "ex":
        theirs = shift_name(d["theirs"])
        to_nm = display_name_of(d["to"], "them")
        await update.message.reply_text(
            f"✅ Asked <b>{esc(to_nm)}</b> first.\n\n<b>You give:</b> {esc(mine)}\n<b>You take:</b> {esc(theirs)}\n\n"
            "Once they agree, it goes to your manager. You both stay on your own shifts until it's approved.",
            parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(
                d["to"],
                f"🔁 <b>{esc(me)} asks to exchange shifts with you</b>\n\n"
                f"<b>You take:</b> {esc(mine)}\n<b>You give:</b> {esc(theirs)}\n<i>{esc(reason)}</i>\n\n"
                "If you agree, it goes to a manager to approve.",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Agree", callback_data=f"xa:y:{req_id}"),
                    InlineKeyboardButton("❌ No", callback_data=f"xa:n:{req_id}")]]))
        except Exception:
            await update.message.reply_text(
                f"I couldn't message {to_nm}. Ask them to open the bot, then try again.")
            run("UPDATE drop_requests SET status='declined' WHERE id=?", (req_id,))
        return ConversationHandler.END

    if mode == "give":
        to_nm = display_name_of(d["to"], "them")
        await update.message.reply_text(
            f"✅ Sent to your manager.\n\n<b>{esc(mine)}</b> → {esc(to_nm)}\n\nYou're still on it until they approve.",
            parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(
                d["to"], f"➡️ {me} has asked to give you {mine}.\nReason: {reason}\n\nWaiting on a manager to approve it.")
        except Exception:
            pass
        text = f"🔄 <b>Shift swap request</b>\n\n<b>{esc(me)}</b> → <b>{esc(to_nm)}</b>\n{esc(mine)}\n<i>{esc(reason)}</i>"
    else:
        await update.message.reply_text(
            f"✅ Sent to your manager.\n\n<b>{esc(mine)}</b>\n<i>{esc(reason)}</i>\n\n"
            "You're still on the shift until they approve it.", parse_mode=constants.ParseMode.HTML)
        text = f"🙋 <b>Shift drop request</b>\n\n<b>{esc(me)}</b> can't work <b>{esc(mine)}</b>\n<i>{esc(reason)}</i>"
    await send_for_approval(context.bot, req_id, text)
    return ConversationHandler.END


async def send_for_approval(bot, req_id: int, text: str) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"dq:a:{req_id}"),
        InlineKeyboardButton("🚫 Decline", callback_data=f"dq:d:{req_id}")]])
    sent = await notify_admins(bot, text, kb)
    run("UPDATE drop_requests SET notified=? WHERE id=?", (json.dumps(sent), req_id))


def exchange_text(req) -> str:
    a = display_name_of(req["agent_id"], "them")
    b = display_name_of(req["target_id"], "them")
    back = int(setting(f"exchange_back:{req['id']}", "0") or 0)
    return (f"🔁 <b>Shift exchange request</b>\n\n<b>{esc(a)}</b> ⇄ <b>{esc(b)}</b>\n"
            f"{esc(a)} gives <b>{esc(shift_name(req['slot_id']))}</b>\n"
            f"{esc(a)} takes <b>{esc(shift_name(back))}</b>\n"
            + (f"<i>{esc(req['reason'])}</i>" if req["reason"] else ""))


async def on_exchange_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """xa:y|n:<id> — the other person agrees to (or turns down) an exchange."""
    query = update.callback_query
    _, ans, raw = query.data.split(":")
    req = q1("SELECT * FROM drop_requests WHERE id=?", (int(raw),))
    if not req or req["target_id"] != query.from_user.id:
        await query.answer("This isn't yours to answer.", show_alert=True)
        return
    if req["status"] != "awaiting":
        await query.answer("Already answered.")
        return
    await query.answer()
    who = display_name_of(query.from_user.id, "They")
    if ans == "n":
        run("UPDATE drop_requests SET status='declined', decided_by=?, decided_at=? WHERE id=?",
            (query.from_user.id, now().isoformat(), req["id"]))
        await query.edit_message_text(query.message.text_html + "\n\n❌ You said no.",
                                      parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(req["agent_id"],
                                           f"❌ {who} said no to exchanging {shift_name(req['slot_id'])}. "
                                           "You're still on it.")
        except Exception:
            pass
        return
    run("UPDATE drop_requests SET status='pending' WHERE id=?", (req["id"],))
    await query.edit_message_text(query.message.text_html + "\n\n✅ You agreed. Waiting on a manager.",
                                  parse_mode=constants.ParseMode.HTML)
    try:
        await context.bot.send_message(req["agent_id"], f"✅ {who} agreed. It's with your manager now.")
    except Exception:
        pass
    await send_for_approval(context.bot, req["id"], exchange_text(req))


async def decide_exchange(query, context, req, approve: bool) -> None:
    """A manager's answer to an exchange: both shifts move together, or nothing does."""
    back = int(setting(f"exchange_back:{req['id']}", "0") or 0)
    a, b = req["agent_id"], req["target_id"]
    a_nm, b_nm = display_name_of(a, "them"), display_name_of(b, "them")
    give, take = shift_name(req["slot_id"]), shift_name(back)
    moved = False
    async with write_lock:
        still = (q1("SELECT 1 FROM signups WHERE slot_id=? AND user_id=?", (req["slot_id"], a))
                 and q1("SELECT 1 FROM signups WHERE slot_id=? AND user_id=?", (back, b)))
        if approve and not still:
            run("UPDATE drop_requests SET status='declined', decided_by=?, decided_at=? WHERE id=?",
                (query.from_user.id, now().isoformat(), req["id"]))
        else:
            run("UPDATE drop_requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
                ("approved" if approve else "declined", query.from_user.id, now().isoformat(), req["id"]))
        if approve and still:
            ts = now().isoformat()
            run("DELETE FROM signups WHERE slot_id=? AND user_id=?", (req["slot_id"], a))
            run("DELETE FROM signups WHERE slot_id=? AND user_id=?", (back, b))
            run("INSERT OR IGNORE INTO signups (slot_id, user_id, name, ts) VALUES (?,?,?,?)",
                (req["slot_id"], b, b_nm, ts))
            run("INSERT OR IGNORE INTO signups (slot_id, user_id, name, ts) VALUES (?,?,?,?)",
                (back, a, a_nm, ts))
            moved = True
    if approve and not moved:
        await query.answer("One of those shifts has changed, so nothing moved.", show_alert=True)
        verdict = "not done: one of the shifts changed since it was asked"
    else:
        await query.answer("Approved ✅" if approve else "Declined 🚫")
        verdict = "approved ✅" if approve else "declined 🚫"
    settled = (f"🔁 <b>Shift exchange request</b>\n<b>{esc(a_nm)}</b> ⇄ <b>{esc(b_nm)}</b>\n"
               f"{esc(give)} ⇄ {esc(take)}\n\n{verdict} by {esc(display_name_of(query.from_user.id, 'admin'))}")
    try:
        sent = json.loads(req["notified"] or "[]")
    except (json.JSONDecodeError, TypeError):
        sent = []
    if sent:
        await settle_admin_messages(context.bot, sent, settled)
    else:
        try:
            await query.edit_message_text(settled, parse_mode=constants.ParseMode.HTML)
        except BadRequest:
            pass
    try:
        if moved:
            await context.bot.send_message(a, f"✅ Exchange approved.\nYou're now on {take} and off {give}.\n\nCheck /myshifts.")
            await context.bot.send_message(b, f"✅ Exchange approved.\nYou're now on {give} and off {take}.\n\nCheck /myshifts.")
        else:
            why = "one of the shifts changed" if approve else "your manager declined it"
            for who in (a, b):
                await context.bot.send_message(who, f"The exchange of {give} and {take} didn't go ahead: {why}. "
                                                    "You both keep your own shifts.")
    except Exception:
        pass
    if moved:
        for sid in (req["slot_id"], back):
            s = q1("SELECT d.id AS day_id, d.week_id FROM slots s JOIN days d ON d.id = s.day_id WHERE s.id=?", (sid,))
            if s:
                await refresh_group(context, s["week_id"], s["day_id"])
