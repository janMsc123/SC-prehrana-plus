"""What is actually ordered, read back from the school.

The day board used to show what the app *meant* to order. That can drift from
what the school holds -- an order made on the school's own site, a call that
failed, a day changed on another device -- so the board now shows the school's
own record, kept in `school_order` and refreshed:

  * when the dashboard is opened, if the copy is more than a couple of minutes
    old (with a short timeout, falling back to the stored copy if the school is
    slow or down);
  * straight after the app orders or cancels anything;
  * by the daily job, which also keeps the history that research is built on
    (whether each ordered meal was actually picked up, `claimed_at`).

Only read calls are made here.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from . import collector, db
from .malcomat import AuthError, Client, MalcomatError

log = logging.getLogger("prehrana.school")

LOOKBACK_DAYS = 45
LOOKAHEAD_DAYS = 28
#: How old the stored copy may be before the dashboard reads it again.
FRESH_SECONDS = 120
#: Dashboard reads give up quickly; the stored copy is shown instead.
QUICK_TIMEOUT = 6.0


def store(user_id, orders, order_days=None):
    """Write one read of the school's orders. Returns how many rows came back."""
    stamp = db.now()
    with db.transaction():
        for row in orders:
            order_id = row.get("id") or "{}:{}:{}".format(
                user_id, row.get("order_date"), row.get("menu_id"))
            db.execute(
                """INSERT INTO school_order (id, user_id, order_date, menu_id,
                         menu_name, menu_description, canceled, locked,
                         claimed_at, updated_at, first_seen_at, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     menu_id = excluded.menu_id,
                     menu_name = excluded.menu_name,
                     menu_description = excluded.menu_description,
                     canceled = excluded.canceled,
                     locked = excluded.locked,
                     claimed_at = excluded.claimed_at,
                     updated_at = excluded.updated_at,
                     synced_at = excluded.synced_at""",
                (
                    order_id, user_id, row.get("order_date"), row.get("menu_id"),
                    row.get("menu_name"), row.get("menu_description"),
                    1 if row.get("canceled") else 0,
                    1 if row.get("locked") else 0,
                    row.get("claimed_at"), row.get("updated_at"), stamp, stamp,
                ),
            )
        db.execute(
            """INSERT INTO school_sync (user_id, synced_at, ok, detail, order_days_json)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 synced_at = excluded.synced_at, ok = 1, detail = excluded.detail,
                 order_days_json = COALESCE(excluded.order_days_json,
                                            school_sync.order_days_json)""",
            (user_id, stamp, "{} orders".format(len(orders)),
             json.dumps(order_days) if order_days is not None else None),
        )
    return len(orders)


def _record_failure(user_id, detail):
    db.execute(
        """INSERT INTO school_sync (user_id, synced_at, ok, detail)
           VALUES (?, ?, 0, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             synced_at = excluded.synced_at, ok = 0, detail = excluded.detail""",
        (user_id, db.now(), str(detail)[:300]),
    )


def sync_with_client(user_row, client, today=None, lookback=LOOKBACK_DAYS):
    """Read orders through a session that is already logged in."""
    today = today or date.today()
    orders = client.get_orders(today - timedelta(days=lookback),
                               today + timedelta(days=LOOKAHEAD_DAYS))
    try:
        days = [d.get("order_date") for d in client.get_order_days()
                if d.get("order_date")
                and (not user_row["location_id"]
                     or d.get("location_id") in (None, user_row["location_id"]))]
    except MalcomatError:
        days = None
    return store(user_row["id"], orders or [], days)


def sync_user(user_row, timeout=None, today=None, lookback=LOOKBACK_DAYS):
    """Log in and read. Raises MalcomatError/AuthError on failure."""
    password = collector.decrypt_password(user_row["password_enc"])
    if not password:
        raise MalcomatError("no stored password for {}".format(user_row["username"]))
    with Client(timeout=timeout) as client:
        client.login(user_row["username"], password)
        return sync_with_client(user_row, client, today=today, lookback=lookback)


def last_sync(user_id):
    return db.query_one("SELECT * FROM school_sync WHERE user_id = ?", (user_id,))


def ensure_fresh(user_row, max_age=FRESH_SECONDS):
    """Refresh if stale. Never raises: the stored copy is the fallback."""
    row = last_sync(user_row["id"])
    if row is not None:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["synced_at"])
            if age.total_seconds() < max_age:
                return row
        except ValueError:
            pass
    if not user_row["password_enc"]:
        return row
    try:
        sync_user(user_row, timeout=QUICK_TIMEOUT, lookback=7)
    except (MalcomatError, AuthError) as exc:
        log.info("order sync for %s failed: %s", user_row["username"], exc)
        _record_failure(user_row["id"], exc)
    return last_sync(user_row["id"])


def orders_by_date(user_id, start=None):
    """{order_date: row} of orders standing at school (not cancelled)."""
    start = str(start or date.today())
    out = {}
    for row in db.query(
        """SELECT * FROM school_order
            WHERE user_id = ? AND order_date >= ? AND canceled = 0
            ORDER BY COALESCE(updated_at, '') ASC""",
        (user_id, start),
    ):
        out[row["order_date"]] = row  # the newest wins
    return out


def orderable_dates(user_id):
    """Dates the school accepted orders for at the last read, or None."""
    row = last_sync(user_id)
    if row is None or not row["order_days_json"]:
        return None
    try:
        return set(json.loads(row["order_days_json"]))
    except ValueError:
        return None


def run():
    """Daily job: refresh everyone who has ordering switched on."""
    totals = {"users": 0, "orders": 0}
    errors = []
    for user in db.query(
        "SELECT * FROM app_user WHERE password_enc IS NOT NULL AND autopilot = 1"
    ):
        try:
            totals["orders"] += sync_user(user)
            totals["users"] += 1
        except (MalcomatError, AuthError) as exc:
            _record_failure(user["id"], exc)
            errors.append("{}: {}".format(user["username"], exc))
    db.execute(
        "INSERT INTO job_run (id, job, started_at, ok, summary) VALUES (?, 'sync', ?, ?, ?)",
        (db.new_id(), db.now(), 0 if errors else 1,
         json.dumps({"totals": totals, "errors": errors}, ensure_ascii=False)),
    )
    return totals, errors
