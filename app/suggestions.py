"""Suggestions from whoever is using the instance.

Anyone logged in can send an idea. It is stored locally first and then
forwarded to a chat webhook if one is configured, so a webhook that is unset,
rotated or temporarily failing loses nothing -- the suggestion is still in the
database and can be re-sent.

The webhook URL is a credential: anyone holding it can post into that channel
until it is rotated. It therefore lives in the environment and never in the
repository, which matters here because this project is meant to be forked.
"""

from __future__ import annotations

import logging

import httpx

from . import db
from .config import settings

log = logging.getLogger("prehrana.suggestions")

MAX_LENGTH = 1500

#: Suggestions one account may send per hour, to keep a bored classmate from
#: turning the webhook into a firehose.
RATE_LIMIT_PER_HOUR = 5


class RateLimited(RuntimeError):
    """Too many suggestions from this account too quickly."""


def recent_count(user_id):
    row = db.query_one(
        """SELECT COUNT(*) AS n FROM suggestion
            WHERE user_id = ? AND created_at > datetime('now', '-1 hour')""",
        (user_id,),
    )
    return row["n"] if row else 0


def _sanitise(body):
    """Trim, cap, and defuse anything that would format as a chat mention.

    The text lands in a chat channel, so an @everyone in a suggestion would
    otherwise ping a whole server. Zero-width joiners keep the text readable
    while stopping it resolving as a mention.
    """
    text = (body or "").strip()[:MAX_LENGTH]
    return (
        text.replace("@everyone", "@​everyone")
            .replace("@here", "@​here")
    )


def record(user, body):
    """Store a suggestion and try to forward it. Returns the stored row id."""
    text = _sanitise(body)
    if not text:
        raise ValueError("empty suggestion")

    user_id = user["id"] if user else None
    if user_id and recent_count(user_id) >= RATE_LIMIT_PER_HOUR:
        raise RateLimited(
            "you have sent {} suggestions in the last hour".format(RATE_LIMIT_PER_HOUR)
        )

    who = "anonymous"
    if user:
        who = user["username"] or "?"
        if user["class_name"]:
            who = "{} ({})".format(who, user["class_name"])

    suggestion_id = db.new_id()
    db.execute(
        """INSERT INTO suggestion (id, user_id, username, body, delivered, created_at)
           VALUES (?, ?, ?, ?, 0, ?)""",
        (suggestion_id, user_id, who, text, db.now()),
    )

    delivered, detail = _forward(who, text)
    db.execute(
        "UPDATE suggestion SET delivered = ?, detail = ? WHERE id = ?",
        (1 if delivered else 0, detail, suggestion_id),
    )
    return suggestion_id, delivered


def _forward(who, text):
    """Post to the configured webhook. Never raises -- delivery is best effort."""
    url = settings.suggestions_webhook_url
    if not url:
        return False, "no webhook configured; stored locally only"

    payload = {
        "username": "Prehrana Plus",
        "content": "**Predlog od {}**\n{}".format(who, text),
        # Belt and braces alongside the text substitution above.
        "allowed_mentions": {"parse": []},
    }
    try:
        response = httpx.post(url, json=payload, timeout=10.0)
    except httpx.HTTPError as exc:
        log.warning("suggestion webhook unreachable: %s", exc)
        return False, "webhook unreachable: {}".format(exc)

    if response.status_code >= 400:
        log.warning("suggestion webhook rejected: %s %s",
                    response.status_code, response.text[:120])
        return False, "webhook returned {}".format(response.status_code)
    return True, "delivered"


def resend_failed():
    """Retry suggestions that were stored but never delivered."""
    sent = 0
    for row in db.query("SELECT * FROM suggestion WHERE delivered = 0 ORDER BY created_at"):
        delivered, detail = _forward(row["username"] or "anonymous", row["body"])
        db.execute(
            "UPDATE suggestion SET delivered = ?, detail = ? WHERE id = ?",
            (1 if delivered else 0, detail, row["id"]),
        )
        sent += 1 if delivered else 0
    return sent
