"""[[caller]] and [[profile]]: who may call, and the narrower rule sets they may choose from."""

import pytest

from conftest import GATED, HOUSE_POLICY, PHRASE_HASH
from hey_claude_relay.config import ConfigError, load_config

ACCESS = """
[access]
team_domain = "example.cloudflareaccess.com"
audiences = ["aud-1"]
"""
CALLERS = """
[[caller]]
name = "phone"
token = "phone-token-client-id.access"
profiles = ["personal"]

[[caller]]
name = "speaker"
token = "speaker-token-client-id.access"
profiles = ["office", "shared"]
"""
PROFILES = """
[[profile]]
name = "personal"

[[profile]]
name = "office"

[[profile]]
name = "shared"
servers = ["house"]
allow_services = ["light.*"]
"""
MATINEE = """
[[server]]
name = "matinee"
url = "http://127.0.0.1:9001/mcp"
auth = "none"
"""
ROOMS = ACCESS + CALLERS + PROFILES + MATINEE + HOUSE_POLICY


def load(tmp_path, text, env=None):
    p = tmp_path / "relay.toml"
    p.write_text(text)
    return load_config(p, {"RELAY_PASSPHRASE_HASH": PHRASE_HASH, **(env or {})})


def with_profile(extra: str, servers: str = MATINEE + HOUSE_POLICY) -> str:
    """A config whose only caller may use one profile, laid out as ``extra``."""
    caller = '\n[[caller]]\nname = "phone"\ntoken = "phone-token-client-id.access"\nprofiles = ["p"]\n'
    return ACCESS + caller + '\n[[profile]]\nname = "p"\n' + extra + "\n" + servers


def test_callers_and_profiles_load(tmp_path):
    cfg, _ = load(tmp_path, ROOMS)
    assert [c.name for c in cfg.caller] == ["phone", "speaker"]
    assert cfg.caller[1].profiles == ("office", "shared")
    assert [p.name for p in cfg.profile] == ["personal", "office", "shared"]
    shared = cfg.profile[2]
    assert shared.servers == ("house",) and shared.allow_services == ("light.*",) and shared.tools is None
    assert cfg.profile[0].servers is None and cfg.profile[0].gated is None
    assert cfg.access is not None and cfg.access.service_tokens == ()
    assert "speaker-token-client-id" not in repr(cfg)  # who is allowed in is not for logs


def test_no_callers_and_no_profiles_is_todays_config(tmp_path):
    cfg, _ = load(tmp_path, ACCESS + 'service_tokens = ["a.access"]\n')
    assert cfg.caller == () and cfg.profile == ()


def test_a_local_relay_may_have_profiles_without_callers(tmp_path):
    cfg, _ = load(tmp_path, '[[profile]]\nname = "home"\n[[profile]]\nname = "shared"\n')
    assert [p.name for p in cfg.profile] == ["home", "shared"]


def test_gated_may_be_left_out_when_there_are_no_gates(tmp_path):
    cfg, _ = load(tmp_path, ROOMS)
    assert all(p.gated is None for p in cfg.profile)


def test_gated_is_kept_when_given(tmp_path):
    cfg, _ = load(tmp_path, with_profile('gated = "refuse"', GATED))
    assert cfg.profile[0].gated == "refuse"


def test_profile_prompt_file_is_relative_to_the_config(tmp_path):
    cfg, _ = load(tmp_path, with_profile('prompt_file = "shared.md"'))
    assert cfg.profile[0].prompt_file == tmp_path / "shared.md"


@pytest.mark.parametrize(
    "allowed",
    [
        ["light.*"],
        ["light.turn_on"],
        ["cover.close_cover"],
        ["LIGHT.*", " light.turn_off "],
    ],
)
def test_narrowing_service_patterns_are_accepted(tmp_path, allowed):
    patterns = ", ".join(f'"{p}"' for p in allowed)
    cfg, _ = load(tmp_path, with_profile(f"allow_services = [{patterns}]"))
    assert cfg.profile[0].allow_services == tuple(allowed)


def test_narrowing_tool_patterns_are_accepted(tmp_path):
    cfg, _ = load(tmp_path, with_profile('servers = ["house"]\ntools = ["house.ha_get_state", "house.ha_search*"]'))
    assert cfg.profile[0].tools == ("house.ha_get_state", "house.ha_search*")


