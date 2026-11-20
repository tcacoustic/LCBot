"""Special requests to Ops (ops.py) on the live test database: saving one, taking and answering it
once, rounds, the sheet's match, and the App's lists.

Run: LCBOT_LIVE_DB=1 python -m unittest discover tests  (with requirements.txt installed)
"""
import asyncio
import unittest
from datetime import timedelta

import botcode
from live import LiveTestCase

core = botcode.load()
ops = __import__("ops") if core else None

SENDER, OPS = 990000021, 990000022


class FakeBot:
    username = "lc_test_bot"

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return type("Msg", (), {"message_id": 500 + len(self.sent), "chat_id": chat_id})()


@unittest.skipIf(core is None, "needs LCBOT_LIVE_DB=1 and the bot's packages")
class OpsFlowTest(LiveTestCase):
    def setUp(self):
        super().setUp()
        self.data = core.data
        self.data._pid.clear()
        self.data._tg.clear()
        self.data._changed()
        self.data._settings = None
        self.data.add_member(self.person(SENDER, "Send Person"))
        core.set_depts(SENDER, ["SH"])
        self.data.add_member(self.person(OPS, "Ops Person"))
        core.set_depts(OPS, ["OPS"])
        self.when = (core.now() + timedelta(days=5)).date().isoformat()

    def send(self, **over):
        d = {"order_no": "SO-77", "country": "MY", "category": "DATE", "details": "Move to Friday",
             "delivery_date": self.when, "timeframe": "AM", **over}

        async def go():
            out = await ops.create_request(FakeBot(), d, SENDER)
            await asyncio.gather(*list(ops._background))
            return out
        return asyncio.run(go())

    def test_a_request_is_saved_open_with_its_flags(self):
        rid, _, kind = self.send()
        r = ops.get_request(rid)
        self.assertEqual((r["status"], r["created_by"], r["delivery_date"], r["urgent"], r["sheet_dirty"]),
                         ("OPEN", SENDER, self.when, kind == "URGENT", False))
        self.assertEqual(ops.my_requests(SENDER, 8)[0]["order"], "SO-77")
        self.assertEqual(ops.sender_name(r), "Send Person")

    def test_answered_once_and_the_taker_kept(self):
        rid, _, _ = self.send()
        self.data.change("special_requests", {"status": "TAKEN", "taken_by": OPS}, id=rid, status="OPEN")
        ops.answer_request(rid, "CANT", "Out of stock", 990000099)
        r = ops.get_request(rid)
        self.assertEqual((r["status"], r["reason"], r["taken_by"], r["sheet_dirty"]), ("CANT", "Out of stock", OPS, True))
        ops.answer_request(rid, "DONE", None, OPS)
        self.assertEqual(ops.get_request(rid)["status"], "CANT")

    def test_the_sheet_finds_the_bots_own_row_by_its_timestamp(self):
        rid, _, _ = self.send()
        r = ops.get_request(rid)
        stamp = ops.sheet_values(r)[1]
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(ops.find_sheet_request("SO-77", stamp, "")["id"], rid)
        self.assertIsNone(ops.find_sheet_request("SO-77", "2000-01-01 00:00:00", ""))

    def test_ops_home_lists_whats_open(self):
        rid, _, _ = self.send()
        home = ops.ops_home(OPS)
        self.assertEqual((home["open"], home["doneToday"]), (1, 0))
        ops.answer_request(rid, "DONE", None, OPS)
        self.assertEqual(ops.ops_home(OPS)["doneToday"], 1)


if __name__ == "__main__":
    unittest.main()
