#!/bin/sh
# Build dist/setup-hey-claude-relay.sh: the installer header, then this app as a base64 tarball
# after __PAYLOAD_BELOW__. Only the files listed here go in: never relay.toml, .env or tests.
#
#   sh installer/build.sh             writes dist/setup-hey-claude-relay.sh
#   DIST=/some/dir sh installer/build.sh
set -eu
cd "$(dirname "$0")/.."
DIST="${DIST:-dist}"
STAGE=$(mktemp -d /tmp/hey-claude-relay-stage.XXXXXX 2>/dev/null || mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/hey-claude-relay"
mkdir -p "$DIST" "$APP/scripts" "$APP/installer"
cp -r pyproject.toml uv.lock LICENSE README.md relay.example.toml src "$APP/"
cp scripts/smoke.sh "$APP/scripts/"
cp installer/hey-claude-relay.initd installer/hey-claude-relay.wrapper "$APP/installer/"
find "$APP" \( -name __pycache__ -o -name '*.egg-info' \) -exec rm -rf {} \; 2>/dev/null || true
find "$APP" -name '*.pyc' -exec rm -f {} \; 2>/dev/null || true

# The commit /healthz reports, and the uv the image is built with (one pin, in the Dockerfile).
commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)
[ -z "$(git status --porcelain 2>/dev/null)" ] || commit="$commit-dirty"
echo "$commit" > "$APP/COMMIT"
uv_version=$(sed -n 's|.*ghcr\.io/astral-sh/uv:\([0-9.]*\)@.*|\1|p' Dockerfile | head -n1)
[ -n "$uv_version" ] || { echo "build.sh: no uv version found in Dockerfile" >&2; exit 1; }
echo "$uv_version" > "$APP/UV_VERSION"

# BusyBox tar has no --owner/--group.
TAR_EXTRA=""
if tar --owner=0 --group=0 --help >/dev/null 2>&1; then
  TAR_EXTRA="--owner=0 --group=0"
fi

OUT="$DIST/setup-hey-claude-relay.sh"
{
  cat installer/setup-head.sh
  echo "__PAYLOAD_BELOW__"
  # shellcheck disable=SC2086  # TAR_EXTRA is empty or two flags: it must word-split
  ( cd "$STAGE" && tar $TAR_EXTRA -czf - hey-claude-relay ) | base64
} > "$OUT"
chmod +x "$OUT"
sh -n "$OUT"
echo "Built $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes, commit $commit)"
