"""Entry point: starts the bot, schedules jobs, wires the commands up."""

from __future__ import annotations

from core import *  # noqa: F401,F403
from board import *  # noqa: F401,F403
from people import *  # noqa: F401,F403
from shifts import *  # noqa: F401,F403
from timeclock import *  # noqa: F401,F403
from handover import *  # noqa: F401,F403
from onsite import *  # noqa: F401,F403
from ops import *  # noqa: F401,F403
from webapp import *  # noqa: F401,F403
from telegram.ext import AIORateLimiter

async def on_keyboard_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a tap on the persistent keyboard to the right command."""
    text = (update.message.text or "").strip()
    context.args = []
    if text == BTN_IN:
        await cmd_clockin(update, context)
    elif text == BTN_OUT:
        await cmd_clockout(update, context)
    elif text == BTN_HOURS:
        await cmd_mytime(update, context)
    elif text == BTN_SHIFTS:
        await cmd_myshifts(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def cmd_shiftcall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post the on-duty tags now, or preview them without posting."""
    if not is_admin(update.effective_user.id):
        return

    args = [a.lower() for a in context.args]
    preview = "preview" in args or "test" in args
    target = None
    for a in context.args:
        if a.lower() in ("preview", "test"):
            continue
        try:
            target = date.fromisoformat(a)
        except ValueError:
            await update.message.reply_text(
                "Usage:\n"
                "<code>/shiftcall preview</code> — show it here, post nothing\n"
                "<code>/shiftcall</code> — post it to the group\n"
                "<code>/shiftcall 2026-09-08</code> — a specific day",
                parse_mode=constants.ParseMode.HTML,
            )
            return

    text, reason = build_shift_call(target)
    if not text:
        await update.message.reply_text(f"Nothing to post — {reason}")
        return

    if preview:
        # Mentions are shown as plain text so nobody is notified.
        shown = re.sub(r'<a href="tg://user\?id=\d+">([^<]*)</a>', r"\1", text)
        shown = shown.replace("@", "＠")
        await update.message.reply_text(
            "<b>Preview — nothing has been posted</b>\n"
            "<i>Handles shown as plain text so nobody is pinged.</i>\n\n"
            + shown + "\n\n<i>Send /shiftcall to post it for real.</i>",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    await send_group(
        context.bot, text, thread=SHIFTCALL_THREAD_ID,
        chat_id=SHIFTCALL_CHAT_ID,
        parse_mode=constants.ParseMode.HTML,
    )
    await update.message.reply_text("Posted to the group ✅")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        return
    w = latest_week()
    if not w:
        await update.message.reply_text("No week has been posted yet.")
        return
    b = data.board(w["id"])
    rows = [{"day": d["name"], "the_date": d["the_date"], "label": sl["label"], "agent": su["name"]}
            for d in b["days"] for sl in b["slots"][d["id"]]
            for su in (b["signups"][sl["id"]] or [{"name": None}])]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["day", "date", "slot", "agent"])
    for r in rows:
        writer.writerow([r["day"], r["the_date"], r["label"], r["agent"] or ""])
    out = io.BytesIO(buf.getvalue().encode())
    out.name = f"{w['label'].replace(' ', '_')}_avails.csv"
    await update.message.reply_document(out, filename=out.name)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The same commands as the / menu, with the App pointed to first."""
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        await update.message.reply_text("Use /schedule here, or message me directly.")
        return
    user = update.effective_user
    await reply_long(
        update.message, help_text(user.id), parse_mode=constants.ParseMode.HTML,
        reply_markup=agent_keyboard(user.id) if is_livechat(user.id) else ReplyKeyboardRemove(),
    )


async def job_shift_call(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Evening-before group post tagging tomorrow's agents."""
    text, reason = build_shift_call()
    if not text:
        log.info("Shift call skipped: %s", reason)
        return
    try:
        await send_group(
            context.bot, text, thread=SHIFTCALL_THREAD_ID,
            chat_id=SHIFTCALL_CHAT_ID,
            parse_mode=constants.ParseMode.HTML,
        )
    except Exception as e:
        log.warning("Shift call failed: %s", e)


