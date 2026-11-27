# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Always read [HANDOVER.md](HANDOVER.md) first and follow its house rules** (section 9): test bot first, live only when the owner says "ship", made-up data only, no secrets, database changes go to Wilson, and explain things in plain words because the owner isn't technical.

## Which repo to work through

- **Database-related work** (schemas, warehouse tables, data pipelines, queries against company data): go through the separate data warehouse repo, not this one
- **Everything else** (this bot's code, features, fixes): go through https://github.com/tcacoustic/LCBot (this repo's `origin`)

## What this is

A Telegram bot (python-telegram-bot v21–22, with `job-queue`) for the SH livechat team. It collects weekly shift availability on a group "board", and handles shift drop/pickup/swap requests, clock-in/out and pay/overtime, closing handovers, and a Telegram Mini App served over HTTP from the same process. State lives in the TC data warehouse (Supabase): app data in the `lc_bot` schema (`lc_bot_dev` for LCBot Test), people in `identity`, reached through the Data API as the bot's own role.

## Running

```bash
./run-local.sh
```
This exports the env config, creates `venv/` if it's missing, and runs `python bot.py`. In production it runs as `worker: python bot.py` (Procfile, Railway) or via the systemd unit `availsbot.service`, which reads env vars from `bot.env`. There is no linter or build step; the tests are below, under Database. To check syntax, run `python -m py_compile *.py`.

`run-local.sh`, `bot.env` and `*.service` are gitignored on purpose because they hold the bot token and live data. Don't commit them. `bot 2.py` is an untracked, older single-file version of the bot. Ignore it.

## Architecture

- **Module layout uses star imports.** `core.py` holds the config (all env vars are read at import time), the data module (`data`), shared helpers, renderers and the embedded Mini App HTML (`MINIAPP_HTML`). Every feature module starts with `from core import *`. `bot.py` star-imports all of them and does the wiring. So any name defined in `core.py` is available everywhere without an explicit import.
- **Feature modules:** `board.py` (the `/newweek` setup conversation, presets, posting the board, per-week jobs via `schedule_week_jobs`), `people.py` (`/start` access requests, roles, roster), `shifts.py` (`/plan` claiming, drop/pickup/swap, slot admin), `timeclock.py` (clock in/out, timesheets, payroll, OT claims, auto-close), `handover.py` (handover conversation), `webapp.py` (Mini App JSON payloads plus `MiniAppHandler`, a stdlib `ThreadingHTTPServer` on `PORT`, default 8080).
- **Handler registration is all in `bot.py` `main()`.** Adding a command means writing the handler in a feature module and then registering it in `main()`. Inline buttons are routed by short `callback_data` prefixes matched with regex (`ws:`, `ds:`, `dq:a|d:<id>`, `ot:yes:`, `ho:`, `hs:`, …). Check the existing prefixes in `main()` before choosing a new one, so they don't collide. Multi-step flows use `ConversationHandler` with `/cancel` as the fallback.
- **Scheduled jobs:** they live in memory only, so `post_init` in `bot.py` re-arms them on every restart. This covers the shift call, a 30-minute auto-close, the overtime ask and the weekly digest, and it also re-arms the open week's jobs. Times are parsed with `parse_time_token` and use `TZ` (default Asia/Singapore).
- **Database:** every read and write goes through `data` (`data.py`), one module over the warehouse's Data API (`rest.py`, with a one-hour working token from `service_token.py`). Feature code keeps the old table and column names and Telegram ids; `data.py` maps them to the warehouse's tables and person ids. Row ids are uuids, shown as an 8-character ref (`ref()`, `#1a2b3c4d`). Schema changes are migration PRs in the tc-data-warehouse repo, never made here. Tests: `LCBOT_LIVE_DB=1 python -m unittest discover tests` against a local PostgREST (see `tests/README.md`).
- **Mini App auth:** each API request carries Telegram `initData` in the `X-Init-Data` header. `verify_init_data` checks it with an HMAC against `BOT_TOKEN`, then `web_has_access` gates by role. `app_url()` appends a per-boot `BUILD_STAMP` so Telegram's webview cache gets busted on each deploy.
- **Roles:** the owner and admins come from `ADMIN_IDS` (env) plus each member's access level (`agents.role` in the bot's code, `members.access_level` in the warehouse). Use `is_owner` / `is_admin`. Pay is in cents (`money()`), with `PAY_MODE` (`slot` or actual time) and per-agent rates from `rate_for`.
- **Messages** are sent with Telegram HTML parse mode. Escape user text with `esc()`, and use `reply_long` / `split_message` for anything that might exceed Telegram's length limit.

## Key env vars

The required ones are `BOT_TOKEN`, `GROUP_CHAT_ID`, `DB_SCHEMA`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SERVICE_TOKEN_SEED` and `SERVICE_TOKEN_VOLUME_PATH`. Commonly tuned: `ADMIN_IDS`, `TZ_NAME`, `BOARD_MODE` (`single`/`daily`), `GROUP_BUTTONS`, `SHIFT_CALL_TIME`, `DM_REMINDERS`, `PUBLIC_URL` (enables the Mini App menu button), `OPS_CHAT_ID`/`OPS_THREAD_ID`, `SHIFTCALL_CHAT_ID`, `PAY_MODE`, and the `OT_*` thresholds. See the Config section at the top of `core.py` for the full list and defaults.
