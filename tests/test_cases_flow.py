"""Handover cases (core.py and handover.py) on the live test database: raising, closing from both
sides, handing back, notes, edits, search, store-chat posts and the case report.

Run: LCBOT_LIVE_DB=1 python -m unittest discover tests  (with requirements.txt installed)
"""
import asyncio
import json
import unittest

import botcode
from live import LiveTestCase, sql

core = botcode.load()
handover = __import__("handover") if core else None

CASE = {"section": "open", "prio": "🔴", "platform": "DUOKE", "flag": "🇸🇬", "store": "SGLAZADASONOS",
        "username": "buyer_one", "order_no": "SO-100", "product": "Era 100", "happened": "Arrived broken",
        "done": "", "need": "Send a replacement"}


class FakeBot:
    username = "lc_test_bot"

    def __init__(self, fail=False):
        self.sent, self.edits, self.fail = [], [], fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("no chat")
        self.sent.append((chat_id, text))
        return type("Msg", (), {"message_id": 700 + len(self.sent)})()

    async def edit_message_text(self, text, **kw):
        self.edits.append((kw.get("chat_id"), kw.get("message_id"), text))

    async def delete_message(self, chat_id, message_id):
        self.edits.append((chat_id, message_id, None))


@unittest.skipIf(core is None, "needs LCBOT_LIVE_DB=1 and the bot's packages")
class CasesFlowTest(LiveTestCase):
    def setUp(self):
        super().setUp()
        self.data = core.data
        self.data._pid.clear()
        self.data._tg.clear()
        self.data._changed()
        self.data._settings = None
        self.data._boards.clear()
        self.data._small.clear()
        self.data.add_member(self.person(990000001, "Sh Agent"))
        core.set_depts(990000001, ["SH"])
        self.data.add_member(self.person(990000002, "Pm Person"))
        core.set_depts(990000002, ["ON"])
        self.data.set_agent(990000002, channels=json.dumps(["LAZADA_SG"]))
        self.today = core.now().date()

    def raise_case(self, **over):
        return core.insert_case(990000001, {**CASE, **over}, self.today)

    def test_a_raised_case_is_open_with_its_channel_and_body(self):
        cid = self.raise_case()
        [r] = core.open_cases()
        self.assertEqual((r["id"], r["channel"], r["agent_id"], r["chat_posted"]), (cid, "LAZADA_SG", 990000001, False))
        self.assertIn("‼️Need Help: Send a replacement", r["body"])
        self.assertTrue(core.awaiting_pm(r))

    def test_closing_needs_both_sides_where_online_covers_the_channel(self):
        cid = self.raise_case()
        core.close_case(cid, 990000001, "sh")
        r = self.data.row("ho_cases", id=cid)
        self.assertEqual((r["sh_closed"], r["closed"], r["sh_closed_by"]), (True, False, 990000001))
        self.assertEqual(core.open_cases(), [])
        core.close_case(cid, 990000002, "on")
        r = self.data.row("ho_cases", id=cid)
        self.assertEqual((r["closed"], r["closed_by"]), (True, 990000002))
        self.assertEqual([e["action"] for e in core.case_history(cid)], ["closed", "closed"])

    def test_sh_alone_closes_a_channel_online_doesnt_cover(self):
        cid = self.raise_case(store="", platform="DUOKE")
        core.close_case(cid, 990000001, "sh")
        self.assertTrue(self.data.row("ho_cases", id=cid)["closed"])

    def test_reopen_side_reopen_all_and_force_close_keeps_the_first_closer(self):
        cid = self.raise_case()
        core.close_case(cid, 990000001, "sh")
        core.reopen_side(cid, 990000001, "sh")
        self.assertFalse(self.data.row("ho_cases", id=cid)["sh_closed"])
        core.close_case(cid, 990000001, "sh")
        core.force_close_case(cid, 990000002)
        r = self.data.row("ho_cases", id=cid)
        self.assertEqual((r["closed"], r["sh_closed_by"], r["on_closed_by"]), (True, 990000001, 990000002))
        core.reopen_case(cid, 990000002)
        r = self.data.row("ho_cases", id=cid)
        self.assertEqual((r["closed"], r["sh_closed"], r["on_closed"], r["sh_closed_by"]), (False, False, False, None))

    def test_pm_picks_live_in_settings_and_skips_leave_the_list(self):
        a, b = self.raise_case(), self.raise_case(username="buyer_two")
        core.pm_will_help(a, 990000002)
        core.pm_not_needed(b, 990000002)
        self.assertEqual(core.helper_of(a), 990000002)
        self.assertEqual(core.skipped_ids(), {b})
        self.assertTrue(self.data.row("ho_cases", id=b)["on_closed"])
        core.clear_pick(b)
        self.assertEqual(core.skipped_ids(), set())

    def test_notes_hand_back_and_the_latest_word(self):
        cid = self.raise_case()
        core.close_case(cid, 990000001, "sh")
        core.log_case(cid, 990000002, "on", "note", "Courier booked")
        self.assertEqual(core.reusable_note(cid, 990000002)["note"], "Courier booked")
        core.hand_back_case(cid, 990000002, "Need the photos")
        r = self.data.row("ho_cases", id=cid)
        self.assertEqual((r["handed_back"], r["sh_closed"]), (True, False))
        self.assertEqual(core.latest_note(cid)["note"], "Need the photos")
        self.assertEqual(core.latest_note(cid, ("note",), side="on")["note"], "Courier booked")
        self.assertIn("Pm Person: Need the photos", core.case_row_to_dict(r)["handback"])

    def test_an_edit_rebuilds_the_body_and_is_logged(self):
        cid = self.raise_case()
        core.save_case_edit(cid, 990000001, "need", "Refund instead")
        r = self.data.row("ho_cases", id=cid)
        self.assertIn("Need Help: Refund instead", r["body"])
        self.assertEqual(core.case_history(cid)[-1]["note"], "What's needed next: Send a replacement → Refund instead")

    def test_search_by_words_and_by_date(self):
        self.raise_case()
        self.raise_case(username="other_buyer", order_no="SO-200", product="Move 2")
        self.assertEqual([r["username"] for r in core.search_cases("buyer_one")], ["buyer_one"])
        self.assertEqual([r["username"] for r in core.search_cases("so- move")], ["other_buyer"])
        self.assertEqual(len(core.search_cases(self.today.isoformat())), 2)
        self.assertEqual(core.search_cases("100%"), [])

    def test_new_cases_post_to_their_store_chat_once_and_a_failure_is_undone(self):
        core.set_setting("chat:LAZADA_SG", json.dumps({"chat": -1001234, "thread": None}))
        cid = self.raise_case()
        self.assertEqual(asyncio.run(core.post_new_cases_to_chats(FakeBot(fail=True), 990000001)), 0)
        self.assertEqual(self.data.count("channel_posts"), 0)
        self.assertFalse(self.data.row("ho_cases", id=cid)["chat_posted"])
        bot = FakeBot()
        self.assertEqual(asyncio.run(core.post_new_cases_to_chats(bot, 990000001)), 1)
        self.assertEqual(asyncio.run(core.post_new_cases_to_chats(bot, 990000001)), 0)
        post = self.data.row("channel_posts")
        self.assertEqual((post["chat_id"], post["message_id"], post["agent_id"]), (-1001234, 701, 990000001))
        self.assertIn("buyer_one", bot.sent[0][1])
        asyncio.run(core.move_to_tc_online(bot, cid, 990000002))
        self.assertEqual(bot.edits, [(-1001234, 701, None)])
        self.assertIsNone(self.data.row("ho_cases", id=cid)["post_id"])

    def test_pms_hear_once_about_new_cases_on_their_channels(self):
        self.raise_case()
        bot = FakeBot()
        asyncio.run(core.notify_pms(bot, 990000001))
        asyncio.run(core.notify_pms(bot, 990000001))
        self.assertEqual([c for c, _ in bot.sent], [990000002])

    def test_shift_summary_and_handovers(self):
        cid = self.raise_case()
        core.add_handover(990000001, self.today, "something open")
        text, closed = core.render_shift_summary(990000001, "Sh Agent")
        self.assertIn("Raised this shift (1)", text)
        self.assertEqual(closed, 0)
        self.assertEqual(core.last_open_handover()["body"], "something open")
        core.close_case(cid, 990000001, "sh")
        self.assertEqual(core.render_shift_summary(990000001, "Sh Agent")[1], 0)   # raised this shift, not "closed"

    def test_case_report_counts_a_channel(self):
        cid = self.raise_case()
        core.log_case(cid, 990000002, "on", "handed back", "x")
        core.log_case(cid, 990000002, "on", "handed back", "y")
        text = handover.case_report(["LAZADA_SG"], self.today, self.today)
        self.assertIn("🆕 1 new · ✅ 0 closed · ⏳ 1 open", text)
        self.assertIn("2", text)


if __name__ == "__main__":
    unittest.main()
