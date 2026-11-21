"""Owner commands and startup in bot.py, on the live test database: the board and payroll exports, the
shift call, and seeding the built-in presets.

Run: LCBOT_LIVE_DB=1 python -m unittest discover tests  (with requirements.txt installed)
"""
import asyncio
import csv
import io
import unittest
from types import SimpleNamespace

import botcode
from live import LiveTestCase

core = botcode.load()
bot = __import__("bot") if core else None

OWNER = 990000099


class Message:
    def __init__(self):
        self.texts, self.files = [], []

    async def reply_text(self, text, **kw):
        self.texts.append(text)

    async def reply_document(self, doc, **kw):
        self.files.append((kw.get("filename"), doc.getvalue().decode()))


def owner_update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=OWNER), message=Message())


@unittest.skipIf(core is None, "needs LCBOT_LIVE_DB=1 and the bot's packages")
class BotCommandsTest(LiveTestCase):
    def setUp(self):
        super().setUp()
        self.data = core.data
        self.data._pid.clear()
        self.data._tg.clear()
        self.data._changed()
        self.data._settings = None
        self.data._boards.clear()
        self.data._small.clear()
        self.data.add_member(self.person(990000001, "Ann Tan"))
        core.set_depts(990000001, ["LIVECHAT"])
        today = core.now().date()
        w = self.data.add("weeks", {"label": "This Week", "start_date": today.isoformat(),
                                    "end_date": (today + core.timedelta(days=6)).isoformat(),
                                    "deadline": core.now().isoformat(), "status": "open"})
        self.week = w["id"]
        self.tomorrow = today + core.timedelta(days=1)
        days = self.data.add("days", [{"week_id": self.week, "idx": 0, "name": "MON", "the_date": today.isoformat()},
                                      {"week_id": self.week, "idx": 1, "name": "TUE",
                                       "the_date": self.tomorrow.isoformat()}])
        self.slots = self.data.add("slots", [
            {"day_id": days[1]["id"], "idx": 0, "label": "10am-12pm", "start_min": 600},
            {"day_id": days[1]["id"], "idx": 1, "label": "12pm-2pm", "start_min": 720}])

    def test_export_writes_every_slot_with_its_holders(self):
        core.give_slot(self.slots[0]["id"], 990000001)
        u = owner_update()
        asyncio.run(bot.cmd_export(u, SimpleNamespace(args=[])))
        [(name, body)] = u.message.files
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(name, "This_Week_avails.csv")
        self.assertEqual(rows[1:], [["TUE", self.tomorrow.isoformat(), "10am-12pm", "Ann Tan"],
                                    ["TUE", self.tomorrow.isoformat(), "12pm-2pm", ""]])

    def test_payroll_lists_the_months_shifts(self):
        start = core.now().replace(day=1, hour=10, minute=0, second=0, microsecond=0)
        e = self.data.add("time_entries", {"agent_id": 990000001, "slot_id": None, "the_date": start.date().isoformat(),
                                           "clock_in": start.isoformat(),
                                           "clock_out": (start + core.timedelta(hours=2)).isoformat(),
                                           "status": "closed"})
        u = owner_update()
        asyncio.run(bot.cmd_payroll(u, SimpleNamespace(args=[])))
        [(_, body)] = u.message.files
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual([(r[0], r[1], r[2]) for r in rows[1:]], [(e["id"], "Ann Tan", "990000001")])

    def test_the_shift_call_tags_tomorrows_people_unless_they_opted_out(self):
        core.give_slot(self.slots[0]["id"], 990000001)
        old = bot.SHIFTCALL_CHAT_ID
        bot.SHIFTCALL_CHAT_ID = -100
        try:
            text, why = bot.build_shift_call(self.tomorrow)
            self.assertEqual(why, "ok")
            self.assertIn("10am-12pm: Ann Tan", text)
            self.assertIn("12pm-2pm: —", text)
            self.assertIn("tg://user?id=990000001", text)
            self.data.set_agent(990000001, tag_calls=False)
            text, _ = bot.build_shift_call(self.tomorrow)
            self.assertNotIn("tg://user?id=990000001", text)
        finally:
            bot.SHIFTCALL_CHAT_ID = old

    def test_presets_are_seeded_once_and_never_overwritten(self):
        core.seed_presets()
        self.data.add("presets", {"name": "standard", "config": "[]"}, on_conflict="name")
        core.seed_presets()
        self.assertEqual({r["name"] for r in self.data.rows("presets")}, {"standard", "campaign"})
        self.assertEqual(self.data.row("presets", name="standard")["config"], "[]")


if __name__ == "__main__":
    unittest.main()
