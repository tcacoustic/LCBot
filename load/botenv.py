"""Import the bot's own modules without a bot: harmless config, nothing started, nothing reached.

core.py reads its config and builds the data module at import, so the load and the parity check
give it placeholders first. Nothing here talks to Telegram or the database.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def placeholders(schema: str = "lc_bot_dev") -> None:
    for k, v in {"BOT_TOKEN": "1:unused", "GROUP_CHAT_ID": "-100", "DB_SCHEMA": schema,
                 "SUPABASE_URL": "http://unused.invalid", "SUPABASE_PUBLISHABLE_KEY": "unused",
                 "SERVICE_TOKEN_SEED": "unused",
                 "SERVICE_TOKEN_VOLUME_PATH": os.path.join(tempfile.mkdtemp(), "token.json")}.items():
        os.environ.setdefault(k, v)


def bot_module(name: str, *, schema: str = "lc_bot_dev"):
    """One of this checkout's modules (core, onsite, data ...)."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    placeholders(schema)
    return importlib.import_module(name)
