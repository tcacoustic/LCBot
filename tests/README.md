# Tests

`python -m unittest discover tests` runs the unit tests anywhere.

The live tests (`LiveTestCase`, tests/live.py) run the data module against a real Postgres and
PostgREST as the bot's own role, `lc_bot_dev_app`, so a missing grant or a wrong filter fails here
and not on LCBot Test. To run them:

1. A Postgres 16+ you can throw away, with a database `lcstub`.
2. Load `tests/fixtures/estate_stub.sql` (stand-ins for production's `identity` and `sts`; no real
   people), then the warehouse's LC Bot migrations in order:
   `tc-data-warehouse/supabase/migrations/20261120150000_*` to `20261120150600_*`, and any later `lc_bot*` ones.
3. PostgREST 12, with `db-uri` pointing at `lcstub` as `authenticator`, `db-schemas = "lc_bot_dev,lc_bot,identity"`,
   `db-anon-role = "anon"`, `jwt-secret` = `LCBOT_JWT_SECRET` (default in tests/live.py), on port 3300.
4. `LCBOT_LIVE_DB=1 python -m unittest discover tests`

Supabase's local stack (`supabase start`) works too: point `LCBOT_PG`, `LCBOT_PGRST_URL` and
`LCBOT_JWT_SECRET` at it (its REST URL ends in `/rest/v1`, so adjust `rest_path` in tests/live.py).
