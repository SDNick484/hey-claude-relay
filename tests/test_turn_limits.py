import time

import anyio

from conftest import MATINEE, assert_valid_history, make_relay
from fakes import refuse, say, use
from hey_claude_relay.config import RelayConfig
from hey_claude_relay.errors import RelayError
from hey_claude_relay.turn import OUT_OF_TIME, WRAP_UP
from stubs import sdk_stub

SEARCH = use(("matinee__search", {}))


def only_conversation(relay):
    return next(iter(relay.app.state.conversations._conversations.values()))


async def test_round_limit_ends_in_a_spoken_reply():
    script = [SEARCH] * 8 + [say("I found two so far.")]
    async with sdk_stub("matinee", {"search": lambda: "more"}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        resp = await r.turn("keep looking")
        assert_valid_history(only_conversation(r).messages)
    assert resp.status_code == 200
    assert resp.json()["reply"] == "I found two so far. I ran out of time before I could finish."
    assert resp.json()["tools_used"] == ["matinee.search"] and resp.json()["expect_secret"] is False
    assert len(m.calls) == 8 and len(r.claude.calls) == 9
    wrap = r.claude.calls[8]
    assert wrap["allow_tools"] is False and wrap["timeout"] == 10
    assert wrap["messages"][-1]["content"][-1] == {"type": "text", "text": WRAP_UP}
    assert (
        wrap["messages"][-1]["content"][0]["type"] == "tool_result"
    )  # the note follows the results, in the same message
    assert all(c["allow_tools"] is True for c in r.claude.calls[:8])


async def test_max_rounds_comes_from_config():
    script = [SEARCH, SEARCH, say("Two looks.")]
    async with sdk_stub("matinee", {"search": lambda: "more"}) as m:
        async with make_relay("max_rounds = 2\n" + MATINEE, *script, stubs=[m]) as r:
            body = (await r.turn("keep looking")).json()
    assert body["reply"] == "Two looks. I ran out of time before I could finish." and len(m.calls) == 2


async def test_a_turn_that_finishes_on_the_last_round_is_not_cut():
    script = [SEARCH, say("Found it.")]
    async with sdk_stub("matinee", {"search": lambda: "more"}) as m:
        async with make_relay("max_rounds = 2\n" + MATINEE, *script, stubs=[m]) as r:
            body = (await r.turn("look")).json()
    assert body["reply"] == "Found it."


async def test_time_limit_ends_in_a_spoken_reply():
    async def slow() -> str:
        await anyio.sleep(30)
        return "late"

    started = time.perf_counter()
    script = [use(("matinee__slow", {}), ("matinee__slow", {})), say("Nothing yet")]
    async with sdk_stub("matinee", {"slow": slow}) as m:
        async with make_relay("turn_seconds = 0.2\n" + MATINEE, *script, stubs=[m]) as r:
            resp = await r.turn("look it up")
            assert_valid_history(only_conversation(r).messages)
    assert resp.status_code == 200 and time.perf_counter() - started < 5
    assert resp.json()["reply"] == "Nothing yet. I ran out of time before I could finish."
    results = [b for b in r.claude.calls[1]["messages"][-1]["content"] if b["type"] == "tool_result"]
    assert [x["tool_use_id"] for x in results] == ["toolu_1", "toolu_2"] and all(x["is_error"] for x in results)
    assert "stopped waiting" in results[0]["content"] and "may or may not have finished" in results[0]["content"]
    assert results[1]["content"] == "Not run: the turn ran out of time first."
    assert len(m.calls) == 1  # the second call was never sent


async def test_time_limit_while_claude_is_thinking():
    async def thinking(call):
        await anyio.sleep(30)
        return say("never heard")

    async with make_relay("turn_seconds = 0.2\n", thinking, say("Short answer.")) as r:
        resp = await r.turn("a hard one")
        assert_valid_history(only_conversation(r).messages)
    assert resp.status_code == 200 and resp.json()["reply"] == "Short answer. I ran out of time before I could finish."
    assert r.claude.calls[1]["allow_tools"] is False
    assert r.claude.calls[1]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "a hard one"}, {"type": "text", "text": WRAP_UP}]}
    ]


async def test_each_call_gets_no_more_time_than_the_turn_has_left():
    async with (
        sdk_stub("matinee", {"search": lambda: "more"}) as m,
        make_relay("turn_seconds = 5\nclaude_seconds = 30\n" + MATINEE, SEARCH, say("Done."), stubs=[m]) as r,
    ):
        await r.turn("look")
    assert all(0 < c["timeout"] <= 5 for c in r.claude.calls)


async def test_wrap_up_failure_still_speaks():
    script = [SEARCH] * 8 + [RelayError("upstream_timeout", "x")]
    async with sdk_stub("matinee", {"search": lambda: "more"}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        resp = await r.turn("keep looking")
        assert_valid_history(only_conversation(r).messages)
    assert resp.status_code == 200 and resp.json()["reply"] == "I ran out of time before I could finish."
    assert OUT_OF_TIME == "I ran out of time before I could finish."


async def test_a_wrap_up_with_no_words_or_a_refusal_still_speaks():
    for last in (say(""), refuse()):
        script = [SEARCH, last]
        async with sdk_stub("matinee", {"search": lambda: "more"}) as m:
            async with make_relay("max_rounds = 1\n" + MATINEE, *script, stubs=[m]) as r:
                body = (await r.turn("look")).json()
                assert_valid_history(only_conversation(r).messages)
        assert body["reply"] == "I ran out of time before I could finish."


async def test_the_conversation_is_still_usable_after_a_limit():
    script = [SEARCH, say("One so far."), say("Here is the rest.")]
    async with sdk_stub("matinee", {"search": lambda: "more"}) as m:
        async with make_relay("max_rounds = 1\n" + MATINEE, *script, stubs=[m]) as r:
            first = (await r.turn("look")).json()
            second = await r.turn("go on", conversation_id=first["conversation_id"])
            assert_valid_history(only_conversation(r).messages)
    assert second.status_code == 200 and second.json()["reply"] == "Here is the rest."
    assert [m["role"] for m in r.claude.calls[2]["messages"]] == ["user", "assistant", "user", "assistant", "user"]


async def test_a_limit_is_logged_as_its_own_outcome(caplog):
    import logging

    caplog.set_level(logging.INFO)
    async with sdk_stub("matinee", {"search": lambda: "more"}) as m:
        async with make_relay("max_rounds = 1\n" + MATINEE, SEARCH, say("One."), stubs=[m]) as r:
            await r.turn("look")
    line = next(x.getMessage() for x in caplog.records if x.getMessage().startswith("turn "))
    assert "outcome=limit_rounds" in line and "rounds=2" in line and "tools=matinee.search" in line


def test_a_turn_always_answers_inside_sixty_seconds():
    cfg = RelayConfig()
    assert cfg.turn_seconds + cfg.wrapup_seconds == 55  # clients allow 60
