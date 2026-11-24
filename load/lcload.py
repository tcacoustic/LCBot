"""Load LC Bot's SQLite snapshot into the warehouse: `lc_bot` (production) or `lc_bot_dev` (LCBot Test).

    LOAD_DATABASE_URL=postgresql://... python load/lcload.py SNAPSHOT.db --schema lc_bot_dev \
        [--people MERGE_LIST.csv] [--apply]

Without --apply it loads inside a transaction and rolls back, so the report shows what a real run
would do. Run it again after a real run and every table should say 0 new and 0 changed.

- People are matched to identity.people by Telegram id only. Someone the bot knows who isn't there
  is created through the schema's join function (`add_person`), as `active` if the bot has them
  active and `revoked` if they've left. Declined people with no history aren't created.
- The merge list (kept outside git) is a CSV with `telegram_id,full_name,nickname,country_code`.
  full_name and country_code are used only for someone being created; a nickname is set on anyone.
- Every row keeps its SQLite id in legacy_id; tables without one are keyed on their primary key.
  Re-running updates rows in place and never duplicates them.
- Naive times are read as Asia/Singapore, and as the visit country's zone for appointments.

The report is counts only, never names or ids: the repo is public and so are CI logs.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from botenv import bot_module  # noqa: E402

SG = ZoneInfo("Asia/Singapore")

# Old table -> {old column: old table it points at}. Person columns come from data.TABLES.
FKS = {
    "days": {"week_id": "weeks"},
    "slots": {"day_id": "days"},
    "signups": {"slot_id": "slots"},
    "confirmations": {"week_id": "weeks"},
    "drop_requests": {"slot_id": "slots"},
    "time_entries": {"slot_id": "slots"},
    "ot_claims": {"entry_id": "time_entries"},
    "time_edits": {"entry_id": "time_entries"},
    "ho_cases": {"post_id": "channel_posts"},
    "case_log": {"case_id": "ho_cases"},
    "appointments": {"followup_of": "appointments"},
    "appointment_log": {"appt_id": "appointments"},
    "onsite_requests": {"appt_id": "appointments"},
    "cab_claims": {"appt_id": "appointments"},
}
ORDER = ["weeks", "days", "slots", "signups", "confirmations", "fixed_slots", "presets", "drop_requests",
         "time_entries", "ot_claims", "time_edits", "pay_rates", "reviews", "handovers", "channel_posts",
         "ho_cases", "case_log", "appointments", "appointment_log", "specialist_blocks", "onsite_requests",
         "cab_claims", "special_requests"]
# Old columns with no new home, on purpose (#378: names come from identity now).
DROPPED = {("signups", "name")}

# Settings whose key ends in an old row id, and the table that id is from. Values that hold an id too.
SETTING_KEYS = {"case_pick": "ho_cases", "visit_pin": "appointments", "nf_draft": "appointments",
                "handover_msgs": "appointments", "visit_checks": "appointments",
                "exchange_back": "drop_requests", "red_flag": "special_requests", "cab_msgs": "cab_claims"}
SETTING_VALUES = {"exchange_back": "slots", "payphoto_wait": "appointments"}
SETTINGS_DROPPED = {"memberships_moved"}   # the old one-off move of depts into memberships


class Skip(Exception):
    """A row that can't be loaded, with the reason the report gives."""


class Report:
    def __init__(self):
        self.tables: dict[str, Counter] = defaultdict(Counter)
        self.skips: dict[str, Counter] = defaultdict(Counter)
        self.people = Counter()
        self.notes = Counter()

    def skip(self, table, reason):
        self.skips[table][reason] += 1

    def lines(self) -> list[str]:
        out = ["People: " + ", ".join(f"{k} {v}" for k, v in sorted(self.people.items()))]
        out.append(f"{'table':<20}{'exported':>9}{'new':>6}{'changed':>8}{'same':>6}{'skipped':>8}")
        for t, c in self.tables.items():
            skipped = sum(self.skips[t].values())
            out.append(f"{t:<20}{c['exported']:>9}{c['new']:>6}{c['changed']:>8}{c['same']:>6}{skipped:>8}")
            for reason, n in sorted(self.skips[t].items()):
                out.append(f"    skipped {n}: {reason}")
        for note, n in sorted(self.notes.items()):
            out.append(f"Note: {note}: {n}")
        return out

    def balanced(self) -> bool:
        return all(c["exported"] == c["new"] + c["changed"] + c["same"] + sum(self.skips[t].values())
                   for t, c in self.tables.items())

    def changes(self) -> int:
        return sum(c["new"] + c["changed"] for c in self.tables.values()) + self.people["created"] \
            + self.people["nicknames set"]


