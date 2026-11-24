import logging

import jwt
import pytest

from conftest import client_for
from hey_claude_relay.__main__ import main
from hey_claude_relay.access import FORWARDED_HEADERS, AccessPolicy, AccessVerifier, check_bind
from hey_claude_relay.app import create_app
from hey_claude_relay.config import AccessConfig, ConfigError, RelayConfig, Secrets
from keys import AUD, CALLER, TEAM, mint, resolver

UNAUTHORIZED = {"error": {"code": "unauthorized", "spoken": "This device isn't allowed to use the relay."}}


@pytest.fixture
def access_app():
    """The relay behind Access: the test key stands in for Cloudflare's."""
    cfg = RelayConfig(access=AccessConfig(team_domain=TEAM, audiences=(AUD,), service_tokens=(CALLER,)))
    verifier = AccessVerifier(AccessPolicy.from_config(cfg.access), resolver)
    return create_app(cfg, Secrets(anthropic_api_key="test-key-not-real"), verifier=verifier)


@pytest.fixture
def local_app():
    """No [access] table: this machine only."""
    return create_app(RelayConfig(), Secrets(anthropic_api_key="test-key-not-real"))


async def post(app, headers=None):
    async with client_for(app) as c:
        return await c.post("/v1/conversations/x/end", headers=headers or {})


async def test_no_assertion_is_unauthorized(access_app):
    r = await post(access_app)
    assert r.status_code == 403 and r.json() == UNAUTHORIZED


async def test_proxy_headers_without_an_assertion_are_refused(access_app):
    r = await post(access_app, {"Cf-Connecting-Ip": "203.0.113.9", "Cf-Ray": "abc"})
    assert r.status_code == 403 and r.json() == UNAUTHORIZED


async def test_listed_token_gets_through(access_app):
    assert (await post(access_app, {"Cf-Access-Jwt-Assertion": mint()})).status_code == 204


async def test_listed_token_gets_through_the_tunnel(access_app):
    # What a real request looks like: Cloudflare's forwarding headers and the public Host, plus the assertion.
    headers = {"Cf-Access-Jwt-Assertion": mint(), "Cf-Connecting-Ip": "203.0.113.9", "Host": "relay.example.com"}
    assert (await post(access_app, headers)).status_code == 204


async def test_unlisted_token_is_unauthorized(access_app):
    r = await post(access_app, {"Cf-Access-Jwt-Assertion": mint(common_name="stranger.access")})
    assert r.status_code == 403 and r.json() == UNAUTHORIZED


async def test_a_bad_assertion_is_unauthorized(access_app):
    r = await post(access_app, {"Cf-Access-Jwt-Assertion": mint(aud="another-application")})
    assert r.status_code == 403 and r.json() == UNAUTHORIZED


async def test_an_unknown_path_without_an_assertion_is_unauthorized_not_404(access_app):
    async with client_for(access_app) as c:
        assert (await c.post("/v1/turn", json={"turn_id": "t", "text": "hi"})).status_code == 403


async def test_healthz_answers_before_auth(access_app):
    async with client_for(access_app) as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/healthz", headers={"Host": "relay.example.com", "Cf-Ray": "abc"})).status_code == 200


async def test_only_get_healthz_skips_auth(access_app):
    async with client_for(access_app) as c:
        assert (await c.post("/healthz")).status_code == 403


async def test_the_caller_is_recorded_for_the_handler(access_app):
    seen = []

    async def spy(scope, receive, send):
        seen.append(scope["state"]["caller"])
        await inner(scope, receive, send)

    # wrap the router, underneath the middleware stack
    inner = access_app.router
    access_app.router = spy
    access_app.middleware_stack = None
    await post(access_app, {"Cf-Access-Jwt-Assertion": mint()})
    assert seen == [CALLER]


@pytest.mark.parametrize("header", FORWARDED_HEADERS)
async def test_without_access_a_forwarded_request_is_refused(local_app, header):
    r = await post(local_app, {header: "203.0.113.9"})
    assert r.status_code == 403 and r.json() == UNAUTHORIZED


async def test_without_access_even_healthz_refuses_a_forwarded_request(local_app):
    async with client_for(local_app) as c:
        assert (await c.get("/healthz", headers={"X-Forwarded-For": "203.0.113.9"})).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8787", "localhost:8787", "localhost", "[::1]:8787"])
async def test_without_access_a_loopback_host_is_served(local_app, host):
    assert (await post(local_app, {"Host": host})).status_code == 204


OTHER_HOSTS = [
    "relay.example.com",
    "192.0.2.10:8787",
    "127.0.0.1.example.com",
    "localhost.example.com",
    "",
    "127.0.0.1:8787@evil.example",
    "[::1]evil.example",
    "localhost:0.0.0.0",
    "localhost:port",
    "0.0.0.0:8787",
]


@pytest.mark.parametrize("host", OTHER_HOSTS)
async def test_without_access_any_other_host_is_refused(local_app, host):
    assert (await post(local_app, {"Host": host})).status_code == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "http://relay.example.com:8787", "null", "file://", ""])
