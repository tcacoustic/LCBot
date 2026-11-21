"""The working-token source: a stored token on the volume (or the first seed) swapped for one-hour working tokens.

Mirrors tcacoustic/tc-one-app's service-token module, the estate's reference client (tc-data-warehouse
docs/signing-service.md, "Workloads"). Run: python -m unittest discover tests
"""
import base64
import json
import os
import tempfile
import threading
import unittest

from service_token import (ChainRefused, ServiceTokenConfigError, ServiceTokenSource, ServiceTokenVolumeError,
                           TokenServiceRefused)

T0 = 1_800_000_000.0  # a fixed "now", in seconds


def jwt(**claims):
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'ES256'})}.{part(claims)}.sig"


def iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class FakeSts:
    """Answers like sign-token-workload, recording what each request presented."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.presented = []

    def __call__(self, url, body):
        assert url == "https://x.supabase.co/functions/v1/sign-token-workload", url
        req = json.loads(body)
        assert req["want"] == "working"
        self.presented.append(req["storedToken"])
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a  # (status, dict)


class ServiceTokenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.vol = os.path.join(self.dir, "sts", "stored.json")
        self.now = T0
        self.slept = []

    def src(self, fake, seed="seed-token"):
        return ServiceTokenSource("https://x.supabase.co/", seed=seed, volume_path=self.vol, post=fake,
                                  now=lambda: self.now, sleep=self.slept.append)

    def test_first_boot_presents_the_seed_and_caches_the_working_token(self):
        fake = FakeSts([(200, {"token": "w1", "expiresAt": iso(T0 + 3600)})])
        s = self.src(fake)
        self.assertEqual(s.get_token(), "w1")
        self.assertEqual(s.get_token(), "w1")
        self.assertEqual(fake.presented, ["seed-token"])

    def test_refreshes_five_minutes_before_expiry(self):
        fake = FakeSts([(200, {"token": "w1", "expiresAt": iso(T0 + 3600)}),
                        (200, {"token": "w2", "expiresAt": iso(T0 + 7200)})])
        s = self.src(fake)
        s.get_token()
        self.now = T0 + 3600 - 299
        self.assertEqual(s.get_token(), "w2")

    def test_a_renewal_is_saved_to_the_volume_and_presented_next(self):
        fake = FakeSts([(200, {"token": "w1", "expiresAt": iso(T0 + 3600),
                               "renewal": {"storedToken": "stored-2", "expiresAt": iso(T0 + 172800)}}),
                        (200, {"token": "w2", "expiresAt": iso(T0 + 7200)})])
        s = self.src(fake)
        s.get_token()
        with open(self.vol) as f:
            self.assertEqual(json.load(f)["storedToken"], "stored-2")
        self.assertEqual(oct(os.stat(self.vol).st_mode & 0o777), "0o600")
        self.now = T0 + 3600
        s.get_token()
        self.assertEqual(fake.presented, ["seed-token", "stored-2"])

    def test_the_volume_wins_over_the_seed(self):
        os.makedirs(os.path.dirname(self.vol))
        with open(self.vol, "w") as f:
            json.dump({"storedToken": "on-volume"}, f)
        fake = FakeSts([(200, {"token": "w1", "expiresAt": iso(T0 + 3600)})])
        self.src(fake).get_token()
        self.assertEqual(fake.presented, ["on-volume"])

    def test_a_newer_chain_in_the_seed_replaces_a_dead_one_on_the_volume(self):
        old, new = jwt(chain="c1", iat=100), jwt(chain="c2", iat=200)
        os.makedirs(os.path.dirname(self.vol))
        with open(self.vol, "w") as f:
            json.dump({"storedToken": old}, f)
        fake = FakeSts([(200, {"token": "w1", "expiresAt": iso(T0 + 3600)})])
        self.src(fake, seed=new).get_token()
        self.assertEqual(fake.presented, [new])

    def test_an_older_or_same_chain_seed_never_replaces_the_volume(self):
        vol, seed = jwt(chain="c2", iat=200), jwt(chain="c1", iat=100)
        os.makedirs(os.path.dirname(self.vol))
        with open(self.vol, "w") as f:
            json.dump({"storedToken": vol}, f)
        fake = FakeSts([(200, {"token": "w1", "expiresAt": iso(T0 + 3600)})])
        self.src(fake, seed=seed).get_token()
        self.assertEqual(fake.presented, [vol])

    def test_no_seed_and_no_volume_is_a_config_error(self):
        with self.assertRaises(ServiceTokenConfigError):
            self.src(FakeSts([]), seed=None).get_token()

    def test_an_unreadable_volume_stops_rather_than_presenting_the_seed(self):
        os.makedirs(os.path.dirname(self.vol))
        with open(self.vol, "w") as f:
            f.write("not json")
        fake = FakeSts([])
        with self.assertRaises(ServiceTokenVolumeError):
            self.src(fake).get_token()
        self.assertEqual(fake.presented, [])

    def test_5xx_and_network_errors_retry_with_the_same_token(self):
        fake = FakeSts([OSError("down"), (503, {"error": "unavailable"}),
                        (200, {"token": "w1", "expiresAt": iso(T0 + 3600)})])
        self.assertEqual(self.src(fake).get_token(), "w1")
        self.assertEqual(fake.presented, ["seed-token"] * 3)
        self.assertEqual(self.slept, [0.5, 2.0])

    def test_gives_up_after_three_tries(self):
        fake = FakeSts([(503, {}), (503, {}), (503, {})])
        with self.assertRaises(TokenServiceRefused) as e:
            self.src(fake).get_token()
        self.assertEqual(e.exception.status, 503)

    def test_4xx_is_never_retried(self):
        fake = FakeSts([(401, {"error": "unauthorized"})])
        with self.assertRaises(TokenServiceRefused):
            self.src(fake).get_token()
        self.assertEqual(len(fake.presented), 1)

    def test_a_refused_chain_says_to_reseed(self):
        fake = FakeSts([(403, {"error": "chain refused"})])
        with self.assertRaises(ChainRefused):
            self.src(fake).get_token()

    def test_one_exchange_at_a_time(self):
        """Two exchanges in flight past half-life would refuse our own chain (signing-service.md)."""
        inside, peak, gate = [0], [0], threading.Event()

        def slow(url, body):
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
            gate.wait(0.2)
            inside[0] -= 1
            return 200, {"token": "w1", "expiresAt": iso(T0 + 3600)}

        s = self.src(slow)
        threads = [threading.Thread(target=s.get_token) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(peak[0], 1)


if __name__ == "__main__":
    unittest.main()
