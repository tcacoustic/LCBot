"""Access requests, roles and the roster.

Part of the LC avails bot. Shared helpers live in core.py.
"""

from __future__ import annotations

from core import *  # noqa: F401,F403

async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Setup helper — reports the IDs you need for the env file."""
    if not is_owner(update.effective_user.id):
        return
    chat = update.effective_chat
    user = update.effective_user
    lines = [
        "<b>IDs for your config</b>",
        "",
        f"This chat ({chat.type}):",
        f"<code>{chat.id}</code>",
        "",
        f"You ({user.full_name}):",
        f"<code>{user.id}</code>",
    ]
    thread = getattr(update.message, "message_thread_id", None)
    if chat.type in (constants.ChatType.GROUP, constants.ChatType.SUPERGROUP):
        lines += ["", f"→ <code>GROUP_CHAT_ID={chat.id}</code>"]
        if thread:
            lines += [
                f"→ <code>GROUP_THREAD_ID={thread}</code>",
                "<i>(this topic — set that to post here)</i>",
            ]
        else:
            lines.append("<i>Not in a topic — posts go to General.</i>")
        if GROUP_CHAT_ID and chat.id != GROUP_CHAT_ID:
            lines.append("⚠️ This differs from the GROUP_CHAT_ID currently configured.")
        elif not GROUP_CHAT_ID:
            lines.append("Set that in your env and restart the bot.")
    else:
        lines += ["", f"→ <code>ADMIN_IDS={user.id}</code>", "",
                  "<i>Run this inside your livechat group to get GROUP_CHAT_ID.</i>"]
    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return ConversationHandler.END

    status = agent_status(user.id)

    # From 📝 Note to sender under a special request in an Ops chat.
    if context.args and context.args[0].startswith("srn_"):
        from ops import start_ops_note
        return await start_ops_note(update, context, context.args[0][4:])
    # From ✏️ Other reason under Can't do: the reason is typed here.
    if context.args and context.args[0].startswith("srx_"):
        from ops import start_cant_reason
        return await start_cant_reason(update, context, context.args[0][4:])

    # From 📝 Note under a case in a store chat: take the note here, privately.
    if context.args and context.args[0].startswith("note_") and (
            status == "active" or is_owner(user.id)):
        raw = context.args[0][5:]
        r = q1("SELECT username, closed FROM ho_cases WHERE id=?",
               (int(raw),)) if raw.isdigit() else None
        if r and not r["closed"]:
            context.user_data["case_note"] = ("n", int(raw))
            await update.message.reply_text(
                f"📝 <b>Note on {esc(r['username'])}</b>\n\nWhat's the latest? "
                "(e.g. refund done, waiting on courier)\n\n<i>/cancel to stop</i>",
                parse_mode=constants.ParseMode.HTML)
            return CASE_NOTE
        await update.message.reply_text("That case is closed or gone.")
        return ConversationHandler.END

    if status == "active" and not is_livechat(user.id):
        touch_agent(user, dm_ok=1)
        await update.message.reply_text(
            team_help(user.id), parse_mode=constants.ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    if status == "active":
        touch_agent(user, dm_ok=1)
        await update.message.reply_text(
            livechat_welcome(update.effective_user.id),
            reply_markup=agent_keyboard(update.effective_user.id),
            parse_mode=constants.ParseMode.HTML,
        )
        return ConversationHandler.END

    if status == "pending":
        await update.message.reply_text(
            "Your request is still waiting for approval. "
            "I'll message you the moment it's approved 👍"
        )
        return ConversationHandler.END

    if status == "declined":
        await update.message.reply_text(
            "Your access request wasn't approved. "
            "Please speak to your manager if you think that's a mistake."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Hello! Before I can let you in, what's your full name?\n\n"
        "<i>Use the name your manager knows you by — it's what shows on the "
        "schedule, so everyone can tell who's on which shift.</i>",
        parse_mode=constants.ParseMode.HTML,
    )
    return ASK_NAME


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    given = " ".join(update.message.text.split()).strip()

    if len(given) < 2 or len(given) > 40:
        await update.message.reply_text(
            "That doesn't look like a name — try again with 2 to 40 characters."
        )
        return ASK_NAME

    async with write_lock:
        run(
            "INSERT OR REPLACE INTO agents "
            "(user_id, name, username, dm_ok, active, role, status, "
            " display_name, requested_at) "
            "VALUES (?,?,?,1,1,'agent','pending',?,?)",
            (user.id, user.full_name, user.username, given, now().isoformat()),
        )

    await update.message.reply_text(
        f"Thanks {esc(given)}! I've sent your request to the team admins.\n\n"
        "You'll get a message here once you're approved.",
        parse_mode=constants.ParseMode.HTML,
    )

    handle = f"@{user.username}" if user.username else "no username"
    mismatch = ""
    if given.lower() != (user.full_name or "").lower():
        mismatch = f"\nTelegram name: <i>{esc(user.full_name)}</i>"
    text = (
        "🔔 <b>Access request</b>\n\n"
        f"Says they are: <b>{esc(given)}</b>{mismatch}\n"
        f"{handle} · <code>{user.id}</code>\n\n"
        "Approve only if you recognise this person.\n"
        f"<i>Wrong name? Fix it with /rename {user.id} Correct Name</i>"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"ap:{user.id}"),
                InlineKeyboardButton("🚫 Decline", callback_data=f"dn:{user.id}"),
            ],
        ]
    )
    sent = await notify_admins(context.bot, text, kb)
    run("UPDATE agents SET req_msgs=? WHERE user_id=?",
        (json.dumps(sent), user.id))
    if not sent:
        log.warning("Access request from %s but no admin could be reached.", user.id)
    return ConversationHandler.END


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the name that appears on the schedule."""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /rename @handle Jia En\n"
            "or:    /rename 123456789 Jia En"
        )
        return

    target, new_name = context.args[0], " ".join(context.args[1:]).strip()
    row = None
    if target.startswith("@"):
        row = q1("SELECT * FROM agents WHERE lower(username)=lower(?)", (target[1:],))
    elif target.isdigit():
        row = q1("SELECT * FROM agents WHERE user_id=?", (int(target),))
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return

    async with write_lock:
        run(
            "UPDATE agents SET display_name=? WHERE user_id=?",
            (new_name, row["user_id"]),
        )
        # keep the schedule in step with the new name
        run("UPDATE signups SET name=? WHERE user_id=?", (new_name, row["user_id"]))

    await update.message.reply_text(
        f"Renamed to <b>{esc(new_name)}</b> on the schedule.",
        parse_mode=constants.ParseMode.HTML,
    )
    w = open_week() or latest_week()
    if w:
        await refresh_group(context, w["id"])


