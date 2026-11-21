"""Working tokens for the bot's database role, from the warehouse's sign-token-workload.

A Python copy of tcacoustic/tc-one-app's service-token module (the copy SVA runs is
store-visit-app src/db/service-token.ts); keep them in step. The rules are the warehouse's
(tc-data-warehouse docs/signing-service.md, "Workloads"):

- The service holds a stored token (2 days). The Railway variable SERVICE_TOKEN_SEED holds only the
  first one; every renewal is written to a file on the service's volume (SERVICE_TOKEN_VOLUME_PATH),
  and from then on the volume wins. A restart that presented a spent seed would refuse our own chain.
- One exchange at a time, and a renewal is on disk before the next exchange.
- 5xx and network failures are retried with the same stored token; 4xx never are.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import threading
import time
import urllib.error
import urllib.request

MAX_TRIES = 3
BACKOFF_S = [0.5, 2.0]
REFRESH_MARGIN_S = 5 * 60


class ServiceTokenConfigError(Exception):
    pass


class ServiceTokenVolumeError(Exception):
    """The stored token on the volume can't be read. Presenting the seed would refuse our own chain, so stop."""

    def __init__(self, path: str, cause: object):
        super().__init__(f"The stored service token at {path} could not be read: {cause}")


class ChainRefused(Exception):
    def __init__(self):
        super().__init__(
            "The token service refused this service's token chain. Re-seed it: mint a new seed in the warehouse "
            "and set SERVICE_TOKEN_SEED. A newer seed (a different chain) replaces the stored token on its own; "
            "if the seed is not a readable token, delete the stored token file on the volume.")


class TokenServiceRefused(Exception):
    def __init__(self, status: int):
        super().__init__(f"sign-token-workload answered {status}")
        self.status = status


def _claims(token: str) -> dict | None:
    """The claims of a stored token, read without verifying (the warehouse verifies)."""
    try:
        part = token.split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return c if isinstance(c, dict) else None
    except Exception:
        return None


def _parse_ts(s: str) -> float:
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _post_json(url: str, body: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body.encode(), method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class ServiceTokenSource:
    def __init__(self, supabase_url: str, *, volume_path: str, seed: str | None = None,
                 post=_post_json, now=time.time, sleep=time.sleep):
        self._endpoint = supabase_url.rstrip("/") + "/functions/v1/sign-token-workload"
        self._volume, self._seed = volume_path, seed
        self._post, self._now, self._sleep = post, now, sleep
        self._lock = threading.Lock()
        self._working: tuple[str, float] | None = None

    def get_token(self) -> str:
        """A working token (1 hour), refreshed five minutes before it runs out."""
        with self._lock:
            if self._working and self._working[1] - REFRESH_MARGIN_S > self._now():
                return self._working[0]
            a = self._exchange()
            self._working = (a["token"], _parse_ts(a["expiresAt"]))
            return a["token"]

    def _stored_token(self) -> str:
        try:
            with open(self._volume) as f:
                raw = f.read()
        except FileNotFoundError:
            if not self._seed:
                raise ServiceTokenConfigError("SERVICE_TOKEN_SEED must be set (the volume holds no stored token)")
            return self._seed  # first boot
        except OSError as e:
            raise ServiceTokenVolumeError(self._volume, e)
        try:
            token = json.loads(raw).get("storedToken")
        except (ValueError, AttributeError) as e:
            raise ServiceTokenVolumeError(self._volume, e)
        if not isinstance(token, str) or not token:
            raise ServiceTokenVolumeError(self._volume, "no storedToken field")
        # A re-seed after a refused chain: present the seed only when it is demonstrably a newer, different chain.
        if self._seed and self._seed != token:
            on_volume, seeded = _claims(token) or {}, _claims(self._seed) or {}
            if (on_volume.get("chain") and seeded.get("chain") and on_volume["chain"] != seeded["chain"]
                    and (seeded.get("iat") or 0) > (on_volume.get("iat") or 0)):
                return self._seed
        return token

    def _save_renewal(self, renewal: dict) -> None:
        os.makedirs(os.path.dirname(self._volume) or ".", exist_ok=True)
        tmp = self._volume + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(renewal, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._volume)

    def _exchange(self) -> dict:
        attempt = 0
        while True:
            attempt += 1
            body = json.dumps({"storedToken": self._stored_token(), "want": "working"})
            try:
                status, answer = self._post(self._endpoint, body)
            except OSError:
                if attempt >= MAX_TRIES:
                    raise
                self._sleep(BACKOFF_S[attempt - 1])
                continue
            if status < 500 or attempt >= MAX_TRIES:
                break
            self._sleep(BACKOFF_S[attempt - 1])
        if not 200 <= status < 300:
            if isinstance(answer, dict) and answer.get("error") == "chain refused":
                raise ChainRefused()
            raise TokenServiceRefused(status)
        if answer.get("renewal"):
            self._save_renewal(answer["renewal"])
        return answer


def service_token_from_env(env=os.environ) -> ServiceTokenSource:
    """Build from the environment, naming whichever variable is missing."""
    def need(name):
        v = env.get(name)
        if not v:
            raise ServiceTokenConfigError(f"{name} must be set")
        return v
    volume = need("SERVICE_TOKEN_VOLUME_PATH")
    seed = (env.get("SERVICE_TOKEN_SEED") or None) if os.path.exists(volume) else need("SERVICE_TOKEN_SEED")
    return ServiceTokenSource(need("SUPABASE_URL"), volume_path=volume, seed=seed)
