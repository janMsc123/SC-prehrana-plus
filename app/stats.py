"""Numbers for the admin page, and the data export behind them.

Everything here is read-only and computed straight from the database on each
request -- the instance has a handful of users, so there is nothing to cache.
The export is the raw material for analysis (raziskovalna naloga): one CSV per
table, users replaced by stable pseudonyms unless names are asked for.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from . import db, plans
from .config import settings

DNEVI = ("pon", "tor", "sre", "čet", "pet", "sob", "ned")


def pseudonym(user_id):
    digest = hashlib.sha256((settings.secret_key + ":" + (user_id or "")).encode()).hexdigest()
    return "U" + digest[:8]


def _count(sql, params=()):
    row = db.query_one(sql, params)
    return row[0] if row else 0


def _bars(counter, order=None, label=None):
    """[{label, n, pct}] scaled to the biggest value, for CSS bar charts."""
    keys = order if order is not None else [k for k, _ in counter.most_common()]
    top = max([counter.get(k, 0) for k in keys] or [0]) or 1
    total = sum(counter.get(k, 0) for k in keys) or 1
    return [
        {"label": label(k) if label else k, "n": counter.get(k, 0),
         "pct": round(100 * counter.get(k, 0) / top),
         "share": round(100 * counter.get(k, 0) / total)}
        for k in keys
    ]


def _since(iso, days):
    if not iso:
        return False
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(iso) < timedelta(days=days)
    except ValueError:
        return False


def users_table():
    today = str(date.today())
    out = []
    for u in db.query("SELECT * FROM app_user ORDER BY created_at"):
        uid = u["id"]
        orders = db.query(
            "SELECT * FROM school_order WHERE user_id = ? AND canceled = 0", (uid,))
        past = [o for o in orders if o["order_date"] < today]
        claimed = sum(1 for o in past if o["claimed_at"])
        sync = db.query_one("SELECT * FROM school_sync WHERE user_id = ?", (uid,))
        picks = Counter(r["status"] for r in db.query(
            "SELECT status FROM pick WHERE user_id = ?", (uid,)))
        top = db.query_one(
            """SELECT m.head FROM ranking r JOIN meal m ON m.id = r.meal_id
                WHERE r.user_id = ? ORDER BY r.position LIMIT 1""", (uid,))
        plan = plans.for_user(u)
        out.append({
            "id": uid,
            "pseudonym": pseudonym(uid),
            "username": u["username"],
            "name": " ".join(p for p in (u["first_name"], u["last_name"]) if p) or u["username"],
            "class_name": u["class_name"] or "",
            "created_at": (u["created_at"] or "")[:10],
            "last_login_at": (u["last_login_at"] or "")[:16].replace("T", " "),
            "active_7d": _since(u["last_login_at"], 7),
            "autopilot": bool(u["autopilot"]),
            "has_password": bool(u["password_enc"]),
            "plan": plan.label,
            "skip_floor": plan.skip_floor,
            "ratings": _count("SELECT COUNT(*) FROM rating WHERE user_id = ?", (uid,)),
            "exclusions": _count("SELECT COUNT(*) FROM exclusion WHERE user_id = ?", (uid,)),
            "overrides": _count(
                "SELECT COUNT(*) FROM override WHERE user_id = ? AND order_date >= ?",
                (uid, today)),
            "placed": picks.get("placed", 0) + picks.get("already_correct", 0),
            "canceled": picks.get("canceled", 0),
            "failed": picks.get("failed", 0),
            "orders": len(orders),
            "orders_past": len(past),
            "claimed": claimed,
            "claim_rate": round(100 * claimed / len(past)) if past else None,
            "sync_ok": bool(sync["ok"]) if sync else None,
            "sync_at": (sync["synced_at"][:16].replace("T", " ") if sync else ""),
            "top_meal": top["head"] if top else "",
        })
    return out


def overview():
    today = str(date.today())
    users = db.query("SELECT * FROM app_user")
    orders = db.query("SELECT * FROM school_order WHERE canceled = 0")
    past = [o for o in orders if o["order_date"] < today]
    claimed_past = [o for o in past if o["claimed_at"]]

    tiles = [
        {"label": "Uporabniki", "value": len(users)},
        {"label": "Samodejno vklopljeno", "value": sum(1 for u in users if u["autopilot"])},
        {"label": "Aktivni (7 dni)", "value": sum(1 for u in users if _since(u["last_login_at"], 7))},
        {"label": "Ocen jedi", "value": _count("SELECT COUNT(*) FROM rating")},
        {"label": "Izločenih jedi", "value": _count("SELECT COUNT(*) FROM exclusion")},
        {"label": "Naročil pri šoli", "value": len(orders)},
        {"label": "Prevzetih", "value": "{} %".format(
            round(100 * len(claimed_past) / len(past))) if past else "–",
         "hint": "{} od {} preteklih".format(len(claimed_past), len(past))},
        {"label": "Dni jedilnika", "value": _count(
            "SELECT COUNT(DISTINCT menu_date) FROM observation")},
        {"label": "Različnih jedi", "value": len(db.load_meals())},
    ]

    plan_counter = Counter(plans.for_user(u).label for u in users)
    class_counter = Counter((u["class_name"] or "?") for u in users)

    slot_counter = Counter(plans.slot_label(o["menu_name"]) for o in orders)
    slot_order = [plans.slot_label(s) for s in plans.SLOT_NAMES]
    slot_bars = _bars(slot_counter, order=slot_order)

    # Pickup rate per menu line: which lines are ordered and then left.
    pickup = []
    for slot in plans.SLOT_NAMES:
        ordered = [o for o in past if o["menu_name"] == slot]
        got = sum(1 for o in ordered if o["claimed_at"])
        pickup.append({
            "label": plans.slot_label(slot), "n": len(ordered), "claimed": got,
            "pct": round(100 * got / len(ordered)) if ordered else 0,
        })

    weekday_counter = Counter(
        date.fromisoformat(o["order_date"]).weekday() for o in orders)
    weekday_bars = _bars(weekday_counter, order=list(range(5)),
                         label=lambda k: DNEVI[k])

    pick_counter = Counter(r["status"] for r in db.query("SELECT status FROM pick"))
    pick_labels = {
        "placed": "naročeno", "already_correct": "že pravilno",
        "canceled": "odjavljeno", "skipped": "preskočeno",
        "failed": "napaka", "dry_run": "poskusno",
    }
    pick_bars = _bars(pick_counter, label=lambda k: pick_labels.get(k, k))

    # Hand-made vs automatic: overrides ever set, from the event history and
    # what is currently stored.
    source = Counter()
    for row in db.query("SELECT detail, status FROM pick WHERE status IN ('placed','already_correct')"):
        detail = row["detail"] or ""
        if "ročna" in detail:
            source["ročna izbira"] += 1
        elif "rezerva" in detail:
            source["rezerva"] += 1
        else:
            source["po lestvici"] += 1
    source_bars = _bars(source)

    # Orders per day over the last weeks: how many people the app feeds.
    by_day = Counter(o["order_date"] for o in orders
                     if o["order_date"] >= str(date.today() - timedelta(days=42)))
    day_keys = sorted(by_day)
    timeline = _bars(by_day, order=day_keys,
                     label=lambda k: "{}.{}.".format(int(k[8:10]), int(k[5:7])))

    return {
        "tiles": tiles,
        "plans": _bars(plan_counter),
        "classes": _bars(class_counter),
        "slots": slot_bars,
        "pickup": pickup,
        "weekdays": weekday_bars,
        "picks": pick_bars,
        "source": source_bars,
        "timeline": timeline,
    }


def meal_popularity(limit=15):
    """Per dish: how many rated it, average score, how many excluded it."""
    meals = db.load_meals()
    scores = defaultdict(list)
    for row in db.query("SELECT meal_id, score FROM rating"):
        scores[db.canonical_meal_id(row["meal_id"])].append(row["score"])
    excluded = Counter(db.canonical_meal_id(r["meal_id"])
                       for r in db.query("SELECT meal_id FROM exclusion"))
    rows = []
    for meal_id, meal in meals.items():
        s = scores.get(meal_id, [])
        x = excluded.get(meal_id, 0)
        if not s and not x:
            continue
        rows.append({
            "name": meal["head"],
            "raters": len(s),
            "avg": round(sum(s) / len(s), 1) if s else None,
            "excluded": x,
            "approval": round(100 * len(s) / (len(s) + x)) if (s or x) else None,
            "seen": meal["times_seen"],
        })
    liked = sorted([r for r in rows if r["avg"] is not None and r["raters"] >= 2],
                   key=lambda r: (-r["avg"], -r["raters"]))[:limit]
    disliked = sorted(rows, key=lambda r: (-r["excluded"], r["avg"] or 0))[:limit]
    return {"liked": liked, "disliked": disliked, "count": len(rows)}


def recent_events(limit=40):
    rows = db.query(
        """SELECT e.*, u.username FROM event e LEFT JOIN app_user u ON u.id = e.user_id
            ORDER BY e.created_at DESC LIMIT ?""", (limit,))
    return [dict(r) for r in rows]


def recent_jobs(limit=12):
    return [dict(r) for r in db.query(
        "SELECT * FROM job_run ORDER BY started_at DESC LIMIT ?", (limit,))]


def suggestions(limit=30):
    return [dict(r) for r in db.query(
        "SELECT * FROM suggestion ORDER BY created_at DESC LIMIT ?", (limit,))]


# --- export ----------------------------------------------------------------

#: (file, sql, columns holding a user id). Passwords are never exported.
_EXPORTS = (
    ("users.csv",
     """SELECT id AS user_id, username, first_name, last_name, class_name,
               autopilot, menu_plan, plan_slots, plan_fallback, skip_floor,
               created_at, last_login_at, onboarded_at, tutorial_seen_at
          FROM app_user""", ("user_id",)),
    ("ratings.csv", "SELECT * FROM rating", ("user_id",)),
    ("exclusions.csv", "SELECT * FROM exclusion", ("user_id",)),
    ("rankings.csv", "SELECT * FROM ranking", ("user_id",)),
    ("overrides.csv", "SELECT * FROM override", ("user_id",)),
    ("picks.csv", "SELECT * FROM pick", ("user_id",)),
    ("school_orders.csv", "SELECT * FROM school_order", ("user_id",)),
    ("events.csv", "SELECT * FROM event", ("user_id",)),
    ("suggestions.csv", "SELECT id, user_id, body, delivered, created_at FROM suggestion",
     ("user_id",)),
    ("meals.csv", "SELECT * FROM meal", ()),
    ("meal_aliases.csv", "SELECT * FROM meal_alias", ()),
    ("menu_observations.csv", "SELECT * FROM observation", ()),
    ("job_runs.csv", "SELECT * FROM job_run", ()),
)

_IDENTIFYING = {"username", "first_name", "last_name"}

README = """Prehrana Plus -- izvoz podatkov
==============================

