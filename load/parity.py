"""Parity check: ask the old SQLite bot and the new warehouse bot the same questions after a load.

    PARITY_URL=http://127.0.0.1:54321 PARITY_APIKEY=... PARITY_JWT_SECRET=... \
        python load/parity.py SNAPSHOT.db --schema lc_bot_dev --old-code ../lcbot-sqlite \
        [--expected EXPECTED.txt] [--details DETAILS.txt]

--old-code is a checkout of the bot before the move (staging d925687). The old side runs on a copy
of the snapshot; the new side reads through the Data API as the schema's own role (`<schema>_app`),
with a token signed by the local stack's JWT secret. Never point this at production.

For every person, week, slot, month, overtime claim, open case and appointment in the snapshot,
both sides answer with their own code: access status, teams, admin, who holds each slot, who
confirmed each week, each person's timesheet per week and per month (minutes, pay, overtime,
shifts, open), each overtime claim, the open cases and each appointment.

One difference is agreed in #378 and built in: a removed member the old bot shows as `declined` is
`revoked` now (no access either way). It's counted as expected and listed in --details.
--expected lists the other differences already agreed, one fnmatch pattern of question keys per line
(`#` starts a comment). It names people by Telegram id, so keep it outside git, next to the
snapshot. Any other difference fails the run. The output is counts and PASS or FAIL only;
--details writes each difference (with ids) to a local file for you to read.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import hashlib
import hmac
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
UTC = dt.timezone.utc


# ---------------------------------------------------------------------------- what to ask about
def subjects(snapshot: str) -> dict:
    """Every id the questions cover, read from the snapshot itself (so both sides get the same list)."""
    db = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    def ids(q):
        try:
            return [r[0] for r in db.execute(q)]
        except sqlite3.OperationalError:
            return []

    people = set(ids("select user_id from agents")) | set(ids("select user_id from memberships"))
    for t, c in [("time_entries", "agent_id"), ("signups", "user_id"), ("pay_rates", "agent_id"),
                 ("ot_claims", "agent_id"), ("ho_cases", "agent_id"), ("appointments", "specialist_id")]:
        people |= set(ids(f"select distinct {c} from {t} where {c} is not null"))
    weeks = [dict(r) for r in db.execute("select id, start_date, end_date from weeks order by id")]
    slots = {w["id"]: ids(f"select s.id from slots s join days d on d.id = s.day_id where d.week_id = {int(w['id'])}")
             for w in weeks}
    months = sorted({m for m in ids("select distinct substr(the_date, 1, 7) from time_entries") if m})
    db.close()
    return {"people": sorted(int(p) for p in people), "weeks": weeks,
            "slots": {str(k): v for k, v in slots.items()}, "months": months}


# ---------------------------------------------------------------------------- one side's answers
def utc(v, tz) -> str | None:
    if not v:
        return None
    t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=tz)).astimezone(UTC).isoformat()


def answers(core, onsite, rows, ref, legacy, s: dict) -> dict:
    """The questions, asked through the bot's own functions where both versions have them.

    rows(table) -> that side's rows in the old shape; ref(table, legacy id) -> that side's id;
    legacy(row) -> the row's old id."""
    out = {}
    for tg in s["people"]:
        out[f"person:{tg}:status"] = core.agent_status(tg)
        out[f"person:{tg}:access"] = core.has_access(tg)
        out[f"person:{tg}:admin"] = core.is_admin(tg)
        out[f"person:{tg}:teams"] = sorted(core.depts_of(tg))
    confirmations = rows("confirmations")
    for w in s["weeks"]:
        wid = ref("weeks", w["id"])
        out[f"week:{w['id']}:confirmed"] = sorted(r["user_id"] for r in confirmations if r["week_id"] == wid) \
            if wid else "(not loaded)"
        for sl in s["slots"][str(w["id"])]:
            sid = ref("slots", sl)
            out[f"slot:{sl}:holders"] = sorted(r["user_id"] for r in core.slot_holders(sid)) if sid else "(not loaded)"
            out[f"slot:{sl}:capacity"] = core.slot_capacity(sid) if sid else "(not loaded)"
    periods = [(f"week {w['id']}", dt.date.fromisoformat(w["start_date"][:10]), dt.date.fromisoformat(w["end_date"][:10]))
               for w in s["weeks"]]
    for m in s["months"]:
        first = dt.date.fromisoformat(m + "-01")
        periods.append((m, *core.month_bounds(first)))
    for tg in s["people"]:
        for name, first, last in periods:
            t = core.timesheet(tg, first, last)
            if t["shifts"] or t["open"]:
                out[f"sheet:{tg}:{name}"] = {k: t[k] for k in ("minutes", "cents", "overtime", "open", "days")} | \
                    {"shifts": len(t["shifts"])}
    for r in rows("ot_claims"):
        out[f"ot:{legacy(r)}"] = [r["agent_id"], r["status"], r["ot_minutes"]]
    for r in core.open_cases():
        out[f"case:{legacy(r)}"] = [r["agent_id"], r["section"], r["prio"], r["store"], r["channel"]]
    for r in rows("appointments"):
        tz = onsite.COUNTRY_TZ.get(r["country"] or "", core.TZ)
        out[f"appt:{legacy(r)}"] = [r["status"], utc(r["starts_at"], tz), r["specialist_id"], r["country"]]
    return out


