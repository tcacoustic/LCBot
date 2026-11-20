"""A small client for the warehouse's Data API (PostgREST), the only way the bot reaches its database.

Every request carries the project's publishable key and a one-hour working token for the bot's own
role (`lc_bot_app`, or `lc_bot_dev_app` for LCBot Test), from service_token.py. Requests go to one
schema, `lc_bot` or `lc_bot_dev`, except reads of who someone is, which go to `identity`.
Filters are PostgREST's own (`status="eq.active"`, `order="person_id"`); see
https://postgrest.org/en/stable/references/api/tables_views.html.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class DataApiError(Exception):
    def __init__(self, status: int, code: str | None, message: str):
        super().__init__(f"Data API {status} {code or ''}: {message}".strip())
        self.status, self.code = status, code


def _urllib_http(method: str, url: str, headers: dict, body: str | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body.encode() if body is not None else None, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class RestClient:
    def __init__(self, supabase_url: str, publishable_key: str, tokens, *, schema: str, http=_urllib_http,
                 rest_path: str = "/rest/v1/"):
        # rest_path is "/" only for a bare local PostgREST in tests; Supabase serves it under /rest/v1/.
        self._base = supabase_url.rstrip("/") + rest_path
        self._key, self._tokens, self.schema, self._http = publishable_key, tokens, schema, http

    def _call(self, method: str, path: str, params: dict, body=None, *, schema: str | None = None,
              prefer: str | None = None):
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        profile = schema or self.schema
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._tokens.get_token()}",
                   "Accept-Profile" if method == "GET" else "Content-Profile": profile}
        if method != "GET":
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        status, raw = self._http(method, url, headers, json.dumps(body) if body is not None else None)
        data = json.loads(raw) if raw else None
        if not 200 <= status < 300:
            d = data if isinstance(data, dict) else {}
            raise DataApiError(status, d.get("code"), d.get("message") or str(data))
        return data

    def select(self, table: str, *, schema: str | None = None, **params) -> list[dict]:
        return self._call("GET", table, params, schema=schema)

    def insert(self, table: str, rows, *, on_conflict: str | None = None, returning: bool = True) -> list[dict] | None:
        """`returning=False` for a table the role may write but not read (time_edits): the answer would need select."""
        prefer = ("return=representation" if returning else "return=minimal") + (
            ",resolution=merge-duplicates" if on_conflict else "")
        params = {"on_conflict": on_conflict} if on_conflict else {}
        return self._call("POST", table, params, rows, prefer=prefer)

    def update(self, table: str, values: dict, **filters) -> list[dict]:
        if not filters:
            raise ValueError(f"update of {table} needs a filter")
        return self._call("PATCH", table, filters, values, prefer="return=representation")

    def delete(self, table: str, **filters) -> list[dict]:
        if not filters:
            raise ValueError(f"delete from {table} needs a filter")
        return self._call("DELETE", table, filters, prefer="return=representation")

    def rpc(self, function: str, args: dict):
        return self._call("POST", "rpc/" + function, {}, args)
