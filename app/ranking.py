"""Working out what you like.

Each meal is shown once. Either you reject it with the X -- it drops below the
daily fallback and is never asked about or ordered again -- or you give it a
score from 1 to 100 on a slider. That is the whole questionnaire: one screen per
meal, and every X makes the rest shorter.

Meals whose scores land within TIE_WINDOW points of each other are *not* put to
a head-to-head question. Two dishes a point or two apart are near enough that
either is a fine pick, so the list simply highlights them and moves on. If the
order between them does matter to you, drag them -- a dragged meal is pinned
where you put it and later re-sorts leave it alone.

New dishes appear each month. They slot into the list at the position their
score implies, flagged until you have looked at them, so keeping up is a few
slider screens rather than starting over.
"""

from __future__ import annotations

import logging

from . import db

log = logging.getLogger("prehrana.ranking")

#: Scores this close are treated as too similar to be worth separating. Meals
#: within this many points of a neighbour are highlighted in the list rather
#: than being turned into a question. Narrowing it highlights fewer pairs.
TIE_WINDOW = 2

MIN_SCORE = 1
MAX_SCORE = 100


# --- stored answers ------------------------------------------------------


def excluded_meals(user_id):
    return {
        r["meal_id"]
        for r in db.query("SELECT meal_id FROM exclusion WHERE user_id = ?", (user_id,))
    }


def get_ratings(user_id):
    """{meal_id: score} for meals still in play."""
    excluded = excluded_meals(user_id)
    known = db.load_meals()
    out = {}
    for row in db.query("SELECT meal_id, score FROM rating WHERE user_id = ?", (user_id,)):
        meal_id = db.canonical_meal_id(row["meal_id"])
        if meal_id in excluded or meal_id not in known:
            continue
        # A merge can leave two ratings pointing at one meal; keep the higher.
        out[meal_id] = max(out.get(meal_id, 0), row["score"])
    return out


def unacknowledged(user_id):
    """Meals that appeared after onboarding and have not been placed by hand."""
    excluded = excluded_meals(user_id)
    known = db.load_meals()
    out = set()
    for row in db.query(
        "SELECT meal_id FROM rating WHERE user_id = ? AND acknowledged = 0", (user_id,)
    ):
        meal_id = db.canonical_meal_id(row["meal_id"])
        if meal_id in known and meal_id not in excluded:
            out.add(meal_id)
    return out


def save_rating(user_id, meal_id, score):
    """Record a slider score.

    A meal rated after onboarding is left unacknowledged so the list can flag it
    as newly arrived until you have had a chance to place it.
    """
    score = max(MIN_SCORE, min(MAX_SCORE, int(score)))
    user = db.query_one("SELECT onboarded_at FROM app_user WHERE id = ?", (user_id,))
    arrived_later = bool(user and user["onboarded_at"])
    db.execute(
        """INSERT INTO rating (user_id, meal_id, score, acknowledged, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, meal_id) DO UPDATE SET
             score = excluded.score, updated_at = excluded.updated_at""",
        (user_id, meal_id, score, 0 if arrived_later else 1, db.now(), db.now()),
    )
    refresh_ranking(user_id)


def exclude(user_id, meal_id):
    """The X button: below the daily fallback, never asked about again.

    The fallback dish is on offer every single day, so anything ranked beneath
    it can never win a day -- which is exactly what "never order this" means.
    """
    db.execute(
        "INSERT OR IGNORE INTO exclusion (user_id, meal_id, created_at) VALUES (?, ?, ?)",
        (user_id, meal_id, db.now()),
    )
    db.execute("DELETE FROM rating WHERE user_id = ? AND meal_id = ?", (user_id, meal_id))
    db.execute("DELETE FROM ranking WHERE user_id = ? AND meal_id = ?", (user_id, meal_id))
    refresh_ranking(user_id)


def unexclude(user_id, meal_id):
    db.execute("DELETE FROM exclusion WHERE user_id = ? AND meal_id = ?", (user_id, meal_id))
    refresh_ranking(user_id)


# --- the order -----------------------------------------------------------


def _pinned_order(user_id):
    """Meals placed by hand, in the order they were left."""
    rows = db.query(
        "SELECT meal_id FROM ranking WHERE user_id = ? AND pinned = 1 ORDER BY position",
        (user_id,),
    )
    return [r["meal_id"] for r in rows]


def compute_order(user_id):
    """The full ranking, best first.

    Meals you have dragged keep the order you left them in. Everything else --
    including anything new since -- is placed by score, slotted in above the
    first hand-placed meal it outscores.
    """
    scores = get_ratings(user_id)
    pinned = [m for m in _pinned_order(user_id) if m in scores]
    pinned_set = set(pinned)

    loose = sorted(
        (m for m in scores if m not in pinned_set),
        key=lambda m: (-scores[m], m),
    )

    if not pinned:
        return loose

    order = list(pinned)
    for meal_id in loose:
        score = scores[meal_id]
        for index, placed in enumerate(order):
            if scores[placed] < score:
                order.insert(index, meal_id)
                break
        else:
            order.append(meal_id)
    return order


