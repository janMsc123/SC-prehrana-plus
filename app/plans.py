"""Menu plans: which slots the picker is allowed to choose from.

The ranking says what you like; a plan narrows where to look for it. "Vegi"
means MALICA 2 every day, whatever else is on; "hladna malica" means only the
cold lines, MALICA 5 and 6, whichever of the two you rank higher. A plan is a
per-user setting and applies to every day that has no choice made by hand --
a hand-made choice still beats it, exactly as it beats the ranking.

Each plan is two things:

  * `slots`    -- the slot names it may choose from (None = all of them);
  * `fallback` -- the slot it takes when nothing it may choose from is ranked.

The rule is the same for every plan: the best-ranked, not-excluded dish among
the allowed slots wins; failing that, the plan's own fallback; failing that,
the school-wide floor (MALICA 7). Because a fallback ignores the ranking, the
vegi plan's fallback being MALICA 2 is what makes it "MALICA 2 every day".

`skip_floor` is the one switch that changes the floor itself. On, a day where
the floor would be ordered is signed off instead -- no lunch rather than
MALICA 7. It only ever replaces the floor: a plan's own fallback (MALICA 5 for
hladna, MALICA 2 for vegi) is a choice the plan makes and is still ordered.
"""

from __future__ import annotations

import json
from collections import OrderedDict

from . import fallback
from .config import settings

#: Stored instead of a slot name for "sign the day off" as a custom fallback.
SKIP = "__skip__"

SLOT_NAMES = tuple("MALICA {}".format(n) for n in range(1, 8))

PLANS = OrderedDict([
    ("ranking", {
        "label": "Po lestvici",
        "short": "Lestvica",
        "about": "Vsak dan najvišja jed s tvoje lestvice, ne glede na to, "
                 "v katerem meniju je.",
        "slots": None,
        "fallback": None,
    }),
    ("vegi", {
        "label": "Vegi meni",
        "short": "Vegi",
        "about": "Vsak dan MENI 2, vegetarijanski.",
        "slots": ("MALICA 2",),
        "fallback": "MALICA 2",
    }),
    ("hladna", {
        "label": "Hladna malica",
        "short": "Hladna",
        "about": "MENI 5 ali MENI 6, kateri je višje na tvoji lestvici. "
                 "Če ni nobeden, MENI 5.",
        "slots": ("MALICA 5", "MALICA 6"),
        "fallback": "MALICA 5",
    }),
    ("custom", {
        "label": "Po meri",
        "short": "Po meri",
        "about": "Sam izbereš menije, med katerimi se izbira po lestvici, "
                 "in kaj se naroči, ko nobeden ne ustreza.",
        "slots": None,
        "fallback": None,
    }),
])


def _norm(name):
    return " ".join((name or "").split()).casefold()


def slot_label(name):
    """"MALICA 5" -> "MENI 5": the word everyone at school actually uses."""
    return (name or "").replace("MALICA", "MENI")


class Plan:
    def __init__(self, key, slots, fallback_slot, skip_floor):
        self.key = key
        self.slots = None if slots is None else {_norm(s) for s in slots}
        self.fallback_slot = fallback_slot
        self.skip_floor = bool(skip_floor)

    @property
    def label(self):
        return PLANS[self.key]["label"]

    def allows(self, slot_name):
        return self.slots is None or _norm(slot_name) in self.slots


def _column(user_row, name, default=None):
    keys = user_row.keys() if hasattr(user_row, "keys") else ()
    return user_row[name] if name in keys and user_row[name] is not None else default


def custom_slots(user_row):
    raw = _column(user_row, "plan_slots")
    try:
        chosen = json.loads(raw) if raw else []
    except ValueError:
        chosen = []
    return [s for s in SLOT_NAMES if s in chosen]


def for_user(user_row, key=None, skip_floor=None):
    """The plan a user follows. `key`/`skip_floor` override the stored ones,
    which is how the menu page previews a plan before it is chosen."""
    key = key or _column(user_row, "menu_plan", "ranking")
    if key not in PLANS:
        key = "ranking"
    if skip_floor is None:
        skip_floor = _column(user_row, "skip_floor", 0)
    if key == "custom":
        slots = custom_slots(user_row) or None
        fallback_slot = _column(user_row, "plan_fallback") or None
        return Plan(key, slots, fallback_slot, skip_floor)
    spec = PLANS[key]
    return Plan(key, spec["slots"], spec["fallback"], skip_floor)


def choose(slots, plan):
    """Pick one day's slot under a plan.

    `slots` are dicts with `slot_name`, `position` (None = unranked) and
    `excluded`. Returns `(slot, reason)`:

      ("ranked")         best-ranked allowed dish
      ("plan_fallback")  the plan's own fallback, nothing allowed was ranked
      ("fallback")       the school-wide floor
      (None, "skip")     the floor would have been ordered, but skip_floor is on
      (None, None)       nothing at all could be chosen
    """
    best = None
    for slot in slots:
        if not plan.allows(slot["slot_name"]) or slot["excluded"]:
            continue
        if slot["position"] is None:
            continue
        if best is None or slot["position"] < best["position"]:
            best = slot
    if best is not None:
        return best, "ranked"

    if plan.fallback_slot == SKIP:
        return None, "skip"

    if plan.fallback_slot and not fallback.is_fallback_slot(plan.fallback_slot):
        own = next(
            (s for s in slots if _norm(s["slot_name"]) == _norm(plan.fallback_slot)),
            None,
        )
        if own is not None:
            return own, "plan_fallback"

    floor = next((s for s in slots if fallback.is_fallback_slot(s["slot_name"])), None)
    if floor is None:
        return None, None
    if plan.skip_floor:
        return None, "skip"
    return floor, "fallback"


def floor_label():
    return slot_label(settings.fallback_slot_name) or "rezerva"