async def on_access_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    kind, raw = query.data.split(":", 1)
    target_id = int(raw)
    decider = query.from_user

    if not is_admin(decider.id):
        await query.answer("Only admins can approve access.", show_alert=True)
        return

    row = q1("SELECT * FROM agents WHERE user_id=?", (target_id,))
    if not row:
        await query.answer("That request no longer exists.", show_alert=True)
        return
    if row["status"] != "pending":
        await query.answer(
            f"Already handled — {esc(row['name'])} is {row['status']}.", show_alert=True
        )
        return

    approve = kind in ("ap", "ao")
    new_status = "active" if approve else "declined"
    async with write_lock:
        run(
            "UPDATE agents SET status=?, decided_by=?, decided_at=? WHERE user_id=?",
            (new_status, decider.id, now().isoformat(), target_id),
        )
        if kind == "ao":                # an older request's "Approve as Online"
            set_depts(target_id, ["ON"])

    shown = row["display_name"] or row["name"]
    verdict = "approved ✅" if approve else "declined 🚫"
    await query.answer(f"{shown} {verdict}")
    settled = (
        f"🔔 <b>Access request</b>\n\n<b>{esc(shown)}</b>\n\n"
        f"{verdict} by {esc(decider.full_name)}"
    )
    try:
        sent = json.loads(row["req_msgs"] or "[]")
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

    if approve:
        await context.bot.send_message(
            decider.id, dept_question(shown), parse_mode=constants.ParseMode.HTML,
            reply_markup=dept_picker(target_id))
        return
    try:
        await context.bot.send_message(
            target_id,
            "Your access request wasn't approved. "
            "Please speak to your manager if you think that's a mistake.",
        )
    except Exception:
        pass


LIVECHAT_WELCOME = ("👋 <b>You're in!</b>\n\n"
    "/plan — pick your slots for the week\n"
    "/clockin — start your shift\n"
    "/clockout — end your shift\n"
    "/myshifts — what you're signed up for\n"
    "/mytime — your hours this month\n"
    "/help — this list again\n\n"
    "<i>Slots are one person, first come first served.\n"
    "The group schedule updates itself — no need to post avails there.</i>"
)


def dept_question(shown: str) -> str:
    return (f"<b>Which team is {esc(shown)} in?</b>\n"
            "Tick one or more, then Done.")


def dept_picker(user_id: int) -> InlineKeyboardMarkup:
    have = {r["dept"] for r in q("SELECT dept FROM memberships WHERE user_id=?", (user_id,))}
    rows = [[InlineKeyboardButton(f"{'✅' if key in have else '⬜️'} {name}",
                                  callback_data=f"dp:{user_id}:{key}")]
            for key, name in DEPARTMENTS]
    rows.append([InlineKeyboardButton("Done", callback_data=f"dp:{user_id}:done")])
    return InlineKeyboardMarkup(rows)


