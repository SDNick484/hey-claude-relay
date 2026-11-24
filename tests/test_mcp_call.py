import json
import logging
import time

import anyio
import mcp_types
from mcp.server.mcpserver.exceptions import ToolError

from conftest import COOKBOOKS_NONE, HOUSE, MATINEE, TWO_SERVERS, servers
from hey_claude_relay.mcp_client import MAX_RESULT_CHARS, ToolOutcome
from stubs import legacy_stub, sdk_stub


def where_is(title: str) -> str:
    return f"{title}: Shelf 2"


async def test_call_returns_the_text_and_records_the_arguments():
    async with sdk_stub("matinee", {"where_is": where_is}) as stub, servers(MATINEE, stub).session() as s:
        out = await s.call("matinee", "where_is", {"title": "Paddington 2"}, timeout=5)
    assert out == ToolOutcome("Paddington 2: Shelf 2", False)
    assert stub.calls == [("where_is", {"title": "Paddington 2"})]


async def test_calls_carry_the_servers_credentials():
    async with sdk_stub("matinee", {"where_is": where_is}) as stub, servers(MATINEE, stub).session() as s:
        await s.call("matinee", "where_is", {"title": "x"}, timeout=5)
    assert stub.headers and all(h["cf-access-client-secret"] == "s3cret" for h in stub.headers)


async def test_legacy_json_server_works_too():
    stub = legacy_stub("cookbooks", {"search_recipes": lambda a: {"hits": [a["query"]]}})
    async with servers(COOKBOOKS_NONE, stub).session() as s:
        out = await s.call("cookbooks", "search_recipes", {"query": "leeks"}, timeout=5)
    assert json.loads(out.text) == {"hits": ["leeks"]} and out.is_error is False
    assert stub.calls == [("search_recipes", {"query": "leeks"})]


async def test_one_connection_serves_several_calls_in_a_turn():
    async with sdk_stub("matinee", {"where_is": where_is}) as stub:
        async with servers(MATINEE, stub).session() as s:
            await s.call("matinee", "where_is", {"title": "a"}, timeout=5)
            one_call = len(stub.headers)
            await s.call("matinee", "where_is", {"title": "b"}, timeout=5)
            assert len(stub.headers) == one_call + 1  # the second call did not connect again
        async with servers(MATINEE, stub).session() as s:  # a new turn connects afresh
            before = len(stub.headers)
            await s.call("matinee", "where_is", {"title": "c"}, timeout=5)
            assert len(stub.headers) - before == one_call
    assert [c[1]["title"] for c in stub.calls] == ["a", "b", "c"]


async def test_two_servers_in_one_turn():
    async with (
        sdk_stub("matinee", {"where_is": where_is}) as m,
        sdk_stub("house", {"ha_search": lambda query: "found"}) as h,
        servers(TWO_SERVERS, m, h).session() as s,
    ):
        first = await s.call("matinee", "where_is", {"title": "a"}, timeout=5)
        second = await s.call("house", "ha_search", {"query": "lamp"}, timeout=5)
    assert (first.text, second.text) == ("a: Shelf 2", "found")
    assert all("cf-access-client-id" not in x for x in h.headers)  # the house stub is auth = "none"


async def test_tool_error_is_an_error_outcome_not_an_exception():
    def boom():
        raise ToolError("no such title")  # the SDK passes a ToolError's words on; other errors it hides

    async with sdk_stub("matinee", {"boom": boom, "where_is": where_is}) as stub, servers(MATINEE, stub).session() as s:
        out = await s.call("matinee", "boom", {}, timeout=5)
        after = await s.call("matinee", "where_is", {"title": "a"}, timeout=5)  # the connection is still good
    assert out.is_error and "no such title" in out.text
    assert after == ToolOutcome("a: Shelf 2", False)


async def test_a_tool_the_server_does_not_have_is_an_error_outcome():
    stub = legacy_stub("cookbooks", {"search_recipes": lambda a: {}})
    async with servers(COOKBOOKS_NONE, stub).session() as s:
        out = await s.call("cookbooks", "delete_everything", {}, timeout=5)
    assert out.is_error and "delete_everything" in out.text and stub.calls == []


