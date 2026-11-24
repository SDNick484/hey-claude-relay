"""The HTTP app: routes, the error shape, and the last-resort crash handler."""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .config import RelayConfig, Secrets
from .errors import INTERNAL, NOT_UNDERSTOOD, RelayError, error_response

log = logging.getLogger(__name__)

Clock = Callable[[], float]
HEALTH_PATH = "/healthz"
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


async def _healthz(request: Request) -> JSONResponse:
    body = {"status": "ok", "version": __version__}
    commit = os.environ.get("RELAY_COMMIT", "")
    if _COMMIT_RE.fullmatch(commit):  # set at image build; anything else is left out, never echoed
        body["commit"] = commit
    return JSONResponse(body)


async def _http_error(request: Request, exc: Exception) -> Response:
    """Starlette's own 404 and 405, in the relay's error shape."""
    status = exc.status_code if isinstance(exc, HTTPException) else 400
    return error_response("bad_request", NOT_UNDERSTOOD, status=status)


async def _relay_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RelayError)
    return error_response(exc.code, exc.spoken)


class CrashGuard:
    """Outermost wrapper: an exception nothing else handled becomes a spoken 500.

    The detail goes to the log, never to the caller.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def watching_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, watching_send)
        except Exception:
            log.exception("Unhandled error in %s %s", scope.get("method"), scope.get("path"))
            if not started:
                await error_response("internal", INTERNAL)(scope, receive, send)


def create_app(cfg: RelayConfig, secrets: Secrets, *, clock: Clock = time.monotonic) -> Starlette:
    """Build the app. Later arguments are keyword-only so tests can swap in fakes one at a time."""
    app = Starlette(
        routes=[Route(HEALTH_PATH, _healthz, methods=["GET"])],
        exception_handlers={HTTPException: _http_error, RelayError: _relay_error},
    )
    app.state.cfg = cfg
    app.state.clock = clock
    app.add_middleware(CrashGuard)
    return app
