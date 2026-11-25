"""Who is calling, and which rules a turn runs under.

The auth middleware leaves the identity it verified in the request: a service token's Client ID,
or ``local`` with no Access in front. The roster turns that into a caller, and the request's
``profile`` into one of that caller's profiles. The request picks; the token decides what may
be picked. A name the caller may not use, or that does not exist, is no profile at all.

With no ``[[caller]]`` tables there is one caller, as before profiles: every admitted token is
``default``, or the relay's only caller is ``local``. With no ``[[profile]]`` tables every caller
runs under EVERYTHING, which is the relay's rules with nothing taken away.
"""

from __future__ import annotations

from dataclasses import dataclass

from .access import LOCAL_CALLER
from .config import ProfileConfig, RelayConfig

# The implicit profile: every server, every tool, today's gates. Its name is not a valid profile
# name, so nothing can ask for it, and the log line shows "-" for it.
EVERYTHING = ProfileConfig.model_construct(name="-", gated="allow")
DEFAULT_CALLER = "default"


@dataclass(frozen=True)
class Caller:
    name: str
    profiles: tuple[ProfileConfig, ...]  # the first is the default


class Roster:
    def __init__(self, cfg: RelayConfig) -> None:
        self._named = bool(cfg.profile)
        everything = (EVERYTHING,)
        if cfg.access is None:
            self.implicit: Caller | None = Caller(LOCAL_CALLER, cfg.profile or everything)
            self._by_token: dict[str, Caller] = {LOCAL_CALLER: self.implicit}
        elif not cfg.caller:
            self.implicit = Caller(DEFAULT_CALLER, everything)
            self._by_token = {token: self.implicit for token in cfg.access.service_tokens}
        else:
            self.implicit = None
            by_name = {profile.name: profile for profile in cfg.profile}
            self._by_token = {
                caller.token: Caller(caller.name, tuple(by_name[name] for name in caller.profiles) or everything)
                for caller in cfg.caller
            }

    def caller(self, identity: str) -> Caller | None:
        """The caller a verified identity belongs to, or None if it is nobody the config names."""
        return self._by_token.get(identity)

    def choose(self, caller: Caller, requested: str | None) -> ProfileConfig | None:
        """The profile a turn runs under: the one asked for if this caller may use it, else None."""
        if requested is None:
            return caller.profiles[0]
        if not self._named:
            return None
        return next((profile for profile in caller.profiles if profile.name == requested), None)
