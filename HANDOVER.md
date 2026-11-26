# SH Bot: handover guide

Written for Zoe, who takes over the SH Bot from Cayden on Fri 27 Nov 2026. It covers what the bot does, who uses it, how changes go live, and what's still open. There are no passwords or tokens in this file. Those live in Railway → Variables.

**Data lives in the TC data warehouse (Supabase) since Wed 25 Nov 2026.** Wilson moved the live bot off its SQLite file (PR #84, merged 25 Nov 12:09). The live bot uses schema `lc_bot`; the test bot uses `lc_bot_dev`. Wilson (AI lead) owns the warehouse side, including backups and schema changes.

For how the code is laid out, read [CLAUDE.md](CLAUDE.md). For the On-Site App (its code is live but not yet launched to staff), read [docs/ONSITE-LAUNCH.md](docs/ONSITE-LAUNCH.md).

---

## 1. What the SH Bot is

One Telegram bot, plus an App that opens inside Telegram (the Mini App), used by several teams:

| Team | What they use it for |
|---|---|
| **Livechat** | Weekly availability board, shift drops / pickups / swaps, clock in and out, timesheets and pay, overtime claims, end-of-shift handover |
| **SH** (phone team) | Hand over customer cases, book On-Site visits, special requests to Ops |
| **TC Online (ON)** | Cases from SH for their stores (Shopee, Lazada, Webstore in SG / MY / HK / TH), I'll help / Close / Note, "Online request" back to SH, `/cases` list |
| **FS** (Flagship stores) | Hand customers over to On-Site, special requests to Ops |
| **On-Site** | Home visits: take handovers, reminders, Arrived / Leave, payment, reports (the new App in PR #81 adds much more) |
| **Ops** | Special requests (date / address / product changes, stop delivery) with Done / Can't do, linked to the Ops Google Sheet |

Each team gets its own Home screen in the App. Admins get an 🛡 Admin switch (People, Team).

## 2. Accounts and access (transfer these first)

| What | Where | To do at handover |
|---|---|---|
| Live bot and test bot | @BotFather in Telegram | /mybots → bot → Bot Settings → **Transfer Ownership**, for **both** bots |
| Bot owner inside the bot | Railway env var `ADMIN_IDS` (Telegram user IDs) | Add the new owner's ID. Remove the old owner's on their last day |
| Hosting | Railway project "livechat-agent-bot": live service + **LCBot Test** service | Invite the new owner as admin; check billing |
| Code | GitHub [tcacoustic/LCBot](https://github.com/tcacoustic/LCBot) | New owner needs admin access |
| Ops sheet link | Google Apps Script project "Special Req – bot link" (separate from the sheet's own script) + the "Special Req" tab of the 🇸🇬 SG B2C Order Entry Tracker | Share or transfer the script project so it keeps running after the old owner's account closes |
| Data warehouse | `tcacoustic/tc-data-warehouse`, plan #378 (Wilson owns it) | Nothing to transfer. Schema changes go to Wilson as warehouse PRs |
| Telegram chats the bot works in | The groups in section 4 | Make the new owner an admin of each group, so they can add or remove the bot |
| Claude artifacts (ideas page, App vision, tutorial videos, team guide, test checklist) | The previous owner's Claude account | Share each with the new owner before the last day. They stop working when the account closes, so ask IT whether to copy them |
| Claude's notes about this project | Saved only on the previous owner's Mac | The rules that matter are copied into section 9 of this file |

**After the previous owner leaves:** rotate the bot token (BotFather → API Token → Revoke, then update `BOT_TOKEN` in Railway) and `REQUEST_SHEET_SECRET` (Railway + the Apps Script's Script Properties). Rotating is normal practice when someone who knew a secret leaves.

## 3. How changes go live

1. A change is made on a branch from `main` and opened as a PR on GitHub. Code reads and writes data through the `data` module (`data.py`), not SQL.
2. **Test:** merge the PR into the `staging` branch. Railway's **LCBot Test** service deploys it automatically (about 1–2 minutes). Try it on the test bot. The test bot has its **own separate database** (`lc_bot_dev`), so use made-up customers.
3. **Ship:** merge the PR into `main`. Railway deploys the live bot automatically. Ship **after 8pm** when the bot is quiet, then check the deploy succeeded (Railway → Deployments, or the green tick on the GitHub commit).
4. Keep `staging` = `main` + whatever is being tested.

Rules agreed with management:
- **Database changes need the AI lead's review before going live.** New tables and columns are migration PRs in `tc-data-warehouse`, reviewed and merged by Wilson. The bot doesn't create tables itself.
- Made-up data only in tests and screenshots. Never put real customer or staff data in the repo.
- No secrets in code: everything comes from Railway variables.
- The bot never messages customers.

If a deploy breaks the live bot: Railway → Deployments → the previous good deploy → **Redeploy**. Then revert the PR on GitHub.

## 4. Day-to-day admin

- **New people** send `/start` to the bot. The owner approves each one **by hand** (deliberately, for security) and ticks their teams. Everyone must `/start` before they can be added anywhere.
- **Backups:** `/backup` and `/restore` are gone. The warehouse handles backups and recovery (ask Wilson).
- **Teams and stores:** `/departments`, `/channels @name` (which stores a TC Online person looks after), `/store` (FS store), country for On-Site.
- **Admin for:** an admin can be limited to some teams (People → Admin for). The owner has everything.
- **View as a team:** `/viewas` (owner) shows the bot as another team sees it. Admin powers pause while viewing; switch off with `/viewas` again.
- **What's new** card in the App: drafted, approved by the owner (wording and audience), then posted. Keep audiences narrow.
- **Feedback:** the 🔧 Feedback button in the App sends ideas / problems straight to the owners.

### Chats the bot is linked to (live)

| Chat | Command used in it |
|---|---|
| Livechat group | board (set by `GROUP_CHAT_ID`) |
| TC Online handover group | per-shift summary (`OPS_CHAT_ID`) |
| Store chats: Shopee SG / MY, Lazada SG / MY, TH (Lazada + Shopee), Webstore SG + HK, MY, TH | `/onchat <store>` in each |
| Delivery Helpdesk: OPS X ON WE X SH (special-requests topic) | `/opschat` (SG) |
| 🇲🇾 MY SH x Ops, 🇭🇰 HK SH x Ops | `/opschat MY`, `/opschat HK` |
| 🇹🇭 TH chat | `/onchat TH` and `/opschat TH` |

Still to link when the On-Site App launches: `/visitchat`, `/requestchat`, `/financechat`, `/paynowqr` (see the launch checklist).

## 5. Settings (Railway variables, names only)

Required: `BOT_TOKEN`, `GROUP_CHAT_ID`, and the warehouse settings `DB_SCHEMA`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SERVICE_TOKEN_SEED`, `SERVICE_TOKEN_VOLUME_PATH` (Wilson sets these; don't change them without him).

Common: `ADMIN_IDS`, `TZ_NAME` (Asia/Singapore), `PUBLIC_URL` (turns on the App), `OPS_CHAT_ID` / `OPS_THREAD_ID`, `SHIFTCALL_CHAT_ID`, `BOARD_MODE`, `GROUP_BUTTONS`, `DM_REMINDERS`, `PAY_MODE`, `SHIFT_CALL_TIME`, the `OT_*` overtime settings, `REQUEST_SHEET_URL` / `REQUEST_SHEET_SECRET` (Ops sheet), `REQUEST_CUTOFF` / `REQUEST_CARRY_TIME` (Ops 2pm cut-off), `HANDOVER_NUDGE_TIME`, `ONSITE_FORM_URL` (Zoho sign-off form).

With the On-Site App, optional: `PAYNOW_UEN` / `PAYNOW_NAME` (only for a generated QR; not needed if TC's own QR is set with `/paynowqr`), `REVIEW_URL` (review QR).

The full list with defaults is in the Config section at the top of `core.py`. The test bot has its own values (its own token, test sheet copy).

## 6. Open work (as of Wed 25 Nov 2026)

| What | State | Next step |
|---|---|---|
| **Warehouse switch** (PR #84, warehouse #378) | Done 25 Nov 2026 (live on `lc_bot`) | Watch the first days for odd data or errors; report to Wilson |
| **On-Site App** (PR #81, combines #67–#77 and #80) | Code went live with #84 on 25 Nov; not launched to staff | Follow [docs/ONSITE-LAUNCH.md](docs/ONSITE-LAUNCH.md): real checklists, sign-off wording, Finance (QR, cab rules, chat), Ops chat, MY/HK/TH rates, link the chats, dry run, SG first |
| PR #48 (old Pay tab) and #67–#80 (stacked parts) | History, not separate work. #67–#77 and #80 are all inside #81; #48 was rebuilt as #80; #78 is closed (went live as #79) | #81 is merged, so close each with the comment "Included in #81". Closing doesn't delete: they stay readable on GitHub as a record of why each piece was built |
| LCBot #60 (old load script) | Replaced by Wilson's move | Close after checking with Wilson |
| MY / HK / TH sheets for Ops requests | Parked | SG sheet only today |
| RMA app (Service Concierge replacement) | AppSheet trial planned | Waiting on IT |
| Standalone phone app (outside Telegram) | Idea | Installable web app with "Log in with Telegram" first; needs IT / AI lead sign-off |
| App error box says "send it to @caydenseee" (`core.py` `MINIAPP_HTML`) | Points at the previous owner | Change to the new owner's handle, or "use the Feedback button". Normal test → ship |
| `/stats` (owner-only counts) | Idea | Build if wanted |

## 7. Handover checklist

| When | Do |
|---|---|
| Before the last day | New owner added to `ADMIN_IDS` (live + test), Railway, GitHub (admin), each Telegram group (admin), the Apps Script project. Both bots transferred in @BotFather. Artifacts shared. New owner makes one small change test → ship with the old owner watching |
| Last day | Old owner removed from `ADMIN_IDS`, Railway, GitHub admin, Telegram group admin, Apps Script. Old owner's local copies of any data deleted (`*.db`, `bot.env`, exports) |
| Right after | New owner rotates `BOT_TOKEN` (both bots) and `REQUEST_SHEET_SECRET`, and confirms the bot still works. Tell Wilson, who can update the warehouse side if needed |

Check that nobody's personal account is the only way into anything (Railway billing, Google script, bots).

## 8. Troubleshooting

The new owner isn't technical. When they bring a problem, Claude should do the investigating, explain in plain words, and give click-by-click steps for anything they must do in Railway, BotFather or Google. First question: does it affect **one person** (access, teams, their phone) or **everyone** (the bot, Railway, Telegram)?

| Symptom | Check first | Fix | Then |
|---|---|---|---|
| Bot doesn't reply to anyone | status.railway.com, Telegram status; Railway → live service → Deployments: is the newest deploy Active? | Outage: wait. Crashed: Redeploy the last good deploy | Read Railway → Logs (owner pastes them with names removed) |
| Broke right after a ship | The newest deploy is that change | Redeploy the previous deploy, then revert the PR on GitHub | Fix on a branch, test, ship again |
| Logs: `Conflict: terminated by other getUpdates request` | Someone else is polling with the live token | Revoke in @BotFather, new `BOT_TOKEN` in Railway. Bot is offline until the new deploy is Active, so check the deploy queue is moving first | Tell IT and Wilson the same day |
| App error box / App won't load, for everyone | Is the bot replying? `401` = `BOT_TOKEN` changed or `initData` check failing; blank = `PUBLIC_URL` | Close and reopen the App (cache); `BUILD_STAMP` changes each boot | Logs: `... view failed` lines in `webapp.py` name the screen |
| One person: no access / App won't open | `/pending`, App → Admin → People | Approve, tick teams, reopen Telegram | `/access @handle` shows who approved them and when |
| Wrong screens or buttons | Their teams (memberships) | Fix teams in App → Admin → People; menu refreshes (`refresh_menu_for`) | `/viewas` to see what they see |
| Board / shift call / reminders didn't post | `/newweek` done? Bot still in the group? Jobs live in memory and are re-armed in `post_init` on restart | Re-add the bot; restart (redeploy) re-arms jobs | Group upgraded to supergroup = new chat ID: `/chatid` in the group (owner only), update `GROUP_CHAT_ID` |
| Store or Ops chat stopped getting posts | Bot removed from the chat, or chat moved | Re-add, then `/onchat <store>` or `/opschat` (add MY, HK or TH) in that chat | |
| Ops request in Telegram but not in the sheet | Apps Script "Special Req – bot link" → run `checkBotLink` then `checkSetup` | `REQUEST_SHEET_SECRET` must match in Railway and Script Properties; `REQUEST_SHEET_URL` must be the current web app URL | After editing the script: Deploy → **New deployment** (or Edit → New version), else the old code keeps running |
| Hours or pay look wrong | `/timesheet`, `/audit`, App Hours tab | `/clockoutfor @handle 18:00`, `/addtime @handle YYYY-MM-DD HH:MM HH:MM`, `/fixtime <id> HH:MM HH:MM` | Explain the calculation (`PAY_MODE`, `rate_for`, `OT_*`) |
| Data missing or wrong | Everyone, since a time? | Don't hand-edit data | Wilson owns the data |

Golden rules: undo first (Redeploy the last good deploy), investigate after. Never ask for or accept tokens or passwords in chat. Logs and screenshots may contain staff or customer names; ask the owner to remove them first.

## 9. Improving the bot with Claude

Most of this bot was built with Claude Code, working in this repo. The new owner can carry on the same way.

**Set up:** install Claude Code (desktop app or terminal) with a TC account, clone the repo, and open the folder:

```bash
git clone https://github.com/tcacoustic/LCBot.git
```

Then open the `LCBot` folder in Claude Code. Claude reads `CLAUDE.md` automatically. Ask it to read this file too, at the start of a session.

**How to ask:** describe the problem in team words, with a screenshot if you have one (blur customer details). For example:
- "On-Site says the Leave button does nothing on Android. Find out why, fix it on a branch, and put it on the test bot."
- "Mock up a way for SH to see today's On-Site visits. Show me before building."
- "Which PRs are open, and what does each do?"
- "Write a What's new card for TC Online about this change. Don't post it."

**Words that worked:** "test #N" = put PR N on the test bot. "ship #N" = merge to `main` and watch the deploy. Claude should never go live without that word.

**House rules (tell Claude these if it doesn't follow them):**
- Test bot first, live only on "ship". Ship after 8pm. Never stress-test the live bot.
- Made-up data only. Never paste real customer or staff details; if a screenshot has them, Claude should flag it (PDPA).
- No secrets in chat or code. If a token is pasted anywhere, rotate it.
- Every new user is approved by hand. No auto-approve lists.
- Database changes: AI lead / Wilson review first.
- The bot never messages customers. Claude drafts announcements; the owner approves wording and audience and posts them.
- Look and feel: dates as "Fri 16 Oct" (no Today/Tomorrow), a Back button on every step, country flags and platform logos on every screen, App first (the `/` menu shows only chat commands).
- Build what the team asked for, launch it small (SG first), and improve from their feedback.

**Working with a non-technical owner:** explain in plain words, no jargon. Give click-by-click steps for Railway, BotFather, GitHub and Google. Do the git and GitHub work yourself, and say what each step does before it changes anything live.

**Finding new needs:** once a month, the owner can paste in staff Feedback messages (names removed) and ask Claude to group them into themes and quick wins. They can also ask each team lead "what do you still do outside the bot?" and ask which of those could move into the bot. Suggest small steps: one team or SG first, then improve from feedback. Don't add extras nobody asked for.

**What to check yourself:** read the PR description, try it on the test bot, and ask Claude "what could this break?" before shipping. Claude can be confidently wrong; the test bot is where you find out.

## 10. Useful links

- Bot code: https://github.com/tcacoustic/LCBot (open PRs: https://github.com/tcacoustic/LCBot/pulls)
- Data warehouse: https://github.com/tcacoustic/tc-data-warehouse (move plan: https://github.com/tcacoustic/tc-data-warehouse/issues/378)
- LCBot Test Run checklist, On-Site ideas and App vision page, tutorial videos, team guide: Claude artifacts (see section 2).
- Telegram bot docs: https://core.telegram.org/bots
- Plain-language guide for the owner (transfers, everyday jobs, troubleshooting, using Claude): Claude Doc "SH Bot: guide for the new owner" (shared with the owner; ask IT to keep a copy).
- Railway: the "livechat-agent-bot" project (Deployments → Logs for errors).
