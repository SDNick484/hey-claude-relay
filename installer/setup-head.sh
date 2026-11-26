#!/bin/sh
# hey-claude-relay installer for Alpine Linux (OpenRC). Re-runnable: upgrades the relay in place,
# never replaces relay.toml, and only adds missing settings to /etc/conf.d/hey-claude-relay.
#
#   sh setup-hey-claude-relay.sh                    install or upgrade
#   RELAY_PORT=8788 sh setup-hey-claude-relay.sh    another port (first install; later runs keep it)
#   sh setup-hey-claude-relay.sh --uninstall        remove the relay and its service; keeps the config
#
# Alpine's own Python may be older than the relay needs, so uv (the version the Docker image uses)
# installs Python 3.13 and the exact dependency tree in uv.lock. That step needs PyPI and GitHub.
set -eu
APP_DIR=/opt/hey-claude-relay
RUNTIME_DIR=/opt/hey-claude-relay-runtime   # uv, the Python it installs, and its cache: kept across upgrades
CONF_DIR=/etc/hey-claude-relay              # relay.toml, and voice.md if you add one
CONF=/etc/conf.d/hey-claude-relay           # settings and secrets for the service, root only
OPS_CONF="${OPS_CONF:-/etc/conf.d/ops}"
LOG=/var/log/hey-claude-relay.log
SVC=hey-claude-relay
SVC_USER=relay
PYTHON_VERSION=3.13                         # what the Docker image runs
PORT="${RELAY_PORT:-8787}"

