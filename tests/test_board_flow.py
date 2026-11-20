"""The board's own code (core.py) on the live test database: fixed rosters, stats, rendering, runs.

Run: LCBOT_LIVE_DB=1 python -m unittest discover tests  (with requirements.txt installed)
"""
import unittest

import botcode
from live import LiveTestCase

core = botcode.load()


@unittest.skipIf(core is None, "needs LCBOT_LIVE_DB=1 and the bot's packages")
class BoardFlowTest(LiveTestCase):
    def setUp(self):
        super().setUp()
        self.data = core.data
        self.data._pid.clear()            # each test makes its people afresh, with new ids
        self.data._tg.clear()
        self.data._changed()
        self.data._settings = None
        self.data._boards.clear()
        self.data._small.clear()
        for tg, name in ((990000001, "Ann Tan"), (990000002, "Ben Lim")):
            self.data.add_member(self.person(tg, name))
            core.set_depts(tg, ["LIVECHAT"])
        w = self.data.add("weeks", {"label": "W48", "start_date": "2026-11-23", "end_date": "2026-11-29",
                                    "deadline": "2026-11-21T18:00:00+08:00", "status": "open"})
        self.week = w["id"]
        days = self.data.add("days", [{"week_id": self.week, "idx": 0, "name": "MON", "the_date": "2026-11-23"},
                                      {"week_id": self.week, "idx": 1, "name": "TUE", "the_date": "2026-11-24"}])
        self.slots = self.data.add("slots", [
            {"day_id": days[0]["id"], "idx": 0, "label": "10am-12pm", "start_min": 600},
            {"day_id": days[0]["id"], "idx": 1, "label": "12pm-2pm", "start_min": 720},
            {"day_id": days[1]["id"], "idx": 0, "label": "10am-12pm", "start_min": 600}])

    def test_fixed_rosters_fill_a_new_week_once(self):
        self.data.add("fixed_slots", {"user_id": 990000001, "day_name": "MON", "label": "10AM-12PM"})
        self.assertEqual(core.apply_fixed_slots(self.week), 1)
        self.assertEqual(core.apply_fixed_slots(self.week), 0)
        self.assertEqual([h["name"] for h in core.slot_holders(self.slots[0]["id"])], ["Ann Tan"])

    def test_stats_render_and_confirm(self):
        core.give_slot(self.slots[0]["id"], 990000001)
        self.data.add("confirmations", {"week_id": self.week, "user_id": 990000001, "ts": core.now().isoformat()})
        st = core.week_stats(self.week)
        self.assertEqual(st["confirmed"], {990000001})
        self.assertEqual([a["user_id"] for a in st["missing"]], [990000002])
        self.assertEqual(len(st["gaps"]), 2)
        text, _ = core.render_board(self.week)
        self.assertIn("10am-12pm: Ann Tan", text)
        self.assertIn('tg://user?id=990000002', text)
        self.assertTrue(core.is_locked(self.week, 990000001))

    def test_a_run_of_back_to_back_slots_and_the_slot_a_clock_in_belongs_to(self):
        core.give_slot(self.slots[0]["id"], 990000002)
        core.give_slot(self.slots[1]["id"], 990000002)
        self.assertEqual([s["label"] for s in core.slot_run_from(self.slots[0]["id"])], ["10am-12pm", "12pm-2pm"])
        when = core.datetime(2026, 11, 23, 12, 5, tzinfo=core.TZ)
        self.assertEqual(core.current_slot_for(990000002, when)["label"], "12pm-2pm")
        self.assertEqual([s["label"] for s in core.week_slots(self.week, since="2026-11-24")], ["10am-12pm"])
        self.assertEqual(core.find_slot(self.week, "TUE", "10AM-12PM")["id"], self.slots[2]["id"])

    def test_the_week_finders(self):
        self.assertEqual(core.open_week()["id"], self.week)
        self.assertEqual(core.week_containing(core.date(2026, 11, 25))["id"], self.week)
        self.assertEqual(core.day_row_for("2026-11-24")["name"], "TUE")

    def test_people_helpers(self):
        self.assertTrue(core.has_access(990000001))
        self.assertFalse(core.is_admin(990000001))
        self.data.set_agent(990000001, role="admin")
        self.assertTrue(core.is_admin(990000001))
        self.assertIn(990000001, core.admin_ids())
        self.assertEqual(core.depts_of(990000002), {"LIVECHAT"})
        core.set_depts(990000002, ["ON"])
        self.assertFalse(self.data.agent(990000002)["on_avails"])
        self.assertEqual(core.display_name_of(990000002), "Ben Lim")


@unittest.skipIf(core is None, "needs LCBOT_LIVE_DB=1 and the bot's packages")
class TimeclockFlowTest(BoardFlowTest):
    def test_rates_their_own_else_the_teams_by_date(self):
        now = core.now().isoformat()
        self.data.add("pay_rates", [
            {"agent_id": None, "cents": 1000, "effective_from": "2026-01-01", "created_at": now},
            {"agent_id": 990000001, "cents": 1500, "effective_from": "2026-11-01", "created_at": now}])
        self.assertEqual(core.rate_for(990000001, core.date(2026, 11, 23)), 1500)
        self.assertEqual(core.rate_for(990000001, core.date(2026, 10, 1)), 1000)
        self.assertEqual(core.rate_for(990000002, core.date(2026, 11, 23)), 1000)

    def test_a_timesheet_pays_a_rostered_block(self):
        core.give_slot(self.slots[0]["id"], 990000001)
        self.data.add("time_entries", {"agent_id": 990000001, "slot_id": self.slots[0]["id"],
                                       "the_date": "2026-11-23", "clock_in": "2026-11-23T10:03:00+08:00",
                                       "clock_out": "2026-11-23T12:00:00+08:00", "status": "closed"})
        t = core.timesheet(990000001, core.date(2026, 11, 1), core.date(2026, 11, 30))
        self.assertEqual(len(t["shifts"]), 1)
        self.assertEqual(t["shifts"][0]["minutes"], 120)
        self.assertFalse(core.data.row("time_entries", clock_out=None))