Izvoženo: {when}
Uporabniki: {mode}

Datoteke (ena na tabelo, UTF-8, vejica kot ločilo):

users.csv               uporabniki, razred, nastavitve (načrt menija, samodejno)
ratings.csv             ocena jedi 1-100 (user_id, meal_id, score)
exclusions.csv          jedi, ki jih uporabnik nikoli noče (X)
rankings.csv            končni vrstni red (position 0 = najljubša)
overrides.csv           ročne izbire za posamezen dan (__skip__ = odjava)
picks.csv               kaj je aplikacija naročila za dan in s kakšnim izidom
school_orders.csv       kar ima šola zapisano; claimed_at = malica prevzeta
events.csv              zgodovina vseh dejanj (prijave, nastavitve, naročila)
suggestions.csv         predlogi uporabnikov
meals.csv               jedi, prepoznane po besedilu (head = glavna jed)
meal_aliases.csv        združene jedi (meal_id -> canonical_id)
menu_observations.csv   jedilnik: vsak meni na vsak dan
job_runs.csv            zagoni samodejnih opravil

user_id je {idnote}
"""


def export_zip(with_names=False):
    """All tables as CSVs in one zip. Pseudonymous unless `with_names`."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, sql, user_columns in _EXPORTS:
            rows = db.query(sql)
            out = io.StringIO()
            writer = csv.writer(out)
            if rows:
                columns = [c for c in rows[0].keys()
                           if with_names or c not in _IDENTIFYING]
                writer.writerow(columns)
                for row in rows:
                    values = []
                    for column in columns:
                        value = row[column]
                        if column in user_columns and not with_names:
                            value = pseudonym(value) if value else value
                        values.append(value)
                    writer.writerow(values)
            archive.writestr(name, out.getvalue())
        archive.writestr("README.txt", README.format(
            when=db.now(),
            mode="z imeni" if with_names else "psevdonimizirani",
            idnote=("pravi id uporabnika" if with_names else
                    "psevdonim (Uxxxxxxxx), stalen med izvozi; imena so odstranjena"),
        ))
        archive.writestr("summary.json", json.dumps(
            {k: v for k, v in overview().items() if k != "tiles"},
            ensure_ascii=False, indent=1, default=str))
    return buffer.getvalue()
