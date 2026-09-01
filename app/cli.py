"""Command line entry points.

    python -m app.cli collect            read the menu and store it (no writes)
    python -m app.cli pick [--dry-run]   choose meals for the orderable window
    python -m app.cli stats              what has been collected so far
    python -m app.cli questions          identity questions still unanswered
    python -m app.cli refingerprint      rebuild meal identities from observations

Useful for a first run, for debugging, and for driving the jobs from the host's
cron instead of the built-in scheduler.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import collector, db, identity, picker


def cmd_collect(args):
    totals, errors = collector.run()
    print(json.dumps(totals, indent=2))
    for error in errors:
        print("error:", error, file=sys.stderr)
    return 1 if errors else 0


def cmd_pick(args):
    totals, errors = picker.run(dry_run=args.dry_run)
    print(json.dumps(totals, indent=2))
    for error in errors:
        print("error:", error, file=sys.stderr)
    return 1 if errors else 0


def cmd_stats(args):
    days = db.query_one("SELECT COUNT(DISTINCT menu_date) AS n FROM observation")["n"]
    observations = db.query_one("SELECT COUNT(*) AS n FROM observation")["n"]
    meals = len(db.load_meals())
    pending = db.query_one(
        "SELECT COUNT(*) AS n FROM match_question WHERE status='pending'"
    )["n"]
    span = db.query_one(
        "SELECT MIN(menu_date) AS a, MAX(menu_date) AS b FROM observation"
    )
    print("days collected   :", days, "({} .. {})".format(span["a"], span["b"]))
    print("observations     :", observations)
    print("distinct meals   :", meals)
    print("open questions   :", pending)

    repeats = db.query(
        """SELECT m.head, m.times_seen,
                  COUNT(DISTINCT o.menu_date) AS days
             FROM meal m JOIN observation o ON o.meal_id = m.id
            GROUP BY m.id HAVING days > 1
            ORDER BY days DESC LIMIT 15"""
    )
    if repeats:
        print("\nmeals seen on more than one day (the cycle, as observed):")
        for row in repeats:
            print("  {:3}x  {}".format(row["days"], row["head"]))
    return 0


def cmd_questions(args):
    rows = db.query(
        "SELECT * FROM match_question WHERE status='pending' ORDER BY score DESC"
    )
    if not rows:
        print("no open questions")
        return 0
    for row in rows:
        a = db.query_one("SELECT head, sample_description FROM meal WHERE id=?", (row["meal_a"],))
        b = db.query_one("SELECT head, sample_description FROM meal WHERE id=?", (row["meal_b"],))
        print("- {} ({:.0%})".format(row["reason"], row["score"]))
        print("    A:", a["head"] if a else "?")
        print("    B:", b["head"] if b else "?")
    print("\nanswer them at /review")
    return 0


def cmd_refingerprint(args):
    """Recompute meal identities after the FILLER set or parser changes.

    Rebuilds every meal from the stored observations. Meals that now share an
    identity are reported rather than merged -- merging stays a human decision,
    so this prints what to confirm at /review.
    """
    rows = db.query("SELECT DISTINCT description_raw FROM observation")
    buckets = {}
    for row in rows:
        parsed = identity.parse(row["description_raw"])
        buckets.setdefault(parsed.fingerprint, []).append(row["description_raw"])

    collisions = {fp: v for fp, v in buckets.items() if len(v) > 1}
    print("distinct descriptions:", len(rows))
    print("distinct identities  :", len(buckets))
    print("version              :", identity.FINGERPRINT_VERSION)
    if collisions:
        print("\ndescriptions that now share one identity:")
        for fingerprint, descriptions in collisions.items():
            print(" -", fingerprint[:70])
            for description in descriptions:
                print("     ", description[:90])
    else:
        print("\nno descriptions collapse together under the current rules")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="prehrana-plus")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("collect", help="read and store the menu").set_defaults(func=cmd_collect)

    pick_parser = subparsers.add_parser("pick", help="choose (and order) meals")
    pick_parser.add_argument(
        "--dry-run", action="store_true",
        help="work out the choices and record them without ordering",
    )
    pick_parser.set_defaults(func=cmd_pick)

    subparsers.add_parser("stats", help="what has been collected").set_defaults(func=cmd_stats)
    subparsers.add_parser("questions", help="open identity questions").set_defaults(func=cmd_questions)
    subparsers.add_parser("refingerprint", help="recheck meal identities").set_defaults(func=cmd_refingerprint)

    args = parser.parse_args(argv)
    db.init()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
