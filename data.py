"""The bot's data module: the only code that reaches the database, through rest.RestClient.

Feature code speaks Telegram ids and gets rows shaped like the old SQLite ones (`user_id`, `name`,
`role`, `dm_ok`, ...). Underneath, people are `identity.people` (read only; the bot adds someone
only through its schema's `add_person`), membership is `members`, and a /start that isn't yet a
person is a `join_requests` row.
"""
from __future__ import annotations

import datetime
import json
import re
import time

ROSTER_TTL_S = 60

# old agents column -> members column, for flags feature code reads and writes
_MEMBER_FIELDS = {
    "role": "access_level", "status": "status", "active": "active", "dm_ok": "dms_allowed",
    "support_name": "support_name", "on_avails": "shows_on_avails", "tag_calls": "tagged_in_calls",
    "salaried": "salaried", "channels": "online_channels", "store": "fs_store",
}
_PEOPLE_COLS = "id,telegram_id,full_name,nickname,country_code,status"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


NOTHING = "in.()"   # a filter no row matches


def _in(values) -> str:
    return "in.(" + ",".join(str(v) for v in values) + ")"


# ---------------------------------------------------------------------------- tables in the old shape
# Feature code reads and writes the bot's own tables under their SQLite names and columns. Each entry:
# new table name, {old column: new column}, and which old columns hold a person (a Telegram id in the
# old shape, a person_id in the database).
TABLES: dict[str, tuple[str, dict[str, str], set[str]]] = {
    "weeks": ("weeks", {"start_date": "starts_on", "end_date": "ends_on", "deadline": "deadline_at",
                        "header_msg_id": "header_message_id"}, set()),
    "days": ("days", {"the_date": "day_on", "msg_id": "message_id"}, set()),
    "slots": ("slots", {}, set()),
    "signups": ("signups", {"user_id": "person_id", "ts": "signed_at"}, {"user_id"}),
    "confirmations": ("confirmations", {"user_id": "person_id", "ts": "confirmed_at"}, {"user_id"}),
    "fixed_slots": ("fixed_slots", {"user_id": "person_id"}, {"user_id"}),
    "presets": ("presets", {}, set()),
    "drop_requests": ("drop_requests", {"agent_id": "agent_person_id", "target_id": "target_person_id",
                                        "decided_by": "decided_by_person_id"},
                      {"agent_id", "target_id", "decided_by"}),
    "ot_claims": ("ot_claims", {"entry_id": "time_entry_id", "agent_id": "agent_person_id",
                                "claimed_out": "claimed_out_at", "decided_by": "decided_by_person_id"},
                  {"agent_id", "decided_by"}),
    "reviews": ("reviews", {"agent_id": "agent_person_id", "the_date": "reviewed_on", "photo_id": "photo_file_id",
                            "added_by": "added_by_person_id"}, {"agent_id", "added_by"}),
    "pay_rates": ("pay_rates", {"agent_id": "agent_person_id", "effective_from": "effective_on",
                                "created_by": "created_by_person_id"}, {"agent_id", "created_by"}),
    "time_entries": ("time_entries", {"agent_id": "agent_person_id", "the_date": "worked_on",
                                      "clock_in": "clock_in_at", "clock_out": "clock_out_at"}, {"agent_id"}),
    "time_edits": ("time_edits", {"entry_id": "time_entry_id", "changed_by": "changed_by_person_id"},
                   {"changed_by"}),
    "handovers": ("handovers", {"agent_id": "agent_person_id", "the_date": "handover_on"}, {"agent_id"}),
    "ho_cases": ("handover_cases", {"agent_id": "agent_person_id", "the_date": "handover_on", "prio": "priority",
                                    "platform": "chat_platform", "store": "shop", "username": "customer_username",
                                    "closed_by": "closed_by_person_id", "channel": "online_channel",
                                    "sh_closed_by": "sh_closed_by_person_id", "on_closed_by": "on_closed_by_person_id",
                                    "post_id": "chat_post_id"},
                 {"agent_id", "closed_by", "sh_closed_by", "on_closed_by"}),
    "case_log": ("case_events", {"user_id": "person_id"}, {"user_id"}),
    "channel_posts": ("chat_posts", {"channel": "online_channel", "agent_id": "agent_person_id",
                                     "the_date": "posted_on"}, {"agent_id"}),
    "appointments": ("appointments", {"country": "country_code", "specialist_id": "specialist_person_id",
                                      "created_by": "created_by_person_id", "proposed_by": "proposed_by_person_id",
                                      "followup_of": "followup_of_id"},
                     {"specialist_id", "created_by", "proposed_by"}),
    "appointment_log": ("appointment_events", {"appt_id": "appointment_id", "user_id": "person_id"}, {"user_id"}),
    "specialist_blocks": ("specialist_blocks", {"user_id": "person_id"}, {"user_id"}),
    "onsite_requests": ("onsite_requests", {"appt_id": "appointment_id", "country": "country_code",
                                            "created_by": "created_by_person_id", "taken_by": "taken_by_person_id",
                                            "done_by": "done_by_person_id", "msg_id": "message_id"},
                        {"created_by", "taken_by", "done_by"}),
    "cab_claims": ("cab_claims", {"appt_id": "appointment_id", "user_id": "person_id", "receipt": "receipt_file_id",
                                  "decided_by": "decided_by_person_id"}, {"user_id", "decided_by"}),
    "special_requests": ("special_requests", {"delivery_date": "delivery_on", "created_by": "created_by_person_id",
                                              "taken_by": "taken_by_person_id", "handled_by": "handled_by_person_id",
                                              "ops_msg_id": "ops_message_id"},
                         {"created_by", "taken_by", "handled_by"}),
}
SMALL_TABLES = {"pay_rates", "fixed_slots", "presets"}   # read whole and kept until written
BOARD_TABLES = {"weeks", "days", "slots", "signups", "confirmations"}
# Columns that were 0/1 integers in SQLite and are booleans now.
BOOLEANS = {"closed", "sh_closed", "on_closed", "chat_posted", "handed_back", "pm_notified", "opening_posted",
            "followup_done", "after_cutoff", "urgent", "sheet_dirty"}
