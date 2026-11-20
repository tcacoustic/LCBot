"""The old table names and columns over the new tables, live against lc_bot_dev.

Run: LCBOT_LIVE_DB=1 python -m unittest discover tests
"""
from data import Data
from live import LiveTestCase, sql


class TablesTest(LiveTestCase):
    def setUp(self):
        super().setUp()
        self.data = Data(self.db)
        self.data.add_member(self.person(990000001, "Tan Jia En", nickname="Jia"))
        self.week = self.data.add("weeks", {"label": "W1", "start_date": "2026-11-23", "end_date": "2026-11-29",
                                            "deadline": "2026-11-21T18:00:00+08:00", "status": "open"})
        self.day = self.data.add("days", {"week_id": self.week["id"], "idx": 0, "name": "MON", "the_date": "2026-11-23"})
        self.slot = self.data.add("slots", {"day_id": self.day["id"], "idx": 0, "label": "9-1", "start_min": 540})

    def test_rows_come_back_under_the_old_column_names(self):
        w = self.data.row("weeks", id=self.week["id"])
        self.assertEqual((w["start_date"], w["end_date"], w["status"]), ("2026-11-23", "2026-11-29", "open"))
        self.assertEqual(self.data.row("days", week_id=self.week["id"])["the_date"], "2026-11-23")

    def test_a_signup_is_by_telegram_id_and_reads_back_with_the_shown_name(self):
        self.data.add("signups", {"slot_id": self.slot["id"], "user_id": 990000001, "name": "ignored",
                                  "ts": "2026-11-20T10:00:00+08:00"})
        s = self.data.rows("signups", slot_id=self.slot["id"], order="ts")
        self.assertEqual([(r["user_id"], r["name"]) for r in s], [(990000001, "Jia")])
        self.assertEqual(sql("select count(*) from lc_bot_dev.signups s join identity.people p on p.id = s.person_id "
                             "where p.telegram_id = 990000001"), "1")

    def test_filters_lists_ranges_nulls_and_order(self):
        d2 = self.data.add("days", {"week_id": self.week["id"], "idx": 1, "name": "TUE", "the_date": "2026-11-24"})
        self.assertEqual([d["name"] for d in self.data.rows("days", week_id=self.week["id"], order="-idx")], ["TUE", "MON"])
        self.assertEqual([d["name"] for d in self.data.rows("days", id=[self.day["id"], d2["id"]], order="idx")],
                         ["MON", "TUE"])
        self.assertEqual([d["name"] for d in self.data.rows("days", the_date=(("gte", "2026-11-24"), ("lte", "2026-11-30")))],
                         ["TUE"])
        self.assertEqual(len(self.data.rows("days", msg_id=None)), 2)
        self.assertEqual(self.data.count("days", week_id=self.week["id"]), 2)

    def test_booleans_take_old_zero_and_one(self):
        e = self.data.add("time_entries", {"agent_id": 990000001, "the_date": "2026-11-23",
                                           "clock_in": "2026-11-23T09:00:00+08:00", "opening_posted": 0})
        self.data.change("time_entries", {"opening_posted": 1}, id=e["id"])
        self.assertEqual(self.data.rows("time_entries", opening_posted=1)[0]["agent_id"], 990000001)
        self.assertEqual(self.data.rows("time_entries", clock_out=None)[0]["clock_in"], "2026-11-23T09:00:00+08:00")

    def test_preset_config_is_json_text_both_ways(self):
        self.data.add("presets", {"name": "std", "config": '{"MON": ["9-1"]}'}, on_conflict="name")
        self.data.add("presets", {"name": "std", "config": '{"MON": ["1-5"]}'}, on_conflict="name")
        self.assertEqual(self.data.row("presets", name="std")["config"], '{"MON": ["1-5"]}')

    def test_remove_returns_what_went(self):
        self.data.add("confirmations", {"week_id": self.week["id"], "user_id": 990000001,
                                        "ts": "2026-11-20T10:00:00+08:00"})
        gone = self.data.remove("confirmations", week_id=self.week["id"], user_id=990000001)
        self.assertEqual([g["user_id"] for g in gone], [990000001])

    def test_free_week_drops_signups_and_confirmation(self):
        self.data.add("signups", {"slot_id": self.slot["id"], "user_id": 990000001, "ts": "2026-11-20T10:00:00+08:00"})
        self.data.add("confirmations", {"week_id": self.week["id"], "user_id": 990000001, "ts": "2026-11-20T10:00:00+08:00"})
        self.assertEqual(self.data.free_week(990000001, self.week["id"]), 1)
        self.assertEqual(self.data.count("confirmations", week_id=self.week["id"]), 0)

    def test_the_board_loads_a_week_and_a_write_reloads_it(self):
        b = self.data.board(self.week["id"])
        self.assertEqual([d["name"] for d in b["days"]], ["MON"])
        self.assertEqual(b["signups"][self.slot["id"]], [])
        self.data.add("signups", {"slot_id": self.slot["id"], "user_id": 990000001, "ts": "2026-11-20T10:00:00+08:00"})
        b = self.data.board(self.week["id"])
        self.assertEqual([s["name"] for s in b["signups"][self.slot["id"]]], ["Jia"])
        info = self.data.slot_info(self.slot["id"])
        self.assertEqual((info["the_date"], info["day_name"], info["week_id"]), ("2026-11-23", "MON", self.week["id"]))

    def test_insert_or_ignore_keeps_the_first(self):
        first = self.data.add("signups", {"slot_id": self.slot["id"], "user_id": 990000001, "ts": "2026-11-20T10:00:00+08:00"},
                              on_conflict="slot_id,user_id", ignore=True)
        again = self.data.add("signups", {"slot_id": self.slot["id"], "user_id": 990000001, "ts": "2026-11-21T10:00:00+08:00"},
                              on_conflict="slot_id,user_id", ignore=True)
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertEqual(self.data.rows("signups")[0]["ts"], "2026-11-20T10:00:00+08:00")
