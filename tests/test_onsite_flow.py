"""On-Site (onsite.py) on the live test database: who works where, booking with clash checks,
moving, handovers taken once, days marked busy, a visit from arrival to report, follow-ups,
replacement requests and cab claims.

Run: LCBOT_LIVE_DB=1 python -m unittest discover tests  (with requirements.txt installed)
"""
import asyncio
import unittest
from datetime import timedelta

import botcode
from live import LiveTestCase

core = botcode.load()
onsite = __import__("onsite") if core else None

SPEC, BOOKER, FS = 990000011, 990000012, 990000013
CUSTOMER = {"customer_name": "Test Customer", "phone": "+65 0000 0000", "address": "1 Test Road",
            "postal": "000001", "product": "Era 100", "issue": "No sound", "order_no": "SO-1"}


class FakeBot:
    username = "lc_test_bot"

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return type("Msg", (), {"message_id": 800 + len(self.sent), "chat_id": chat_id})()

    async def send_photo(self, chat_id, photo, **kw):
        self.sent.append((chat_id, kw.get("caption", "")))
        return type("Msg", (), {"message_id": 900 + len(self.sent), "chat_id": chat_id})()

    async def edit_message_text(self, *a, **kw):
        pass

    async def unpin_chat_message(self, *a, **kw):
        pass

    async def pin_chat_message(self, *a, **kw):
        pass


