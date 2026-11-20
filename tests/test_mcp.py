"""MCP endpoint and its OAuth flow."""

import base64
import hashlib

from fastapi.testclient import TestClient

from app import auth, db, main

db.init()
client = TestClient(main.app, base_url="https://testserver", follow_redirects=False)


def _user():
    uid = db.new_id()
    db.execute(
        "INSERT INTO app_user (id, username, autopilot, created_at) VALUES (?, ?, 1, ?)",
        (uid, "u" + uid[:6], db.now()),
    )
    return uid


def _rpc(token, method, params=None, id=1):
    return client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}},
        headers={"Authorization": "Bearer " + token} if token else {},
    )


def test_requires_auth():
    r = _rpc(None, "ping")
    assert r.status_code == 401
    assert "resource_metadata" in r.headers["www-authenticate"]


def test_tools_and_status():
    from app import mcp
    token = mcp._access_ser.dumps({"uid": _user()})
    assert _rpc(token, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    names = {t["name"] for t in _rpc(token, "tools/list").json()["result"]["tools"]}
    assert {"list_days", "set_pick", "rate_meal"} <= names
    r = _rpc(token, "tools/call", {"name": "get_status", "arguments": {}}).json()
    assert r["result"]["isError"] is False
    bad = _rpc(token, "tools/call", {"name": "rate_meal", "arguments": {"meal_id": "x", "score": 5}}).json()
    assert bad["result"]["isError"] is True


def test_oauth_flow_pkce():
    reg = client.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    cid = reg["client_id"]
    verifier = "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"response_type": "code", "client_id": cid, "redirect_uri": "https://claude.ai/cb",
         "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"}
    page = client.get("/oauth/authorize", params=q)
    assert page.status_code == 200
    import re
    tok = re.search(r'name="state_token" value="([^"]+)"', page.text).group(1).replace("&#x27;", "'")
    csrf = client.cookies.get("mcp_csrf")
    uid = _user()
    client.cookies.set(auth.SESSION_COOKIE, auth.make_session(uid))
    r = client.post("/oauth/authorize", data={"state_token": tok, "csrf": csrf, "decision": "allow"})
    assert r.status_code == 303
    code = re.search(r"code=([^&]+)", r.headers["location"]).group(1)
    t = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": cid,
        "redirect_uri": "https://claude.ai/cb", "code_verifier": verifier})
    assert t.status_code == 200 and t.json()["access_token"]
    assert _rpc(t.json()["access_token"], "ping").json()["result"] == {}
    # replay and wrong verifier both fail
    again = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": cid,
        "redirect_uri": "https://claude.ai/cb", "code_verifier": verifier})
    assert again.status_code == 400
    client.cookies.clear()


def test_claude_picks_are_marked_and_a_hand_pick_clears_the_mark():
    import datetime
    from app import overrides
    uid = _user()
    day = str(datetime.date.today() + datetime.timedelta(days=3))
    overrides.set_override(uid, day, "s1", None, source="claude")
    assert overrides.get(uid, day)["source"] == "claude"
    overrides.set_override(uid, day, "s1", None)
    assert overrides.get(uid, day)["source"] is None


def test_collect_logs_in_as_one_account_only(monkeypatch):
    from app import collector
    from app.config import settings
    seen = []
    monkeypatch.setattr(collector, "collect_for_user",
                        lambda u, today=None: seen.append(u["username"]) or
                        {"new_observations": 0, "new_meals": 0, "questions": 0})
    for name in ("the_one", "someone_else"):
        db.execute("INSERT INTO app_user (id, username, password_enc, autopilot, created_at) "
                   "VALUES (?, ?, 'x', 1, ?)", (db.new_id(), name, db.now()))
    monkeypatch.setattr(settings, "collect_username", "the_one")
    totals, errors = collector.run()
    assert seen == ["the_one"] and totals["users"] == 1 and not errors
    monkeypatch.setattr(settings, "collect_username", "nobody_here")
    assert collector.run()[1]


def test_connector_signin_saves_password_unless_unticked(monkeypatch):
    import re
    from app import mcp
    calls = []
    monkeypatch.setattr(mcp.auth, "login",
                        lambda u, p, remember=True: calls.append(remember) or
                        db.query_one("SELECT * FROM app_user WHERE id = ?", (_user(),)))
    reg = client.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/cb"]}).json()
    q = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/cb",
         "code_challenge": "x" * 43, "code_challenge_method": "S256"}
    for ticked in (True, False):
        client.cookies.clear()
        page = client.get("/oauth/authorize", params=q)
        assert 'name="remember" value="on" checked' in page.text
        tok = re.search(r'name="state_token" value="([^"]+)"', page.text).group(1).replace("&#x27;", "'")
        data = {"state_token": tok, "csrf": client.cookies.get("mcp_csrf"), "decision": "allow",
                "username": "u", "password": "p"}
        if ticked:
            data["remember"] = "on"
        assert client.post("/oauth/authorize", data=data).status_code == 303
    assert calls == [True, False]
