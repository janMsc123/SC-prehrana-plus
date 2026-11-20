"""MCP server, so Claude can work with your own lunches.

Two pieces live here:

  * an OAuth 2.1 authorization server (authorization code + PKCE, dynamic client
    registration) whose "login" is the school login the web app already uses,
    so every classmate only ever reaches their own data;
  * the MCP endpoint itself (`POST /mcp`, Streamable HTTP, JSON replies).

Nothing is stored for OAuth. Client ids, codes and tokens are signed blobs
(itsdangerous, keyed from SECRET_KEY), so there is no new table; changing
SECRET_KEY signs every connector out, exactly as it does for web sessions. Codes
are made single-use by remembering their ids in memory until they expire.

The tools mirror what the dashboard does for the signed-in user and nothing
more: no admin actions, no password handling, no collecting.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import auth, db, fallback, overrides, picker, plans, ranking, school
from .config import settings
from .malcomat import AuthError, MalcomatError

log = logging.getLogger("prehrana.mcp")

router = APIRouter()

CODE_TTL = 600
ACCESS_TTL = 60 * 60
REFRESH_TTL = 60 * 60 * 24 * 60
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_client_ser = URLSafeTimedSerializer(settings.secret_key, salt="mcp-client")
_code_ser = URLSafeTimedSerializer(settings.secret_key, salt="mcp-code")
_access_ser = URLSafeTimedSerializer(settings.secret_key, salt="mcp-access")
_refresh_ser = URLSafeTimedSerializer(settings.secret_key, salt="mcp-refresh")

_used_lock = threading.Lock()
_used = {}  # single-use id -> expiry (monotonic)


def _consume(token_id, ttl):
    """True the first time an id is seen, False on every replay."""
    now = time.monotonic()
    with _used_lock:
        for key in [k for k, exp in _used.items() if exp < now]:
            del _used[key]
        if token_id in _used:
            return False
        _used[token_id] = now + ttl
        return True


def issuer():
    return settings.public_url


# --- discovery -----------------------------------------------------------


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource():
    return {
        "resource": issuer() + "/mcp",
        "authorization_servers": [issuer()],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["prehrana"],
    }


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
def authorization_server():
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["prehrana"],
    }


# --- OAuth ---------------------------------------------------------------


def _oauth_error(error, description, status=400):
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status, headers={"Cache-Control": "no-store"},
    )


def _redirect_ok(uri):
    """https anywhere, or http only on loopback (Claude Code's local callback)."""
    parts = urlsplit(uri)
    if parts.scheme == "https" and parts.netloc:
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


@router.post("/oauth/register")
async def register(request: Request):
    try:
        body = await request.json()
        uris = body["redirect_uris"]
        assert isinstance(uris, list) and uris and len(uris) <= 10
        assert all(isinstance(u, str) and _redirect_ok(u) for u in uris)
    except Exception:
        return _oauth_error("invalid_redirect_uri", "redirect_uris must be https or loopback URLs")
    name = str(body.get("client_name") or "MCP client")[:80]
    client_id = _client_ser.dumps({"ru": uris, "name": name})
    return JSONResponse(
        {
            "client_id": client_id,
            "client_name": name,
            "redirect_uris": uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


def _read_client(client_id):
    try:
        return _client_ser.loads(client_id or "")
    except BadSignature:
        return None


_PAGE = """<!doctype html><html lang="sl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prehrana Plus · povezava s Claudom</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:26rem;margin:12vh auto;padding:0 1rem;
background:#fafaf7;color:#1c1c1a}}
@media(prefers-color-scheme:dark){{body{{background:#161614;color:#eee}}input{{background:#222;color:#eee}}}}
h1{{font-size:1.3rem}}input,button{{font:inherit;padding:.6rem;width:100%;box-sizing:border-box;margin:.3rem 0}}
button{{cursor:pointer}}.go{{background:#2f7d4f;color:#fff;border:0;border-radius:.4rem}}
.no{{background:none;border:1px solid #888;border-radius:.4rem;color:inherit}}
.err{{color:#c0392b}}
</style>
<h1>Poveži Claude s Prehrana Plus</h1>
<p><b>{client}</b> bo lahko videl tvoje jedilnike, lestvico in naročila, ocenjeval jedi,
izbiral obroke in vklopil ali izklopil samodejno naročanje. Gesla ne dobi.</p>
{error}
<form method="post" action="/oauth/authorize">
<input type="hidden" name="state_token" value="{token}">
<input type="hidden" name="csrf" value="{csrf}">
{login}
<button class="go" name="decision" value="allow">Dovoli</button>
<button class="no" name="decision" value="deny">Prekliči</button>
</form></html>"""

_LOGIN = (
    '<p>Prijavi se s šolskim računom:</p>'
    '<input name="username" placeholder="Uporabniško ime" autocomplete="username" required>'
    '<input name="password" type="password" placeholder="Geslo" '
    'autocomplete="current-password" required>'
    '<label style="display:flex;gap:.5rem;align-items:flex-start;margin:.4rem 0 .8rem;font-size:.9rem">'
    '<input type="checkbox" name="remember" value="on" checked style="width:auto;margin:.2rem 0 0">'
    '<span>Zapomni si geslo, da lahko samodejno naročam namesto mene '
    '(shranjeno šifrirano; izbrišeš ga lahko kadarkoli v aplikaciji).</span></label>'
)


def _consent_page(request, params, client_name, user, error=""):
    token = _code_ser.dumps(params)
    csrf = request.cookies.get("mcp_csrf") or secrets.token_urlsafe(24)
    body = _PAGE.format(
        client=html.escape(client_name),
        token=html.escape(token),
        csrf=html.escape(csrf),
        login="" if user else _LOGIN,
        error='<p class="err">{}</p>'.format(html.escape(error)) if error else "",
    )
    response = HTMLResponse(body, headers={"X-Frame-Options": "DENY", "Cache-Control": "no-store"})
    response.set_cookie("mcp_csrf", csrf, max_age=3600, httponly=True,
                        samesite="strict", secure=True, path="/oauth")
    return response


def _session_user(request):
    return auth.get_user(auth.read_session(request.cookies.get(auth.SESSION_COOKIE)))


@router.get("/oauth/authorize")
def authorize_form(request: Request):
    q = request.query_params
    client = _read_client(q.get("client_id"))
    redirect_uri = q.get("redirect_uri")
    if client is None or redirect_uri not in client["ru"]:
        return HTMLResponse("<h1>Neveljavna zahteva</h1>", status_code=400)
    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" \
            or not q.get("code_challenge"):
        return _redirect_error(redirect_uri, q.get("state"), "invalid_request",
                               "PKCE with S256 is required")
    params = {
        "cid": q["client_id"], "ru": redirect_uri, "cc": q["code_challenge"],
        "state": q.get("state"),
    }
    return _consent_page(request, params, client["name"], _session_user(request))


def _redirect_error(redirect_uri, state, error, description):
    query = {"error": error, "error_description": description}
    if state:
        query["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + sep + urlencode(query), status_code=303)


@router.post("/oauth/authorize")
async def authorize_submit(request: Request):
    form = await request.form()
    try:
        params = _code_ser.loads(form.get("state_token", ""), max_age=1800)
    except BadSignature:
        return HTMLResponse("<h1>Seja je potekla, poskusi znova.</h1>", status_code=400)
    cookie = request.cookies.get("mcp_csrf")
    if not cookie or not secrets.compare_digest(cookie, str(form.get("csrf", ""))):
        return HTMLResponse("<h1>Neveljavna zahteva</h1>", status_code=400)
    client = _read_client(params["cid"])
    if client is None or params["ru"] not in client["ru"]:
        return HTMLResponse("<h1>Neveljavna zahteva</h1>", status_code=400)

    if form.get("decision") != "allow":
        return _redirect_error(params["ru"], params["state"], "access_denied", "denied")

    user = _session_user(request)
    if user is None:
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        try:
            user = await run_in_threadpool(
                auth.login, username, password,
                remember=(form.get("remember") == "on"))
        except AuthError:
            return _consent_page(request, params, client["name"], None,
                                 error="Napačno uporabniško ime ali geslo.")
        except MalcomatError as exc:
            return _consent_page(request, params, client["name"], None,
                                 error="Šolski sistem ni dosegljiv: {}".format(exc))
        db.log_event(user["id"], "login", via="mcp")

    code = _code_ser.dumps({
        "uid": user["id"], "cid": params["cid"], "ru": params["ru"],
        "cc": params["cc"], "jti": secrets.token_urlsafe(12),
    })
    query = {"code": code}
    if params["state"]:
        query["state"] = params["state"]
    sep = "&" if "?" in params["ru"] else "?"
    db.log_event(user["id"], "mcp.connect", client=client["name"])
    return RedirectResponse(params["ru"] + sep + urlencode(query), status_code=303)


def _tokens(user_id):
    return JSONResponse(
        {
            "access_token": _access_ser.dumps({"uid": user_id}),
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": _refresh_ser.dumps({"uid": user_id, "jti": secrets.token_urlsafe(12)}),
            "scope": "prehrana",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    grant = form.get("grant_type")

    if grant == "authorization_code":
        try:
            data = _code_ser.loads(str(form.get("code", "")), max_age=CODE_TTL)
        except BadSignature:
            return _oauth_error("invalid_grant", "code is invalid or expired")
        if "jti" not in data or not _consume("code:" + data["jti"], CODE_TTL):
            return _oauth_error("invalid_grant", "code was already used")
        if form.get("client_id") != data["cid"] or form.get("redirect_uri") != data["ru"]:
            return _oauth_error("invalid_grant", "client or redirect_uri mismatch")
        verifier = str(form.get("code_verifier", ""))
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii", "ignore")).digest()
        ).rstrip(b"=").decode("ascii")
        if not verifier or not secrets.compare_digest(digest, data["cc"]):
            return _oauth_error("invalid_grant", "PKCE verification failed")
        if auth.get_user(data["uid"]) is None:
            return _oauth_error("invalid_grant", "unknown user")
        return _tokens(data["uid"])

    if grant == "refresh_token":
        try:
            data = _refresh_ser.loads(str(form.get("refresh_token", "")), max_age=REFRESH_TTL)
        except BadSignature:
            return _oauth_error("invalid_grant", "refresh token is invalid or expired")
        if not _consume("refresh:" + data["jti"], REFRESH_TTL):
            return _oauth_error("invalid_grant", "refresh token was already used")
        if auth.get_user(data["uid"]) is None:
            return _oauth_error("invalid_grant", "unknown user")
        return _tokens(data["uid"])

    return _oauth_error("unsupported_grant_type", "use authorization_code or refresh_token")


# --- tools ---------------------------------------------------------------


def _meal_name(meal_id):
    row = db.query_one("SELECT head, sample_description FROM meal WHERE id = ?",
                       (db.canonical_meal_id(meal_id),))
    if row is None:
        return None
    return row["head"] or row["sample_description"]


def _slot_arg(value):
    """"3", "meni 3" or "MALICA 3" -> "MALICA 3"; "skip" -> overrides.SKIP."""
    text = " ".join(str(value or "").split()).upper()
    if text in ("SKIP", "NONE", "NOTHING"):
        return overrides.SKIP
    text = text.replace("MENI", "MALICA")
    if text.isdigit():
        text = "MALICA " + text
    return text


def _push_now(user, order_date):
    """Send the saved choice to the school now (best effort, as the web app does)."""
    try:
        picker.apply_override(user, order_date)
        return None
    except (MalcomatError, AuthError) as exc:
        log.warning("instant order failed for %s %s: %s", user["username"], order_date, exc)
        return "Saved, but the school could not be reached; the next scheduled run will place it."


def _day_json(day):
    def slot(s):
        return {
            "slot": s["label"], "meal": s["name"], "meal_id": s["meal_id"],
            "rank": None if s["position"] is None else s["position"] + 1,
            "rejected": s["excluded"], "ordered": s["is_ordered"],
            "will_order": s["is_target"], "picked_by_hand": s["is_override"],
        }

    return {
        "date": day["date"],
        "weekday": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[day["weekday"]],
        "state": day["state"],
        "locked": day["locked"],
        "skipped": day["skipped"] or day["plan_skip"],
        "slots": [slot(s) for s in day["slots"]],
    }


def tool_get_status(user, args):
    progress = ranking.progress(user["id"])
    plan = plans.for_user(user)
    return {
        "autopilot": bool(user["autopilot"]),
        "has_stored_password": bool(user["password_enc"]),
        "menu_plan": plan.key,
        "dishes_rated": progress["rated"],
        "dishes_still_to_rate": progress["to_rate"],
        "dishes_rejected": progress["excluded"],
        "new_dishes": progress["new"],
        "orders_are_real": settings.place_orders,
    }


def tool_list_days(user, args):
    count = max(1, min(int(args.get("count", 10)), 28))
    days = overrides.days(user, school_orders=school.orders_by_date(user["id"]))
    return [_day_json(d) for d in days[:count]]


def tool_get_ranking(user, args):
    limit = max(1, min(int(args.get("limit", 30)), 200))
    scores = ranking.get_ratings(user["id"])
    out = []
    for i, meal_id in enumerate(ranking.current_ranking(user["id"])[:limit]):
        name = _meal_name(meal_id)
        if name:
            out.append({"rank": i + 1, "meal_id": meal_id, "meal": name,
                        "score": scores.get(meal_id)})
    return out


def tool_list_unrated(user, args):
    limit = max(1, min(int(args.get("limit", 15)), 50))
    out = []
    for meal_id in ranking.unrated_meals(user["id"])[:limit]:
        row = db.query_one("SELECT head, sample_description, times_seen FROM meal WHERE id = ?",
                           (meal_id,))
        if row:
            out.append({"meal_id": meal_id, "meal": row["head"],
                        "description": row["sample_description"],
                        "times_seen": row["times_seen"]})
    return out


def _known_meal(meal_id):
    meal_id = db.canonical_meal_id(str(meal_id or ""))
    if meal_id not in db.load_meals():
        raise ValueError("unknown meal_id")
    if meal_id in fallback.meal_ids():
        raise ValueError("this is the daily fallback; it is not rated")
    return meal_id


def tool_rate_meal(user, args):
    meal_id = _known_meal(args.get("meal_id"))
    score = int(args.get("score"))
    if not ranking.MIN_SCORE <= score <= ranking.MAX_SCORE:
        raise ValueError("score must be 1-100")
    ranking.save_rating(user["id"], meal_id, score)
    db.log_event(user["id"], "mcp.rate", meal_id=meal_id, score=score)
    return {"ok": True, "meal": _meal_name(meal_id), "score": score}


def tool_reject_meal(user, args):
    meal_id = _known_meal(args.get("meal_id"))
    ranking.exclude(user["id"], meal_id)
    db.log_event(user["id"], "mcp.exclude", meal_id=meal_id)
    return {"ok": True, "meal": _meal_name(meal_id), "rejected": True}


def tool_set_pick(user, args):
    order_date = str(args.get("date", ""))[:10]
    date.fromisoformat(order_date)
    if overrides.window_locked(user["username"], order_date):
        raise ValueError("{} is locked for changes (past, or after the 14:00 cutoff)"
                         .format(order_date))
    slot_name = _slot_arg(args.get("slot"))
    day = next((d for d in overrides.days(user) if d["date"] == order_date), None)
    if day is None:
        raise ValueError("no menu known for {}".format(order_date))
    if slot_name == overrides.SKIP:
        slot_id, meal_id, label = overrides.SKIP, None, "no order"
    else:
        slot = next((s for s in day["slots"] if s["slot_name"].upper() == slot_name), None)
        if slot is None:
            raise ValueError("no {} on {}; available: {}".format(
                slot_name, order_date, ", ".join(s["slot_name"] for s in day["slots"])))
        slot_id, meal_id, label = slot["slot_menu_id"], slot["meal_id"], slot["name"]
    overrides.set_override(user["id"], order_date, slot_id, meal_id, source="claude")
    db.log_event(user["id"], "override.set", order_date, slot_menu_id=slot_id,
                 meal_id=meal_id, via="mcp")
    result = {"ok": True, "date": order_date, "pick": label}
    note = _push_now(user, order_date)
    if note:
        result["note"] = note
    return result


def tool_clear_pick(user, args):
    order_date = str(args.get("date", ""))[:10]
    date.fromisoformat(order_date)
    if overrides.window_locked(user["username"], order_date):
        raise ValueError("{} is locked for changes".format(order_date))
    overrides.clear(user["id"], order_date)
    db.log_event(user["id"], "override.clear", order_date, via="mcp")
    result = {"ok": True, "date": order_date, "now": "back to automatic"}
    note = _push_now(user, order_date)
    if note:
        result["note"] = note
    return result


def tool_set_autopilot(user, args):
    enabled = args.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    auth.set_autopilot(user["id"], enabled)
    db.log_event(user["id"], "autopilot", on=enabled, via="mcp")
    return {"ok": True, "autopilot": enabled}


def _schema(props=None, required=()):
    return {"type": "object", "properties": props or {}, "required": list(required)}


_DATE = {"type": "string", "description": "YYYY-MM-DD"}

TOOLS = {
    "get_status": (
        "Your account at a glance: whether automatic ordering is on, the menu plan, "
        "and how many dishes are rated.",
        _schema(), tool_get_status, True),
    "list_days": (
        "Upcoming school days with every menu slot (MENI 1-7), the dish in each, your "
        "ranking of it, and what is ordered / will be ordered.",
        _schema({"count": {"type": "integer", "description": "days to show, default 10, max 28"}}),
        tool_list_days, True),
    "get_ranking": (
        "Your dishes, best first, with the 1-100 score you gave each.",
        _schema({"limit": {"type": "integer", "description": "default 30"}}),
        tool_get_ranking, True),
    "list_unrated": (
        "Dishes you have not scored or rejected yet, most frequently served first.",
        _schema({"limit": {"type": "integer", "description": "default 15"}}),
        tool_list_unrated, True),
    "rate_meal": (
        "Give a dish a score from 1 (hate) to 100 (love). Changes the ranking and so what "
        "gets ordered.",
        _schema({"meal_id": {"type": "string"}, "score": {"type": "integer"}},
                ["meal_id", "score"]),
        tool_rate_meal, False),
    "reject_meal": (
        "Reject a dish: it will never be ordered automatically or asked about again.",
        _schema({"meal_id": {"type": "string"}}, ["meal_id"]),
        tool_reject_meal, False),
    "set_pick": (
        "Choose the meal for one date by hand (beats the ranking) and order it at school "
        "now. slot is 'MENI 3' / '3', or 'skip' to order nothing that day. Refused once "
        "the day is locked.",
        _schema({"date": _DATE, "slot": {"type": "string"}}, ["date", "slot"]),
        tool_set_pick, False),
    "clear_pick": (
        "Remove the hand-made choice for a date so the automatic pick applies again.",
        _schema({"date": _DATE}, ["date"]), tool_clear_pick, False),
    "set_autopilot": (
        "Turn the automatic daily ordering on or off.",
        _schema({"enabled": {"type": "boolean"}}, ["enabled"]),
        tool_set_autopilot, False),
}


#: Tools that overwrite or remove something the user set earlier.
_DESTRUCTIVE = {"rate_meal", "reject_meal", "set_pick", "clear_pick"}


def _tool_list():
    return [
        {
            "name": name, "title": name.replace("_", " ").capitalize(),
            "description": desc, "inputSchema": schema,
            "annotations": {
                "title": name.replace("_", " ").capitalize(),
                "readOnlyHint": read_only,
                "destructiveHint": name in _DESTRUCTIVE,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }
        for name, (desc, schema, _fn, read_only) in TOOLS.items()
    ]


# --- MCP endpoint --------------------------------------------------------


def _bearer_user(request):
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    try:
        data = _access_ser.loads(header[7:].strip(), max_age=ACCESS_TTL)
    except BadSignature:
        return None
    return auth.get_user(data.get("uid"))


def _rpc_result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _handle(user, msg):
    """One JSON-RPC message -> a reply dict, or None for notifications."""
    if not isinstance(msg, dict) or "method" not in msg:
        return None  # a stray response from the client; nothing to answer
    msg_id, method = msg.get("id"), msg["method"]
    if "id" not in msg:
        return None  # notification

    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion")
        return _rpc_result(msg_id, {
            "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "prehrana-plus", "version": "1.0"},
        })
    if method == "ping":
        return _rpc_result(msg_id, {})
    if method == "tools/list":
        return _rpc_result(msg_id, {"tools": _tool_list()})
    if method == "tools/call":
        params = msg.get("params") or {}
        entry = TOOLS.get(params.get("name"))
        if entry is None:
            return _rpc_error(msg_id, -32602, "unknown tool")
        try:
            data = entry[2](user, params.get("arguments") or {})
            text, is_error = json.dumps(data, ensure_ascii=False, indent=1), False
        except (ValueError, TypeError, KeyError) as exc:
            text, is_error = "Error: {}".format(exc), True
        except (MalcomatError, AuthError) as exc:
            text, is_error = "School system error: {}".format(exc), True
        return _rpc_result(msg_id, {"content": [{"type": "text", "text": text}],
                                    "isError": is_error})
    return _rpc_error(msg_id, -32601, "method not found")


def _handle_all(user, payload):
    if isinstance(payload, list):
        replies = [r for r in (_handle(user, m) for m in payload) if r is not None]
        return replies or None
    return _handle(user, payload)


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    user = _bearer_user(request)
    if user is None:
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer resource_metadata="{}/.well-known/oauth-protected-resource"'
                     .format(issuer())},
        )
    if not user["claude_used_at"]:
        db.execute("UPDATE app_user SET claude_used_at = ? WHERE id = ?",
                   (datetime.now(timezone.utc).isoformat(timespec="seconds"), user["id"]))
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(_rpc_error(None, -32700, "parse error"), status_code=400)
    reply = await run_in_threadpool(_handle_all, user, payload)
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(reply)


@router.get("/mcp")
def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})
