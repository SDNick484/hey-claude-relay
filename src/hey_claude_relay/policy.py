"""The relay's own say over every tool call, in code that nothing Claude reads can argue with.

The servers enforce what an automated caller may do. This is the second layer, for what the
servers cannot know: that the caller is a voice in a car. Every tool call Claude asks for is
put to ``Policy.decide`` before anything is sent, and comes back as one of three things: allow
it, refuse it, or hold it until a gate's checks pass.

Rules are about a tool, and for tools broad enough to "call any service", about the service.
Which tool carries a service, and where in its arguments, is config (``service_from``): there
is no knowledge of any one server here.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .config import GateConfig, ServerConfig, template_fields

DENIED = "Refused by the relay: {what} is not allowed by voice. Tell the driver plainly and do not try another way."
UNREADABLE = "Refused by the relay: it could not tell which service this call uses, so nothing was done."
EXTRA_ARGUMENT = (
    "Refused by the relay: this call carries an argument ({name}) that the relay's rules do not cover, "
    "so nothing was done."
)
UNKNOWN_SERVER = "Refused by the relay: it has no rules for the server this tool belongs to, so nothing was done."


@dataclass(frozen=True)
class ToolCall:
    server: str
    tool: str  # the server's own name for it
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Allow:
    """Send the call."""


@dataclass(frozen=True)
class Refuse:
    """Do not send it. ``message`` is the tool result Claude gets instead."""

    message: str


@dataclass(frozen=True)
class Hold:
    """Do not send it yet: ``gate`` names the checks that must pass first."""

    gate: GateConfig


Decision = Allow | Refuse | Hold


def _matches(value: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern.strip().casefold()) for pattern in patterns)


class Policy:
    def __init__(self, servers: Sequence[ServerConfig], gates: Sequence[GateConfig]) -> None:
        self._servers = {server.name: server for server in servers}
        self._gates = tuple(gates)

    def service_of(self, call: ToolCall) -> str | None:
        """The service a call uses, lower-cased, or None when it has none or it cannot be read.

        Only the text of top-level string arguments is used. Letter case and surrounding spaces
        are folded away first: a server may do the same before acting, and a rule must not be
        a stricter reader than the thing it guards.
        """
        server = self._servers.get(call.server)
        template = server.service_from.get(call.tool) if server is not None else None
        if template is None:
            return None
        parts: dict[str, str] = {}
        for name in template_fields(template) or []:
            value = call.arguments.get(name)
            if not isinstance(value, str) or not value.strip():
                return None
            parts[name] = value.strip().casefold()
        return template.format_map(parts) if parts else None

    def decide(self, call: ToolCall) -> Decision:
        server = self._servers.get(call.server)
        if server is None:
            return Refuse(UNKNOWN_SERVER)
        service = self.service_of(call)
        if call.tool in server.service_from:
            if service is None:
                # A raw escape hatch, a missing field, a value that is not text: whatever it is,
                # the rules below could not be applied to it.
                return Refuse(UNREADABLE)
            covered = {
                *(template_fields(server.service_from[call.tool]) or []),
                *server.service_args.get(call.tool, ()),
            }
            for name, value in call.arguments.items():
                if value is not None and name not in covered:
                    return Refuse(EXTRA_ARGUMENT.format(name=str(name)[:40]))
            if _matches(service, server.deny_services):
                return Refuse(DENIED.format(what=service))
            if server.allow_services and not _matches(service, server.allow_services):
                return Refuse(DENIED.format(what=service))
        for gate in self._gates:
            if gate.server != call.server or not fnmatch.fnmatchcase(call.tool, gate.match.tool):
                continue
            if gate.match.service is None or (service is not None and _matches(service, [gate.match.service])):
                return Hold(gate)
        return Allow()
