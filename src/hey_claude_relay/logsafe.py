"""Keep secrets out of the log.

The relay's own code never logs a secret. This filter is the second line: it masks every known
secret value in any record that reaches the handler, whoever logged it, traceback included.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable

from .config import Secrets

MASK = "[redacted]"
MIN_LENGTH = 6  # shorter values would mask ordinary words
# Libraries that can log request details at DEBUG. Held at WARNING whatever RELAY_LOG_LEVEL says,
# so the text of a turn cannot reach the log through them.
QUIET_LOGGERS = ("anthropic", "mcp", "httpx2", "httpcore2")


class RedactSecrets(logging.Filter):
    """Replaces each listed value with ``[redacted]`` in the message and in any traceback."""

    def __init__(self, values: Iterable[str]) -> None:
        super().__init__()
        self._values = sorted({v for v in values if len(v) >= MIN_LENGTH}, key=len, reverse=True)

    def _mask(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._values:
            return True
        try:
            message = record.getMessage()
        except Exception:  # a malformed log call somewhere else; logging would only have complained
            message = str(record.msg)
        record.msg, record.args = self._mask(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._mask(record.exc_text)
        return True


def secret_values(secrets: Secrets) -> list[str]:
    """Every secret value that is set, for the filter."""
    named = (secrets.anthropic_api_key, secrets.cf_client_id, secrets.cf_client_secret, secrets.passphrase_hash)
    return [v for v in (*named, *secrets.bearer.values()) if v]


def configure_logging(level: str, secrets: Secrets) -> None:
    """Log to stderr. ``level`` (RELAY_LOG_LEVEL) applies to the relay's own logger only."""
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, force=True, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    redact = RedactSecrets(secret_values(secrets))
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)
    chosen = logging.getLevelNamesMapping().get(level.strip().upper(), logging.INFO)
    logging.getLogger("hey_claude_relay").setLevel(chosen)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
