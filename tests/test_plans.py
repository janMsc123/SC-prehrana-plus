"""Menu plans, the no-MALICA-7 switch, and the board's view of the school.

A plan narrows which slots the picker may choose from; the rule inside it is
unchanged (best ranked, then a fallback). What has to hold: vegi is MALICA 2
every day, hladna is the better of 5 and 6 with 5 as its floor, the switch
replaces only MALICA 7 with a sign-off, and the board marks as ordered only
what the school actually holds.
"""

from __future__ import annotations

import datetime

import pytest

from app import db, overrides, picker, plans, ranking, school
from tests.test_override import FakeClient, add_meal, menu_row, observe


@pytest.fixture
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_connection", None)
    monkeypatch.setattr(db.settings, "database_path", tmp_path / "t.db")
    monkeypatch.setattr(db.settings, "fallback_slot_name", "MALICA 7")
    db.init()
    user_id = db.new_id()
    db.execute(
        """INSERT INTO app_user (id, username, location_id, autopilot, created_at)
           VALUES (?, ?, ?, 1, ?)""",
        (user_id, "tester", "loc1", db.now()),
    )
    return user_id


def fresh(user_id, **columns):
    for key, value in columns.items():
        db.execute("UPDATE app_user SET {} = ? WHERE id = ?".format(key), (value, user_id))
    return db.query_one("SELECT * FROM app_user WHERE id = ?", (user_id,))


DAY = "2026-09-10"
TODAY = datetime.date(2026, 9, 9)


def week(meals):
    """One day's seven slots: {slot number: head}."""
    ids = {n: add_meal(head) for n, head in meals.items()}
    rows = [menu_row(DAY, "s{}".format(n), "MALICA {}".format(n), head, n)
            for n, head in meals.items()]
    return ids, FakeClient(menus=rows,
                           order_days=[{"order_date": DAY, "location_id": "loc1"}])


MENU = {1: "PIZZA", 2: "ZELENJAVNA MUSAKA", 5: "SENDVIC", 6: "SKUTA", 7: "BURGER"}


def test_ranking_plan_takes_the_best_anywhere(user):
    ids, client = week(MENU)
    ranking.save_rating(user, ids[1], 90)
    ranking.save_rating(user, ids[6], 50)
    decision = picker.plan(fresh(user), client, today=TODAY)[0]
    assert decision["choice"]["slot_name"] == "MALICA 1"


def test_vegi_is_menu_two_even_when_it_is_not_ranked(user):
    ids, client = week(MENU)
    ranking.save_rating(user, ids[1], 90)
    decision = picker.plan(fresh(user, menu_plan="vegi"), client, today=TODAY)[0]
    assert decision["choice"]["slot_name"] == "MALICA 2"


def test_vegi_is_menu_two_even_when_it_is_excluded(user):
    ids, client = week(MENU)
    ranking.exclude(user, ids[2])
    decision = picker.plan(fresh(user, menu_plan="vegi"), client, today=TODAY)[0]
    assert decision["choice"]["slot_name"] == "MALICA 2"


def test_hladna_takes_the_higher_of_five_and_six(user):
    ids, client = week(MENU)
    ranking.save_rating(user, ids[1], 99)
    ranking.save_rating(user, ids[5], 40)
    ranking.save_rating(user, ids[6], 70)
    decision = picker.plan(fresh(user, menu_plan="hladna"), client, today=TODAY)[0]
    assert decision["choice"]["slot_name"] == "MALICA 6"


def test_hladna_falls_back_to_five(user):
    ids, client = week(MENU)
    ranking.save_rating(user, ids[1], 99)
    decision = picker.plan(fresh(user, menu_plan="hladna"), client, today=TODAY)[0]
    assert decision["choice"]["slot_name"] == "MALICA 5"
    assert decision["is_fallback"] is True


def test_skip_floor_signs_off_instead_of_seven(user):
    _ids, client = week(MENU)
    decision = picker.plan(fresh(user, skip_floor=1), client, today=TODAY)[0]
    assert decision["choice"] is None
    assert decision["status"] == "skipped"
    assert decision["skipped_by_plan"] is True


def test_skip_floor_cancels_an_order_already_standing(user):
    _ids, client = week(MENU)
    client._orders = [{"order_date": DAY, "menu_id": "s7", "menu_name": "MALICA 7",
                       "canceled": False}]
    decision = picker.plan(fresh(user, skip_floor=1), client, today=TODAY)[0]
    assert decision["existing_order_to_cancel"] is True