# ---------------------------------------------------------------------------- values
def moment(v, tz=SG):
    if v in (None, ""):
        return None
    t = dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=tz)


def convert(v, pgtype: str, tz=SG):
    if v is None or (v == "" and pgtype != "text"):
        return None
    if pgtype == "timestamp with time zone":
        return moment(v, tz)
    if pgtype == "date":
        return dt.date.fromisoformat(str(v)[:10])
    if pgtype == "boolean":
        return bool(int(v))
    if pgtype == "bigint":
        return int(v)
    if pgtype == "jsonb":
        return Jsonb(json.loads(v) if isinstance(v, str) else v)
    return str(v)


class Loader:
    def __init__(self, conn, schema: str, snapshot: sqlite3.Connection, people_csv: dict[int, dict],
                 report: Report):
        self.conn, self.schema, self.old, self.merge, self.r = conn, schema, snapshot, people_csv, report
        self.data = bot_module("data")
        self.cols = self._columns()
        self.pid: dict[int, str] = {}
        self.ids: dict[str, dict[int, str]] = {}   # old table -> {legacy id: uuid}

    # ------------------------------------------------------------------ database helpers
    def _columns(self) -> dict[str, dict[str, tuple[str, bool]]]:
        cols: dict = defaultdict(dict)
        for t, c, typ, nullable in self.conn.execute(
                "select table_name, column_name, data_type, is_nullable = 'YES' from information_schema.columns "
                "where table_schema = %s", (self.schema,)):
            cols[t][c] = (typ, nullable)
        return cols

    def _key(self, new: str) -> list[str]:
        if "legacy_id" in self.cols[new]:
            return ["legacy_id"]
        rows = self.conn.execute(
            "select a.attname from pg_index i join pg_attribute a on a.attrelid = i.indrelid "
            "and a.attnum = any(i.indkey) where i.indrelid = %s::regclass and i.indisprimary order by a.attnum",
            (f"{self.schema}.{new}",)).fetchall()
        return [r[0] for r in rows]

    def legacy(self, old: str) -> dict[int, str]:
        if old not in self.ids:
            new = self.data.TABLES[old][0]
            self.ids[old] = {k: str(v) for k, v in self.conn.execute(
                sql.SQL("select legacy_id, id from {} where legacy_id is not null").format(
                    sql.Identifier(self.schema, new)))}
        return self.ids[old]

    def upsert(self, label: str, new: str, rows: list[dict]) -> None:
        """Insert or update rows keyed on legacy_id (or the primary key); count new, changed and same."""
        c = self.r.tables[label]
        c["same"] += 0
        if not rows:
            return
        cols = list(rows[0])
        key = self._key(new)
        rest = [x for x in cols if x not in key]
        target = sql.Identifier(self.schema, new)
        if rest:
            on_conflict = sql.SQL("do update set {} where ({}) is distinct from ({})").format(
                sql.SQL(", ").join(sql.SQL("{0} = excluded.{0}").format(sql.Identifier(x)) for x in rest),
                sql.SQL(", ").join(sql.SQL("{}.{}").format(sql.Identifier(new), sql.Identifier(x)) for x in rest),
                sql.SQL(", ").join(sql.SQL("excluded.{}").format(sql.Identifier(x)) for x in rest))
            if len(rest) == 1:   # a one-column row comparison needs the row() spelling
                on_conflict = sql.SQL("do update set {0} = excluded.{0} where {1}.{0} is distinct from excluded.{0}") \
                    .format(sql.Identifier(rest[0]), sql.Identifier(new))
        else:
            on_conflict = sql.SQL("do nothing")
        stmt = sql.SQL("insert into {} as {} ({}) values ({}) on conflict ({}) {} returning (xmax = 0)").format(
            target, sql.Identifier(new), sql.SQL(", ").join(map(sql.Identifier, cols)),
            sql.SQL(", ").join(sql.Placeholder() * len(cols)), sql.SQL(", ").join(map(sql.Identifier, key)),
            on_conflict)
        with self.conn.cursor() as cur:
            cur.executemany(stmt, [tuple(r[x] for x in cols) for r in rows], returning=True)
            while True:
                got = cur.fetchone()
                if got is None:
                    c["same"] += 1
                else:
                    c["new" if got[0] else "changed"] += 1
                if not cur.nextset():
                    break

    # ------------------------------------------------------------------ the snapshot
    def old_rows(self, table: str) -> list[dict]:
        try:
            return [dict(r) for r in self.old.execute(f'select * from "{table}" order by rowid')]
        except sqlite3.OperationalError:
            return []

    def history_ids(self) -> set[int]:
        """Telegram ids in any person column of any table: everyone with history."""
        seen = set()
        for old, (_, _, people) in self.data.TABLES.items():
            for r in self.old_rows(old):
                seen |= {int(r[c]) for c in people if r.get(c) not in (None, "")}
        seen |= {int(r["user_id"]) for r in self.old_rows("memberships")}
        return seen

    # ------------------------------------------------------------------ people
    def people(self) -> None:
        agents = {int(a["user_id"]): a for a in self.old_rows("agents")}
        history = self.history_ids()
        wanted = {tg for tg, a in agents.items() if a["status"] == "active"} | history
        for tg, a in agents.items():
            if tg not in wanted:
                self.r.people["declined or pending, no history: not created"] += 1
        existing = {int(tg): str(i) for tg, i in self.conn.execute(
            "select telegram_id, id from identity.people where telegram_id = any(%s)", (list(wanted),))}
        fn = sql.SQL("select {}.add_person(%s, %s, %s, %s, %s)").format(sql.Identifier(self.schema))
        for tg in sorted(wanted):
            if tg in existing:
                self.pid[tg] = existing[tg]
                self.r.people["matched"] += 1
                continue
            a, m = agents.get(tg), self.merge.get(tg, {})
            name = m.get("full_name") or (a and (a["display_name"] or a["name"]))
            if not name:
                self.r.people["skipped: no name in the bot or the merge list"] += 1
                continue
            status = "active" if a and a["status"] == "active" else "revoked"
            country = m.get("country_code") or (a and a["country"]) or None
            self.pid[tg] = str(self.conn.execute(fn, (tg, name, m.get("nickname") or None, country, status))
                               .fetchone()[0])
            self.r.people[f"created {status}"] += 1
        for tg, m in self.merge.items():
            if m.get("nickname") and tg in self.pid:
                cur = self.conn.execute("update identity.people set nickname = %s "
                                        "where telegram_id = %s and nickname is distinct from %s",
                                        (m["nickname"], tg, m["nickname"]))
                self.r.people["nicknames set"] += cur.rowcount
        self.r.people["created"] = sum(v for k, v in self.r.people.items() if k.startswith("created "))
        self.members(agents, history)

    def members(self, agents: dict[int, dict], history: set[int]) -> None:
        conv = self.cols["members"]
        members, requests = [], []
        for tg, a in agents.items():
            self.r.tables["agents"]["exported"] += 1
            member = tg in self.pid and (a["status"] == "active" or (a["status"] == "declined" and tg in history))
            req = {"legacy_id": tg, "telegram_id": tg, "given_name": a["display_name"] or a["name"],
                   "telegram_name": a["name"], "telegram_username": a["username"],
                   "requested_at": moment(a["requested_at"]),
                   "request_message_ids": convert(a["req_msgs"], "jsonb"),
                   "decided_by_person_id": self.pid.get(int(a["decided_by"])) if a["decided_by"] else None,
                   "decided_at": moment(a["decided_at"]), "status": None, "person_id": None}
            if member:
                row = {"person_id": self.pid[tg]}
                for old, new in self.data._MEMBER_FIELDS.items():
                    row[new] = convert(a[old], conv[new][0])
                row.update({k: req[k] for k in ("requested_at", "decided_at", "decided_by_person_id",
                                                 "request_message_ids")})
                members.append(row)
                if a["decided_at"]:
                    requests.append({**req, "status": "approved", "person_id": self.pid[tg]})
            elif a["status"] == "pending":
                requests.append({**req, "status": "pending", "decided_at": None})
            elif a["status"] == "declined" and (req["decided_at"] or req["requested_at"]):
                requests.append({**req, "status": "declined", "decided_at": req["decided_at"] or req["requested_at"]})
            else:
                self.r.skip("agents", f"{a['status']} with no person and no date to record")
        self.upsert("members", "members", members)
        self.upsert("join_requests", "join_requests", requests)
        # every agents row went to members, join_requests or a skip
        c = self.r.tables.pop("agents")
        self.r.notes["bot users read (agents rows)"] = c["exported"]
        self.r.tables["members"]["exported"] = len(members)
        self.r.tables["join_requests"]["exported"] = len(requests)
        if self.r.skips.get("agents"):
            self.r.tables["agents (not loaded)"]["exported"] = sum(self.r.skips["agents"].values())
            self.r.skips["agents (not loaded)"] = self.r.skips.pop("agents")
        teams = []
        for m in self.old_rows("memberships"):
            self.r.tables["team_members"]["exported"] += 1
            tg = int(m["user_id"])
            if not any(x["person_id"] == self.pid.get(tg) for x in members):
                self.r.skip("team_members", "not a member")
                continue
            teams.append({"person_id": self.pid[tg], "team": m["dept"], "created_at": moment(m["created_at"])})
        self.upsert("team_members", "team_members", teams)

    # ------------------------------------------------------------------ tables
    def shape(self, old: str, r: dict) -> dict:
        new, names, people = self.data.TABLES[old]
        cols = self.cols[new]
        tz = SG
        if old == "appointments":
            tz = self.country_tz.get(r.get("country") or "", SG)
        out = {}
        if "legacy_id" in cols:
            out["legacy_id"] = int(r["id"])
        for c, v in r.items():
            if c == "id" or (old, c) in DROPPED:
                continue
            target = names.get(c, c)
            if target not in cols:
                raise SystemExit(f"{old}.{c} has nowhere to go in {self.schema}.{new}: add it to DROPPED or map it")
            typ, nullable = cols[target]
            if c in people:
                v = None if v in (None, "") else self.pid.get(int(v), False)
                if v is False:
                    if not nullable:
                        raise Skip(f"{c}: not on identity")
                    self.r.notes[f"{new}.{target} emptied: person not on identity"] += 1
                    v = None
            elif c in FKS.get(old, {}):
                ref = FKS[old][c]
                v = None if v in (None, "") else self.legacy(ref).get(int(v), False)
                if v is False:
                    if old == "appointments":   # a follow-up of a later visit: set on the second pass
                        v = None
                    elif not nullable:
                        raise Skip(f"{c}: its {ref} row isn't loaded")
                    else:
                        self.r.notes[f"{new}.{target} emptied: its {ref} row isn't loaded"] += 1
                        v = None
            else:
                try:
                    v = convert(v, typ, tz)
                except (ValueError, TypeError):
                    raise Skip(f"{c}: not a {typ}")
            if v is None and not nullable:
                raise Skip(f"{c}: empty")
            out[target] = v
        if old == "ho_cases" and not out.get("online_channel"):
            out["online_channel"] = self.channel_for(r.get("platform"), r.get("store")) or None
        return out

    def table(self, old: str) -> None:
        new = self.data.TABLES[old][0]
        rows = []
        for r in self.old_rows(old):
            self.r.tables[new]["exported"] += 1
            try:
                rows.append(self.shape(old, r))
            except Skip as e:
                self.r.skip(new, str(e))
        self.upsert(new, new, rows)
        self.ids.pop(old, None)
        if old == "appointments":
            self.followups()

    def followups(self) -> None:
        ids = self.legacy("appointments")
        target = sql.Identifier(self.schema, "appointments")
        for r in self.old_rows("appointments"):
            if r.get("followup_of") and int(r["followup_of"]) in ids and int(r["id"]) in ids:
                self.conn.execute(sql.SQL("update {} set followup_of_id = %s where id = %s "
                                          "and followup_of_id is distinct from %s").format(target),
                                  (ids[int(r["followup_of"])], ids[int(r["id"])], ids[int(r["followup_of"])]))

    def settings(self) -> None:
        rows = []
        for r in self.old_rows("settings"):
            self.r.tables["settings"]["exported"] += 1
            key, value = r["key"], r["value"]
            prefix, _, rest = key.partition(":")
            if key in SETTINGS_DROPPED:
                self.r.skip("settings", "retired setting")
                continue
            if prefix in SETTING_KEYS and rest:
                uuid = self.legacy(SETTING_KEYS[prefix]).get(int(rest)) if rest.isdigit() else None
                if not uuid:
                    self.r.skip("settings", f"{prefix}: its row isn't loaded")
                    continue
                key = f"{prefix}:{uuid}"
            if prefix in SETTING_VALUES and value and str(value).isdigit():
                value = self.legacy(SETTING_VALUES[prefix]).get(int(value))
                if not value:
                    self.r.skip("settings", f"{prefix}: the row it holds isn't loaded")
                    continue
            rows.append({"key": key, "value": value})
        self.upsert("settings", "settings", rows)

    def run(self) -> None:
        onsite = bot_module("onsite", schema=self.schema)
        self.country_tz, self.channel_for = onsite.COUNTRY_TZ, onsite.channel_for
        self.people()
        for old in ORDER:
            self.table(old)
        self.settings()


