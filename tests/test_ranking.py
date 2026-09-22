"""Tests for the rating pass and the resulting order.

Every meal is shown once: an X rejects it for good, otherwise it gets a 1-100
slider score. There is no head-to-head questioning -- meals scored within
TIE_WINDOW of a neighbour are highlighted in the list instead, and if the order
between them matters you drag them.

What has to hold: rejected meals never appear, the order follows the scores,
a meal you dragged stays where you put it, and new dishes slot in by score and
are flagged rather than silently appearing.
"""

from __future__ import annotations

import pytest

from app import db, ranking


@pytest.fixture
def user(tmp_path, monkeypatch):
    """A throwaway database with one user."""
    monkeypatch.setattr(db, "_connection", None)
    monkeypatch.setattr(db.settings, "database_path", tmp_path / "t.db")
    db.init()
    user_id = db.new_id()
    db.execute(
        "INSERT INTO app_user (id, username, autopilot, created_at) VALUES (?, ?, 1, ?)",
        (user_id, "tester", db.now()),
    )
    return user_id


def add_meals(count, prefix="m"):
    ids = []
    for i in range(count):
        meal_id = db.new_id()
        db.execute(
            """INSERT INTO meal (id, fingerprint, fingerprint_version, head,
                                 components_json, sample_description, times_seen,
                                 created_at)
               VALUES (?, ?, 1, ?, '[]', ?, ?, ?)""",
            (meal_id, "{}{}".format(prefix, i), "{}{}".format(prefix, i),
             "{}{}".format(prefix, i), count - i, db.now()),
        )
        ids.append(meal_id)
    return ids


def rate_all(user_id, meals, score=50):
    for meal in meals:
        ranking.save_rating(user_id, meal, score)


# --- the rating pass -----------------------------------------------------


def test_every_meal_is_asked_about_exactly_once(user):
    meals = add_meals(10)
    asked = []
    while True:
        kind, payload = ranking.next_question(user)
        if kind != "rate":
            break
        asked.append(payload)
        ranking.save_rating(user, payload, 50)
    assert sorted(asked) == sorted(meals)
    assert len(asked) == 10, "one screen per meal, no more"


def test_there_are_no_head_to_head_questions(user):
    """Close scores must not produce a comparison -- they get highlighted."""
    meals = add_meals(6)
    rate_all(user, meals, 70)   # everything identical: maximally "tied"
    kind, _payload = ranking.next_question(user)
    assert kind == "done"


def test_rejected_meals_are_never_asked_about_again(user):
    meals = add_meals(5)
    ranking.exclude(user, meals[0])

    seen = []
    while True:
        kind, payload = ranking.next_question(user)
        if kind != "rate":
            break
        seen.append(payload)
        ranking.save_rating(user, payload, 50)

    assert meals[0] not in seen
    assert meals[0] not in ranking.current_ranking(user)


def test_rejected_meals_stay_out_of_the_ranking(user):
    meals = add_meals(4)
    rate_all(user, meals, 80)
    ranking.exclude(user, meals[1])
    assert meals[1] not in ranking.current_ranking(user)
    assert len(ranking.current_ranking(user)) == 3


def test_score_is_clamped_to_the_slider_range(user):
    meals = add_meals(2)
    ranking.save_rating(user, meals[0], 999)
    ranking.save_rating(user, meals[1], -5)
    scores = ranking.get_ratings(user)
    assert scores[meals[0]] == ranking.MAX_SCORE
    assert scores[meals[1]] == ranking.MIN_SCORE


def test_order_follows_the_scores(user):
    meals = add_meals(5)
    for meal, score in zip(meals, [10, 90, 50, 70, 30]):
        ranking.save_rating(user, meal, score)
    order = ranking.current_ranking(user)
    scores = ranking.get_ratings(user)
    assert [scores[m] for m in order] == [90, 70, 50, 30, 10]


