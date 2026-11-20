# SH Bot: handover guide

Written for the person taking over the SH Bot. It covers what the bot does, who uses it, how changes go live, and what's still open. There are no passwords or tokens in this file. Those live in Railway → Variables.

For how the code is laid out, read [CLAUDE.md](CLAUDE.md). For the On-Site App that's ready to launch, read [docs/ONSITE-LAUNCH.md](https://github.com/tcacoustic/LCBot/blob/onsite-app/docs/ONSITE-LAUNCH.md) (in PR #81).

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
| Code | GitHub `tcacoustic/LCBot` | New owner needs admin access |
| Ops sheet link | Google Apps Script project "Special Req – bot link" (separate from the sheet's own script) + the "Special Req" tab of the 🇸🇬 SG B2C Order Entry Tracker | Share or transfer the script project so it keeps running after the old owner's account closes |
| Data warehouse move (in progress) | `tcacoustic/tc-data-warehouse` draft PR #63 (from a personal fork) + LCBot #60 | Wilson / the AI lead decide whether to continue |

**After the previous owner leaves:** rotate the bot token (BotFather → API Token → Revoke, then update `BOT_TOKEN` in Railway) and `REQUEST_SHEET_SECRET` (Railway + the Apps Script's Script Properties). Rotating is normal practice when someone who knew a secret leaves.

## 3. How changes go live

1. A change is made on a branch and opened as a PR on GitHub.
2. **Test:** merge the PR into the `staging` branch. Railway's **LCBot Test** service deploys it automatically (about 1–2 minutes). Try it on the test bot. The test bot has its **own empty database**, so use made-up customers.
3. **Ship:** merge the PR into `main`. Railway deploys the live bot automatically. Ship **after 8pm** when the bot is quiet, then check the deploy succeeded (Railway → Deployments, or the green tick on the GitHub commit).
4. Keep `staging` = `main` + whatever is being tested.

Rules agreed with management:
- **Database changes need the AI lead's review before going live.** New tables and columns are added in code on startup (`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN`). There's no separate migration step.
- Made-up data only in tests and screenshots. Never put real customer or staff data in the repo.
- No secrets in code: everything comes from Railway variables.
- The bot never messages customers.

If a deploy breaks the live bot: Railway → Deployments → the previous good deploy → **Redeploy**. Then revert the PR on GitHub.

## 4. Day-to-day admin

- **New people** send `/start` to the bot. The owner approves each one **by hand** (deliberately, for security) and ticks their teams. Everyone must `/start` before they can be added anywhere.
- **Backups:** the bot backs up the database daily. `/backup` (owner) sends a copy on demand and is safe while the bot runs. **Don't use `/restore`** unless you really mean to replace all data.
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

Required: `BOT_TOKEN`, `GROUP_CHAT_ID`.

Common: `ADMIN_IDS`, `TZ_NAME` (Asia/Singapore), `DB_PATH`, `PUBLIC_URL` (turns on the App), `OPS_CHAT_ID` / `OPS_THREAD_ID`, `SHIFTCALL_CHAT_ID`, `BOARD_MODE`, `GROUP_BUTTONS`, `DM_REMINDERS`, `PAY_MODE`, `SHIFT_CALL_TIME`, `BACKUP_TIME`, the `OT_*` overtime settings, `REQUEST_SHEET_URL` / `REQUEST_SHEET_SECRET` (Ops sheet), `REQUEST_CUTOFF` / `REQUEST_CARRY_TIME` (Ops 2pm cut-off), `HANDOVER_NUDGE_TIME`, `ONSITE_FORM_URL` (Zoho sign-off form).

With the On-Site App (PR #81), optional: `PAYNOW_UEN` / `PAYNOW_NAME` (only for a generated QR; not needed if TC's own QR is set with `/paynowqr`), `REVIEW_URL` (review QR).

The full list with defaults is in the Config section at the top of `core.py`. The test bot has its own values (its own token, test sheet copy).

## 6. Open work (as of Fri 20 Nov 2026)

| What | State | Next step |
|---|---|---|
| **On-Site App** (PR #81, combines #67–#77 and #80) | Built, on the test bot | Follow [docs/ONSITE-LAUNCH.md](https://github.com/tcacoustic/LCBot/blob/onsite-app/docs/ONSITE-LAUNCH.md): AI lead DB review, real checklists, sign-off wording, Finance (QR, cab rules, chat), Ops chat, MY/HK/TH rates, dry run, ship, SG first |
| Make Zoe an admin with Admin for = On-Site (live) | Not done | People in the App. Needed for booked FYIs, cab claims, 11am nudge |
| PR #48 (old Pay tab) and #67–#80 (stacked parts) | Replaced by #81 | Close them once #81 is reviewed |
| Supabase move (LCBot #60 + warehouse #63) | Draft | AI lead / Wilson decide; dev bot first, then live |
| MY / HK / TH sheets for Ops requests | Parked | SG sheet only today |
| RMA app (Service Concierge replacement) | AppSheet trial planned | Waiting on IT |
| Standalone phone app (outside Telegram) | Idea | Installable web app with "Log in with Telegram" first; needs IT / AI lead sign-off |
| `/stats` (owner-only counts) | Idea | Build if wanted |

## 7. Useful links

- On-Site ideas and App vision page, the tutorial videos and the team guide are Claude artifacts owned by the previous owner. Ask them to share the links before they leave.
- Telegram bot docs: https://core.telegram.org/bots
- Railway: the "livechat-agent-bot" project (Deployments → Logs for errors).
