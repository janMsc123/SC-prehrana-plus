"""Tests for picking a meal by hand for one date.

An override is the strongest statement a user can make: it beats the ranking,
it beats a dish never having been rated, and it beats an exclusion. What has to
hold is that it applies only to its own date, that replacing one leaves no
second row behind, and that a slot which vanishes from the school's menu falls
back to the ranking instead of ordering something arbitrary.
"""

from __future__ import annotations

import json

import pytest

from app import db, overrides, picker, ranking


@pytest.fixture
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_connection", None)
    monkeypatch.setattr(db.settings, "database_path", tmp_path / "t.db")
    db.init()
    user_id = db.new_id()
    db.execute(
        """INSERT INTO app_user (id, username, location_id, autopilot, created_at)
           VALUES (?, ?, ?, 1, ?)""",
        (user_id, "tester", "loc1", db.now()),
    )
    return db.query_one("SELECT * FROM app_user WHERE id = ?", (user_id,))


def add_meal(head, components=None):
    meal_id = db.new_id()
    db.execute(
        """INSERT INTO meal (id, fingerprint, fingerprint_version, head,
                             components_json, sample_description, times_seen,
                             created_at)
           VALUES (?, ?, 1, ?, ?, ?, 1, ?)""",
        (meal_id, "fp-" + head, head,
         json.dumps(components or [head]), head, db.now()),
    )
    return meal_id


def observe(menu_date, slot_menu_id, slot_name, meal_id, description,
            location_id="loc1", sort_order=0):
    db.execute(
        """INSERT INTO observation (id, menu_date, slot_menu_id, slot_name,
                                    sort_order, location_id, location_name,
                                    description_raw, meal_id, first_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, 'Šola', ?, ?, ?)""",
        (db.new_id(), menu_date, slot_menu_id, slot_name, sort_order,
         location_id, description, meal_id, db.now()),
    )


# --- storage -------------------------------------------------------------


def test_setting_an_override_replaces_rather_than_duplicates(user):
    meal = add_meal("burger")
    overrides.set_override(user["id"], "2026-09-10", "slot-1", meal)
    overrides.set_override(user["id"], "2026-09-10", "slot-2", meal)

    rows = db.query("SELECT * FROM override WHERE user_id = ?", (user["id"],))
    assert len(rows) == 1
    assert rows[0]["slot_menu_id"] == "slot-2"


def test_clear_removes_only_that_date(user):
    meal = add_meal("burger")
    overrides.set_override(user["id"], "2026-09-10", "slot-1", meal)
    overrides.set_override(user["id"], "2026-09-11", "slot-2", meal)

    overrides.clear(user["id"], "2026-09-10")

    assert overrides.get(user["id"], "2026-09-10") is None
    assert overrides.get(user["id"], "2026-09-11") is not None


def test_clear_past_leaves_today_and_later(user):
    meal = add_meal("burger")
    for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
        overrides.set_override(user["id"], day, "slot", meal)

    overrides.clear_past(user["id"], today=__import__("datetime").date(2026, 9, 2))

    remaining = sorted(overrides.for_dates(user["id"]))
    assert remaining == ["2026-09-02", "2026-09-03"]


# --- the calendar --------------------------------------------------------


def test_calendar_only_shows_this_users_location(user):
    import datetime

    mine, theirs = add_meal("moja"), add_meal("tuja")
    observe("2026-09-10", "s1", "MALICA 1", mine, "moja")
    observe("2026-09-10", "s2", "MALICA 2", theirs, "tuja", location_id="loc2")

    days = overrides.days(user, today=datetime.date(2026, 9, 9))

    assert len(days) == 1
    assert [s["slot_menu_id"] for s in days[0]["slots"]] == ["s1"]


def test_calendar_marks_the_automatic_choice(user):
    import datetime

    good, bad = add_meal("dobra"), add_meal("slaba")
    ranking.save_rating(user["id"], good, 90)
    ranking.save_rating(user["id"], bad, 10)
    observe("2026-09-10", "s1", "MALICA 1", bad, "slaba", sort_order=0)
    observe("2026-09-10", "s2", "MALICA 2", good, "dobra", sort_order=1)

    day = overrides.days(user, today=datetime.date(2026, 9, 9))[0]

    assert day["auto"]["slot_menu_id"] == "s2"
    assert [s["is_auto"] for s in day["slots"]] == [False, True]


