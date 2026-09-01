"""Login, sessions, and credential storage.

You log in with your school account: the credentials are checked against
MALCOMAT itself, so there is no separate password to remember and this app
never invents its own account system.

The weekly picker has to log in as you while you are asleep, so the school
password is stored, encrypted with a key derived from SECRET_KEY. That is
reversible encryption, not hashing -- it has to be, because the plaintext is
needed to authenticate later. Consequences worth being honest about:

  * anyone who can read both the database file and SECRET_KEY can recover the
    password, so keep .env out of version control and off shared volumes;
  * changing SECRET_KEY makes every stored password unreadable, and users will
    be asked to log in again.

Storing the password is optional per user: "forget my password" erases it, at
the cost of the weekly job no longer being able to act for you. Simply
switching the ordering off leaves it in place, so the switch can be flipped
back without logging in again.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import db
from .config import settings
from .malcomat import AuthError, verify_credentials

log = logging.getLogger("prehrana.auth")

SESSION_COOKIE = "prehrana_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

_fernet = Fernet(settings.fernet_key)
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="prehrana-session")


# --- credential storage --------------------------------------------------


def encrypt_password(password):
    return _fernet.encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_password(blob):
    """Return the stored password, or None if it cannot be read.

    An unreadable blob almost always means SECRET_KEY changed. That is not a
    crash -- the user simply has to log in again.
    """
    if not blob:
        return None
    try:
        return _fernet.decrypt(blob.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        log.warning("stored password could not be decrypted; SECRET_KEY may have changed")
        return None


# --- sessions ------------------------------------------------------------


def make_session(user_id):
    return _serializer.dumps({"uid": user_id})


def read_session(token):
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return data.get("uid")


# --- accounts ------------------------------------------------------------


def login(username, password, remember=True):
    """Verify against the school system and upsert the local account.

    Raises AuthError if the school rejects the credentials, so a bad password
    never results in a stored account.
    """
    profile = verify_credentials(username, password)

    existing = db.query_one(
        "SELECT * FROM app_user WHERE username = ?", (username,)
    )
    user_id = existing["id"] if existing else db.new_id()
    encrypted = encrypt_password(password) if remember else None

    if existing:
        db.execute(
            """UPDATE app_user
                  SET password_enc   = COALESCE(?, password_enc),
                      school_user_id = ?, first_name = ?, last_name = ?,
                      class_name = ?, location_id = ?, last_login_at = ?
                WHERE id = ?""",
            (
                encrypted,
                profile.get("id"),
                profile.get("first_name"),
                profile.get("last_name"),
                profile.get("class_name"),
                profile.get("default_location_id"),
                db.now(),
                user_id,
            ),
        )
    else:
        db.execute(
            """INSERT INTO app_user (id, username, password_enc, school_user_id,
                                     first_name, last_name, class_name,
                                     location_id, autopilot, created_at,
                                     last_login_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                user_id,
                username,
                encrypted,
                profile.get("id"),
                profile.get("first_name"),
                profile.get("last_name"),
                profile.get("class_name"),
                profile.get("default_location_id"),
                db.now(),
                db.now(),
            ),
        )

    return db.query_one("SELECT * FROM app_user WHERE id = ?", (user_id,))


def set_autopilot(user_id, enabled):
    """Turn the weekly ordering on or off.

    Deliberately does not touch the stored password. This is a switch people
    will flip both ways -- off for a week away, on again after -- and wiping
    credentials on every "off" would mean logging in again each time. Use
    :func:`forget_password` to actually remove them.
    """
    db.execute(
        "UPDATE app_user SET autopilot = ? WHERE id = ?",
        (1 if enabled else 0, user_id),
    )


def forget_password(user_id):
    """Erase the stored password and stop acting on this account.

    The account, its scores and its history stay; only the ability to log in on
    your behalf goes away, so the weekly job is switched off with it.
    """
    db.execute(
        "UPDATE app_user SET password_enc = NULL, autopilot = 0 WHERE id = ?",
        (user_id,),
    )


def mark_tutorial_seen(user_id):
    db.execute(
        "UPDATE app_user SET tutorial_seen_at = ? WHERE id = ?", (db.now(), user_id)
    )


def get_user(user_id):
    if not user_id:
        return None
    return db.query_one("SELECT * FROM app_user WHERE id = ?", (user_id,))


__all__ = [
    "AuthError", "SESSION_COOKIE", "SESSION_MAX_AGE", "decrypt_password",
    "encrypt_password", "forget_password", "get_user", "login",
    "make_session", "mark_tutorial_seen", "read_session", "set_autopilot",
]