def read_people(path: str | None) -> dict[int, dict]:
    if not path:
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        out = {}
        for r in csv.DictReader(f):
            if (r.get("telegram_id") or "").strip().isdigit():
                out[int(r["telegram_id"])] = {k: (v or "").strip() for k, v in r.items()}
        return out


def load(database_url: str, snapshot: str, schema: str, people: str | None, apply: bool) -> Report:
    report = Report()
    old = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    with psycopg.connect(database_url, autocommit=False) as conn:
        try:
            Loader(conn, schema, old, read_people(people), report).run()
        except psycopg.Error as e:   # the message can quote a row's values, so it isn't printed
            conn.rollback()
            diag = e.diag
            raise SystemExit(f"Stopped, nothing written: {type(e).__name__} ({e.sqlstate}) on "
                             f"{diag.table_name or '?'} {diag.constraint_name or ''}".rstrip())
        if apply:
            conn.commit()
        else:
            conn.rollback()
    old.close()
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("snapshot")
    p.add_argument("--schema", required=True, choices=["lc_bot", "lc_bot_dev"])
    p.add_argument("--people", help="the merge list CSV (outside git)")
    p.add_argument("--apply", action="store_true", help="commit; without it the load is rolled back")
    a = p.parse_args(argv)
    url = os.environ.get("LOAD_DATABASE_URL")
    if not url:
        p.error("set LOAD_DATABASE_URL")
    report = load(url, a.snapshot, a.schema, a.people, a.apply)
    print(f"{'Loaded' if a.apply else 'Dry run (rolled back)'} into {a.schema}")
    print("\n".join(report.lines()))
    print(f"Counts balance: {'yes' if report.balanced() else 'NO'}")
    print(f"Changes: {report.changes()}")
    return 0 if report.balanced() else 1


if __name__ == "__main__":
    sys.exit(main())
