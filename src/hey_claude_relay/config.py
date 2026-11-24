"""Configuration: relay.toml for settings, the environment for secrets.

Every model forbids unknown keys. A misspelled policy key must stop startup with a message that
names it, never be read as "no policy". Error messages name keys and environment variables and
never repeat a value, so a secret pasted into the wrong place does not end up in a log.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]+$")


class ConfigError(ValueError):
    """The configuration is unsafe or incomplete; the message says what to fix."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Names = Annotated[tuple[str, ...], Field(min_length=1)]


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


class RelayConfig(_Model):
    model: str = "claude-sonnet-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    max_tokens: int = Field(default=16000, gt=0)
    idle_minutes: float = Field(default=15, gt=0)
    max_turns: int = Field(default=40, ge=2)
    prompt_file: Path = Path("prompts/voice.md")
    max_rounds: int = Field(default=8, ge=1)
    turn_seconds: float = Field(default=45, gt=0)
    wrapup_seconds: float = Field(default=10, gt=0)
    claude_seconds: float = Field(default=30, gt=0)
    tools_ttl_seconds: float = Field(default=300, ge=0)
    max_text_chars: int = Field(default=2000, gt=0)
    turns_per_minute: int = Field(default=30, gt=0)
    access: AccessConfig | None = None


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
    if not cfg.prompt_file.is_absolute():
        cfg = cfg.model_copy(update={"prompt_file": path.parent / cfg.prompt_file})
    secrets = Secrets(
        anthropic_api_key=_env(env, "ANTHROPIC_API_KEY"),
        cf_client_id=_env(env, "CF_ACCESS_CLIENT_ID"),
        cf_client_secret=_env(env, "CF_ACCESS_CLIENT_SECRET"),
        passphrase_hash=_env(env, "RELAY_PASSPHRASE_HASH"),
    )
    return cfg, secrets
