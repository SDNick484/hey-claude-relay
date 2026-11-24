"""Shared test helpers. No test here needs a real server, a real API key, or anything beyond loopback."""

import httpx2
import pytest

from hey_claude_relay.app import create_app
from hey_claude_relay.config import RelayConfig, Secrets

# Placeholder values for every environment variable relay.example.toml needs. It grows with the example.
EXAMPLE_ENV: dict[str, str] = {}


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
    return Secrets()


@pytest.fixture
def app(cfg, secrets):
    return create_app(cfg, secrets)
