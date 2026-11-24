import pytest

from conftest import HOUSE_POLICY, load_toml
from hey_claude_relay import gates
from hey_claude_relay.config import ConfigError
from hey_claude_relay.gates import (
    CANCEL_WORDS,
    check_passphrase_hash,
    hash_passphrase,
    normalize_passphrase,
    verify_passphrase,
)

HEARD = [
    "open sesame forty two",
    "Open, Sesame 42!",
    "open  sesame forty-two.",
    "OPEN SESAME FORTY TWO",
    "open sesame 42",
    "Open sesame, forty two",
    "  open\tsesame\nforty   two  ",
    "“Open sesame forty-two.”",
    "open sesame forty‑two",
]


@pytest.mark.parametrize("heard", HEARD)
def test_speech_to_text_variants_normalise_alike(heard):
    assert normalize_passphrase(heard) == "open sesame forty two"


@pytest.mark.parametrize(
    "raw, norm",
    [
        ("7", "seven"),
        ("0", "zero"),
        ("19", "nineteen"),
        ("40", "forty"),
        ("105", "one hundred five"),
        ("2026", "two thousand twenty six"),
        ("1,000", "one thousand"),
        ("999999", "nine hundred ninety nine thousand nine hundred ninety nine"),
        ("007", "zero zero seven"),
        ("1234567", "one two three four five six seven"),
        ("room 4b", "room four b"),
        ("don't stop", "dont stop"),
        ("don’t stop", "dont stop"),
        ("crème brûlée", "crème brûlée"),
        ("", ""),
        ("?!...", ""),
    ],
)
def test_numbers_become_words(raw, norm):
    assert normalize_passphrase(raw) == norm


def test_hash_round_trip_and_salt():
    a, b = hash_passphrase("open sesame 42"), hash_passphrase("open sesame 42")
    assert a != b and a.startswith("scrypt:16384:8:1:") and "$" not in a and " " not in a and "=" not in a
    assert verify_passphrase("Open Sesame forty-two", a) and verify_passphrase("open sesame 42", b)
    assert not verify_passphrase("open sesame forty three", a)
    assert not verify_passphrase("open sesame", a) and not verify_passphrase("", a)


def test_a_fixed_salt_gives_a_fixed_hash():
    salt = bytes(range(16))
    assert hash_passphrase("open sesame 42", salt=salt) == hash_passphrase("Open, sesame forty-two!", salt=salt)


GARBAGE = [
    "",
    "not-a-hash",
    "scrypt:16384:8:1",
    "scrypt:16384:8:1:AAAA:BBBB",
    "bcrypt:16384:8:1:AAAAAAAAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "scrypt:x:8:1:AAAAAAAAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "scrypt:16385:8:1:AAAAAAAAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "scrypt:1048576:8:1:AAAAAAAAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "scrypt:16384:8:1:!!!!:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
]


@pytest.mark.parametrize("encoded", GARBAGE)
def test_verify_never_raises_on_garbage(encoded):
    assert verify_passphrase("open sesame forty two", encoded) is False
    with pytest.raises(ConfigError, match="RELAY_PASSPHRASE_HASH"):
        check_passphrase_hash(encoded)


def test_a_good_hash_passes_the_check():
    check_passphrase_hash(hash_passphrase("open sesame 42"))


def test_verify_compares_in_constant_time(monkeypatch):
    calls = []
    real = gates.hmac.compare_digest

    def counting(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(gates.hmac, "compare_digest", counting)
    encoded = hash_passphrase("open sesame 42")
    assert verify_passphrase("open sesame 42", encoded) is True
    assert verify_passphrase("something else entirely", encoded) is False
    assert verify_passphrase("", encoded) is False
    assert calls == [(32, 32)] * 3  # one comparison each, match or miss, always of two full keys


def test_cancel_words_are_normalised_words():
    assert frozenset({"cancel", "never mind", "nevermind", "stop", "no"}) == CANCEL_WORDS
    assert all(normalize_passphrase(word) == word for word in CANCEL_WORDS)


GATE = """
[[gate]]
server = "house"
match = { tool = "ha_call_service", service = "cover.open_cover" }
require = ["passphrase"]
"""


def test_passphrase_gate_needs_the_hash_in_the_environment():
    with pytest.raises(ConfigError, match="RELAY_PASSPHRASE_HASH"):
        load_toml(HOUSE_POLICY + GATE)
    with pytest.raises(ConfigError, match="RELAY_PASSPHRASE_HASH"):
        load_toml(HOUSE_POLICY + GATE, {"RELAY_PASSPHRASE_HASH": "not-a-hash"})
    _, secrets = load_toml(HOUSE_POLICY + GATE, {"RELAY_PASSPHRASE_HASH": hash_passphrase("open sesame 42")})
    assert secrets.passphrase_hash.startswith("scrypt:")


def test_a_bad_hash_is_reported_without_being_repeated():
    with pytest.raises(ConfigError) as e:
        load_toml(HOUSE_POLICY + GATE, {"RELAY_PASSPHRASE_HASH": "my-actual-passphrase-pasted-by-mistake"})
    assert "my-actual-passphrase" not in str(e.value)


def test_no_gate_no_hash_needed():
    load_toml(HOUSE_POLICY)


def test_a_phrase_with_no_words_in_it_never_verifies():
    assert verify_passphrase("!!!", hash_passphrase("...")) is False


def test_a_hash_asking_for_more_memory_than_scrypt_is_given_is_refused_at_startup():
    heavy = "scrypt:131072:16:1:AAAAAAAAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    with pytest.raises(ConfigError, match="RELAY_PASSPHRASE_HASH"):
        check_passphrase_hash(heavy)
    assert verify_passphrase("open sesame", heavy) is False
