#!/usr/bin/env python3
"""Load SH Bot's SQLite data into the `sh_bot` schema on the TC data estate (Supabase).

    python scripts/supabase/load.py --sqlite path/to/avails.db --dry-run
    python scripts/supabase/load.py --sqlite path/to/avails.db

Reads DATABASE_URL from the environment: your PR's preview branch for a rehearsal, or
production when Wilson runs the cutover load. Never put it in a file in this repo.

The load is safe to run again and again:
  * Every row's id is a uuid made from its SQLite table and id (uuid5), so a second run
    updates the same rows instead of adding new ones, and references resolve without a
    lookup. The SQLite id is also kept in `legacy_id`.
  * --dry-run does all of it inside one transaction and rolls it back, so foreign keys and
    checks are tested but nothing stays.

People are matched to identity.people by telegram_id, to exactly one row. Anyone unmatched
goes on unmatched.csv for HR. Their rows are skipped where a person is required, and the
person column is left empty where it is optional. Both kinds are counted in the report.

Writes to --out (default ./supabase-export/, which is gitignored):
  report.md      per-table counts: exported = loaded + skipped, with each skip's reason
  unmatched.csv  Telegram users with no person on identity.people. It holds names: keep it
                 on your laptop, send it to HR only, and delete it after cutover.

On-Site appointments hold customers' names, phones, emails and addresses. They are skipped
until the data security decision allows them; --include-appointments loads them.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import pathlib
import sqlite3
import sys
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Singapore")
NS = uuid.UUID("5b0d1c6e-6f4e-4c55-9a43-2b2f6f0c8a11")   # SH Bot legacy ids; never change


def uid(table: str, key) -> str:
    return str(uuid.uuid5(NS, f"{table}:{key}"))


# ------------------------------------------------------------------ value conversions

def ts(v):
    if v in (None, ""):
        return None
    d = datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def day(v):
    if v in (None, ""):
        return None
    return date.fromisoformat(str(v)[:10])


def flag(v):
    return None if v is None else bool(int(v))


def js(v):
    if v in (None, ""):
        return None
    return json.dumps(json.loads(v))


class Skip(Exception):
    """This row can't be loaded; the message is the reason in the report."""


