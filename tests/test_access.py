import time

import jwt
import pytest

from hey_claude_relay.access import AccessDenied, AccessPolicy, AccessVerifier
from hey_claude_relay.config import AccessConfig
from keys import AUD, CALLER, OTHER_KEY, PRIVATE_KEY, TEAM, mint, resolver

POLICY = AccessPolicy(TEAM, (AUD,), frozenset({CALLER}))
V = AccessVerifier(POLICY, resolver)


async def test_listed_service_token_is_admitted():
    assert V.caller(await V.verify(mint())) == "phone.access"


@pytest.mark.parametrize(
    "bad", [dict(aud="other"), dict(iss="https://evil.example"), dict(expires_in=-120), dict(key=OTHER_KEY)]
)
async def test_assertion_that_does_not_check_out_is_denied(bad):
    with pytest.raises(AccessDenied):
        await V.verify(mint(**bad))


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud"])
async def test_an_assertion_missing_a_required_claim_is_denied(claim):
    with pytest.raises(AccessDenied):
        await V.verify(mint(drop=(claim,)))


async def test_a_little_clock_skew_is_tolerated():
    await V.verify(mint(expires_in=-10))  # expired 10 s ago: inside the 30 s leeway


async def test_unsigned_token_is_denied():
    with pytest.raises(AccessDenied):
        await V.verify(jwt.encode({"aud": [AUD], "iss": f"https://{TEAM}"}, "", algorithm="none"))


async def test_a_token_signed_with_a_shared_secret_is_denied():
    forged = jwt.encode({"aud": [AUD], "iss": f"https://{TEAM}", "common_name": CALLER}, "g" * 32, algorithm="HS256")
    with pytest.raises(AccessDenied):
        await V.verify(forged)


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c"])
async def test_garbage_is_denied_not_an_error(garbage):
    with pytest.raises(AccessDenied):
        await V.verify(garbage)


async def test_unlisted_service_token_is_denied():
    with pytest.raises(AccessDenied):
        V.caller(await V.verify(mint(common_name="stranger.access")))


async def test_a_person_is_not_a_caller():
    # A browser login has an email and no listed common_name. The relay admits service tokens only.
    with pytest.raises(AccessDenied):
        V.caller(await V.verify(mint(common_name=None, email="user@example.com")))


@pytest.mark.parametrize("name", ["", ["phone.access"], {"phone.access": 1}, 7, True])
async def test_a_common_name_that_is_not_a_listed_string_is_denied(name):
    with pytest.raises(AccessDenied):
        V.caller(await V.verify(mint(common_name=name)))


async def test_a_login_with_an_email_is_refused_even_with_a_listed_common_name():
    with pytest.raises(AccessDenied, match="person"):
        V.caller(await V.verify(mint(email="user@example.com")))


@pytest.mark.parametrize("failure", [ValueError("Expecting value: line 1 column 1"), OSError("connection reset")])
async def test_a_broken_certs_endpoint_is_a_denial_not_a_crash(failure):
    async def broken(token):
        raise failure

    with pytest.raises(AccessDenied, match="signing keys"):
        await AccessVerifier(POLICY, broken).verify(mint())


def test_policy_urls_and_repr():
    assert POLICY.issuer == f"https://{TEAM}"
    assert POLICY.certs_url == f"https://{TEAM}/cdn-cgi/access/certs"
    assert CALLER not in repr(POLICY)  # who is allowed in is not for logs


def test_policy_from_config():
    cfg = AccessConfig(team_domain=TEAM, audiences=("aud-1", "aud-2"), service_tokens=("a.access", "b.access"))
    assert AccessPolicy.from_config(cfg) == AccessPolicy(TEAM, ("aud-1", "aud-2"), frozenset({"a.access", "b.access"}))


async def test_unknown_key_id_refetches_at_most_once_a_minute(monkeypatch):
    # Ported from mcp-server-shared: a burst of tokens naming keys we don't have must not turn
    # into one request to Cloudflare each.
    class FakeKey:
        def __init__(self, key_id):
            self.key_id = key_id

    class FakeJWKSet:
        keys = [FakeKey("known-kid")]

    class FakeCache:
        def is_expired(self):
            return False

        def get(self):
            return FakeJWKSet()

    calls = 0

    class FakePyJWKClient:
        def __init__(self, *args, **kwargs):
            self.jwk_set_cache = FakeCache()

        def fetch_data(self):
            nonlocal calls
            calls += 1
            return {"keys": []}

        def get_signing_key_from_jwt(self, _token):
            self.fetch_data()  # as PyJWKClient does on an unknown kid
            raise jwt.PyJWKClientError("unknown kid")

    monkeypatch.setattr(jwt, "PyJWKClient", FakePyJWKClient)
    fake_now = [5.0]  # near zero, as on a freshly booted machine: the first fetch must not be throttled
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])
    verifier = AccessVerifier(POLICY)

    with pytest.raises(AccessDenied, match="unknown kid"):
        await verifier.verify(mint(kid="unknown-1"))
    assert calls == 1

    fake_now[0] = 10.0  # within the minute: refused without a fetch
    with pytest.raises(AccessDenied, match="Unable to find a signing key"):
        await verifier.verify(mint(kid="unknown-2"))
    assert calls == 1

    fake_now[0] = 70.0  # the minute has passed: one more fetch is allowed
    with pytest.raises(AccessDenied, match="unknown kid"):
        await verifier.verify(mint(kid="unknown-3"))
    assert calls == 2


async def test_key_rotation_is_picked_up_while_the_old_key_is_in_use(monkeypatch):
    # The real PyJWKClient with only its HTTP request faked. Tokens served from the cache must not
    # count as fetches, or steady traffic on the old key would keep a rotated key refused.
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key(), as_dict=True)
    published = [{**jwk, "kid": "old-kid", "use": "sig"}]
    fetches = 0

    def fetch_data(self):
        nonlocal fetches
        fetches += 1
        return {"keys": list(published)}

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch_data)
    fake_now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])
    verifier = AccessVerifier(POLICY)

    await verifier.verify(mint(kid="old-kid"))  # empty cache: one fetch
    assert fetches == 1
    fake_now[0] = 1100.0
    await verifier.verify(mint(kid="old-kid"))  # a cache hit: no fetch
    assert fetches == 1
    published.append({**jwk, "kid": "new-kid", "use": "sig"})
    fake_now[0] = 1110.0  # 10 s after the last cache hit, 110 s after the last fetch: fetched, not throttled
    assert V.caller(await verifier.verify(mint(kid="new-kid"))) == CALLER
    assert fetches == 2
