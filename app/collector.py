"""Phase 1: watch and record.

Logs in, reads the menu on offer, and stores it. It never writes anything back
to the school's system -- the only endpoints it touches are reads.

Its second job is meal identity. Every description is parsed and matched
against what is already known:

  * identical core components  -> the same dish, linked automatically
  * merely similar             -> stored as a SEPARATE dish AND a question is
                                  queued for the user
  * unlike anything known      -> a new dish

The middle case is the important one. Nothing is ever merged because it looks
similar; a lookalike becomes its own record and stays that way until a human
says otherwise. Splitting a dish in two is recoverable (answer the question and
they merge); silently fusing two different dishes corrupts the ranking with no
sign that anything is wrong.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from . import db, identity
from .malcomat import AuthError, Client, MalcomatError

log = logging.getLogger("prehrana.collector")

#: How far either side of today to sweep. The school publishes roughly three
#: weeks ahead; looking back a little catches days added retroactively.
LOOKBACK_DAYS = 7
LOOKAHEAD_DAYS = 28


def _parsed_known_meals():
    """Known meals as {id: ParsedMeal}, rebuilt from stored components."""
    known = {}
    for meal_id, row in db.load_meals().items():
        components = tuple(db.components_of(row))
        core = tuple(sorted({c for c in components if c not in identity.FILLER})) \
            or tuple(sorted(set(components)))
        known[meal_id] = identity.ParsedMeal(
            raw=row["sample_description"],
            components=components,
            core=core,
            head=row["head"],
            fingerprint=row["fingerprint"],
        )
    return known


def _create_meal(parsed, menu_date):
    meal_id = db.new_id()
    db.execute(
        """INSERT INTO meal (id, fingerprint, fingerprint_version, head,
                             components_json, sample_description, times_seen,
                             first_seen_date, last_seen_date, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
        (
            meal_id,
            parsed.fingerprint,
            identity.FINGERPRINT_VERSION,
            parsed.head,
            json.dumps(list(parsed.components), ensure_ascii=False),
            parsed.raw,
            menu_date,
            menu_date,
            db.now(),
        ),
    )
    return meal_id


def _queue_question(meal_a, meal_b, score, reason):
    """Ask the user whether two lookalikes are the same dish.

    Ordered consistently so the same pair cannot be queued twice, and skipped
    if it has already been answered either way.
    """
    a, b = sorted((meal_a, meal_b))
    existing = db.query_one(
        "SELECT status FROM match_question WHERE meal_a = ? AND meal_b = ?", (a, b)
    )
    if existing is not None:
        return False
    db.execute(
        """INSERT INTO match_question (id, meal_a, meal_b, score, reason,
                                       status, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (db.new_id(), a, b, score, reason, db.now()),
    )
    log.info("queued identity question: %s ~ %s (%s)", a, b, reason)
    return True


def resolve_meal(description, menu_date, known=None):
    """Find or create the meal for a description.

    Returns ``(meal_id, verdict, question_queued)``.
    """
    parsed = identity.parse(description)

    # Fast path: this exact identity is already on file.
    row = db.query_one(
        "SELECT id FROM meal WHERE fingerprint = ?", (parsed.fingerprint,)
    )
    if row is not None:
        return db.canonical_meal_id(row["id"]), identity.SAME, False

    if known is None:
        known = _parsed_known_meals()

    verdict, match_id, score, reason = identity.find_match(parsed, known)

    if verdict == identity.SAME and match_id:
        return db.canonical_meal_id(match_id), identity.SAME, False

    # Similar or unlike anything: either way this becomes its own record.
    meal_id = _create_meal(parsed, menu_date)
    known[meal_id] = parsed

    queued = False
    if verdict == identity.ASK and match_id:
        queued = _queue_question(meal_id, match_id, score, reason)

    return meal_id, verdict, queued


def _store_observation(row, meal_id):
    """Append one slot-on-a-date. Returns True if it was new."""
    cursor = db.execute(
        """INSERT OR IGNORE INTO observation
             (id, menu_date, slot_menu_id, slot_name, sort_order, location_id,
              location_name, description_raw, meal_id, first_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            db.new_id(),
            row["menu_date"],
            row["menu_id"],
            row.get("menu_name") or "",
            row.get("sort_order") or 0,
            row["location_id"],
            row.get("location_name"),
            row["menu_description"],
            meal_id,
            db.now(),
        ),
    )
    return cursor.rowcount > 0


def _touch_meal(meal_id, menu_date):
    db.execute(
        """UPDATE meal
              SET times_seen      = times_seen + 1,
                  first_seen_date = MIN(COALESCE(first_seen_date, ?), ?),
                  last_seen_date  = MAX(COALESCE(last_seen_date, ?), ?)
            WHERE id = ?""",
        (menu_date, menu_date, menu_date, menu_date, meal_id),
    )


def ingest(rows):
    """Store menu rows. Pure bookkeeping -- no network, so this is testable."""
    stats = {
        "rows": 0, "new_observations": 0, "new_meals": 0,
        "questions": 0, "skipped_no_menu": 0,
    }
    known = _parsed_known_meals()

    for row in rows:
        stats["rows"] += 1
        description = (row.get("menu_description") or "").strip()
        if not description:
            # Weekends and holidays come back with a null description.
            stats["skipped_no_menu"] += 1
            continue

        before = len(known)
        meal_id, verdict, queued = resolve_meal(description, row["menu_date"], known)
        if len(known) > before:
            stats["new_meals"] += 1
        if queued:
            stats["questions"] += 1

        if _store_observation(row, meal_id):
            stats["new_observations"] += 1
            _touch_meal(meal_id, row["menu_date"])

    return stats


