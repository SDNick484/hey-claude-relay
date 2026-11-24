"""The checks a gate can ask for before a held call is allowed to run.

A gate is for stopping a misheard command, or text inside a tool result, from doing something
that matters: the check is code, and nothing Claude reads can argue with it. It is not
protection from someone who has heard the passphrase said in the car. The real credential is
the caller's Access token.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import time
import unicodedata
from collections.abc import Mapping

from .config import ConfigError, GateSettings, NearHomeConfig
from .conversations import Clock
from .mcp_client import TurnSession

# --- the spoken passphrase ----------------------------------------------------------------
# What the driver says reaches the relay as text from speech recognition, which is free with
# capitals, punctuation and whether a number comes as digits or words. So an attempt is
# normalised before it is hashed, and so is the phrase when the hash is made.

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_APOSTROPHES = str.maketrans("", "", "'’‘ʼ`")
_THOUSANDS_COMMA = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_DIGITS = re.compile(r"\d+")
_NOT_A_WORD_CHARACTER = re.compile(r"[^\w]|_")

# Said after "Say your passphrase.", these drop the held call without counting as a wrong guess.
CANCEL_WORDS = frozenset({"cancel", "never mind", "nevermind", "stop", "no"})


def _cardinal(number: int) -> str:
    """0 to 999,999 in words, the plain way: no "and", no hyphens."""
    if number < 20:
        return _ONES[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")
    if number < 1000:
        hundreds, rest = divmod(number, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {_cardinal(rest)}" if rest else "")
    thousands, rest = divmod(number, 1000)
    return f"{_cardinal(thousands)} thousand" + (f" {_cardinal(rest)}" if rest else "")


def _digits_as_words(match: re.Match[str]) -> str:
    digits = match.group()
    if len(digits) > 6 or (len(digits) > 1 and digits[0] == "0"):
        words = " ".join(_ONES[int(digit)] for digit in digits)  # a code or a long number: digit by digit
    else:
        words = _cardinal(int(digits))
    return f" {words} "


def normalize_passphrase(text: str) -> str:
    """``text`` as lower-case words separated by single spaces, with digits spelled out."""
    text = unicodedata.normalize("NFKC", text).casefold().translate(_APOSTROPHES)
    text = _THOUSANDS_COMMA.sub("", text)
    text = _DIGITS.sub(_digits_as_words, text)
    return " ".join(_NOT_A_WORD_CHARACTER.sub(" ", text).split())


# The hash is "scrypt:<n>:<r>:<p>:<salt>:<key>", salt and key in URL-safe base64 without padding.
# No "$" and no "=", so the line survives an env file unquoted. scrypt because a spoken phrase
# is short: if the hash leaks, guessing must stay slow.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_SALT_BYTES, _KEY_BYTES = 16, 32
_MAX_MEMORY = 128 * 1024 * 1024  # scrypt needs 128 * n * r bytes; more than this is refused too
_MAX_N = 2**17  # a hash asking for more work than this is refused: it would stall every attempt


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _derive(phrase: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    spoken = normalize_passphrase(phrase).encode()
    return hashlib.scrypt(spoken, salt=salt, n=n, r=r, p=p, dklen=length, maxmem=2 * _MAX_MEMORY)


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    """The parts of a hash line, or None if it is not one this relay could have made."""
    parts = encoded.strip().split(":")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = (int(part) for part in parts[1:4])
        salt, key = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, binascii.Error):
        return None
    sane = 2 <= n <= _MAX_N and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4
    if not sane or 128 * n * r > _MAX_MEMORY or len(salt) < 8 or len(key) < 16:
        return None
    return n, r, p, salt, key


def hash_passphrase(phrase: str, *, salt: bytes | None = None) -> str:
    """The line to put in RELAY_PASSPHRASE_HASH for ``phrase``. A fresh salt each time."""
    salt = os.urandom(_SALT_BYTES) if salt is None else salt
    key = _derive(phrase, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _KEY_BYTES)
    return f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}:{_b64(salt)}:{_b64(key)}"


def verify_passphrase(attempt: str, encoded: str) -> bool:
    """Whether ``attempt``, as heard, is the phrase behind ``encoded``. Never raises."""
    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, salt, key = parsed
    # Always derive and always compare whole keys, so a miss costs the same as a match.
    matched = hmac.compare_digest(_derive(attempt, salt, n, r, p, len(key)), key)
    return matched and bool(normalize_passphrase(attempt))  # a phrase with no words in it is never right


def check_passphrase_hash(encoded: str) -> None:
    """Raise ConfigError unless ``encoded`` is a hash line. The message never repeats the value."""
    if _parse(encoded) is None:
        raise ConfigError(
            "RELAY_PASSPHRASE_HASH is not a hash made by `hey-claude-relay hash-passphrase`. "
            "Run that command and put the line it prints in the environment."
        )


# --- wrong guesses ------------------------------------------------------------------------
class GateKeeper:
    """Counts wrong passphrases across every conversation, and locks all gated actions after too many.

    One for the whole relay, in memory: a restart clears it.
    """

    def __init__(self, settings: GateSettings, clock: Clock = time.monotonic) -> None:
        self._max = settings.max_misses
        self._window = settings.lockout_minutes * 60
        self._clock = clock
        self._misses: list[float] = []
        self._locked_until = float("-inf")

    def locked(self) -> bool:
        return self._clock() < self._locked_until

    def miss(self) -> bool:
        """Record a wrong passphrase. True when this one started a lockout."""
        now = self._clock()
        if now < self._locked_until:
            return False  # already locked; a guess made meanwhile neither counts nor extends it
        self._misses = [at for at in self._misses if now - at <= self._window] + [now]
        if len(self._misses) < self._max:
            return False
        self._misses = []
        self._locked_until = now + self._window
        return True

    def success(self) -> None:
        """The right passphrase: earlier wrong guesses are forgotten."""
        self._misses = []


# --- near home ----------------------------------------------------------------------------
class NearHome:
    """Asks a server, on the relay's own account, where the owner is. Anything short of a clear
    "near" is a no: an error, a timeout, an answer that cannot be read."""

    def __init__(self, cfg: NearHomeConfig) -> None:
        self.cfg = cfg

    async def check(self, session: TurnSession, *, timeout: float) -> bool:
        outcome = await session.call(self.cfg.server, self.cfg.tool, self.cfg.arguments, timeout=timeout)
        if outcome.is_error:
            return False
        try:
            value = json.loads(outcome.text)
        except ValueError:
            return False
        for key in self.cfg.result_path.split("."):
            if not isinstance(value, Mapping) or key not in value:
                return False
            value = value[key]
        return isinstance(value, str) and value in self.cfg.states