def test_ranking_is_usable_before_the_pass_is_finished(user):
    meals = add_meals(6)
    ranking.save_rating(user, meals[0], 90)
    ranking.save_rating(user, meals[1], 20)
    assert ranking.current_ranking(user) == [meals[0], meals[1]]


# --- the yellow highlight ------------------------------------------------


def test_close_neighbours_are_flagged(user):
    meals = add_meals(3)
    for meal, score in zip(meals, [70, 69, 20]):
        ranking.save_rating(user, meal, score)
    order = ranking.current_ranking(user)
    close = ranking.close_to_neighbour(order, ranking.get_ratings(user))
    assert close == {meals[0], meals[1]}


def test_exactly_at_the_window_edge_is_close(user):
    meals = add_meals(2)
    ranking.save_rating(user, meals[0], 70)
    ranking.save_rating(user, meals[1], 70 - ranking.TIE_WINDOW)
    order = ranking.current_ranking(user)
    assert ranking.close_to_neighbour(order, ranking.get_ratings(user)) == set(meals)


def test_just_outside_the_window_is_not_close(user):
    meals = add_meals(2)
    ranking.save_rating(user, meals[0], 70)
    ranking.save_rating(user, meals[1], 70 - ranking.TIE_WINDOW - 1)
    order = ranking.current_ranking(user)
    assert ranking.close_to_neighbour(order, ranking.get_ratings(user)) == set()


def test_highlight_only_looks_at_adjacent_meals(user):
    """A meal is highlighted against its neighbours, not the whole list."""
    meals = add_meals(3)
    for meal, score in zip(meals, [70, 60, 59]):
        ranking.save_rating(user, meal, score)
    order = ranking.current_ranking(user)
    close = ranking.close_to_neighbour(order, ranking.get_ratings(user))
    assert meals[0] not in close
    assert close == {meals[1], meals[2]}


# --- dragging ------------------------------------------------------------


def test_dragging_sets_the_order(user):
    meals = add_meals(4)
    for meal, score in zip(meals, [90, 80, 70, 60]):
        ranking.save_rating(user, meal, score)

    wanted = [meals[3], meals[0], meals[2], meals[1]]
    ranking.set_manual_order(user, wanted)
    assert ranking.current_ranking(user) == wanted


def test_a_dragged_meal_survives_a_recompute(user):
    """Re-rating something else must not undo a hand-placed order."""
    meals = add_meals(4)
    for meal, score in zip(meals, [90, 80, 70, 60]):
        ranking.save_rating(user, meal, score)

    wanted = [meals[3], meals[2], meals[1], meals[0]]
    ranking.set_manual_order(user, wanted)
    ranking.refresh_ranking(user)
    assert ranking.current_ranking(user) == wanted


def test_reorder_ignores_unknown_ids(user):
    meals = add_meals(3)
    rate_all(user, meals, 50)
    ranking.set_manual_order(user, [meals[2], "not-a-meal", meals[0]])
    order = ranking.current_ranking(user)
    assert order[0] == meals[2] and order[1] == meals[0]
    assert len(order) == 3, "meals left out keep a place at the end"


def test_excluding_after_a_drag_keeps_the_rest_in_place(user):
    meals = add_meals(4)
    for meal, score in zip(meals, [90, 80, 70, 60]):
        ranking.save_rating(user, meal, score)
    ranking.set_manual_order(user, [meals[3], meals[2], meals[1], meals[0]])
    ranking.exclude(user, meals[2])
    assert ranking.current_ranking(user) == [meals[3], meals[1], meals[0]]


# --- new dishes ----------------------------------------------------------


def test_nothing_is_flagged_new_during_onboarding(user):
    meals = add_meals(4)
    rate_all(user, meals, 50)
    assert ranking.unacknowledged(user) == set()


def test_a_dish_rated_after_onboarding_is_flagged(user):
    meals = add_meals(3)
    rate_all(user, meals, 50)
    ranking.mark_onboarded(user)

    later = add_meals(1, prefix="new")[0]
    ranking.save_rating(user, later, 60)
    assert ranking.unacknowledged(user) == {later}
    assert ranking.progress(user)["new"] == 1


