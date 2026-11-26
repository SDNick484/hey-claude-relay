#!/bin/sh
# End-to-end check of dist/setup-hey-claude-relay.sh in a throwaway Alpine container that boots
# OpenRC as PID 1, the way an LXC container does. Needs Docker and the network (apk, PyPI, uv's
# Python download). Placeholders only: nothing here reaches a real service.
#
#   sh installer/build.sh && sh installer/e2e.sh alpine:3.22
# shellcheck disable=SC2016  # in_ct's commands are expanded inside the container, not here
set -eu
cd "$(dirname "$0")/.."
IMAGE="${1:?usage: installer/e2e.sh ALPINE_IMAGE}"
NAME="relay-e2e-$$"
TAG="relay-e2e-base:$$"
SETUP=/root/setup-hey-claude-relay.sh
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; docker rmi "$TAG" >/dev/null 2>&1 || true' EXIT

step() { printf '\n=== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; docker exec "$NAME" sh -c 'tail -n 30 /var/log/hey-claude-relay.log' 2>/dev/null || true; exit 1; }
in_ct() { docker exec "$NAME" sh -c "$1"; }
health() { in_ct 'wget -qO- "http://$(. /etc/conf.d/hey-claude-relay; echo "$RELAY_BIND:$RELAY_PORT")/healthz"'; }

step "boot $IMAGE with OpenRC"
printf 'FROM %s\nRUN apk add --no-cache openrc && sed -i "s/^#rc_sys=\\"\\"/rc_sys=\\"docker\\"/" /etc/rc.conf\nCMD ["/sbin/init"]\n' "$IMAGE" |
  docker build -q -t "$TAG" - >/dev/null
docker run -d --name "$NAME" "$TAG" >/dev/null
i=0
until in_ct 'rc-status default >/dev/null 2>&1'; do i=$((i+1)); [ $i -lt 30 ] || fail "OpenRC did not boot"; sleep 1; done
docker cp dist/setup-hey-claude-relay.sh "$NAME:$SETUP"

step "first install: not started until configured"
out=$(in_ct "sh $SETUP" 2>&1) || { echo "$out"; fail "first install exited non-zero"; }
echo "$out" | tail -n 12
echo "$out" | grep -q "NOT started" || fail "an unconfigured relay was started"
in_ct 'test "$(stat -c %a:%U /etc/conf.d/hey-claude-relay)" = 600:root' || fail "conf file is not root-only"
in_ct 'test "$(stat -c %a:%U:%G /etc/hey-claude-relay/relay.toml)" = 640:root:relay' || fail "relay.toml permissions"

step "configure with placeholders, as CI's docker job does"
in_ct '
  c=/etc/conf.d/hey-claude-relay
  sed -i "s/^ANTHROPIC_API_KEY=\$/ANTHROPIC_API_KEY=placeholder-not-a-real-key/" $c
  sed -i "s/^CF_ACCESS_CLIENT_ID=\$/CF_ACCESS_CLIENT_ID=placeholder.access/" $c
  sed -i "s/^CF_ACCESS_CLIENT_SECRET=\$/CF_ACCESS_CLIENT_SECRET=placeholder-not-a-real-secret/" $c
  echo "COOKBOOKS_MCP_TOKEN=placeholder-not-a-real-token" >> $c
  h=$(printf "a throwaway phrase for ci\n" | hey-claude-relay hash-passphrase | cut -d= -f2-)
  sed -i "s/^RELAY_PASSPHRASE_HASH=\$/RELAY_PASSPHRASE_HASH=$h/" $c
  hey-claude-relay check-config' || fail "check-config with placeholders"

step "second run: starts it"
out=$(in_ct "sh $SETUP" 2>&1) || { echo "$out"; fail "second run exited non-zero"; }
echo "$out" | tail -n 4
echo "$out" | grep -q "The relay is running" || fail "the relay did not start"
in_ct 'rc-update show default | grep -q hey-claude-relay' || fail "not in the default runlevel"
in_ct 'ps -o user,args | grep -q "^relay .*-m hey_claude_relay serve"' || fail "not running as the relay user"
code=$(in_ct 'wget -S -qO- --post-data="{\"turn_id\":\"e2e\",\"text\":\"hi\"}" --header=content-type:application/json \
  "http://$(. /etc/conf.d/hey-claude-relay; echo "$RELAY_BIND:$RELAY_PORT")/v1/turn" 2>&1 | sed -n "s/^ *HTTP\/[0-9.]* \([0-9]*\).*/\1/p" | head -n1')
[ "$code" = 403 ] || fail "a turn without credentials answered $code, not 403"
health | grep -q '"status":"ok"' || fail "/healthz"
commit=$(sed -n '/^__PAYLOAD_BELOW__$/,$p' dist/setup-hey-claude-relay.sh | tail -n +2 | base64 -d | tar -xzOf - hey-claude-relay/COMMIT)
case "$commit" in
  *-dirty | unknown) echo "(built from an uncommitted tree: /healthz leaves the commit out)" ;;
  *) health | grep -q "\"commit\":\"$commit\"" || fail "/healthz does not name $commit" ;;
esac
in_ct '! grep -q placeholder /var/log/hey-claude-relay.log' || fail "a secret reached the log"

step "upgrade while running: keeps settings, comes back up"
out=$(in_ct "sh $SETUP" 2>&1) || { echo "$out"; fail "upgrade exited non-zero"; }
echo "$out" | grep -q "The relay is running" || fail "the relay did not come back after an upgrade"
echo "$out" | grep -q "New settings" && fail "an upgrade added settings that were already there"
in_ct 'test "$(grep -c "^ANTHROPIC_API_KEY=" /etc/conf.d/hey-claude-relay)" = 1' || fail "a setting was duplicated"

step "a broken upgrade puts the running version back"
in_ct "
  set -e; rm -rf /tmp/b && mkdir /tmp/b && cd /tmp/b
  sed -n '/^__PAYLOAD_BELOW__\$/,\$p' $SETUP | tail -n +2 | base64 -d | tar -xzf -
  sed -i 's/^dependencies = \[/dependencies = [\"no-such-package-for-relay-e2e==1\", /' hey-claude-relay/pyproject.toml
  sed -n '1,/^__PAYLOAD_BELOW__\$/p' $SETUP > /root/broken.sh
  tar -czf - hey-claude-relay | base64 >> /root/broken.sh"
if in_ct 'sh /root/broken.sh' >/dev/null 2>&1; then fail "a broken upgrade exited zero"; fi
in_ct 'rc-service hey-claude-relay status' >/dev/null 2>&1 || fail "the old relay was not restarted"
i=0
until health >/dev/null 2>&1; do i=$((i+1)); [ $i -lt 30 ] || fail "the old relay does not answer"; sleep 1; done

step "uninstall keeps the configuration"
in_ct "sh $SETUP --uninstall"
in_ct 'test ! -e /opt/hey-claude-relay && test ! -e /opt/hey-claude-relay-runtime && test ! -e /etc/init.d/hey-claude-relay' ||
  fail "uninstall left the app behind"
in_ct 'test -f /etc/hey-claude-relay/relay.toml && grep -q "^ANTHROPIC_API_KEY=placeholder" /etc/conf.d/hey-claude-relay' ||
  fail "uninstall removed the configuration"

printf '\nok: the installer works on %s\n' "$IMAGE"
