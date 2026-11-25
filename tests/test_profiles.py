"""The roster: which caller a verified token is, and which profile a turn runs under."""

from hey_claude_relay.config import AccessConfig, CallerConfig, ProfileConfig, RelayConfig
from hey_claude_relay.profiles import EVERYTHING, Roster

ACCESS = AccessConfig(team_domain="example.cloudflareaccess.com", audiences=("aud-1",))
PERSONAL, OFFICE, SHARED = (ProfileConfig(name=n) for n in ("personal", "office", "shared"))
ROOMS = RelayConfig(
    access=ACCESS,
    caller=(
        CallerConfig(name="phone", token="phone.access", profiles=("personal",)),
        CallerConfig(name="speaker", token="speaker.access", profiles=("office", "shared")),
    ),
    profile=(PERSONAL, OFFICE, SHARED),
)


def test_no_callers_no_profiles_is_one_caller_with_everything():
    roster = Roster(RelayConfig())
    caller = roster.caller("local")
    assert caller is not None and caller.name == "local" and roster.implicit == caller
    assert roster.choose(caller, None) is EVERYTHING
    assert roster.choose(caller, "-") is None and roster.choose(caller, "office") is None


def test_service_tokens_are_one_caller():
    roster = Roster(RelayConfig(access=ACCESS.model_copy(update={"service_tokens": ("a.access", "b.access")})))
    a, b = roster.caller("a.access"), roster.caller("b.access")
    assert a is not None and a == b and a.name == "default" and roster.implicit == a
    assert roster.choose(a, None) is EVERYTHING and roster.choose(a, "personal") is None


def test_a_caller_gets_its_own_profiles_first_is_default():
    roster = Roster(ROOMS)
    speaker = roster.caller("speaker.access")
    assert speaker is not None and speaker.name == "speaker" and roster.implicit is None
    assert roster.choose(speaker, None) == OFFICE
    assert roster.choose(speaker, "shared") == SHARED
    assert roster.choose(speaker, "personal") is None  # another caller's profile: not this one's to claim
    assert roster.choose(speaker, "-") is None and roster.choose(speaker, "kitchen") is None


def test_an_unknown_token_is_no_caller():
    assert Roster(ROOMS).caller("stranger.access") is None
    assert Roster(ROOMS).caller("local") is None  # with Access in front, "local" is not a token


def test_local_may_use_every_profile():
    roster = Roster(RelayConfig(profile=(OFFICE, SHARED)))
    local = roster.caller("local")
    assert local is not None and roster.choose(local, None) == OFFICE and roster.choose(local, "shared") == SHARED


def test_callers_without_profiles_get_everything():
    cfg = RelayConfig(access=ACCESS, caller=(CallerConfig(name="phone", token="phone.access"),))
    roster = Roster(cfg)
    phone = roster.caller("phone.access")
    assert phone is not None and roster.choose(phone, None) is EVERYTHING and roster.choose(phone, "-") is None


def test_everything_has_no_limits():
    assert (EVERYTHING.servers, EVERYTHING.tools, EVERYTHING.allow_services, EVERYTHING.gated) == (
        None,
        None,
        (),
        "allow",
    )