def test_a_new_dish_lands_where_its_score_belongs(user):
    meals = add_meals(3)
    for meal, score in zip(meals, [90, 50, 20]):
        ranking.save_rating(user, meal, score)
    ranking.set_manual_order(user, meals)      # pin them all
    ranking.mark_onboarded(user)

    later = add_meals(1, prefix="new")[0]
    ranking.save_rating(user, later, 70)       # between 90 and 50
    assert ranking.current_ranking(user) == [meals[0], later, meals[1], meals[2]]


def test_a_new_dish_better_than_everything_goes_first(user):
    meals = add_meals(2)
    for meal, score in zip(meals, [60, 40]):
        ranking.save_rating(user, meal, score)
    ranking.set_manual_order(user, meals)
    ranking.mark_onboarded(user)

    later = add_meals(1, prefix="new")[0]
    ranking.save_rating(user, later, 99)
    assert ranking.current_ranking(user)[0] == later


def test_a_new_dish_worse_than_everything_goes_last(user):
    meals = add_meals(2)
    for meal, score in zip(meals, [60, 40]):
        ranking.save_rating(user, meal, score)
    ranking.set_manual_order(user, meals)
    ranking.mark_onboarded(user)

    later = add_meals(1, prefix="new")[0]
    ranking.save_rating(user, later, 5)
    assert ranking.current_ranking(user)[-1] == later


def test_placing_a_new_dish_clears_its_flag(user):
    meals = add_meals(2)
    rate_all(user, meals, 50)
    ranking.mark_onboarded(user)
    later = add_meals(1, prefix="new")[0]
    ranking.save_rating(user, later, 60)

    ranking.set_manual_order(user, ranking.current_ranking(user))
    assert ranking.unacknowledged(user) == set()


def test_flags_can_be_dismissed_without_reordering(user):
    meals = add_meals(2)
    rate_all(user, meals, 50)
    ranking.mark_onboarded(user)
    later = add_meals(1, prefix="new")[0]
    ranking.save_rating(user, later, 60)

    before = ranking.current_ranking(user)
    ranking.acknowledge_new(user)
    assert ranking.unacknowledged(user) == set()
    assert ranking.current_ranking(user) == before


def test_onboarding_is_only_marked_once(user):
    meals = add_meals(2)
    rate_all(user, meals, 50)
    assert ranking.mark_onboarded(user) is True
    assert ranking.mark_onboarded(user) is False


# --- housekeeping --------------------------------------------------------


def test_progress_reaches_done(user):
    meals = add_meals(8)
    for i, meal in enumerate(meals):
        ranking.save_rating(user, meal, 100 - i * 10)
    stats = ranking.progress(user)
    assert stats["rated"] == 8 and stats["to_rate"] == 0 and stats["done"] is True


def test_restart_keeps_rejections(user):
    """An X means never ask again, so a restart must not resurrect it."""
    meals = add_meals(4)
    ranking.exclude(user, meals[0])
    rate_all(user, meals[1:], 50)

    ranking.restart(user)
    assert ranking.get_ratings(user) == {}

    seen = []
    while True:
        kind, payload = ranking.next_question(user)
        if kind != "rate":
            break
        seen.append(payload)
        ranking.save_rating(user, payload, 50)
    assert meals[0] not in seen


def test_restart_clears_a_hand_made_order(user):
    meals = add_meals(3)
    rate_all(user, meals, 50)
    ranking.set_manual_order(user, list(reversed(meals)))
    ranking.restart(user)
    assert ranking.current_ranking(user) == []


def test_unexclude_puts_a_meal_back_in_the_queue(user):
    meals = add_meals(3)
    ranking.exclude(user, meals[0])
    ranking.unexclude(user, meals[0])
    assert meals[0] in ranking.unrated_meals(user)


