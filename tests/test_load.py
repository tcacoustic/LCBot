"""The SQLite-to-warehouse load (load/lcload.py) on a made-up snapshot, into the live test database.

Run: LCBOT_LIVE_DB=1 python -m unittest discover tests  (with requirements.txt and load/requirements.txt)
"""
import csv
import os
import sqlite3
import sys
import tempfile
import unittest

from live import JWT_SECRET, PG, PGRST_URL, LiveTestCase, sql

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "load"))
try:
    import psycopg  # noqa: F401
    import telegram  # noqa: F401
    import lcload
except ImportError:
    lcload = None

STAFF, NEWBIE, LEAVER, DECLINED, PENDING, ADMIN = 990000001, 990000002, 990000003, 990000004, 990000005, 990000099
T = "2026-11-02T10:00:00+08:00"


def snapshot(path: str) -> None:
    db = sqlite3.connect(path)
    with open(os.path.join(ROOT, "tests", "fixtures", "sqlite_schema.sql")) as f:
        db.executescript(f.read())
    agents = [
        (STAFF, "Ann Tan", "ann", 1, 1, "admin", "active", "Ann", T, ADMIN, T, "[11, 12]", None, 1, 1, 0, "[\"SHOPEE_SG\"]", "SG", None),
        (NEWBIE, "Ben Lim", None, 1, 1, "agent", "active", "Benny", T, ADMIN, T, None, None, 1, 0, 1, None, "MY", None),
        (LEAVER, "Cal Ng", None, 0, 0, "agent", "declined", None, T, ADMIN, T, None, None, 1, 1, 0, None, "SG", None),
        (DECLINED, "Dee Ong", "dee", 1, 0, "agent", "declined", "Dee", T, ADMIN, "2026-11-03 09:00:00", None, None, 1, 1, 0, None, None, None),
        (PENDING, "Eve Koh", "eve", 1, 1, "agent", "pending", "Eve", T, None, None, "[5]", None, 1, 1, 0, None, None, None),
    ]
    db.executemany("insert into agents values (" + ",".join("?" * 19) + ")", agents)
    db.executemany("insert into memberships values (?,?,?)",
                   [(STAFF, "LIVECHAT", T), (STAFF, "ON", T), (LEAVER, "LIVECHAT", T)])
    db.execute("insert into weeks values (7, 'Wk 45', '2026-11-02', '2026-11-08', null, '2026-10-31 18:00:00', 'open', 501)")
    db.execute("insert into days values (70, 7, 0, 'MON', '2026-11-02', 601)")
    db.executemany("insert into slots values (?,?,?,?,?,?)", [(700, 70, 0, "10am-12pm", 600, 2), (701, 70, 1, "12pm-2pm", 720, 2)])
    db.executemany("insert into signups values (?,?,?,?)", [(700, STAFF, "Ann", T), (701, LEAVER, "Cal", T)])
    db.execute("insert into confirmations values (7, ?, ?)", (STAFF, T))
    db.execute("insert into time_entries (id, agent_id, slot_id, the_date, clock_in, clock_out, status, source) "
               "values (40, ?, 700, '2026-11-02', '2026-11-02T10:00:00+08:00', '2026-11-02T12:30:00+08:00', 'closed', 'bot')", (STAFF,))
    db.execute("insert into time_entries (id, agent_id, slot_id, the_date, clock_in, status) "
               "values (41, 123456789, null, '2026-11-02', '2026-11-02T10:00:00+08:00', 'open')")   # someone nobody knows
    db.execute("insert into ot_claims values (50, 40, ?, '2026-11-02 12:30:00', 30, 'approved', ?, ?, ?, null)", (STAFF, T, ADMIN, T))
    db.execute("insert into pay_rates values (60, ?, 1200, '2026-01-01', ?, ?)", (STAFF, ADMIN, T))
    db.execute("insert into channel_posts (id, channel, chat_id, agent_id, the_date, created_at, kind) "
               "values (80, 'SHOPEE_SG', -1001, ?, '2026-11-02', ?, 'cases')", (STAFF, T))
    db.execute("insert into ho_cases (id, agent_id, the_date, section, platform, store, closed, created_at, post_id, channel) "
               "values (90, ?, '2026-11-02', 'open', 'DUOKE', 'SGLAZADASONOS', 0, ?, 80, null)", (STAFF,  T))
    db.execute("insert into case_log values (91, 90, ?, 'SH', 'note', 'x', ?)", (STAFF, T))
    appt = ("insert into appointments (id, country, specialist_id, visit_type, starts_at, customer_name, address, "
            "status, created_by, created_at, followup_of) values (?, 'MY', ?, 'repair', ?, 'Cust', 'Addr', 'booked', ?, ?, ?)")
    db.execute(appt, (30, NEWBIE, "2026-11-05 14:00:00", STAFF, T, None))
    db.execute(appt, (29, NEWBIE, "2026-11-12 14:00:00", STAFF, T, 30))   # a follow-up with a lower id
    db.execute("insert into appointment_log values (31, 30, ?, 'booked', null, ?)", (STAFF, T))
    db.execute("insert into presets values ('custom', '[{\"d\": 1}]')")
    db.executemany("insert into settings values (?,?)", [
        ("pm_alerts", "on"), ("case_pick:90", f"help:{STAFF}"), ("visit_pin:30", "x"), ("memberships_moved", "1"),
        ("payphoto_wait:-55", "30"), ("case_pick:999", "gone")])
    db.commit()
    db.close()


