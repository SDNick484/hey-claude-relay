"""A profile narrows every policy decision and never widens one."""

from hey_claude_relay.config import ProfileConfig, ServerConfig
from hey_claude_relay.policy import DENIED, NO_PROMPT_HERE, NOT_HERE, Allow, Hold, Policy, Refuse, ToolCall
from test_policy import GATE, HOUSE_CFG, P, call

SHARED = ProfileConfig(name="shared", servers=("house",), gated="refuse", allow_services=("light.*",))
MOVIES = ServerConfig(name="matinee", url="http://127.0.0.1:9001/mcp", auth="none")
TWO = Policy([HOUSE_CFG, MOVIES], [GATE])


def test_a_profile_refuses_a_service_the_server_allows():
    refusal = P.decide(call(domain="cover", service="close_cover"), SHARED)
    assert refusal == Refuse(DENIED.format(what="cover.close_cover"))
    assert P.decide(call(domain="light", service="turn_on"), SHARED) == Allow()
    assert P.decide(ToolCall("house", "ha_get_state", {"entity_id": "light.a"}), SHARED) == Allow()


def test_a_profile_never_widens():
    # A profile built past the startup checks still cannot let through what the server refuses.
    wide = ProfileConfig.model_construct(name="wide", gated="allow", allow_services=("*",))
    assert isinstance(P.decide(call(domain="lock", service="unlock"), wide), Refuse)
    assert isinstance(P.decide(call(domain="fan", service="turn_on"), wide), Refuse)


def test_a_server_outside_the_profile_is_refused():
    assert TWO.decide(ToolCall("matinee", "search", {}), SHARED) == Refuse(NOT_HERE.format(what="matinee"))
    assert TWO.decide(ToolCall("matinee", "search", {})) == Allow()


def test_a_tool_outside_the_profile_tools_is_refused():
    lookups = ProfileConfig(name="lookups", tools=("house.ha_get_state", "matinee.*"), gated="allow")
    assert TWO.decide(ToolCall("house", "ha_get_state", {}), lookups) == Allow()
    assert TWO.decide(ToolCall("matinee", "search", {}), lookups) == Allow()
    refusal = TWO.decide(call(domain="light", service="turn_on"), lookups)
    assert refusal == Refuse(NOT_HERE.format(what="house.ha_call_service"))


def test_gated_refuse_refuses_without_holding():
    guest = ProfileConfig(name="guest", gated="refuse")
    decision = P.decide(call(domain="cover", service="open_cover", entity_id="cover.garage_door"), guest)
    assert decision == Refuse(NO_PROMPT_HERE.format(what="cover.open_cover"))
    assert "passphrase" in decision.message and "Refused by the relay" in decision.message


def test_gated_allow_holds_as_today():
    office = ProfileConfig(name="office", gated="allow")
    assert P.decide(call(domain="cover", service="open_cover"), office) == Hold(GATE)


def test_one_argument_decide_is_everything():
    assert P.decide(call(domain="cover", service="open_cover")) == Hold(GATE)
    assert P.decide(call(domain="cover", service="close_cover")) == Allow()
