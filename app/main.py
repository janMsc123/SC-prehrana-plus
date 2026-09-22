"""Web application and scheduled jobs."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, collector, db, fallback, identity, overrides, picker, ranking, suggestions
from .config import settings
from .malcomat import AuthError, MalcomatError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("prehrana")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Prehrana Plus", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _scheduler_timezone():
    """Resolve the configured timezone, falling back to UTC.

    A typo in TZ should not stop the whole app from starting -- the jobs still
    need to run, just on UTC until it is corrected.
    """
    try:
        return ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "unknown timezone %r, falling back to UTC; set TZ to an IANA name "
            "such as Europe/Ljubljana",
            settings.timezone,
        )
        return ZoneInfo("UTC")


scheduler = BackgroundScheduler(timezone=_scheduler_timezone())

#: Weekday names for the countdown label, Monday first.
_DNEVI = (
    "ponedeljek", "torek", "sreda", "četrtek", "petek", "sobota", "nedelja",
)


def next_pick_time(now=None):
    """When the ordering job will next run.

    Read from the same cron fields the scheduler is given, so the countdown on
    the dashboard cannot drift away from what actually happens.
    """
    zone = _scheduler_timezone()
    trigger = CronTrigger(
        day_of_week=settings.pick_day_of_week,
        hour=settings.pick_hour,
        minute=settings.pick_minute,
        timezone=zone,
    )
    return trigger.get_next_fire_time(None, now or datetime.now(zone))


def pick_label(when):
    """"v ponedeljek ob 07:00", or None when the schedule cannot be read."""
    if when is None:
        return None
    return "v {} ob {:02d}:{:02d}".format(
        _DNEVI[when.weekday()], when.hour, when.minute
    )


#: Paths reachable while the one-off explainer is still outstanding.
_TUTORIAL_EXEMPT = ("/tutorial", "/logout", "/login", "/static", "/healthz", "/rank")


@app.middleware("http")
async def tutorial_gate(request: Request, call_next):
    """Hold new users on the explainer until they have read it.

    Gating in one place rather than per route means a link added later cannot
    accidentally become a way around it. Only page loads are intercepted; the
    rating screens stay reachable so a part-finished pass can be continued.
    """
    path = request.url.path
    if request.method == "GET" and not path.startswith(_TUTORIAL_EXEMPT):
        user = current_user(request)
        if user is not None and not user["tutorial_seen_at"]:
            if ranking.progress(user["id"])["done"]:
                return RedirectResponse("/tutorial", status_code=303)
    return await call_next(request)


# --- helpers -------------------------------------------------------------


def current_user(request: Request):
    return auth.get_user(auth.read_session(request.cookies.get(auth.SESSION_COOKIE)))


def require_user(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


@app.exception_handler(HTTPException)
async def redirect_on_auth(request: Request, exc: HTTPException):
    if exc.status_code == 303 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return HTMLResponse(
        "<h1>{}</h1><p>{}</p>".format(exc.status_code, exc.detail),
        status_code=exc.status_code,
    )


def _distinguish(meal_id, head, extras):
    """Tell two meals apart that share the same headline dish.

    identity.py deliberately keeps "CEVAPCICI + bombeta" and "CEVAPCICI +
    rice" as separate meals rather than guessing they are the same -- see its
    docstring. But showing both as plain "CEVAPCICI" in the rating list and
    the board makes them look like the exact same row twice, which reads as a
    bug even though the two really are different orders. Naming whichever
    side actually differs fixes the display without touching the matching
    rules that keep the two records apart in the first place.
    """
    if head == "(unnamed)":
        return head
    clash = db.query_one(
        """SELECT 1 FROM meal
            WHERE head = ? AND id != ?
              AND id NOT IN (SELECT meal_id FROM meal_alias)
            LIMIT 1""",
        (head, meal_id),
    )
    if clash is None:
        return head
    detail = next((e for e in extras if e not in identity.FILLER), None)
    if detail is None:
        detail = extras[0] if extras else None
    return "{} · {}".format(head, detail) if detail else head


def meal_view(meal_id):
    """A meal shaped for display, or None if it has gone away."""
    row = db.query_one("SELECT * FROM meal WHERE id = ?", (db.canonical_meal_id(meal_id),))
    if row is None:
        return None
    components = db.components_of(row)
    head = row["head"] or "(unnamed)"
    extras = [c for c in components if c != row["head"]]
    return {
        "id": row["id"],
        "name": _distinguish(row["id"], head, extras),
        "components": components,
        "extras": extras,
        "description": row["sample_description"],
        "times_seen": row["times_seen"],
        "first_seen": row["first_seen_date"],
        "last_seen": row["last_seen_date"],
    }


def pending_question_count():
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM match_question WHERE status = 'pending'"
    )
    return row["n"] if row else 0


def is_admin(user):
    """Whether this account maintains the shared meal archive.

    Answering an identity question merges two meals for *everyone*, so it is
    upkeep for whoever runs the instance rather than a per-user setting.
    """
    if user is None or not settings.admin_usernames:
        return False
    return (user["username"] or "").lower() in settings.admin_usernames


def require_admin(request: Request):
    user = require_user(request)
    if not is_admin(user):
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


def render(request, name, user, **context):
    context.setdefault("is_admin", is_admin(user))
    # Non-admins are never told about pending questions: they cannot act on
    # them, so the badge would only be noise.
    context.setdefault(
        "pending_questions", pending_question_count() if is_admin(user) else 0
    )
    context.setdefault("place_orders", settings.place_orders)
    context.setdefault("tie_window", ranking.TIE_WINDOW)
    context.setdefault("hide_nav", False)
    return templates.TemplateResponse(
        request, name, {"user": user, **context}
    )


# --- auth ----------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", None, error=None)


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: str = Form("on"),
):
    try:
        user = auth.login(username.strip(), password, remember=(remember == "on"))
    except AuthError:
        return render(
            request, "login.html", None,
            error="Napačno uporabniško ime ali geslo.",
        )
    except MalcomatError as exc:
        return render(
            request, "login.html", None,
            error="Šolski sistem ni dosegljiv: {}".format(exc),
        )

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.make_session(user["id"]),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


# --- dashboard -----------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(require_user)):
    """The whole app on one screen: the switch, the rating prompt, the days.

    The day board is built from the local archive rather than a live call to
    the school, so the page stays fast and still works when MALCOMAT is down.
    """
    stats = ranking.progress(user["id"])
    overrides.clear_past(user["id"])
    days = overrides.days(user)
    when = next_pick_time()

    return render(
        request, "dashboard.html", user,
        meal_count=len(db.load_meals()),
        progress=stats,
        rated_anything=stats["rated"] > 0,
        days=days,
        chosen_count=sum(1 for d in days if d["override"] or d["skipped"]),
        skip_value=overrides.SKIP,
        archive_empty=not db.query_one("SELECT COUNT(*) AS n FROM observation")["n"],
        next_pick_iso=when.isoformat() if when else None,
        next_pick_label=pick_label(when),
    )


# --- ranking -------------------------------------------------------------


@app.get("/rank", response_class=HTMLResponse)
def rank_page(request: Request, user=Depends(require_user)):
    kind, payload = ranking.next_question(user["id"])
    stats = ranking.progress(user["id"])

    if kind == "rate":
        meal = meal_view(payload)
        if meal is None:
            return RedirectResponse("/rank", status_code=303)
        return render(
            request, "rate.html", user, meal=meal, progress=stats,
            default_score=ranking.MAX_SCORE // 2,
        )

    # The pass is finished. First time through, send them to the explainer so
    # dragging and the highlighting are not left to be discovered by accident.
    just_finished = ranking.mark_onboarded(user["id"])
    if just_finished or not user["tutorial_seen_at"]:
        return RedirectResponse("/tutorial", status_code=303)

    return render(request, "rank_done.html", user,
                  total=len(ranking.current_ranking(user["id"])), progress=stats)


@app.post("/rank/rate")
def rank_rate(
    request: Request,
    meal_id: str = Form(...),
    score: int = Form(...),
    user=Depends(require_user),
):
    ranking.save_rating(user["id"], meal_id, score)
    return RedirectResponse("/rank", status_code=303)


@app.post("/rank/exclude")
def rank_exclude(
    request: Request, meal_id: str = Form(...), user=Depends(require_user)
):
    """The X button: never serve me this."""
    ranking.exclude(user["id"], meal_id)
    return RedirectResponse("/rank", status_code=303)


@app.post("/rank/restart")
def rank_restart(request: Request, user=Depends(require_user)):
    ranking.restart(user["id"])
    return RedirectResponse("/rank", status_code=303)


@app.get("/meals", response_class=HTMLResponse)
def meals_page(request: Request, user=Depends(require_user)):
    """The league table: the order, the rejected pile, and what is still unrated.

    All three columns are sent to the page together and come back together, so
    a meal can be dragged from any one of them into any other.
    """
    order = ranking.current_ranking(user["id"])
    scores = ranking.get_ratings(user["id"])
    close = ranking.close_to_neighbour(order, scores)
    fresh = ranking.unacknowledged(user["id"])
    excluded = ranking.excluded_meals(user["id"])

    ranked = []
    for index, meal_id in enumerate(order):
        view = meal_view(meal_id)
        if view is None:
            continue
        view["position"] = index + 1
        view["score"] = scores.get(meal_id)
        view["close"] = meal_id in close
        view["is_new"] = meal_id in fresh
        ranked.append(view)

    # The daily fallback is the floor everything else is measured against, not
    # one of the choices, so it is left out of the pile waiting to be rated.
    floor = fallback.meal_ids()
    rejected, unrated = [], []
    for meal_id in db.load_meals():
        if meal_id in scores or meal_id in floor:
            continue
        view = meal_view(meal_id)
        if view is None:
            continue
        (rejected if meal_id in excluded else unrated).append(view)
    rejected.sort(key=lambda m: m["name"])
    unrated.sort(key=lambda m: (-m["times_seen"], m["name"]))

    return render(request, "meals.html", user, ranked=ranked, rejected=rejected,
                  unrated=unrated, new_count=len(fresh))


@app.post("/meals/save")
def meals_save(
    request: Request,
    order: str = Form(""),
    rejected: str = Form(""),
    unrated: str = Form(""),
    scores: str = Form(""),
    user=Depends(require_user),
):
    """Save the arranged list. The three columns arrive as comma-separated ids."""
    def ids(raw):
        return [part for part in (raw or "").split(",") if part]

    try:
        typed = json.loads(scores) if scores else {}
    except ValueError:
        typed = {}

    ranking.apply_board(
        user["id"], ids(order), ids(rejected), ids(unrated),
        typed if isinstance(typed, dict) else {},
    )
    return RedirectResponse("/meals", status_code=303)


@app.post("/meals/acknowledge")
def meals_acknowledge(request: Request, user=Depends(require_user)):
    ranking.acknowledge_new(user["id"])
    return RedirectResponse("/meals", status_code=303)


# --- identity questions --------------------------------------------------


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request, user=Depends(require_admin)):
    rows = db.query(
        "SELECT * FROM match_question WHERE status = 'pending' ORDER BY score DESC"
    )
    questions = []
    for row in rows:
        a, b = meal_view(row["meal_a"]), meal_view(row["meal_b"])
        if a is None or b is None or a["id"] == b["id"]:
            # Already resolved by an earlier merge.
            db.execute(
                "UPDATE match_question SET status = 'same', answered_at = ? WHERE id = ?",
                (db.now(), row["id"]),
            )
            continue
        questions.append(
            {"id": row["id"], "a": a, "b": b,
             "score": row["score"], "reason": row["reason"]}
        )
    return render(request, "review.html", user, questions=questions)


@app.post("/review/answer")
def review_answer(
    request: Request,
    question_id: str = Form(...),
    verdict: str = Form(...),
    user=Depends(require_admin),
):
    collector.answer_question(question_id, same=(verdict == "same"))
    return RedirectResponse("/review", status_code=303)


# --- picking by hand -----------------------------------------------------

# The day board lives on the dashboard, so both of these come back to it. The
# fragment reopens the day that was just changed instead of the first one.


def _apply_override_now(user, order_date):
    """Push the day's just-saved choice to school instead of waiting for the
    next scheduled run. Best-effort: a failure here is logged and the board
    still shows the override, ready to be picked up by the next run."""
    try:
        picker.apply_override(user, order_date)
    except (MalcomatError, AuthError) as exc:
        log.warning("instant order failed for %s %s: %s",
                    user["username"], order_date, exc)


@app.post("/override/set")
def override_set(
    request: Request,
    order_date: str = Form(...),
    slot_menu_id: str = Form(...),
    meal_id: str = Form(""),
    user=Depends(require_user),
):
    overrides.set_override(user["id"], order_date, slot_menu_id, meal_id or None)
    _apply_override_now(user, order_date)
    return RedirectResponse("/#d-{}".format(order_date), status_code=303)


@app.post("/override/clear")
def override_clear(
    request: Request, order_date: str = Form(...), user=Depends(require_user)
):
    overrides.clear(user["id"], order_date)
    _apply_override_now(user, order_date)
    return RedirectResponse("/#d-{}".format(order_date), status_code=303)


# --- manual job triggers -------------------------------------------------


@app.post("/run/collect")
def run_collect(request: Request, user=Depends(require_user)):
    """Read the menu now. Offered only while the archive is still empty: after
    that the daily job keeps it current and there is nothing to press."""
    try:
        stats = collector.collect_for_user(user)
        log.info("manual collect: %s", stats)
    except (MalcomatError, AuthError) as exc:
        log.warning("manual collect failed: %s", exc)
    return RedirectResponse("/", status_code=303)


@app.get("/tutorial", response_class=HTMLResponse)
def tutorial_page(request: Request, user=Depends(require_user)):
    # Nav is suppressed so the only way onward is the acknowledge button.
    return render(request, "tutorial.html", user, hide_nav=True,
                  progress=ranking.progress(user["id"]))


@app.post("/tutorial/done")
def tutorial_done(request: Request, user=Depends(require_user)):
    auth.mark_tutorial_seen(user["id"])
    return RedirectResponse("/meals", status_code=303)


@app.get("/suggest", response_class=HTMLResponse)
def suggest_page(request: Request, user=Depends(require_user)):
    mine = db.query(
        "SELECT * FROM suggestion WHERE user_id = ? ORDER BY created_at DESC LIMIT 5",
        (user["id"],),
    )
    return render(request, "suggest.html", user, sent=None, error=None,
                  mine=[dict(row) for row in mine],
                  max_length=suggestions.MAX_LENGTH)


@app.post("/suggest")
def suggest_submit(
    request: Request, body: str = Form(...), user=Depends(require_user)
):
    error = None
    delivered = False
    try:
        _id, delivered = suggestions.record(user, body)
    except ValueError:
        error = "Predlog je prazen."
    except suggestions.RateLimited:
        error = "Preveč predlogov naenkrat. Poskusi čez kakšno uro."

    mine = db.query(
        "SELECT * FROM suggestion WHERE user_id = ? ORDER BY created_at DESC LIMIT 5",
        (user["id"],),
    )
    return render(request, "suggest.html", user,
                  sent=(None if error else delivered), error=error,
                  mine=[dict(row) for row in mine],
                  max_length=suggestions.MAX_LENGTH)


@app.post("/settings/autopilot")
def toggle_autopilot(
    request: Request, enabled: str = Form("off"), user=Depends(require_user)
):
    """The big switch: whether the weekly job orders for you.

    Turning it off only stops the ordering. The stored password is kept so the
    switch can be flipped back on without logging in again; use "forget my
    password" to remove it.
    """
    auth.set_autopilot(user["id"], enabled == "on")
    return RedirectResponse("/", status_code=303)


@app.post("/settings/forget-password")
def forget_password(request: Request, user=Depends(require_user)):
    auth.forget_password(user["id"])
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
def healthz():
    db.query_one("SELECT 1")
    return {"ok": True, "place_orders": settings.place_orders}


# --- lifecycle -----------------------------------------------------------


@app.on_event("startup")
def startup():
    db.init()

    scheduler.add_job(
        collector.run,
        CronTrigger(hour=settings.collect_hour, minute=settings.collect_minute),
        id="collect",
        replace_existing=True,
    )
    scheduler.add_job(
        picker.run,
        CronTrigger(
            day_of_week=settings.pick_day_of_week,
            hour=settings.pick_hour,
            minute=settings.pick_minute,
        ),
        id="pick",
        replace_existing=True,
    )
    scheduler.start()
    log.info(
        "started; collect daily %02d:%02d, pick %s %02d:%02d, place_orders=%s",
        settings.collect_hour, settings.collect_minute,
        "daily" if settings.pick_day_of_week.strip() == "*"
        else settings.pick_day_of_week,
        settings.pick_hour, settings.pick_minute,
        settings.place_orders,
    )


@app.on_event("shutdown")
def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)
