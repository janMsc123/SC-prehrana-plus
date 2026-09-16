"""The daily fallback slot -- MALICA 7 at our school.

Everything else in this app identifies a dish by its text and ignores slot
numbers, and that is right: MALICA 3 on Monday and MALICA 3 on Tuesday are
unrelated dishes. The fallback is the one real exception. It is on offer every
single school day, which makes it not a preference but a **floor**:

  * anything you would rather have than the fallback is worth ordering;
  * anything you would not is, by definition, never ordered;
  * and on a day where nothing clears the floor, you still eat -- the fallback
    is ordered rather than the day being skipped.

That is why the fallback is never put to the rating pass. Asking "how much do
you like the floor?" is a question with no useful answer: it is the reference
everything else is measured against, not one of the options.

Identified by slot name rather than by dish, because it is the *slot* that is
guaranteed daily. If no slot matches the configured name on a given day, no
fallback is claimed and the day behaves as it did before -- a wrong name makes
the feature inert instead of ordering something arbitrary.
"""

from __future__ import annotations

import logging

from . import db
from .config import settings

log = logging.getLogger("prehrana.fallback")


def _norm(name):
    return " ".join((name or "").split()).casefold()


def is_fallback_slot(slot_name):
    """Whether this slot is the daily floor."""
    target = _norm(settings.fallback_slot_name)
    return bool(target) and _norm(slot_name) == target


def find_in_menu(slots):
    """The fallback row among one day's live menu rows, or None.

    `slots` are rows as the school returns them, so the slot name lives in
    `menu_name`.
    """
    if not _norm(settings.fallback_slot_name):
        return None
    for slot in slots:
        if is_fallback_slot(slot.get("menu_name")):
            return slot
    return None


def meal_ids():
    """Dish ids that have been seen in the fallback slot.

    Read from the archive, so this only knows what the collector has already
    recorded. Before the first collection it is empty, which simply means the
    rating pass has nothing to skip yet.
    """
    target = _norm(settings.fallback_slot_name)
    if not target:
        return set()
    rows = db.query("SELECT DISTINCT meal_id, slot_name FROM observation")
    return {
        db.canonical_meal_id(row["meal_id"])
        for row in rows
        if row["meal_id"] and _norm(row["slot_name"]) == target
    }