if [ "$(id -u)" != 0 ]; then echo "Run as root"; exit 1; fi
command -v apk >/dev/null || { echo "This installer is for Alpine (apk)."; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  rc-service "$SVC" stop 2>/dev/null || true
  rc-update del "$SVC" default 2>/dev/null || true
  rm -f "/etc/init.d/$SVC" /usr/local/bin/hey-claude-relay /etc/logrotate.d/hey-claude-relay
  rm -rf "$APP_DIR" "$RUNTIME_DIR"
  echo "Removed the relay, its service and its Python. Kept: $CONF_DIR, $CONF and $LOG."
  exit 0
fi

# The value a setting has once the file is read as the service reads it (last line wins, quotes
# removed). Only for settings that are not secret.
# shellcheck disable=SC1090  # the conf file is read as the service reads it
conf_val() { ( set +u; . "$CONF" >/dev/null 2>&1; eval "printf '%s' \"\${$1:-}\"" ); }

# an existing install keeps its port
if [ -f "$CONF" ]; then p=$(conf_val RELAY_PORT); PORT=${p:-$PORT}; fi

echo "==> Installing packages"
apk add --no-cache python3 py3-pip ca-certificates >/dev/null

WAS_RUNNING=0
[ -x "/etc/init.d/$SVC" ] && rc-service "$SVC" status >/dev/null 2>&1 && WAS_RUNNING=1
IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
IP=${IP:-127.0.0.1}

echo "==> Creating the service user"
# BusyBox adduser -S puts a new user in "nogroup" unless told otherwise, so make its group first.
grep -q "^$SVC_USER:" /etc/group || addgroup -S "$SVC_USER"
id "$SVC_USER" >/dev/null 2>&1 || adduser -S -D -H -h /nonexistent -s /sbin/nologin -G "$SVC_USER" "$SVC_USER"

echo "==> Unpacking the relay"
TMP=$(mktemp -d /tmp/hey-claude-relay.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
sed -n '/^__PAYLOAD_BELOW__$/,$p' "$0" | tail -n +2 | base64 -d | ( cd "$TMP" && tar -xzf - )
COMMIT=$(cat "$TMP/hey-claude-relay/COMMIT")
UV_VERSION=$(cat "$TMP/hey-claude-relay/UV_VERSION")

echo "==> Installing uv $UV_VERSION"
mkdir -p "$RUNTIME_DIR"
UV="$RUNTIME_DIR/uv/bin/uv"
if [ "$("$UV" --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]; then
  rm -rf "$RUNTIME_DIR/uv"
  python3 -m venv "$RUNTIME_DIR/uv"
  "$RUNTIME_DIR/uv/bin/pip" install --no-cache-dir --disable-pip-version-check -q "uv==$UV_VERSION"
fi
export UV_PYTHON_INSTALL_DIR="$RUNTIME_DIR/python" UV_CACHE_DIR="$RUNTIME_DIR/cache"
export UV_PYTHON_PREFERENCE=only-managed UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_NO_PROGRESS=1

# From here the running relay is stopped. If the new one cannot be set up, the old one goes back.
if [ "$WAS_RUNNING" = 1 ]; then rc-service "$SVC" stop >/dev/null 2>&1 || true; fi
if netstat -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$PORT\$"; then
  echo "Port $PORT is already in use by something else. Re-run with RELAY_PORT=<free port>."
  if [ "$WAS_RUNNING" = 1 ]; then rc-service "$SVC" start >/dev/null 2>&1 || true; fi
  exit 1
fi
rm -rf "$APP_DIR.old"
if [ -d "$APP_DIR" ]; then mv "$APP_DIR" "$APP_DIR.old"; fi
mv "$TMP/hey-claude-relay" "$APP_DIR"
chown -R root:root "$APP_DIR"

rollback() {
  echo "    Could not install the relay's Python or dependencies (see above)."
  rm -rf "$APP_DIR"
  if [ -d "$APP_DIR.old" ]; then
    mv "$APP_DIR.old" "$APP_DIR"
    echo "    The previous version is back in place."
    if [ "$WAS_RUNNING" = 1 ]; then rc-service "$SVC" start >/dev/null 2>&1 || true; fi
  fi
  exit 1
}

echo "==> Installing Python $PYTHON_VERSION and the dependencies in uv.lock"
"$UV" sync --project "$APP_DIR" --locked --no-dev --no-editable --python "$PYTHON_VERSION" --quiet || rollback
"$APP_DIR/.venv/bin/python" -c 'import hey_claude_relay' || rollback
rm -rf "$APP_DIR.old"
chmod -R a+rX "$RUNTIME_DIR/python"   # the service user runs this Python

install -m 755 "$APP_DIR/installer/hey-claude-relay.initd" "/etc/init.d/$SVC"
install -m 755 "$APP_DIR/installer/hey-claude-relay.wrapper" /usr/local/bin/hey-claude-relay

# ---------------------------------------------------------------- config
echo "==> Configuration"
mkdir -p "$CONF_DIR"
NEW_TOML=0
[ -f "$CONF_DIR/relay.toml" ] || { cp "$APP_DIR/relay.example.toml" "$CONF_DIR/relay.toml"; NEW_TOML=1; }
# The service user reads relay.toml (and a prompt file beside it), never writes it.
chown -R "root:$SVC_USER" "$CONF_DIR"
chmod -R u=rwX,g=rX,o= "$CONF_DIR"

add_setting() {  # add_setting KEY VALUE COMMENT: only if KEY is not in $CONF yet
  grep -Eq "^[[:space:]]*(export[[:space:]]+)?$1=" "$CONF" 2>/dev/null && return 0
  printf '\n# %s\n%s=%s\n' "$3" "$1" "$2" >> "$CONF"
  ADDED="${ADDED:-} $1"
}

if [ ! -f "$CONF" ]; then
  cat > "$CONF" <<'EOF'
# hey-claude-relay: settings and secrets for /etc/init.d/hey-claude-relay and the
# hey-claude-relay command. Root only. After editing: rc-service hey-claude-relay restart
#
# One KEY=value per line. Single-quote a value that has spaces, $, # or a quote in it.
# Every line here reaches the relay, so add one for each server's token_env, e.g.
#   COOKBOOKS_MCP_TOKEN=...
EOF
fi
add_setting RELAY_CONFIG "$CONF_DIR/relay.toml" "The relay's settings: servers, rules, callers and profiles"
add_setting RELAY_BIND "$IP" "Address the relay listens on, where the tunnel's route points (127.0.0.1 if cloudflared runs on this host, outside Docker)"
add_setting RELAY_PORT "$PORT" "Port the relay listens on"
add_setting ANTHROPIC_API_KEY "" "Anthropic API key the turns are billed to (required)"
add_setting CF_ACCESS_CLIENT_ID "" "The relay's own Access service token, shown to servers with auth = \"cf-service-token\". Not a caller's token"
add_setting CF_ACCESS_CLIENT_SECRET "" "That token's Client Secret"
add_setting RELAY_PASSPHRASE_HASH "" "Needed when a [[gate]] asks for a passphrase: run hey-claude-relay hash-passphrase and paste the value here"
chmod 600 "$CONF"; chown root:root "$CONF"

# ---------------------------------------------------------------- health check and log rotation
add_health_check() {  # add_health_check "Name|url|service" service  (sets HC_ADDED / HC_MANUAL)
  [ -f "$OPS_CONF" ] || return 0
  grep -Eq "\|$2([^-A-Za-z0-9_]|\$)" "$OPS_CONF" && return 0
  if ! grep -q '^HEALTH_CHECKS=' "$OPS_CONF"; then
    printf '\n# Apps checked every 15 min (name|url|service); added by setup-hey-claude-relay.sh\nHEALTH_CHECKS="%s"\n' "$1" >> "$OPS_CONF"
    HC_ADDED="${HC_ADDED:-} $2"
    return 0
  fi
  # append inside the existing (usually multi-line) HEALTH_CHECKS="..." value, before its closing quote
  awk -v add="$1" '
    !done && /^HEALTH_CHECKS="/ { inb = 1; if ($0 == "HEALTH_CHECKS=\"") { print; next } }
    inb && /"[ \t]*$/ { sub(/"[ \t]*$/, ""); print; print add "\""; inb = 0; done = 1; next }
    { print }' "$OPS_CONF" > "$OPS_CONF.new"
  if grep -Eq "\|$2([^-A-Za-z0-9_]|\$)" "$OPS_CONF.new" && sh -n "$OPS_CONF.new" 2>/dev/null; then
    cat "$OPS_CONF.new" > "$OPS_CONF"; HC_ADDED="${HC_ADDED:-} $2"
  else
    HC_MANUAL="${HC_MANUAL:-}
      $1"
  fi
  rm -f "$OPS_CONF.new"
}

