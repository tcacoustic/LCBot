"""The bot's data module: the only code that reaches the database, through rest.RestClient.

Feature code speaks Telegram ids and gets rows shaped like the old SQLite ones (`user_id`, `name`,
`role`, `dm_ok`, ...). Underneath, people are `identity.people` (read only; the bot adds someone
only through its schema's `add_person`), membership is `members`, and a /start that isn't yet a
person is a `join_requests` row.
"""
from __future__ import annotations

import datetime
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


def _in(values) -> str:
    return "in.(" + ",".join(str(v) for v in values) + ")"


class Data:
    def __init__(self, rest, *, clock=time.monotonic):
        self.db, self._clock = rest, clock
        self._pid: dict[int, str] = {}   # telegram id -> person id; neither changes once set
        self._tg: dict[str, int] = {}
        self._cache: tuple[float, dict] | None = None
        self._settings: tuple[float, dict] | None = None

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

    def decide(self, tg: int, *, approve: bool, by: int) -> dict | None:
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
                                         "p_nickname": None, "p_country_code": None})
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
        return len(freed)
