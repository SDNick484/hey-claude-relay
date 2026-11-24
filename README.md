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

## Configuration reference

Everything is in `relay.toml` except secrets. An unknown key stops startup. `relay.example.toml`
has every key with comments.

| Key | Default | Meaning |
| --- | --- | --- |
| `model` | `claude-sonnet-5-5` | the Claude model |
| `effort` | `low` | how hard the model thinks: `low`, `medium`, `high`, `xhigh`, `max`; leave the key out for a model that has no such setting |
| `max_tokens` | `16000` | the most one reply (thinking included) may use |
| `idle_minutes` | `15` | a conversation is forgotten after this long without a turn |
| `max_turns` | `40` | past this many turns a conversation keeps only its newest half |
| `prompt_file` | unset | your own voice prompt, a path beside `relay.toml`; unset uses the prompt that ships with the relay |
| `max_rounds` | `8` | rounds of tool calls in one turn |
| `turn_seconds` | `45` | time for the tool loop of one turn, tool listing included |
| `wrapup_seconds` | `10` | time for the one last call after a limit; with `turn_seconds` it must stay under 58 |
| `claude_seconds` | `30` | the longest one call to Claude may take |
| `list_seconds` | `5` | how long a server gets to list its tools when a conversation starts |
| `tools_ttl_seconds` | `300` | how long a server's tool list is remembered |
| `max_text_chars` | `2000` | a longer `text` is a `bad_request` |
| `turns_per_minute` | `30` | more than this answers `rate_limited` |
| `max_description_chars` | `2000` | a longer tool description is cut there |
| `[access]` `team_domain`, `audiences`, `service_tokens` | none | the Access application in front of the relay and the service-token client IDs allowed to call it; without the table the relay serves this machine only |
| `[[server]]` `name`, `url`, `auth` | required | an MCP server; `auth` is `cf-service-token`, `bearer` or `none` |
| `[[server]]` `token_env` | none | with `auth = "bearer"`: the environment variable holding that server's token |
| `[[server]]` `tools`, `deny` | all, none | allow-list and deny-list of tool names (globs) |
| `[[server]]` `timeout_seconds` | `20` | the longest one tool call on that server may take |
| `[[server]]` `service_from`, `allow_services`, `deny_services` | none | see "What a voice may and may not do" |
| `[[gate]]` `server`, `match`, `require` | none | a held action and its checks: `near-home`, `passphrase` |
| `[near_home]` `server`, `tool`, `arguments`, `result_path`, `states` | none | how the relay checks where you are |
| `[gates]` `hold_seconds`, `max_misses`, `lockout_minutes` | `30`, `3`, `15` | timing for gated actions |

Environment (never in a file in git): `ANTHROPIC_API_KEY`, `CF_ACCESS_CLIENT_ID`,
`CF_ACCESS_CLIENT_SECRET`, `RELAY_PASSPHRASE_HASH`, and each server's `token_env` variable. Also
`RELAY_CONFIG`, `RELAY_BIND`, `RELAY_PORT` (where to find the config and where to listen; the
image sets them) and `RELAY_LOG_LEVEL` (`info`, or `debug` to log what was said and answered).

## Deploying

The relay runs as one container on any Docker host, published through a Cloudflare tunnel
behind its own Access application. These are the steps that need your accounts and your
secrets. Nothing here asks you to put a secret anywhere but `.env` on the host and the phone
app's settings.

On the host, get the code and build the image:

```sh
git clone https://github.com/SDNick484/hey-claude-relay.git && cd hey-claude-relay
docker build --build-arg GIT_COMMIT="$(git rev-parse HEAD)" -t hey-claude-relay .
cp relay.example.toml relay.toml
cp compose.example.yaml compose.yaml
touch .env && chmod 600 .env
```

Then:

1. **An Access application for the relay.** In Cloudflare Zero Trust, add a self-hosted
   application for the relay's hostname (say `relay.example.com`) with one policy whose action
   is *Service Auth*. Put its AUD tag in `[access] audiences` and your team domain
   (`<team>.cloudflareaccess.com`) in `[access] team_domain`.
2. **A service token for the phone.** Create a service token and add it to that policy. Put its
   Client ID (it ends in `.access`) in `[access] service_tokens`. Its Client ID and Client Secret
   go into the phone app's settings and nowhere else.
3. **A second service token, for the relay itself.** This is what the relay shows your MCP
   servers. In `.env`:

   ```sh
   CF_ACCESS_CLIENT_ID=<the relay token's Client ID>
   CF_ACCESS_CLIENT_SECRET=<the relay token's Client Secret>
   ```
4. **Admit the relay on each server behind Access.** For every `[[server]]` with
   `auth = "cf-service-token"`:
   - add the relay's token to that server's Access policy (action *Service Auth*);
   - on servers built on `mcp-server-shared`, add its Client ID to `MCP_ALLOWED_SERVICE_TOKENS`,
     and where the relay should do more than read, set
     `MCP_AUTOMATION_TIERS=read,power-off,everyday`. A server only sees a setting its init
     script exports: check that `MCP_AUTOMATION_TIERS` is in the export list.
   - a third-party server (Home Assistant's) has no tiers. There the relay's own `tools`,
     `allow_services`, `deny_services` and `[[gate]]` rules are the control, so set them with care.
5. **A server with its own token.** For `auth = "bearer"`, put that server's token in `.env`
   under the name its `token_env` gives:

   ```sh
   COOKBOOKS_MCP_TOKEN=<that server's token>
   ```
6. **An Anthropic API key.** Turns are billed to it. In `.env`:

   ```sh
   ANTHROPIC_API_KEY=<the key>
   ```
7. **The passphrase**, if a gate requires one. Run this, type the phrase twice (it is not shown),
   and paste the one line it prints into `.env`:

   ```sh
   docker run --rm -it hey-claude-relay hash-passphrase
   ```

   Three or more words, no numbers, and nothing you would say in the car for another reason.
8. **Your configuration.** Edit the two files, then let the relay check them:

   ```sh
   vim relay.toml      # hosts, servers, service lists, the gate, [near_home]
   vim .env            # KEY=value lines, no quotes
   docker run --rm --env-file .env -v "$PWD/relay.toml:/config/relay.toml:ro" hey-claude-relay check-config
   ```

   `check-config` prints `ok` or one line saying what to fix. It names keys and variables,
   never their values.
9. **Start it, route it, test it.**

   ```sh
   vim compose.yaml    # the published address: where the tunnel's route will point
   docker compose up -d
   curl -s http://127.0.0.1:8787/healthz
   ```

   Add a route in the tunnel from the relay's hostname to that address. Then, from any machine,
   with the phone token's two values exported in the shell:

   ```sh
   export RELAY_URL=https://relay.example.com
   export CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET
   sh scripts/smoke.sh
   ```

   It prints one `ok` line per check and sends two real turns to Claude.

To update: `git pull`, build the image again, `docker compose up -d`. A restart forgets
conversations, held calls and the lockout, which is by design.

If something is wrong, `docker compose logs relay` has one line per turn and one per refusal.
Refusals name the reason (no assertion, an unlisted token, a bad audience) and never the token.

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
