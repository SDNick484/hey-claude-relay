import anyio
from mcp.server.mcpserver.exceptions import ToolError

from conftest import COOKBOOKS_NONE, MATINEE, TWO_SERVERS, assert_valid_history, make_relay
from fakes import THINKING, message, refuse, say, use
from hey_claude_relay.errors import RelayError
from hey_claude_relay.turn import SAFETY_RULES
from stubs import legacy_stub, sdk_stub


def where_is(title: str) -> str:
    return "Shelf 2"


def history(relay, conversation_id):
    return relay.app.state.conversations.get(conversation_id).messages


async def test_tool_round_trip_and_tools_used():
    script = [use(("matinee__where_is", {"title": "Paddington 2"})), say("It's on shelf two.")]
    async with sdk_stub("matinee", {"where_is": where_is}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        body = (await r.turn("where is Paddington 2")).json()
        assert_valid_history(history(r, body["conversation_id"]))
    assert body["reply"] == "It's on shelf two." and body["tools_used"] == ["matinee.where_is"]
    assert m.calls == [("where_is", {"title": "Paddington 2"})]
    sent = r.claude.calls[1]["messages"]
    assert sent[-1] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "Shelf 2", "is_error": False}],
    }
    assert [t["name"] for t in r.claude.calls[0]["tools"]] == ["matinee__where_is"]


async def test_the_models_own_blocks_go_back_unchanged():
    script = [use(("matinee__where_is", {"title": "Up"}), text="Looking."), say("Shelf two.")]
    async with sdk_stub("matinee", {"where_is": where_is}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        await r.turn("where is Up")
    assert r.claude.calls[1]["messages"][1] == {
        "role": "assistant",
        "content": [
            THINKING,
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "toolu_1", "name": "matinee__where_is", "input": {"title": "Up"}},
        ],
    }