def collect_for_user(user_row, today=None):
    """Log in as one user and archive the menu around today."""
    today = today or date.today()
    start = today - timedelta(days=LOOKBACK_DAYS)
    end = today + timedelta(days=LOOKAHEAD_DAYS)

    password = decrypt_password(user_row["password_enc"])
    if not password:
        raise MalcomatError(
            "no stored password for {}".format(user_row["username"])
        )

    with Client() as client:
        client.login(user_row["username"], password)
        rows = client.get_menus(start, end)

    with db.transaction():
        stats = ingest(rows)
    log.info("collected %s..%s for %s: %s", start, end, user_row["username"], stats)
    return stats


def decrypt_password(blob):
    from .auth import decrypt_password as _decrypt

    return _decrypt(blob)


def run(today=None):
    """The scheduled Phase 1 job: collect for every user with autopilot on."""
    run_id = db.new_id()
    db.execute(
        "INSERT INTO job_run (id, job, started_at) VALUES (?, 'collect', ?)",
        (run_id, db.now()),
    )
    totals = {"users": 0, "new_observations": 0, "new_meals": 0, "questions": 0}
    errors = []

    users = db.query(
        "SELECT * FROM app_user WHERE password_enc IS NOT NULL AND autopilot = 1"
    )
    for user in users:
        try:
            stats = collect_for_user(user, today=today)
            totals["users"] += 1
            for key in ("new_observations", "new_meals", "questions"):
                totals[key] += stats[key]
        except (MalcomatError, AuthError) as exc:
            log.warning("collect failed for %s: %s", user["username"], exc)
            errors.append("{}: {}".format(user["username"], exc))

    summary = json.dumps({"totals": totals, "errors": errors}, ensure_ascii=False)
    db.execute(
        "UPDATE job_run SET ok = ?, summary = ? WHERE id = ?",
        (0 if errors else 1, summary, run_id),
    )
    return totals, errors


def merge_meals(loser_id, winner_id):
    """Fold one meal record into another after the user confirms they match.

    Everything pointing at the loser is repointed at the winner: observations,
    recorded comparisons, exclusions and the ranking. Where both were ranked
    the better position wins, since the two records were always one dish and
    the higher placement is the more considered judgement.
    """
    loser_id = db.canonical_meal_id(loser_id)
    winner_id = db.canonical_meal_id(winner_id)
    if loser_id == winner_id:
        return winner_id

    db.execute("UPDATE observation SET meal_id = ? WHERE meal_id = ?",
               (winner_id, loser_id))
    db.execute("UPDATE comparison SET meal_a = ? WHERE meal_a = ?",
               (winner_id, loser_id))
    db.execute("UPDATE comparison SET meal_b = ? WHERE meal_b = ?",
               (winner_id, loser_id))
    db.execute("DELETE FROM comparison WHERE meal_a = meal_b")

    # Keep the better of the two rankings, per user.
    for row in db.query("SELECT user_id, position FROM ranking WHERE meal_id = ?",
                        (loser_id,)):
        winner_row = db.query_one(
            "SELECT position FROM ranking WHERE user_id = ? AND meal_id = ?",
            (row["user_id"], winner_id),
        )
        if winner_row is None:
            db.execute(
                """INSERT INTO ranking (user_id, meal_id, position, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (row["user_id"], winner_id, row["position"], db.now()),
            )
        elif row["position"] < winner_row["position"]:
            db.execute(
                "UPDATE ranking SET position = ?, updated_at = ? "
                "WHERE user_id = ? AND meal_id = ?",
                (row["position"], db.now(), row["user_id"], winner_id),
            )
    db.execute("DELETE FROM ranking WHERE meal_id = ?", (loser_id,))

    # An exclusion on either record excludes the merged dish.
    for row in db.query("SELECT user_id FROM exclusion WHERE meal_id = ?", (loser_id,)):
        db.execute(
            "INSERT OR IGNORE INTO exclusion (user_id, meal_id, created_at) "
            "VALUES (?, ?, ?)",
            (row["user_id"], winner_id, db.now()),
        )
    db.execute("DELETE FROM exclusion WHERE meal_id = ?", (loser_id,))

    db.execute(
        """UPDATE meal
              SET times_seen      = times_seen +
                    COALESCE((SELECT times_seen FROM meal WHERE id = ?), 0),
                  first_seen_date = MIN(COALESCE(first_seen_date, '9999'),
                    COALESCE((SELECT first_seen_date FROM meal WHERE id = ?), '9999')),
                  last_seen_date  = MAX(COALESCE(last_seen_date, ''),
                    COALESCE((SELECT last_seen_date FROM meal WHERE id = ?), ''))
            WHERE id = ?""",
        (loser_id, loser_id, loser_id, winner_id),
    )

    db.execute(
        "INSERT OR REPLACE INTO meal_alias (meal_id, canonical_id, created_at) "
        "VALUES (?, ?, ?)",
        (loser_id, winner_id, db.now()),
    )
    # Stale ranking sessions would still reference the folded-away id.
    db.execute("DELETE FROM rank_session WHERE status = 'active'")
    log.info("merged meal %s into %s", loser_id, winner_id)
    return winner_id


def answer_question(question_id, same):
    """Resolve one identity question.

    Answering "different" is recorded too, so the same pair is never queued
    again -- otherwise every collection run would re-ask about dishes that were
    already ruled apart.
    """
    row = db.query_one("SELECT * FROM match_question WHERE id = ?", (question_id,))
    if row is None or row["status"] != "pending":
        return None
    with db.transaction():
        if same:
            merge_meals(row["meal_b"], row["meal_a"])
        db.execute(
            "UPDATE match_question SET status = ?, answered_at = ? WHERE id = ?",
            ("same" if same else "different", db.now(), question_id),
        )
    return row
