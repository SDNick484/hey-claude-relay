# Hey Claude in the car: design

Date: 2026-11-23
Status: approved by the owner, 2026-11-23
Home: `docs/superpowers/specs/` in the relay repo once it exists

## Intent

The owner wants to talk to Claude through Android Auto and have it act on the apps
he has already put MCP servers on. His examples: ask the cookbook collection
what to make for dinner, ask Matinee what to watch tonight, and control the
house through Home Assistant. The value is reach: one voice front door to a
growing set of servers, so a server added later is usable from the car with no
car-side work.

It shares a name with the laptop `hey-claude`, but not code. The laptop app is
a local wake-word launcher for Claude Code sessions. This is a turn-based
voice conversation whose brain runs on the homelab.

## Goals

1. From the car, by voice, ask questions of Cookbooks, Matinee, and Home
   Assistant and hear a short spoken answer.
2. Follow-ups work within a conversation ("what about the second one?").
3. Adding or removing an MCP server is a config edit on the relay. No app
   rebuild.
4. The relay is a clean text-in, text-out service that other clients (the
   laptop app, a script, a future watch app) can use later.

## Non-goals (v1)

- A wake word or open mic in the car. Android Auto does not allow it.
- Any custom car screen beyond what the messaging templates give.
- Play Store release. This is sideloaded for personal use.
- Wiring the laptop `hey-claude` to the relay. The API should allow it; the
  work is later.
- The relay doing its own speech. Android Auto does speech-to-text and reads
  replies aloud.
- More than one user.

## Anti-goals

- Anything that needs the driver to read or touch the screen beyond one tap
  to start.
- A voice path that can do things that are a chore to undo. The car is the
  wrong place to confirm a firmware update or a reboot.
- Secrets compiled into the APK or committed to a repo.
- Long answers. A reply that takes more than about 20 seconds to hear is a
  failure even if it is correct.
- Silent failure. Every turn ends in something spoken, including "I couldn't
  reach the house."

## Constraints found in the existing work

- **Android Auto messaging.** A conversation is a `MessagingStyle`
  notification with reply and mark-as-read actions. The driver's spoken reply
  arrives in the app as transcribed text in the background. Notifications
  alone only let the driver respond to a message, so starting a turn needs a
  templated messaging screen (Car App Library) with a "Claude" conversation
  to tap.
- **The MCP servers check a Cloudflare Access JWT on every request, even from
  the LAN** (`mcp-server-shared/src/mcp_shared/remote.py`). The relay cannot
  reach them directly on the Docker host's network; it has to come in through Access.
- **The servers already model this kind of caller.** An Access service token
  listed in `MCP_ALLOWED_SERVICE_TOKENS` is an "automated" caller, limited to
  the tiers in `MCP_AUTOMATION_TIERS` (`read,power-off` by default, optionally
  `everyday`, never `costly`). The relay should be exactly that.
- **The Messages API's MCP connector cannot be used.** It authenticates with a
  bearer token only, and Access service tokens are a pair of headers. So the
  relay is its own MCP client and runs the tool loop itself.
- **Hosting.** An Alpine container (Docker) on the homelab, with the existing cloudflared tunnel.
- **Testing.** The car side cannot be exercised from a cloud session. CI
  builds the APK; the owner tests on the Desktop Head Unit and in the car.

## Approaches

| | What | Trade-off |
|---|---|---|
| A | Phone app calls the Claude API itself and lists the servers | Fewest parts. Keys and server tokens live on the phone, and every server change is an app rebuild. Also blocked by the Access header constraint above. |
| **B (chosen)** | Thin phone app, small relay on Proxmox | One more service, and the car depends on the homelab being reachable. Server list, keys, voice rules, and safety rules live in one place. |
| C | Use the official Claude app in the car | Not available on Android Auto. |

The owner picked B: "let's abstract the server."

## Design

Two repos (my call, so the relay stands alone as the reusable piece):
`hey-claude-relay` and `hey-claude-auto`.

```
car mic ─▶ Android Auto ─▶ phone app ─▶ Cloudflare Access ─▶ relay (homelab)
                              ▲                                  │  Claude API
car speaker ◀─ Auto reads ◀───┘ reply text                       ├─▶ Cookbooks MCP
                                                                 ├─▶ Matinee MCP
                                                                 └─▶ Home Assistant MCP
```