def build_shift_call(for_date: date | None = None) -> tuple[str | None, str]:
    """Returns (message, reason). Message is None when there's nothing to send."""
    target = for_date or (now() + timedelta(days=1)).date()
    if not SHIFTCALL_CHAT_ID:
        return None, "No chat is set for the shift call."
    day = day_row_for(target.isoformat())
    if not day:
        return None, f"No posted week covers {target.isoformat()}."

    lines = [f"\U0001F4E2 <b>On duty tomorrow \u2014 {day['name']} {fmt_day(target)}</b>", ""]
    working: dict[int, str] = {}
    gaps = 0
    for s_ in data.board(day["week_id"])["slots"][day["id"]]:
        holders = slot_holders(s_["id"])
        if holders:
            lines.append(f"{s_['label']}: " + esc(", ".join(h["name"] for h in holders)))
            for h in holders:
                working[h["user_id"]] = h["name"]
        else:
            lines.append(f"{s_['label']}: \u2014")
            gaps += 1

    if not working:
        return None, f"Nobody has claimed any slot on {day['name']} {fmt_day(target)}."
    if gaps:
        lines.append(f"\n\u26A0\uFE0F {gaps} slot(s) with nobody on")
    taggable = []
    for uid, nm in working.items():
        row = data.agent(uid)
        if row is None or row["tag_calls"]:
            taggable.append(mention(uid, nm))
    if taggable:
        lines.append("\n" + " ".join(taggable))
    return "\n".join(lines), "ok"


