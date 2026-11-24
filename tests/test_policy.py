import pytest

from conftest import HOUSE, HOUSE_POLICY, PHRASE_HASH, load_toml
from hey_claude_relay.config import ConfigError, GateConfig, GateMatch, ServerConfig
from hey_claude_relay.policy import DENIED, UNREADABLE, Allow, Hold, Policy, Refuse, ToolCall

HOUSE_CFG = ServerConfig(
    name="house",
    url="http://127.0.0.1:9003/mcp",
    auth="none",
    tools=("ha_search", "ha_get_state", "ha_call_service"),
    service_from={"ha_call_service": "{domain}.{service}"},
    allow_services=("light.*", "cover.open_cover", "cover.close_cover"),
    deny_services=("lock.unlock", "alarm_control_panel.alarm_disarm"),
)
GATE = GateConfig(
    server="house",
    match=GateMatch(tool="ha_call_service", service="cover.open_cover"),
    require=("near-home", "passphrase"),
)
P = Policy([HOUSE_CFG], [GATE])
DENY_ONLY = Policy([HOUSE_CFG.model_copy(update={"allow_services": ()})], [])


def call(**arguments):
    return ToolCall("house", "ha_call_service", arguments)


def test_plain_service_is_allowed():
    assert P.decide(call(domain="light", service="turn_on", entity_id="light.kitchen")) == Allow()
    assert P.service_of(call(domain="light", service="turn_on")) == "light.turn_on"


def test_denied_service_is_refused():
    refusal = P.decide(call(domain="lock", service="unlock"))
    assert refusal == Refuse(DENIED.format(what="lock.unlock"))
    assert refusal.message == (
        "Refused by the relay: lock.unlock is not allowed by voice. Tell the driver plainly and do not try another way."
    )


def test_deny_wins_over_allow():
    p = Policy([HOUSE_CFG.model_copy(update={"allow_services": ("*",)})], [])
    assert isinstance(p.decide(call(domain="lock", service="unlock")), Refuse)
    assert p.decide(call(domain="lock", service="lock")) == Allow()


OFF_THE_LIST = [
    dict(domain="cover", service="toggle"),
    dict(domain="lock", service="open"),
    dict(domain="homeassistant", service="turn_on", entity_id="cover.garage_door"),
    dict(domain="script", service="turn_on"),
    dict(domain="cover", service="set_cover_position", data={"position": 100}),
]


@pytest.mark.parametrize("arguments", OFF_THE_LIST, ids=lambda a: f"{a['domain']}.{a['service']}")
def test_a_service_off_the_allow_list_is_refused(arguments):
    assert isinstance(P.decide(call(**arguments)), Refuse)


def test_without_an_allow_list_only_the_deny_list_refuses():
    assert DENY_ONLY.decide(call(domain="cover", service="toggle")) == Allow()
    assert isinstance(DENY_ONLY.decide(call(domain="lock", service="unlock")), Refuse)


@pytest.mark.parametrize(
    "arguments",
    [
        dict(domain="LOCK", service="Unlock"),
        dict(domain=" lock", service="unlock "),
        dict(domain="Lock", service="UNLOCK"),
    ],
)
def test_letter_case_and_stray_spaces_do_not_get_around_a_rule(arguments):
    # A server may fold case before acting; the relay's rules must not be stricter readers than it is.
    assert isinstance(DENY_ONLY.decide(call(**arguments)), Refuse)
    assert isinstance(P.decide(call(**arguments)), Refuse)


UNREADABLE_CALLS = [
    dict(ws_command="call_service", data={"domain": "lock", "service": "unlock"}),
    dict(domain="cover"),
    dict(domain="cover", service=7),
    dict(domain="", service="open_cover"),
    dict(domain="cover", service=None),
    dict(domain=["lock"], service="unlock"),
    dict(domain="cover", service="   "),
    dict(),
]


@pytest.mark.parametrize("arguments", UNREADABLE_CALLS, ids=str)
def test_a_call_whose_service_cannot_be_read_is_refused(arguments):
    assert P.decide(call(**arguments)) == Refuse(UNREADABLE)
    assert DENY_ONLY.decide(call(**arguments)) == Refuse(UNREADABLE)


