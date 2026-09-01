"""Tests for classmate suggestions.

The webhook is an external service that can be unset, slow or broken. A
suggestion must survive all three, and text written by a student must not be
able to ping a whole chat server.
"""

from __future__ import annotations

import httpx
import pytest

from app import db, suggestions


@pytest.fixture
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_connection", None)
    monkeypatch.setattr(db.settings, "database_path", tmp_path / "t.db")
    db.init()
    user_id = db.new_id()
    db.execute(
        """INSERT INTO app_user (id, username, class_name, autopilot, created_at)
           VALUES (?, ?, ?, 1, ?)""",
        (user_id, "someone", "R4B", db.now()),
    )
    return db.query_one("SELECT * FROM app_user WHERE id = ?", (user_id,))


@pytest.fixture
def captured(monkeypatch):
    """Capture what would be posted instead of hitting the network."""
    sent = []

    class FakeResponse:
        status_code = 204
        text = ""

    def fake_post(url, json=None, timeout=None):
        sent.append({"url": url, "json": json})
        return FakeResponse()

    monkeypatch.setattr(suggestions.settings, "suggestions_webhook_url",
                        "https://example.invalid/webhook")
    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


def test_a_suggestion_is_stored_and_forwarded(user, captured):
    _id, delivered = suggestions.record(user, "dodaj temni način")
    assert delivered is True
    assert len(captured) == 1
    assert "dodaj temni način" in captured[0]["json"]["content"]

    row = db.query_one("SELECT * FROM suggestion")
    assert row["body"] == "dodaj temni način"
    assert row["delivered"] == 1


def test_the_sender_is_identified(user, captured):
    suggestions.record(user, "ideja")
    content = captured[0]["json"]["content"]
    assert "someone" in content and "R4B" in content


def test_nothing_is_lost_when_no_webhook_is_configured(user, monkeypatch):
    monkeypatch.setattr(suggestions.settings, "suggestions_webhook_url", "")
    _id, delivered = suggestions.record(user, "brez webhooka")
    assert delivered is False
    row = db.query_one("SELECT * FROM suggestion")
    assert row["body"] == "brez webhooka" and row["delivered"] == 0


def test_nothing_is_lost_when_the_webhook_is_unreachable(user, monkeypatch):
    monkeypatch.setattr(suggestions.settings, "suggestions_webhook_url",
                        "https://example.invalid/webhook")

    def boom(url, json=None, timeout=None):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", boom)
    _id, delivered = suggestions.record(user, "webhook je dol")
    assert delivered is False
    assert db.query_one("SELECT * FROM suggestion")["delivered"] == 0


def test_a_rejected_webhook_is_recorded_not_raised(user, monkeypatch):
    monkeypatch.setattr(suggestions.settings, "suggestions_webhook_url",
                        "https://example.invalid/webhook")

    class Rejected:
        status_code = 404
        text = "unknown webhook"

    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: Rejected())
    _id, delivered = suggestions.record(user, "mrtev webhook")
    assert delivered is False
    assert "404" in db.query_one("SELECT * FROM suggestion")["detail"]


def test_failed_suggestions_can_be_resent(user, monkeypatch, captured):
    db.execute(
        """INSERT INTO suggestion (id, user_id, username, body, delivered, created_at)
           VALUES (?, ?, 'someone', 'stara ideja', 0, ?)""",
        (db.new_id(), user["id"], db.now()),
    )
    assert suggestions.resend_failed() == 1
    assert db.query_one("SELECT delivered FROM suggestion")["delivered"] == 1


def test_mass_mentions_cannot_be_smuggled_through(user, captured):
    """A suggestion must not be able to ping everyone in the channel."""
    suggestions.record(user, "@everyone poglejte to @here")
    content = captured[0]["json"]["content"]
    assert "@everyone" not in content
    assert "@here" not in content
    # The reader should still be able to see what was written.
    assert "everyone" in content and "here" in content
    assert captured[0]["json"]["allowed_mentions"] == {"parse": []}


def test_overlong_text_is_capped(user, captured):
    suggestions.record(user, "x" * (suggestions.MAX_LENGTH + 500))
    assert len(db.query_one("SELECT body FROM suggestion")["body"]) == suggestions.MAX_LENGTH


def test_an_empty_suggestion_is_refused(user, captured):
    with pytest.raises(ValueError):
        suggestions.record(user, "   ")
    assert db.query_one("SELECT COUNT(*) AS n FROM suggestion")["n"] == 0


def test_one_account_cannot_flood_the_channel(user, captured):
    for i in range(suggestions.RATE_LIMIT_PER_HOUR):
        suggestions.record(user, "ideja {}".format(i))
    with pytest.raises(suggestions.RateLimited):
        suggestions.record(user, "ena preveč")
    assert len(captured) == suggestions.RATE_LIMIT_PER_HOUR
