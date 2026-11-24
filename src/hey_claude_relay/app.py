"""The HTTP app: routes, the error shape, and the last-resort crash handler."""

from __future__ import annotations

import logging
import os
import re
import time
from collections import deque

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .access import HEALTH_PATH, AccessMiddleware, AccessPolicy, AccessVerifier
from .claude import AnthropicClaude, ClaudeClient
from .config import RelayConfig, Secrets
from .conversations import Clock, ConversationStore, Stored, TurnLedger
from .errors import BAD_REQUEST, INTERNAL, NOT_UNDERSTOOD, RATE_LIMITED, RelayError, error_body, error_response
from .turn import TurnRunner

log = logging.getLogger(__name__)

_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_NOT_A_NAME_CHAR = re.compile(r"[^a-z0-9_-]")
_NOT_AN_ID_CHAR = re.compile(r"[^A-Za-z0-9_-]")
MAX_BODY_BYTES = 64 * 1024  # a turn is a sentence or two; anything near this size is not a turn
TURN_ID_TTL_SECONDS = 600  # the contract: a repeated turn_id gets the stored response for 10 minutes


class TurnRequest(BaseModel):
    """The body of POST /v1/turn. Unknown fields are ignored, so a newer client still works."""

    model_config = ConfigDict(extra="ignore")

    turn_id: StrictStr = Field(min_length=1, max_length=128)
    text: StrictStr
    conversation_id: StrictStr | None = Field(default=None, max_length=128)
    client: StrictStr | None = None
    # Strict: "yes" or 1 must not be read as false and send a passphrase on to Claude.
    secret: StrictBool = False


class TurnLimiter:
    """At most ``per_minute`` turns in any sixty seconds. A guard on the API bill, not on abuse."""

    def __init__(self, per_minute: int, clock: Clock) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._starts: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._starts and now - self._starts[0] >= 60:
            self._starts.popleft()
        if len(self._starts) >= self.per_minute:
            return False
        self._starts.append(now)
        return True


async def _read_body(request: Request) -> bytes | None:
    """The request body, or None when it is larger than a turn could be."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return body


async def _turn(request: Request) -> Response:
    state = request.app.state
    cfg: RelayConfig = state.cfg
    raw = await _read_body(request)
    try:
        if raw is None:
            raise ValueError("body too large")
        turn = TurnRequest.model_validate_json(raw)
        text = turn.text.strip()
        if not turn.turn_id.strip() or not text or len(text) > cfg.max_text_chars:
            raise ValueError("turn_id or text out of bounds")
    except (ValidationError, ValueError) as exc:
        # The kind of problem only: the body may hold what the driver said, or a passphrase.
        log.info("turn rejected: bad request (%s)", type(exc).__name__)
        return error_response("bad_request", BAD_REQUEST)
    client = _NOT_A_NAME_CHAR.sub("", (turn.client or "").lower())[:32] or "-"

    async def run() -> Stored:
        started = time.monotonic()

        def line(conversation: str, outcome: str, rounds: int = 0, tools: tuple[str, ...] = ()) -> None:
            log.info(
                "turn conversation=%s client=%s outcome=%s rounds=%d tools=%s duration_ms=%d",
                _NOT_AN_ID_CHAR.sub("", conversation)[:8] or "-",  # a caller's own id may hold anything
                client,
                outcome,
                rounds,
                ",".join(tools) or "-",
                (time.monotonic() - started) * 1000,
            )

        if not state.limiter.allow():
            line(turn.conversation_id or "", "error:rate_limited")
            return Stored(429, error_body("rate_limited", RATE_LIMITED), keep=False)
        if not turn.secret:
            log.debug("heard: %s", text)
        try:
            result = await state.runner.run(text, turn.conversation_id or None, secret=turn.secret)
        except RelayError as exc:
            line(turn.conversation_id or "", f"error:{exc.code}")
            return Stored(exc.status, error_body(exc.code, exc.spoken), keep=exc.store)
        line(result.conversation_id, result.outcome, result.rounds, result.tools_used)
        log.debug("reply: %s", result.reply)
        return Stored(
            200,
            {
                "conversation_id": result.conversation_id,
                "reply": result.reply,
                "expect_secret": result.expect_secret,
                "tools_used": list(result.tools_used),
            },
        )

    stored: Stored = await state.ledger.run_once(turn.turn_id, run)
    return JSONResponse(stored.body, status_code=stored.status)


async def _healthz(request: Request) -> JSONResponse:
    body = {"status": "ok", "version": __version__}
    commit = os.environ.get("RELAY_COMMIT", "")
    if _COMMIT_RE.fullmatch(commit):  # set at image build; anything else is left out, never echoed
        body["commit"] = commit
    return JSONResponse(body)


async def _end_conversation(request: Request) -> Response:
    request.app.state.conversations.end(request.path_params["conversation_id"])
    return Response(status_code=204)


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


def create_app(
    cfg: RelayConfig,
    secrets: Secrets,
    *,
    clock: Clock = time.monotonic,
    verifier: AccessVerifier | None = None,
    insecure_no_auth: bool = False,
    claude: ClaudeClient | None = None,
) -> Starlette:
    """Build the app. Later arguments are keyword-only so tests can swap in fakes one at a time.

    With an ``[access]`` table every request but ``GET /healthz`` needs a verified assertion
    (``verifier`` is built from the config unless a test passes its own). Without one the app
    serves this machine only, unless ``insecure_no_auth`` says otherwise. ``claude`` is the
    Anthropic API unless a test passes a fake.
    """
    if verifier is None and cfg.access is not None:
        verifier = AccessVerifier(AccessPolicy.from_config(cfg.access))
    if claude is None:
        claude = AnthropicClaude.from_secrets(cfg, secrets)
    conversations = ConversationStore(idle_seconds=cfg.idle_minutes * 60, max_turns=cfg.max_turns, clock=clock)
    app = Starlette(
        routes=[
            Route(HEALTH_PATH, _healthz, methods=["GET"]),
            Route("/v1/turn", _turn, methods=["POST"]),
            Route("/v1/conversations/{conversation_id}/end", _end_conversation, methods=["POST"]),
        ],
        exception_handlers={HTTPException: _http_error, RelayError: _relay_error},
    )
    app.state.cfg = cfg
    app.state.clock = clock
    app.state.conversations = conversations
    app.state.ledger = TurnLedger(ttl_seconds=TURN_ID_TTL_SECONDS, clock=clock)
    app.state.limiter = TurnLimiter(cfg.turns_per_minute, clock)
    app.state.runner = TurnRunner(cfg, claude, conversations, clock=clock)
    # Added last, runs first: CrashGuard wraps the auth check, which wraps the routes.
    app.add_middleware(AccessMiddleware, verifier=verifier, local_only=verifier is None and not insecure_no_auth)
    app.add_middleware(CrashGuard)
    return app