async def post_init(app: Application) -> None:
    """Re-arm jobs after a restart."""
    seed_presets()
    start_web_server(app)
    if PUBLIC_URL:
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="App", web_app=WebAppInfo(url=app_url())
                )
            )
            log.info("Mini App button set to %s", app_url())
        except Exception as e:
            log.info("Couldn't set the menu button: %s", e)
    await publish_command_menus(app)
    w = open_week()
    if w:
        schedule_week_jobs(app, w["id"])
        log.info("Re-armed jobs for %s", w["label"])
    mins = parse_time_token(SHIFT_CALL_TIME)
    app.job_queue.run_daily(
        job_shift_call,
        time(mins // 60, mins % 60, tzinfo=TZ),
        name="shift-call",
    )
    log.info(
        "Group shift call scheduled for %s daily, to chat %s topic %s",
        SHIFT_CALL_TIME, SHIFTCALL_CHAT_ID, SHIFTCALL_THREAD_ID or "(none)",
    )
    app.job_queue.run_repeating(
        job_auto_close, interval=timedelta(minutes=30),
        first=timedelta(minutes=5), name="auto-close",
    )
    omins = parse_time_token(OT_ASK_TIME or "10:00")
    app.job_queue.run_daily(
        job_ask_overtime, time(omins // 60, omins % 60, tzinfo=TZ),
        name="ask-overtime",
    )
    dmins = parse_time_token(os.environ.get("DIGEST_TIME", "").strip() or "09:00")
    app.job_queue.run_daily(
        job_week_digest, time(dmins // 60, dmins % 60, tzinfo=TZ),
        name="week-digest",
    )
    cmins = parse_time_token(REQUEST_CARRY_TIME)
    app.job_queue.run_daily(
        job_carry_over, time(cmins // 60, cmins % 60, tzinfo=TZ), name="ops-carry-over",
    )
    app.job_queue.run_repeating(
        job_sheet_retry, interval=timedelta(minutes=15), first=timedelta(minutes=2),
        name="ops-sheet-retry",
    )
    # Ops rounds: every half hour, a minute past :00 and :30.
    t = now()
    first = t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0) + timedelta(minutes=31)
    app.job_queue.run_repeating(
        job_ops_rounds, interval=timedelta(minutes=30), first=first, name="ops-rounds",
    )
    app.job_queue.run_repeating(
        job_visit_reminders, interval=timedelta(minutes=5), first=timedelta(minutes=1),
        name="visit-reminders",
    )
    kmins = parse_time_token(BUMP_TIME)
    app.job_queue.run_daily(
        job_case_bump, time(kmins // 60, kmins % 60, tzinfo=TZ), name="case-bump",
    )
    hmins = parse_time_token(HANDOVER_NUDGE_TIME)
    app.job_queue.run_daily(
        job_handover_nudge, time(hmins // 60, hmins % 60, tzinfo=TZ), name="handover-nudge",
    )
    rmins = parse_time_token(os.environ.get("REPORT_TIME", "").strip() or "09:00")
    app.job_queue.run_daily(
        job_case_report, time(rmins // 60, rmins % 60, tzinfo=TZ),
        name="case-report",
    )
    if DM_REMINDERS:
        app.job_queue.run_daily(
            job_shift_reminders,
            time(SHIFT_REMINDER_HOUR, 0, tzinfo=TZ),
            name="shift-reminders",
        )


# --------------------------------------------------------------------------


def main() -> None:
    # The rate limiter queues messages instead of losing them when Telegram's
    # limits are hit (about 20 a minute into one group), e.g. on 11.11.
    app = (Application.builder().token(BOT_TOKEN).post_init(post_init)
           .rate_limiter(AIORateLimiter(max_retries=3)).build())

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("newweek", newweek)],
            states={
                ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_date)],
                ASK_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_label)],
                ASK_EVENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_events)],
                ASK_SLOTS: [
                    CallbackQueryHandler(on_week_shape, pattern=r"^ws:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_slots),
                ],
                ASK_SHAPE_LABEL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_shape_label)
                ],
                ASK_DAYS: [CallbackQueryHandler(on_week_days, pattern=r"^wd:")],
                ASK_CAPS: [CallbackQueryHandler(on_week_caps, pattern=r"^cap:")],
                ASK_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_deadline)],
                CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", cmd_start)],
            states={
                ASK_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)
                ],
                CASE_NOTE: [   # /start note_<id> from a store chat's 📝 Note
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_case_note_text)
                ],
                SR_NOTE: [     # /start srn_<id> from 📝 Note to sender in an Ops chat
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_ops_note_text)
                ],
                SR_REASON: [   # /start srx_<id> from ✏️ Other reason under Can't do
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_cant_reason_text)
                ],
                ONS_TEXT: [    # /start oqn_<id> from 📝 Note under an On-Site request
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_ons_text)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("feedback", cmd_feedback)],
            states={FEEDBACK_TEXT: [MessageHandler(
                (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.IMAGE,
                on_feedback_message)]},
            fallbacks=[CommandHandler("cancel", cancel)],
            conversation_timeout=1800,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND
            & filters.Regex(f"^({BTN_IN}|{BTN_OUT}|{BTN_HOURS}|{BTN_SHIFTS})$"),
            on_keyboard_button,
        )
    )
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("access", cmd_access))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("shiftcall", cmd_shiftcall))
    app.add_handler(CommandHandler("myshifts", cmd_myshifts))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("gaps", cmd_gaps))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("closeweek", cmd_close))
    app.add_handler(CommandHandler("roster", cmd_roster))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("clockin", cmd_clockin))
    # /changeshift: drop, give or exchange. /dropshift and /swap open it too.
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler(["changeshift", "dropshift", "swap"], cmd_changeshift)],
            states={
                CG_PICK: [CallbackQueryHandler(on_cg_button, pattern=r"^cg:")],
                CG_WHAT: [CallbackQueryHandler(on_cg_button, pattern=r"^cg:")],
                CG_WHO: [CallbackQueryHandler(on_cg_button, pattern=r"^cg:")],
                CG_THEIRS: [CallbackQueryHandler(on_cg_button, pattern=r"^cg:")],
                CG_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_cg_reason),
                            CallbackQueryHandler(on_cg_button, pattern=r"^cg:")],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True,
            conversation_timeout=1800,
        )
    )
    app.add_handler(CallbackQueryHandler(on_exchange_answer, pattern=r"^xa:[yn]:[0-9a-f-]{36}$"))
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("pickup", cmd_pickup)],
            states={
                PICKUP_PICK: [CallbackQueryHandler(on_pickup_pick, pattern=r"^pu:")],
                PICKUP_REASON: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_pickup_reason)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True,
            conversation_timeout=1800,
        )
    )
    app.add_handler(CommandHandler("dropreqs", cmd_dropreqs))
    app.add_handler(CallbackQueryHandler(on_drop_decision, pattern=r"^dq:[ad]:[0-9a-f-]{36}$"))
    app.add_handler(CommandHandler("clockout", cmd_clockout))
    app.add_handler(CommandHandler("mytime", cmd_mytime))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("payslip", cmd_payslip))
    app.add_handler(CommandHandler("setrate", cmd_setrate))
    app.add_handler(CommandHandler("timesheet", cmd_timesheet))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("addtime", cmd_addtime))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("payroll", cmd_payroll))
    app.add_handler(CallbackQueryHandler(on_ot_yes, pattern=r"^ot:yes:"))
    app.add_handler(CallbackQueryHandler(on_ot_decision, pattern=r"^oc:"))
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(on_ot_no, pattern=r"^ot:no:")],
            states={
                OT_WHEN: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_ot_time)
                ]
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True,
            conversation_timeout=3600,
        )
    )
    app.add_handler(CommandHandler("openshifts", cmd_openshifts))
    app.add_handler(CommandHandler("clockoutfor", cmd_clockoutfor))
    app.add_handler(CommandHandler("fixtime", cmd_fixtime))
    app.add_handler(CommandHandler("removeagent", cmd_removeagent))
    app.add_handler(CommandHandler("avails", cmd_avails))
    app.add_handler(CommandHandler("salaried", cmd_salaried))
    app.add_handler(CommandHandler("tag", cmd_tag))
    app.add_handler(CommandHandler("fixed", cmd_fixed))
    app.add_handler(CommandHandler("capacity", cmd_capacity))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("addslot", cmd_addslot))
    app.add_handler(CommandHandler("delslot", cmd_delslot))
    app.add_handler(CommandHandler("dropslot", cmd_dropslot))
    app.add_handler(CommandHandler("whohas", cmd_whohas))
    app.add_handler(CommandHandler("applyfixed", cmd_applyfixed))
    app.add_handler(CommandHandler("makeadmin", cmd_makeadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("presets", cmd_presets))
    app.add_handler(CommandHandler("savepreset", cmd_savepreset))
    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^t:[0-9a-f-]{36}$"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^[cu]:[0-9a-f-]{36}$"))
    app.add_handler(CallbackQueryHandler(on_plan_toggle, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(on_plan_nav, pattern=r"^pd:"))
    app.add_handler(CallbackQueryHandler(on_clockin_slot, pattern=r"^ci:[0-9a-f]{8}:\d+$"))
    app.add_handler(CallbackQueryHandler(on_access_decision, pattern=r"^(ap|dn|ao):\d+$"))
    app.add_handler(CallbackQueryHandler(on_channel_pick, pattern=r"^tc:\d+:"))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("departments", cmd_departments))
    app.add_handler(CallbackQueryHandler(on_dept_pick, pattern=r"^dp:\d+:"))
    app.add_handler(CommandHandler("viewas", cmd_viewas))
    app.add_handler(CommandHandler("specialists", cmd_specialists))
    app.add_handler(CallbackQueryHandler(on_country_pick, pattern=r"^vz:cy:\d+:[A-Z]{2}$"))
    app.add_handler(CommandHandler("store", cmd_store))
    app.add_handler(CallbackQueryHandler(on_store_pick, pattern=r"^vz:(fc|fs):\d+:\w+$"))
    app.add_handler(CallbackQueryHandler(on_reschedule_answer, pattern=r"^vz:r[ad]:[0-9a-f-]{36}$"))
    app.add_handler(CallbackQueryHandler(on_take_button, pattern=r"^vz:tk:[0-9a-f-]{36}$"))
    app.add_handler(CommandHandler("visits", cmd_visits))
    app.add_handler(CommandHandler("visitchat", cmd_visitchat))
    # Ops special requests (sr: buttons)
    text_in = filters.TEXT & ~filters.COMMAND
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("request", cmd_request)],
            states={
                R_ORDER: [MessageHandler(text_in, on_request_order)],
                R_COUNTRY: [CallbackQueryHandler(on_request_country, pattern=r"^sr:y:[A-Z]{2}$")],
                R_CAT: [CallbackQueryHandler(on_request_category, pattern=r"^sr:c:[A-Z]+$")],
                R_DETAILS: [MessageHandler(text_in, on_request_details),
                            CallbackQueryHandler(on_request_option, pattern=r"^sr:q:(\d|none)$")],
                R_DATE: [CallbackQueryHandler(on_request_date, pattern=r"^sr:d:")],
                R_DATE_TYPED: [MessageHandler(text_in, on_request_date_typed)],
                R_TIME: [CallbackQueryHandler(on_request_time, pattern=r"^sr:t:[A-Z]+$")],
                R_TIME_TYPED: [MessageHandler(text_in, on_request_time_typed)],
                R_CONFIRM: [CallbackQueryHandler(on_request_confirm, pattern=r"^sr:(send|stop|flag)$")],
            },
            fallbacks=[CommandHandler("cancel", cancel),
                       CallbackQueryHandler(on_request_back, pattern=r"^sr:back$")],
            allow_reentry=True,
            conversation_timeout=1800,
        )
    )
    app.add_handler(CallbackQueryHandler(on_ops_button, pattern=r"^sr:(tk|ok|no|bk):[0-9a-f-]{36}$|^sr:nr:[0-9a-f-]{36}:[A-Z]+$"))
    app.add_handler(CommandHandler("opschat", cmd_opschat))
    app.add_handler(CommandHandler("opsrounds", cmd_opsrounds))
    app.add_handler(CommandHandler("requests", cmd_requests))
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(on_amount_start, pattern=r"^vz:am:[0-9a-f-]{36}$")],
            states={VISIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_amount_text)]},
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True,
            conversation_timeout=1800,
        )
    )
    app.add_handler(CallbackQueryHandler(
        on_visit_button, pattern=r"^vz:(lst|v|ar|lv|oc|ok|nc|pm|fd)(:|$)"))
    # On-Site typed steps: a note on a visit that didn't finish (and later more).
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(on_nf_note_start, pattern=r"^vn:n:[0-9a-f-]{36}$"),
                          CallbackQueryHandler(on_nf_time_start, pattern=r"^vn:x:[0-9a-f-]{36}$"),
                          CallbackQueryHandler(on_rq_reason_start, pattern=r"^oq:r:[0-9a-f-]{36}$"),
                          CallbackQueryHandler(on_rq_extra_start, pattern=r"^oq:[mc]:[0-9a-f-]{36}$"),
                          CallbackQueryHandler(on_cab_trip, pattern=r"^cb:p:[0-9a-f-]{36}:[A-Z]+$")],
            states={ONS_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ons_text)]},
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True,
            conversation_timeout=1800,
        )
    )
    app.add_handler(CallbackQueryHandler(on_nf_button, pattern=r"^vn:[rudtlbk]:[0-9a-f-]{36}(:\w+)?$"))
    app.add_handler(CallbackQueryHandler(on_rq_form, pattern=r"^oq:[npsk]:[0-9a-f-]{36}(:\w+)*$"))
    app.add_handler(CallbackQueryHandler(on_rq_button, pattern=r"^oq:[td]:[0-9a-f-]{36}$"))
    app.add_handler(CallbackQueryHandler(on_paynow_button, pattern=r"^vq:[pb]:[0-9a-f-]{36}$"))
    app.add_handler(CommandHandler("requestchat", cmd_requestchat))
    app.add_handler(CommandHandler("financechat", cmd_financechat))
    app.add_handler(CommandHandler("paynowqr", cmd_paynowqr))
    app.add_handler(CallbackQueryHandler(on_finance_button, pattern=r"^fl:[td]:[0-9a-f-]{36}$"))
    app.add_handler(CallbackQueryHandler(on_cab_form, pattern=r"^cb:n:[0-9a-f-]{36}$"))
    app.add_handler(CallbackQueryHandler(on_cab_decision, pattern=r"^cb:[arbx]:[0-9a-f-]{36}(:[A-Z]+)?$"))
    app.add_handler(CallbackQueryHandler(on_viewas_pick, pattern=r"^va:"))
    app.add_handler(CallbackQueryHandler(on_plan_quick, pattern=r"^pq:"))
    app.add_handler(CallbackQueryHandler(on_handover_none, pattern=r"^ho:none$"))
    app.add_handler(CallbackQueryHandler(on_handover_carry, pattern=r"^ho:carry$"))
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(on_handover_add, pattern=r"^ho:add$"),
                CallbackQueryHandler(on_request_start, pattern=r"^cs:new$"),
            ],
            states={
                HO_SECTION: [CallbackQueryHandler(on_ho_section, pattern=r"^hs:")],
                HO_PRIO: [CallbackQueryHandler(on_ho_prio, pattern=r"^hp:")],
                HO_PLATFORM: [CallbackQueryHandler(on_ho_platform, pattern=r"^hf:")],
                HO_STORE: [CallbackQueryHandler(on_ho_store, pattern=r"^hb:")],
                HO_BODY: [
                    CommandHandler("back", on_ho_back_cmd),
                    CommandHandler("skip", on_ho_skip),
                    CallbackQueryHandler(on_ho_platform, pattern=r"^hf:"),
                    CallbackQueryHandler(on_ho_store, pattern=r"^hb:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_ho_body),
                ],
                HO_MORE: [CallbackQueryHandler(on_ho_more, pattern=r"^hm:")],
                HO_PICK: [CallbackQueryHandler(on_handover_pick, pattern=r"^hk:")],
            },
            fallbacks=[
                CommandHandler("cancel", cancel),
                CommandHandler("handover", cmd_handover),
            ],
            allow_reentry=True,
            conversation_timeout=1800,
        )
    )
    app.add_handler(CommandHandler("handover", cmd_handover))
    app.add_handler(CommandHandler("handovers", cmd_handovers))
    app.add_handler(CommandHandler(["onchat", "linkchat"], cmd_linkchat))
    app.add_handler(CommandHandler("bumpnow", cmd_bumpnow))
    app.add_handler(CommandHandler("postupdate", cmd_postupdate))   # /linkchat: the old name
    app.add_handler(CallbackQueryHandler(on_store_close, pattern=r"^sc:x:[0-9a-f-]{36}$"))
    app.add_handler(CallbackQueryHandler(on_store_reopen, pattern=r"^sc:o:[0-9a-f-]{36}$"))
    app.add_handler(CallbackQueryHandler(on_store_pick, pattern=r"^sc:[hn]:[0-9a-f-]{36}$"))
    app.add_handler(CommandHandler("cases", cmd_cases))
    app.add_handler(CommandHandler("pmalerts", cmd_pmalerts))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(on_case_note_start, pattern=r"^cs:[nh]:[0-9a-f-]{36}$"),
                CallbackQueryHandler(on_case_edit_start, pattern=r"^cs:ef:[0-9a-f-]{36}:\w+$"),
            ],
            states={
                CASE_NOTE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_case_note_text),
                    CallbackQueryHandler(on_handback_choice, pattern=r"^cs:(hy|hw|v):[0-9a-f-]{36}$"),
                ],
                CASE_EDIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_case_edit_text)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True,
            conversation_timeout=1800,
        )
    )
    app.add_handler(CallbackQueryHandler(on_cases_button,
                                         pattern=r"^cs:(l|v|ro|x|s|f|e|ep)(:|$)"))
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("addreview", cmd_addreview)],
            states={
                REVIEW_PHOTO: [
                    MessageHandler(
                        (filters.PHOTO | (filters.TEXT & ~filters.COMMAND)),
                        got_review_photo,
                    )
                ]
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True,
            conversation_timeout=1800,
        )
    )
    app.add_handler(CommandHandler("reviews", cmd_reviews))
    # Last: a photo sent to the bot goes with the visit the specialist is on.
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, on_visit_photo))

    log.info("Avails bot running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
