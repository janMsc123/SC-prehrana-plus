"""Tests for how a meal is named for display.

identity.py deliberately keeps two dishes that share a headline but differ in
their sides as separate records -- see its own docstring on why merging on
similarity alone is the wrong failure to risk. The consequence, unfixed, was
that both showed up in the rating list and the board as the exact same plain
name (e.g. two rows both just "CEVAPCICI"), which reads as a duplicate bug
even though the underlying records are correctly kept apart. `meal_view`
disambiguates the *display* only, using whichever side actually differs.
"""

from __future__ import annotations

import json

import pytest

from app import db, main


@pytest.fixture
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_connection", None)
    monkeypatch.setattr(db.settings, "database_path", tmp_path / "t.db")
    db.init()


def add_meal(head, extras=()):
    meal_id = db.new_id()
    components = [head, *extras]
    db.execute(
        """INSERT INTO meal (id, fingerprint, fingerprint_version, head,
                             components_json, sample_description, times_seen,
                             created_at)
           VALUES (?, ?, 1, ?, ?, ?, 1, ?)""",
        (meal_id, "fp-" + meal_id, head,
         json.dumps(components, ensure_ascii=False), head, db.now()),
    )
    return meal_id


def test_a_meal_with_no_namesake_is_shown_plain(_db):
    meal_id = add_meal("JOTA", ["KRUH", "NAPITEK"])
    assert main.meal_view(meal_id)["name"] == "JOTA"


def test_two_meals_sharing_a_head_are_told_apart(_db):
    """The reported bug: both looked like the exact same row."""
    with_bombeta = add_meal("ČEVAPČIČI", ["BOMBETA", "KRUH", "NAPITEK"])
    with_rice = add_meal("ČEVAPČIČI", ["DŽUVEČ RIŽ", "KRUH", "NAPITEK"])

    names = {main.meal_view(with_bombeta)["name"], main.meal_view(with_rice)["name"]}

    assert len(names) == 2, "must no longer read as duplicates"
    assert all(n.startswith("ČEVAPČIČI") for n in names)
    assert any("BOMBETA" in n for n in names)
    assert any("DŽUVEČ RIŽ" in n for n in names)


def test_the_disambiguator_skips_generic_filler_sides(_db):
    """KRUH/NAPITEK/etc. ride along on almost every dish and would not help
    tell two meals apart, so the first non-filler extra is used instead.
    """
    a = add_meal("RIŽOTA", ["KRUH", "NAPITEK", "GOBOVA OMAKA"])
    b = add_meal("RIŽOTA", ["KRUH", "NAPITEK", "BUČNA OMAKA"])

    name_a = main.meal_view(a)["name"]
    name_b = main.meal_view(b)["name"]

    assert "GOBOVA OMAKA" in name_a
    assert "BUČNA OMAKA" in name_b


def test_a_merged_alias_is_not_treated_as_a_namesake(_db):
    """A meal merged away via meal_alias must not make the survivor look like
    it still collides with something.
    """
    survivor = add_meal("JOTA", ["KRUH"])
    merged_away = add_meal("JOTA", ["KRUH"])
    db.execute(
        "INSERT INTO meal_alias (meal_id, canonical_id, created_at) VALUES (?, ?, ?)",
        (merged_away, survivor, db.now()),
    )

    assert main.meal_view(survivor)["name"] == "JOTA"


def test_three_way_collision_each_gets_a_distinct_name(_db):
    a = add_meal("PICA", ["ŠUNKA"])
    b = add_meal("PICA", ["TUNA"])
    c = add_meal("PICA", ["ZELENJAVA"])

    names = {main.meal_view(m)["name"] for m in (a, b, c)}
    assert len(names) == 3
