"""A scripted stand-in for Claude, and builders for the messages it answers with."""

import copy
import inspect
from collections.abc import Callable
from typing import Any

from anthropic.types import Message

_BASE = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
# What the model sends ahead of a tool call: a thinking block with nothing readable in it. It has
# to come back unchanged on the next request, so every scripted tool call carries one.
THINKING = {"type": "thinking", "thinking": "", "signature": "sig-test"}


def message(content: list[dict[str, Any]], stop_reason: str) -> Message:
    return Message.model_validate({**_BASE, "content": content, "stop_reason": stop_reason})


def say(text: str) -> Message:
    """A final answer."""
    return message([{"type": "text", "text": text}] if text else [], "end_turn")


def use(*calls: tuple[str, dict[str, Any]], text: str = "") -> Message:
    """The model asking for tools: ids are toolu_1, toolu_2, ... in the order given."""
    content: list[dict[str, Any]] = [dict(THINKING)]
    if text:
        content.append({"type": "text", "text": text})
    for n, (name, arguments) in enumerate(calls, start=1):
        content.append({"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": arguments})
    return message(content, "tool_use")


def refuse() -> Message:
    """The API's safety classifiers declining the request."""
    return message([], "refusal")


def _plain(value: Any) -> Any:
    """A deep copy made of dicts, lists and scalars, so a later change to the original cannot alter it."""
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return copy.deepcopy(value)


Step = Message | Exception | Callable[[dict[str, Any]], Any]


class FakeClaude:
    """Answers each create() call with the next step of its script and records what it was sent."""

    def __init__(self, *script: Step) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def create(self, *, system, tools, messages, allow_tools, timeout) -> Message:
        call = _plain(
            {"system": system, "tools": tools, "messages": messages, "allow_tools": allow_tools, "timeout": timeout}
        )
        self.calls.append(call)
        if not self._script:
            raise AssertionError(f"FakeClaude was called {len(self.calls)} times; its script is shorter than that")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            result = step(call)
            return await result if inspect.isawaitable(result) else result
        return step

    def sent(self) -> str:
        """Everything this fake was ever sent, as one string to search."""
        import json

        return json.dumps(self.calls)
