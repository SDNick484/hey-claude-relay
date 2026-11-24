"""Stub MCP servers, served in memory. No sockets, no network.

Two kinds: one built on the MCP SDK's own server (what the sibling servers are), and one
hand-written stateless JSON-RPC endpoint that only knows the older handshake (what a small
home-grown server looks like). Both record the calls and the headers they receive.
"""

import functools
import inspect
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

LOGIN_PAGE = "https://example.cloudflareaccess.com/cdn-cgi/access/login/mcp.example.com"


@dataclass
class Stub:
    name: str
    app: Any = None  # the ASGI app the relay's HTTP client is wired to
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    fail: bool = False  # True: answer every request the way Access answers a caller it does not admit
    hang: bool = False  # True: accept every request and never answer


class _Front:
    """Records request headers, and stands in for Access turning a caller away."""

    def __init__(self, app: Any, stub: Stub) -> None:
        self.app, self.stub = app, stub

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            self.stub.headers.append({k.decode().lower(): v.decode() for k, v in scope["headers"]})
            if self.stub.hang:
                await anyio.sleep(3600)
            if self.stub.fail:
                await Response(status_code=302, headers={"location": LOGIN_PAGE})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _recording(stub: Stub, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            stub.calls.append((name, kwargs))
            return await fn(*args, **kwargs)

    else:

        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            stub.calls.append((name, kwargs))
            return fn(*args, **kwargs)

    return run


@asynccontextmanager
async def sdk_stub(name: str, tools: Mapping[str, Callable[..., Any]]) -> AsyncIterator[Stub]:
    """A server built with the MCP SDK, behind its Streamable HTTP app. Its URL needs a port:
    the SDK's Host check turns away a bare 127.0.0.1."""
    stub = Stub(name=name)
    server = MCPServer(name)
    for tool_name, fn in tools.items():
        server.tool(name=tool_name, description=fn.__doc__ or f"The {tool_name} tool.")(_recording(stub, tool_name, fn))
    app = server.streamable_http_app(streamable_http_path="/mcp")
    async with app.router.lifespan_context(app):
        stub.app = _Front(app, stub)
        yield stub


LEGACY_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
GARBLED = object()  # a legacy stub tool returns this to answer with a body no client can read


def legacy_stub(
    name: str, tools: Mapping[str, Callable[[dict[str, Any]], Any]], *, listing: list[dict[str, Any]] | None = None
) -> Stub:
    """A hand-written stateless JSON-RPC endpoint: older protocol, POST only, no session.

    ``listing`` replaces what tools/list answers, for a server whose tool list is odd.
    """
    stub = Stub(name=name)

    def answer(mid: Any, result: Any) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": result})

    async def endpoint(request: Request) -> Response:
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        message = await request.json()
        mid, method, params = message.get("id"), message["method"], message.get("params") or {}
        if mid is None:  # a notification
            return Response(status_code=202)
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return answer(
                mid,
                {
                    "protocolVersion": wanted if wanted in LEGACY_VERSIONS else LEGACY_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": name, "version": "1"},
                },
            )
        if method == "tools/list":
            schema = {"type": "object", "properties": {"query": {"type": "string"}}}
            listed = [{"name": n, "description": f"The {n} tool.", "inputSchema": schema} for n in tools]
            return answer(mid, {"tools": listing if listing is not None else listed})
        if method == "tools/call":
            tool, arguments = params.get("name"), params.get("arguments") or {}
            if tool not in tools:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {tool}"}}
                )
            stub.calls.append((tool, arguments))
            result = tools[tool](arguments)
            if result is GARBLED:  # the tool ran, and then the answer was lost on the way back
                body = b'{"jsonrpc": "2.0", "id": ' + json.dumps(mid).encode() + b', "result": {"content": 7}}'
                return Response(body, media_type="application/json")
            return answer(mid, {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result})
        return JSONResponse(
            {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        )

    stub.app = _Front(Starlette(routes=[Route("/mcp", endpoint, methods=["GET", "POST", "DELETE"])]), stub)
    return stub


def _no_route(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("no stub is standing in for this server")


def transport_for(*stubs: Stub) -> Callable[[Any], httpx2.AsyncBaseTransport]:
    """Wire each configured server to the stub of the same name; any other server is unreachable."""
    by_name = {stub.name: stub for stub in stubs}

    def pick(server: Any) -> httpx2.AsyncBaseTransport:
        stub = by_name.get(server.name)
        return httpx2.ASGITransport(app=stub.app) if stub is not None else httpx2.MockTransport(_no_route)

    return pick
