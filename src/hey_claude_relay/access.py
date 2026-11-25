# Derived from SDNick484/mcp-server-shared src/mcp_shared/remote.py @ a48209bc6a615a22f16418cf4d8a3575b238d313
# (AccessPolicy, AccessVerifier and its key-resolver throttle, AccessMiddleware's order of checks).
# A smaller copy, not a synced one:
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

Without Access the relay is meant for this machine only (a local test). Binding to 127.0.0.1
isn't enough to guarantee that: a tunnel or reverse proxy running here reaches the port from
loopback too, and would carry anyone's request in. So with no Access and no --insecure-no-auth,
three things are refused: starting on a non-loopback address, any request carrying a proxy's
forwarding headers (Cloudflare adds Cf-Connecting-IP and Cf-Ray, most proxies X-Forwarded-For
or Forwarded), and any request whose Host is not this machine.

A browser is the other way in. A web page open on this machine can post to a local port
without asking, so with no Access a request whose Origin is not this machine is refused, in
both modes. And --insecure-no-auth still requires the Host to be ``localhost`` or an address:
a host name can be re-pointed at this machine by a hostile page (DNS rebinding), an address
cannot.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio
import anyio.to_thread
import jwt
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import AccessConfig, CallerConfig, ConfigError
from .errors import UNAUTHORIZED, error_response

log = logging.getLogger(__name__)

ACCESS_HEADER = "cf-access-jwt-assertion"
HEALTH_PATH = "/healthz"
# Headers a proxy adds to what it forwards. A request with one didn't start on this machine.
FORWARDED_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "true-client-ip",
    "cf-connecting-ip",
    "cf-ray",
    "via",
)
# Where AccessMiddleware leaves who sent the request: scope["state"][CALLER_STATE_KEY].
CALLER_STATE_KEY = "caller"
LOCAL_CALLER = "local"


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
    def from_config(cls, cfg: AccessConfig, callers: Sequence[CallerConfig] = ()) -> AccessPolicy:
        """The policy for ``cfg``, admitting its service tokens and every ``[[caller]]``'s token."""
        return cls(cfg.team_domain, cfg.audiences, frozenset(cfg.service_tokens) | {c.token for c in callers})


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
        except (OSError, ValueError) as exc:
            # The certs endpoint answered with something that is not a key set, or the connection
            # broke in the middle. Refuse the request; do not crash on it.
            raise AccessDenied(f"could not fetch or read Access's signing keys ({type(exc).__name__})") from exc
        return claims

    def caller(self, claims: Mapping[str, Any]) -> str:
        """The service token the verified ``claims`` belong to: its client ID, if it is admitted.

        Anything else is refused: a person's login, a token that is not on the list, a
        ``common_name`` that is not a plain string. An Access identity the relay doesn't know is
        never given the benefit of the doubt.
        """
        if claims.get("email"):
            raise AccessDenied("a person's login, not a service token")
        name = claims.get("common_name")
        if isinstance(name, str) and name and name in self.policy.service_tokens:
            return name
        raise AccessDenied("unknown caller: no common_name in the [access] service_tokens list")


# A Host header value: a name, an IPv4 address or a bracketed IPv6 address, with an optional port.
# Nothing else is accepted, so "127.0.0.1:8787@evil.example" is not read as 127.0.0.1.
_HOST_RE = re.compile(r"^(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<name>[A-Za-z0-9.-]+))(?::\d{1,5})?$")


def _host_of(value: str) -> str | None:
    """The host in a Host header value or a bind address, or None if it is not plainly one."""
    value = value.strip()
    if match := _HOST_RE.fullmatch(value):
        return match.group("v6") or match.group("name")
    try:  # a bare IPv6 address, as given to --bind
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _is_address(host: str) -> bool:
    """Whether ``host`` is ``localhost`` or an IP address, not a name that DNS could point anywhere."""
    name = _host_of(host)
    if name is None:
        return False
    if name.lower() == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _is_loopback(host: str) -> bool:
    """Whether ``host`` (an address, or a Host header value with an optional port) is this machine."""
    name = _host_of(host)
    if name is None:
        return False
    if name.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _is_loopback_origin(origin: str) -> bool:
    """Whether an Origin header names a page served from this machine."""
    scheme, separator, host = origin.partition("://")
    return separator == "://" and scheme in ("http", "https") and _is_loopback(host)


def _printable(text: object) -> str:
    """``text`` with nothing in it that could start a new log line, and not too much of it."""
    return "".join(ch if ch.isprintable() else "?" for ch in str(text))[:300]


def check_bind(bind: str, *, access: bool, insecure_no_auth: bool) -> None:
    """Refuse to listen beyond this machine without Cloudflare Access."""
    if access or insecure_no_auth or _is_loopback(bind):
        return
    raise ConfigError(
        f"Refusing to serve on {bind} without Cloudflare Access: anyone who can reach this port could use the "
        "relay. Add an [access] table to relay.toml, bind to 127.0.0.1, or pass --insecure-no-auth for a "
        "trusted-LAN test."
    )


class AccessMiddleware:
    """Lets a request through only with a valid Access assertion from an admitted service token.

    ``GET /healthz`` is answered before the assertion is looked at, so a monitor can use it.
    With no verifier, the checks in the module docstring apply instead, and they come first,
    health check included: a cross-site Origin is refused; with ``local_only`` so is a request
    a proxy forwarded or one addressed to another Host; without ``local_only``
    (--insecure-no-auth) the Host must still be an address, not a name.

    Every request passed on carries who sent it in ``scope["state"]["caller"]``: the service
    token's client ID, or ``"local"`` when there is no verifier.
    """

    def __init__(self, app: ASGIApp, verifier: AccessVerifier | None, *, local_only: bool) -> None:
        self.app = app
        self.verifier = verifier
        self.local_only = local_only

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        if self.verifier is None and (refusal := self._unguarded(headers)) is not None:
            await self._deny(scope, receive, send, refusal)
            return
        if scope["path"] == HEALTH_PATH and scope.get("method") in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        caller = LOCAL_CALLER
        if self.verifier is not None:
            try:
                token = headers.get(ACCESS_HEADER, "")
                if not token:
                    raise AccessDenied("no Cloudflare Access assertion (the request didn't come through Access)")
                caller = self.verifier.caller(await self.verifier.verify(token))
            except AccessDenied as exc:
                await self._deny(scope, receive, send, str(exc))
                return
            log.debug("Access OK for %s", caller)
        scope.setdefault("state", {})[CALLER_STATE_KEY] = caller
        await self.app(scope, receive, send)

    def _unguarded(self, headers: dict[str, str]) -> str | None:
        """With no Access in front: why this request is refused, or None if it may go on."""
        origin = headers.get("origin")
        if origin is not None and not _is_loopback_origin(origin):
            return "a cross-site request (its Origin is not this machine)"
        host = headers.get("host", "")
        if not self.local_only:
            return None if _is_address(host) else "addressed to a host name, not an address"
        forwarded = [h for h in FORWARDED_HEADERS if h in headers]
        if forwarded:
            return f"forwarded by a proxy ({', '.join(forwarded)})"
        return None if _is_loopback(host) else "addressed to a Host that is not this machine"

    async def _deny(self, scope: Scope, receive: Receive, send: Send, reason: str) -> None:
        client = scope.get("client") or ("?", 0)
        # The path and the reason can carry text the sender chose (an unverified key id, for one).
        log.warning(
            "Denied %s %s from %s: %s",
            _printable(scope.get("method")),
            _printable(scope["path"]),
            client[0],
            _printable(reason),
        )
        await error_response("unauthorized", UNAUTHORIZED)(scope, receive, send)
