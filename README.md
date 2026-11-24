# hey-claude-relay

A small service that takes one line of text (what someone said), runs a Claude conversation
with tools from your own MCP servers, and returns a short plain-text reply meant to be read
aloud. It was built as the back end of a voice assistant for the car: the phone app sends what
the driver said and speaks what comes back. Anything that can send a line of text can use it.

The relay is where the server list, the API key, the voice rules and the safety rules live, so
adding an MCP server is a config edit here and no client changes.

> **Status:** feature-complete, one milestone per pull request; the deployment guide is the last
> piece. The design is in
> [`docs/superpowers/specs/`](docs/superpowers/specs/2026-11-23-hey-claude-car-design.md) and
> the build plan in [`docs/superpowers/plans/`](docs/superpowers/plans/2026-11-23-hey-claude-relay.md).

## API

This is the contract clients build against.

Every request carries the caller's Cloudflare Access service token in the `CF-Access-Client-Id`
and `CF-Access-Client-Secret` headers. Access turns that into a signed
`Cf-Access-Jwt-Assertion`, which the relay verifies.

### `POST /v1/turn`

```json
{ "turn_id": "uuid chosen by the client",
  "text": "what the driver said",
  "conversation_id": "optional, omit to start a conversation",
  "client": "android-auto",
  "secret": false }
```

`200`:

```json
{ "conversation_id": "...",
  "reply": "plain text to be read aloud",
  "expect_secret": false,
  "tools_used": ["matinee.search"] }
```

- `turn_id` makes retries safe. A repeated `turn_id` returns the stored response and runs
  nothing again. Responses are stored for 10 minutes.
- `expect_secret: true` means the reply asked for the passphrase. The client sends the next
  message as a normal turn on the same conversation, with `"secret": true`, and must not
  display or keep its text.
- `secret` is optional and defaults to `false`. A turn sent with `"secret": true` is never
  passed to Claude, stored or logged, whatever state the relay is in. If the relay holds no
  call for that conversation, it answers `200` with a spoken line saying nothing was done.
- An unknown or expired `conversation_id` starts a new conversation and returns the new id.
  It is not an error.

The response is a single JSON body with no streaming. Clients should allow 60 seconds.

### Errors

Errors are JSON with a 4xx or 5xx status:

```json
{ "error": { "code": "upstream_timeout", "spoken": "Claude took too long to answer. Try again in a moment." } }
```

Read `spoken` aloud when it is present. Decide what to do from `code`, not from the HTTP status:

| `code` | Status | Meaning |
| --- | --- | --- |
| `unauthorized` | 403 | no valid Access assertion, or a service token that is not on the allow-list |
| `bad_request` | 400 (404, 405) | the request could not be understood |
| `upstream_timeout` | 504 | Claude did not answer in time |
| `upstream_error` | 502 | Claude could not be reached |
| `rate_limited` | 429 | too many turns; try again shortly |
| `internal` | 500 | a fault in the relay |

### `POST /v1/conversations/{id}/end`

Forgets a conversation. Answers `204`.

### `GET /healthz`

Answers before any authentication: `{"status": "ok", "version": "...", "commit": "..."}`.

## Running it

