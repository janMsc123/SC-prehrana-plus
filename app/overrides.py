"""Meals chosen by hand for a particular date.

The ranking answers "what do you like in general"; an override answers "on this
one day, order that". It is deliberately the stronger of the two: an override
wins over the ranking, over an unranked dish, and even over an exclusion --
picking a dish by hand on a date is a clearer statement of intent than a
blanket rule made weeks earlier.

Overrides are keyed by date, not by meal, so there is at most one per day and
setting a new one replaces the old. One of them, `SKIP`, names no meal at all:
it means "order nothing on this date", which is the only way to come out below
the daily fallback, since the fallback otherwise guarantees you lunch. They are stored against `slot_menu_id`
because that is what the school's order call takes; the meal id rides along so
the choice can still be named in the UI.

The day board on the dashboard is built from the local `observation` archive
rather than from a live call to the school, so the page stays fast and works
even when MALCOMAT is down. The consequence is that it can only show dates the
collector has already seen -- a day the archive has never recorded simply is
not offered.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, timedelta

from . import db, ranking

#: How far ahead the day board looks. The school publishes roughly three weeks,
#: and the collector sweeps 28 days, so this matches what can actually be there.
HORIZON_DAYS = 28

#: Stored where a slot id would go, to mean "order nothing that day".
#:
#: Saying no is a choice like any other, so it is kept as an override rather
#: than as a second table: one row per user per date either names a slot or
#: says this. It can never collide with a real slot id, which is a uuid.
SKIP = "__skip__"


def is_skip(slot_menu_id):
    return slot_menu_id == SKIP


# --- storage -------------------------------------------------------------


def get(user_id, order_date):
    return db.query_one(
        "SELECT * FROM override WHERE user_id = ? AND order_date = ?",
        (user_id, str(order_date)),
    )


def for_dates(user_id, dates=None):
    """Overrides as {order_date: row}, optionally limited to `dates`."""
    rows = db.query("SELECT * FROM override WHERE user_id = ?", (user_id,))
    wanted = {str(d) for d in dates} if dates is not None else None
    return {
        row["order_date"]: row
        for row in rows
        if wanted is None or row["order_date"] in wanted
    }


def set_override(user_id, order_date, slot_menu_id, meal_id=None):
    db.execute(
        """INSERT INTO override (user_id, order_date, slot_menu_id, meal_id,
                                 created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id, order_date) DO UPDATE SET
             slot_menu_id = excluded.slot_menu_id,
             meal_id      = excluded.meal_id,
             created_at   = excluded.created_at""",
        (user_id, str(order_date), slot_menu_id, meal_id, db.now()),
    )


def clear(user_id, order_date):
    db.execute(
        "DELETE FROM override WHERE user_id = ? AND order_date = ?",
        (user_id, str(order_date)),
    )


def clear_past(user_id, today=None):
    """Drop overrides for days that have already been and gone.

    Nothing depends on this -- a stale row is simply never looked at again --
    but it keeps the table from growing a line per school day forever.
    """
    today = today or date.today()
    db.execute(
        "DELETE FROM override WHERE user_id = ? AND order_date < ?",
        (user_id, str(today)),
    )


# --- the day board -------------------------------------------------------


def _auto_choice(slots, positions, excluded):
    """Which slot the picker would take on its own, or None.

    Mirrors the rule in picker.plan: best ranked position wins, exclusions and
    unranked dishes are passed over, and when nothing clears the floor the
    daily fallback is taken. Kept here so the board shows the same answer
    the picker will reach, without a network call.
    """
    from . import fallback

    best = None
    best_position = None
    for slot in slots:
        if slot["meal_id"] in excluded:
            continue
        position = positions.get(slot["meal_id"])
        if position is None:
            continue
        if best is None or position < best_position:
            best, best_position = slot, position
    if best is None:
        best = next(
            (s for s in slots if fallback.is_fallback_slot(s["slot_name"])), None
        )
    return best


def days(user_row, today=None, horizon=HORIZON_DAYS):
    """Upcoming archived days, each with its slots and the current choice.

    Returns a list of dicts ordered by date. Only dates from today onward are
    included -- a day whose cutoff has passed cannot be changed, so offering it
    would only invite a click that does nothing.
    """
    today = today or date.today()
    end = today + timedelta(days=horizon)

    params = [str(today), str(end)]
    sql = (
        "SELECT * FROM observation WHERE menu_date >= ? AND menu_date <= ?"
    )
    location_id = user_row["location_id"] if "location_id" in user_row.keys() else None
    if location_id:
        sql += " AND location_id = ?"
        params.append(location_id)
    sql += " ORDER BY menu_date, sort_order"

    from . import fallback

    positions = ranking.rank_position(user_row["id"])
    excluded = ranking.excluded_meals(user_row["id"])
    chosen = for_dates(user_row["id"])

    by_date = OrderedDict()
    for row in db.query(sql, tuple(params)):
        meal_id = db.canonical_meal_id(row["meal_id"])
        meal = db.query_one("SELECT head FROM meal WHERE id = ?", (meal_id,))
        by_date.setdefault(row["menu_date"], []).append(
            {
                "slot_menu_id": row["slot_menu_id"],
                "slot_name": row["slot_name"],
                "location_name": row["location_name"],
                "meal_id": meal_id,
                "name": (meal["head"] if meal else None) or row["description_raw"],
                "description": row["description_raw"],
                "position": positions.get(meal_id),
                "excluded": meal_id in excluded,
                # The floor is never rated, so "not rated" would misdescribe it.
                "is_fallback": fallback.is_fallback_slot(row["slot_name"]),
            }
        )

    out = []
    for menu_date, slots in by_date.items():
        override_row = chosen.get(menu_date)
        override_slot = override_row["slot_menu_id"] if override_row else None
        auto = _auto_choice(slots, positions, excluded)

        for slot in slots:
            slot["is_override"] = slot["slot_menu_id"] == override_slot
            slot["is_auto"] = auto is not None and slot["slot_menu_id"] == auto["slot_menu_id"]

        skipped = is_skip(override_slot)
        out.append(
            {
                "date": menu_date,
                "weekday": date.fromisoformat(menu_date).weekday(),
                "location": next(
                    (s["location_name"] for s in slots if s["location_name"]), None
                ),
                "slots": slots,
                "auto": auto,
                "override": next(
                    (s for s in slots if s["is_override"]), None
                ),
                # Signed off this day: nothing is ordered, not even the floor.
                "skipped": skipped,
                # An override whose slot is no longer on the menu for that day:
                # the school changed the offer after the choice was made. A
                # skip never matches a slot and is not stale for that reason.
                "override_stale": bool(override_slot)
                and not skipped
                and not any(s["is_override"] for s in slots),
            }
        )
    return out