def test_skip_floor_leaves_a_ranked_day_alone(user):
    ids, client = week(MENU)
    ranking.save_rating(user, ids[5], 60)
    decision = picker.plan(fresh(user, skip_floor=1), client, today=TODAY)[0]
    assert decision["choice"]["slot_name"] == "MALICA 5"


def test_skip_floor_does_not_replace_a_plans_own_fallback(user):
    _ids, client = week(MENU)
    decision = picker.plan(fresh(user, menu_plan="hladna", skip_floor=1),
                           client, today=TODAY)[0]
    assert decision["choice"]["slot_name"] == "MALICA 5"


def test_a_hand_pick_still_beats_the_plan(user):
    ids, client = week(MENU)
    overrides.set_override(user, DAY, "s1", ids[1])
    decision = picker.plan(fresh(user, menu_plan="vegi"), client, today=TODAY)[0]
    assert decision["choice"]["slot_name"] == "MALICA 1"


def test_custom_plan_with_sign_off_fallback(user):
    ids, client = week(MENU)
    ranking.save_rating(user, ids[1], 90)
    row = fresh(user, menu_plan="custom", plan_slots='["MALICA 5", "MALICA 6"]',
                plan_fallback=plans.SKIP)
    decision = picker.plan(row, client, today=TODAY)[0]
    assert decision["choice"] is None and decision["skipped_by_plan"] is True


# --- the board ------------------------------------------------------------


def board_day(user_id, **kwargs):
    return overrides.days(fresh(user_id), today=TODAY, **kwargs)[0]


def seed_board(user_id):
    ids = {n: add_meal(head) for n, head in MENU.items()}
    for n, head in MENU.items():
        observe(DAY, "s{}".format(n), "MALICA {}".format(n), ids[n], head, sort_order=n)
    return ids


def test_board_says_will_be_ordered_until_the_school_has_it(user):
    ids = seed_board(user)
    ranking.save_rating(user, ids[1], 90)
    day = board_day(user)
    assert day["state"] == "pending"
    assert [s["slot_name"] for s in day["slots"] if s["is_target"]] == ["MALICA 1"]
    assert not any(s["is_ordered"] for s in day["slots"])


def test_board_marks_ordered_only_from_the_school(user):
    ids = seed_board(user)
    ranking.save_rating(user, ids[1], 90)
    school.store(user, [{"id": "o1", "order_date": DAY, "menu_id": "s1",
                         "menu_name": "MALICA 1", "canceled": False}])
    day = board_day(user, school_orders=school.orders_by_date(user, start=DAY))
    assert day["state"] == "ordered"
    assert [s["slot_name"] for s in day["slots"] if s["is_ordered"]] == ["MALICA 1"]


def test_board_promises_nothing_with_ordering_off(user):
    ids = seed_board(user)
    ranking.save_rating(user, ids[1], 90)
    db.execute("UPDATE app_user SET autopilot = 0 WHERE id = ?", (user,))
    day = board_day(user)
    assert day["auto"] is None
    assert day["state"] == "none"
    assert not any(s["is_target"] for s in day["slots"])


def test_board_still_shows_a_real_order_with_ordering_off(user):
    seed_board(user)
    db.execute("UPDATE app_user SET autopilot = 0 WHERE id = ?", (user,))
    school.store(user, [{"id": "o1", "order_date": DAY, "menu_id": "s5",
                         "menu_name": "MALICA 5", "canceled": False}])
    day = board_day(user, school_orders=school.orders_by_date(user, start=DAY))
    assert day["state"] == "ordered"
    assert day["ordered_slot"] == "s5"


def test_board_ignores_cancelled_orders(user):
    seed_board(user)
    school.store(user, [{"id": "o1", "order_date": DAY, "menu_id": "s5",
                         "menu_name": "MALICA 5", "canceled": True}])
    assert school.orders_by_date(user, start=DAY) == {}


def test_board_shows_the_plans_sign_off(user):
    seed_board(user)
    db.execute("UPDATE app_user SET skip_floor = 1 WHERE id = ?", (user,))
    day = board_day(user)
    assert day["plan_skip"] is True
    assert day["state"] == "off"


def test_board_locks_today_after_the_cutoff(user):
    seed_board(user)
    day = overrides.days(fresh(user), today=datetime.date(2026, 9, 10), orderable=set())[0]
    assert day["locked"] is True
