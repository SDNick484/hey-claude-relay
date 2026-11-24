"""Configuration: relay.toml for settings, the environment for secrets.

Every model forbids unknown keys. A misspelled policy key must stop startup with a message that
names it, never be read as "no policy". Error messages name keys and environment variables and
never repeat a value, so a secret pasted into the wrong place does not end up in a log.
"""

from __future__ import annotations

import fnmatch
import re
import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]+$")
# The relay's own secrets. A server's token_env may not name one, or it would be sent to that server.
RESERVED_VARIABLES = ("ANTHROPIC_API_KEY", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET", "RELAY_PASSPHRASE_HASH")
_LOOPBACK_URL = re.compile(r"^http://(localhost|127\.\d+\.\d+\.\d+|\[::1\])(:\d+)?(/|$)")


_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def template_fields(template: str) -> list[str] | None:
    """The argument names a ``service_from`` template reads, or None if it is not a safe template.

    Safe means: at least one ``{name}`` field, every field a plain argument name, and no
    indexing, attribute access, conversion or format spec, so rendering can only ever copy the
    text of top-level string arguments.
    """
    names: list[str] = []
    try:
        for _, name, spec, conversion in string.Formatter().parse(template):
            if name is None:
                continue
            if not _FIELD_NAME.fullmatch(name) or spec or conversion:
                return None
            names.append(name)
    except ValueError:
        return None
    return names or None


class ConfigError(ValueError):
    """The configuration is unsafe or incomplete; the message says what to fix."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# An allow-list: at least one entry, and no blank ones. A blank entry must never come to mean
# "a caller with no name is allowed".
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Names = Annotated[tuple[Name, ...], Field(min_length=1)]


class AccessConfig(_Model):
    """Which Cloudflare Access application guards the relay, and which service tokens may call it."""

    team_domain: str
    audiences: Names
    service_tokens: Names

    @field_validator("team_domain")
    @classmethod
    def _hostname(cls, value: str) -> str:
        host = value.strip().removeprefix("https://").rstrip("/")
        if not _HOSTNAME_RE.fullmatch(host):
            raise ValueError("must be a hostname such as example.cloudflareaccess.com")
        return host


class ServerConfig(_Model):
    """One MCP server the relay may use."""

    # Lower-case letters, digits and hyphens: the name becomes the prefix of every tool name
    # Claude sees ("matinee__search_collection"), so it must be safe there and must not hold "__".
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    url: str
    # cf-service-token: CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET from the environment, for a
    # server behind Cloudflare Access. bearer: the token in the variable named by token_env, for a
    # server with its own. none: no credentials.
    auth: Literal["cf-service-token", "bearer", "none"]
    token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    tools: tuple[str, ...] = ()  # allow-list of tool names, as globs; empty allows everything not denied
    deny: tuple[str, ...] = ()  # tool names never offered, as globs
    timeout_seconds: float = Field(default=20, gt=0, allow_inf_nan=False)
    # The relay's own rules for a server whose tools are too broad to allow or deny by name: one
    # tool that can call any "service". service_from says which tool that is and how to read the
    # service out of a call's arguments, e.g. { ha_call_service = "{domain}.{service}" }. A call
    # on such a tool whose service cannot be read is refused.
    service_from: dict[str, str] = Field(default_factory=dict)
    # Besides the arguments the template reads, the only ones such a call may carry. Anything
    # else that is set (a raw escape hatch, say) gets the call refused: the service says nothing
    # about what another argument might make the server do.
    service_args: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    allow_services: tuple[str, ...] = ()  # globs; when set, any other service is refused
    deny_services: tuple[str, ...] = ()  # globs; always refused, whatever the allow-list says

    @field_validator("service_from")
    @classmethod
    def _safe_templates(cls, value: dict[str, str]) -> dict[str, str]:
        for tool, template in value.items():
            if template_fields(template) is None:
                raise ValueError(
                    f"the template for {tool!r} must be made of plain argument names in braces, "
                    'such as "{domain}.{service}"'
                )
        return value

    @model_validator(mode="after")
    def _service_rules_name_listed_tools(self) -> ServerConfig:
        # A rule about a tool nobody offers protects nothing, and usually means a typo.
        if self.service_from and not self.tools:
            raise ValueError("a server with service_from needs a tools list, so no other tool slips past its rules")
        for tool in self.service_from:
            if not any(fnmatch.fnmatchcase(tool, pattern) for pattern in self.tools):
                raise ValueError(f"service_from names {tool!r}, which the tools list does not offer")
        for tool in self.service_args:
            if tool not in self.service_from:
                raise ValueError(f"service_args names {tool!r}, which service_from does not")
        return self

    @model_validator(mode="after")
    def _service_lists_need_service_from(self) -> ServerConfig:
        if (self.allow_services or self.deny_services) and not self.service_from:
            raise ValueError(
                "allow_services and deny_services need service_from, which says how to read a service out of a call"
            )
        return self

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not re.fullmatch(r"https?://[^\s/]+(/\S*)?", value):
            raise ValueError("must be an http:// or https:// address")
        return value

    @model_validator(mode="after")
    def _token_env_goes_with_bearer(self) -> ServerConfig:
        if (self.auth == "bearer") != (self.token_env is not None):
            raise ValueError('token_env names the token\'s variable; set it with auth = "bearer" and only then')
        if self.token_env in RESERVED_VARIABLES:
            raise ValueError("token_env names one of the relay's own secrets, which would be sent to this server")
        return self

    @model_validator(mode="after")
    def _service_token_goes_over_https(self) -> ServerConfig:
        # The Access service token opens everything behind Access; it is not sent in the clear.
        if self.auth == "cf-service-token" and not (self.url.startswith("https://") or _LOOPBACK_URL.match(self.url)):
            raise ValueError('auth = "cf-service-token" needs an https:// url')
        return self


class GateMatch(_Model):
    """Which calls a gate holds: a tool (a glob), and optionally a service (a glob)."""

    tool: str = Field(min_length=1)
    service: str | None = Field(default=None, min_length=1)


class GateConfig(_Model):
    """A step-up rule: a matching call is held by the relay until every check in ``require`` passes."""

    server: str
    match: GateMatch
    require: tuple[Literal["near-home", "passphrase"], ...] = Field(min_length=1)

    @field_validator("require")
    @classmethod
    def _each_check_once(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("names a check twice")
        return value


class NearHomeConfig(_Model):
    """How the relay finds out for itself whether the owner is near home: one tool call.

    ``result_path`` is where in the tool's JSON answer the state is ("data.state"), and
    ``states`` are the values that count as near.
    """

    server: str
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_path: str = Field(pattern=r"^[^.\s]+(\.[^.\s]+)*$")
    states: Names


class GateSettings(_Model):
    """Timing for gated actions."""

    hold_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)  # how long a held call waits for the passphrase
    max_misses: int = Field(default=3, ge=1)  # wrong passphrases before every gated action is locked
    lockout_minutes: float = Field(default=15, gt=0, allow_inf_nan=False)


class RelayConfig(_Model):
    model: str = Field(default="claude-sonnet-5-5", min_length=1)
    # "none" sends no effort at all, for a model that has no such setting.
    effort: Literal["low", "medium", "high", "xhigh", "max", "none"] | None = "low"
    max_tokens: int = Field(default=16000, gt=0)
    idle_minutes: float = Field(default=15, gt=0, allow_inf_nan=False)
    max_turns: int = Field(default=40, ge=2)
    # The voice instructions. Unset: the prompt that ships with the relay. Set: your own file,
    # relative to relay.toml, read when each conversation starts so it can be edited in place.
    prompt_file: Path | None = None
    max_rounds: int = Field(default=8, ge=1)
    # The tool loop may run for turn_seconds; after it, one last call gets wrapup_seconds. Clients
    # wait 60 seconds for an answer, so the two together must stay under that.
    turn_seconds: float = Field(default=45, gt=0, allow_inf_nan=False)
    wrapup_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    claude_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)  # the longest one call to Claude may take
    tools_ttl_seconds: float = Field(default=300, ge=0, allow_inf_nan=False)
    # How long a server gets to answer with its tool list when a conversation starts. Short on
    # purpose: this time comes out of the turn's own, and a server that hangs must not eat it.
    list_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    max_text_chars: int = Field(default=2000, gt=0)
    turns_per_minute: int = Field(default=30, gt=0)
    max_description_chars: int = Field(default=2000, ge=0)  # a longer tool description is cut there
    access: AccessConfig | None = None
    server: tuple[ServerConfig, ...] = ()
    gate: tuple[GateConfig, ...] = ()
    near_home: NearHomeConfig | None = None
    gates: GateSettings = GateSettings()

    @model_validator(mode="after")
    def _near_home_can_be_checked(self) -> RelayConfig:
        names = {server.name for server in self.server}
        if self.near_home is not None and self.near_home.server not in names:
            raise ValueError(f"near_home names server {self.near_home.server!r}, which is not configured")
        if self.near_home is None and any("near-home" in gate.require for gate in self.gate):
            raise ValueError("a gate requires near-home, which needs a [near_home] table saying how to check it")
        return self

    @model_validator(mode="after")
    def _gates_can_be_enforced(self) -> RelayConfig:
        servers = {server.name: server for server in self.server}
        for gate in self.gate:
            server = servers.get(gate.server)
            if server is None:
                raise ValueError(f"a gate names server {gate.server!r}, which is not configured")
            if not server.tools:
                raise ValueError(f"server {gate.server!r} has a gate, so it needs a tools list")
            offered = any(
                fnmatch.fnmatchcase(gate.match.tool, pattern) or fnmatch.fnmatchcase(pattern, gate.match.tool)
                for pattern in server.tools
            )
            if not offered:
                raise ValueError(
                    f"a gate matches tool {gate.match.tool!r}, which server {gate.server!r} does not offer"
                )
            wanted = gate.match.service
            if wanted is not None and server.allow_services:
                reachable = any(
                    fnmatch.fnmatchcase(wanted.casefold(), allowed.casefold())
                    or fnmatch.fnmatchcase(allowed.casefold(), wanted.casefold())
                    for allowed in server.allow_services
                )
                if not reachable:
                    raise ValueError(f"a gate matches service {wanted!r}, which allow_services never lets through")
            reads_a_service = any(fnmatch.fnmatchcase(tool, gate.match.tool) for tool in server.service_from)
            if gate.match.service is not None and not reads_a_service:
                # Without it the relay could not tell which calls the gate is for, and none would be held.
                raise ValueError(
                    f"a gate on server {gate.server!r} matches a service, but that server's service_from "
                    f"has no entry for a tool matching {gate.match.tool!r}"
                )
        return self

    @model_validator(mode="after")
    def _a_turn_fits_what_clients_wait(self) -> RelayConfig:
        if self.turn_seconds + self.wrapup_seconds > 58:
            raise ValueError("turn_seconds plus wrapup_seconds must stay under 58: clients wait 60 seconds for a reply")
        return self

    @model_validator(mode="after")
    def _server_names_are_unique(self) -> RelayConfig:
        seen: set[str] = set()
        for server in self.server:
            if server.name in seen:
                raise ValueError(f"server name {server.name!r} is used twice")
            seen.add(server.name)
        return self


@dataclass(frozen=True)
class Secrets:
    """What comes from the environment. Nothing here is shown by repr(), so a traceback or a
    debug print of this object cannot leak a value."""

    anthropic_api_key: str | None = field(default=None, repr=False)
    cf_client_id: str | None = field(default=None, repr=False)
    cf_client_secret: str | None = field(default=None, repr=False)
    passphrase_hash: str | None = field(default=None, repr=False)
    bearer: Mapping[str, str] = field(default_factory=dict, repr=False)


def _env(env: Mapping[str, str], name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


def _explain(exc: ValidationError) -> str:
    """One clause per problem: the key's path and what is wrong with it, never the value given."""
    parts = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        where = ".".join(str(p) for p in err["loc"]) or "(top level)"
        what = "unknown key" if err["type"] == "extra_forbidden" else err["msg"]
        parts.append(f"{where}: {what}")
    return "; ".join(parts)


def load_config(path: Path, env: Mapping[str, str]) -> tuple[RelayConfig, Secrets]:
    """Read ``path`` and the secrets in ``env``. Raises ConfigError with a message fit to print."""
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: not found. Copy relay.example.toml to relay.toml and edit it.") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read ({exc.strerror}).") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML ({exc}).") from exc
    try:
        cfg = RelayConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {_explain(exc)}") from None
    if cfg.prompt_file is not None and not cfg.prompt_file.is_absolute():
        cfg = cfg.model_copy(update={"prompt_file": path.parent / cfg.prompt_file})
    passphrase_hash = _env(env, "RELAY_PASSPHRASE_HASH")
    if any("passphrase" in gate.require for gate in cfg.gate):
        from .gates import check_passphrase_hash  # here, not at the top: gates imports this module

        if passphrase_hash is None:
            raise ConfigError(
                f"{path}: a gate requires a passphrase, which needs RELAY_PASSPHRASE_HASH in the environment. "
                "Run `hey-claude-relay hash-passphrase` to make one."
            )
        check_passphrase_hash(passphrase_hash)
    bearer: dict[str, str] = {}
    for server in cfg.server:
        if server.auth == "cf-service-token":
            missing = [name for name in ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET") if not _env(env, name)]
            if missing:
                raise ConfigError(
                    f'{path}: server {server.name!r} uses auth = "cf-service-token", which needs '
                    f"{' and '.join(missing)} in the environment."
                )
        elif server.auth == "bearer" and server.token_env is not None:
            token = _env(env, server.token_env)
            if token is None:
                raise ConfigError(
                    f'{path}: server {server.name!r} uses auth = "bearer", which needs {server.token_env} '
                    "in the environment."
                )
            bearer[server.token_env] = token
    secrets = Secrets(
        anthropic_api_key=_env(env, "ANTHROPIC_API_KEY"),
        cf_client_id=_env(env, "CF_ACCESS_CLIENT_ID"),
        cf_client_secret=_env(env, "CF_ACCESS_CLIENT_SECRET"),
        passphrase_hash=passphrase_hash,
        bearer=bearer,
    )
    return cfg, secrets


def prompt_text(cfg: RelayConfig) -> str:
    """The voice instructions: ``cfg.prompt_file`` if set, else the prompt that ships with the relay."""
    if cfg.prompt_file is None:
        return resources.files(__package__).joinpath("prompts", "voice.md").read_text(encoding="utf-8").strip()
    try:
        text = cfg.prompt_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"prompt_file {cfg.prompt_file}: cannot be read ({exc.strerror}).") from exc
    if not text:
        raise ConfigError(f"prompt_file {cfg.prompt_file}: is empty.")
    return text
