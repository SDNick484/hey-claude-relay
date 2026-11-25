# Hey Claude at home: profiles and a Home Assistant speaker

Date: 2026-11-24
Status: approved by the owner, 2026-11-25
Builds on: `docs/superpowers/specs/2026-11-23-hey-claude-car-design.md` in
`hey-claude-relay` (built: milestones 1 to 6 merged, security fixes merged)

## Intent

The owner wants the same voice front door he is building for the car to work
from a speaker at home: say a wake word in his office, ask about dinner or a
movie or the house, and hear the answer. He will start with one Home
Assistant Voice Preview Edition in his office and may add the bedroom and
kitchen if it works well. A watch client is wanted too and gets its own
design after this one.

## Goals

1. A Voice PE speaker in Home Assistant can use the relay as its
   conversation agent.
2. The relay applies different rules to different places: the owner's own
   devices, his office, and rooms anyone may use.
3. Adding a speaker in a new room is one setting in Home Assistant. No relay
   change, no code change.
4. Nothing already built against the relay API breaks.

## Non-goals

- Recognising who is speaking. Rules attach to the room, not the voice.
- Replacing Home Assistant's own fast handling of simple commands.
- The watch app (next design).
- Streaming speech. The relay still returns one whole reply.

## Anti-goals

- A shared room that can do anything the owner would not want a guest or a
  child to trigger.
- A caller able to claim a more trusted profile than its token allows.
- The garage passphrase ending up in a Home Assistant log or history.

## Decided with the owner

- Rules are named profiles, and a caller chooses among the profiles its
  token is allowed (approach B). Rejected: one rule set per token (every
  speaker behind Home Assistant would share it), and speaker recognition
  (not reliable enough to hang a rule on).
- A speaker in a shared room answering anyone is fine.
- Hardware: Home Assistant Voice Preview Edition is the intended speaker.
- First test device: an Echo Show 5 converted with the TECHO5 firmware.
  It is in the Home Assistant area "office" and stays there for testing.

## Part 1: profiles in the relay (`hey-claude-relay`)

Today the relay has one rule set and one list of admitted service tokens,
and the `client` field is only logged.

**Config**

```toml
[[caller]]
name = "phone"
token = "phone-token-client-id.access"
profiles = ["personal"]            # the first is the default

[[caller]]
name = "home-assistant"
token = "ha-token-client-id.access"
profiles = ["office", "shared"]

[[profile]]
name = "personal"                  # everything, as today

[[profile]]
name = "office"
gated = "allow"                    # see the open risk below

[[profile]]
name = "shared"
servers = ["cookbooks", "matinee", "home-assistant"]
gated = "refuse"                   # a gated call is refused, never prompted
allow_services = ["light.*", "fan.*", "media_player.*"]   # narrows the server's own list
# prompt_file = "voice-shared.md"  # optional per-profile voice instructions
```

- A profile can only narrow what the server sections allow. It cannot add a
  tool or a service the server section does not already permit.
- `[access].service_tokens` keeps working: a config with no `[[caller]]`
  or `[[profile]]` behaves exactly as it does today.

**API**: one optional field on `POST /v1/turn`.

```json
{ "turn_id": "...", "text": "...", "conversation_id": "...",
  "client": "home-assistant", "profile": "office" }
```

- Omitted: the caller's first allowed profile.
- Named but not allowed for this caller: `403` with code `unauthorized`.
- A conversation keeps the profile it started with. A turn that names a
  different profile on an existing conversation starts a new conversation.
- Gate lockout is counted per caller, so misses on a room speaker cannot
  lock the phone out.
- The log line for each turn adds the caller name and the profile.

## Part 2: a Home Assistant conversation agent (new repo `hey-claude-ha`)

A custom integration, installable through HACS as a custom repository.

- **Setup**: relay URL, Access client ID and secret, checked against
  `/healthz` and one test turn. Options: a default profile, and a profile
  per Home Assistant area.
- **A turn**: the conversation entity receives the text and the device it
  came from, looks up the device's area to pick the profile, and posts to
  `/v1/turn` with a fresh `turn_id` and `client = "home-assistant"`. The
  reply becomes the spoken response. Home Assistant's conversation ID is
  mapped to the relay's.
- **Errors**: the relay's `spoken` line if there is one, otherwise "I
  couldn't reach Claude." Never silence.
- **Passphrase turns**: when a reply has `expect_secret`, the entity asks
  Home Assistant to keep listening so the speaker takes the phrase without
  a second wake word.
- **Simple commands**: the voice pipeline's "prefer handling commands
  locally" stays on, so "turn on the lights" never leaves the house and the
  relay gets everything else.
- **Path**: Home Assistant reaches the relay through its public Access URL
  with its own service token, the same way the phone does. The relay then
  reaches Home Assistant's MCP server as it does today.

**Open risk, to settle first in the build**: Home Assistant keeps what was
said in its chat log and pipeline debug history. If the passphrase attempt
cannot be kept out of those, the `office` profile is switched to
`gated = "refuse"` and the garage stays a phone and car action. The
builder's call unless the owner says otherwise.

## Order of work

1. Relay profiles (small, self-contained, testable with the existing fakes).
2. Home Assistant integration, tested with Home Assistant's own test
   harness and a fake relay, then on the office speaker by the owner.
3. The owner's steps: a second Access service token for Home Assistant, the
   `[[caller]]` and `[[profile]]` entries, installing the integration,
   choosing it as the conversation agent in the office speaker's pipeline.

## Left to the builder

- Exact config validation and naming in the relay.
- Home Assistant API details, which change between releases: check the
  current developer docs for the conversation entity, chat log, and
  continued conversation before designing.
- Whether a turn that takes many seconds needs an interim "one moment"
  from the speaker.
- Timeouts on the Home Assistant side.

## The test device, as Home Assistant shows it today

- A voice satellite (`assist_satellite.example_satellite`).
- Two assistant slots, each with its own pipeline choice and wake word
  settings. Both are on the preferred pipeline. The plan is to leave slot 1
  alone and point slot 2 at a pipeline that uses the relay.
- Follow-up settings per slot (follow-up time, follow-ups in a row), both 0
  today. The passphrase turn depends on continued listening working here.
- Max thinking time 90 s per slot, above the relay's 45 s limit.
- Recordings kept 0.
- Speech: local Whisper (`small.en`) and Piper in a container on the homelab,
  added to Home Assistant over Wyoming. Before this there was no
  speech-to-text engine at all, and the built-in agent was the only
  conversation agent.

## Hardware notes

Voice PE needs 2.4 GHz Wi-Fi and a 5 V, 2 A USB-C supply, which is not in
the box. Speech-to-text and text-to-speech run in Home Assistant, either
through Home Assistant Cloud or locally.