### Relay (`hey-claude-relay`)

Python, in Docker on the homelab, published through the existing tunnel behind its
own Access application (my call: Python, to match the MCP servers and reuse
their Access-checking approach).

**API** (the contract both repos build against)

Every request carries the caller's Cloudflare Access service token in the
`CF-Access-Client-Id` and `CF-Access-Client-Secret` headers. Access turns
that into a signed `Cf-Access-Jwt-Assertion`, which the relay verifies.

`POST /v1/turn`

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

- `turn_id` makes retries safe. A repeated `turn_id` returns the stored
  response and runs nothing again. Stored for 10 minutes.
- `expect_secret: true` means the reply asked for the passphrase. The
  client must send the driver's next message as a normal turn on the same
  conversation, with `"secret": true`, and must not display or keep its text.
- `secret` is optional and defaults to `false`. A turn sent with
  `"secret": true` is never passed to Claude, stored or logged, whatever
  state the relay is in. If the relay holds no call for that conversation
  (it restarted, the conversation expired, the call was already settled), it
  answers `200` with a spoken line saying nothing was done.
- An unknown or expired `conversation_id` starts a new conversation and
  returns the new id. It is not an error.

Errors are JSON with a status of 4xx or 5xx:

```json
{ "error": { "code": "upstream_timeout", "spoken": "I ran out of time reaching the house." } }
```

The client reads `spoken` aloud when present, and its own fallback line
otherwise. Codes: `unauthorized`, `bad_request`, `upstream_timeout`,
`upstream_error`, `rate_limited`, `internal`.

`POST /v1/conversations/{id}/end` forgets a conversation (`204`).

`GET /healthz` answers before auth, like the sibling servers.

The response is a single JSON body with no streaming, because the car reads a
whole message at once. Clients should allow 60 seconds.

**A turn**

1. Verify the caller's Access JWT (signature, audience, issuer, expiry, and
   that the service token's client ID is on the relay's allow-list).
2. Load or start the conversation. Conversations live in memory, expire after
   15 minutes idle, and are capped in length. A restart forgets them, which
   is acceptable.
3. Call Claude with the voice instructions, the conversation, and the tools
   gathered from the configured servers.
4. Run tool calls against the MCP servers and loop, up to 8 rounds and 45
   seconds in total. On either limit, reply with what is known plus one
   sentence saying it ran out of time.
5. Return the final text, stripped of markdown.

**Voice instructions** (a prompt file on the relay, editable without a
rebuild): answer for the ear, lead with the answer, two or three sentences,
at most three options when listing, no markdown or URLs, say numbers the way
a person would, end with a short offer when there is more. When a tool
refuses, say so plainly and do not retry around it.

**Config** (`relay.toml`, secrets from the environment):

```toml
model = "claude-sonnet-5-5"        # a faster model is a one-line change
idle_minutes = 15

[[server]]
name = "matinee"
url = "https://.../mcp"
auth = "cf-service-token"          # CF_ACCESS_CLIENT_ID / _SECRET from env
# tools = ["search_*", "list_*"]   # optional allow-list
# deny  = []

[[server]]
name = "home-assistant"
url = "https://.../mcp"
auth = "cf-service-token"
tools = ["ha_search", "ha_get_state", "ha_get_overview", "ha_call_service", "ha_bulk_control"]
deny_services = ["lock.unlock", "alarm_control_panel.alarm_disarm"]

[[gate]]                           # step-up: held until the checks pass
server = "home-assistant"
match = { tool = "ha_call_service", service = "cover.open_cover" }
require = ["near-home", "passphrase"]

[near_home]
entity = "person.example"          # asked of Home Assistant by the relay itself
states = ["home"]                  # or a wider "arriving" zone if he defines one
```

The passphrase is stored as a salted hash from the environment, never in the
file.

**Safety, in two layers**

- The servers enforce tiers for the relay's service token. Proposed setting:
  `read,power-off,everyday` on the servers the relay may use, so "pause the
  TV" works and nothing costly does.