# jsonb columns the old code kept as JSON text: given and taken as text, so json.loads still works.
JSON_TEXT = {("presets", "config")}


class _Tables:
    """Mixed into Data: rows(), row(), add(), change(), remove() on the old table names."""

    def _spec(self, table):
        return TABLES[table]

    def _out_value(self, table, col, v):
        new, cols, people = self._spec(table)
        if col in people:
            return self.person_id(v) if v is not None else None
        if col in BOOLEANS and v is not None:
            return bool(v)
        if (table, col) in JSON_TEXT and isinstance(v, str):
            return json.loads(v)
        return v

    def _filter(self, table, col, v) -> tuple[str, str]:
        new, cols, people = self._spec(table)
        name = cols.get(col, col)
        if isinstance(v, tuple):              # ("gte", x), ("neq", x), ("not.is", None) ...
            op, val = v
            if op.endswith("is"):
                return name, f"{op}.null" if val is None else f"{op}.{str(val).lower()}"
            out = self._out_value(table, col, val)
            if out is None and col in people:
                return name, NOTHING
            return name, f"{op}.{out}"
        if isinstance(v, (list, set, frozenset)):
            vals = [x for x in (self._out_value(table, col, x) for x in v) if x is not None]
            return name, "in.(" + ",".join(f'"{x}"' for x in vals) + ")"
        if v is None:
            return name, "is.null"
        val = self._out_value(table, col, v)
        if val is None and col in people:      # nobody on identity with that Telegram id: no rows
            return name, NOTHING
        if isinstance(val, bool):
            return name, f"is.{str(val).lower()}"
        return name, f"eq.{val}"

    def _filters(self, table, filters: dict) -> dict:
        out, ands = {}, []
        for col, v in filters.items():
            if isinstance(v, tuple) and v and isinstance(v[0], tuple):   # (("gte", a), ("lt", b)) on one column
                for cond in v:
                    name, f = self._filter(table, col, cond)
                    ands.append(f"{name}.{f}")
                continue
            name, f = self._filter(table, col, v)
            out[name] = f
        if ands:
            out["and"] = "(" + ",".join(ands) + ")"
        return out

    def _back(self, table, rows: list[dict]) -> list[dict]:
        new, cols, people = self._spec(table)
        rev = {v: k for k, v in cols.items()}
        pids = {r[cols.get(c, c)] for r in rows for c in people if r.get(cols.get(c, c))}
        unknown = [p for p in pids if p not in self._tg]
        if unknown:
            self._people(id=_in(unknown))
        out = []
        for r in rows:
            o = {}
            for k, v in r.items():
                old = rev.get(k, k)
                if old in people:
                    v = self._tg.get(v) if v else None
                elif (table, old) in JSON_TEXT and v is not None and not isinstance(v, str):
                    v = json.dumps(v)
                o[old] = v
            if table == "signups":             # the name on the board is the person's, as it is now
                o["name"] = self._shown(o["user_id"])
            hook = getattr(self, "row_hooks", {}).get(table)
            out.append(hook(o) if hook else o)
        return out

    def _board_changed(self, table):
        if table in BOARD_TABLES:
            self._boards.clear()
        self._small.pop(table, None)

    def all_rows(self, table: str) -> list[dict]:
        """A small table whole (pay rates, fixed rosters, presets), kept until this module writes to it."""
        assert table in SMALL_TABLES, table
        if table not in self._small:
            self._small[table] = self.rows(table)
        return self._small[table]

    def board(self, week_id) -> dict | None:
        """A week whole, in four reads, kept until the next write to the board:
        {"week", "days": [...by idx], "slots": {day_id: [...by idx]}, "signups": {slot_id: [...by ts]},
         "slot": {slot_id: slot + the_date, day_name, week_id}, "confirmed": {telegram id}}."""
        if week_id in self._boards:
            return self._boards[week_id]
        week = self.row("weeks", id=week_id)
        if not week:
            return None
        days = self.rows("days", week_id=week_id, order="idx")
        slots = self.rows("slots", day_id=[x["id"] for x in days], order="idx") if days else []
        signups = self.rows("signups", slot_id=[s["id"] for s in slots], order="ts") if slots else []
        by_day, by_slot, slot = {x["id"]: [] for x in days}, {s["id"]: [] for s in slots}, {}
        day_of = {x["id"]: x for x in days}
        for s in slots:
            by_day[s["day_id"]].append(s)
            dd = day_of[s["day_id"]]
            slot[s["id"]] = {**s, "the_date": dd["the_date"], "day_name": dd["name"], "week_id": week_id,
                             "status": week["status"]}
        for su in signups:
            by_slot[su["slot_id"]].append(su)
        confirmed = {c["user_id"] for c in self.rows("confirmations", week_id=week_id)}
        b = {"week": week, "days": days, "slots": by_day, "signups": by_slot, "slot": slot, "confirmed": confirmed}
        self._boards[week_id] = b
        return b

    def slot_info(self, slot_id) -> dict | None:
        """A slot with its day's date and name and its week: through the board, so cheap when it's loaded."""
        for b in list(self._boards.values()):      # the Mini App's threads may clear it meanwhile
            if slot_id in b["slot"]:
                return b["slot"][slot_id]
        s = self.row("slots", id=slot_id)
        if not s:
            return None
        day = self.row("days", id=s["day_id"])
        b = self.board(day["week_id"])
        return b["slot"].get(slot_id) if b else None

    def _shown(self, tg):
        row = self._roster().get(tg)
        return row["name"] if row else ""

    def _order(self, table, order: str | None) -> dict:
        if not order:
            return {}
        new, cols, people = self._spec(table)
        parts = []
        for o in order.split(","):
            o = o.strip()
            desc = o.startswith("-")
            c = o.lstrip("-")
            parts.append(cols.get(c, c) + (".desc" if desc else ""))
        return {"order": ",".join(parts)}

    def _either(self, table, alts: list[dict]) -> str:
        """Filter dicts, each ANDed within, joined by OR: the inside of a PostgREST or=(...)."""
        items = []
        for alt in alts:
            conds = []
            for k, v in self._filters(table, alt).items():
                conds += v[1:-1].split(",") if k == "and" else [f"{k}.{v}"]
            items.append(conds[0] if len(conds) == 1 else "and(" + ",".join(conds) + ")")
        return ",".join(items)

    def rows(self, table: str, *, order: str | None = None, limit: int | None = None,
             either: list[dict] | None = None, **filters) -> list[dict]:
        """Rows matching every filter; with either=[{...}, {...}], also matching one of those."""
        rid = filters.get("id")
        if isinstance(rid, (str, int)) and not re.fullmatch(r"[0-9a-f-]{36}", str(rid)):
            return []        # not a row id (an old number on a stale button, a typo): no such row
        params = {**self._filters(table, filters), **self._order(table, order)}
        if either:
            params["or"] = "(" + self._either(table, either) + ")"
        if limit:
            params["limit"] = str(limit)
        return self._back(table, self.db.select(self._spec(table)[0], **params))

    def search(self, table: str, words: list[str], fields, *, order: str | None = None,
               limit: int | None = None) -> list[dict]:
        """Rows where every word turns up, case blind, in at least one of the fields."""
        def pattern(w):
            w = w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            w = f"*{w}*".replace("\\", "\\\\").replace('"', '\\"')
            return f'"{w}"'
        groups = ["or(" + self._either(table, [{f: ("ilike", pattern(w))} for f in fields]) + ")" for w in words]
        params = {"and": "(" + ",".join(groups) + ")", **self._order(table, order)}
        if limit:
            params["limit"] = str(limit)
        return self._back(table, self.db.select(self._spec(table)[0], **params))

    def row(self, table: str, *, order: str | None = None, **filters) -> dict | None:
        rows = self.rows(table, order=order, limit=1, **filters)
        return rows[0] if rows else None

    def by_ref(self, table: str, ref: str) -> dict | None:
        """A row by the short reference people see and type (#1a2b3c4d: the first 8 of its uuid), or by a
        whole uuid, or by an old SQLite number (#123, kept as legacy_id)."""
        ref = ref.strip().lstrip("#").lower()
        if len(ref) == 36:
            return self.row(table, id=ref)
        if len(ref) == 8 and all(c in "0123456789abcdef" for c in ref):
            rows = self.rows(table, id=(("gte", f"{ref}-0000-0000-0000-000000000000"),
                                        ("lte", f"{ref}-ffff-ffff-ffff-ffffffffffff")), limit=2)
            if len(rows) == 1:
                return rows[0]
        if ref.isdigit():
            return self.row(table, legacy_id=int(ref))
        return None

    def count(self, table: str, **filters) -> int:
        return len(self.db.select(self._spec(table)[0], select=self._count_col(table), **self._filters(table, filters)))

    def _count_col(self, table):
        new, cols, people = self._spec(table)
        return "id" if table not in ("signups", "confirmations", "fixed_slots", "presets") else \
            {"signups": "slot_id", "confirmations": "week_id", "fixed_slots": "label", "presets": "name"}[table]

    def _values(self, table, values: dict) -> dict:
        new, cols, people = self._spec(table)
        return {cols.get(k, k): self._out_value(table, k, v) for k, v in values.items() if not
                (table == "signups" and k == "name")}

    def add(self, table: str, values, *, on_conflict: str | None = None, returning: bool = True,
            ignore: bool = False):
        """Insert one row (dict) or many (list). Returns the row(s) written, in the old shape."""
        many = isinstance(values, list)
        body = [self._values(table, v) for v in values] if many else self._values(table, values)
        if on_conflict:
            new, cols, people = self._spec(table)
            on_conflict = ",".join(cols.get(c.strip(), c.strip()) for c in on_conflict.split(","))
        out = self.db.insert(self._spec(table)[0], body, on_conflict=on_conflict, returning=returning, ignore=ignore)
        self._board_changed(table)
        if not returning:
            return None
        out = self._back(table, out or [])
        return out if many else (out[0] if out else None)

    def change(self, table: str, values: dict, **filters) -> list[dict]:
        self._board_changed(table)
        return self._back(table, self.db.update(self._spec(table)[0], self._values(table, values),
                                                **self._filters(table, filters)))

    def remove(self, table: str, **filters) -> list[dict]:
        self._board_changed(table)
        return self._back(table, self.db.delete(self._spec(table)[0], **self._filters(table, filters)))