class SnapshotCase(LiveTestCase):
    """A made-up snapshot and merge list, with two people already on identity."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "snap.db")
        snapshot(self.db_path)
        self.people = os.path.join(self.dir, "people.csv")
        with open(self.people, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["telegram_id", "full_name", "nickname", "country_code"])
            w.writerow([STAFF, "", "Annie", ""])
            w.writerow([NEWBIE, "Benjamin Lim", "", "MY"])
        self.person(STAFF, "Ann Tan Hui", nickname="Ann T")
        self.person(ADMIN, "The Owner")

    def load(self):
        return lcload.load(PG, self.db_path, "lc_bot_dev", self.people, apply=True)

    def one(self, q):
        return sql(q)


@unittest.skipIf(lcload is None, "needs load/requirements.txt and the bot's packages")
class LoadTest(SnapshotCase):
    def test_the_counts_balance_and_name_no_one(self):
        r = self.load()
        self.assertTrue(r.balanced())
        text = "\n".join(r.lines())
        for secret in ("Ann", "Ben", "Cal", str(STAFF), "Cust"):
            self.assertNotIn(secret, text)
        self.assertEqual(r.people["matched"], 2)
        self.assertEqual(r.people["created active"], 1)
        self.assertEqual(r.people["created revoked"], 1)
        self.assertEqual(r.people["skipped: no name in the bot or the merge list"], 1)
        self.assertEqual(r.skips["time_entries"], {"agent_id: not on identity": 1})

    def test_a_second_run_changes_nothing(self):
        self.load()
        r = self.load()
        self.assertEqual(r.changes(), 0)
        self.assertTrue(all(c["new"] == c["changed"] == 0 for c in r.tables.values()))

    def test_a_dry_run_writes_nothing(self):
        lcload.load(PG, self.db_path, "lc_bot_dev", self.people, apply=False)
        self.assertEqual(self.one("select count(*) from lc_bot_dev.weeks"), "0")
        self.assertEqual(self.one(f"select count(*) from identity.people where telegram_id = {NEWBIE}"), "0")

    def test_people(self):
        self.load()
        self.assertEqual(self.one("select concat_ws('|', full_name, coalesce(nickname, '-'), country_code, person_type, "
                                  f"status) from identity.people where telegram_id = {NEWBIE}"),
                         "Benjamin Lim|-|MY|freelancer|active")   # created from the merge list
        self.assertEqual(self.one(f"select full_name || '|' || nickname from identity.people where telegram_id = {STAFF}"),
                         "Ann Tan Hui|Annie")   # matched: keeps their name, takes the approved nickname
        self.assertEqual(self.one(f"select status from identity.people where telegram_id = {LEAVER}"), "revoked")
        self.assertEqual(self.one(f"select count(*) from identity.people where telegram_id in ({DECLINED}, {PENDING})"), "0")

    def test_members_teams_and_requests(self):
        self.load()
        self.assertEqual(self.one(
            "select string_agg(p.telegram_id || ':' || m.access_level || ':' || m.status || ':' || m.active, ',' "
            "order by p.telegram_id) from lc_bot_dev.members m join identity.people p on p.id = m.person_id"),
            f"{STAFF}:admin:active:true,{NEWBIE}:agent:active:true,{LEAVER}:agent:declined:false")
        self.assertEqual(self.one("select online_channels::text || request_message_ids::text from lc_bot_dev.members m "
                                  f"join identity.people p on p.id = m.person_id where p.telegram_id = {STAFF}"),
                         '["SHOPEE_SG"][11, 12]')
        self.assertEqual(self.one("select string_agg(team, ',' order by team) from lc_bot_dev.team_members"), "LIVECHAT,LIVECHAT,ON")
        self.assertEqual(self.one("select string_agg(telegram_id || ':' || status, ',' order by telegram_id) "
                                  "from lc_bot_dev.join_requests"),
                         f"{STAFF}:approved,{NEWBIE}:approved,{LEAVER}:approved,{DECLINED}:declined,{PENDING}:pending")

    def test_times_links_and_settings(self):
        self.load()
        # naive times are Singapore's, and the visit country's for appointments
        self.assertEqual(self.one("select clock_in_at at time zone 'UTC' from lc_bot_dev.time_entries"), "2026-11-02 02:00:00")
        self.assertEqual(self.one("select starts_at at time zone 'UTC' from lc_bot_dev.appointments where legacy_id = 30"),
                         "2026-11-05 06:00:00")
        self.assertEqual(self.one("select f.legacy_id from lc_bot_dev.appointments a join lc_bot_dev.appointments f "
                                  "on f.id = a.followup_of_id where a.legacy_id = 29"), "30")
        self.assertEqual(self.one("select count(*) from lc_bot_dev.signups s join lc_bot_dev.slots sl on sl.id = s.slot_id "
                                  "join lc_bot_dev.days d on d.id = sl.day_id join lc_bot_dev.weeks w on w.id = d.week_id "
                                  "where w.legacy_id = 7"), "2")
        self.assertEqual(self.one("select online_channel from lc_bot_dev.handover_cases"), "LAZADA_SG")
        self.assertEqual(self.one("select config::text from lc_bot_dev.presets where name = 'custom'"), '[{"d": 1}]')
        case = self.one("select id from lc_bot_dev.handover_cases")
        appt = self.one("select id from lc_bot_dev.appointments where legacy_id = 30")
        self.assertEqual(self.one("select string_agg(key || '=' || value, ',' order by key) from lc_bot_dev.settings"),
                         ",".join(sorted([f"case_pick:{case}=help:{STAFF}", "pm_alerts=on", f"visit_pin:{appt}=x",
                                          f"payphoto_wait:-55={appt}"])))


OLD_CODE = os.environ.get("LCBOT_OLD_CODE")   # a checkout of the bot before the move (staging d925687)


@unittest.skipIf(lcload is None or not OLD_CODE, "needs LCBOT_OLD_CODE and load/requirements.txt")
class ParityTest(SnapshotCase):
    def parity(self, expected=()):
        import parity
        path = os.path.join(self.dir, "expected.txt")
        with open(path, "w") as f:
            f.write("\n".join(expected) + "\n")
        os.environ.update({"ADMIN_IDS": str(ADMIN), "PARITY_URL": PGRST_URL, "PARITY_REST_PATH": "/",
                           "PARITY_JWT_SECRET": JWT_SECRET})
        return parity.main([self.db_path, "--schema", "lc_bot_dev", "--old-code", OLD_CODE, "--expected", path])

    def test_old_and_new_answer_the_same_once_the_known_gaps_are_listed(self):
        self.load()
        self.assertEqual(self.parity(), 1)     # the unknown person's shift isn't loaded
        self.assertEqual(self.parity(["sheet:123456789:*"]), 0)

    def test_a_wrong_row_fails_the_check(self):
        self.load()
        sql("delete from lc_bot_dev.signups where slot_id = (select id from lc_bot_dev.slots where legacy_id = 700)")
        self.assertEqual(self.parity(["sheet:123456789:*"]), 1)


if __name__ == "__main__":
    unittest.main()
