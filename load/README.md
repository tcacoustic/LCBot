# Moving LC Bot's SQLite data into the warehouse (#383)

Two scripts, no data. The snapshots, the merge list and the expected-differences list stay on
Wilson's Mac, outside this repo: it is public.

- `lcload.py` loads a SQLite snapshot into `lc_bot` or `lc_bot_dev`. It links people to
  `identity.people` by Telegram id, creates the rest through the schema's `add_person`, sets the
  merge list's nicknames, and writes every table keyed on `legacy_id`, so running it again updates
  rather than duplicates. Without `--apply` it rolls back. It prints counts per table (exported =
  new + changed + same + skipped, with a reason for each skip) and people matched, created and
  skipped. It never prints a name or an id.
- `parity.py` asks the old bot (on a copy of the snapshot) and the new bot (through the Data API,
  as the schema's own role) the same questions for every person, week, slot, month, overtime
  claim, open case and appointment, and fails on any difference not on the expected list. One
  difference is built in, because #378 decided it: a removed member shown as `declined` by the old
  bot is `revoked` now (no access either way).

## Files kept outside git

- **Snapshot**: the bot's SQLite file.
- **Merge list** (`--people`): a CSV with the header `telegram_id,full_name,nickname,country_code`.
  `full_name` and `country_code` are used only for someone being created; `nickname` is set on
  anyone (the approved board names). Leave a cell empty to leave it alone.
- **Expected differences** (`--expected`): one pattern of question keys per line, such as
  `sheet:<telegram id>:*`, with `#` comments. `parity.py --details FILE` writes every difference
  with its key, so you can see what to agree to.

## Setup (once)

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt -r load/requirements.txt
git worktree add ../lcbot-sqlite d925687      # the bot before the move, for the old side of parity
```

## Rehearsal on a local Supabase

1. In tc-data-warehouse, add `"lc_bot", "lc_bot_dev", "identity"` to `[api] schemas` in
   `supabase/config.toml` (local only, don't commit it), then `supabase start`. Local `identity` is
   empty, so every person is "created" here; that's expected. `supabase status -o env` shows the
   values below.
2. Load, twice. The second run must say `Changes: 0`.

   ```bash
   export LOAD_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres
   venv/bin/python load/lcload.py ~/path/to/snapshot.db --schema lc_bot --people ~/path/to/merge.csv --apply
   venv/bin/python load/lcload.py ~/path/to/snapshot.db --schema lc_bot --people ~/path/to/merge.csv --apply
   ```
3. Parity. Use the same `ADMIN_IDS` and `PAY_MODE` the bot runs with.

   ```bash
   export PARITY_URL=http://127.0.0.1:54321 PARITY_APIKEY=<ANON_KEY> PARITY_JWT_SECRET=<JWT_SECRET>
   ADMIN_IDS=... venv/bin/python load/parity.py ~/path/to/snapshot.db --schema lc_bot \
       --old-code ../lcbot-sqlite --expected ~/path/to/expected.txt --details ~/path/to/details.txt
   ```

## Production

Same `lcload.py` command with production's connection string in `LOAD_DATABASE_URL` (typed into
your shell, never saved here): first without `--apply` and read the report, then with it, then once
more to see `Changes: 0`. Production has no local JWT secret, so parity runs only in the rehearsal.

## Tests

`tests/test_load.py` builds a made-up snapshot from `tests/fixtures/sqlite_schema.sql` (the old
schema, no data) and loads it into the live test database. The parity tests also need
`LCBOT_OLD_CODE` pointing at the old checkout.
