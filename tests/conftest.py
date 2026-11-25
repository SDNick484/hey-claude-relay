"""Shared test helpers. No test here needs a real server, a real API key, or anything beyond loopback."""

import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette

from fakes import FakeClaude
from hey_claude_relay.access import AccessPolicy, AccessVerifier
from hey_claude_relay.app import create_app
from hey_claude_relay.config import RelayConfig, Secrets, load_config
from hey_claude_relay.gates import hash_passphrase
from hey_claude_relay.mcp_client import McpServers
from keys import AUD, CALLER, TEAM, mint, resolver
from stubs import Stub, house_stub, transport_for

# Placeholder values for every environment variable relay.example.toml needs. It grows with the example.
EXAMPLE_ENV: dict[str, str] = {
    "ANTHROPIC_API_KEY": "placeholder-not-a-real-key",
    "CF_ACCESS_CLIENT_ID": "placeholder.access",
    "CF_ACCESS_CLIENT_SECRET": "placeholder-not-a-real-secret",
    "COOKBOOKS_MCP_TOKEN": "placeholder-not-a-real-token",
    "RELAY_PASSPHRASE_HASH": hash_passphrase("a throwaway phrase for the example"),
}
# The environment every test relay starts from; a test's own env= is laid over it.
TEST_ENV: dict[str, str] = {
    "ANTHROPIC_API_KEY": "test-key-not-real",
    "CF_ACCESS_CLIENT_ID": "relay.access",
    "CF_ACCESS_CLIENT_SECRET": "s3cret",
    "COOKBOOKS_MCP_TOKEN": "tok-1",
}

# The passphrase the gate tests say, and its hash (made once: scrypt is deliberately slow).
PHRASE = "open sesame forty two"
PHRASE_HASH = hash_passphrase(PHRASE)

# Whole relay.toml texts. Each ends in its [[server]] table, so a test can append a key to it.
MATINEE = """
[[server]]
name = "matinee"
url = "http://127.0.0.1:9001/mcp"
auth = "cf-service-token"
"""
COOKBOOKS_BEARER = """
[[server]]
name = "cookbooks"
url = "http://127.0.0.1:9002/mcp"
auth = "bearer"
token_env = "COOKBOOKS_MCP_TOKEN"
"""
COOKBOOKS_NONE = """
[[server]]
name = "cookbooks"
url = "http://127.0.0.1:9002/mcp"
auth = "none"
"""
HOUSE = """
[[server]]
name = "house"
url = "http://127.0.0.1:9003/mcp"
auth = "none"
tools = ["ha_search", "ha_get_state", "ha_call_service"]
"""
# The house server with the relay's own rules on what its catch-all tool may do.
HOUSE_POLICY = (
    HOUSE
    + """service_from = { ha_call_service = "{domain}.{service}" }
service_args = { ha_call_service = ["entity_id", "data"] }
allow_services = ["light.*", "cover.open_cover", "cover.close_cover"]
deny_services = ["lock.unlock", "alarm_control_panel.alarm_disarm"]
"""
)
# ...and a gate: opening a cover needs the owner near home and the spoken passphrase.
GATED = (
    HOUSE_POLICY
    + """
[[gate]]
server = "house"
match = { tool = "ha_call_service", service = "cover.open_cover" }
require = ["near-home", "passphrase"]

[near_home]
server = "house"
tool = "ha_get_state"
arguments = { entity_id = "person.example", fields = ["state"] }
result_path = "data.state"
states = ["home"]
"""
)
TWO_SERVERS = (
    MATINEE
    + """
[[server]]
name = "house"
url = "http://127.0.0.1:9003/mcp"
auth = "none"
"""
)

# Rooms: the relay behind Access, two callers, four profiles, a matinee and the gated house.
SPEAKER = "speaker.access"  # the second caller's token, beside keys.CALLER (the phone's)
ROOMS = (
    f"""
[access]
team_domain = "{TEAM}"
audiences = ["{AUD}"]

[[caller]]
name = "phone"
token = "{CALLER}"
profiles = ["personal"]

[[caller]]
name = "speaker"
token = "{SPEAKER}"
profiles = ["office", "shared", "guest"]

[[profile]]
name = "personal"
gated = "allow"

[[profile]]
name = "office"
gated = "allow"

[[profile]]
name = "shared"
servers = ["house"]
gated = "refuse"
allow_services = ["light.*"]

[[profile]]
name = "guest"
gated = "refuse"

[[server]]
name = "matinee"
url = "http://127.0.0.1:9001/mcp"
auth = "none"
"""
    + GATED
)


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