def test_calendar_flags_an_override_whose_slot_is_gone(user):
    import datetime

    meal = add_meal("burger")
    observe("2026-09-10", "s1", "MALICA 1", meal, "burger")
    overrides.set_override(user["id"], "2026-09-10", "vanished", meal)

    day = overrides.days(user, today=datetime.date(2026, 9, 9))[0]

    assert day["override_stale"] is True
    assert day["override"] is None


# --- the picker honours it ----------------------------------------------


class FakeClient:
    """Stands in for the school's API: fixed order days, menu and orders."""

    def __init__(self, menus, order_days, orders=()):
        self._menus = menus
        self._order_days = order_days
        self._orders = list(orders)
        self.placed = []

    def get_order_days(self):
        return self._order_days

    def get_menus(self, start, end, user_id=None):
        return self._menus

    def get_orders(self, start, end, user_id=None):
        return self._orders

    def place_order(self, slot_menu_id, order_date, user_id=None):
        self.placed.append((slot_menu_id, order_date))


def menu_row(menu_date, menu_id, name, description, sort_order=0):
    return {
        "menu_date": menu_date, "menu_id": menu_id, "menu_name": name,
        "menu_description": description, "location_id": "loc1",
        "location_name": "Šola", "sort_order": sort_order,
    }


def test_override_beats_the_ranking(user):
    import datetime

    top = add_meal("PIZZA")
    other = add_meal("SOLATA")
    ranking.save_rating(user["id"], top, 95)
    ranking.save_rating(user["id"], other, 20)

    client = FakeClient(
        menus=[
            menu_row("2026-09-10", "s1", "MALICA 1", "PIZZA", 0),
            menu_row("2026-09-10", "s2", "MALICA 2", "SOLATA", 1),
        ],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )

    overrides.set_override(user["id"], "2026-09-10", "s2", other)
    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert decision["choice"]["slot_menu_id"] == "s2"
    assert decision["overridden"] is True
    assert "ročna izbira" in decision["detail"]


def test_override_wins_even_over_an_exclusion(user):
    import datetime

    liked = add_meal("PIZZA")
    hated = add_meal("SOLATA")
    ranking.save_rating(user["id"], liked, 95)
    ranking.exclude(user["id"], hated)

    client = FakeClient(
        menus=[
            menu_row("2026-09-10", "s1", "MALICA 1", "PIZZA", 0),
            menu_row("2026-09-10", "s2", "MALICA 2", "SOLATA", 1),
        ],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )

    overrides.set_override(user["id"], "2026-09-10", "s2", hated)
    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert decision["choice"]["slot_menu_id"] == "s2"


def test_override_applies_only_to_its_own_date(user):
    import datetime

    top = add_meal("PIZZA")
    other = add_meal("SOLATA")
    ranking.save_rating(user["id"], top, 95)
    ranking.save_rating(user["id"], other, 20)

    client = FakeClient(
        menus=[
            menu_row("2026-09-10", "s1", "MALICA 1", "PIZZA", 0),
            menu_row("2026-09-10", "s2", "MALICA 2", "SOLATA", 1),
            menu_row("2026-09-11", "s3", "MALICA 1", "PIZZA", 0),
            menu_row("2026-09-11", "s4", "MALICA 2", "SOLATA", 1),
        ],
        order_days=[
            {"order_date": "2026-09-10", "location_id": "loc1"},
            {"order_date": "2026-09-11", "location_id": "loc1"},
        ],
    )

    overrides.set_override(user["id"], "2026-09-10", "s2", other)
    decisions = picker.plan(user, client, today=datetime.date(2026, 9, 9))

    assert decisions[0]["choice"]["slot_menu_id"] == "s2"
    assert decisions[0]["overridden"] is True
    # The next day is untouched: the ranking still picks the favourite.
    assert decisions[1]["choice"]["slot_menu_id"] == "s3"
    assert decisions[1]["overridden"] is False


def test_override_lets_an_unrated_dish_be_ordered(user):
    """Nothing is rated, so the picker alone would skip the day entirely."""
    import datetime

    add_meal("PIZZA")

    client = FakeClient(
        menus=[menu_row("2026-09-10", "s1", "MALICA 1", "PIZZA", 0)],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )

    without = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]
    assert without["status"] == "skipped"

    overrides.set_override(user["id"], "2026-09-10", "s1", None)
    with_override = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert with_override["status"] == "to_order"
    assert with_override["choice"]["slot_menu_id"] == "s1"