def old_side(code: str, snapshot: str, s: dict) -> dict:
    copy = os.path.join(tempfile.mkdtemp(), "old.db")
    shutil.copy(snapshot, copy)                    # the old bot alters its file at start; never the real one
    os.environ["DB_PATH"] = copy
    from botenv import placeholders
    placeholders()
    sys.path.insert(0, code)                       # the old checkout, not this one
    core = importlib.import_module("core")
    onsite = importlib.import_module("onsite")

    def rows(t):
        return [dict(r) for r in core.q(f"SELECT * FROM {t}")]

    return answers(core, onsite, rows, lambda t, i: i, lambda r: r["id"], s)


def token(role: str, secret: str):
    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"role": role, "exp": int(time.time()) + 3600}).encode())
    sig = b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())

    class T:
        def get_token(self):
            return f"{head}.{body}.{sig}"
    return T()


def new_side(schema: str, s: dict) -> dict:
    from botenv import bot_module
    core = bot_module("core", schema=schema)
    onsite = bot_module("onsite", schema=schema)
    from rest import RestClient
    core.data.db = RestClient(os.environ["PARITY_URL"], os.environ.get("PARITY_APIKEY", "unused"),
                              token(os.environ.get("PARITY_ROLE", f"{schema}_app"), os.environ["PARITY_JWT_SECRET"]),
                              schema=schema, rest_path=os.environ.get("PARITY_REST_PATH", "/rest/v1/"),
                              timezone=str(core.TZ))
    cache: dict = {}

    def rows(t):
        if t not in cache:
            cache[t] = core.data.rows(t)
        return cache[t]

    def ref(t, i):
        return next((r["id"] for r in rows(t) if r.get("legacy_id") == i), None)

    # a row made since the move has no old id: it can only ever be a difference
    return answers(core, onsite, rows, ref, lambda r: r.get("legacy_id") or f"new {r['id']}", s)


# ---------------------------------------------------------------------------- comparing
def side(args: list[str]) -> dict:
    """Run one side in its own process: the two versions are both called `core`."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "answers.json")
        subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--out", out], check=True)
        with open(out) as f:
            return json.load(f)


def read_expected(path: str | None) -> list[str]:
    if not path:
        return []
    with open(path) as f:
        return [ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()]


def compare(old: dict, new: dict, expected: list[str]) -> tuple[Counter, list[str]]:
    counts, details = Counter(), []
    for key in sorted(set(old) | set(new)):
        part = key.split(":")
        kind = f"{part[0]}:{part[-1]}" if part[0] in ("person", "slot", "week") else part[0]   # never an id
        counts[(kind, "asked")] += 1
        a, b = old.get(key, "(not asked)"), new.get(key, "(not asked)")
        if a == b:
            continue
        if a == "declined" and b == "revoked" and key.endswith(":status"):
            counts[(kind, "expected")] += 1      # #378: a leaver is revoked on identity; no access either way
            details.append(f"agreed     {key}: old {a!r} new {b!r} (#378: leavers are revoked)")
            continue
        if any(fnmatch.fnmatchcase(key, p) for p in expected):
            counts[(kind, "expected")] += 1
            details.append(f"expected   {key}: old {a!r} new {b!r}")
            continue
        counts[(kind, "UNEXPECTED")] += 1
        details.append(f"UNEXPECTED {key}: old {a!r} new {b!r}")
    return counts, details


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("snapshot")
    p.add_argument("--schema", required=True, choices=["lc_bot", "lc_bot_dev"])
    p.add_argument("--old-code")
    p.add_argument("--expected")
    p.add_argument("--details")
    p.add_argument("--side", choices=["old", "new"], help=argparse.SUPPRESS)
    p.add_argument("--subjects", help=argparse.SUPPRESS)
    p.add_argument("--out", help=argparse.SUPPRESS)
    a = p.parse_args(argv)

    if a.side:
        with open(a.subjects) as f:
            s = json.load(f)
        got = old_side(a.old_code, a.snapshot, s) if a.side == "old" else new_side(a.schema, s)
        with open(a.out, "w") as f:
            json.dump(got, f, default=str)
        return 0

    if not a.old_code:
        p.error("--old-code is required")
    for var in ("PARITY_URL", "PARITY_JWT_SECRET"):
        if not os.environ.get(var):
            p.error(f"set {var}")
    with tempfile.TemporaryDirectory() as d:
        sub = os.path.join(d, "subjects.json")
        with open(sub, "w") as f:
            json.dump(subjects(a.snapshot), f)
        common = [a.snapshot, "--schema", a.schema, "--subjects", sub, "--old-code", os.path.abspath(a.old_code)]
        old = side([*common, "--side", "old"])
        new = side([*common, "--side", "new"])
    counts, details = compare(old, new, read_expected(a.expected))
    kinds = sorted({k for k, _ in counts})
    print(f"{'question':<22}{'asked':>7}{'expected diff':>15}{'unexpected':>12}")
    for k in kinds:
        print(f"{k:<22}{counts[(k, 'asked')]:>7}{counts[(k, 'expected')]:>15}{counts[(k, 'UNEXPECTED')]:>12}")
    bad = sum(v for (k, w), v in counts.items() if w == "UNEXPECTED")
    if a.details:
        with open(a.details, "w") as f:
            f.write("\n".join(details) + "\n")
    print("PASS" if bad == 0 else f"FAIL: {bad} unexpected difference(s)")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
