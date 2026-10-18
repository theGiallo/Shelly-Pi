"""Single shared password via HTTP Basic (user "admin"). Empty password = open.

Plain pass-through ASGI middleware (no BaseHTTPMiddleware) so server-sent
events stream without buffering.
"""

from __future__ import annotations

import base64
import binascii
import hmac

from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

USER = "admin"


class BasicAuthMiddleware:
    def __init__(self, app: ASGIApp, password: str, exempt: tuple[str, ...] = ("/healthz",)):
        self.app = app
        self.password = password
        self.exempt = exempt

    def _ok(self, header: str | None) -> bool:
        if not header or not header.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        user, _, pw = raw.partition(":")
        # Compare both, so timing doesn't reveal which half was wrong.
        ok_user = hmac.compare_digest(user.encode(), USER.encode())
        ok_pw = hmac.compare_digest(pw.encode(), self.password.encode())
        return ok_user and ok_pw

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.password or scope["path"] in self.exempt:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if self._ok(headers.get("authorization")):
            await self.app(scope, receive, send)
            return
        resp = Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Shelly Monitor", charset="UTF-8"'},
        )
        await resp(scope, receive, send)


class RequireCustomHeader:
    """Changes through the API need a header a web page on another site can't send.

    Browsers keep a page's Basic-auth login and attach it to requests from any site, so
    without this a page elsewhere on the internet could, say, switch plugs off or replace
    a backup. A custom header forces a cross-site request through a preflight that the
    server never approves, so it never goes out. Reading (GET) stays open.
    """

    HEADER = b"x-requested-with"

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http" and scope["path"].startswith("/api/")
                and scope["method"] not in ("GET", "HEAD", "OPTIONS")
                and not any(k == self.HEADER for k, _ in scope["headers"])):
            resp = Response('{"detail":"Missing X-Requested-With header"}', status_code=403,
                            media_type="application/json")
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


class HttpsRedirect:
    """When "HTTPS only" is on, plain-HTTP requests from other machines go to the HTTPS address.

    Requests that arrive through the local TLS listener come from 127.0.0.1 and are left alone, as
    are the health check and the download of the certificate (people need that one before
    their device trusts the Pi).
    """

    EXEMPT = ("/healthz", "/api/https/ca.crt")

    def __init__(self, app: ASGIApp, manager_getter):
        self.app = app
        self.get = manager_getter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in self.EXEMPT:
            mgr = self.get()
            client = (scope.get("client") or ("", 0))[0]
            if mgr is not None and client not in ("127.0.0.1", "::1") and mgr.redirecting():
                headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
                host = mgr.redirect_host(headers.get("host", ""))
                port = "" if mgr.cfg.https_port == 443 else f":{mgr.cfg.https_port}"
                query = scope.get("query_string", b"").decode("latin-1")
                target = f"https://{host}{port}{scope['path']}" + (f"?{query}" if query else "")
                await Response(status_code=308, headers={"Location": target})(scope, receive, send)
                return
        await self.app(scope, receive, send)
