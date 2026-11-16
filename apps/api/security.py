"""Token authentication for the HTTP surface.

The dashboard and the JSON API sit behind one shared secret, ``AEGIS_API_TOKEN``:

* ``/health`` and ``/metrics`` stay open — a container healthcheck and Prometheus
  must be able to reach them without credentials;
* everything else, including the HTMX partials and the UI mutation routes, needs
  the token. There is no read/write split: a dashboard that can convert a signal
  must not be reachable by anyone who can load a page;
* a browser may present it three ways: ``Authorization: Bearer <token>``, HTTP
  Basic (any username, the token as the password, which browsers prompt for), or
  the ``aegis_token`` cookie this middleware sets after a successful Basic
  request, so HTMX keeps working without re-prompting;
* an unset token means development mode: requests pass and the application logs
  a warning at startup. That is a deliberate convenience, never a default for a
  deployed stack.

Comparisons go through ``hmac.compare_digest`` so a wrong token cannot be found
one byte at a time.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

__all__ = [
    "COOKIE_NAME",
    "PUBLIC_PATHS",
    "ApiTokenGuard",
    "install_api_token_middleware",
    "is_public_path",
]

COOKIE_NAME = "aegis_token"
PUBLIC_PATHS = frozenset({"/health", "/metrics"})
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)
_CHALLENGE = 'Basic realm="AegisTrade", charset="UTF-8"'
_UNAUTHORISED = "missing or invalid API token"


def is_public_path(path: str) -> bool:
    """True for the two operational probes and the dashboard's static assets."""
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _matches(candidate: str, token: str) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8"), token.encode("utf-8"))


def _bearer(header: str) -> str | None:
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def _basic_password(header: str) -> str | None:
    """Password half of an HTTP Basic header (the username is ignored)."""
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "basic" or not credential.strip():
        return None
    try:
        decoded = base64.b64decode(credential.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    _, _, password = decoded.partition(":")
    return password or None


@dataclass(frozen=True)
class ApiTokenGuard:
    """Answers one question: may this request reach the application?"""

    token: str | None

    @property
    def enabled(self) -> bool:
        """True when a token is configured and therefore required."""
        return bool(self.token)

    def credential(self, request: Request) -> tuple[str | None, bool]:
        """Return the presented token and whether it arrived through HTTP Basic."""
        header = request.headers.get("authorization", "")
        if header:
            bearer = _bearer(header)
            if bearer is not None:
                return bearer, False
            basic = _basic_password(header)
            if basic is not None:
                return basic, True
            return None, False
        cookie = request.cookies.get(COOKIE_NAME)
        return (cookie, False) if cookie else (None, False)

    def allows(self, request: Request) -> tuple[bool, bool]:
        """``(authorised, came_from_basic)``; development mode authorises everything."""
        if not self.enabled or self.token is None:
            return True, False
        presented, from_basic = self.credential(request)
        if presented is None:
            return False, False
        return _matches(presented, self.token), from_basic


def _unauthorised(request: Request) -> Response:
    """401 with a Basic challenge, so a browser prompts instead of showing JSON."""
    if "text/html" in request.headers.get("accept", ""):
        response: Response = Response(
            content=(
                "<!doctype html><html><head><title>401 Unauthorized</title></head>"
                f"<body><h1>401 Unauthorized</h1><p>{_UNAUTHORISED}</p></body></html>"
            ),
            status_code=401,
            media_type="text/html",
        )
    else:
        response = JSONResponse(status_code=401, content={"detail": _UNAUTHORISED})
    response.headers["WWW-Authenticate"] = _CHALLENGE
    return response


def install_api_token_middleware(app: FastAPI, guard: ApiTokenGuard, logger: Any) -> None:
    """Register the guard on ``app``; public paths bypass it entirely."""

    @app.middleware("http")
    async def require_api_token(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if is_public_path(request.url.path):
            return await call_next(request)

        authorised, from_basic = guard.allows(request)
        if not authorised:
            logger.warning(
                "api_auth_rejected",
                path=request.url.path,
                method=request.method,
                client=None if request.client is None else request.client.host,
            )
            return _unauthorised(request)

        response = await call_next(request)
        if from_basic and guard.token is not None:
            # Remember a Basic login so HTMX requests and page reloads do not
            # depend on the browser replaying the header.
            response.set_cookie(
                COOKIE_NAME,
                guard.token,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                path="/",
            )
        return response
