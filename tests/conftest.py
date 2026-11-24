"""Shared test helpers. No test here needs a real server, a real API key, or anything beyond loopback."""

import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette

from fakes import FakeClaude
from hey_claude_relay.app import create_app
from hey_claude_relay.config import RelayConfig, Secrets, load_config

# Placeholder values for every environment variable relay.example.toml needs. It grows with the example.
EXAMPLE_ENV: dict[str, str] = {"ANTHROPIC_API_KEY": "placeholder-not-a-real-key"}
# The environment every test relay starts from; a test's own env= is laid over it.
TEST_ENV: dict[str, str] = {"ANTHROPIC_API_KEY": "test-key-not-real"}


@pytest.fixture
def anyio_backend():
    """Async tests run on asyncio only (pyproject sets anyio_mode = "auto")."""
    return "asyncio"


def client_for(app) -> httpx2.AsyncClient:
    """An HTTP client wired straight to the ASGI app: no sockets, and a loopback Host header."""
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8787")


@pytest.fixture
def cfg():
    return RelayConfig()


@pytest.fixture
def secrets():
    return Secrets(anthropic_api_key="test-key-not-real")


@pytest.fixture
def app(cfg, secrets):
    return create_app(cfg, secrets)


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@dataclass
class Relay:
    """A relay under test: the app, its fake Claude, its clock, and a client that talks to it."""

    http: httpx2.AsyncClient
    claude: FakeClaude
    clock: FakeClock
    app: Starlette
    dir: Path
    stubs: dict[str, Any] = field(default_factory=dict)

    async def turn(
        self, text: str, *, conversation_id: str | None = None, turn_id: str | None = None, secret: bool = False
    ) -> httpx2.Response:
        body: dict[str, Any] = {"turn_id": turn_id or str(uuid.uuid4()), "text": text, "client": "test"}
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        if secret:
            body["secret"] = True
        return await self.http.post("/v1/turn", json=body)


@asynccontextmanager
async def make_relay(
    toml: str = "", *script: Any, env: Mapping[str, str] | None = None, files: Mapping[str, str] | None = None
) -> AsyncIterator[Relay]:
    """A relay built from ``toml`` (a whole relay.toml) with a scripted Claude. No [access]: loopback only.

    ``files`` are written beside relay.toml first, for a test that names its own prompt file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name, content in (files or {}).items():
            (folder / name).write_text(content)
        (folder / "relay.toml").write_text(toml)
        cfg, secrets = load_config(folder / "relay.toml", {**TEST_ENV, **(env or {})})
        claude, clock = FakeClaude(*script), FakeClock()
        app = create_app(cfg, secrets, claude=claude, clock=clock)
        async with client_for(app) as http:
            yield Relay(http=http, claude=claude, clock=clock, app=app, dir=folder)