- The relay enforces its own per-server tool allow-list and, for Home
  Assistant (a third-party server with no tiers), a deny-list of services.
  Unlocking and disarming are refused by voice (my call until the owner says
  otherwise).
- Gated actions. The owner wants "open the garage" to work, behind a spoken
  passphrase. A gate is a rule in the relay config naming a tool call and the
  checks it needs. When Claude makes a matching call, the relay's own code
  holds it and does not run it:
  1. `near-home` (my proposal, for the garage): the relay asks Home Assistant
     where the owner's phone is. Not near home means a spoken refusal and no
     passphrase prompt.
  2. `passphrase`: the turn's reply is "Say your passphrase." The next
     message on that conversation is taken by the relay as the attempt. It is
     normalized (case, punctuation, spacing, digits as words), compared to
     the hash in constant time, and never sent to Claude, stored in the
     conversation, or logged. A match runs the held call once and the turn
     continues as normal. A miss drops the held call.
  The held call expires after 30 seconds. Three misses lock all gated actions
  for 15 minutes. The phone app must not echo the driver's passphrase message
  into the visible conversation.

  What this is for: stopping a misheard command or text inside a tool result
  from opening the garage, since the check is code that nothing Claude reads
  can argue with. What it is not: protection from someone who has heard the
  phrase in the car. The real credential is the phone's Access token.
  Google's speech-to-text also sees the phrase.

Tool results are treated as data. Instructions that appear inside a tool
result are not followed.

**Logging**: one line per turn with duration, tools used, and outcome. Spoken
text is logged at debug level only. Secrets are redacted.

### Phone app (`hey-claude-auto`)

Kotlin, sideloaded, deliberately thin.

- **Car side:** a templated messaging screen with one conversation, "Claude",
  plus a "New conversation" action. Tapping it lets the driver dictate.
  Replies are posted as `MessagingStyle` notifications with reply and
  mark-as-read actions, so Android Auto announces them, reads them aloud, and
  takes a spoken follow-up.
- **Plumbing:** the reply action hands the transcribed text to a background
  worker, which calls `POST /v1/turn` and posts the answer. On any failure it
  posts a spoken error ("I couldn't reach the relay").
- **Phone side:** one settings screen with the relay URL, the Access service
  token (stored with the Android Keystore), and a "send test message" button.
  The same conversation is usable from the phone's notification shade, which
  makes testing possible without a car.

**First thing to prove:** that a driver can start a turn from the car screen
with no incoming message waiting. If the template will not open the reply
flow on an empty conversation, the fallback is a standing "What do you need?"
message from Claude that is always there to reply to. This gets a short
throwaway test on the Desktop Head Unit before the rest of the app is built.

### Order of work

1. Relay, tested here against stub MCP servers and with `curl`.
2. Deploy to the homelab, create the Access application and service tokens, admit
   the relay's token on the three servers. These steps are the owner's; the repo
   documents them.
3. Phone app: the head-unit test first, then the rest. CI builds the APK.

## Left to the builder

- Web framework, project layout, and test structure for the relay.
- Whether the relay's Access check vendors `remote.py` through `sync_shared`
  or carries a smaller copy of its own.
- How Cookbooks and Matinee admit a service token today (both have their own
  MCP auth code; check before assuming they match the siblings).
- Tool-list caching and refresh, and trimming if the combined tool list grows
  large.
- Minimum Android version and library versions.

## Decided with the owner

- Opening the garage by voice is wanted, behind a voice passphrase.
- Repo names `hey-claude-relay` and `hey-claude-auto`.

- The garage gate also requires being near home.
- Unlocking and disarming stay refused by voice.
- Build happens in Claude Code, one session per repo, working in parallel
  against the API contract above.

## Cost

Each turn is billed to an API key on the relay, separate from the plan
credits used to build it.

## Amendments

2026-11-23, agreed with the owner while planning the relay:

- The contract gained the optional `secret` request field described under
  **API**. It closes one gap: a passphrase reply that reaches a relay which
  has lost the held call would otherwise be handled as ordinary text.
- The relay's config and gate behaviour differ from the sketch above in the
  ways listed under "Decisions beyond the spec" in
  `docs/superpowers/plans/2026-11-23-hey-claude-relay.md`. None of them
  touches the API.