def test_gated_service_is_held():
    assert P.decide(call(domain="cover", service="open_cover", entity_id="cover.garage_door")) == Hold(GATE)
    assert P.decide(call(domain="Cover", service="OPEN_COVER")) == Hold(GATE)
    assert P.decide(call(domain="cover", service="close_cover")) == Allow()


def test_tools_without_a_service_pass():
    assert P.decide(ToolCall("house", "ha_get_state", {"entity_id": "light.kitchen"})) == Allow()
    assert P.service_of(ToolCall("house", "ha_get_state", {"entity_id": "light.kitchen"})) is None


def test_gate_on_a_whole_tool():
    gate = GateConfig(server="house", match=GateMatch(tool="ha_bulk_*"), require=("passphrase",))
    p = Policy([HOUSE_CFG], [gate])
    assert p.decide(ToolCall("house", "ha_bulk_control", {"operations": []})) == Hold(gate)
    assert p.decide(ToolCall("house", "ha_search", {"query": "x"})) == Allow()


def test_a_gate_belongs_to_its_server():
    other = HOUSE_CFG.model_copy(update={"name": "cabin"})
    p = Policy([HOUSE_CFG, other], [GATE])
    assert p.decide(ToolCall("cabin", "ha_call_service", {"domain": "cover", "service": "open_cover"})) == Allow()


def test_a_denied_service_is_refused_before_a_gate_is_considered():
    gate = GateConfig(server="house", match=GateMatch(tool="ha_call_service"), require=("passphrase",))
    p = Policy([HOUSE_CFG], [gate])
    assert isinstance(p.decide(call(domain="lock", service="unlock")), Refuse)
    assert p.decide(call(domain="light", service="turn_on")) == Hold(gate)


def test_a_server_the_policy_does_not_know_is_refused():
    assert isinstance(P.decide(ToolCall("elsewhere", "anything", {})), Refuse)


GATE_TOML = """
[[gate]]
server = "house"
match = { tool = "ha_call_service", service = "cover.open_cover" }
require = ["passphrase"]
"""


def test_policy_keys_are_read_from_config():
    cfg, _ = load_toml(HOUSE_POLICY + GATE_TOML, {"RELAY_PASSPHRASE_HASH": PHRASE_HASH})
    server, gate = cfg.server[0], cfg.gate[0]
    assert server.service_from == {"ha_call_service": "{domain}.{service}"}
    assert server.deny_services == ("lock.unlock", "alarm_control_panel.alarm_disarm")
    assert (gate.server, gate.match.tool, gate.match.service, gate.require) == (
        "house",
        "ha_call_service",
        "cover.open_cover",
        ("passphrase",),
    )


def test_config_refuses_a_gate_the_relay_could_not_enforce():
    with pytest.raises(ConfigError, match="service_from"):
        load_toml(HOUSE + GATE_TOML)  # match.service, but this server says nothing about services


def test_config_refuses_deny_services_without_service_from():
    with pytest.raises(ConfigError, match="service_from"):
        load_toml(HOUSE + 'deny_services = ["lock.unlock"]\n')
    with pytest.raises(ConfigError, match="service_from"):
        load_toml(HOUSE + 'allow_services = ["light.*"]\n')


@pytest.mark.parametrize(
    "template", ["{data[domain]}.{service}", "{0}.{1}", "{domain!r}.{service}", "{}", "no fields", "{domain.x}"]
)
def test_config_refuses_a_template_it_could_not_read_safely(template):
    with pytest.raises(ConfigError, match="service_from"):
        load_toml(HOUSE + f"service_from = {{ ha_call_service = '{template}' }}\n")


def test_config_refuses_a_gate_on_an_unknown_server():
    with pytest.raises(ConfigError, match="garage"):
        load_toml(HOUSE_POLICY + GATE_TOML.replace('server = "house"', 'server = "garage"'))


@pytest.mark.parametrize("require", ["[]", '["fingerprint"]', '["passphrase", "passphrase"]'])
def test_config_refuses_a_gate_with_no_or_unknown_checks(require):
    with pytest.raises(ConfigError, match="require"):
        load_toml(HOUSE_POLICY + GATE_TOML.replace('require = ["passphrase"]', f"require = {require}"))


def test_a_misspelled_policy_key_stops_startup():
    with pytest.raises(ConfigError, match="deny_service"):
        load_toml(HOUSE_POLICY + 'deny_service = ["lock.unlock"]\n')