def refresh_ranking(user_id):
    """Recompute and store the ranking the picker reads.

    Called after every answer, so ordering can begin as soon as anything is
    rated rather than only once the whole pass is finished.
    """
    order = compute_order(user_id)
    pinned = set(_pinned_order(user_id))
    stamp = db.now()
    db.execute("DELETE FROM ranking WHERE user_id = ?", (user_id,))
    for position, meal_id in enumerate(order):
        db.execute(
            """INSERT INTO ranking (user_id, meal_id, position, pinned, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, meal_id, position, 1 if meal_id in pinned else 0, stamp),
        )


def set_manual_order(user_id, ordered_ids):
    """Save an order the user arranged by hand.

    Everything present is pinned: having taken the trouble to arrange the list,
    it should stay arranged. Meals flagged as newly arrived count as dealt with
    once they have been placed.
    """
    known = set(get_ratings(user_id))
    seen = []
    for meal_id in ordered_ids:
        meal_id = db.canonical_meal_id(meal_id)
        if meal_id in known and meal_id not in seen:
            seen.append(meal_id)
    # Anything the form left out keeps its current relative place, at the end.
    for meal_id in compute_order(user_id):
        if meal_id not in seen:
            seen.append(meal_id)

    stamp = db.now()
    db.execute("DELETE FROM ranking WHERE user_id = ?", (user_id,))
    for position, meal_id in enumerate(seen):
        db.execute(
            """INSERT INTO ranking (user_id, meal_id, position, pinned, updated_at)
               VALUES (?, ?, ?, 1, ?)""",
            (user_id, meal_id, position, stamp),
        )
    db.execute("UPDATE rating SET acknowledged = 1 WHERE user_id = ?", (user_id,))
    return seen


def acknowledge_new(user_id):
    """Clear the "new" flags without changing the order."""
    db.execute("UPDATE rating SET acknowledged = 1 WHERE user_id = ?", (user_id,))


def current_ranking(user_id):
    rows = db.query(
        "SELECT meal_id FROM ranking WHERE user_id = ? ORDER BY position", (user_id,)
    )
    return [r["meal_id"] for r in rows]


def rank_position(user_id):
    """{meal_id: position}, lower is better."""
    return {m: i for i, m in enumerate(current_ranking(user_id))}


def close_to_neighbour(order, scores, window=TIE_WINDOW):
    """Meals scored within `window` of the meal above or below them.

    These are the ones the list highlights. Their relative order came from a
    difference too small to trust, so it is worth showing that rather than
    pretending the ranking is precise there.
    """
    close = set()
    for index, meal_id in enumerate(order):
        score = scores.get(meal_id)
        if score is None:
            continue
        for neighbour in (index - 1, index + 1):
            if 0 <= neighbour < len(order):
                other = scores.get(order[neighbour])
                if other is not None and abs(score - other) <= window:
                    close.add(meal_id)
                    break
    return close


# --- what to ask next ----------------------------------------------------


def unrated_meals(user_id):
    """Meals with no score and no X yet, most-seen first.

    The daily fallback is left out: it is the floor every other dish is judged
    against, not one of the choices, so asking for a score on it means nothing.
    """
    from . import fallback

    excluded = excluded_meals(user_id)
    rated = set(get_ratings(user_id))
    floor = fallback.meal_ids()
    rows = db.query(
        """SELECT id FROM meal
            WHERE id NOT IN (SELECT meal_id FROM meal_alias)
            ORDER BY times_seen DESC, first_seen_date, id"""
    )
    return [
        r["id"] for r in rows
        if r["id"] not in excluded and r["id"] not in rated and r["id"] not in floor
    ]


def next_question(user_id):
    """``("rate", meal_id)`` while anything is unrated, else ``("done", None)``."""
    pending = unrated_meals(user_id)
    if pending:
        return "rate", pending[0]
    return "done", None


def mark_onboarded(user_id):
    """Record that the first full pass is finished.

    Only from this point are later dishes treated as new arrivals worth
    flagging -- during onboarding everything is new, which would be noise.
    """
    # Nothing rated means the menu has not been collected yet, not that the
    # user is finished. Marking them onboarded here would flag every dish that
    # ever arrives as a new arrival.
    if not get_ratings(user_id):
        return False
    row = db.query_one("SELECT onboarded_at FROM app_user WHERE id = ?", (user_id,))
    if row is not None and not row["onboarded_at"]:
        db.execute("UPDATE app_user SET onboarded_at = ? WHERE id = ?", (db.now(), user_id))
        db.execute("UPDATE rating SET acknowledged = 1 WHERE user_id = ?", (user_id,))
        return True
    return False


def progress(user_id):
    """Counts for the progress bar and the dashboard."""
    from . import fallback

    excluded = excluded_meals(user_id)
    # The fallback is never asked about, so counting it would leave the pass
    # permanently one short of finished.
    floor = fallback.meal_ids()
    total = len([
        m for m in db.load_meals() if m not in excluded and m not in floor
    ])
    rated = len([m for m in get_ratings(user_id) if m not in floor])
    return {
        "total": total,
        "rated": rated,
        "to_rate": max(total - rated, 0),
        "excluded": len(excluded),
        "new": len(unacknowledged(user_id)),
        # An empty database is "nothing collected yet", not "finished".
        "done": total > 0 and rated >= total,
    }


def restart(user_id, keep_exclusions=True):
    """Start the whole pass again.

    Exclusions are kept by default: an X means "never ask me about this again",
    so re-asking would contradict it. They can still be undone individually
    from the meal list.
    """
    db.execute("DELETE FROM rating WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM ranking WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM rank_session WHERE user_id = ?", (user_id,))
    db.execute("UPDATE app_user SET onboarded_at = NULL WHERE id = ?", (user_id,))
    if not keep_exclusions:
        db.execute("DELETE FROM exclusion WHERE user_id = ?", (user_id,))
