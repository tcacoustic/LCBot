# Moving SH Bot from Railway to Supabase

SH Bot's data moves from one SQLite file on Railway into the `sh_bot` schema on TC's data
estate (the `tc-data-warehouse` Supabase project). The schema lives in that repo; this repo holds
the load script. The rules are in `docs/guide/moving-an-existing-app.md` there.

## Who does what

| step | who |
|---|---|
| schema migrations (`sh_bot` tables, access rules) | PR in tcacoustic/tc-data-warehouse, merged by Wilson |
| export the SQLite file from the live bot | you: `/backup` in a DM with the bot |
| rehearse the load | you, on your PR's preview branch |
| the production load | Wilson, with this script, at cutover |

You never hold production credentials. No real data goes into git: exports and the script's
output stay in `supabase-export/`, which is gitignored.

## Running a rehearsal

```bash
pip install -r scripts/supabase/requirements.txt
export DATABASE_URL='<preview branch connection string from Wilson>'
python scripts/supabase/load.py --sqlite supabase-export/avails.db --dry-run
```

The script reads `DATABASE_URL` from your shell only, so don't save it in a file in this repo.
`--dry-run` loads everything in one transaction and rolls it back. Leave it off to keep the
rows on the preview branch.

It writes two files to `supabase-export/`:

- `report.md` has per-table counts (exported = loaded + skipped, with a reason for each skip) and
  the unmatched count. Share this with Wilson.
- `unmatched.csv` lists the Telegram users who aren't on `identity.people`. It holds names, so
  send it to HR only and delete it after cutover.

Re-run the script until the unmatched list is empty. Each run updates the same rows, because
every id is derived from the SQLite table and id.

## What the load changes

- **People.** A Telegram user id becomes a `person_id`, matched by `identity.people.telegram_id`.
  Names and usernames aren't copied over. Approved agents become `members`, and pending or
  declined ones become `join_requests`.
- **Renames.**

  | SQLite | Supabase |
  |---|---|
  | `agents` | `members` |
  | `memberships` | `member_teams` |
  | `agents.channels` | `member_channels` |
  | `drop_requests` | `shift_requests` |
  | `ho_cases` | `handover_cases` |
  | `case_log` | `case_events` |
  | `appointment_log` | `appointment_events` |

  Every `*_id` or `*_by` that held a Telegram id is now `*_person_id`. Dates are `<x>_on`,
  times are `<x>_at` (timestamptz), and 0/1 flags are booleans.
- **Held back.** On-Site `appointments` hold customer names, phones, emails and addresses. They
  are skipped until the data security decision; `--include-appointments` loads them once
  that's approved.

## Not done yet

- **The bot's code still uses SQLite.** Before cutover, the `q`/`q1`/`run` helpers and every
  query need porting to Postgres and the new names. The bot also needs to connect as `sh_bot_app`
  with its role token (ADR-0011). That's a separate piece of work.
- **Cutover** follows the guide: freeze writes, take a final `/backup`, Wilson loads it,
  switch the bot over, and keep Railway read-only for about two weeks.
