import copy

import anyio
import pytest

from fakes import THINKING
from hey_claude_relay.config import ProfileConfig
from hey_claude_relay.conversations import ConversationStore, Stored, TurnLedger


@pytest.fixture
def store(clock):
    return ConversationStore(idle_seconds=900, max_turns=40, clock=clock)


@pytest.fixture
def ledger(clock):
    return TurnLedger(clock=clock)


def turn_with_tool(i):
    """One turn as the runner stores it: the question, a tool call, its result, the answer."""
    return [
        {"role": "user", "content": f"q{i}"},
        {
            "role": "assistant",
            "content": [dict(THINKING), {"type": "tool_use", "id": f"t{i}", "name": "a__b", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok", "is_error": False}],
        },
        {"role": "assistant", "content": [dict(THINKING), {"type": "text", "text": f"a{i}"}]},
    ]


def blocks(conv, role, kind):
    return [
        b
        for m in conv.messages
        if m["role"] == role and isinstance(m["content"], list)
        for b in m["content"]
        if b["type"] == kind
    ]


def tool_use_ids(conv):
    return [b["id"] for b in blocks(conv, "assistant", "tool_use")]


def tool_result_ids(conv):
    return [b["tool_use_id"] for b in blocks(conv, "user", "tool_result")]


def test_unknown_and_none_are_no_conversation(store):
    assert store.get(None) is None and store.get("nope") is None


def test_a_started_conversation_is_found_and_has_its_own_id(store):
    a, b = store.start("S", [{"name": "t"}]), store.start("S", [])
    assert store.get(a.id) is a and a.id != b.id and len(a.id) >= 16
    assert (a.system, a.tools, a.messages, a.pending) == ("S", [{"name": "t"}], [], None) and len(store) == 2


def test_idle_expiry_is_fifteen_minutes(store, clock):
    c = store.start("S", [])
    clock.advance(899)
    assert store.get(c.id) is c  # still there, and this use restarts the fifteen minutes
    clock.advance(899)
    assert store.get(c.id) is c
    clock.advance(901)
    assert store.get(c.id) is None and len(store) == 0


def test_expired_conversations_are_swept_without_being_asked_for(store, clock):
    store.start("S", [])
    clock.advance(901)
    fresh = store.start("S", [])
    assert len(store) == 1 and store.get(fresh.id) is fresh


def test_end_forgets(store):
    c = store.start("S", [])
    store.end(c.id)
    store.end("never-existed")
    assert store.get(c.id) is None


def test_trim_keeps_whole_recent_turns_and_drops_thinking(clock):
    store = ConversationStore(idle_seconds=900, max_turns=4, clock=clock)
    c = store.start("S", [])
    for i in range(5):
        c.messages += turn_with_tool(i)
    store.trim(c)
    assert [m["content"] for m in c.messages if isinstance(m["content"], str)] == ["q3", "q4"]
    assert c.messages[0]["role"] == "user"
    assert blocks(c, "assistant", "thinking") == [] and blocks(c, "assistant", "redacted_thinking") == []
    assert tool_use_ids(c) == tool_result_ids(c) == ["t3", "t4"]  # no orphaned pair
    assert [b["text"] for b in blocks(c, "assistant", "text")] == ["a3", "a4"]


def test_trim_leaves_a_conversation_at_the_cap_untouched(clock):
    store = ConversationStore(idle_seconds=900, max_turns=4, clock=clock)
    c = store.start("S", [])
    for i in range(4):
        c.messages += turn_with_tool(i)
    before = copy.deepcopy(c.messages)
    store.trim(c)
    assert c.messages == before  # thinking blocks included: nothing earlier changed, so they stay


def test_a_question_with_extra_text_blocks_still_starts_a_turn(clock):
    # After a time limit the question's content is a list of text blocks, not a string.
    store = ConversationStore(idle_seconds=900, max_turns=2, clock=clock)
    c = store.start("S", [])
    for i in range(3):
        turn = turn_with_tool(i)
        turn[0]["content"] = [{"type": "text", "text": f"q{i}"}, {"type": "text", "text": "[relay] note"}]
        c.messages += turn
    store.trim(c)
    assert [
        m["content"][0]["text"] for m in c.messages if m["content"][0]["type"] == "text" and m["role"] == "user"
    ] == ["q2"]


def test_trim_never_leaves_an_assistant_message_empty(clock):
    store = ConversationStore(idle_seconds=900, max_turns=2, clock=clock)
    c = store.start("S", [])
    for i in range(3):
        c.messages += [{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": [dict(THINKING)]}]
    store.trim(c)
    assert all(m["content"] for m in c.messages) and len(c.messages) == 2


async def test_ledger_replays_for_ten_minutes(ledger, clock):
    runs = []

    async def fn():
        runs.append(1)
        return Stored(200, {"reply": "hi"})

    assert await ledger.run_once("t1", fn) == await ledger.run_once("t1", fn) == Stored(200, {"reply": "hi"})
    assert len(runs) == 1
    clock.advance(599)
    await ledger.run_once("t1", fn)
    assert len(runs) == 1
    clock.advance(2)
    await ledger.run_once("t1", fn)
    assert len(runs) == 2


async def test_ledger_keeps_turn_ids_apart(ledger):
    async def one():
        return Stored(200, {"reply": "one"})

    async def two():
        return Stored(200, {"reply": "two"})

    assert (await ledger.run_once("t1", one)).body == {"reply": "one"}
    assert (await ledger.run_once("t2", two)).body == {"reply": "two"}


async def test_ledger_joins_a_turn_that_is_still_running(ledger):
    gate, runs, results = anyio.Event(), [], []

    async def slow():
        runs.append(1)
        await gate.wait()
        return Stored(200, {"reply": "hi"})

    async def call():
        results.append(await ledger.run_once("t1", slow))

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
        await anyio.sleep(0.01)
        gate.set()
    assert len(runs) == 1 and results[0] == results[1] == Stored(200, {"reply": "hi"})


async def test_ledger_does_not_keep_what_asked_not_to_be_kept(ledger):
    runs = []

    async def fn():
        runs.append(1)
        return Stored(502, {"error": {}}, keep=False)

    assert (await ledger.run_once("t1", fn)).status == 502
    await ledger.run_once("t1", fn)
    assert len(runs) == 2


async def test_a_crash_reaches_every_waiter_and_is_not_stored(ledger):
    gate, runs, errors = anyio.Event(), [], []

    async def crash():
        runs.append(1)
        await gate.wait()
        raise RuntimeError("boom")

    async def call():
        try:
            await ledger.run_once("t1", crash)
        except RuntimeError as exc:
            errors.append(str(exc))

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
        await anyio.sleep(0.01)
        gate.set()
    assert runs == [1] and errors == ["boom", "boom"]

    async def fine():
        return Stored(200, {"reply": "recovered"})

    assert (await ledger.run_once("t1", fine)).body == {"reply": "recovered"}


def test_start_records_caller_and_profile(store):
    office = ProfileConfig(name="office", gated="allow")
    conversation = store.start("sys", [], caller="speaker", profile=office)
    assert (conversation.caller, conversation.profile) == ("speaker", office)
    assert (store.start("sys", []).caller, store.start("sys", []).profile) == ("", None)


def test_get_with_an_owner_hides_other_callers(store, clock):
    mine = store.start("sys", [], caller="phone")
    clock.advance(60)
    assert store.get(mine.id, owner="speaker") is None
    assert mine.last_used == 1000.0  # someone else asking for it does not keep it alive
    assert store.get(mine.id, owner="phone") is mine and mine.last_used == 1060.0
    assert store.get(mine.id) is mine  # no owner given: as before


def test_end_with_an_owner_leaves_other_callers_alone(store):
    mine = store.start("sys", [], caller="phone")
    store.end(mine.id, owner="speaker")
    assert store.get(mine.id) is mine
    store.end(mine.id, owner="phone")
    assert store.get(mine.id) is None
