"""The one error shape the API has: ``{"error": {"code": ..., "spoken": ...}}``.

Every failure carries a line the client can read aloud, because a voice turn must never end in
silence. The codes are part of the contract shared with clients; the HTTP status that goes with
each is not, so clients key on ``code``.
"""

from __future__ import annotations

from typing import Literal

from starlette.responses import JSONResponse

ErrorCode = Literal["unauthorized", "bad_request", "upstream_timeout", "upstream_error", "rate_limited", "internal"]

STATUS: dict[ErrorCode, int] = {
    "unauthorized": 403,
    "bad_request": 400,
    "upstream_timeout": 504,
    "upstream_error": 502,
    "rate_limited": 429,
    "internal": 500,
}

# What is read aloud when nothing more specific is known.
UNAUTHORIZED = "This device isn't allowed to use the relay."
BAD_REQUEST = "I didn't catch that."
NOT_UNDERSTOOD = "I didn't understand that request."
UPSTREAM_TIMEOUT = "Claude took too long to answer. Try again in a moment."
UPSTREAM_ERROR = "I couldn't reach Claude just now."
RATE_LIMITED = "I'm getting too many requests. Give me a minute."
INTERNAL = "Something went wrong on my end."

# Replies that are not errors: the turn was answered, just not by the model.
REFUSAL = "I can't help with that one."
EMPTY = "I don't have an answer for that."
LOST = "I lost track of that request, so nothing was done. Ask again if you still want it."
OUT_OF_TIME = "I ran out of time before I could finish."

# Spoken around a gated action. ASK is the one reply that comes with expect_secret: true.
ASK = "Say your passphrase."
MISS = "That didn't match, so I left it alone."
LOCKED_NOW = "That didn't match. Protected actions are locked for {minutes:g} minutes."
TOO_LATE = "That took too long, so I left it alone. Ask again if you still want it."
CANCELLED = "Okay, I left it alone."


class RelayError(Exception):
    """A failure with a code for the client and a line for the driver.

    ``store`` says whether the answer may be kept for ``turn_id`` replay: False when nothing was
    done, so a retry should run the turn again.
    """

    def __init__(self, code: ErrorCode, spoken: str, *, store: bool = True) -> None:
        super().__init__(f"{code}: {spoken}")
        self.code: ErrorCode = code
        self.spoken = spoken
        self.status = STATUS[code]
        self.store = store


def error_body(code: ErrorCode, spoken: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "spoken": spoken}}


def error_response(code: ErrorCode, spoken: str, *, status: int | None = None) -> JSONResponse:
    """The error as an HTTP response. ``status`` overrides the code's usual one (404, 405)."""
    return JSONResponse(error_body(code, spoken), status_code=STATUS[code] if status is None else status)
