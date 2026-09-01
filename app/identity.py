"""
Meal identity: deciding when two menu descriptions are the same dish.

A meal's identity is its description text, never its slot number. MALICA 3 on
Monday and MALICA 3 on Tuesday are unrelated dishes; the same dish reappears in
a different slot next month. The school's API confirms this -- `menu_id` is
constant per slot across every date, so it identifies the *position*, not the
food.

Getting this wrong is expensive and silent. One dish split into two records
splits its ranking in half and neither half is right. So the rule here is:

    auto-merge ONLY on an exact match of the core component set.
    anything merely similar becomes a question for the user.
    never merge on similarity alone.

Validated against 93 real menu observations (75 distinct dishes): zero false
questions, while catching typos, dropped components and allergen-list churn.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Tuning constants.
#
# FILLER is deliberately FROZEN in code rather than learned from the data.
# If it were recomputed as observations accumulated, previously stored
# fingerprints would silently change meaning and one dish would split into two
# records -- the exact failure this module exists to prevent. Changing this set
# is a schema migration: bump FINGERPRINT_VERSION and re-run the refingerprint
# maintenance command.
# ---------------------------------------------------------------------------

#: Components that appear alongside almost every meal and carry no signal about
#: which dish it is. Measured frequencies in the observed data:
#: NAPITEK 85%, SADJE 53%, JOGURT 49%, KRUH 39%, MENI SOLATA 32%.
#: Kept deliberately small -- an over-broad filler set merges distinct dishes,
#: which is the more damaging of the two possible errors.
FILLER = frozenset({
    "NAPITEK",       # generic drink
    "KRUH",          # bread
    "SADJE",         # fruit
    "JOGURT",        # yoghurt
    "MENI SOLATA",   # side salad
})

FINGERPRINT_VERSION = 1

#: Two components are "the same component" above this string similarity.
#: Catches typos and punctuation drift without conflating CARBONARA/ARRABIATA.
COMPONENT_MATCH = 0.88

#: Overall similarity at or above which two different fingerprints become a
#: question. Below it they are treated as genuinely different dishes.
ASK_THRESHOLD = 0.60

_PAREN = re.compile(r"\([^)]*\)")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")
_WS = re.compile(r"\s+")

_PAREN_SENTINEL = "\x00P{}\x00"
_DECIMAL_SENTINEL = "\x00D\x00"


def split_components(description):
    """Split a raw menu description into its component parts.

    Descriptions look like::

        NJOKI (jajca, psenica), KRUH (psenica, soja), OMAKA CARBONARA (mleko)

    Components are comma-separated, but commas also appear *inside* the
    parenthesised allergen lists and inside decimals (JABOLCNI SOK 0,2L).
    Both are masked before splitting so they do not fragment a component.
    """
    if not description:
        return []

    holes = {}

    def _stash(match):
        key = _PAREN_SENTINEL.format(len(holes))
        holes[key] = match.group(0)
        return key

    masked = _PAREN.sub(_stash, description)
    masked = _DECIMAL_COMMA.sub(_DECIMAL_SENTINEL, masked)

    out = []
    for part in masked.split(","):
        part = part.replace(_DECIMAL_SENTINEL, ",")
        for key, original in holes.items():
            part = part.replace(key, original)
        part = part.strip().strip(",").strip()
        if part:
            out.append(part)
    return out


def normalise(component):
    """Reduce a component to its comparable form.

    Drops parenthesised annotations -- these hold allergen lists (mleko,
    psenica) and meat declarations (Svinjina). Both describe the same dish and
    both get re-typed by the kitchen over time, so including them would split a
    dish whenever the allergen text was edited.
    """
    text = _PAREN.sub("", component).replace("*", "")
    text = _WS.sub(" ", text)
    return text.strip(" ,.-").upper()


@dataclass(frozen=True)
class ParsedMeal:
    """A menu description reduced to its identity."""

    raw: str
    components: tuple
    core: tuple
    head: str
    fingerprint: str

    @property
    def core_set(self):
        return set(self.core)


def parse(description):
    """Parse a raw description into its identity form."""
    raw = (description or "").strip()
    components = tuple(
        c for c in (normalise(p) for p in split_components(raw)) if c
    )
    core_list = [c for c in components if c not in FILLER]
    # A meal made entirely of filler (e.g. just bread and a drink) still needs
    # an identity, so fall back to the full component list rather than empty.
    if not core_list:
        core_list = list(components)

    core = tuple(sorted(set(core_list)))
    head = components[0] if components else ""
    fingerprint = " | ".join(core)
    return ParsedMeal(
        raw=raw,
        components=components,
        core=core,
        head=head,
        fingerprint=fingerprint,
    )


def _similarity(a, b):
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def fuzzy_jaccard(a_core, b_core, threshold=COMPONENT_MATCH):
    """Set overlap that tolerates small text differences within a component.

    A plain Jaccard index treats CORDON BLUE and CORDON BLU as unrelated, so a
    single typo would look like a brand new dish. This pairs each component
    with its closest counterpart and counts near-identical pairs as matches,
    while genuinely different components (CARBONARA vs ARRABIATA, scoring
    ~0.5) stay unpaired.
    """
    a_list, b_list = list(a_core), list(b_core)
    if not a_list and not b_list:
        return 0.0

    used = set()
    matches = 0
    for a in a_list:
        best_score, best_index = 0.0, None
        for i, b in enumerate(b_list):
            if i in used:
                continue
            score = _similarity(a, b)
            if score > best_score:
                best_score, best_index = score, i
        if best_index is not None and best_score >= threshold:
            used.add(best_index)
            matches += 1

    denominator = len(a_list) + len(b_list) - matches
    return matches / denominator if denominator else 0.0


#: Verdicts returned by compare().
SAME = "same"            # identical identity -- safe to merge without asking
ASK = "ask"              # plausibly the same dish -- must be confirmed by a human
DIFFERENT = "different"  # treat as unrelated dishes


def compare(a, b):
    """Decide how two parsed meals relate.

    Returns ``(verdict, score, reason)``. The only path to an automatic merge
    is an exact core-set match; everything else is either a question or a
    decision to keep the dishes apart.
    """
    if a.core == b.core:
        return SAME, 1.0, "identical core components"

    score = fuzzy_jaccard(a.core, b.core)
    if score >= ASK_THRESHOLD:
        # A perfect fuzzy score with differing text means every component found
        # a near-twin -- almost always a spelling change rather than a new dish.
        if score >= 0.999:
            reason = "same components, spelled slightly differently"
        else:
            reason = "{:.0%} of the components match".format(score)
        return ASK, score, reason

    # One dish's components being a strict subset of the other's, under the
    # same headline dish, usually means a side was added to or dropped from the
    # listing rather than a different dish being served.
    a_set, b_set = a.core_set, b.core_set
    if a_set and b_set and (a_set < b_set or b_set < a_set):
        head_score = _similarity(a.head, b.head)
        if head_score >= COMPONENT_MATCH:
            return ASK, max(score, head_score), (
                "same main dish, one lists extra components"
            )

    return DIFFERENT, score, "only {:.0%} of the components match".format(score)


def find_match(candidate, known):
    """Match a newly observed meal against already-known meals.

    ``known`` maps meal id -> parsed meal. Returns
    ``(verdict, meal_id, score, reason)``. An exact fingerprint hit wins
    outright; otherwise the single strongest ASK candidate is returned so the
    user is asked once, not once per lookalike.
    """
    for meal_id, existing in known.items():
        if existing.core == candidate.core:
            return SAME, meal_id, 1.0, "identical core components"

    best = None
    for meal_id, existing in known.items():
        verdict, score, reason = compare(candidate, existing)
        if verdict == ASK and (best is None or score > best[0]):
            best = (score, meal_id, reason)

    if best is not None:
        return ASK, best[1], best[0], best[2]
    return DIFFERENT, None, 0.0, "no similar meal known"


def display_name(parsed, max_length=80):
    """A short human label for a meal, used in the ranking UI."""
    if not parsed.components:
        return "(empty menu)"
    name = parsed.head or parsed.components[0]
    if len(name) > max_length:
        name = name[: max_length - 1].rstrip() + "…"
    return name
