import logging

from conftest import COOKBOOKS_BEARER, COOKBOOKS_NONE, MATINEE, TWO_SERVERS, FakeClock, load_toml, servers
from hey_claude_relay.config import Secrets
from hey_claude_relay.mcp_client import auth_headers
from stubs import legacy_stub, sdk_stub


def where_is(title: str) -> str:
    """Say where a title is shelved."""
    return f"{title}: Shelf 2"


async def test_service_token_headers_reach_the_server_on_every_request():
    async with sdk_stub("matinee", {"search_collection": lambda query="": "[]"}) as stub:
        cat = await servers(MATINEE, stub).catalog()
    assert [s.claude_name for s in cat.specs] == ["matinee__search_collection"]
    assert stub.headers
    assert all(
        h["cf-access-client-id"] == "relay.access" and h["cf-access-client-secret"] == "s3cret" for h in stub.headers
    )
    assert all("authorization" not in h for h in stub.headers)


async def test_bearer_server_gets_its_token_and_no_access_headers():
    stub = legacy_stub("cookbooks", {"search_recipes": lambda a: {"hits": []}})
    cat = await servers(COOKBOOKS_BEARER, stub).catalog()
    assert cat.specs[0].label == "cookbooks.search_recipes" and cat.unavailable == ()
    assert all(h.get("authorization") == "Bearer tok-1" and "cf-access-client-id" not in h for h in stub.headers)


async def test_a_server_with_no_auth_gets_no_credentials():
    stub = legacy_stub("cookbooks", {"search_recipes": lambda a: {"hits": []}})
    await servers(COOKBOOKS_NONE, stub).catalog()
    assert stub.headers and all("authorization" not in h and "cf-access-client-id" not in h for h in stub.headers)


def test_auth_headers_by_mode():
    secrets = Secrets(cf_client_id="relay.access", cf_client_secret="s3cret", bearer={"COOKBOOKS_MCP_TOKEN": "tok-1"})
    by_name = {s.name: s for s in load_toml(MATINEE + COOKBOOKS_BEARER)[0].server}
    assert auth_headers(by_name["matinee"], secrets) == {
        "CF-Access-Client-Id": "relay.access",
        "CF-Access-Client-Secret": "s3cret",
    }
    assert auth_headers(by_name["cookbooks"], secrets) == {"Authorization": "Bearer tok-1"}
    assert auth_headers(load_toml(COOKBOOKS_NONE)[0].server[0], secrets) == {}


async def test_the_catalog_carries_what_claude_needs():
    async with sdk_stub("matinee", {"where_is": where_is}) as stub:
        cat = await servers(MATINEE, stub).catalog()
    spec = cat.specs[0]
    assert (spec.server, spec.name, spec.claude_name, spec.label) == (
        "matinee",
        "where_is",
        "matinee__where_is",
        "matinee.where_is",
    )
    tool = cat.tools()[0]
    assert tool["name"] == "matinee__where_is" and tool["description"] == "Say where a title is shelved."
    assert tool["input_schema"]["type"] == "object" and "title" in tool["input_schema"]["properties"]
    assert cat.find("matinee__where_is") is spec and cat.find("matinee__nope") is None and cat.find("where_is") is None


async def test_allow_and_deny_lists_are_globs():
    names = ("search_collection", "list_people", "get_movie", "rate_movie")
    async with sdk_stub("matinee", {n: (lambda: "x") for n in names}) as stub:
        toml = MATINEE + 'tools = ["search_*", "list_*", "get_*"]\ndeny = ["get_movie"]\n'
        cat = await servers(toml, stub).catalog()
    assert [s.name for s in cat.specs] == ["list_people", "search_collection"]


async def test_deny_alone_removes_from_everything():
    async with sdk_stub("matinee", {n: (lambda: "x") for n in ("a", "rate_movie", "add_movie")}) as stub:
        cat = await servers(MATINEE + 'deny = ["*_movie"]\n', stub).catalog()
    assert [s.name for s in cat.specs] == ["a"]


async def test_tools_are_listed_in_config_order_then_by_name():
    async with (
        sdk_stub("matinee", {"b": lambda: "x", "a": lambda: "x"}) as m,
        sdk_stub("house", {"z": lambda: "x"}) as h,
    ):
        cat = await servers(TWO_SERVERS, m, h).catalog()
    assert [t["name"] for t in cat.tools()] == ["matinee__a", "matinee__b", "house__z"]


async def test_a_name_claude_cannot_use_is_left_out(caplog):
    long_name = "t" * 70
    async with sdk_stub("matinee", {long_name: lambda: "x", "ok": lambda: "x"}) as stub:
        cat = await servers(MATINEE, stub).catalog()
    assert [s.name for s in cat.specs] == ["ok"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and long_name in warnings[0]


async def test_a_long_description_is_cut():
    def wordy():
        return "x"

    wordy.__doc__ = "d" * 5000
    async with sdk_stub("matinee", {"wordy": wordy}) as stub:
        cat = await servers(MATINEE, stub).catalog()
    assert cat.tools()[0]["description"] == "d" * 2000 + " ..."


async def test_a_large_catalog_is_reported(caplog):
    async with sdk_stub("matinee", {f"tool_{n:02d}": (lambda: "x") for n in range(61)}) as stub:
        cat = await servers(MATINEE, stub).catalog()
    assert len(cat.specs) == 61
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "61 tools" in warnings[0] and "tools" in warnings[0]


async def test_catalog_is_cached_for_the_ttl():
    clock = FakeClock()
    async with sdk_stub("matinee", {"a": lambda: "x"}) as stub:
        s = servers(MATINEE, stub, clock=clock)
        first = await s.catalog()
        seen = len(stub.headers)
        assert await s.catalog() == first and len(stub.headers) == seen
        clock.advance(301)
        await s.catalog()
        assert len(stub.headers) > seen


async def test_a_server_that_answers_a_login_page_is_unavailable_not_fatal(caplog):
    async with sdk_stub("matinee", {"a": lambda: "x"}) as ok, sdk_stub("house", {"b": lambda: "x"}) as down:
        down.fail = True
        cat = await servers(TWO_SERVERS, ok, down).catalog()
    assert [s.server for s in cat.specs] == ["matinee"] and cat.unavailable == ("house",)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "house" in r.getMessage()]
    assert len(warnings) == 1 and "s3cret" not in caplog.text


async def test_a_server_with_no_route_is_unavailable_not_fatal():
    async with sdk_stub("matinee", {"a": lambda: "x"}) as ok:
        cat = await servers(TWO_SERVERS, ok).catalog()  # nothing stands in for "house"
    assert [s.server for s in cat.specs] == ["matinee"] and cat.unavailable == ("house",)


async def test_a_failed_listing_is_tried_again_after_thirty_seconds():
    clock = FakeClock()
    async with sdk_stub("house", {"b": lambda: "x"}) as stub:
        stub.fail = True
        s = servers(TWO_SERVERS, stub, clock=clock)
        assert "house" in (await s.catalog()).unavailable
        stub.fail = False
        clock.advance(29)
        assert "house" in (await s.catalog()).unavailable
        clock.advance(2)
        assert "house" not in (await s.catalog()).unavailable


async def test_no_servers_is_an_empty_catalog():
    cat = await servers("").catalog()
    assert cat.specs == () and cat.unavailable == () and cat.tools() == []