async def test_all_results_of_one_message_go_back_together():
    script = [
        use(("matinee__where_is", {"title": "a"}), ("matinee__where_is", {"title": "b"})),
        say("Both on shelf two."),
    ]
    async with sdk_stub("matinee", {"where_is": where_is}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        body = (await r.turn("where are a and b")).json()
    results = r.claude.calls[1]["messages"][-1]["content"]
    assert [x["tool_use_id"] for x in results] == ["toolu_1", "toolu_2"] and len(r.claude.calls[1]["messages"]) == 3
    assert [c[1]["title"] for c in m.calls] == ["a", "b"] and body["tools_used"] == ["matinee.where_is"]


async def test_several_rounds_and_several_servers():
    script = [use(("matinee__where_is", {"title": "a"})), use(("house__ha_search", {"query": "lamp"})), say("Done.")]
    async with (
        sdk_stub("matinee", {"where_is": where_is}) as m,
        sdk_stub("house", {"ha_search": lambda query: "x"}) as h,
        make_relay(TWO_SERVERS, *script, stubs=[m, h]) as r,
    ):
        body = (await r.turn("find things")).json()
        assert_valid_history(history(r, body["conversation_id"]))
    assert body["tools_used"] == ["matinee.where_is", "house.ha_search"] and len(r.claude.calls) == 3


async def test_a_tool_that_was_never_offered_is_not_run():
    tools = {"where_is": where_is, "rate_movie": lambda title_id: "rated"}
    script = [
        use(("matinee__rate_movie", {"title_id": 1}), ("nope__x", {}), ("where_is", {"title": "a"})),
        say("I can't do that."),
    ]
    async with sdk_stub("matinee", tools) as m, make_relay(MATINEE + 'tools = ["where_is"]\n', *script, stubs=[m]) as r:
        body = (await r.turn("rate it ten")).json()
        assert_valid_history(history(r, body["conversation_id"]))
    assert m.calls == [] and body["tools_used"] == [] and body["reply"] == "I can't do that."
    results = r.claude.calls[1]["messages"][-1]["content"]
    assert [x["is_error"] for x in results] == [True, True, True]
    assert results[0]["content"] == "No tool named matinee__rate_movie is available. Use only the tools you were given."


async def test_a_tool_error_reaches_claude_as_an_error():
    def missing(title: str) -> str:
        raise ToolError("No title like that.")

    script = [use(("matinee__where_is", {"title": "zzz"})), say("I couldn't find that one.")]
    async with sdk_stub("matinee", {"where_is": missing}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        body = (await r.turn("where is zzz")).json()
    result = r.claude.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "No title like that." in result["content"]
    assert body["tools_used"] == ["matinee.where_is"]  # it was sent, so it is listed


async def test_repeated_turn_id_runs_no_tool_again():
    script = [use(("matinee__log", {})), say("Done.")]
    async with sdk_stub("matinee", {"log": lambda: "logged"}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        a = await r.turn("log it", turn_id="t-9")
        b = await r.turn("log it", turn_id="t-9")
    assert a.status_code == 200 and a.json() == b.json() and len(m.calls) == 1 and len(r.claude.calls) == 2


async def test_a_retry_that_arrives_mid_turn_runs_no_tool_again():
    release = anyio.Event()

    async def slow_log() -> str:
        await release.wait()
        return "logged"

    script = [use(("matinee__log", {})), say("Done.")]
    async with sdk_stub("matinee", {"log": slow_log}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        replies = []

        async def ask():
            replies.append((await r.turn("log it", turn_id="t-9")).json())

        async with anyio.create_task_group() as tg:
            tg.start_soon(ask)
            tg.start_soon(ask)
            await anyio.sleep(0.1)
            release.set()
    assert len(m.calls) == 1 and replies[0] == replies[1] and replies[0]["reply"] == "Done."


async def test_tool_list_is_fixed_for_the_life_of_a_conversation():
    tools = {"search_recipes": lambda a: {}}
    stub = legacy_stub("cookbooks", tools)
    async with make_relay(COOKBOOKS_NONE, say("One."), say("Two."), say("Three."), stubs=[stub]) as r:
        cid = (await r.turn("a")).json()["conversation_id"]
        tools["get_recipe"] = lambda a: {}
        r.clock.advance(301)  # the cached tool list is stale now
        await r.turn("b", conversation_id=cid)
        await r.turn("c")
    names = [[t["name"] for t in call["tools"]] for call in r.claude.calls]
    assert names[0] == names[1] == ["cookbooks__search_recipes"]  # the conversation keeps what it started with
    assert names[2] == ["cookbooks__get_recipe", "cookbooks__search_recipes"]  # a new one sees the new tool
    assert r.claude.calls[0]["system"] == r.claude.calls[1]["system"]


async def test_unreachable_server_is_named_in_the_system_prompt_and_the_turn_still_answers():
    async with sdk_stub("matinee", {"where_is": where_is}) as m, sdk_stub("house", {"b": lambda: "x"}) as h:
        h.fail = True
        async with make_relay(TWO_SERVERS, say("I couldn't reach the house."), stubs=[m, h]) as r:
            resp = await r.turn("turn off the lights")
    system = r.claude.calls[0]["system"]
    assert resp.status_code == 200 and resp.json()["reply"] == "I couldn't reach the house."
    assert "could not be reached when this conversation started: house." in system
    assert system.index(SAFETY_RULES) < system.index("could not be reached")
    assert [t["name"] for t in r.claude.calls[0]["tools"]] == ["matinee__where_is"]


async def test_no_note_when_every_server_answers():
    async with sdk_stub("matinee", {"where_is": where_is}) as m, make_relay(MATINEE, say("Hi."), stubs=[m]) as r:
        await r.turn("hello")
    assert r.claude.calls[0]["system"].endswith(SAFETY_RULES)


async def test_a_failed_turn_after_a_tool_ran_is_stored_not_rerun():
    script = [
        use(("matinee__log", {})),
        RelayError("upstream_error", "I couldn't reach Claude just now."),
        say("Yes, it was logged."),
    ]
    async with sdk_stub("matinee", {"log": lambda: "logged"}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        a = await r.turn("log it", turn_id="t-1")
        b = await r.turn("log it", turn_id="t-1")
        conversation = next(iter(r.app.state.conversations._conversations.values()))
        assert_valid_history(conversation.messages)
        after = await r.turn("did that work", conversation_id=conversation.id)
    assert a.status_code == b.status_code == 502 and a.json() == b.json() and len(m.calls) == 1
    assert after.status_code == 200 and after.json()["reply"] == "Yes, it was logged."
    assert r.claude.calls[2]["messages"][-2]["content"] == [
        {"type": "text", "text": "I couldn't reach Claude just now."}
    ]


async def test_a_refusal_after_a_tool_ran_keeps_the_conversation_usable():
    script = [use(("matinee__where_is", {"title": "a"})), refuse(), say("Shelf two, as I found.")]
    async with sdk_stub("matinee", {"where_is": where_is}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        first = (await r.turn("where is a")).json()
        assert first["reply"] == "I can't help with that one." and first["tools_used"] == ["matinee.where_is"]
        assert_valid_history(history(r, first["conversation_id"]))
        assert (await r.turn("and?", conversation_id=first["conversation_id"])).status_code == 200


async def test_no_words_after_a_tool_ran_is_still_spoken_and_usable():
    script = [use(("matinee__where_is", {"title": "a"})), say(""), say("Shelf two.")]
    async with sdk_stub("matinee", {"where_is": where_is}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        first = (await r.turn("where is a")).json()
        assert first["reply"] == "I don't have an answer for that."
        assert_valid_history(history(r, first["conversation_id"]))
        assert (await r.turn("again?", conversation_id=first["conversation_id"])).json()["reply"] == "Shelf two."


async def test_a_cut_off_tool_call_leaves_usable_history():
    cut_off = message(
        [
            THINKING,
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "toolu_9", "name": "matinee__where_is", "input": {}},
        ],
        "max_tokens",
    )
    async with (
        sdk_stub("matinee", {"where_is": where_is}) as m,
        make_relay(MATINEE, cut_off, say("Shelf two."), stubs=[m]) as r,
    ):
        first = (await r.turn("where is a")).json()
        assert first["reply"] == "Let me check." and m.calls == []
        assert_valid_history(history(r, first["conversation_id"]))
        await r.turn("well?", conversation_id=first["conversation_id"])
    assert r.claude.calls[1]["messages"][1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "Let me check."}],
    }


async def test_a_crash_after_a_tool_ran_keeps_the_conversation_usable():
    script = [use(("matinee__where_is", {"title": "a"})), RuntimeError("unexpected"), say("Shelf two.")]
    async with sdk_stub("matinee", {"where_is": where_is}) as m, make_relay(MATINEE, *script, stubs=[m]) as r:
        crashed = await r.turn("where is a")
        conversation = next(iter(r.app.state.conversations._conversations.values()))
        assert_valid_history(conversation.messages)
        after = await r.turn("well?", conversation_id=conversation.id)
    assert crashed.status_code == 500 and after.json()["reply"] == "Shelf two."


async def test_a_prompt_file_that_disappears_does_not_stop_new_conversations(caplog):
    files = {"voice.md": "Be brief."}
    async with make_relay('prompt_file = "voice.md"\n', say("One."), say("Two."), files=files) as r:
        await r.turn("a")
        (r.dir / "voice.md").unlink()
        second = await r.turn("b")
    assert second.status_code == 200 and r.claude.calls[1]["system"].startswith("Be brief.")
    assert any("voice.md" in x.getMessage() for x in caplog.records)