With [uv](https://docs.astral.sh/uv/):

```sh
uv sync
cp relay.example.toml relay.toml
vim relay.toml
export ANTHROPIC_API_KEY    # set it in your shell first; never write it in a file in this repository
uv run hey-claude-relay check-config
uv run hey-claude-relay serve          # 127.0.0.1:8787
curl -s http://127.0.0.1:8787/healthz
```

In Docker (Alpine):

```sh
docker build --build-arg GIT_COMMIT="$(git rev-parse HEAD)" -t hey-claude-relay .
docker run --rm -p 127.0.0.1:8787:8787 --env-file .env \
  -v "$PWD/relay.toml:/config/relay.toml:ro" hey-claude-relay
```

`relay.toml` holds settings; secrets come from the environment (`.env` is gitignored) and are
never written to a file in this repository. `relay.example.toml` documents every key.

| Variable | What it is |
| --- | --- |
| `ANTHROPIC_API_KEY` | the key turns are billed to |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | the relay's own Access service token, sent to servers with `auth = "cf-service-token"` |
| the name in a server's `token_env` | that server's bearer token, for `auth = "bearer"` |
| `RELAY_PASSPHRASE_HASH` | the line printed by `hey-claude-relay hash-passphrase`; needed when a gate requires a passphrase |

## How a turn runs

1. The caller's Access assertion is verified, and a repeated `turn_id` is answered from memory.
2. A new conversation reads the voice prompt and asks each configured server for its tools. It
   keeps both for its whole life, so an edit to either takes effect in the next conversation.
3. Claude is called with the conversation and the tools. When it asks for tools, the relay runs
   them and returns the results, for up to 8 rounds and 45 seconds in all.
4. At either limit the relay asks once more with tools switched off and adds "I ran out of time
   before I could finish." That is a `200`, not an error.
5. The reply is stripped of markdown and returned.

A server that cannot be reached does not fail the turn: its tools are missing, or its calls
come back as errors, and Claude says it could not reach it.

## What a voice may and may not do

Two layers. The servers enforce what an automated caller may do at all (for servers built on
`mcp-server-shared`, the tiers in `MCP_AUTOMATION_TIERS`). The relay adds rules of its own,
because only it knows the caller is a voice in a car. They are code: every tool call Claude asks
for is checked before anything is sent, and nothing Claude reads can change the answer.

- **Tool lists.** Per server, `tools` is an allow-list and `deny` a deny-list, both globs. A tool
  that is not offered is refused even if Claude asks for it by name.
- **Services.** Some servers have one tool that can call anything (Home Assistant's
  `ha_call_service`). `service_from` tells the relay which tool that is and how to read the
  service out of a call, e.g. `{ ha_call_service = "{domain}.{service}" }`. Then:
  - `deny_services` are refused, always.
  - `allow_services`, when set, are the only services allowed. Use it. A deny-list alone is easy
    to walk around: `cover.toggle` is not `cover.open_cover`, `lock.open` is not `lock.unlock`.
  - A call whose service cannot be read (a raw escape hatch, a missing field) is refused.
  - `service_args` lists the other arguments such a call may carry. A call with any argument
    beyond those and the template's own is refused, so a second way of saying what to do
    (`ws_command`) cannot ride along with an allowed service.
  - A server with `service_from` or a gate must have a `tools` list, and a rule that names a tool
    or service that could never match stops startup.
  - Letter case and stray spaces are ignored when matching.
  - Leave tools whose actions are not services (such as `ha_bulk_control`) off the `tools` list:
    these rules cannot see through them.
- **Gates.** A `[[gate]]` names a tool call and the checks it needs. A matching call is held by
  the relay, not run:
  1. `near-home`: the relay asks a server where you are (`[near_home]`). Not near means a spoken
     refusal and no passphrase prompt.
  2. `passphrase`: the reply is "Say your passphrase." with `expect_secret: true`. The next
     message on that conversation is the attempt, whatever it says. It is normalised (case,
     punctuation, spacing, digits as words), compared in constant time with the salted hash in
     `RELAY_PASSPHRASE_HASH`, and goes nowhere else: not to Claude, not into the conversation,
     not into any log at any level. A match runs the held call once. A miss drops it.
  - The held call expires after 30 seconds. A message that arrives later is still swallowed,
    not compared, and answered with "that took too long".
  - "Cancel", "never mind", "stop" or "no" drops the call without counting as a miss.
  - Three misses lock every gated action for 15 minutes.
  - One call can be held per conversation.

What a gate is for: stopping a misheard command, or text inside a tool result, from doing
something that matters. What it is not: protection from someone who has heard the phrase said
in the car. The real credential is the caller's Access service token, and the speech recogniser
hears the phrase too.

To make the hash (the phrase is never an argument, so it does not land in shell history):

```sh
uv run hey-claude-relay hash-passphrase          # or: docker run --rm -it hey-claude-relay hash-passphrase
```

Choose three or more words, no numbers, and nothing you would say in the car for another reason.

Turns are logged one line each: conversation (first eight characters), client, outcome, rounds,
tools and duration. What was said and what was answered are logged only at
`RELAY_LOG_LEVEL=debug`, and a turn marked `secret` is never logged at any level.

Without an `[access]` table the relay serves this machine only: it refuses to start on any
address but loopback, and refuses any request that a proxy forwarded, that is addressed to
another host, or that a web page on another site sent. `--insecure-no-auth` lifts the first
two for a test on a trusted LAN; reach the relay by IP address then, because it still refuses
requests addressed to a host name and cross-site requests.

## Development

```sh
uv sync
uv run pytest               # no test needs a real server, a real API key or the network
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## License

MIT. See [LICENSE](LICENSE).
