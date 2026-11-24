#!/bin/sh
# Smoke test for a running hey-claude-relay, using nothing but curl.
#
#   export RELAY_URL=https://relay.example.com
#   export CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET     # set them in your shell first
#   sh scripts/smoke.sh
#
# The two Access variables are handed to curl in a private file. They are never echoed and never
# appear on a command line. RELAY_SMOKE_LOCAL=1 skips the "strangers are refused" check, for a
# relay on this machine with no Access in front of it.
#
# It sends two real turns, so it costs two small calls to Claude.
set -eu

: "${RELAY_URL:?Set RELAY_URL to where the relay answers, for example https://relay.example.com}"
base=${RELAY_URL%/}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
trap 'exit 130' INT TERM HUP
auth="$tmp/auth"
: > "$auth"
chmod 600 "$auth"
if [ -n "${CF_ACCESS_CLIENT_ID:-}" ] && [ -n "${CF_ACCESS_CLIENT_SECRET:-}" ]; then
    case "$CF_ACCESS_CLIENT_ID$CF_ACCESS_CLIENT_SECRET" in
        *\"* | *\\*)
            echo "CF_ACCESS_CLIENT_ID or CF_ACCESS_CLIENT_SECRET holds a quote or a backslash, which this script cannot pass on whole." >&2
            exit 2
            ;;
    esac
    printf 'header = "CF-Access-Client-Id: %s"\nheader = "CF-Access-Client-Secret: %s"\n' \
        "$CF_ACCESS_CLIENT_ID" "$CF_ACCESS_CLIENT_SECRET" > "$auth"
fi
code=000

# new_id LABEL: a turn id no earlier run has used.
new_id() {
    if [ -r /proc/sys/kernel/random/uuid ]; then
        cat /proc/sys/kernel/random/uuid
    else
        printf 'smoke-%s-%s-%s\n' "$(date +%s)" "$$" "$1"
    fi
}

# call METHOD PATH [JSON]: send it with the credentials. The status lands in $code, the body in $tmp/body.
call() {
    : > "$tmp/body"
    if [ -n "${3:-}" ]; then
        printf '%s' "$3" > "$tmp/request"
        code=$(curl -sS -o "$tmp/body" -w '%{http_code}' --max-time 65 -X "$1" -K "$auth" \
            -H 'content-type: application/json' --data-binary "@$tmp/request" "$base$2") || code=000
    else
        code=$(curl -sS -o "$tmp/body" -w '%{http_code}' --max-time 65 -X "$1" -K "$auth" "$base$2") || code=000
    fi
}

# field NAME: the value of a JSON string field in the last body.
field() {
    sed -n 's/.*"'"$1"'" *: *"\([^"]*\)".*/\1/p' "$tmp/body" | head -n 1
}

ok() {
    printf 'ok   %s\n' "$1"
}

fail() {
    printf 'FAIL %s (HTTP %s)\n' "$1" "$code" >&2
    if [ -s "$tmp/body" ]; then
        cut -c1-300 "$tmp/body" | head -n 5 >&2
    fi
    exit 1
}

call GET /healthz
[ "$code" = 200 ] || fail "GET /healthz answers"
ok "GET /healthz answers"

if [ "${RELAY_SMOKE_LOCAL:-}" != 1 ]; then
    : > "$tmp/body"
    code=$(curl -sS -o "$tmp/body" -w '%{http_code}' --max-time 65 -X POST -H 'content-type: application/json' \
        --data-binary '{"turn_id":"smoke-stranger","text":"hello"}' "$base/v1/turn") || code=000
    # Access answers a stranger with a redirect to its login page or a 403; the relay itself, 403.
    case "$code" in
        302 | 401 | 403) : ;;
        *) fail "a turn without credentials is refused" ;;
    esac
    ok "a turn without credentials is refused (HTTP $code)"
fi

first=$(new_id first)
ask="{\"turn_id\":\"$first\",\"text\":\"This is a smoke test. Reply with one short sentence.\",\"client\":\"smoke\"}"
call POST /v1/turn "$ask"
reply=$(field reply)
conversation=$(field conversation_id)
if [ "$code" != 200 ] || [ -z "$reply" ] || [ -z "$conversation" ]; then
    fail "a turn is answered"
fi
cp "$tmp/body" "$tmp/first"
ok "a turn is answered: $reply"

second=$(new_id second)
call POST /v1/turn "{\"turn_id\":\"$second\",\"text\":\"And once more.\",\"conversation_id\":\"$conversation\",\"client\":\"smoke\"}"
if [ "$code" != 200 ] || [ "$(field conversation_id)" != "$conversation" ]; then
    fail "a follow-up stays in the same conversation"
fi
ok "a follow-up stays in the same conversation"

call POST /v1/turn "$ask"
if [ "$code" != 200 ] || ! cmp -s "$tmp/first" "$tmp/body"; then
    fail "a repeated turn_id gets the stored answer"
fi
ok "a repeated turn_id gets the stored answer"

call POST /v1/turn "{\"turn_id\":\"$(new_id empty)\",\"text\":\"\"}"
if [ "$code" != 400 ] || [ -z "$(field spoken)" ]; then
    fail "an empty turn is a spoken bad request"
fi
ok "an empty turn is a spoken bad request"

call POST /v1/turn "{\"turn_id\":\"$(new_id secret)\",\"text\":\"smoke test, not a passphrase\",\"secret\":true}"
case "$code:$(field reply)" in
    "200:I lost track"*) ok "a turn marked secret is not passed on" ;;
    *) fail "a turn marked secret is not passed on" ;;
esac

call POST "/v1/conversations/$conversation/end"
[ "$code" = 204 ] || fail "ending the conversation answers 204"
ok "ending the conversation answers 204"

printf 'All checks passed.\n'