class Loader:
    def __init__(self, src: sqlite3.Connection, pg, people: dict[int, str], args):
        self.src, self.pg, self.people, self.args = src, pg, people, args
        self.counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.unmatched: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
        self.loaded_ids: dict[str, set] = collections.defaultdict(set)

    # -- helpers used by the row builders

    def has(self, table: str) -> bool:
        return bool(self.src.execute(
            "select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone())

    def cols(self, table: str) -> set:
        return {r[1] for r in self.src.execute(f"pragma table_info({table})")}

    def person(self, tg, where: str, required: bool):
        if tg in (None, "", 0):
            if required:
                raise Skip("no person on the row")
            return None
        tg = int(tg)
        pid = self.people.get(tg)
        if pid is None:
            self.unmatched[tg][where] += 1
            if required:
                raise Skip("person not on identity.people")
            self.counts[where]["person left empty (unmatched)"] += 1
        return pid

    def ref(self, table: str, legacy, required: bool = True):
        if legacy in (None, ""):
            if required:
                raise Skip(f"no {table} on the row")
            return None
        u = uid(table, legacy)
        if u not in self.loaded_ids[table]:
            if required:
                raise Skip(f"its {table} row was skipped or is missing")
            return None
        return u

    # -- the load

    def table(self, target: str, source: str, build, conflict: list[str], where: str = "",
              skip_all: str | None = None):
        c = self.counts.setdefault(target, collections.Counter())
        if not self.has(source):
            c["(source table not in this export)"] += 0
            return
        rows = self.src.execute(f"select * from {source} {where}").fetchall()
        c["exported"] += len(rows)
        if skip_all:
            c[f"skipped: {skip_all}"] += len(rows)
            return
        out = []
        for r in rows:
            try:
                rec = build(dict(r))
            except Skip as e:
                c[f"skipped: {e}"] += 1
                continue
            except (ValueError, json.JSONDecodeError) as e:
                c[f"skipped: value doesn't fit ({type(e).__name__})"] += 1
                continue
            out.append(rec)
        if out:
            self.upsert(target, out, conflict)
            if "id" in out[0]:
                self.loaded_ids[target].update(r["id"] for r in out)
        c["loaded"] += len(out)

    def upsert(self, target: str, rows: list[dict], conflict: list[str]):
        names = list(rows[0])
        updates = [n for n in names if n not in conflict]
        sql = (f"insert into sh_bot.{target} ({', '.join(names)}) "
               f"values ({', '.join(['%s'] * len(names))}) "
               f"on conflict ({', '.join(conflict)}) do ")
        sql += ("update set " + ", ".join(f"{n} = excluded.{n}" for n in updates)) if updates else "nothing"
        with self.pg.cursor() as cur:
            cur.executemany(sql, [tuple(r[n] for n in names) for r in rows])


def run(L: Loader):
    p = L.person
    has_col = {t: L.cols(t) for t in ("agents", "special_requests") if L.has(t)}

    # ---- people. Approved agents are members; pending and declined are join requests.
    def member(r):
        return {
            "person_id": p(r["user_id"], "members", True),
            "access_level": r.get("role") or "agent",
            "active": bool(r.get("active", 1)),
            "support_name": r.get("support_name"),
            "dm_reachable": bool(r.get("dm_ok") or 0),
            "on_availability_board": bool(r.get("on_avails", 1)),
            "tagged_in_shift_calls": bool(r.get("tag_calls", 1)),
            "salaried": bool(r.get("salaried") or 0),
            "country_code": r.get("country") or None,
            "fs_store_name": r.get("store") or None,
            "requested_at": ts(r.get("requested_at")),
            "decided_by_person_id": p(r.get("decided_by"), "members", False),
            "decided_at": ts(r.get("decided_at")),
        }
    status_ok = "status" in has_col.get("agents", set())
    L.table("members", "agents", member, ["person_id"],
            "where status = 'active'" if status_ok else "")
    members = {str(r[0]) for r in L.pg.execute(
        "select person_id from sh_bot.members").fetchall()} if L.pg else set()

    def join_request(r):
        tg = int(r["user_id"])
        if not (r["name"] or "").strip():
            raise Skip("no name on the request")
        return {
            "id": uid("join_requests", tg),
            "telegram_id": tg,
            "telegram_username": r.get("username"),
            "full_name": r["name"],
            "status": r["status"],
            "person_id": L.people.get(tg),
            "notice_messages": js(r.get("req_msgs")),
            "requested_at": ts(r.get("requested_at")) or datetime.now(TZ),
            "decided_by_person_id": p(r.get("decided_by"), "join_requests", False),
            "decided_at": None if r["status"] == "pending" else (ts(r.get("decided_at")) or datetime.now(TZ)),
        }
    if status_ok:
        L.table("join_requests", "agents", join_request, ["id"],
                "where status in ('pending', 'declined')")

    def team(r):
        pid = p(r["user_id"], "member_teams", True)
        if pid not in members:
            raise Skip("not a member")
        return {"person_id": pid, "team": r["dept"], "created_at": ts(r["created_at"])}
    L.table("member_teams", "memberships", team, ["person_id", "team"])

    # agents.channels is a JSON list: one row per channel.
    c = L.counts.setdefault("member_channels", collections.Counter())
    if "channels" in has_col.get("agents", set()):
        chan_rows = []
        for r in L.src.execute("select user_id, channels from agents where channels is not null "
                               + ("and status = 'active'" if status_ok else "")):
            pid = L.people.get(int(r[0]))
            keys = json.loads(r[1] or "[]")
            c["exported"] += len(keys)
            if pid not in members:
                c["skipped: not a member"] += len(keys)
                continue
            chan_rows += [{"person_id": pid, "channel_key": k} for k in keys]
        if chan_rows:
            L.upsert("member_channels", chan_rows, ["person_id", "channel_key"])
        c["loaded"] += len(chan_rows)

    # ---- settings
    L.table("settings", "settings", lambda r: {"key": r["key"], "value": r["value"]}, ["key"])
    L.table("presets", "presets", lambda r: {"name": r["name"], "config": js(r["config"])}, ["name"])

    # ---- the board
    L.table("weeks", "weeks", lambda r: {
        "id": uid("weeks", r["id"]), "legacy_id": r["id"], "label": r["label"],
        "starts_on": day(r["start_date"]), "ends_on": day(r["end_date"]),
        "key_events": r["key_events"], "deadline_at": ts(r["deadline"]),
        "status": r["status"], "header_message_id": r["header_msg_id"],
    }, ["id"])
    L.table("days", "days", lambda r: {
        "id": uid("days", r["id"]), "legacy_id": r["id"], "week_id": L.ref("weeks", r["week_id"]),
        "position": r["idx"], "name": r["name"], "falls_on": day(r["the_date"]),
        "message_id": r["msg_id"],
    }, ["id"])
    L.table("slots", "slots", lambda r: {
        "id": uid("slots", r["id"]), "legacy_id": r["id"], "day_id": L.ref("days", r["day_id"]),
        "position": r["idx"], "label": r["label"], "start_minute": r["start_min"],
        "capacity": r.get("capacity"),
    }, ["id"])
    L.table("signups", "signups", lambda r: {
        "slot_id": L.ref("slots", r["slot_id"]), "person_id": p(r["user_id"], "signups", True),
        "signed_up_at": ts(r["ts"]),
    }, ["slot_id", "person_id"])
    L.table("confirmations", "confirmations", lambda r: {
        "week_id": L.ref("weeks", r["week_id"]), "person_id": p(r["user_id"], "confirmations", True),
        "confirmed_at": ts(r["ts"]),
    }, ["week_id", "person_id"])
    L.table("fixed_slots", "fixed_slots", lambda r: {
        "person_id": p(r["user_id"], "fixed_slots", True), "day_name": r["day_name"], "label": r["label"],
    }, ["person_id", "day_name", "label"])
    L.table("shift_requests", "drop_requests", lambda r: {
        "id": uid("shift_requests", r["id"]), "legacy_id": r["id"], "kind": r.get("kind") or "drop",
        "person_id": p(r["agent_id"], "shift_requests", True), "slot_id": L.ref("slots", r["slot_id"]),
        "reason": r["reason"], "target_person_id": p(r.get("target_id"), "shift_requests", False),
        "requested_at": ts(r["requested_at"]), "status": r["status"],
        "decided_by_person_id": p(r["decided_by"], "shift_requests", False),
        "decided_at": ts(r["decided_at"]), "notified": r.get("notified"),
    }, ["id"])

    # ---- time and pay
    L.table("time_entries", "time_entries", lambda r: {
        "id": uid("time_entries", r["id"]), "legacy_id": r["id"],
        "person_id": p(r["agent_id"], "time_entries", True),
        "slot_id": uid("slots", r["slot_id"]) if r["slot_id"] else None,
        "worked_on": day(r["the_date"]), "clock_in_at": ts(r["clock_in"]), "clock_out_at": ts(r["clock_out"]),
        "status": r["status"], "source": r["source"], "note": r["note"],
        "shift_label": r.get("shift_label"), "opening_posted": bool(r.get("opening_posted") or 0),
    }, ["id"])
    L.table("time_edits", "time_edits", lambda r: {
        "id": uid("time_edits", r["id"]), "legacy_id": r["id"],
        "time_entry_id": L.ref("time_entries", r["entry_id"]),
        "changed_by_person_id": p(r["changed_by"], "time_edits", True),
        "before": r["before"], "after": r["after"], "reason": r["reason"], "changed_at": ts(r["changed_at"]),
    }, ["id"])
    L.table("ot_claims", "ot_claims", lambda r: {
        "id": uid("ot_claims", r["id"]), "legacy_id": r["id"],
        "time_entry_id": L.ref("time_entries", r["entry_id"]),
        "person_id": p(r["agent_id"], "ot_claims", True), "claimed_out_at": ts(r["claimed_out"]),
        "ot_minutes": r["ot_minutes"], "status": r["status"], "asked_at": ts(r["asked_at"]),
        "decided_by_person_id": p(r["decided_by"], "ot_claims", False), "decided_at": ts(r["decided_at"]),
        "notified": r["notified"],
    }, ["id"])
    L.table("pay_rates", "pay_rates", lambda r: {
        "id": uid("pay_rates", r["id"]), "legacy_id": r["id"],
        # No agent means the default rate; an unmatched agent's own rate can't be loaded.
        "person_id": p(r["agent_id"], "pay_rates", True) if r["agent_id"] else None,
        "cents": r["cents"], "effective_on": day(r["effective_from"]),
        "created_by_person_id": p(r["created_by"], "pay_rates", False), "created_at": ts(r["created_at"]),
    }, ["id"])
    L.table("reviews", "reviews", lambda r: {
        "id": uid("reviews", r["id"]), "legacy_id": r["id"], "person_id": p(r["agent_id"], "reviews", True),
        "reviewed_on": day(r["the_date"]), "telegram_photo_id": r["photo_id"], "note": r["note"],
        "added_by_person_id": p(r["added_by"], "reviews", True), "created_at": ts(r["created_at"]),
    }, ["id"])

    # ---- handovers
    L.table("channel_posts", "channel_posts", lambda r: {
        "id": uid("channel_posts", r["id"]), "legacy_id": r["id"], "kind": r.get("kind") or "handover",
        "channel_key": r["channel"], "chat_id": r["chat_id"], "thread_id": r["thread_id"],
        "message_id": r["message_id"], "person_id": p(r["agent_id"], "channel_posts", False),
        "posted_on": day(r["the_date"]), "created_at": ts(r["created_at"]),
    }, ["id"])
    L.table("handovers", "handovers", lambda r: {
        "id": uid("handovers", r["id"]), "legacy_id": r["id"], "person_id": p(r["agent_id"], "handovers", True),
        "handover_on": day(r["the_date"]), "body": r["body"], "created_at": ts(r["created_at"]),
    }, ["id"])

    def case(r):
        g = r.get
        return {
            "id": uid("handover_cases", r["id"]), "legacy_id": r["id"],
            "person_id": p(r["agent_id"], "handover_cases", True), "handover_on": day(r["the_date"]),
            "section": r["section"], "priority": r["prio"], "platform": r["platform"], "flag": r["flag"],
            "online_store_code": r["store"], "channel_key": g("channel"), "customer_username": r["username"],
            "order_no": g("order_no"), "product": g("product"), "happened": g("happened"),
            "done": g("done"), "need": g("need"), "body": r["body"], "origin": g("origin") or "sh",
            "closed": bool(r["closed"]), "closed_by_person_id": p(r["closed_by"], "handover_cases", False),
            "closed_at": ts(r["closed_at"]),
            "sh_closed": bool(g("sh_closed") or 0),
            "sh_closed_by_person_id": p(g("sh_closed_by"), "handover_cases", False),
            "sh_closed_at": ts(g("sh_closed_at")),
            "on_closed": bool(g("on_closed") or 0),
            "on_closed_by_person_id": p(g("on_closed_by"), "handover_cases", False),
            "on_closed_at": ts(g("on_closed_at")),
            "posted_to_chat": bool(g("chat_posted") or 0),
            "channel_post_id": L.ref("channel_posts", g("post_id"), required=False),
            "handed_back": bool(g("handed_back") or 0), "pm_notified": bool(g("pm_notified") or 0),
            "created_at": ts(r["created_at"]),
        }
    L.table("handover_cases", "ho_cases", case, ["id"])
    L.table("case_events", "case_log", lambda r: {
        "id": uid("case_events", r["id"]), "legacy_id": r["id"],
        "handover_case_id": L.ref("handover_cases", r["case_id"]),
        "person_id": p(r["user_id"], "case_events", False), "side": r["side"], "action": r["action"],
        "note": r["note"], "created_at": ts(r["created_at"]),
    }, ["id"])

    # ---- On-Site
    hold = None if L.args.include_appointments else "customer data waits for the data security decision"

    def appointment(r):
        return {
            "id": uid("appointments", r["id"]), "legacy_id": r["id"], "country_code": r["country"],
            "specialist_person_id": p(r["specialist_id"], "appointments", False),
            "visit_type": r["visit_type"], "starts_at": ts(r["starts_at"]),
            "duration_minutes": r["duration_min"], "customer_name": r["customer_name"],
            "customer_phone": r["phone"], "customer_email": r["email"], "address": r["address"],
            "postal_code": r["postal"], "product": r["product"], "issue": r["issue"],
            "order_no": r["order_no"], "status": r["status"],
            "created_by_person_id": p(r["created_by"], "appointments", False), "created_at": ts(r["created_at"]),
            "proposed_starts_at": ts(r["proposed_starts_at"]),
            "proposed_by_person_id": p(r["proposed_by"], "appointments", False),
            "arrived_at": ts(r["arrived_at"]), "left_at": ts(r["left_at"]), "outcome": r["outcome"],
            "amount_cents": r["amount_cents"], "currency": r["currency"], "pay_method": r["pay_method"],
            "followed_up": bool(r["followup_done"]),
        }
    L.table("appointments", "appointments", appointment, ["id"], skip_all=hold)
    L.table("appointment_events", "appointment_log", lambda r: {
        "id": uid("appointment_events", r["id"]), "legacy_id": r["id"],
        "appointment_id": L.ref("appointments", r["appt_id"]),
        "person_id": p(r["user_id"], "appointment_events", False), "action": r["action"],
        "note": r["note"], "created_at": ts(r["created_at"]),
    }, ["id"], skip_all=hold)
    L.table("specialist_blocks", "specialist_blocks", lambda r: {
        "id": uid("specialist_blocks", r["id"]), "legacy_id": r["id"],
        "person_id": p(r["user_id"], "specialist_blocks", True), "starts_at": ts(r["starts_at"]),
        "ends_at": ts(r["ends_at"]), "part": r.get("part"), "note": r["note"], "created_at": ts(r["created_at"]),
    }, ["id"])

    # ---- Ops
    L.table("special_requests", "special_requests", lambda r: {
        "id": uid("special_requests", r["id"]), "legacy_id": r["id"], "order_no": r["order_no"],
        "country_code": r.get("country") or None, "category": r["category"], "details": r["details"],
        "delivery_on": day(r["delivery_date"]), "timeframe": r["timeframe"],
        "timeframe_other": r["timeframe_other"], "after_cutoff": bool(r["after_cutoff"]),
        "urgent": bool(r.get("urgent") or 0), "source": r.get("source") or "bot",
        "submitted_name": r.get("submitted_name"), "status": r["status"], "reason": r["reason"],
        "created_by_person_id": p(r["created_by"], "special_requests", False), "created_at": ts(r["created_at"]),
        "taken_by_person_id": p(r.get("taken_by"), "special_requests", False), "taken_at": ts(r.get("taken_at")),
        "ops_note": r.get("ops_note"),
        "handled_by_person_id": p(r["handled_by"], "special_requests", False), "handled_at": ts(r["handled_at"]),
        "ops_chat_id": r["ops_chat_id"], "ops_message_id": r["ops_msg_id"],
        "needs_sheet_sync": bool(r["sheet_dirty"]), "sheet_row": r.get("sheet_row"),
    }, ["id"])


def write_report(L: Loader, out: pathlib.Path, dry: bool):
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# SH Bot load report ({'dry run, rolled back' if dry else 'loaded'})", "",
             f"Run {datetime.now(TZ):%a %d %b %Y %H:%M} SGT.", "",
             "| table | exported | loaded | skipped | why |", "|---|---:|---:|---:|---|"]
    bad = []
    for t, c in L.counts.items():
        skips = {k: v for k, v in c.items() if k.startswith("skipped")}
        notes = {k: v for k, v in c.items() if k not in ("exported", "loaded") and not k.startswith("skipped")}
        why = "; ".join(f"{k.removeprefix('skipped: ')} ({v})" for k, v in skips.items())
        why += ("; " if why and notes else "") + "; ".join(f"{k} ({v})" for k, v in notes.items() if v)
        lines.append(f"| {t} | {c['exported']} | {c['loaded']} | {sum(skips.values())} | {why} |")
        if c["exported"] != c["loaded"] + sum(skips.values()):
            bad.append(t)
    lines += ["", f"Unmatched Telegram users: **{len(L.unmatched)}** (see unmatched.csv).",
              "", "Counts reconcile." if not bad else f"**Counts don't reconcile:** {', '.join(bad)}"]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    with open(out / "unmatched.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["telegram_id", "name_in_bot", "telegram_username", "rows_by_table"])
        for tg, where in sorted(L.unmatched.items()):
            a = L.src.execute("select name, username from agents where user_id = ?", (tg,)).fetchone() \
                if L.has("agents") else None
            w.writerow([tg, a["name"] if a else "", a["username"] if a else "",
                        "; ".join(f"{k} {v}" for k, v in where.items())])
    return not bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", required=True, help="the bot's SQLite file (from /backup)")
    ap.add_argument("--out", default="supabase-export", help="where report.md and unmatched.csv go")
    ap.add_argument("--dry-run", action="store_true", help="load inside a transaction, then roll back")
    ap.add_argument("--include-appointments", action="store_true",
                    help="load On-Site appointments (customer data); only once that is approved")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL (preview branch or production) in your shell, not in a file.")
    import psycopg   # pip install -r scripts/supabase/requirements.txt

    src = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    with psycopg.connect(url) as pg:
        people = {tg: str(pid) for tg, pid in pg.execute(
            "select telegram_id, id from identity.people where telegram_id is not null")}
        L = Loader(src, pg, people, args)
        run(L)
        ok = write_report(L, pathlib.Path(args.out), args.dry_run)
        if args.dry_run or not ok:
            pg.rollback()
        else:
            pg.commit()
    print((pathlib.Path(args.out) / "report.md").read_text())
    if not ok:
        sys.exit("Counts don't reconcile, so nothing was kept. Fix the cause and run again.")


if __name__ == "__main__":
    main()