class Data(_Tables):
    def __init__(self, rest, *, clock=time.monotonic):
        self.db, self._clock = rest, clock
        self._pid: dict[int, str] = {}   # telegram id -> person id; neither changes once set
        self._tg: dict[str, int] = {}
        self._cache: tuple[float, dict] | None = None
        self._settings: tuple[float, dict] | None = None
        self._boards: dict = {}
        self._small: dict = {}
        self.row_hooks: dict = {}         # table -> fn(row) -> row, for feature code's own shaping

    # ------------------------------------------------------------------ identity
    def _people(self, **filters) -> list[dict]:
        rows = self.db.select("people", schema="identity", select=_PEOPLE_COLS, **filters)
        for p in rows:
            if p["telegram_id"] is not None:
                self._pid[p["telegram_id"]] = p["id"]
                self._tg[p["id"]] = p["telegram_id"]
        return rows

    def telegram_id(self, person_id: str | None) -> int | None:
        if person_id and person_id not in self._tg:
            self._people(id=f"eq.{person_id}")
        return self._tg.get(person_id) if person_id else None

    def person_id(self, tg: int) -> str | None:
        if tg not in self._pid:
            self._people(telegram_id=f"eq.{tg}")
        return self._pid.get(tg)

    # ------------------------------------------------------------------ rows in the old shape
    def _member_row(self, m: dict, p: dict) -> dict:
        revoked = p["status"] == "revoked"
        return {
            "user_id": p["telegram_id"], "person_id": p["id"],
            "name": p["nickname"] or p["full_name"], "full_name": p["full_name"],
            "display_name": p["nickname"] or p["full_name"], "country": p["country_code"],
            "role": m["access_level"], "status": "revoked" if revoked else m["status"],
            "active": m["active"] and not revoked, "dm_ok": m["dms_allowed"],
            "requested_at": m["requested_at"], "decided_at": m["decided_at"],
            "decided_by": self.telegram_id(m["decided_by_person_id"]), "username": None,
            "req_msgs": m["request_message_ids"], "support_name": m["support_name"],
            "on_avails": m["shows_on_avails"], "tag_calls": m["tagged_in_calls"],
            "salaried": m["salaried"], "channels": m["online_channels"], "store": m["fs_store"],
        }

    def _request_row(self, r: dict) -> dict:
        return {
            "user_id": r["telegram_id"], "person_id": r["person_id"], "name": r["given_name"],
            "full_name": r["given_name"], "display_name": r["given_name"], "country": None,
            "role": "agent", "status": "active" if r["status"] == "approved" else r["status"],
            "active": r["status"] == "pending",
            "dm_ok": True, "requested_at": r["requested_at"], "decided_at": r["decided_at"],
            "decided_by": self.telegram_id(r["decided_by_person_id"]), "username": r["telegram_username"],
            "req_msgs": r["request_message_ids"], "support_name": None, "on_avails": False,
            "tag_calls": False, "salaried": False, "channels": None, "store": None,
            "telegram_name": r["telegram_name"],
        }

    def _roster(self) -> dict[int, dict]:
        """Every member, keyed by Telegram id, with their teams. Kept for ROSTER_TTL_S, and dropped
        on any write through this module, so a change made elsewhere (a new nickname, a revoked
        person) shows within the TTL."""
        if self._cache and self._cache[0] > self._clock():
            return self._cache[1]
        ms = self.db.select("members")
        people = {p["id"]: p for p in self._people(id=_in(m["person_id"] for m in ms))} if ms else {}
        teams: dict[str, set] = {}
        for t in self.db.select("team_members", select="person_id,team"):
            teams.setdefault(t["person_id"], set()).add(t["team"])
        roster = {}
        for m in ms:
            p = people.get(m["person_id"])
            if p and p["telegram_id"] is not None:
                row = self._member_row(m, p)
                row["teams"] = teams.get(m["person_id"], set())
                roster[p["telegram_id"]] = row
        self._cache = (self._clock() + ROSTER_TTL_S, roster)
        return roster

    def _changed(self) -> None:
        self._cache = None

    def _latest_request(self, tg: int) -> dict | None:
        rows = self.db.select("join_requests", telegram_id=f"eq.{tg}", order="requested_at.desc", limit="1")
        return rows[0] if rows else None

    def agent(self, tg: int) -> dict | None:
        """Someone by Telegram id: a member, else their latest /start request, else None."""
        if not isinstance(tg, int) or isinstance(tg, bool):   # nobody (a row's empty person column)
            return None
        row = self._roster().get(tg)
        if row:
            return dict(row)
        r = self._latest_request(tg)
        return self._request_row(r) if r else None

    def agents(self, *, status: str | None = None, team: str | None = None, **flags) -> list[dict]:
        """Members (or, for pending/declined, requests) sorted by shown name. Flags use the old names."""
        roster = self._roster()
        if status in ("pending", "declined"):
            rows = [self._request_row(r) for r in
                    self.db.select("join_requests", status=f"eq.{status}", order="requested_at")]
            # someone declined once and later let in is a member, not declined
            return [r for r in rows if r["user_id"] not in roster] if status == "declined" else rows
        rows = [dict(r) for r in roster.values()
                if (status is None or r["status"] == status) and (team is None or team in r["teams"])
                and all(r[k] == v for k, v in flags.items())]
        return sorted(rows, key=lambda r: (r["name"].lower(), r["user_id"]))

    def decisions(self, limit: int) -> list[dict]:
        """The latest access decisions, newest first: each decided request, as its person is now."""
        rows = []
        for r in self.db.select("join_requests", decided_at="not.is.null", order="decided_at.desc", limit=str(limit)):
            row = self._request_row(r)
            row["status"] = "active" if r["status"] == "approved" else r["status"]
            rows.append(row)
        return rows

    def find(self, text: str) -> dict | None:
        """A member by Telegram id or by name (shown or full), case blind. No @usernames."""
        text = text.strip()
        if text.isdigit():
            return self.agent(int(text))
        want = text.lower()
        for r in self.agents():
            if want in (r["name"].lower(), r["full_name"].lower()):
                return r
        return None

    # ------------------------------------------------------------------ /start and approval
    def request_access(self, tg: int, given_name: str, *, telegram_name: str | None = None,
                       telegram_username: str | None = None) -> None:
        body = {"given_name": given_name, "telegram_name": telegram_name,
                "telegram_username": telegram_username, "requested_at": _now()}
        if self.db.update("join_requests", body, telegram_id=f"eq.{tg}", status="eq.pending"):
            return
        self.db.insert("join_requests", {"telegram_id": tg, **body})

    def rename_request(self, tg: int, given_name: str) -> None:
        self.db.update("join_requests", {"given_name": given_name}, telegram_id=f"eq.{tg}", status="eq.pending")

    def set_request_messages(self, tg: int, sent: list) -> None:
        self.db.update("join_requests", {"request_message_ids": sent}, telegram_id=f"eq.{tg}", status="eq.pending")

    def decide(self, tg: int, *, approve: bool, by: int, country: str | None = None) -> dict | None:
        """Settle a pending request. Returns it as it was, or None if there's no pending one."""
        pending = self.db.select("join_requests", telegram_id=f"eq.{tg}", status="eq.pending")
        if not pending:
            return None
        r, at, decider = pending[0], _now(), self.person_id(by)
        if not approve:
            self.db.update("join_requests", {"status": "declined", "decided_at": at,
                                             "decided_by_person_id": decider}, id=f"eq.{r['id']}")
            return self._request_row(r)
        pid = self.db.rpc("add_person", {"p_telegram_id": tg, "p_full_name": r["given_name"],
                                         "p_nickname": None, "p_country_code": country})
        self._pid[tg] = pid
        self.add_member(pid, requested_at=r["requested_at"], decided_at=at, decided_by_person_id=decider,
                        request_message_ids=r["request_message_ids"], dms_allowed=True)
        self.db.update("join_requests", {"status": "approved", "person_id": pid, "decided_at": at,
                                         "decided_by_person_id": decider}, id=f"eq.{r['id']}")
        return self._request_row(r)

    # ------------------------------------------------------------------ members and teams
    def add_member(self, person_id: str, **fields) -> None:
        self.db.insert("members", {"person_id": person_id, **fields}, on_conflict="person_id")
        self._changed()

    def set_agent(self, tg: int, **fields) -> None:
        pid = self.person_id(tg)
        if pid:
            self.db.update("members", {_MEMBER_FIELDS[k]: v for k, v in fields.items()}, person_id=f"eq.{pid}")
            self._changed()

    def teams(self, tg: int) -> set[str]:
        row = self._roster().get(tg)
        return set(row["teams"]) if row else set()

    def set_teams(self, tg: int, teams) -> None:
        pid = self.person_id(tg)
        if not pid:
            return
        self.db.delete("team_members", person_id=f"eq.{pid}")
        if teams:
            self.db.insert("team_members", [{"person_id": pid, "team": t} for t in teams])
        self._changed()

    # ------------------------------------------------------------------ settings
    def _all_settings(self) -> dict[str, str]:
        if not (self._settings and self._settings[0] > self._clock()):
            rows = self.db.select("settings", select="key,value")
            self._settings = (self._clock() + ROSTER_TTL_S, {r["key"]: r["value"] for r in rows})
        return self._settings[1]

    def setting(self, key: str, default: str = "") -> str:
        return self._all_settings().get(key) or default

    def settings_like(self, prefix: str) -> dict[str, str]:
        return {k: v for k, v in self._all_settings().items() if k.startswith(prefix)}

    def set_setting(self, key: str, value: str) -> None:
        self.db.insert("settings", {"key": key, "value": value}, on_conflict="key")
        self._settings = None

    def del_setting(self, key: str) -> None:
        self.db.delete("settings", key=f"eq.{key}")
        self._settings = None

    # ------------------------------------------------------------------ the board
    def _week_slot_ids(self, week_id: str) -> list[str]:
        days = [r["id"] for r in self.db.select("days", select="id", week_id=f"eq.{week_id}")]
        if not days:
            return []
        return [r["id"] for r in self.db.select("slots", select="id", day_id=_in(days))]

    def free_week(self, tg: int, week_id: str) -> int:
        """Drop someone's signups and confirmation for a week. Returns the slots freed."""
        pid = self.person_id(tg)
        slots = self._week_slot_ids(week_id)
        if not pid:
            return 0
        freed = self.db.delete("signups", person_id=f"eq.{pid}", slot_id=_in(slots)) if slots else []
        self.db.delete("confirmations", person_id=f"eq.{pid}", week_id=f"eq.{week_id}")
        self._boards.clear()
        return len(freed)
