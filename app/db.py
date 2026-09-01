"""SQLite storage.

One file, no migrations framework -- the schema is created on startup and
extended with additive ALTERs. Keeping it plain makes the whole thing easy to
inspect with the sqlite3 CLI, which matters when the data is the point.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_user (
    id              TEXT PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    password_enc    TEXT,
    school_user_id  TEXT,
    first_name      TEXT,
    last_name       TEXT,
    class_name      TEXT,
    location_id     TEXT,
    autopilot       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT,
    onboarded_at    TEXT,   -- when the first rating pass was completed
    tutorial_seen_at TEXT   -- the one-off explainer has been read
);

-- Every menu slot seen on every date. This is the Phase 1 archive and is
-- append-only: nothing here is ever rewritten once the description is known.
CREATE TABLE IF NOT EXISTS observation (
    id              TEXT PRIMARY KEY,
    menu_date       TEXT NOT NULL,
    slot_menu_id    TEXT NOT NULL,   -- the school's per-slot uuid (NOT the dish)
    slot_name       TEXT NOT NULL,   -- "MALICA 3"
    sort_order      INTEGER NOT NULL,
    location_id     TEXT NOT NULL,
    location_name   TEXT,
    description_raw TEXT NOT NULL,
    meal_id         TEXT,
    first_seen_at   TEXT NOT NULL,
    UNIQUE (menu_date, slot_menu_id, location_id)
);

-- A dish, identified by its text. Slot numbers are deliberately absent.
CREATE TABLE IF NOT EXISTS meal (
    id                  TEXT PRIMARY KEY,
    fingerprint         TEXT NOT NULL UNIQUE,
    fingerprint_version INTEGER NOT NULL,
    head                TEXT NOT NULL,
    components_json     TEXT NOT NULL,
    sample_description  TEXT NOT NULL,
    times_seen          INTEGER NOT NULL DEFAULT 0,
    first_seen_date     TEXT,
    last_seen_date      TEXT,
    created_at          TEXT NOT NULL
);

-- Meals that looked alike but were not identical. Never resolved automatically.
CREATE TABLE IF NOT EXISTS match_question (
    id           TEXT PRIMARY KEY,
    meal_a       TEXT NOT NULL,
    meal_b       TEXT NOT NULL,
    score        REAL NOT NULL,
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|same|different
    created_at   TEXT NOT NULL,
    answered_at  TEXT,
    UNIQUE (meal_a, meal_b)
);

-- Merges the user confirmed. meal_id is folded into canonical_id.
CREATE TABLE IF NOT EXISTS meal_alias (
    meal_id      TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- How much you like a meal, 1-100, from the slider in the rating pass.
CREATE TABLE IF NOT EXISTS rating (
    user_id    TEXT NOT NULL,
    meal_id    TEXT NOT NULL,
    score      INTEGER NOT NULL,
    -- 0 while a newly-appeared dish is still flagged for you to place by hand.
    acknowledged INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, meal_id)
);

-- Head-to-head answers, used only to separate meals whose scores were too
-- close to call.
CREATE TABLE IF NOT EXISTS comparison (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    meal_a     TEXT NOT NULL,
    meal_b     TEXT NOT NULL,
    winner     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- The finished ordering. position 0 is the favourite.
-- `pinned` marks a place you set by hand: dragging a meal fixes it there, and
-- later re-sorts by score must not undo that.
CREATE TABLE IF NOT EXISTS ranking (
    user_id    TEXT NOT NULL,
    meal_id    TEXT NOT NULL,
    position   INTEGER NOT NULL,
    pinned     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, meal_id)
);

-- The X button: never pick this, whatever else is on offer.
CREATE TABLE IF NOT EXISTS exclusion (
    user_id    TEXT NOT NULL,
    meal_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, meal_id)
);

-- Resumable state of an in-progress binary-insertion sort.
CREATE TABLE IF NOT EXISTS rank_session (
    user_id    TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    status     TEXT NOT NULL,  -- active|done
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- What the picker decided, and what happened when it tried to order.
CREATE TABLE IF NOT EXISTS pick (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    order_date   TEXT NOT NULL,
    meal_id      TEXT,
    slot_menu_id TEXT,
    slot_name    TEXT,
    status       TEXT NOT NULL,  -- placed|dry_run|skipped|failed
    detail       TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (user_id, order_date)
);

-- Ideas from whoever is using the instance. Kept locally even when the
-- forwarding webhook is unset or fails, so nothing is silently dropped.
CREATE TABLE IF NOT EXISTS suggestion (
    id          TEXT PRIMARY KEY,
    user_id     TEXT,
    username    TEXT,
    body        TEXT NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_run (
    id         TEXT PRIMARY KEY,
    job        TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ok         INTEGER,
    summary    TEXT
);

CREATE INDEX IF NOT EXISTS idx_obs_date ON observation (menu_date);
CREATE INDEX IF NOT EXISTS idx_obs_meal ON observation (meal_id);
CREATE INDEX IF NOT EXISTS idx_question_status ON match_question (status);
CREATE INDEX IF NOT EXISTS idx_ranking_user ON ranking (user_id, position);
CREATE INDEX IF NOT EXISTS idx_rating_user ON rating (user_id, score DESC);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id():
    return str(uuid.uuid4())


_connection = None


def connect():
    global _connection
    if _connection is None:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        _connection = sqlite3.connect(
            settings.database_path, check_same_thread=False, isolation_level=None
        )
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA foreign_keys=ON")
        _connection.execute("PRAGMA busy_timeout=5000")
    return _connection


#: Columns added after the first release. SQLite cannot add a column that
#: already exists, so each is applied only when missing.
_ADDED_COLUMNS = (
    ("app_user", "onboarded_at", "TEXT"),
    ("app_user", "tutorial_seen_at", "TEXT"),
    ("rating", "acknowledged", "INTEGER NOT NULL DEFAULT 1"),
    ("ranking", "pinned", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate(conn):
    for table, column, definition in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(
            "PRAGMA table_info({})".format(table)
        )}
        if column not in existing:
            conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(
                table, column, definition))


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def transaction():
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def query(sql, params=()):
    return connect().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return connect().execute(sql, params).fetchone()


def execute(sql, params=()):
    return connect().execute(sql, params)


# --- meal aliasing -------------------------------------------------------


def canonical_meal_id(meal_id):
    """Follow confirmed merges to the surviving meal record."""
    seen = set()
    current = meal_id
    while current and current not in seen:
        seen.add(current)
        row = query_one(
            "SELECT canonical_id FROM meal_alias WHERE meal_id = ?", (current,)
        )
        if row is None:
            return current
        current = row["canonical_id"]
    return current


def load_meals():
    """All non-merged meals, as {id: row}."""
    rows = query(
        "SELECT * FROM meal WHERE id NOT IN (SELECT meal_id FROM meal_alias)"
    )
    return {r["id"]: r for r in rows}


def components_of(meal_row):
    return json.loads(meal_row["components_json"])