@unittest.skipIf(core is None, "needs LCBOT_LIVE_DB=1 and the bot's packages")
class OnsiteFlowTest(LiveTestCase):
    def setUp(self):
        super().setUp()
        self.data = core.data
        self.data._pid.clear()
        self.data._tg.clear()
        self.data._changed()
        self.data._settings = None
        self.data._small.clear()
        self.data.add_member(self.person(SPEC, "Spec Person", country="TH"))
        core.set_depts(SPEC, ["ONSITE"])
        self.data.add_member(self.person(BOOKER, "Book Person"))
        core.set_depts(BOOKER, ["SH"])
        self.data.add_member(self.person(FS, "Store Person"))
        core.set_depts(FS, ["FS"])
        self.day = (core.now() + timedelta(days=3)).date().isoformat()

    def book(self, hhmm="14:00", **over):
        vid, err = onsite.create_visit(BOOKER, {"country": "TH", "visit_type": "SETUP", "specialist_id": SPEC,
                                                "date": self.day, "time": hhmm, **CUSTOMER, **over})
        self.assertIsNone(err)
        return vid

    def test_where_people_work(self):
        self.assertEqual([r["user_id"] for r in onsite.specialists_in("TH")], [SPEC])
        self.assertEqual(onsite.specialists_in("SG"), [])
        self.data.set_agent(FS, store="MY_SONOS")
        self.assertEqual(onsite.work_place(FS), {"country": "MY", "store": "MY_SONOS"})
        self.assertEqual(onsite.store_of(FS), "Sonos Concept Store by TC Acoustic")

    def test_a_booking_keeps_its_country_time_and_clashes_are_refused(self):
        vid = self.book()
        v = onsite.visit(vid)
        self.assertTrue(v["starts_at"].endswith("+07:00"))
        self.assertEqual(onsite.span_of(v)[0].strftime("%H:%M"), "14:00")
        self.assertEqual(v["created_by"], BOOKER)
        self.assertTrue(onsite.has_time(vid))
        _, err = onsite.create_visit(BOOKER, {"country": "TH", "visit_type": "SETUP", "specialist_id": SPEC,
                                              "date": self.day, "time": "15:00", **CUSTOMER})
        self.assertIn("clashes", err)

    def test_moving_a_visit_and_cancelling_it(self):
        vid = self.book()
        ok, err = onsite.request_reschedule(vid, BOOKER, self.day, "17:30")
        self.assertTrue(ok, err)
        v = onsite.visit(vid)
        self.assertEqual((onsite.datetime.fromisoformat(v["proposed_starts_at"]).strftime("%H:%M"),
                          v["proposed_by"]), ("17:30", BOOKER))
        ok, _ = onsite.cancel_visit(vid, BOOKER, "customer away")
        v = onsite.visit(vid)
        self.assertEqual((v["status"], v["proposed_starts_at"]), ("CANCELLED", None))
        self.assertEqual(onsite.last_log(vid, "cancelled")["note"], "customer away")

    def test_a_handover_is_taken_once_then_booked(self):
        vid, err = onsite.create_handover(BOOKER, {"country": "TH", "visit_type": "TROUBLESHOOT",
                                                   "note": "call after 6", **CUSTOMER})
        self.assertIsNone(err)
        self.assertEqual(onsite.handover_note(vid), "call after 6")
        self.assertFalse(onsite.has_time(vid))
        self.assertEqual(onsite.take_handover(vid, SPEC), (True, None))
        self.assertEqual(onsite.take_handover(vid, SPEC), (False, "You already have it."))
        self.assertEqual(onsite.give_back(vid, SPEC), (True, None))
        self.assertEqual(onsite.visit(vid)["specialist_id"], None)
        onsite.take_handover(vid, SPEC)
        self.assertEqual(onsite.set_time(vid, SPEC, self.day, "11:00"), (True, None))
        self.assertEqual(onsite.visit(vid)["status"], "SCHEDULED")

    def test_days_marked_busy_and_blocks(self):
        self.book("15:00")
        ok, err = onsite.set_day_part(SPEC, "TH", self.day, "PM")
        self.assertFalse(ok)
        self.assertIn("visit then", err)
        self.assertEqual(onsite.set_day_part(SPEC, "TH", self.day, "AM"), (True, None))
        self.assertEqual(onsite.day_part(SPEC, "TH", self.day), "AM")
        self.assertEqual(onsite.free_text(onsite.free_windows(SPEC, "TH", self.day)), "Available 2pm–3pm, 5pm–6pm")
        onsite.set_day_part(SPEC, "TH", self.day, "FREE")
        self.assertEqual(onsite.day_part(SPEC, "TH", self.day), "")
        bid, err = onsite.add_block(SPEC, "TH", self.day, "10:00", "11:00", "dentist")
        self.assertIsNone(err)
        self.assertTrue(onsite.remove_block(SPEC, bid))
        self.assertFalse(onsite.remove_block(SPEC, bid))

    def test_a_visit_not_finished_books_its_follow_up(self):
        vid = self.book()
        self.assertEqual(onsite.arrive(vid, SPEC), (True, None))
        new_id, err = onsite.book_followup(vid, SPEC, {"reason": "PART", "note": "bracket", "urgency": "HIGH",
                                                       "later": True})
        self.assertIsNone(err)
        old, new = onsite.visit(vid), onsite.visit(new_id)
        self.assertEqual((old["outcome"], old["followup_done"], old["amount_cents"]), ("RETURN", True, 0))
        self.assertEqual((new["followup_of"], new["status"], new["urgency"], new["specialist_id"]),
                         (vid, "ARRANGING", "HIGH", SPEC))
        self.assertEqual(onsite.next_visit_of(vid)["id"], new_id)
        self.assertEqual(onsite.nf_reason_text(vid), "Need a part: bracket")
        asyncio.run(onsite.finish_visit(FakeBot(), None, vid, SPEC))
        self.assertEqual(onsite.visit(vid)["status"], "COMPLETED")
        hist = onsite.my_history(SPEC)
        self.assertEqual((hist["stats"]["total"], hist["stats"]["followups"]), (1, 1))

    def test_a_request_is_taken_and_done_once(self):
        vid = self.book()
        onsite.arrive(vid, SPEC)
        rid, err = onsite.create_onsite_request(SPEC, vid, {"need": "PART", "warranty": "IN", "pays_by": "NONE",
                                                            "reason": "won't power on"})
        self.assertIsNone(err)
        self.assertIn(f"#{core.ref(rid)}", onsite.last_log(vid, "request")["note"])
        bot = FakeBot()
        self.assertFalse(asyncio.run(onsite.post_onsite_request(bot, rid)))   # no chat set: DMs instead
        self.assertEqual(self.data.change("onsite_requests", {"status": "TAKEN", "taken_by": BOOKER},
                                          id=rid, status="OPEN")[0]["taken_by"], BOOKER)
        self.assertEqual(self.data.change("onsite_requests", {"status": "TAKEN"}, id=rid, status="OPEN"), [])
        self.assertEqual(onsite.my_onsite_requests(SPEC)[0]["status"], "TAKEN")

    def test_a_cab_claim_waits_for_its_receipt(self):
        vid = self.book()
        onsite.arrive(vid, SPEC)
        first, err = onsite.create_cab_claim(SPEC, vid, "TO", "18.40")
        self.assertIsNone(err)
        cid, _ = onsite.create_cab_claim(SPEC, vid, "BOTH", "30")
        self.assertIsNone(self.data.row("cab_claims", id=first))           # an older one waiting is dropped
        c = self.data.row("cab_claims", id=cid)
        self.assertEqual((c["user_id"], c["amount_cents"], c["currency"], c["status"]), (SPEC, 3000, "THB", "RECEIPT"))
        self.assertEqual([x["status"] for x in onsite.my_cab_claims(SPEC)], ["RECEIPT"])

    def test_screens_build(self):
        vid = self.book()
        p = onsite.visits_payload(SPEC, week=self.day)
        self.assertEqual(p["myCountry"], "TH")
        self.assertIn(vid, [it["id"] for d in p["days"] for it in d["items"]])
        t = onsite.tracker_payload(BOOKER, week=self.day)
        self.assertIn(vid, [it["id"] for it in t["items"]])
        text, _ = onsite.visits_list(SPEC)
        self.assertIn("My visits", text)
        self.assertEqual(onsite.pay_payload(SPEC)["count"], 0)
        self.assertEqual(onsite.onsite_team_payload()["people"][0]["name"], "Spec Person")


if __name__ == "__main__":
    unittest.main()
