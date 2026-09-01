"""Configuration, read once from the environment."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path


def _flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Settings:
    def __init__(self):
        self.secret_key = os.environ.get("SECRET_KEY", "")
        self.base_url = os.environ.get(
            "MALCOMAT_BASE_URL", "https://prehrana.sc-celje.si"
        ).rstrip("/")

        self.database_path = Path(
            os.environ.get("DATABASE_PATH", "data/prehrana.db")
        )

        # When false the picker computes and records its choices but never
        # calls the school's ordering endpoint.
        self.place_orders = _flag("PLACE_ORDERS", True)

        self.collect_hour = _int("COLLECT_CRON_HOUR", 6)
        self.collect_minute = _int("COLLECT_CRON_MINUTE", 30)
        self.pick_day_of_week = os.environ.get("PICK_CRON_DAY_OF_WEEK", "mon")
        self.pick_hour = _int("PICK_CRON_HOUR", 7)
        self.pick_minute = _int("PICK_CRON_MINUTE", 0)

        self.timezone = os.environ.get("TZ", "Europe/Ljubljana")

        # Chat webhook that suggestions are forwarded to. A credential: anyone
        # holding it can post to that channel, so it is read from the
        # environment and must never be committed. Unset simply means
        # suggestions are stored locally and not forwarded.
        self.suggestions_webhook_url = os.environ.get(
            "SUGGESTIONS_WEBHOOK_URL", ""
        ).strip()

        if not self.secret_key:
            # Never silently invent a key in production: stored passwords
            # encrypted under a random key become unreadable on restart.
            if _flag("ALLOW_INSECURE_SECRET"):
                self.secret_key = "insecure-development-key"
            else:
                raise RuntimeError(
                    "SECRET_KEY is not set. Generate one with:\n"
                    '  python -c "import secrets;print(secrets.token_urlsafe(48))"\n'
                    "and put it in your .env file."
                )

    @property
    def fernet_key(self):
        """A stable Fernet key derived from SECRET_KEY."""
        digest = hashlib.sha256(self.secret_key.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)


settings = Settings()
