"""Tests for meal identity.

The expensive failure is silent: two different dishes fused into one record, or
one dish split across two. Both corrupt a ranking without any visible error, so
these tests lean on real menu text rather than invented examples.

tests/fixtures/real_menus.json holds 93 observations pulled from the school's
API (14 school days, 75 distinct dishes).
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from app import identity

FIXTURE = Path(__file__).parent / "fixtures" / "real_menus.json"


@pytest.fixture(scope="module")
def real_menus():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def distinct_meals(real_menus):
    """One ParsedMeal per distinct identity in the real data."""
    meals = {}
    for row in real_menus:
        parsed = identity.parse(row["menu_description"])
        meals.setdefault(parsed.fingerprint, parsed)
    return meals


# --- parsing -------------------------------------------------------------


def test_allergen_commas_do_not_split_components():
    parsed = identity.parse("NJOKI (jajca, pšenica), KRUH (pšenica, soja)")
    assert parsed.components == ("NJOKI", "KRUH")


def test_decimal_comma_does_not_split_a_component():
    # "JABOLČNI SOK 0,2L" must stay one component, not become two.
    components = identity.split_components("JABOLČNI SOK 0,2L , JOGURT (mleko)")
    assert components == ["JABOLČNI SOK 0,2L", "JOGURT (mleko)"]


def test_allergens_are_dropped_from_identity():
    a = identity.parse("CORDON BLUE (mleko, pšenica), NAPITEK")
    b = identity.parse("CORDON BLUE (mleko, pšenica, soja), NAPITEK")
    assert a.fingerprint == b.fingerprint


def test_meat_declarations_are_dropped_from_identity():
    a = identity.parse("ČEVAPČIČI (Govedina, Svinjina), KRUH (pšenica)")
    b = identity.parse("ČEVAPČIČI, KRUH (pšenica)")
    assert a.fingerprint == b.fingerprint


def test_filler_does_not_define_a_meal():
    with_filler = identity.parse("MESNA LAZANJA (jajca), KRUH, NAPITEK, SADJE")
    without = identity.parse("MESNA LAZANJA (jajca)")
    assert with_filler.fingerprint == without.fingerprint


def test_meal_made_only_of_filler_still_gets_an_identity():
    parsed = identity.parse("KRUH (pšenica), NAPITEK")
    assert parsed.fingerprint
    assert parsed.core


def test_empty_description_is_handled():
    parsed = identity.parse("")
    assert parsed.components == ()
    assert identity.display_name(parsed) == "(empty menu)"


def test_component_order_does_not_change_identity():
    a = identity.parse("PIZZA S ŠUNKO, NAPITEK, SADJE")
    b = identity.parse("SADJE, PIZZA S ŠUNKO, NAPITEK")
    assert a.fingerprint == b.fingerprint


# --- the merge rule ------------------------------------------------------


def test_identical_text_is_the_same_meal():
    text = "MESNA LAZANJA (jajca, mleko), KRUH (pšenica, soja), NAPITEK"
    verdict, _score, _why = identity.compare(identity.parse(text), identity.parse(text))
    assert verdict == identity.SAME


@pytest.mark.parametrize(
    "a, b",
    [
        # Same base, different sauce -- two different meals on the same day.
        ("NJOKI (jajca), OMAKA CARBONARA (mleko), PARMEZAN",
         "NJOKI (jajca), OMAKA ARRABIATA (soja), PARMEZAN"),
        # Tofu vs chicken: one word apart, entirely different meal.
        ("TERIYAKI STICKI TOFU RIŽ (soja), KRUH",
         "TERIYAKI STICKI PIŠČANČJI RIŽ (soja), KRUH"),
        # Vegetarian vs meat.
        ("ZELENJAVNA LAZANJA (jajca), KRUH", "MESNA LAZANJA (jajca), KRUH"),
        ("CURRY S ČIČERIKO IN EKO BULGUR (mleko)", "CURRY S PIŠČANCEM (mleko)"),
        ("ZELENJAVNA MUSAKA", "MESNA MUSAKA"),
        ("CHEESBURGER (jajca)", "HAMBURGER (jajca)"),
        ("ŠPAGETI S TUNINO OMAKO, PARMEZAN", "ŠPAGETI PO BOLONJSKO, PARMEZAN"),
        ("WRAP MOZZARELLA - PESTO, PUDING BREZ SMETANE",
         "WRAP S HUMUSOM IN ZELENJAVO, PUDING BREZ SMETANE"),
    ],
)
def test_genuinely_different_dishes_are_never_merged(a, b):
    """These pairs really are different meals. Fusing them would be silent damage."""
    verdict, _score, _why = identity.compare(identity.parse(a), identity.parse(b))
    assert verdict != identity.SAME, "must not auto-merge {} with {}".format(a, b)


@pytest.mark.parametrize(
    "a, b",
    [
        # A typo in the dish name should be queried, not treated as a new dish.
        ("CORDON BLUE (mleko), DODATEK LIMONA", "CORDON BLU (mleko), DODATEK LIMONA"),
        # A listed side disappears from an otherwise identical meal.
        ("PIŠČANČJI SAUTE, DUŠEN RIŽ, PARMEZAN", "PIŠČANČJI SAUTE, DUŠEN RIŽ"),
    ],
)
def test_probable_variants_are_queried_not_guessed(a, b):
    verdict, _score, _why = identity.compare(identity.parse(a), identity.parse(b))
    assert verdict == identity.ASK


def test_find_match_prefers_an_exact_identity_over_a_lookalike():
    known = {
        "exact": identity.parse("NJOKI, OMAKA CARBONARA, PARMEZAN"),
        "similar": identity.parse("NJOKI, OMAKA ARRABIATA, PARMEZAN"),
    }
    verdict, meal_id, _score, _why = identity.find_match(
        identity.parse("NJOKI, OMAKA CARBONARA, PARMEZAN, NAPITEK"), known
    )
    assert (verdict, meal_id) == (identity.SAME, "exact")


def test_find_match_reports_nothing_when_the_meal_is_new():
    known = {"a": identity.parse("MESNA LAZANJA")}
    verdict, meal_id, _score, _why = identity.find_match(
        identity.parse("RIČET S KLOBASO, KROF"), known
    )
    assert verdict == identity.DIFFERENT and meal_id is None


# --- behaviour on the real menu -----------------------------------------


def test_real_data_produces_no_spurious_questions(distinct_meals):
    """Every pair of genuinely distinct real dishes must stay distinct.

    Each question here would be a false alarm, and a wrong "same" answer to one
    would merge two real dishes. Zero is the bar.
    """
    asked = [
        (a.head, b.head)
        for a, b in itertools.combinations(distinct_meals.values(), 2)
        if identity.compare(a, b)[0] == identity.ASK
    ]
    assert asked == []


def test_real_data_repeats_are_recognised(real_menus):
    """The dish served every single day must collapse to one identity."""
    by_fingerprint = {}
    for row in real_menus:
        parsed = identity.parse(row["menu_description"])
        by_fingerprint.setdefault(parsed.fingerprint, set()).add(row["menu_date"])

    most_repeated = max(by_fingerprint.values(), key=len)
    assert len(most_repeated) >= 13, "the daily fallback meal should be one record"


def test_the_same_dish_in_different_slots_is_one_meal(real_menus):
    """Slot number must not affect identity.

    In the real data the fallback dish appears as MALICA 7 every day and also as
    MALICA 1 on the first day of term. That is one dish, not two.
    """
    by_fingerprint = {}
    for row in real_menus:
        parsed = identity.parse(row["menu_description"])
        by_fingerprint.setdefault(parsed.fingerprint, set()).add(row["menu_name"])

    multi_slot = [slots for slots in by_fingerprint.values() if len(slots) > 1]
    assert multi_slot, "fixture should contain a dish appearing in two slots"


def test_every_real_description_parses_to_something(real_menus):
    for row in real_menus:
        parsed = identity.parse(row["menu_description"])
        assert parsed.core, "no identity for: {}".format(row["menu_description"][:60])
        assert identity.display_name(parsed) != "(empty menu)"


def test_identity_is_stable_across_repeated_parses(real_menus):
    """Parsing is deterministic -- a re-parse must not invent a new record."""
    for row in real_menus[:30]:
        first = identity.parse(row["menu_description"])
        second = identity.parse(row["menu_description"])
        assert first.fingerprint == second.fingerprint
