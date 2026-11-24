# hey-claude-relay

A small service that takes one line of text (what someone said), runs a Claude conversation
with tools from your own MCP servers, and returns a short plain-text reply meant to be read
aloud. It was built as the back end of a voice assistant for the car: the phone app sends what
the driver said and speaks what comes back. Anything that can send a line of text can use it.

The relay is where the server list, the API key, the voice rules and the safety rules live, so
adding an MCP server is a config edit here and no client changes.

> **Status:** under construction, one milestone per pull request. Today the relay verifies who
> is calling, holds a conversation with Claude, and runs tools on the MCP servers it is given.
> The safety rules for those tools (denied services, gates behind a passphrase) come next;
> until they are in, do not point it at a server that can do anything you would mind. The design is in
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
