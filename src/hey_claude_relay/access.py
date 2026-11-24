# Derived from SDNick484/mcp-server-shared src/mcp_shared/remote.py @ a48209bc6a615a22f16418cf4d8a3575b238d313
# (AccessPolicy, AccessVerifier and its key-resolver throttle). A smaller copy, not a synced one:
# the rest of that file is MCP-server code (tiers, elicitation, the SDK app) the relay has no use
# for. When the verifier changes upstream, diff against that commit and carry the change over.
"""Who may call the relay: verifying Cloudflare Access assertions.

Every request Access lets through arrives at the tunnel's origin with a signed JWT in the
``Cf-Access-Jwt-Assertion`` header. The relay checks it on every request: signature (Access's
published keys), the application's audience tag, issuer, expiry, and that the service token's
client ID (the JWT's ``common_name``) is on the relay's own allow-list. So the relay is safe
even from the LAN, where nothing passes through Access.

Only service tokens are admitted. A person's Access login (a JWT with an ``email``) is refused:
the relay's callers are apps holding a token, not browsers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import anyio
import anyio.to_thread
import jwt

from .config import AccessConfig

log = logging.getLogger(__name__)


class AccessDenied(Exception):
    """A request's Access assertion is missing or doesn't check out."""


@dataclass(frozen=True)
class AccessPolicy:
    """Which Cloudflare Access application's assertions to accept, and from which service tokens."""

    team_domain: str  # "example.cloudflareaccess.com"
    audiences: tuple[str, ...]  # the application's AUD tag(s)
    # Service-token client IDs allowed in. repr=False: a config repr in a log or a traceback must
    # not list who is allowed in.
    service_tokens: frozenset[str] = field(default=frozenset(), repr=False)

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"{self.issuer}/cdn-cgi/access/certs"

    @classmethod
    def from_config(cls, cfg: AccessConfig) -> AccessPolicy:
        return cls(cfg.team_domain, cfg.audiences, frozenset(cfg.service_tokens))


KeyResolver = Callable[[str], Awaitable[Any]]


class AccessVerifier:
    """Checks a ``Cf-Access-Jwt-Assertion`` against an AccessPolicy.

    The signing keys come from the team's published certs endpoint. PyJWT's PyJWKClient caches
    them and refetches when a token names an unknown key id, which is how Cloudflare's key
    rotation is picked up. Its fetch is blocking, so it runs in a worker thread. Tests pass their
    own ``key_resolver``.
    """

    leeway = 30  # seconds of clock skew tolerated on exp/iat
    min_refetch_interval = 60.0  # minimum seconds between JWKS network fetches on unknown kid

    def __init__(self, policy: AccessPolicy, key_resolver: KeyResolver | None = None) -> None:
        self.policy = policy
        if key_resolver is None:
            client = jwt.PyJWKClient(policy.certs_url, cache_jwk_set=True, lifespan=3600, timeout=10)
            # One lock around the whole lookup, network fetch included, so every verification waits
            # while a refetch is in flight (up to the 10 s timeout). That is the point: a burst of
            # tokens naming a key we don't have yet makes one fetch, not one each. Fetches are rare
            # (cache expiry, key rotation), and a hit in the cache holds the lock for microseconds.
            lock = anyio.Lock()
            # When PyJWKClient last went to the network, successful or not: the throttle below
            # counts fetches, so a steady stream of tokens served from the cache must not move it,
            # or a rotated key would be refused until the cache expired. fetch_data is the one
            # method that does network I/O, so wrapping it on this instance catches every fetch.
            last_fetch: float | None = None
            fetch_data = client.fetch_data

            def fetch_and_record() -> Any:
                nonlocal last_fetch
                try:
                    return fetch_data()
                finally:
                    last_fetch = time.monotonic()

            client.fetch_data = fetch_and_record  # type: ignore[method-assign]

            async def resolve(token: str) -> Any:
                async with lock:
                    header = jwt.get_unverified_header(token)
                    kid = header.get("kid")
                    now = time.monotonic()
                    if client.jwk_set_cache is not None and not client.jwk_set_cache.is_expired():
                        cached_set = client.jwk_set_cache.get()
                        if cached_set is not None:
                            cached_kids = {k.key_id for k in cached_set.keys}
                            if (
                                kid not in cached_kids
                                and last_fetch is not None
                                and (now - last_fetch) < self.min_refetch_interval
                            ):
                                raise jwt.PyJWKClientError(f"Unable to find a signing key that matches {kid!r}")
                    key = await anyio.to_thread.run_sync(client.get_signing_key_from_jwt, token)
                return key.key

            key_resolver = resolve
        self._resolve = key_resolver

    async def verify(self, token: str) -> dict[str, Any]:
        """The assertion's claims, or AccessDenied saying what did not check out."""
        try:
            key = await self._resolve(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=list(self.policy.audiences),
                issuer=self.policy.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AccessDenied(f"invalid Access assertion: {exc}") from exc
        return claims

    def caller(self, claims: Mapping[str, Any]) -> str:
        """The service token the verified ``claims`` belong to: its client ID, if it is admitted.

        Anything else is refused: a person's login, a token that is not on the list, a
        ``common_name`` that is not a plain string. An Access identity the relay doesn't know is
        never given the benefit of the doubt.
        """
        name = claims.get("common_name")
        if isinstance(name, str) and name and name in self.policy.service_tokens:
            return name
        raise AccessDenied("unknown caller: no common_name in the [access] service_tokens list")