# --- a brand-new install -------------------------------------------------


def test_an_empty_database_is_not_finished(user):
    """Nothing collected yet must not read as "you have rated everything"."""
    stats = ranking.progress(user)
    assert stats["total"] == 0
    assert stats["done"] is False, "an empty install would trap the user on the tutorial"


def test_onboarding_is_not_marked_before_anything_is_rated(user):
    """Otherwise every dish that ever arrives is flagged as a new arrival."""
    assert ranking.mark_onboarded(user) is False
    add_meals(2)
    assert ranking.mark_onboarded(user) is False, "meals exist but none are rated"


def test_onboarding_is_marked_once_rating_has_happened(user):
    meals = add_meals(2)
    rate_all(user, meals, 50)
    assert ranking.mark_onboarded(user) is True


# --- the whole board at once ---------------------------------------------
#
# The meal page keeps its three columns in the browser and posts all of them
# together, so one call has to reconcile the order, the rejected pile and the
# meals still waiting for an opinion.


def test_the_board_saves_the_order_it_was_given(user):
    meals = add_meals(4)
    for meal, score in zip(meals, [90, 80, 70, 60]):
        ranking.save_rating(user, meal, score)

    wanted = [meals[2], meals[0], meals[3], meals[1]]
    ranking.apply_board(user, wanted, [], [])
    assert ranking.current_ranking(user) == wanted


def test_a_typed_score_is_kept_as_typed(user):
    meals = add_meals(3)
    rate_all(user, meals, 50)
    ranking.apply_board(user, meals, [], [], {meals[1]: 97})
    assert ranking.get_ratings(user)[meals[1]] == 97


def test_a_typed_score_is_clamped_to_the_scale(user):
    meals = add_meals(2)
    rate_all(user, meals, 50)
    ranking.apply_board(user, meals, [], [], {meals[0]: 500, meals[1]: -7})
    scores = ranking.get_ratings(user)
    assert scores[meals[0]] == ranking.MAX_SCORE
    assert scores[meals[1]] == ranking.MIN_SCORE


def test_a_meal_dragged_in_lands_between_its_neighbours(user):
    """Dropping an unrated dish into the list is how it gets rated."""
    meals = add_meals(3)
    ranking.save_rating(user, meals[0], 90)
    ranking.save_rating(user, meals[1], 70)
    # meals[2] has never been rated; it is dropped between the other two.
    ranking.apply_board(user, [meals[0], meals[2], meals[1]], [], [])
    assert ranking.get_ratings(user)[meals[2]] == 80


def test_a_meal_dragged_to_the_top_outscores_what_was_there(user):
    meals = add_meals(2)
    ranking.save_rating(user, meals[0], 40)
    ranking.apply_board(user, [meals[1], meals[0]], [], [])
    assert ranking.get_ratings(user)[meals[1]] > 40


def test_the_board_rejects_and_restores_meals(user):
    meals = add_meals(3)
    rate_all(user, meals, 50)

    ranking.apply_board(user, [meals[0]], [meals[1]], [meals[2]])
    assert ranking.excluded_meals(user) == {meals[1]}
    assert meals[2] not in ranking.get_ratings(user), "set aside, not rated"
    assert ranking.current_ranking(user) == [meals[0]]

    # Dragged back out of the rejected pile and into the order.
    ranking.apply_board(user, [meals[0], meals[1]], [], [meals[2]])
    assert ranking.excluded_meals(user) == set()
    assert meals[1] in ranking.get_ratings(user)


def test_the_board_ignores_an_empty_post(user):
    """A form that arrives with nothing in it must not wipe the list."""
    meals = add_meals(3)
    rate_all(user, meals, 50)
    before = ranking.current_ranking(user)
    ranking.apply_board(user, [], [], [])
    assert ranking.current_ranking(user) == before


