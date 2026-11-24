"""What the driver says after "Say your passphrase." goes nowhere: not to Claude, not into the
stored conversation, not into any answer, not into any log line at any level."""

import json
import logging

import pytest

from conftest import PHRASE, gated
from fakes import say, use
from hey_claude_relay.gates import normalize_passphrase

ASK_OPEN = use(
    ("house__ha_call_service", {"domain": "cover", "service": "open_cover", "entity_id": "cover.garage_door"})
)
WRONG = "purple monkey dishwasher eleven"
# (what happens, what is said, whether the client marks the turn secret)
CASES = [
    ("match", PHRASE, True),
    ("match", PHRASE, False),
    ("match", "Open, Sesame 42!", True),
    ("miss", WRONG, True),
    ("miss", WRONG, False),
    ("late", WRONG, True),
    ("late", WRONG, False),
    ("late", PHRASE, False),
    ("cancel-then-say-it", WRONG, True),
    ("lost", WRONG, True),
    ("locked", WRONG, False),
]


@pytest.mark.parametrize("case, said, flagged", CASES)
async def test_the_attempt_reaches_neither_claude_nor_the_conversation_nor_any_log(case, said, flagged, caplog):
    words = normalize_passphrase(said).split()
    needles = {said, normalize_passphrase(said), " ".join(words[:2]), " ".join(words[-2:])}
    caplog.set_level(logging.DEBUG)  # the root logger: every library, every level
    async with gated(ASK_OPEN, say("Opening."), say("Anything else?"), say("Sure.")) as (r, h):
        first = (await r.turn("open the garage")).json()
        cid = first["conversation_id"]
        if case == "late":
            r.clock.advance(31)
        if case == "lost":
            r.app.state.conversations.get(cid).pending = None  # the relay forgot the hold
        if case == "locked":
            for _ in range(3):
                r.app.state.keeper.miss()
        resp = await r.turn(said, conversation_id=cid, turn_id="secret-turn", secret=flagged)
        assert resp.status_code == 200
        await r.turn("thanks", conversation_id=cid)  # a later turn replays stored history to Claude
        replay = await r.turn(said, conversation_id=cid, turn_id="secret-turn", secret=flagged)
        conversation = r.app.state.conversations.get(cid)
        haystacks = {
            "sent to Claude": r.claude.sent(),
            "stored conversation": json.dumps(conversation.messages, default=str) + repr(conversation),
            "answer": resp.text,
            "replayed answer": replay.text,
            "log": "\n".join(f"{x.getMessage()} {x.args!r} {x.exc_text or ''}" for x in caplog.records),
        }
    assert caplog.records, "nothing was logged at all: the test is not looking at the log"
    for needle in needles:
        for where, hay in haystacks.items():
            assert needle.casefold() not in hay.casefold(), f"{needle!r} found in the {where}"


async def test_an_attempt_the_relay_rejects_as_malformed_is_not_logged_either(caplog):
    caplog.set_level(logging.DEBUG)
    async with gated(ASK_OPEN) as (r, h):
        cid = (await r.turn("open the garage")).json()["conversation_id"]
        too_long = "open sesame forty two " * 200
        resp = await r.turn(too_long, conversation_id=cid, secret=True)
        assert resp.status_code == 400 and r.app.state.conversations.get(cid).pending is not None
    assert "sesame" not in caplog.text and "sesame" not in resp.text


async def test_an_attempt_in_a_body_of_the_wrong_shape_is_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    async with gated(ASK_OPEN) as (r, h):
        await r.turn("open the garage")
        body = {
            "turn_id": "t",
            "text": "open sesame forty two",
            "secret": "yes",
        }  # a validation error carries its input
        resp = await r.http.post("/v1/turn", json=body)
    assert resp.status_code == 400 and "sesame" not in caplog.text and "sesame" not in resp.text
