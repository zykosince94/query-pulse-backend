"""HTTP middleware: request IDs with a structured access log, and the Origin check.

The Origin check is the cross-site request defence alongside SameSite=Lax cookies. Browsers
send an Origin header on every state-changing request. One that names a different site is
refused before any route runs, so a page elsewhere can't make a signed-in user's browser
change anything here. Requests without an Origin (curl, scripts, the test client) aren't
browser cross-site requests and pass. Same origin means the Origin's host:port matches the
request's Host, which holds behind Vite's proxy and Caddy (both keep the Host header). Other
origins can be allowed explicitly with QP_ALLOWED_ORIGINS.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from query_pulse.log import new_request_id, request_id_var

logger = logging.getLogger("query_pulse.access")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
Next = Callable[[Request], Awaitable[Response]]


def origin_allowed(origin: str, host: str | None, allowed: set[str]) -> bool:
    if origin.rstrip("/").lower() in allowed:
        return True
    if origin == "null" or not host:  # sandboxed iframes, file:// pages, data: URLs
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and parts.netloc.lower() == host.lower()


def install(app: FastAPI, allowed_origins: set[str]) -> None:
    allowed = {o.rstrip("/").lower() for o in allowed_origins}

    # Registered first, so it runs *inside* the request-ID middleware: rejections get logged.
    @app.middleware("http")
    async def origin_check(request: Request, call_next: Next) -> Response:
        origin = request.headers.get("origin")
        if (
            request.method not in SAFE_METHODS
            and origin is not None
            and not origin_allowed(origin, request.headers.get("host"), allowed)
        ):
            logger.warning("rejected cross-site request", extra={"origin": origin[:200]})
            return JSONResponse(
                {"detail": "Cross-site request refused (Origin doesn't match this site)"},
                status_code=403,
            )
        return await call_next(request)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Next) -> Response:
        request_id = new_request_id(request.headers.get("x-request-id"))
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request failed", extra={"method": request.method, "path": request.url.path}
            )
            raise
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        # Health checks run every few seconds; logging them would drown everything else.
        if request.url.path not in ("/health", "/ready"):
            token = request_id_var.set(request_id)
            try:
                logger.info(
                    "%s %s %d",
                    request.method,
                    request.url.path,  # the path only: query strings stay out of the logs
                    response.status_code,
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
            finally:
                request_id_var.reset(token)
        return response