# Only when logrotate is already installed and nothing else names this log.
install_logrotate() {
  command -v logrotate >/dev/null 2>&1 || return 0
  lr_dir="${LOGROTATE_D:-/etc/logrotate.d}"
  [ -d "$lr_dir" ] || return 0
  grep -qs -e "$LOG" "${LOGROTATE_CONF:-/etc/logrotate.conf}" "$lr_dir"/* && return 0
  printf '%s\n' "$LOG {" '    weekly' '    rotate 8' '    missingok' '    notifempty' '    compress' \
    '    delaycompress' '    copytruncate' '}' > "$lr_dir/hey-claude-relay"
  echo "    Added $lr_dir/hey-claude-relay (weekly, keep 8)"
}
install_logrotate

# ---------------------------------------------------------------- start
BIND=$(conf_val RELAY_BIND); BIND=${BIND:-127.0.0.1}
PORT=$(conf_val RELAY_PORT); PORT=${PORT:-8787}
HOST=$BIND; [ "$HOST" = 0.0.0.0 ] && HOST=127.0.0.1
HEALTH="http://$HOST:$PORT/healthz"
STATE=unconfigured
echo "==> Checking the configuration"
# check-config names keys and variables, never their values, so its one line is safe to show.
if CHECK=$(/usr/local/bin/hey-claude-relay check-config 2>&1); then
  echo "==> Starting the relay"
  rc-service hey-claude-relay start || true
  STATE=failed
  i=0
  while [ $i -lt 30 ]; do
    wget -qO- "$HEALTH" >/dev/null 2>&1 && { STATE=up; break; }
    i=$((i+1)); sleep 1
  done
  if [ "$STATE" = up ]; then
    rc-update add "$SVC" default >/dev/null 2>&1 || true
    add_health_check "Hey Claude relay|$HEALTH|$SVC" "$SVC"
  else
    echo "    The relay didn't answer on $HEALTH; the end of $LOG:"
    tail -n 20 "$LOG" 2>/dev/null || true
  fi
fi

echo
echo "hey-claude-relay $COMMIT"
[ -n "${ADDED:-}" ] && echo "New settings in $CONF:${ADDED}"
case "$STATE" in
  up)
    echo "The relay is running: $HEALTH"
    echo "  $(wget -qO- "$HEALTH" 2>/dev/null || true)"
    ;;
  failed)
    echo "The relay did not come up: see $LOG, fix, then rc-service $SVC restart"
    ;;
  *)
    echo "The relay is installed but NOT started. check-config says:"
    echo "  $CHECK"
    echo "Next:"
    [ "$NEW_TOML" = 1 ] && echo "  - vim $CONF_DIR/relay.toml       (a copy of the example: set [access], servers, rules)"
    echo "  - vim $CONF   (ANTHROPIC_API_KEY, the relay's CF_ACCESS_* token, server tokens)"
    echo "  - hey-claude-relay hash-passphrase   (if a gate asks for one; paste the value into $CONF)"
    echo "  - hey-claude-relay check-config      (prints ok: when it is usable)"
    echo "  - then run this installer again, which starts the relay and adds it to the default runlevel"
    ;;
esac
[ -n "${HC_ADDED:-}" ] && echo "Added to HEALTH_CHECKS in $OPS_CONF:${HC_ADDED}"
[ -n "${HC_MANUAL:-}" ] && echo "Couldn't edit HEALTH_CHECKS in $OPS_CONF: add these lines:${HC_MANUAL}"
echo "Tunnel: the relay's public hostname -> http://$BIND:$PORT"
echo "Config: $CONF_DIR/relay.toml and $CONF   Log: $LOG   App: $APP_DIR"
exit 0
