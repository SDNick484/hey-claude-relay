"""What the relay remembers between requests, all of it in memory.

Conversations expire after a stretch of idleness and are capped in length. Answers are kept by
``turn_id`` for a few minutes so a retry gets the same answer and nothing runs twice. A restart
forgets both, which is acceptable.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio

if TYPE_CHECKING:
    from .mcp_client import Catalog

Clock = Callable[[], float]
_THINKING = ("thinking", "redacted_thinking")
_NOTHING_SAID = {"type": "text", "text": "(nothing said)"}


@dataclass
class Conversation:
    """One conversation with Claude, in the shape the Messages API takes.

    ``system`` and ``tools`` are fixed when the conversation starts and never change after:
    the API ties the model's thinking blocks to the prompt, the tool list and every earlier
    message, so history is append-only. ``lock`` serialises turns on the same conversation.
    """

    id: str
    system: str
    tools: list[dict[str, Any]]
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_used: float = 0.0
    lock: anyio.Lock = field(default_factory=anyio.Lock, repr=False)
    catalog: Catalog | None = None  # which server and tool each name in ``tools`` stands for
    pending: Any = None


def _starts_a_turn(message: dict[str, Any]) -> bool:
    """A user message that is not a batch of tool results: what the driver said."""
    if message["role"] != "user":
        return False
    content = message["content"]
    return isinstance(content, str) or not any(block.get("type") == "tool_result" for block in content)


class ConversationStore:
    def __init__(self, *, idle_seconds: float, max_turns: int, clock: Clock = time.monotonic) -> None:
        self.idle_seconds = idle_seconds
        self.max_turns = max_turns
        self._clock = clock
        self._conversations: dict[str, Conversation] = {}

    def __len__(self) -> int:
        return len(self._conversations)

    def _sweep(self) -> None:
        now = self._clock()
        for key in [k for k, c in self._conversations.items() if now - c.last_used > self.idle_seconds]:
            del self._conversations[key]

    def get(self, conversation_id: str | None) -> Conversation | None:
        """The conversation, if it exists and has not sat idle too long. Finding it counts as use."""
        self._sweep()
        conversation = self._conversations.get(conversation_id) if conversation_id else None
        if conversation is not None:
            conversation.last_used = self._clock()
        return conversation

    def start(self, system: str, tools: list[dict[str, Any]], catalog: Catalog | None = None) -> Conversation:
        self._sweep()
        conversation = Conversation(secrets.token_urlsafe(16), system, tools, last_used=self._clock(), catalog=catalog)
        self._conversations[conversation.id] = conversation
        return conversation

    def end(self, conversation_id: str) -> None:
        self._conversations.pop(conversation_id, None)

    def trim(self, conversation: Conversation) -> None:
        """Cap the length: past ``max_turns`` turns, keep the newest half.

        Whole turns go, so no tool call is left without its result. The thinking blocks in what
        remains go too: the API rejects them once the history before them has changed.
        """
        starts = [i for i, message in enumerate(conversation.messages) if _starts_a_turn(message)]
        if len(starts) <= self.max_turns:
            return
        kept = conversation.messages[starts[-(self.max_turns // 2)] :]
        for message in kept:
            if message["role"] == "assistant":
                spoken = [block for block in message["content"] if block.get("type") not in _THINKING]
                message["content"] = spoken or [dict(_NOTHING_SAID)]
        conversation.messages[:] = kept


@dataclass(frozen=True)
class Stored:
    """An answer to a turn: the HTTP status and body. ``keep`` False means do not replay it."""

    status: int
    body: dict[str, Any]
    keep: bool = True


@dataclass
class _Flight:
    """A turn that is running now, for a retry that arrives before it has finished."""

    landed: anyio.Event = field(default_factory=anyio.Event)
    result: Stored | None = None
    error: BaseException | None = None


class TurnLedger:
    """Runs each ``turn_id`` at most once per ``ttl_seconds`` and replays its answer to repeats."""

    def __init__(self, *, ttl_seconds: float = 600, clock: Clock = time.monotonic) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._done: dict[str, tuple[float, Stored]] = {}
        self._flying: dict[str, _Flight] = {}

    async def run_once(self, turn_id: str, fn: Callable[[], Awaitable[Stored]]) -> Stored:
        now = self._clock()
        for key in [k for k, (at, _) in self._done.items() if now - at > self.ttl_seconds]:
            del self._done[key]
        if turn_id in self._done:
            return self._done[turn_id][1]
        if (flight := self._flying.get(turn_id)) is not None:
            await flight.landed.wait()
            if flight.result is not None:
                return flight.result
            raise flight.error if flight.error is not None else RuntimeError("the turn was cancelled")
        flight = self._flying[turn_id] = _Flight()
        try:
            flight.result = await fn()
        except Exception as exc:
            flight.error = exc
            raise
        finally:
            del self._flying[turn_id]
            flight.landed.set()
        if flight.result.keep:
            self._done[turn_id] = (self._clock(), flight.result)
        return flight.result
