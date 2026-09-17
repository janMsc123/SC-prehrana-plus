"""Client for the school's MALCOMAT system.

The site is a SvelteKit front end over a documented JSON API -- it publishes its
own OpenAPI description at /openapi.json, which is where these endpoints and
their shapes come from. Authentication is a session cookie issued by
/api/v1/auth/login.

Only five endpoints are used:

    login             POST /api/v1/auth/login
    get_self          GET  /api/v1/user/get_self
    get_menus_user    POST /api/v1/menu/get_menus_user      (read)
    get_order_days    GET  /api/v1/config/get_order_days    (read)
    get_user_orders   POST /api/v1/menu_order/get_user_orders (read)
    post_menu_order   POST /api/v1/menu_order/post_menu_order (WRITE)
    cancel_menu_order POST /api/v1/menu_order/cancel_menu_order (WRITE)

post_menu_order and cancel_menu_order are the only calls that change anything
at school. Both live behind the same explicit flag so a collect-only
deployment cannot reach either.
"""

from __future__ import annotations

import httpx

from .config import settings

TIMEOUT = httpx.Timeout(30.0, connect=15.0)


class MalcomatError(RuntimeError):
    """Any failure talking to the school's system."""


class AuthError(MalcomatError):
    """Bad credentials, or a session that is no longer valid."""


class Client:
    """A logged-in session. Use as a context manager."""

    def __init__(self, base_url=None):
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Identify ourselves honestly rather than impersonating a browser.
                "User-Agent": "prehrana-plus/1.0 (+https://github.com/)",
            },
        )
        self.user = None

    # -- lifecycle --------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self._http.close()

    # -- plumbing ---------------------------------------------------------

    def _request(self, method, path, json_body=None):
        try:
            response = self._http.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise MalcomatError("could not reach {}: {}".format(self.base_url, exc)) from exc

        if response.status_code in (401, 403):
            raise AuthError("not authenticated ({})".format(response.status_code))
        if response.status_code >= 400:
            raise MalcomatError(
                "{} {} -> {}: {}".format(
                    method, path, response.status_code, response.text[:200]
                )
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise MalcomatError("non-JSON reply from {}".format(path)) from exc

    # -- auth -------------------------------------------------------------

    def login(self, username, password):
        """Authenticate. Raises AuthError on bad credentials."""
        try:
            self._request(
                "POST",
                "/api/v1/auth/login",
                {"username": username, "password": password},
            )
        except MalcomatError as exc:
            # The API answers a wrong password with a 4xx, which _request has
            # already turned into an error; normalise it for callers.
            raise AuthError("login failed for {}: {}".format(username, exc)) from exc

        if not self._http.cookies.get("id"):
            raise AuthError("login did not return a session cookie")

        self.user = self.get_self()
        return self.user

    def get_self(self):
        return self._request("GET", "/api/v1/user/get_self")

    # -- reads ------------------------------------------------------------

    def get_menus(self, start_date, end_date, user_id=None):
        """The menu on offer between two dates (inclusive).

        Each row is one slot on one date. `menu_id` identifies the SLOT and is
        the same value on every date -- the dish is `menu_description`, which
        is null on days with no service.
        """
        body = {"start_date": str(start_date), "end_date": str(end_date)}
        if user_id:
            body["user_id"] = user_id
        return self._request("POST", "/api/v1/menu/get_menus_user", body) or []

    def get_order_days(self):
        """Dates that may be ordered right now, with cutoff times.

        Authoritative -- the picker uses this rather than guessing the window
        from min_lead_days/max_advance_days.
        """
        return self._request("GET", "/api/v1/config/get_order_days") or []

    def get_orders(self, start_date, end_date, user_id=None):
        body = {"start_date": str(start_date), "end_date": str(end_date)}
        if user_id:
            body["user_id"] = user_id
        return self._request("POST", "/api/v1/menu_order/get_user_orders", body) or []

    # -- the only write ---------------------------------------------------

    def place_order(self, slot_menu_id, order_date, user_id=None):
        """Order one slot on one date. This changes data at school.

        Guarded by PLACE_ORDERS so a collect-only deployment cannot order by
        accident even if this is called.
        """
        if not settings.place_orders:
            raise MalcomatError(
                "refusing to order: PLACE_ORDERS is disabled in this deployment"
            )
        body = {"menu_id": slot_menu_id, "order_date": str(order_date)}
        if user_id:
            body["user_id"] = user_id
        return self._request("POST", "/api/v1/menu_order/post_menu_order", body)

    def cancel_order(self, order_date, user_id=None):
        """Withdraw whatever is ordered for one date. This changes data at school.

        Guarded by PLACE_ORDERS like place_order, since it is the same kind of
        write -- just the other direction.
        """
        if not settings.place_orders:
            raise MalcomatError(
                "refusing to cancel: PLACE_ORDERS is disabled in this deployment"
            )
        body = {"order_date": str(order_date)}
        if user_id:
            body["user_id"] = user_id
        return self._request("POST", "/api/v1/menu_order/cancel_menu_order", body)


def verify_credentials(username, password):
    """Check a username/password against the school system.

    Used both by our login page and before storing a password for the weekly
    job, so we never persist credentials that do not work.
    """
    with Client() as client:
        return client.login(username, password)