CALLER_TOKEN = "phone-token-client-id.access"
BAD = [
    # (what is wrong, the whole relay.toml, a key the message must name)
    ("a caller with no [access]", CALLERS + PROFILES + MATINEE + HOUSE_POLICY, "caller"),
    (
        "service_tokens and callers both",
        ACCESS + 'service_tokens = ["a.access"]\n' + CALLERS + PROFILES + MATINEE + HOUSE_POLICY,
        "service_tokens",
    ),
    ("nobody admitted", ACCESS + MATINEE, "service_tokens"),
    (
        "profiles with service_tokens and no callers",
        ACCESS + 'service_tokens = ["a.access"]\n' + PROFILES + MATINEE + HOUSE_POLICY,
        "caller",
    ),
    (
        "two callers with one name",
        ACCESS + CALLERS.replace('name = "speaker"', 'name = "phone"') + PROFILES + MATINEE + HOUSE_POLICY,
        "caller",
    ),
    (
        "two callers with one token",
        ACCESS
        + CALLERS.replace("speaker-token-client-id", "phone-token-client-id")
        + PROFILES
        + MATINEE
        + HOUSE_POLICY,
        "token",
    ),
    (
        "two profiles with one name",
        ROOMS.replace('name = "office"', 'name = "personal"').replace('"office", ', ""),
        "profile",
    ),
    ("a caller naming an unknown profile", ROOMS.replace('["personal"]', '["kitchen"]'), "profiles"),
    ("a caller with no profiles when profiles exist", ROOMS.replace('["personal"]', "[]"), "profiles"),
    (
        "a caller with profiles when none exist",
        ACCESS + CALLERS + MATINEE + HOUSE_POLICY,
        "profiles",
    ),
    ("a caller naming a profile twice", ROOMS.replace('["office", "shared"]', '["office", "office"]'), "profiles"),
    ("a profile without gated when a gate exists", with_profile("", GATED), "gated"),
    ("an unknown server", with_profile('servers = ["nowhere"]'), "servers"),
    (
        "a tool on a server the profile leaves out",
        with_profile('servers = ["house"]\ntools = ["matinee.search"]'),
        "tools",
    ),
    ("a glob for the server part of a tool", with_profile('tools = ["*.search"]'), "tools"),
    ("a tool with no server part", with_profile('tools = ["search"]'), "tools"),
    (
        "allow_services on servers that read no service",
        with_profile('servers = ["matinee"]\nallow_services = ["light.*"]'),
        "allow_services",
    ),
    ("a wider domain", with_profile('allow_services = ["lock.*"]'), "allow_services"),
    ("everything", with_profile('allow_services = ["*"]'), "allow_services"),
    ("a denied service", with_profile('allow_services = ["lock.unlock"]'), "allow_services"),
    ("a domain the server never allows", with_profile('allow_services = ["fan.*"]'), "allow_services"),
    ("an upper-case name", ROOMS.replace('name = "office"', 'name = "Office"'), "name"),
    ("a name with a space", ROOMS.replace('name = "office"', 'name = "a b"'), "name"),
]


@pytest.mark.parametrize("what, text, key", BAD, ids=[case[0] for case in BAD])
def test_a_bad_caller_or_profile_stops_startup(tmp_path, what, text, key):
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, text)
    message = str(caught.value)
    assert key in message, message
    assert CALLER_TOKEN not in message and "speaker-token-client-id" not in message


def test_example_config_has_callers_and_profiles():
    from pathlib import Path

    from conftest import EXAMPLE_ENV

    cfg, _ = load_config(Path("relay.example.toml"), EXAMPLE_ENV)
    assert [c.name for c in cfg.caller] == ["phone", "home-assistant"]
    assert cfg.caller[1].profiles == ("office", "shared")
    assert [p.name for p in cfg.profile] == ["personal", "office", "shared"]
    shared = cfg.profile[2]
    assert shared.gated == "refuse" and shared.servers == ("cookbooks", "matinee", "home-assistant")
    assert shared.allow_services == ("light.*", "fan.*", "media_player.*")
