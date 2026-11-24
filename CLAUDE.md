# CLAUDE.md

## What this is

`hey-claude-relay`: a small Starlette service that takes one line of spoken text, runs a Claude
conversation with tools from configured MCP servers, and returns a short reply to be read aloud.
The design is `docs/superpowers/specs/2026-11-23-hey-claude-car-design.md`; the build plan is
`docs/superpowers/plans/2026-11-23-hey-claude-relay.md`.

## Commands

- `uv sync`: install runtime and dev dependencies from `uv.lock`
- `uv run pytest`: all tests (`--cov` for coverage; the floor is 90%)
- `uv run ruff check .` and `uv run ruff format --check .`: lint and format check
- `uv run mypy`: strict type check of `src/`
- `uv run hey-claude-relay check-config --config relay.example.toml`: load a config
- `docker build -t relay:dev .`: the image as deployed (Alpine, musl)

CI (`.github/workflows/ci.yml`) runs exactly these, plus a Docker build that starts the image
with `relay.example.toml` and checks `/healthz`.

## Rules

- **The API is a contract shared with another repo.** The `API` section of the spec (and the
  README copy) is what clients build against. Do not change a request field, a response field,
  a status meaning or an error code without the owner's say-so.
- **The repo is public and generic.** No real hostnames, personal names, entity IDs or private
  addresses anywhere: code, tests, docs, commit messages. Use `relay.example.com`,
  `mcp.example.com`, `example.cloudflareaccess.com`, `person.example`, `cover.garage_door`.
  `relay.toml` is gitignored; `relay.example.toml` is the template and must always load.
- **Secrets come from the environment only.** Never commit one, print one, log one, or accept
  one as a command-line argument. Error messages name keys and variables, not values.
- **No test needs a real server, a real API key or the network.** Stub MCP servers and a fake
  Claude client only.
- **Config models forbid unknown keys.** A misspelled policy key must stop startup.
- **No special case for one household in code.** A house-specific rule belongs in the example
  config, not in `src/`.
- **Dependencies are few and pinned exactly** in `pyproject.toml`; `uv.lock` pins the tree.
  Run `uv lock` after changing a pin. Docker images and GitHub Actions are pinned by digest/SHA.
- **Alpine and OpenRC.** The image is `python:3.13-alpine`; nothing may assume systemd. Editor
  instructions in docs use `vim`.
- Tests first: write the failing test, watch it fail, then write the code.
- **History is append-only.** A conversation keeps the system prompt and tool list it started with,
  and earlier messages are never edited: the API ties the model's thinking blocks to all three.
  The only removals are whole turns from the end (a turn that came to nothing) and `trim`, which
  also strips thinking blocks from what it keeps.
- Test helpers: `tests/conftest.py` (`make_relay`, `FakeClock`), `tests/fakes.py` (`FakeClaude`, `say`,
  `use`, `refuse`), `tests/keys.py` (`mint`).

## Layout

- `src/hey_claude_relay/config.py`: `relay.toml` and environment into `RelayConfig` and `Secrets`
- `src/hey_claude_relay/errors.py`: the `{"error": {"code", "spoken"}}` shape
- `src/hey_claude_relay/app.py`: `create_app`, routes, request validation, the turn log line, the rate limit,
  the crash guard
- `src/hey_claude_relay/turn.py`: `TurnRunner`, one turn from text to spoken reply
- `src/hey_claude_relay/claude.py`: the Claude API adapter and its error mapping
- `src/hey_claude_relay/conversations.py`: conversations in memory, and `turn_id` replay
- `src/hey_claude_relay/speech.py`: `strip_markdown`
- `src/hey_claude_relay/access.py`: Access assertion check and the auth middleware (a port of part of
  `mcp-server-shared`'s `remote.py`; the header comment names the upstream commit)
- `src/hey_claude_relay/logsafe.py`: the log filter that masks secret values
- `src/hey_claude_relay/__main__.py`: `serve`, `check-config`
- `src/hey_claude_relay/prompts/voice.md`: the default voice instructions, shipped in the package
