import anyio
import pytest

from conftest import HOUSE, HOUSE_POLICY, PHRASE_HASH, load_toml, servers
from hey_claude_relay.config import ConfigError, GateSettings, NearHomeConfig
from hey_claude_relay.gates import GateKeeper, Lockouts, NearHome
from stubs import house_stub, sdk_stub

NEAR = NearHomeConfig(
    server="house",
    tool="ha_get_state",
    arguments={"entity_id": "person.example", "fields": ["state"]},
    result_path="data.state",
    states=("home",),
)


@pytest.fixture
def keeper(clock):
    return GateKeeper(GateSettings(), clock)


def test_the_defaults_are_the_spec_values():
    settings = GateSettings()
    assert (settings.hold_seconds, settings.max_misses, settings.lockout_minutes) == (30, 3, 15)


def test_three_misses_lock_for_fifteen_minutes(keeper, clock):
    assert [keeper.miss(), keeper.miss(), keeper.locked()] == [False, False, False]
    assert keeper.miss() is True and keeper.locked()
    clock.advance(899)
    assert keeper.locked()
    clock.advance(2)
    assert not keeper.locked()


def test_a_success_clears_earlier_misses(keeper):
    keeper.miss()
    keeper.miss()
    keeper.success()
    assert [keeper.miss(), keeper.miss()] == [False, False] and not keeper.locked()


def test_old_misses_are_forgotten(keeper, clock):
    keeper.miss()
    keeper.miss()
    clock.advance(901)
    assert keeper.miss() is False and not keeper.locked()


def test_lockout_starts_clean_afterwards(keeper, clock):
    for _ in range(3):
        keeper.miss()
    clock.advance(901)
    assert keeper.miss() is False and keeper.miss() is False and keeper.miss() is True


def test_a_miss_during_a_lockout_does_not_extend_it(keeper, clock):
    for _ in range(3):
        keeper.miss()
    clock.advance(600)
    assert keeper.miss() is False  # already locked: nothing new starts
    clock.advance(301)
    assert not keeper.locked()


def test_the_numbers_come_from_settings(clock):
    keeper = GateKeeper(GateSettings(max_misses=1, lockout_minutes=1), clock)
    assert keeper.miss() is True and keeper.locked()
    clock.advance(61)
    assert not keeper.locked()


@pytest.mark.parametrize("state, near", [("home", True), ("not_home", False), ("unknown", False), ("Home", False)])
async def test_near_home_reads_the_configured_path(state, near):
    async with house_stub(state=state) as h, servers(HOUSE, h).session() as s:
        assert await NearHome(NEAR).check(s, timeout=5) is near
    assert h.calls == [("ha_get_state", {"entity_id": "person.example", "fields": ["state"]})]


async def test_near_home_accepts_any_listed_state():
    wider = NEAR.model_copy(update={"states": ("home", "arriving")})
    async with house_stub(state="arriving") as h, servers(HOUSE, h).session() as s:
        assert await NearHome(wider).check(s, timeout=5) is True


UNREADABLE = [
    "not json",
    '{"data": {}}',
    '{"data": {"state": 5}}',
    "[]",
    '{"data": "home"}',
    '"home"',
    '{"state": "home"}',
    "",
]


@pytest.mark.parametrize("answer", UNREADABLE)
async def test_near_home_fails_closed_on_an_answer_it_cannot_read(answer):
    def ha_get_state(entity_id: str, fields: list[str] | None = None) -> str:
        return answer

    async with sdk_stub("house", {"ha_get_state": ha_get_state}) as h, servers(HOUSE, h).session() as s:
        assert await NearHome(NEAR).check(s, timeout=5) is False


async def test_near_home_fails_closed_when_the_tool_reports_an_error():
    from mcp.server.mcpserver.exceptions import ToolError

    def ha_get_state(entity_id: str, fields: list[str] | None = None) -> str:
        raise ToolError('{"data": {"state": "home"}}')  # even an error that looks like the right answer

    async with sdk_stub("house", {"ha_get_state": ha_get_state}) as h, servers(HOUSE, h).session() as s:
        assert await NearHome(NEAR).check(s, timeout=5) is False


async def test_near_home_fails_closed_when_the_server_is_down_or_slow():
    async with house_stub() as h:
        h.fail = True
        async with servers(HOUSE, h).session() as s:
            assert await NearHome(NEAR).check(s, timeout=5) is False

    async def slow(entity_id: str, fields: list[str] | None = None) -> str:
        await anyio.sleep(30)
        return '{"data": {"state": "home"}}'

    async with sdk_stub("house", {"ha_get_state": slow}) as h, servers(HOUSE, h).session() as s:
        assert await NearHome(NEAR).check(s, timeout=0.05) is False


GATE = """
[[gate]]
server = "house"
match = { tool = "ha_call_service", service = "cover.open_cover" }
require = ["near-home", "passphrase"]
"""
NEAR_TOML = """
[near_home]
server = "house"
tool = "ha_get_state"
arguments = { entity_id = "person.example", fields = ["state"] }
result_path = "data.state"
states = ["home"]
"""
ENV = {"RELAY_PASSPHRASE_HASH": PHRASE_HASH}


def test_gate_settings_and_near_home_are_read_from_config():
    cfg, _ = load_toml(
        HOUSE_POLICY + GATE + NEAR_TOML + "\n[gates]\nhold_seconds = 20\nmax_misses = 5\nlockout_minutes = 30\n", ENV
    )
    assert cfg.near_home == NEAR
    assert (cfg.gates.hold_seconds, cfg.gates.max_misses, cfg.gates.lockout_minutes) == (20, 5, 30)


def test_a_near_home_gate_needs_the_near_home_table():
    with pytest.raises(ConfigError, match="near_home"):
        load_toml(HOUSE_POLICY + GATE, ENV)


def test_near_home_must_name_a_configured_server():
    with pytest.raises(ConfigError, match="elsewhere"):
        load_toml(HOUSE_POLICY + GATE + NEAR_TOML.replace('server = "house"', 'server = "elsewhere"'), ENV)


@pytest.mark.parametrize("line", ['result_path = ""', "states = []", 'result_path = "data..state"', 'tool = ""'])
def test_near_home_values_that_could_never_match_are_refused(line):
    key = line.split(" ")[0]
    broken = "\n".join(line if row.startswith(key + " ") else row for row in NEAR_TOML.splitlines()) + "\n"
    with pytest.raises(ConfigError, match=key):
        load_toml(HOUSE_POLICY + GATE + broken, ENV)


@pytest.mark.parametrize("line", ["hold_seconds = 0", "max_misses = 0", "lockout_minutes = -1", "hold_seconds = inf"])
def test_gate_settings_that_make_no_sense_are_refused(line):
    with pytest.raises(ConfigError, match=line.split(" ")[0]):
        load_toml(HOUSE + "\n[gates]\n" + line + "\n")


def test_lockouts_are_per_caller(clock):
    lockouts = Lockouts(GateSettings(), clock)
    assert lockouts.of("phone") is lockouts.of("phone")
    for _ in range(3):
        lockouts.of("speaker").miss()
    assert lockouts.of("speaker").locked() and not lockouts.of("phone").locked()
    clock.advance(15 * 60 + 1)
    assert not lockouts.of("speaker").locked()