def load_toml(toml: str, env: Mapping[str, str] | None = None, files: Mapping[str, str] | None = None):
    """(cfg, secrets) for a relay.toml given as text, with the test environment under ``env``."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name, content in (files or {}).items():
            (folder / name).write_text(content)
        (folder / "relay.toml").write_text(toml)
        return load_config(folder / "relay.toml", {**TEST_ENV, **(env or {})})


def servers(toml: str, *stubs: Stub, env: Mapping[str, str] | None = None, clock: Any = None) -> McpServers:
    """The relay's MCP side alone, wired to stubs."""
    cfg, secrets = load_toml(toml, env)
    return McpServers(
        cfg.server,
        secrets,
        ttl_seconds=cfg.tools_ttl_seconds,
        clock=clock or FakeClock(),
        transport_for=transport_for(*stubs),
        max_description_chars=cfg.max_description_chars,
        list_seconds=cfg.list_seconds,
    )


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
        self,
        text: str,
        *,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        secret: bool = False,
        profile: str | None = None,
        token: str | None = None,
    ) -> httpx2.Response:
        """One turn. ``profile`` is sent when given; ``token`` sends an Access assertion for that client ID."""
        body: dict[str, Any] = {"turn_id": turn_id or str(uuid.uuid4()), "text": text, "client": "test"}
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        if secret:
            body["secret"] = True
        if profile is not None:
            body["profile"] = profile
        return await self.http.post("/v1/turn", json=body, headers=as_caller(token))


def as_caller(token: str | None) -> dict[str, str]:
    """The header Access adds for a service token with this client ID, or none."""
    return {"Cf-Access-Jwt-Assertion": mint(common_name=token)} if token is not None else {}


def assert_valid_history(messages: list[dict[str, Any]]) -> None:
    """What the Messages API needs of a conversation, whatever happened in it: user first, roles
    alternating, nothing empty, and every tool call answered in the very next message."""
    assert messages and messages[0]["role"] == "user"
    for earlier, later in zip(messages, messages[1:], strict=False):
        assert earlier["role"] != later["role"], f"two {later['role']} messages in a row"
    for index, message in enumerate(messages):
        assert message["content"], f"message {index} is empty"
        if message["role"] != "assistant":
            continue
        asked = [b["id"] for b in message["content"] if b["type"] == "tool_use"]
        if asked:
            assert index + 1 < len(messages), "a tool call was never answered"
            answered = [b["tool_use_id"] for b in messages[index + 1]["content"] if b["type"] == "tool_result"]
            assert answered == asked, f"tool calls {asked} were answered with {answered}"


@asynccontextmanager
async def make_relay(
    toml: str = "",
    *script: Any,
    stubs: Sequence[Stub] = (),
    env: Mapping[str, str] | None = None,
    files: Mapping[str, str] | None = None,
    access: bool = False,
) -> AsyncIterator[Relay]:
    """A relay built from ``toml`` (a whole relay.toml) with a scripted Claude. No [access]: loopback only.

    ``stubs`` stand in for the MCP servers the toml names. ``files`` are written beside relay.toml
    first, for a test that names its own prompt file. ``access`` checks assertions with the test
    key (keys.py) against the toml's [access] table and [[caller]] tokens.
    """
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name, content in (files or {}).items():
            (folder / name).write_text(content)
        (folder / "relay.toml").write_text(toml)
        cfg, secrets = load_config(folder / "relay.toml", {**TEST_ENV, **(env or {})})
        claude, clock = FakeClaude(*script), FakeClock()
        verifier = None
        if access:
            assert cfg.access is not None, "access=True needs an [access] table in the toml"
            verifier = AccessVerifier(AccessPolicy.from_config(cfg.access, cfg.caller), resolver)
        app = create_app(
            cfg, secrets, claude=claude, clock=clock, transport_for=transport_for(*stubs), verifier=verifier
        )
        async with client_for(app) as http:
            yield Relay(
                http=http, claude=claude, clock=clock, app=app, dir=folder, stubs={stub.name: stub for stub in stubs}
            )


@asynccontextmanager
async def gated(
    *script: Any,
    state: str = "home",
    search: Any = None,
    toml: str = GATED,
    env: Mapping[str, str] | None = None,
    stubs: Sequence[Stub] = (),
    access: bool = False,
    files: Mapping[str, str] | None = None,
) -> AsyncIterator[tuple[Relay, Stub]]:
    """A relay with the garage gate in force and a house to act on: yields (relay, house).

    ``stubs`` are more servers beside the house; ``access`` and ``files`` go to make_relay.
    """
    async with house_stub(state, search=search) as house:
        hashed = {"RELAY_PASSPHRASE_HASH": PHRASE_HASH, **(env or {})}
        async with make_relay(toml, *script, stubs=[house, *stubs], env=hashed, access=access, files=files) as relay:
            yield relay, house