def livechat_welcome(user_id: int) -> str:
    """The Livechat welcome, plus their other teams' tools if they have any."""
    more = team_help(user_id, extra=True)
    return LIVECHAT_WELCOME + ("\n\n" + more if more else "")


async def welcome_to_team(bot, user_id: int) -> None:
    """Tell someone what they can now use, with the right buttons."""
    await refresh_menu_for(bot, user_id)
    try:
        if is_livechat(user_id):
            await bot.send_message(user_id, livechat_welcome(user_id),
                                   parse_mode=constants.ParseMode.HTML,
                                   reply_markup=agent_keyboard(user_id))
        else:
            await bot.send_message(user_id, team_help(user_id),
                                   parse_mode=constants.ParseMode.HTML,
                                   reply_markup=ReplyKeyboardRemove())
    except Exception:
        pass


async def on_dept_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """dp:<id>:<DEPT> ticks a team · dp:<id>:done saves and welcomes them."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Only admins can change this.", show_alert=True)
        return
    _, raw, choice = query.data.split(":", 2)
    uid = int(raw)
    row = q1("SELECT * FROM agents WHERE user_id=?", (uid,))
    if not row:
        await query.answer("That person isn't on the list any more.", show_alert=True)
        return
    shown = row["display_name"] or row["name"]
    have = [r["dept"] for r in q("SELECT dept FROM memberships WHERE user_id=?", (uid,))]

    if choice == "done":
        if not have:
            await query.answer("Tick at least one team first.", show_alert=True)
            return
        await query.answer()
        names = ", ".join(DEPT_NAMES[d] for d, _ in DEPARTMENTS if d in have)
        await query.edit_message_text(f"✅ <b>{esc(shown)}</b> is in: {esc(names)}",
                                      parse_mode=constants.ParseMode.HTML)
        await welcome_to_team(context.bot, uid)
        if "ON" in have:
            await context.bot.send_message(
                query.from_user.id,
                f"<b>Which channels does {esc(shown)} look after?</b>\n"
                "Tick all that apply, then Done.",
                parse_mode=constants.ParseMode.HTML, reply_markup=channel_picker(uid))
        if "ONSITE" in have:
            from onsite import ask_country
            await ask_country(context.bot, query.from_user.id, uid, shown)
        if "FS" in have:
            from onsite import ask_store
            await ask_store(context.bot, uid, uid)
            await context.bot.send_message(
                query.from_user.id, f"I've asked {shown} to pick their store. "
                "To set it for them: /specialists @username")
        return

    if choice not in DEPT_NAMES:
        await query.answer()
        return
    want = [d for d in have if d != choice] if choice in have else have + [choice]
    async with write_lock:
        set_depts(uid, want)
        if "LIVECHAT" in want and "LIVECHAT" not in have:
            run("UPDATE agents SET on_avails=1, tag_calls=1 WHERE user_id=?", (uid,))
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=dept_picker(uid))
    except BadRequest:
        pass


async def cmd_departments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/departments          — how many people are in each team
       /departments @name    — change someone's teams"""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not is_admin(update.effective_user.id):
        return
    target = " ".join(context.args).strip()
    if not target:
        lines = ["<b>Teams</b>", ""]
        for key, name in DEPARTMENTS:
            n = q1("SELECT COUNT(*) c FROM memberships m JOIN agents a ON a.user_id=m.user_id "
                   "WHERE m.dept=? AND a.status='active'", (key,))["c"]
            lines.append(f"• {esc(name)}: {n}")
        lines += ["", "Change someone's team: <code>/departments @username</code>",
                  "Everyone, by team: /roster"]
        await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)
        return
    row = find_agent(target)
    if not row:
        await update.message.reply_text("I can't find that person. Try their @username.")
        return
    await update.message.reply_text(
        dept_question(row["display_name"] or row["name"]),
        parse_mode=constants.ParseMode.HTML, reply_markup=dept_picker(row["user_id"]))


def viewas_buttons() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(name, callback_data=f"va:{key}")]
            for key, name in DEPARTMENTS]
    rows.append([InlineKeyboardButton("Back to my own view", callback_data="va:off")])
    return InlineKeyboardMarkup(rows)


async def apply_viewas(bot, uid: int, dept: str) -> str:
    async with write_lock:
        set_setting(f"view_as:{uid}", dept)
    await refresh_menu_for(bot, uid)
    if not dept:
        return "👑 Back to your own view, with all your admin tools."
    return (f"👀 <b>Viewing as {esc(DEPT_NAMES[dept])}</b>: menus, /help and the App "
            "now show what that team sees, and your admin tools are paused.\n"
            "/viewas to switch back.")


