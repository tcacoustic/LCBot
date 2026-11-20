"""Live tests: the data module against a real Postgres and PostgREST, acting as the bot's own role.

The database is built from the warehouse's real migrations (tcacoustic/tc-data-warehouse, the
`lc_bot`/`lc_bot_dev` files) on top of tests/fixtures/estate_stub.sql, a stand-in for the parts of
production's `identity` and `sts` they depend on. No real person is in it.

Set up once (see tests/README.md), then:
    LCBOT_LIVE_DB=1 python -m unittest discover tests
Without LCBOT_LIVE_DB the live tests are skipped, so the unit tests run anywhere.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
import unittest

from rest import RestClient

PG = os.environ.get("LCBOT_PG", "postgresql://postgres@/lcstub?host=/tmp&port=55432")
PGRST_URL = os.environ.get("LCBOT_PGRST_URL", "http://127.0.0.1:3300")
JWT_SECRET = os.environ.get("LCBOT_JWT_SECRET", "local-test-secret-that-is-at-least-32-chars")
LIVE = os.environ.get("LCBOT_LIVE_DB") == "1"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class LocalToken:
    """An HS256 token for a local PostgREST, carrying `role` (what sign-token-workload's working token carries)."""

    def __init__(self, role: str):
        head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        body = _b64(json.dumps({"role": role, "exp": int(time.time()) + 3600}).encode())
        sig = _b64(hmac.new(JWT_SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
        self._token = f"{head}.{body}.{sig}"

    def get_token(self) -> str:
        return self._token


def sql(statement: str) -> str:
    """Run SQL as postgres (fixtures and checks only; the code under test never does this)."""
    out = subprocess.run(["psql", PG, "-v", "ON_ERROR_STOP=1", "-qtAc", statement],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def client(schema: str = "lc_bot_dev", role: str = "lc_bot_dev_app") -> RestClient:
    return RestClient(PGRST_URL, "local-publishable-key", LocalToken(role), schema=schema, rest_path="/",
                      timezone="Asia/Singapore")


@unittest.skipUnless(LIVE, "set LCBOT_LIVE_DB=1 with a local database and PostgREST (tests/README.md)")
class LiveTestCase(unittest.TestCase):
    """Each test starts from empty lc_bot_dev tables and no test people (telegram ids 990000000 and up)."""

    def setUp(self):
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
        os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
        tables = sql("select string_agg(format('lc_bot_dev.%I', tablename), ', ') from pg_tables "
                     "where schemaname = 'lc_bot_dev'")
        sql(f"truncate {tables} cascade; delete from identity.people where telegram_id >= 990000000;")
        self.db = client()

    def person(self, telegram_id: int, full_name: str, *, nickname: str | None = None, country: str = "SG",
               person_type: str = "staff", status: str = "active") -> str:
        return sql("insert into identity.people (full_name, nickname, telegram_id, country_code, person_type, status) "
                   f"values ({_lit(full_name)}, {_lit(nickname)}, {telegram_id}, {_lit(country)}, "
                   f"{_lit(person_type)}, {_lit(status)}) returning id")


def _lit(v) -> str:
    return "null" if v is None else "'" + str(v).replace("'", "''") + "'"
