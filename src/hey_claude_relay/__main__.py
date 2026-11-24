"""Command line: ``hey-claude-relay serve`` and ``hey-claude-relay check-config``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .app import create_app
from .config import ConfigError, RelayConfig, Secrets, load_config
from .logsafe import configure_logging

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8787


def listen_address(bind: str | None, port: int | None, env: Mapping[str, str]) -> tuple[str, int]:
    """Where to listen: flags, then RELAY_BIND / RELAY_PORT, then 127.0.0.1:8787."""
    raw = port if port is not None else (env.get("RELAY_PORT") or DEFAULT_PORT)
    try:
        number = int(raw)
    except ValueError:
        number = 0
    if not 1 <= number <= 65535:
        raise ConfigError("--port / RELAY_PORT must be a port number between 1 and 65535.")
    return bind or env.get("RELAY_BIND") or DEFAULT_BIND, number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hey-claude-relay", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the relay")
    serve.add_argument("--config", help="path to relay.toml (RELAY_CONFIG, default ./relay.toml)")
    serve.add_argument("--bind", help=f"address to listen on (RELAY_BIND, default {DEFAULT_BIND})")
    serve.add_argument("--port", type=int, help=f"port (RELAY_PORT, default {DEFAULT_PORT})")
    check = sub.add_parser("check-config", help="read the configuration and say whether it is usable")
    check.add_argument("--config", help="path to relay.toml (RELAY_CONFIG, default ./relay.toml)")
    return parser


def _serve(cfg: RelayConfig, secrets: Secrets, host: str, port: int) -> int:  # pragma: no cover
    import uvicorn

    # log_config=None: uvicorn's lines go through the root logger and its redacting filter.
    # proxy_headers=False: the peer address stays the real one; the relay decides what to trust.
    uvicorn.run(
        create_app(cfg, secrets),
        host=host,
        port=port,
        log_config=None,
        proxy_headers=False,
        timeout_graceful_shutdown=2,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command. Returns 2 with one line on stderr when the configuration is unusable."""
    args = _parser().parse_args(argv)
    env = os.environ
    path = Path(args.config or env.get("RELAY_CONFIG") or "relay.toml")
    try:
        cfg, secrets = load_config(path, env)
        if args.command == "check-config":
            print(f"ok: {path}")
            return 0
        host, port = listen_address(args.bind, args.port, env)
    except ConfigError as exc:
        print(f"hey-claude-relay: {exc}", file=sys.stderr)
        return 2
    configure_logging(env.get("RELAY_LOG_LEVEL", "info"), secrets)  # pragma: no cover
    return _serve(cfg, secrets, host, port)  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
