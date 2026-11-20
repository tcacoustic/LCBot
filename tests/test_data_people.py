"""People and access through the data module, live against lc_bot_dev as lc_bot_dev_app.

Run: LCBOT_LIVE_DB=1 python -m unittest tests.test_data_people (see tests/README.md)
"""
from data import Data
from live import LiveTestCase, sql


class PeopleTest(LiveTestCase):
    def setUp(self):
        super().setUp()
        self.data = Data(self.db)

    def test_a_stranger_has_no_row(self):
        self.assertIsNone(self.data.agent(990000001))

    def test_a_request_is_pending_under_the_name_given(self):
        self.data.request_access(990000001, "Jia En", telegram_name="JE", telegram_username="je")
        row = self.data.agent(990000001)
        self.assertEqual((row["user_id"], row["status"], row["name"], row["person_id"]),
                         (990000001, "pending", "Jia En", None))
        self.assertEqual([r["user_id"] for r in self.data.agents(status="pending")], [990000001])

    def test_asking_again_while_pending_keeps_one_request(self):
        self.data.request_access(990000001, "Jia En")
        self.data.request_access(990000001, "Jia En Tan")
        self.assertEqual(self.data.agent(990000001)["name"], "Jia En Tan")
        self.assertEqual(sql("select count(*) from lc_bot_dev.join_requests"), "1")

    def test_request_messages_are_kept_for_settling_later(self):
        self.data.request_access(990000001, "Jia En")
        self.data.set_request_messages(990000001, [[1, 2], [3, 4]])
        self.assertEqual(self.data.agent(990000001)["req_msgs"], [[1, 2], [3, 4]])

    def test_approval_makes_a_person_and_a_member(self):
        admin = self.person(990000009, "Admin Person")
        self.data.request_access(990000001, "Jia En")
        row = self.data.decide(990000001, approve=True, by=990000009)
        self.assertEqual(row["status"], "pending")          # the row as it was, for the message
        now = self.data.agent(990000001)
        self.assertEqual((now["status"], now["name"], now["role"], now["active"]),
                         ("active", "Jia En", "agent", True))
        self.assertEqual(sql("select person_type || ' ' || status from identity.people where telegram_id = 990000001"),
                         "freelancer active")
        self.assertEqual(sql("select status || ' ' || (decided_by_person_id = '%s')::text "
                             "from lc_bot_dev.join_requests" % admin), "approved true")

    def test_a_decided_request_cannot_be_decided_again(self):
        self.person(990000009, "Admin Person")
        self.data.request_access(990000001, "Jia En")
        self.data.decide(990000001, approve=False, by=990000009)
        self.assertEqual(self.data.agent(990000001)["status"], "declined")
        self.assertIsNone(self.data.decide(990000001, approve=True, by=990000009))
        self.assertEqual(sql("select count(*) from identity.people where telegram_id = 990000001"), "0")

    def test_approving_someone_already_on_the_spine_uses_their_record(self):
        self.person(990000009, "Admin Person")
        pid = self.person(990000001, "Tan Jia En", nickname="Jia")
        self.data.request_access(990000001, "jia en")
        self.data.decide(990000001, approve=True, by=990000009)
        row = self.data.agent(990000001)
        self.assertEqual((row["person_id"], row["name"]), (pid, "Jia"))

    def test_shown_name_is_nickname_else_full_name(self):
        self.data.add_member(self.person(990000001, "Tan Jia En", nickname="Jia"))
        self.data.add_member(self.person(990000002, "Lim Wei"))
        self.assertEqual(self.data.agent(990000001)["name"], "Jia")
        self.assertEqual(self.data.agent(990000002)["name"], "Lim Wei")

    def test_a_revoked_person_has_no_access(self):
        self.data.add_member(self.person(990000001, "Gone Person", status="revoked"))
        row = self.data.agent(990000001)
        self.assertEqual((row["status"], row["active"]), ("revoked", False))
        self.assertEqual(self.data.agents(status="active"), [])

    def test_teams_filter_and_replace(self):
        a = self.person(990000001, "A Person")
        b = self.person(990000002, "B Person")
        self.data.add_member(a)
        self.data.add_member(b)
        self.data.set_teams(990000001, ["LIVECHAT", "ON"])
        self.data.set_teams(990000002, ["ON"])
        self.assertEqual(self.data.teams(990000001), {"LIVECHAT", "ON"})
        self.assertEqual([r["user_id"] for r in self.data.agents(status="active", team="LIVECHAT")], [990000001])
        self.assertEqual([r["user_id"] for r in self.data.agents(status="active", team="ON")], [990000001, 990000002])
        self.data.set_teams(990000001, ["ON"])
        self.assertEqual(self.data.teams(990000001), {"ON"})

    def test_flags_read_and_write_under_the_old_names(self):
        self.data.add_member(self.person(990000001, "A Person"))
        self.data.set_agent(990000001, role="admin", tag_calls=False, salaried=True, dm_ok=True)
        row = self.data.agent(990000001)
        self.assertEqual((row["role"], row["tag_calls"], row["salaried"], row["dm_ok"], row["on_avails"]),
                         ("admin", False, True, True, True))
        self.assertEqual([r["user_id"] for r in self.data.agents(status="active", salaried=True)], [990000001])
        self.assertEqual(self.data.agents(status="active", tag_calls=True), [])

    def test_find_by_name_matches_shown_or_full_name_case_blind(self):
        self.data.add_member(self.person(990000001, "Tan Jia En", nickname="Jia"))
        self.assertEqual(self.data.find("jia")["user_id"], 990000001)
        self.assertEqual(self.data.find("TAN JIA EN")["user_id"], 990000001)
        self.assertEqual(self.data.find("990000001")["user_id"], 990000001)
        self.assertIsNone(self.data.find("nobody"))