def test_missing_override_slot_falls_back_to_the_ranking(user):
    import datetime

    top = add_meal("PIZZA")
    ranking.save_rating(user["id"], top, 95)

    client = FakeClient(
        menus=[menu_row("2026-09-10", "s1", "MALICA 1", "PIZZA", 0)],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )

    overrides.set_override(user["id"], "2026-09-10", "gone", top)
    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert decision["overridden"] is False
    assert decision["choice"]["slot_menu_id"] == "s1"
    assert "ni več na jedilniku" in decision["detail"]


# --- the daily fallback (MALICA 7) --------------------------------------


def observe_slot(menu_date, slot_menu_id, slot_name, meal_id, description):
    observe(menu_date, slot_menu_id, slot_name, meal_id, description)


def test_fallback_is_ordered_when_nothing_is_rated(user):
    """The whole point: a user who has rated nothing still gets lunch."""
    import datetime

    add_meal("PIŠČANČJI BURGER")

    client = FakeClient(
        menus=[
            menu_row("2026-09-10", "s1", "MALICA 1", "JOTA", 0),
            menu_row("2026-09-10", "s7", "MALICA 7", "PIŠČANČJI BURGER", 6),
        ],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )

    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert decision["status"] == "to_order"
    assert decision["is_fallback"] is True
    assert decision["choice"]["slot_name"] == "MALICA 7"
    assert "rezerva" in decision["detail"]


def test_a_rated_dish_still_beats_the_fallback(user):
    import datetime

    jota = add_meal("JOTA")
    ranking.save_rating(user["id"], jota, 80)

    client = FakeClient(
        menus=[
            menu_row("2026-09-10", "s1", "MALICA 1", "JOTA", 0),
            menu_row("2026-09-10", "s7", "MALICA 7", "PIŠČANČJI BURGER", 6),
        ],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )

    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert decision["is_fallback"] is False
    assert decision["choice"]["slot_name"] == "MALICA 1"


def test_fallback_still_wins_when_everything_is_excluded(user):
    """× means "below the floor", and the floor itself cannot be crossed out."""
    import datetime

    jota = add_meal("JOTA")
    burger = add_meal("PIŠČANČJI BURGER")
    ranking.exclude(user["id"], jota)
    ranking.exclude(user["id"], burger)

    client = FakeClient(
        menus=[
            menu_row("2026-09-10", "s1", "MALICA 1", "JOTA", 0),
            menu_row("2026-09-10", "s7", "MALICA 7", "PIŠČANČJI BURGER", 6),
        ],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )

    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert decision["status"] == "to_order"
    assert decision["is_fallback"] is True
    assert decision["choice"]["slot_name"] == "MALICA 7"


def test_no_fallback_slot_means_the_day_is_skipped_as_before(user):
    """A wrong slot name must make the feature inert, never order at random."""
    import datetime

    add_meal("JOTA")

    client = FakeClient(
        menus=[menu_row("2026-09-10", "s1", "MALICA 1", "JOTA", 0)],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )

    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert decision["status"] == "skipped"
    assert decision["is_fallback"] is False


def test_an_override_still_beats_the_fallback(user):
    import datetime

    burger = add_meal("PIŠČANČJI BURGER")
    jota = add_meal("JOTA")

    client = FakeClient(
        menus=[
            menu_row("2026-09-10", "s1", "MALICA 1", "JOTA", 0),
            menu_row("2026-09-10", "s7", "MALICA 7", "PIŠČANČJI BURGER", 6),
        ],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )

    overrides.set_override(user["id"], "2026-09-10", "s1", jota)
    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert decision["overridden"] is True
    assert decision["is_fallback"] is False
    assert decision["choice"]["slot_name"] == "MALICA 1"


def test_the_fallback_dish_is_never_put_to_the_rating_pass(user):
    from app import fallback

    burger = add_meal("PIŠČANČJI BURGER")
    jota = add_meal("JOTA")
    observe_slot("2026-09-10", "s7", "MALICA 7", burger, "PIŠČANČJI BURGER")
    observe_slot("2026-09-10", "s1", "MALICA 1", jota, "JOTA")

    assert burger in fallback.meal_ids()
    assert jota not in fallback.meal_ids()

    pending = ranking.unrated_meals(user["id"])
    assert jota in pending
    assert burger not in pending
    # ...and the pass can actually reach "done" without it.
    assert ranking.progress(user["id"])["total"] == 1


