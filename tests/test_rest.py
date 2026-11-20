"""The Data API client the data module talks through: what it sends, and how it reads answers.

Run: python -m unittest discover tests
"""
import json
import unittest

from rest import DataApiError, RestClient


class FakeHttp:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self, method, url, headers, body):
        self.sent.append({"method": method, "url": url, "headers": headers,
                          "body": json.loads(body) if body else None})
        return self.answers.pop(0)


class Tokens:
    def get_token(self):
        return "working-token"


def client(http, schema="lc_bot_dev"):
    return RestClient("https://x.supabase.co/", "pk_publishable", Tokens(), schema=schema, http=http)


class RestClientTest(unittest.TestCase):
    def test_select_sends_the_schema_the_key_and_the_working_token(self):
        http = FakeHttp((200, b'[{"id": "a"}]'))
        rows = client(http).select("members", select="person_id,status", status="eq.active", order="person_id")
        self.assertEqual(rows, [{"id": "a"}])
        s = http.sent[0]
        self.assertEqual(s["method"], "GET")
        self.assertEqual(s["url"], "https://x.supabase.co/rest/v1/members?select=person_id%2Cstatus&status=eq.active&order=person_id")
        self.assertEqual(s["headers"]["apikey"], "pk_publishable")
        self.assertEqual(s["headers"]["Authorization"], "Bearer working-token")
        self.assertEqual(s["headers"]["Accept-Profile"], "lc_bot_dev")

    def test_identity_reads_go_to_the_identity_profile(self):
        http = FakeHttp((200, b"[]"))
        client(http).select("people", schema="identity", select="id,telegram_id", telegram_id="eq.42")
        self.assertEqual(http.sent[0]["headers"]["Accept-Profile"], "identity")

    def test_insert_returns_the_rows_written(self):
        http = FakeHttp((201, b'[{"id": "new"}]'))
        rows = client(http).insert("weeks", {"title": "W1"})
        s = http.sent[0]
        self.assertEqual((s["method"], s["body"]), ("POST", {"title": "W1"}))
        self.assertEqual(s["headers"]["Content-Profile"], "lc_bot_dev")
        self.assertEqual(s["headers"]["Prefer"], "return=representation")
        self.assertEqual(rows, [{"id": "new"}])

    def test_a_write_only_table_asks_for_no_rows_back(self):
        http = FakeHttp((201, b""))
        self.assertIsNone(client(http).insert("time_edits", {"reason": "r"}, returning=False))
        self.assertEqual(http.sent[0]["headers"]["Prefer"], "return=minimal")

    def test_upsert_names_its_conflict_target(self):
        http = FakeHttp((201, b"[]"))
        client(http).insert("settings", {"key": "k", "value": "v"}, on_conflict="key")
        s = http.sent[0]
        self.assertIn("on_conflict=key", s["url"])
        self.assertEqual(s["headers"]["Prefer"], "return=representation,resolution=merge-duplicates")

    def test_update_and_delete_refuse_to_run_without_a_filter(self):
        with self.assertRaises(ValueError):
            client(FakeHttp()).update("slots", {"capacity": 2})
        with self.assertRaises(ValueError):
            client(FakeHttp()).delete("slots")

    def test_update_filters_and_patches(self):
        http = FakeHttp((200, b'[{"id": "s1"}]'))
        client(http).update("slots", {"capacity": 2}, id="eq.s1")
        s = http.sent[0]
        self.assertEqual((s["method"], s["body"]), ("PATCH", {"capacity": 2}))
        self.assertTrue(s["url"].endswith("/rest/v1/slots?id=eq.s1"))

    def test_rpc_posts_its_arguments(self):
        http = FakeHttp((200, b'"00000000-0000-4000-8000-000000000001"'))
        out = client(http).rpc("add_person", {"p_telegram_id": 1, "p_full_name": "A", "p_nickname": None,
                                              "p_country_code": "SG"})
        s = http.sent[0]
        self.assertEqual(s["method"], "POST")
        self.assertTrue(s["url"].endswith("/rest/v1/rpc/add_person"))
        self.assertEqual(s["headers"]["Content-Profile"], "lc_bot_dev")
        self.assertEqual(out, "00000000-0000-4000-8000-000000000001")

    def test_timestamps_are_asked_for_in_the_bots_zone(self):
        http = FakeHttp((200, b"[]"), (201, b"[]"))
        c = RestClient("https://x.supabase.co/", "pk", Tokens(), schema="lc_bot", http=http, timezone="Asia/Singapore")
        c.select("weeks")
        c.insert("weeks", {"label": "W1"})
        self.assertEqual(http.sent[0]["headers"]["Prefer"], "timezone=Asia/Singapore")
        self.assertEqual(http.sent[1]["headers"]["Prefer"], "return=representation,timezone=Asia/Singapore")

    def test_an_error_carries_postgrests_code_and_message(self):
        http = FakeHttp((403, b'{"code": "42501", "message": "permission denied for table members"}'))
        with self.assertRaises(DataApiError) as e:
            client(http).delete("members", person_id="eq.x")
        self.assertEqual((e.exception.status, e.exception.code), (403, "42501"))
        self.assertIn("permission denied", str(e.exception))


if __name__ == "__main__":
    unittest.main()