async def test_slow_tool_times_out_with_a_plain_sentence():
    async def slow():
        await anyio.sleep(30)
        return "late"

    started = time.perf_counter()
    async with sdk_stub("matinee", {"slow": slow, "where_is": where_is}) as stub, servers(MATINEE, stub).session() as s:
        out = await s.call("matinee", "slow", {}, timeout=0.05)
        after = await s.call("matinee", "where_is", {"title": "a"}, timeout=5)  # a fresh connection takes over
    assert out == ToolOutcome(
        "The relay stopped waiting for this tool after 0.05 seconds. It may or may not have finished.", True
    )
    assert after.text == "a: Shelf 2"
    assert time.perf_counter() - started < 5


async def test_unreachable_server_is_an_error_outcome(caplog):
    async with sdk_stub("house", {"a": lambda: "x"}) as stub:
        stub.fail = True
        async with servers(HOUSE, stub).session() as s:
            out = await s.call("house", "a", {}, timeout=5)
            again = await s.call("house", "a", {}, timeout=5)
    assert out == again == ToolOutcome("The relay could not reach the house server.", True)
    assert any("house" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_a_server_that_comes_back_is_reached_again_in_the_same_turn():
    async with sdk_stub("house", {"a": lambda: "x"}) as stub:
        stub.fail = True
        async with servers(HOUSE, stub).session() as s:
            assert (await s.call("house", "a", {}, timeout=5)).is_error
            stub.fail = False
            assert await s.call("house", "a", {}, timeout=5) == ToolOutcome("x", False)


async def test_a_server_that_is_not_configured_is_an_error_outcome():
    async with servers(MATINEE).session() as s:
        out = await s.call("nowhere", "a", {}, timeout=5)
    assert out == ToolOutcome("The relay could not reach the nowhere server.", True)


async def test_long_result_is_cut_with_a_note():
    async with sdk_stub("matinee", {"big": lambda: "x" * 30000}) as stub, servers(MATINEE, stub).session() as s:
        out = await s.call("matinee", "big", {}, timeout=5)
    assert out.text == "x" * MAX_RESULT_CHARS + " [cut: the result was longer]" and MAX_RESULT_CHARS == 20000


async def test_non_text_content_is_named_not_dropped():
    def picture():
        return [
            mcp_types.TextContent(type="text", text="A poster."),
            mcp_types.ImageContent(type="image", data="aGk=", mime_type="image/png"),
        ]

    async with sdk_stub("matinee", {"picture": picture}) as stub, servers(MATINEE, stub).session() as s:
        out = await s.call("matinee", "picture", {}, timeout=5)
    assert out == ToolOutcome("A poster.\n[image omitted]", False)


async def test_a_tool_that_returns_nothing_still_has_a_result():
    async with sdk_stub("matinee", {"quiet": lambda: None}) as stub, servers(MATINEE, stub).session() as s:
        out = await s.call("matinee", "quiet", {}, timeout=5)
    assert out.text and out.is_error is False


async def test_a_turn_deadline_cancels_a_call_cleanly():
    async def slow():
        await anyio.sleep(30)

    started = time.perf_counter()
    async with sdk_stub("matinee", {"slow": slow}) as stub, servers(MATINEE, stub).session() as s:
        with anyio.move_on_after(0.05) as scope:
            await s.call("matinee", "slow", {}, timeout=20)
    assert scope.cancelled_caught and time.perf_counter() - started < 5


async def test_an_exception_inside_a_session_comes_out_as_itself():
    class Oops(Exception):
        pass

    async with sdk_stub("matinee", {"where_is": where_is}) as stub:
        try:
            async with servers(MATINEE, stub).session() as s:
                await s.call("matinee", "where_is", {"title": "a"}, timeout=5)
                raise Oops("from the caller")
        except Oops as exc:
            caught = exc
    assert str(caught) == "from the caller"
