"""The Mini App's payloads and posts (webapp.py) on the live test database: the hours screen, the team
hours view, and the posts that carry a row's uuid.

Run: LCBOT_LIVE_DB=1 python -m unittest discover tests  (with requirements.txt installed)
"""
import unittest
from datetime import timedelta

import botcode
from live import LiveTestCase

core = botcode.load()
webapp = __import__("webapp") if core else None

CASE = {"section": "open", "prio": "🔴", "platform": "DUOKE", "flag": "🇸🇬", "store": "SGLAZADASONOS",
        "username": "buyer_one", "order_no": "SO-100", "product": "Era 100", "happened": "Arrived broken",
        "done": "", "need": "Send a replacement"}


@unittest.skipIf(core is None, "needs LCBOT_LIVE_DB=1 and the bot's packages")
class WebappFlowTest(LiveTestCase):
    def setUp(self):
        super().setUp()
        self.data = core.data
        self.data._pid.clear()
        self.data._tg.clear()
        self.data._changed()
        self.data._settings = None
        self.data._boards.clear()
        self.data._small.clear()
        self.data.add_member(self.person(990000001, "Live Agent", nickname="Livvy"))
        core.set_depts(990000001, ["LIVECHAT"])
        self.today = core.now().date()

    def worked(self, hours: int):
        start = core.now().replace(day=1, hour=10, minute=0, second=0, microsecond=0)
        return self.data.add("time_entries", {
            "agent_id": 990000001, "slot_id": None, "the_date": start.date().isoformat(),
            "clock_in": start.isoformat(), "clock_out": (start + timedelta(hours=hours)).isoformat(),
            "status": "closed"})

    def test_the_hours_screen_shows_the_month_by_the_name_on_record(self):
        self.data.add("pay_rates", {"agent_id": 990000001, "cents": 1200, "effective_from": "2020-01-01",
                                    "created_by": 990000099, "created_at": core.now().isoformat()})
        entry = self.worked(2)
        p = webapp.miniapp_payload(990000001)
        self.assertEqual((p["name"], p["count"], p["days"], p["rate"]), ("Livvy", 1, 1, "$12.00"))
        [s] = p["shifts"]
        self.assertEqual(s["id"], core.ref(entry["id"]))  # the short ref /fixtime takes
        self.assertEqual(p["total"], s["pay"])
        self.assertFalse(p["openShift"])

    def test_the_team_view_lists_active_livechat_people(self):
        self.worked(3)
        self.data.add_member(self.person(990000003, "Other Team"))
        core.set_depts(990000003, ["SH"])
        p = webapp.team_payload(self.today)
        self.assertEqual([r["id"] for r in p["people"]], [990000001])
        self.assertEqual((p["people"][0]["name"], p["headcount"]), ("Livvy", 1))

    def test_posted_ids_must_be_whole_uuids(self):
        cid = core.insert_case(990000001, CASE, self.today)
        self.assertEqual(webapp.uuid_of(cid.upper()), cid)
        for bad in (0, "12", None, "1; drop", cid[:8]):
            self.assertEqual(webapp.uuid_of(bad), "")

    def test_cases_are_sorted_by_when_they_were_raised(self):
        first = core.insert_case(990000001, CASE, self.today)
        second = core.insert_case(990000001, {**CASE, "username": "buyer_two"}, self.today)
        shaped = [webapp.case_shape(r, self.today) for r in core.open_cases()]
        self.assertEqual([c["id"] for c in sorted(shaped, key=lambda c: c["seq"])], [first, second])


if __name__ == "__main__":
    unittest.main()
