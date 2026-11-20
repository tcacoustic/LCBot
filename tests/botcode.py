"""Load the bot's own modules against the live test database, for feature tests.

core.py reads its config and builds the data module at import. Here it gets harmless values, and
then its data module is pointed at the local PostgREST as lc_bot_dev_app. Needs python-telegram-bot
installed (requirements.txt); without it these tests are skipped.
"""
from __future__ import annotations

import importlib
import os
import tempfile

from live import LIVE, client

core = None


def load():
    """core (and every feature module through it), or None when the bot's packages aren't installed."""
    global core
    if core is not None or not LIVE:
        return core
    try:
        import telegram  # noqa: F401
    except ImportError:
        return None
    tmp = tempfile.mkdtemp()
    os.environ.update({
        "BOT_TOKEN": "1:test", "GROUP_CHAT_ID": "-100", "DB_SCHEMA": "lc_bot_dev",
        "SUPABASE_URL": "http://unused.invalid", "SUPABASE_PUBLISHABLE_KEY": "unused",
        "SERVICE_TOKEN_VOLUME_PATH": os.path.join(tmp, "token.json"), "SERVICE_TOKEN_SEED": "unused",
        "ADMIN_IDS": "990000099",
    })
    core = importlib.import_module("core")
    core.data.db = client()          # the same object every module star-imported
    return core
