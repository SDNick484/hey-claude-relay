# hey-claude-relay

A small service that takes one line of text (what someone said), runs a Claude conversation
with tools from your own MCP servers, and returns a short plain-text reply meant to be read
aloud. It was built as the back end of a voice assistant for the car: the phone app sends what
the driver said and speaks what comes back. Anything that can send a line of text can use it.

The relay is where the server list, the API key, the voice rules and the safety rules live, so
adding an MCP server is a config edit here and no client changes.

> **Status:** under construction, one milestone per pull request. Today the relay loads its
> configuration, answers `/healthz`, and verifies who is calling; it does not run turns yet. The design is in
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
uv run hey-claude-relay check-config
uv run hey-claude-relay serve          # 127.0.0.1:8787
curl -s http://127.0.0.1:8787/healthz
```

In Docker (Alpine):

```sh
docker build --build-arg GIT_COMMIT="$(git rev-parse HEAD)" -t hey-claude-relay .
docker run --rm -p 127.0.0.1:8787:8787 -v "$PWD/relay.toml:/config/relay.toml:ro" hey-claude-relay
```

`relay.toml` holds settings; secrets come from the environment and are never written to a file
in this repository. `relay.example.toml` documents every key.

Without an `[access]` table the relay serves this machine only: it refuses to start on any
address but loopback, and refuses any request that a proxy forwarded or that is addressed to
another host. `--insecure-no-auth` turns those checks off for a test on a trusted LAN.

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
