"""The bot's data module: the only code that reaches the database, through rest.RestClient.

Feature code speaks Telegram ids and gets rows shaped like the old SQLite ones (`user_id`, `name`,
`role`, `dm_ok`, ...). Underneath, people are `identity.people` (read only; the bot adds someone
only through its schema's `add_person`), membership is `members`, and a /start that isn't yet a
person is a `join_requests` row.
"""
from __future__ import annotations

import datetime

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
    def __init__(self, rest):
        self.db = rest
        self._pid: dict[int, str] = {}   # telegram id -> person id; neither changes once set

    # ------------------------------------------------------------------ identity
    def _people(self, **filters) -> list[dict]:
        rows = self.db.select("people", schema="identity", select=_PEOPLE_COLS, **filters)
        for p in rows:
            if p["telegram_id"] is not None:
                self._pid[p["telegram_id"]] = p["id"]
        return rows

    def person_id(self, tg: int) -> str | None:
        if tg not in self._pid:
            self._people(telegram_id=f"eq.{tg}")
        return self._pid.get(tg)

    # ------------------------------------------------------------------ rows in the old shape
    @staticmethod
    def _member_row(m: dict, p: dict) -> dict:
        revoked = p["status"] == "revoked"
        return {
            "user_id": p["telegram_id"], "person_id": p["id"],
            "name": p["nickname"] or p["full_name"], "full_name": p["full_name"],
            "display_name": p["nickname"] or p["full_name"], "country": p["country_code"],
            "role": m["access_level"], "status": "revoked" if revoked else m["status"],
            "active": m["active"] and not revoked, "dm_ok": m["dms_allowed"],
            "requested_at": m["requested_at"], "decided_at": m["decided_at"],
            "req_msgs": m["request_message_ids"], "support_name": m["support_name"],
            "on_avails": m["shows_on_avails"], "tag_calls": m["tagged_in_calls"],
            "salaried": m["salaried"], "channels": m["online_channels"], "store": m["fs_store"],
        }

    @staticmethod
    def _request_row(r: dict) -> dict:
        return {
            "user_id": r["telegram_id"], "person_id": r["person_id"], "name": r["given_name"],
            "full_name": r["given_name"], "display_name": r["given_name"], "country": None,
            "role": "agent", "status": r["status"], "active": r["status"] == "pending",
            "dm_ok": True, "requested_at": r["requested_at"], "decided_at": r["decided_at"],
            "req_msgs": r["request_message_ids"], "support_name": None, "on_avails": False,
            "tag_calls": False, "salaried": False, "channels": None, "store": None,
            "telegram_name": r["telegram_name"],
        }

    def _members(self, people: list[dict], **filters) -> list[dict]:
        if not people:
            return []
        by_id = {p["id"]: p for p in people}
        ms = self.db.select("members", person_id=_in(by_id), **filters)
        return [self._member_row(m, by_id[m["person_id"]]) for m in ms]

    def _latest_request(self, tg: int) -> dict | None:
        rows = self.db.select("join_requests", telegram_id=f"eq.{tg}", order="requested_at.desc", limit="1")
        return rows[0] if rows else None

    def agent(self, tg: int) -> dict | None:
        """Someone by Telegram id: a member, else their latest /start request, else None."""
        people = self._people(telegram_id=f"eq.{tg}")
        rows = self._members(people)
        if rows:
            return rows[0]
        r = self._latest_request(tg)
        return self._request_row(r) if r else None

    def agents(self, *, status: str | None = None, team: str | None = None, **flags) -> list[dict]:
        """Members (or, for pending/declined, requests) sorted by shown name. Flags use the old names."""
        if status in ("pending", "declined"):
            rows = [self._request_row(r) for r in
                    self.db.select("join_requests", status=f"eq.{status}", order="requested_at")]
            if status == "declined":   # someone declined once and later let in is a member, not declined
                members = {m["user_id"] for m in self.agents()}
                rows = [r for r in rows if r["user_id"] not in members]
            return rows
        filters = {_MEMBER_FIELDS[k]: f"is.{str(v).lower()}" if isinstance(v, bool) else f"eq.{v}"
                   for k, v in flags.items()}
        if status and status != "revoked":
            filters["status"] = f"eq.{status}"
        if team:
            ids = [t["person_id"] for t in self.db.select("team_members", select="person_id", team=f"eq.{team}")]
            if not ids:
                return []
            filters["person_id"] = _in(ids)
        ms = self.db.select("members", **filters)
        if not ms:
            return []
        people = {p["id"]: p for p in self._people(id=_in(m["person_id"] for m in ms))}
        rows = [self._member_row(m, people[m["person_id"]]) for m in ms]
        if status == "active":
            rows = [r for r in rows if r["status"] == "active"]
        elif status == "revoked":
            rows = [r for r in rows if r["status"] == "revoked"]
        return sorted(rows, key=lambda r: (r["name"].lower(), r["user_id"]))

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

    def set_agent(self, tg: int, **fields) -> None:
        pid = self.person_id(tg)
        if pid:
            self.db.update("members", {_MEMBER_FIELDS[k]: v for k, v in fields.items()}, person_id=f"eq.{pid}")

    def teams(self, tg: int) -> set[str]:
        pid = self.person_id(tg)
        if not pid:
            return set()
        return {t["team"] for t in self.db.select("team_members", select="team", person_id=f"eq.{pid}")}

    def set_teams(self, tg: int, teams) -> None:
        pid = self.person_id(tg)
        if not pid:
            return
        self.db.delete("team_members", person_id=f"eq.{pid}")
        if teams:
            self.db.insert("team_members", [{"person_id": pid, "team": t} for t in teams])