async def test_without_access_a_cross_site_request_is_refused(local_app, origin):
    # What a web page open in a browser on this machine can send without asking: a "simple" POST.
    r = await post(local_app, {"Origin": origin, "Content-Type": "text/plain"})
    assert r.status_code == 403 and r.json() == UNAUTHORIZED


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8787", "http://localhost:3000", "http://[::1]:8787"])
async def test_without_access_a_loopback_origin_is_served(local_app, origin):
    assert (await post(local_app, {"Origin": origin})).status_code == 204


@pytest.fixture
def lan_test_app():
    """--insecure-no-auth: a deliberate test on a trusted LAN."""
    return create_app(RelayConfig(), Secrets(anthropic_api_key="test-key-not-real"), insecure_no_auth=True)


@pytest.mark.parametrize("host", ["192.0.2.10:8787", "localhost:8787", "[2001:db8::10]:8787", "127.0.0.1"])
async def test_insecure_no_auth_serves_requests_addressed_by_ip(lan_test_app, host):
    assert (await post(lan_test_app, {"X-Forwarded-For": "192.0.2.7", "Host": host})).status_code == 204


@pytest.mark.parametrize("host", ["rebind.evil.example:8787", "relay.example.com", "", "192.0.2.10:8787@evil.example"])
async def test_insecure_no_auth_still_refuses_a_host_name(lan_test_app, host):
    # A name can be re-pointed at this machine by a hostile page (DNS rebinding); an address cannot.
    assert (await post(lan_test_app, {"Host": host})).status_code == 403


async def test_insecure_no_auth_still_refuses_a_cross_site_request(lan_test_app):
    r = await post(lan_test_app, {"Host": "192.0.2.10:8787", "Origin": "https://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("insecure", [False, True])
async def test_an_access_table_turns_the_check_on_by_itself(insecure):
    # No verifier handed in: this is the line production relies on. No network is needed to refuse.
    cfg = RelayConfig(access=AccessConfig(team_domain=TEAM, audiences=(AUD,), service_tokens=(CALLER,)))
    app = create_app(cfg, Secrets(anthropic_api_key="test-key-not-real"), insecure_no_auth=insecure)
    r = await post(app, {"Host": "localhost"})
    assert r.status_code == 403 and r.json() == UNAUTHORIZED


async def test_a_denial_cannot_forge_a_log_line(caplog):
    # Two things in a denial line come from the sender: the path, and (inside PyJWT's message) the
    # key id named by an assertion nobody has verified yet.
    async def no_such_key(token):
        raise jwt.PyJWKClientError("Unable to find a signing key that matches: 'x\nWARNING all is well'")

    cfg = RelayConfig(access=AccessConfig(team_domain=TEAM, audiences=(AUD,), service_tokens=(CALLER,)))
    app = create_app(
        cfg,
        Secrets(anthropic_api_key="test-key-not-real"),
        verifier=AccessVerifier(AccessPolicy.from_config(cfg.access), no_such_key),
    )
    caplog.set_level(logging.WARNING)
    async with client_for(app) as c:
        await c.post("/v1/x%0AWARNING%20all%20is%20well", headers={"Cf-Access-Jwt-Assertion": mint()})
    denials = [r.getMessage() for r in caplog.records if "Denied" in r.getMessage()]
    assert len(denials) == 1 and "all is well" in denials[0]
    assert "\n" not in denials[0] and "\r" not in denials[0]


def test_serving_beyond_loopback_needs_access():
    with pytest.raises(ConfigError, match="Cloudflare Access"):
        check_bind("0.0.0.0", access=False, insecure_no_auth=False)
    with pytest.raises(ConfigError, match="Cloudflare Access"):
        check_bind("192.0.2.10", access=False, insecure_no_auth=False)
    check_bind("0.0.0.0", access=True, insecure_no_auth=False)
    check_bind("0.0.0.0", access=False, insecure_no_auth=True)
    for loopback in ("127.0.0.1", "localhost", "::1"):
        check_bind(loopback, access=False, insecure_no_auth=False)


def test_serve_refuses_an_open_bind_without_access(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    cfg = tmp_path / "relay.toml"
    cfg.write_text("")
    assert main(["serve", "--config", str(cfg), "--bind", "0.0.0.0"]) == 2
    assert "Cloudflare Access" in capsys.readouterr().err


async def test_a_denial_is_logged_without_the_token(access_app, caplog):
    caplog.set_level(logging.DEBUG)
    token = mint(common_name="stranger.access")
    await post(access_app, {"Cf-Access-Jwt-Assertion": token})
    denials = [r for r in caplog.records if r.levelno == logging.WARNING and "Denied" in r.getMessage()]
    assert len(denials) == 1 and "unknown caller" in denials[0].getMessage()
    assert token not in caplog.text and token.split(".")[2] not in caplog.text