# --- signing off a day ----------------------------------------------------
#
# The fallback otherwise guarantees lunch every day, so the only way to end a
# day with nothing ordered is to ask for it. That is stored as an override
# naming no slot, and it has to beat the ranking, the fallback and all.


def test_signing_off_a_day_orders_nothing(user, monkeypatch):
    import datetime

    monkeypatch.setattr(picker.settings, "fallback_slot_name", "MALICA 7")
    liked = add_meal("PIZZA")
    ranking.save_rating(user["id"], liked, 95)
    add_meal("REZERVA")

    client = FakeClient(
        menus=[
            menu_row("2026-09-10", "s1", "MALICA 1", "PIZZA", 0),
            menu_row("2026-09-10", "s7", "MALICA 7", "REZERVA", 6),
        ],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )

    overrides.set_override(user["id"], "2026-09-10", overrides.SKIP, None)
    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert decision["choice"] is None, "not even the fallback"
    assert decision["status"] == "skipped"
    assert decision["skipped_by_hand"] is True
    assert decision["detail"] == "odjava"


def test_signing_off_still_reports_what_was_on_offer(user):
    """The day is skipped, not hidden: the board still shows the menu."""
    import datetime

    add_meal("PIZZA")
    client = FakeClient(
        menus=[menu_row("2026-09-10", "s1", "MALICA 1", "PIZZA", 0)],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )
    overrides.set_override(user["id"], "2026-09-10", overrides.SKIP, None)
    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]
    assert len(decision["considered"]) == 1


def test_signing_off_says_so_when_an_order_already_stands(user):
    """There is no cancel call, so the detail must not imply one happened."""
    import datetime

    top = add_meal("PIZZA")
    ranking.save_rating(user["id"], top, 95)
    client = FakeClient(
        menus=[menu_row("2026-09-10", "s1", "MALICA 1", "PIZZA", 0)],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
        orders=[{"order_date": "2026-09-10", "menu_id": "s1",
                 "menu_name": "MALICA 1", "canceled": False}],
    )
    overrides.set_override(user["id"], "2026-09-10", overrides.SKIP, None)
    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]

    assert decision["status"] == "skipped"
    assert "že stoji" in decision["detail"]


def test_signing_off_applies_only_to_its_own_date(user):
    import datetime

    top = add_meal("PIZZA")
    ranking.save_rating(user["id"], top, 95)
    client = FakeClient(
        menus=[
            menu_row("2026-09-10", "s1", "MALICA 1", "PIZZA", 0),
            menu_row("2026-09-11", "s2", "MALICA 1", "PIZZA", 0),
        ],
        order_days=[
            {"order_date": "2026-09-10", "location_id": "loc1"},
            {"order_date": "2026-09-11", "location_id": "loc1"},
        ],
    )
    overrides.set_override(user["id"], "2026-09-10", overrides.SKIP, None)
    decisions = picker.plan(user, client, today=datetime.date(2026, 9, 9))

    assert decisions[0]["choice"] is None
    assert decisions[1]["choice"]["slot_menu_id"] == "s2"


def test_signing_off_can_be_undone(user):
    import datetime

    top = add_meal("PIZZA")
    ranking.save_rating(user["id"], top, 95)
    client = FakeClient(
        menus=[menu_row("2026-09-10", "s1", "MALICA 1", "PIZZA", 0)],
        order_days=[{"order_date": "2026-09-10", "location_id": "loc1"}],
    )
    overrides.set_override(user["id"], "2026-09-10", overrides.SKIP, None)
    overrides.clear(user["id"], "2026-09-10")
    decision = picker.plan(user, client, today=datetime.date(2026, 9, 9))[0]
    assert decision["choice"]["slot_menu_id"] == "s1"


def test_the_board_marks_a_signed_off_day(user):
    import datetime

    meal = add_meal("PIZZA")
    observe("2026-09-10", "s1", "MALICA 1", meal, "PIZZA")
    overrides.set_override(user["id"], "2026-09-10", overrides.SKIP, None)

    day = overrides.days(user, today=datetime.date(2026, 9, 9))[0]
    assert day["skipped"] is True
    assert day["override"] is None
    assert day["override_stale"] is False, "naming no slot is not a stale slot"
