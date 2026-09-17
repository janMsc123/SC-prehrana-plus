"""Phase 3: choose the week's meals.

For every date the school currently allows ordering, look at the seven slots on
offer, work out which dish each one is, and order the one standing highest in
the user's ranking.

The rules that matter:

  * Slot numbers are ignored when choosing. The dish is identified by its text,
    so today's best meal might be MALICA 2 and next month's the same dish in
    MALICA 5.
  * Excluded meals (the X button) are never ordered. They sit below the
    always-available fallback, which is on offer every day, so in practice the
    fallback wins before an excluded dish ever could.
  * A dish nobody has ranked yet is not ordered on a guess. It is reported so
    it can be ranked, and the best *ranked* dish that day is used instead.
  * A date signed off by hand is left alone entirely: not the ranking, not the
    fallback, nothing. It is the only way to end a day without lunch, which is
    why it has to be asked for explicitly.
  * The orderable window comes from the school's own get_order_days, not from
    arithmetic on today's date -- it already accounts for cutoffs, holidays and
    the five-day limit.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime

from . import collector, db, fallback, identity, overrides, ranking
from .config import settings
from .malcomat import AuthError, Client, MalcomatError

log = logging.getLogger("prehrana.picker")


def _orderable_dates(client, location_id, today=None):
    """Dates the school will accept an order for right now."""
    today = today or date.today()
    out = []
    for row in client.get_order_days():
        if location_id and row.get("location_id") != location_id:
            continue
        order_date = row.get("order_date")
        if not order_date:
            continue
        if datetime.strptime(order_date, "%Y-%m-%d").date() < today:
            continue
        out.append(row)
    return sorted(out, key=lambda r: r["order_date"])


def plan(user_row, client, today=None):
    """Work out what to order, without ordering it.

    Returns a list of decisions, one per orderable date. Separated from the
    ordering itself so the same logic backs the preview in the web UI.
    """
    today = today or date.today()
    location_id = user_row["location_id"]
    order_days = _orderable_dates(client, location_id, today)
    if not order_days:
        return []

    dates = [d["order_date"] for d in order_days]
    menus = client.get_menus(min(dates), max(dates))
    existing_orders = {
        o["order_date"]: o
        for o in client.get_orders(min(dates), max(dates))
        if not o.get("canceled")
    }

    by_date = defaultdict(list)
    for row in menus:
        if (row.get("menu_description") or "").strip():
            by_date[row["menu_date"]].append(row)

    positions = ranking.rank_position(user_row["id"])
    excluded = {
        r["meal_id"]
        for r in db.query(
            "SELECT meal_id FROM exclusion WHERE user_id = ?", (user_row["id"],)
        )
    }
    chosen_by_hand = overrides.for_dates(user_row["id"], dates)
    known = collector._parsed_known_meals()

    decisions = []
    for day in order_days:
        order_date = day["order_date"]
        slots = sorted(by_date.get(order_date, []), key=lambda r: r.get("sort_order") or 0)
        decision = {
            "order_date": order_date,
            "cutoff": day.get("order_cutoff_time"),
            "choice": None,
            "status": None,
            "detail": "",
            "unranked": [],
            "considered": [],
            "overridden": False,
            "is_fallback": False,
            "skipped_by_hand": False,
        }

        if not slots:
            decision["status"] = "skipped"
            decision["detail"] = "no menu published for this date"
            decisions.append(decision)
            continue

        for slot in slots:
            meal_id, _verdict, _queued = collector.resolve_meal(
                slot["menu_description"], order_date, known
            )
            decision["considered"].append({
                "meal_id": meal_id,
                "slot_menu_id": slot["menu_id"],
                "slot_name": slot.get("menu_name") or "",
                "name": identity.display_name(identity.parse(slot["menu_description"])),
                "position": positions.get(meal_id),
                "excluded": meal_id in excluded,
            })

        # Signed off this date by hand: order nothing at all, not even the
        # fallback. This is the one instruction that beats the floor, so it is
        # answered before anything is weighed.
        override_row = chosen_by_hand.get(order_date)
        if override_row is not None and overrides.is_skip(override_row["slot_menu_id"]):
            decision["skipped_by_hand"] = True
            decision["status"] = "skipped"
            # An order already standing at school is not withdrawn by this: the
            # app has no cancel call, so say so rather than imply it is undone.
            decision["detail"] = (
                "odjava, a naročilo pri šoli že stoji"
                if existing_orders.get(order_date)
                else "odjava"
            )
            decisions.append(decision)
            continue

        # A meal picked by hand for this date beats everything else: the
        # ranking, an exclusion, and a dish that was never rated. Choosing it
        # on the board is a more specific instruction than any of those.
        best = None
        if override_row is not None:
            best = next(
                (e for e in decision["considered"]
                 if e["slot_menu_id"] == override_row["slot_menu_id"]),
                None,
            )
            if best is not None:
                decision["overridden"] = True
            else:
                # The slot is gone -- the school changed the offer after the
                # choice was made. Say so rather than silently auto-picking.
                decision["detail"] = "ročna izbira ni več na jedilniku"

        if best is None:
            for entry in decision["considered"]:
                if entry["excluded"]:
                    continue
                if entry["position"] is None:
                    decision["unranked"].append(entry["name"])
                    continue
                if best is None or entry["position"] < best["position"]:
                    best = entry

        if best is None:
            # Nothing cleared the floor -- so take the floor. The fallback is
            # on offer every day and is never rated or excluded in its own
            # right, which is the whole point: you still get lunch.
            fallback_slot = fallback.find_in_menu(slots)
            if fallback_slot is not None:
                best = next(
                    (e for e in decision["considered"]
                     if e["slot_menu_id"] == fallback_slot["menu_id"]),
                    None,
                )
                if best is not None:
                    decision["is_fallback"] = True

        if best is None:
            decision["status"] = "skipped"
            decision["detail"] = (
                "nothing on offer is ranked yet"
                if decision["unranked"]
                else "every dish on offer is excluded"
            )
            decisions.append(decision)
            continue

        decision["choice"] = best
        stale_note = decision["detail"]  # set above if an override went missing
        already = existing_orders.get(order_date)
        if already and already.get("menu_id") == best["slot_menu_id"]:
            decision["status"] = "already_correct"
            decision["detail"] = "already ordered"
        elif already and already.get("locked"):
            decision["status"] = "skipped"
            decision["detail"] = "existing order is locked by the school"
        else:
            decision["status"] = "to_order"
            decision["detail"] = "replacing {}".format(already["menu_name"]) if already else ""

        if decision["overridden"]:
            decision["detail"] = " · ".join(
                p for p in ("ročna izbira", decision["detail"]) if p
            )
        elif decision["is_fallback"]:
            decision["detail"] = " · ".join(
                p for p in ("rezerva", decision["detail"]) if p
            )
        elif stale_note:
            decision["detail"] = " · ".join(
                p for p in (stale_note, decision["detail"]) if p
            )
        decisions.append(decision)

    return decisions


def _record(user_id, decision, status, detail):
    choice = decision.get("choice") or {}
    db.execute(
        """INSERT INTO pick (id, user_id, order_date, meal_id, slot_menu_id,
                             slot_name, status, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, order_date) DO UPDATE SET
             meal_id      = excluded.meal_id,
             slot_menu_id = excluded.slot_menu_id,
             slot_name    = excluded.slot_name,
             status       = excluded.status,
             detail       = excluded.detail,
             created_at   = excluded.created_at""",
        (
            db.new_id(), user_id, decision["order_date"], choice.get("meal_id"),
            choice.get("slot_menu_id"), choice.get("slot_name"), status, detail,
            db.now(),
        ),
    )


def apply_override(user_row, order_date, dry_run=None):
    """Push a hand-made choice for one date to school right away.

    Called straight after the dashboard board writes an override (or clears
    one back to automatic), so what stands at school matches what the board
    now shows instead of waiting for the next scheduled run. `plan` already
    reads the override back out of the database, so this is the same
    resolution `pick_for_user` uses, just placed for one date instead of the
    whole window.
    """
    dry_run = (not settings.place_orders) if dry_run is None else dry_run

    password = collector.decrypt_password(user_row["password_enc"])
    if not password:
        raise MalcomatError("no stored password for {}".format(user_row["username"]))

    with Client() as client:
        client.login(user_row["username"], password)
        decisions = plan(user_row, client, today=date.today())
        decision = next(
            (d for d in decisions if d["order_date"] == str(order_date)), None
        )
        if decision is None or decision["status"] != "to_order":
            return decision

        if dry_run:
            decision["status"] = "dry_run"
            _record(user_row["id"], decision, "dry_run",
                    "would order {}".format(decision["choice"]["slot_name"]))
            return decision

        client.place_order(decision["choice"]["slot_menu_id"], order_date)

    decision["status"] = "placed"
    _record(user_row["id"], decision, "placed",
            "ordered {}".format(decision["choice"]["slot_name"]))
    return decision


def pick_for_user(user_row, today=None, dry_run=None):
    """Plan and then place the orders."""
    dry_run = (not settings.place_orders) if dry_run is None else dry_run

    password = collector.decrypt_password(user_row["password_enc"])
    if not password:
        raise MalcomatError("no stored password for {}".format(user_row["username"]))

    results = []
    with Client() as client:
        client.login(user_row["username"], password)
        decisions = plan(user_row, client, today=today)

        for decision in decisions:
            if decision["status"] != "to_order":
                _record(user_row["id"], decision, decision["status"], decision["detail"])
                results.append(decision)
                continue

            if dry_run:
                _record(user_row["id"], decision, "dry_run",
                        "would order {}".format(decision["choice"]["slot_name"]))
                decision["status"] = "dry_run"
                results.append(decision)
                continue

            try:
                client.place_order(
                    decision["choice"]["slot_menu_id"], decision["order_date"]
                )
            except MalcomatError as exc:
                log.warning("order failed %s %s: %s",
                            user_row["username"], decision["order_date"], exc)
                decision["status"] = "failed"
                decision["detail"] = str(exc)
                _record(user_row["id"], decision, "failed", str(exc))
            else:
                decision["status"] = "placed"
                _record(user_row["id"], decision, "placed",
                        "ordered {}".format(decision["choice"]["slot_name"]))
            results.append(decision)

    return results


def run(today=None, dry_run=None):
    """The scheduled Phase 3 job."""
    run_id = db.new_id()
    db.execute(
        "INSERT INTO job_run (id, job, started_at) VALUES (?, 'pick', ?)",
        (run_id, db.now()),
    )
    totals = defaultdict(int)
    errors = []

    users = db.query(
        "SELECT * FROM app_user WHERE password_enc IS NOT NULL AND autopilot = 1"
    )
    for user in users:
        try:
            for decision in pick_for_user(user, today=today, dry_run=dry_run):
                totals[decision["status"]] += 1
        except (MalcomatError, AuthError) as exc:
            log.warning("pick failed for %s: %s", user["username"], exc)
            errors.append("{}: {}".format(user["username"], exc))

    summary = json.dumps({"totals": dict(totals), "errors": errors}, ensure_ascii=False)
    db.execute(
        "UPDATE job_run SET ok = ?, summary = ? WHERE id = ?",
        (0 if errors else 1, summary, run_id),
    )
    return dict(totals), errors
