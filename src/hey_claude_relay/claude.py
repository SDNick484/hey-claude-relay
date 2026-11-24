"""The one place the relay talks to the Claude API.

The turn runner owns the tool loop and calls ``create`` once per round. This adapter builds the
request and turns every way the API can fail into a RelayError with a line to read aloud.

The request is deliberately plain: the stable ``messages.create``, adaptive thinking (the
default, so no ``thinking`` key), effort from config, and automatic prompt caching so the tool
list and the voice prompt are read from cache on every round after the first.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import anthropic
import anyio
from anthropic import AsyncAnthropic
from anthropic.types import Message

from .config import ConfigError, RelayConfig, Secrets
from .errors import RATE_LIMITED, UPSTREAM_ERROR, UPSTREAM_TIMEOUT, RelayError

log = logging.getLogger(__name__)


class ClaudeClient(Protocol):
    """What the turn runner needs from Claude. Tests pass a scripted fake."""

    async def create(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, Any]],
        messages: Sequence[Mapping[str, Any]],
        allow_tools: bool,
        timeout: float,
    ) -> Message: ...


def text_of(message: Message) -> str:
    """The words in a reply. Read by block type: a reply can begin with thinking blocks."""
    return " ".join(block.text for block in message.content if block.type == "text").strip()


class AnthropicClaude:
    """ClaudeClient over the Anthropic SDK."""

    def __init__(self, cfg: RelayConfig, client: AsyncAnthropic) -> None:
        self.cfg = cfg
        self.client = client

    @classmethod
    def from_secrets(cls, cfg: RelayConfig, secrets: Secrets) -> AnthropicClaude:
        if not secrets.anthropic_api_key:
            raise ConfigError("ANTHROPIC_API_KEY is not set; the relay needs it to call Claude.")
        # One retry on a 5xx or a 429 fits inside a turn's time limit. The SDK's default of two would not.
        return cls(cfg, AsyncAnthropic(api_key=secrets.anthropic_api_key, max_retries=1))

    async def create(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, Any]],
        messages: Sequence[Mapping[str, Any]],
        allow_tools: bool,
        timeout: float,
    ) -> Message:
        request: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "system": system,
            "messages": list(messages),
            "cache_control": {"type": "ephemeral"},
        }
        if self.cfg.effort not in (None, "none"):
            request["output_config"] = {"effort": self.cfg.effort}
        if tools:
            request["tools"] = list(tools)
            if not allow_tools:
                request["tool_choice"] = {"type": "none"}
        try:
            # The SDK enforces the timeout on the connection; fail_after enforces it on the call as
            # a whole. A deadline in the caller cancels from outside and is not caught here.
            with anyio.fail_after(timeout):
                reply = await self.client.with_options(timeout=timeout).messages.create(**request)
        except (TimeoutError, anthropic.APITimeoutError) as exc:
            log.warning("Claude did not answer within %.1f s", timeout)
            raise RelayError("upstream_timeout", UPSTREAM_TIMEOUT) from exc
        except anthropic.RateLimitError as exc:
            self._log(exc)
            raise RelayError("rate_limited", RATE_LIMITED) from exc
        except anthropic.APIStatusError as exc:
            self._log(exc)
            raise RelayError("upstream_error", UPSTREAM_ERROR) from exc
        except anthropic.APIConnectionError as exc:
            log.warning("Claude API could not be reached: %s", type(exc).__name__)
            raise RelayError("upstream_error", UPSTREAM_ERROR) from exc
        except anthropic.APIError as exc:
            # Anything else the SDK raises, such as an answer it could not read.
            log.warning("Claude API error: %s", type(exc).__name__)
            raise RelayError("upstream_error", UPSTREAM_ERROR) from exc
        if not isinstance(reply, Message):
            # Something answered that was not the API (a proxy's error page, say): the SDK hands
            # back the raw text instead of raising.
            log.warning("Claude API answered with something that is not a message (%s)", type(reply).__name__)
            raise RelayError("upstream_error", UPSTREAM_ERROR)
        return reply

    @staticmethod
    def _log(exc: anthropic.APIStatusError) -> None:
        # The status and the request id are enough to look a failure up. The message body is left
        # out: it can quote parts of the request.
        log.warning("Claude API error: %s status=%s request_id=%s", type(exc).__name__, exc.status_code, exc.request_id)