def test_the_board_ignores_unknown_and_repeated_ids(user):
    meals = add_meals(2)
    rate_all(user, meals, 50)
    ranking.apply_board(user, [meals[0], "not-a-meal", meals[0], meals[1]], [], [])
    assert ranking.current_ranking(user) == [meals[0], meals[1]]


def test_a_meal_named_twice_keeps_only_its_first_column(user):
    meals = add_meals(2)
    rate_all(user, meals, 50)
    ranking.apply_board(user, [meals[0], meals[1]], [meals[1]], [])
    assert ranking.excluded_meals(user) == set(), "the order named it first"
    assert ranking.current_ranking(user) == [meals[0], meals[1]]


# --- the repeat cooldown --------------------------------------------------
#
# picker.plan uses these to stop one favourite dish from winning every day it
# happens to be on offer. Tested here in isolation from the picker's own
# date-juggling.


def test_best_avoiding_repeats_skips_a_recent_dish_for_the_next_best(user):
    candidates = [{"meal_id": "top", "position": 0}, {"meal_id": "second", "position": 1}]
    best, repeated = ranking.best_avoiding_repeats(candidates, {"top"})
    assert best["meal_id"] == "second"
    assert repeated is False


def test_best_avoiding_repeats_still_wins_when_nothing_is_fresh(user):
    """A repeat still beats not eating -- it is a last resort, not a veto."""
    candidates = [{"meal_id": "top", "position": 0}, {"meal_id": "second", "position": 1}]
    best, repeated = ranking.best_avoiding_repeats(candidates, {"top", "second"})
    assert best["meal_id"] == "top"
    assert repeated is True


def test_best_avoiding_repeats_handles_nothing_ranked(user):
    assert ranking.best_avoiding_repeats([], set()) == (None, False)


def test_still_cooling_down_is_exclusive_at_the_boundary(user):
    """Exactly `cooldown_days` later, the dish has cleared cooldown."""
    last_chosen = {"m1": "2026-09-01"}
    assert ranking.still_cooling_down(last_chosen, "2026-09-05", 4) == set()
    assert ranking.still_cooling_down(last_chosen, "2026-09-04", 4) == {"m1"}


def test_still_cooling_down_disabled_by_zero(user):
    last_chosen = {"m1": "2026-09-01"}
    assert ranking.still_cooling_down(last_chosen, "2026-09-01", 0) == set()


def test_recent_choices_only_counts_real_orders(user):
    meal = add_meals(1)[0]
    for status, order_date in [
        ("placed", "2026-09-01"), ("dry_run", "2026-09-02"),
        ("already_correct", "2026-09-03"), ("skipped", "2026-09-04"),
        ("failed", "2026-09-05"),
    ]:
        db.execute(
            """INSERT INTO pick (id, user_id, order_date, meal_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (db.new_id(), user, order_date, meal, status, db.now()),
        )
    history = ranking.recent_choices(user, "2026-09-10", 30)
    assert history[meal] == "2026-09-03", "the latest real order, not a skipped/failed one"


def test_recent_choices_respects_the_cooldown_window(user):
    meal = add_meals(1)[0]
    db.execute(
        """INSERT INTO pick (id, user_id, order_date, meal_id, status, created_at)
           VALUES (?, ?, ?, ?, 'placed', ?)""",
        (db.new_id(), user, "2026-08-01", meal, db.now()),
    )
    assert ranking.recent_choices(user, "2026-09-10", 5) == {}, "too long ago to matter"


def test_the_board_pins_what_it_saves(user):
    """A later arrival must not shuffle a list that was arranged by hand."""
    meals = add_meals(3)
    for meal, score in zip(meals, [90, 80, 70]):
        ranking.save_rating(user, meal, score)
    wanted = [meals[2], meals[1], meals[0]]
    ranking.apply_board(user, wanted, [], [])

    newcomer, = add_meals(1, prefix="later")
    ranking.save_rating(user, newcomer, 85)
    order = ranking.current_ranking(user)
    assert [m for m in order if m in wanted] == wanted
