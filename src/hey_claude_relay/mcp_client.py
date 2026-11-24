"""The relay as an MCP client: which tools exist, and calling them.

Each configured server is reached over Streamable HTTP with the credentials its ``auth`` mode
names. The Messages API's own MCP connector cannot be used here: it sends a bearer token only,
and a Cloudflare Access service token is a pair of headers.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .config import Secrets, ServerConfig
from .conversations import Clock

log = logging.getLogger(__name__)

# What the Messages API accepts as a tool name.
_CLAUDE_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
FAILED_TTL_SECONDS = 30  # how soon a server that could not be listed is tried again
LARGE_CATALOG = 60  # more tools than this is worth a warning: every one is sent on every turn

# Tests wire a server to an in-memory stub through this; production passes None (the network).
TransportFor = Callable[[ServerConfig], httpx2.AsyncBaseTransport | None]


def auth_headers(server: ServerConfig, secrets: Secrets) -> dict[str, str]:
    """The credentials a server's ``auth`` mode asks for, as request headers."""
    if server.auth == "cf-service-token":
        return {
            "CF-Access-Client-Id": secrets.cf_client_id or "",
            "CF-Access-Client-Secret": secrets.cf_client_secret or "",
        }
    if server.auth == "bearer" and server.token_env is not None:
        return {"Authorization": f"Bearer {secrets.bearer[server.token_env]}"}
    return {}


@dataclass(frozen=True)
class ToolSpec:
    """One tool on one server."""

    server: str
    name: str  # the server's own name for it
    claude_name: str  # what Claude sees: "<server>__<name>"
    description: str
    input_schema: dict[str, Any]

    @property
    def label(self) -> str:
        """The form used in ``tools_used`` and in logs: "<server>.<name>"."""
        return f"{self.server}.{self.name}"


@dataclass(frozen=True)
class Catalog:
    """Every tool the relay may offer right now, and the servers that could not be asked."""

    specs: tuple[ToolSpec, ...] = ()
    unavailable: tuple[str, ...] = ()

    def tools(self) -> list[dict[str, Any]]:
        """The tool list in the shape the Messages API takes, in a stable order."""
        listed = []
        for spec in self.specs:
            tool: dict[str, Any] = {"name": spec.claude_name, "input_schema": spec.input_schema}
            if spec.description:
                tool["description"] = spec.description
            listed.append(tool)
        return listed

    def find(self, claude_name: str) -> ToolSpec | None:
        return next((spec for spec in self.specs if spec.claude_name == claude_name), None)


class McpServers:
    def __init__(
        self,
        servers: Sequence[ServerConfig],
        secrets: Secrets,
        *,
        ttl_seconds: float,
        clock: Clock = time.monotonic,
        transport_for: TransportFor | None = None,
        max_description_chars: int = 2000,
    ) -> None:
        self._servers = tuple(servers)
        self._secrets = secrets
        self._ttl = ttl_seconds
        self._clock = clock
        self._transport_for = transport_for
        self._max_description = max_description_chars
        # name -> (fresh until, the server's tools, or None when it could not be reached)
        self._listed: dict[str, tuple[float, tuple[ToolSpec, ...] | None]] = {}
        self._lock = anyio.Lock()

    @asynccontextmanager
    async def connect(self, server: ServerConfig) -> AsyncIterator[Client]:
        """A connected MCP client for ``server``. Enter and leave it in the same task."""
        http = httpx2.AsyncClient(
            headers=auth_headers(server, self._secrets),
            transport=self._transport_for(server) if self._transport_for is not None else None,
            timeout=server.timeout_seconds,
            # Access answers a caller it does not admit with a redirect to its login page.
            # Following it would turn "not admitted" into a page of HTML.
            follow_redirects=False,
        )
        async with http, Client(streamable_http_client(server.url, http_client=http)) as client:
            yield client

    async def catalog(self) -> Catalog:
        """The current tools. Each server's list is asked for again once it is older than the TTL."""
        async with self._lock:
            now = self._clock()
            stale = [s for s in self._servers if s.name not in self._listed or self._listed[s.name][0] <= now]
            if stale:
                async with anyio.create_task_group() as group:
                    for server in stale:
                        group.start_soon(self._refresh, server)
            specs: list[ToolSpec] = []
            unavailable: list[str] = []
            for server in self._servers:
                listed = self._listed[server.name][1]
                if listed is None:
                    unavailable.append(server.name)
                else:
                    specs.extend(listed)
        if stale and len(specs) > LARGE_CATALOG:
            log.warning(
                "%d tools are offered to Claude on every turn. A `tools` allow-list on the larger servers "
                "would make turns quicker and cheaper.",
                len(specs),
            )
        return Catalog(tuple(specs), tuple(unavailable))

    async def _refresh(self, server: ServerConfig) -> None:
        try:
            with anyio.fail_after(server.timeout_seconds):
                async with self.connect(server) as client:
                    tools: list[Any] = []
                    cursor: str | None = None
                    while True:
                        page = await client.list_tools(cursor=cursor)
                        tools.extend(page.tools)
                        cursor = page.next_cursor
                        if not cursor:
                            break
        except Exception as exc:
            # The kind of failure only: its text can quote a URL or a response body.
            log.warning("MCP server %s could not be asked for its tools (%s)", server.name, type(exc).__name__)
            self._listed[server.name] = (self._clock() + FAILED_TTL_SECONDS, None)
            return
        self._listed[server.name] = (self._clock() + self._ttl, self._specs(server, tools))

    def _specs(self, server: ServerConfig, tools: Sequence[Any]) -> tuple[ToolSpec, ...]:
        specs = []
        for tool in sorted(tools, key=lambda t: str(t.name)):
            name = str(tool.name)
            if server.tools and not any(fnmatch.fnmatchcase(name, pattern) for pattern in server.tools):
                continue
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in server.deny):
                continue
            claude_name = f"{server.name}__{name}"
            if not _CLAUDE_NAME.fullmatch(claude_name):
                log.warning(
                    "Tool %s is left out: %r is not a name Claude can use", f"{server.name}.{name}", claude_name
                )
                continue
            description = (tool.description or "").strip()
            if len(description) > self._max_description:
                description = description[: self._max_description] + " ..."
            schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
            specs.append(ToolSpec(server.name, name, claude_name, description, schema or {"type": "object"}))
        return tuple(specs)