async def cmd_viewas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/viewas on | ON | Livechat | off — the owner sees the bot as a team."""
    uid = update.effective_user.id
    if not is_owner(uid) or update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    arg = re.sub(r"[^a-z]", "", " ".join(context.args).lower())
    pick = {"off": "", "me": "", "livechat": "LIVECHAT", "lc": "LIVECHAT", "on": "ON",
            "online": "ON", "onsite": "ONSITE", "sh": "SH", "fs": "FS", "ops": "OPS"}
    if arg not in pick:
        now_view = viewing_as(uid)
        await update.message.reply_text(
            ("You're viewing as <b>" + esc(DEPT_NAMES[now_view]) + "</b>. " if now_view
             else "") + "<b>See the bot as which team?</b>",
            parse_mode=constants.ParseMode.HTML, reply_markup=viewas_buttons())
        return
    text = await apply_viewas(context.bot, uid, pick[arg])
    await update.message.reply_text(
        text, parse_mode=constants.ParseMode.HTML,
        reply_markup=agent_keyboard(uid) if is_livechat(uid) else ReplyKeyboardRemove())


async def on_viewas_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    if not is_owner(uid):
        await query.answer()
        return
    choice = query.data.split(":", 1)[1]
    text = await apply_viewas(context.bot, uid, "" if choice == "off" else choice)
    await query.answer()
    await query.edit_message_text(text, parse_mode=constants.ParseMode.HTML)
    await context.bot.send_message(
        uid, "Buttons updated.",
        reply_markup=agent_keyboard(uid) if is_livechat(uid) else ReplyKeyboardRemove())


def channel_picker(user_id: int) -> InlineKeyboardMarkup:
    have = set(online_channels(user_id))
    rows = [
        [InlineKeyboardButton(f"{'✅' if key in have else '⬜️'} {flag} {name}",
                              callback_data=f"tc:{user_id}:{key}")]
        for key, flag, name in CHANNELS
    ]
    rows.append([InlineKeyboardButton("Done", callback_data=f"tc:{user_id}:done")])
    rows.append([InlineKeyboardButton("Remove from the Online team",
                                      callback_data=f"tc:{user_id}:remove")])
    return InlineKeyboardMarkup(rows)


def channels_text(user_id: int) -> str:
    have = online_channels(user_id)
    return ", ".join(CHANNEL_NAMES[k] for k, _, _ in CHANNELS if k in have) or "none yet"


async def on_channel_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admins tick the channels an Online team member looks after."""
    query = update.callback_query
    if not admin_for(query.from_user.id, "ON"):
        await query.answer("Only admins can change this.", show_alert=True)
        return
    _, raw, choice = query.data.split(":", 2)
    uid = int(raw)
    row = q1("SELECT * FROM agents WHERE user_id=?", (uid,))
    if not row:
        await query.answer("That person isn't on the list any more.", show_alert=True)
        return
    shown = row["display_name"] or row["name"]

    if choice == "make":
        async with write_lock:
            run("INSERT OR IGNORE INTO memberships (user_id, dept, created_at) "
                "VALUES (?, 'ON', ?)", (uid, now().isoformat()))
            run("UPDATE agents SET channels=COALESCE(channels, '[]') WHERE user_id=?", (uid,))
        await welcome_to_team(context.bot, uid)
        await query.answer(f"{shown} is now in the Online team")
        await query.edit_message_text(
            f"<b>Which channels does {esc(shown)} look after?</b>\n"
            "Tick all that apply, then Done.",
            parse_mode=constants.ParseMode.HTML, reply_markup=channel_picker(uid))
        return
    if choice == "remove":
        async with write_lock:
            run("DELETE FROM memberships WHERE user_id=? AND dept='ON'", (uid,))
            run("UPDATE agents SET channels=NULL WHERE user_id=?", (uid,))
        await welcome_to_team(context.bot, uid)
        await query.answer()
        await query.edit_message_text(
            f"{shown} is no longer in the ON team. Check their other teams with "
            f"/departments.")
        return
    if choice == "done":
        await query.answer()
        await query.edit_message_text(
            f"✅ <b>{esc(shown)}</b> looks after: {esc(channels_text(uid))}",
            parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(
                uid, f"You're set up for: {channels_text(uid)}.\n\n"
                     "The App shows those first.")
        except Exception:
            pass
        return

    have = online_channels(uid)
    have = [k for k in have if k != choice] if choice in have else have + [choice]
    async with write_lock:
        run("UPDATE agents SET channels=? WHERE user_id=?", (json.dumps(have), uid))
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=channel_picker(uid))
    except BadRequest:
        pass


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/channels         — the Online team and what they look after
       /channels @mei    — change Mei's channels, or add her to the team"""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not admin_for(update.effective_user.id, "ON"):
        return
    target = " ".join(context.args).strip()
    if not target:
        team = q("SELECT * FROM agents WHERE status='active' AND user_id IN "
                 "(SELECT user_id FROM memberships WHERE dept='ON') "
                 "ORDER BY name")
        lines = ["<b>Online team</b>", ""]
        lines += [f"• {esc(r['display_name'] or r['name'])}: "
                  f"{esc(channels_text(r['user_id']))}" for r in team] or ["Nobody yet."]
        lines += ["", "Change someone's channels: <code>/channels @username</code>",
                  "New people: tap <b>Approve</b>, then tick ON."]
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML)
        return

    row = find_agent(target)
    if not row:
        await update.message.reply_text("I can't find that person. Try their @username.")
        return
    shown = row["display_name"] or row["name"]
    if not is_online(row["user_id"]):
        await update.message.reply_text(
            f"{shown} isn't in the Online team. Move them over? They'll only "
            "see handover cases, and come off the avails and shift calls.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🛒 Move to the Online team", callback_data=f"tc:{row['user_id']}:make")]]),
        )
        return
    await update.message.reply_text(
        f"<b>Which channels does {esc(shown)} look after?</b>\n"
        "Tick all that apply, then Done.",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=channel_picker(row["user_id"]),
    )


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Who approved or declined whom, and when."""
    if not is_admin(update.effective_user.id):
        return

    if context.args:
        row = find_agent(context.args[0])
        if not row:
            await update.message.reply_text("No one matches that. Check /roster.")
            return
        nm = row["display_name"] or row["name"]
        lines = [f"<b>{esc(nm)}</b>", ""]
        lines.append(f"Status: <b>{row['status']}</b>")
        if row["requested_at"]:
            lines.append(
                "Requested: "
                + datetime.fromisoformat(row["requested_at"]).strftime("%-d %b, %H:%M")
            )
        if row["decided_by"]:
            who = display_name_of(row["decided_by"], str(row["decided_by"]))
            when = (
                datetime.fromisoformat(row["decided_at"]).strftime("%-d %b, %H:%M")
                if row["decided_at"] else "unknown time"
            )
            verb = "Approved" if row["status"] == "active" else "Declined"
            lines.append(f"{verb} by <b>{esc(who)}</b> on {when}")
        else:
            lines.append("<i>No decision recorded — joined before approvals existed.</i>")
        await reply_long(update.message, 
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    rows = q(
        """SELECT * FROM agents WHERE decided_at IS NOT NULL
           ORDER BY decided_at DESC LIMIT 25"""
    )
    pending = q1("SELECT COUNT(*) c FROM agents WHERE status='pending'")["c"]
    lines = ["<b>Access decisions</b>", ""]
    if not rows:
        lines.append("Nothing recorded yet.")
    for r in rows:
        mark = "✅" if r["status"] == "active" else "🚫"
        who = (
            display_name_of(r["decided_by"], str(r["decided_by"]))
            if r["decided_by"] else "?"
        )
        when = (
            datetime.fromisoformat(r["decided_at"]).strftime("%-d %b %H:%M")
            if r["decided_at"] else ""
        )
        lines.append(
            f"{mark} {esc(r['display_name'] or r['name'])} — "
            f"by {esc(who)}, {when}"
        )
    declined = q("SELECT * FROM agents WHERE status='declined'")
    if declined:
        lines += [
            "",
            f"<i>{len(declined)} declined. To let one in, they send /start again "
            "after you clear them with /removeagent.</i>",
        ]
    if pending:
        lines += ["", f"⏳ {pending} waiting now — see /pending"]
    lines.append("\n<code>/access @handle</code> for one person's history.")
    await reply_long(update.message, 
        "\n".join(lines), parse_mode=constants.ParseMode.HTML
    )


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    rows = q(
        "SELECT * FROM agents WHERE status='pending' ORDER BY requested_at"
    )
    if not rows:
        await update.message.reply_text("No one is waiting for approval.")
        return
    await update.message.reply_text(f"{len(rows)} waiting for approval:")
    for r in rows:
        handle = f"@{r['username']}" if r["username"] else "no username"
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"ap:{r['user_id']}"),
                    InlineKeyboardButton("🚫 Decline", callback_data=f"dn:{r['user_id']}"),
                ]
            ]
        )
        await reply_long(update.message, 
            f"<b>{esc(r['display_name'] or r['name'])}</b>\n"
            f"{handle} · <code>{r['user_id']}</code>",
            reply_markup=kb,
            parse_mode=constants.ParseMode.HTML,
        )


async def cmd_roster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    if not is_admin(update.effective_user.id):
        return
    rows = q("SELECT * FROM agents WHERE status='active' AND user_id IN (SELECT user_id FROM memberships WHERE dept='LIVECHAT') ORDER BY name")
    waiting = q1("SELECT COUNT(*) c FROM agents WHERE status='pending'")["c"]
    if not rows:
        await reply_long(update.message, 
            "Roster is empty. Agents join it by tapping a slot, or by sending me /start."
        )
        return
    def line(r):
        role = role_of(r["user_id"])
        badge = {"owner": "👑", "admin": "🛠"}.get(role, "")
        bits = f"{'🔔' if r['dm_ok'] else '🔕'}{badge} "
        bits += esc(r["display_name"] or r["name"])
        if r["username"]:
            bits += f" @{r['username']}"
        if not r["on_avails"]:
            bits += " · not on avails"
        if r["salaried"]:
            bits += " · salaried"
        bits += f" · <code>{r['user_id']}</code>"
        return bits

    owners = [r for r in rows if role_of(r["user_id"]) == "owner"]
    admins = [r for r in rows if role_of(r["user_id"]) == "admin"]
    agents = [r for r in rows if role_of(r["user_id"]) == "agent"]

    lines = [f"<b>Roster ({len(rows)})</b>"]
    if owners:
        lines += ["", "<b>👑 Owners</b>"] + [line(r) for r in owners]
    if admins:
        lines += ["", "<b>🛠 Admins</b>"] + [line(r) for r in admins]
    if agents:
        lines += ["", f"<b>Agents ({len(agents)})</b>"] + [line(r) for r in agents]

    # The other teams have access too, but aren't on shifts: listed apart.
    for key, name in DEPARTMENTS:
        if key == "LIVECHAT":
            continue
        team = q("SELECT a.* FROM agents a JOIN memberships m ON m.user_id=a.user_id "
                 "WHERE m.dept=? AND a.status='active' ORDER BY a.name", (key,))
        if not team:
            continue
        lines += ["", f"<b>{esc(name)} ({len(team)})</b>"] + [
            f"{esc(r['display_name'] or r['name'])}"
            + (f" @{r['username']}" if r["username"] else "")
            + (f" — {esc(channels_text(r['user_id']))}" if key == "ON" else "")
            for r in team
        ]
    lines.append("<i>Change someone's team with /departments @handle.</i>")

    # Owners set in config may have never messaged the bot.
    missing = [
        uid for uid in ADMIN_IDS
        if not any(r["user_id"] == uid for r in rows)
    ]
    if missing:
        lines += ["", "<b>👑 Owners (config only)</b>"] + [
            f"<code>{uid}</code> — hasn't messaged the bot" for uid in missing
        ]

    lines.append("\n🔕 = hasn't sent me /start, so can't get shift reminders.")
    lines.append(
        "<code>/makeadmin @handle</code> · <code>/removeadmin @handle</code> "
        "· <code>/removeagent @handle</code>"
    )
    if waiting:
        lines.append(f"\n⏳ {waiting} waiting for approval — see /pending")
    await reply_long(update.message, "\n".join(lines), parse_mode=constants.ParseMode.HTML)


async def _set_role(update, context, new_role: str) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Only an owner can change roles.")
        return
    if not context.args:
        verb = "makeadmin" if new_role == "admin" else "removeadmin"
        await update.message.reply_text(f"Usage: /{verb} @handle")
        return
    target = " ".join(context.args).strip()
    row = None
    if target.startswith("@"):
        row = q1("SELECT * FROM agents WHERE lower(username)=lower(?)", (target[1:],))
    elif target.isdigit():
        row = q1("SELECT * FROM agents WHERE user_id=?", (int(target),))
    if not row:
        row = q1("SELECT * FROM agents WHERE lower(name)=lower(?)", (target,))
    if not row:
        await update.message.reply_text(
            "No one on the roster matches that. Check /roster."
        )
        return
    if is_owner(row["user_id"]):
        await update.message.reply_text(
            "That person is an owner — set in config, so I can't change it here."
        )
        return

    run("UPDATE agents SET role=? WHERE user_id=?", (new_role, row["user_id"]))
    await refresh_menu_for(context.bot, row["user_id"])

    if new_role == "admin":
        await update.message.reply_text(f"{esc(row['name'])} is now an admin.")
        try:
            await context.bot.send_message(
                row["user_id"],
                "You've been given admin rights. Send /help to see what's new.",
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(f"{esc(row['name'])} is now a regular agent.")


async def cmd_makeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_role(update, context, "admin")


async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_role(update, context, "agent")


async def cmd_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Who gets @-mentioned in the nightly on-duty post.

    They still appear on the rota either way — this only stops the ping.
    """
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        on = q("SELECT * FROM agents WHERE status='active' AND user_id IN (SELECT user_id FROM memberships WHERE dept='LIVECHAT') AND tag_calls=1 ORDER BY name")
        off = q("SELECT * FROM agents WHERE status='active' AND user_id IN (SELECT user_id FROM memberships WHERE dept='LIVECHAT') AND tag_calls=0 ORDER BY name")
        lines = [f"<b>Tagged in the nightly post ({len(on)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in on] or ["  nobody"]
        lines += ["", f"<b>Not tagged ({len(off)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in off] or ["  nobody"]
        lines += [
            "",
            "<code>/tag off @handle</code> — stop pinging them",
            "<code>/tag on @handle</code> — start again",
            "",
            "<i>They still show on the rota either way.</i>",
        ]
        await update.message.reply_text(
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    mode = context.args[0].lower()
    if mode not in ("on", "off") or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/tag off @handle</code> or <code>/tag on @handle</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    row = find_agent(context.args[1])
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return

    want = 1 if mode == "on" else 0
    async with write_lock:
        run("UPDATE agents SET tag_calls=? WHERE user_id=?", (want, row["user_id"]))
    nm = row["display_name"] or row["name"]
    if want:
        msg = f"<b>{esc(nm)}</b> will be tagged in the nightly on-duty post again."
    else:
        msg = (f"<b>{esc(nm)}</b> won't be tagged in the nightly post.\n"
               "They'll still appear on the rota.")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)


async def cmd_salaried(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark someone as salaried, so their hours aren't paid hourly."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        sal = q("SELECT * FROM agents WHERE status='active' AND user_id IN (SELECT user_id FROM memberships WHERE dept='LIVECHAT') AND salaried=1 ORDER BY name")
        hourly = q("SELECT * FROM agents WHERE status='active' AND user_id IN (SELECT user_id FROM memberships WHERE dept='LIVECHAT') AND salaried=0 ORDER BY name")
        lines = [f"<b>Salaried — no hourly pay ({len(sal)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in sal] or ["  nobody"]
        lines += ["", f"<b>Paid hourly ({len(hourly)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in hourly] or ["  nobody"]
        lines += [
            "",
            "<code>/salaried on @handle</code> — stop hourly pay",
            "<code>/salaried off @handle</code> — pay them hourly again",
            "",
            "<i>Salaried agents still appear on the board, still clock in "
            "and out, and their hours are still recorded.</i>",
        ]
        await reply_long(update.message, 
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    mode = context.args[0].lower()
    if mode not in ("on", "off") or len(context.args) < 2:
        await reply_long(update.message, 
            "Usage: <code>/salaried on @handle</code> or "
            "<code>/salaried off @handle</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    row = find_agent(context.args[1])
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return

    want = 1 if mode == "on" else 0
    async with write_lock:
        run("UPDATE agents SET salaried=? WHERE user_id=?", (want, row["user_id"]))
    nm = row["display_name"] or row["name"]
    if want:
        msg = (f"<b>{esc(nm)}</b> is salaried — livechat hours won't be paid "
               "hourly.\nThey still show on the board and still clock in and out.")
    else:
        msg = (f"<b>{esc(nm)}</b> is back on hourly pay at "
               f"{money(rate_for(row['user_id'], now().date()))}/hour.")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)


async def cmd_avails(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Take someone off the weekly avails roster without removing their access.

    Full-timers and anyone who doesn't pick slots shouldn't be tagged on the
    board or chased about confirming.
    """
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        on = q("SELECT * FROM agents WHERE status='active' AND user_id IN (SELECT user_id FROM memberships WHERE dept='LIVECHAT') AND on_avails=1 ORDER BY name")
        off = q("SELECT * FROM agents WHERE status='active' AND user_id IN (SELECT user_id FROM memberships WHERE dept='LIVECHAT') AND on_avails=0 ORDER BY name")
        lines = [f"<b>On the avails roster ({len(on)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in on] or ["  nobody"]
        lines += ["", f"<b>Not tagged or chased ({len(off)})</b>"]
        lines += [f"  {esc(a['display_name'] or a['name'])}" for a in off] or ["  nobody"]
        lines += [
            "",
            "<code>/avails off @handle</code> — stop tagging them",
            "<code>/avails on @handle</code> — put them back",
        ]
        await reply_long(update.message, 
            "\n".join(lines), parse_mode=constants.ParseMode.HTML
        )
        return

    mode = context.args[0].lower()
    if mode not in ("on", "off") or len(context.args) < 2:
        await reply_long(update.message, 
            "Usage: <code>/avails off @handle</code> or <code>/avails on @handle</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    row = find_agent(context.args[1])
    if not row:
        await update.message.reply_text("No one matches that. Check /roster.")
        return

    want = 1 if mode == "on" else 0
    async with write_lock:
        run("UPDATE agents SET on_avails=? WHERE user_id=?", (want, row["user_id"]))
    nm = row["display_name"] or row["name"]
    if want:
        msg = (f"<b>{esc(nm)}</b> is back on the avails roster — "
               "they'll be tagged and chased again.")
    else:
        msg = (f"<b>{esc(nm)}</b> is off the avails roster.\n"
               "They won't be tagged on the board or chased about confirming, "
               "but they keep full access and can still pick slots.")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

    w = open_week() or latest_week()
    if w:
        await refresh_group(context, w["id"])


async def cmd_removeagent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Take someone off the roster and free any slots they were holding."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/removeagent @handle</code>, or their numeric ID from /roster.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    target = " ".join(context.args).strip()
    row = None
    if target.startswith("@"):
        row = q1(
            "SELECT * FROM agents WHERE lower(username)=lower(?)", (target[1:],)
        )
    elif target.isdigit():
        row = q1("SELECT * FROM agents WHERE user_id=?", (int(target),))
    if not row:
        row = q1("SELECT * FROM agents WHERE lower(name)=lower(?)", (target,))
    if not row:
        await update.message.reply_text(
            f"No one on the roster matches {target!r}. Check /roster for exact handles and IDs."
        )
        return

    async with write_lock:
        run(
            "UPDATE agents SET active=0, status='declined' WHERE user_id=?",
            (row["user_id"],),
        )
        freed = 0
        w = open_week() or latest_week()
        if w:
            cur = run(
                """DELETE FROM signups WHERE user_id=? AND slot_id IN
                   (SELECT s.id FROM slots s JOIN days d ON d.id=s.day_id
                    WHERE d.week_id=?)""",
                (row["user_id"], w["id"]),
            )
            freed = cur.rowcount
            run(
                "DELETE FROM confirmations WHERE week_id=? AND user_id=?",
                (w["id"], row["user_id"]),
            )

    msg = f"Removed <b>{esc(row['name'])}</b> from the roster."
    if freed:
        msg += f"\nFreed {freed} slot(s) in the current week — they're open again."
    else:
        msg += "\nThey weren't holding any slots this week."
    msg += "\n\n<i>Past weeks keep their history.</i>"
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

    if w:
        await refresh_group(context, w["id"])


# --------------------------------------------------------------------------
# Feedback: from the App's Feedback button or /feedback, to the owner by DM
# --------------------------------------------------------------------------

FEEDBACK_TEXT = 430   # conversation state: waiting for the message
FEEDBACK_KINDS = {"PROBLEM": "🐞 Problem", "IDEA": "💡 Idea", "QUESTION": "❓ Question"}


def feedback_head(user_id: int, kind: str = "", where: str = "") -> str:
    row = q1("SELECT username FROM agents WHERE user_id=?", (user_id,))
    handle = f" @{row['username']}" if row and row["username"] else ""
    teams = [DEPT_NAMES[k] for k, _ in DEPARTMENTS if k in depts_of(user_id)]
    bits = [b for b in (FEEDBACK_KINDS.get(kind, ""), f"from the App: {where}" if where else "") if b]
    return (f"💬 <b>Feedback from {esc(display_name_of(user_id, 'someone'))}</b>{esc(handle)}"
            f"{' · ' + esc(' + '.join(teams)) if teams else ''}"
            + (f"\n{esc(' · '.join(bits))}" if bits else ""))


async def send_feedback(bot, user_id: int, text: str = "", kind: str = "", where: str = "",
                        photo_from: tuple | None = None) -> int:
    """Send it to the owner(s); returns how many got it. photo_from = (chat, message)
    for a screenshot sent to the bot, copied across as it is."""
    head = feedback_head(user_id, kind, where)
    sent = 0
    for owner in ADMIN_IDS:
        try:
            await bot.send_message(owner, head + (f"\n\n{esc(text)}" if text else ""),
                                   parse_mode=constants.ParseMode.HTML)
            if photo_from:
                await bot.copy_message(owner, photo_from[0], photo_from[1])
            sent += 1
        except Exception as e:
            log.info("Couldn't pass feedback to %s: %s", owner, e)
    return sent


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/feedback — a problem, idea or question, straight to the owner."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Message me directly with /feedback.")
        return ConversationHandler.END
    uid = update.effective_user.id
    if agent_status(uid) != "active" and not is_owner(uid):
        await update.message.reply_text("Send /start first, then try /feedback again.")
        return ConversationHandler.END
    await update.message.reply_text(
        "💬 <b>Feedback</b>\n\nWhat's up? A problem, an idea or a question: type it, or send "
        "a screenshot with a caption. It goes straight to the bot's owner.\n"
        "<i>Please blur any customer details. /cancel to stop.</i>",
        parse_mode=constants.ParseMode.HTML)
    return FEEDBACK_TEXT


async def on_feedback_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    photo = (msg.chat_id, msg.message_id) if msg.photo or msg.document else None
    text = (msg.text or "").strip() if not photo else ""
    if not text and not photo:
        await msg.reply_text("Type your feedback, or send a screenshot. /cancel to stop.")
        return FEEDBACK_TEXT
    sent = await send_feedback(context.bot, update.effective_user.id, text[:3000], photo_from=photo)
    await msg.reply_text("✅ Thanks! It's been passed on." if sent else
                         "Sorry, I couldn't pass that on just now. Please try again later.")
    return ConversationHandler.END
