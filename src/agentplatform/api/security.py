"""HTTP hardening (plan F2-FORM-02): security headers on every response, CSRF token for the public lead form and a
per-IP limiter for public pages. The limiter is anti-abuse only (per process, not business state)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from agentplatform.api.auth import COOKIE, read_session, session_alive
from agentplatform.core.outbox.ratelimit import LocalRateLimiter
from agentplatform.settings import get_settings

log = logging.getLogger(__name__)

# The lead form has no scripts at all; the console loads htmx from the allowed CDN.
CSP_FORM = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")
CSP_APP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
           "style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.cdninstagram.com https://*.fbcdn.net; "
           "form-action 'self' https://www.facebook.com; "
           "frame-ancestors 'none'; base-uri 'none'")


class ConsoleSessionMiddleware(BaseHTTPMiddleware):
    """A deactivated account, or one whose role changed, stops working: its cookie goes and it is sent to the login."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/console/") and not path.startswith(("/console/login", "/console/logout", "/console/invite/")):
            user = read_session(request.cookies.get(COOKIE))
            if user is not None and not await session_alive(user):
                response = RedirectResponse("/console/login", status_code=303)
                response.delete_cookie(COOKIE)
                return response
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if path.startswith("/f/"):
            h["Content-Security-Policy"] = CSP_FORM
        elif "text/html" in h.get("content-type", ""):
            h.setdefault("Content-Security-Policy", CSP_APP)
        if path.startswith(("/f/", "/console")):
            h["Cache-Control"] = "no-store"
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


class RequestSpanMiddleware(BaseHTTPMiddleware):
    """One span per request, named by the route template (never the raw path: it may carry tokens)."""
    async def dispatch(self, request: Request, call_next):
        from agentplatform.core.ops.tracing import span
        with span("http.request", method=request.method) as sp:
            response = await call_next(request)
            route = request.scope.get("route")
            sp.update_name(f"{request.method} {getattr(route, 'path', 'unmatched')}")
            sp.set_attribute("http.status_code", response.status_code)
            return response


_ephemeral = secrets.token_bytes(32)


def _form_secret() -> bytes:
    s = get_settings()
    if s.console_session_secret:
        return b"form-csrf:" + s.console_session_secret.get_secret_value().encode()
    return _ephemeral      # dev without a configured secret: valid only within this process


def form_csrf_token(form_token: str) -> str:
    return hmac.new(_form_secret(), form_token.encode(), hashlib.sha256).hexdigest()


def check_form_csrf(form_token: str, submitted: str | None) -> bool:
    return bool(submitted) and hmac.compare_digest(form_csrf_token(form_token), submitted)


class PublicLimiter:
    def __init__(self) -> None:
        self._rl = LocalRateLimiter()

    async def allow(self, request: Request, bucket: str = "web_form") -> bool:
        ip = request.client.host if request.client else "unknown"
        return await self._rl.try_acquire(ip, bucket) == 0.0
