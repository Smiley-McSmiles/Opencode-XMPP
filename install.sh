#!/bin/sh
# opencode-xmpp installer — sets up the bridge on a fresh host.
#
#   ./install.sh                                  # interactive, installs into ~/.opencode-xmpp
#   ./install.sh --jid opencode@example.org --owner local@example.org
#   ./install.sh --service --jid … --owner …      # also install + start the systemd unit
#
# The opencode CLI is NOT installed or configured here: this script only needs it
# on PATH (or --binary). Run `opencode auth login` once for your model provider.
# Run again to upgrade.
set -eu

DIR="${OPENCODE_XMPP_DIR:-$HOME/.opencode-xmpp}"
VENV="${OPENCODE_XMPP_VENV:-$HOME/.opencode-xmpp/venv}"
BIN="${OPENCODE_XMPP_BIN:-$HOME/.local/bin}"
AGENTS_DIR="${OPENCODE_XMPP_AGENTS:-$HOME/.config/opencode/agents}"
PACKAGE=""
JID=""
OWNER=""
HOST=""
PORT="5222"
OPENCODE_BIN="${OPENCODE_BIN:-opencode}"
WORKSPACE="${OPENCODE_XMPP_WORKSPACE:-$HOME}"
UPLOAD=""
ROOM=""
NICK=""
DO_INIT=1
DO_SERVICE=0
DO_CHECK=1
FORCE=0

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need_value() { [ $# -ge 2 ] && [ -n "$2" ] || die "$1 needs a value"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) need_value "$@"; DIR="$2"; shift 2 ;;
        --venv) need_value "$@"; VENV="$2"; shift 2 ;;
        --bin) need_value "$@"; BIN="$2"; shift 2 ;;
        --agents-dir) need_value "$@"; AGENTS_DIR="$2"; shift 2 ;;
        --package) need_value "$@"; PACKAGE="$2"; shift 2 ;;
        --jid) need_value "$@"; JID="$2"; shift 2 ;;
        --owner) need_value "$@"; OWNER="$2"; shift 2 ;;
        --host) need_value "$@"; HOST="$2"; shift 2 ;;
        --port) need_value "$@"; PORT="$2"; shift 2 ;;
        --opencode) need_value "$@"; OPENCODE_BIN="$2"; shift 2 ;;
        --workspace) need_value "$@"; WORKSPACE="$2"; shift 2 ;;
        --upload-service) need_value "$@"; UPLOAD="$2"; shift 2 ;;
        --room) need_value "$@"; ROOM="$2"; shift 2 ;;
        --nick) need_value "$@"; NICK="$2"; shift 2 ;;
        --service) DO_SERVICE=1; shift ;;
        --skip-init) DO_INIT=0; shift ;;
        --no-check) DO_CHECK=0; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# 1. python ------------------------------------------------------------------
PY=python3
command -v "$PY" >/dev/null 2>&1 || die "python3 not found"
"$PY" - <<'EOF' || die "python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
EOF
say "python      $("$PY" -V 2>&1)"

# 2. opencode ----------------------------------------------------------------
if command -v "$OPENCODE_BIN" >/dev/null 2>&1; then
    say "opencode    $(command -v "$OPENCODE_BIN")"
elif [ -x "$OPENCODE_BIN" ]; then
    say "opencode    $OPENCODE_BIN"
else
    die "opencode CLI not found; install it first or pass --opencode /path/to/opencode"
fi

# 3. venv + package ----------------------------------------------------------
if [ -z "$PACKAGE" ]; then
    if ls "$here"/dist/opencode_xmpp-*.whl >/dev/null 2>&1; then
        PACKAGE=$(ls "$here"/dist/opencode_xmpp-*.whl | sort | tail -1)
    elif [ -f "$here/pyproject.toml" ]; then
        PACKAGE="$here"
    else
        die "no package found; pass --package <wheel|sdist|git-url|path>"
    fi
fi
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
say "installing  $PACKAGE"
"$VENV/bin/pip" install --quiet --upgrade "$PACKAGE"
say "installed   $("$VENV/bin/opencode-xmpp" --version)"

mkdir -p "$BIN"
ln -sf "$VENV/bin/opencode-xmpp" "$BIN/opencode-xmpp"
say "cli         $BIN/opencode-xmpp"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) say "note: add $BIN to PATH to run 'opencode-xmpp' directly" ;;
esac

# 4. agents ------------------------------------------------------------------
mkdir -p "$AGENTS_DIR"
for agent in "$here"/agents/*.md; do
    [ -f "$agent" ] || continue
    target="$AGENTS_DIR/$(basename "$agent")"
    if [ -f "$target" ] && [ "$FORCE" != 1 ]; then
        say "agent       keeping existing $target"
    else
        cp "$agent" "$target"
        say "agent       $target"
    fi
done

# 5. deployment directory ----------------------------------------------------
CONFIG="$DIR/opencode.toml"
if [ -f "$CONFIG" ]; then
    say "config      keeping existing $CONFIG"
    DO_INIT=0
fi

if [ "$DO_INIT" = 1 ]; then
    if [ -z "$JID" ]; then
        printf 'bot JID (e.g. opencode@example.org): '
        read -r JID
    fi
    if [ -z "$OWNER" ]; then
        printf 'admin JID (you): '
        read -r OWNER
    fi
    if [ -z "${OPENCODE_XMPP_PASSWORD:-}" ]; then
        printf 'XMPP password for %s: ' "$JID"
        stty -echo 2>/dev/null || true
        read -r OPENCODE_XMPP_PASSWORD
        stty echo 2>/dev/null || true
        printf '\n'
    fi
    export OPENCODE_XMPP_PASSWORD
    set -- init --dir "$DIR" --jid "$JID" --owner "$OWNER" --port "$PORT" \
        --binary "$OPENCODE_BIN" --workspace "$WORKSPACE" --agents-dir "$AGENTS_DIR"
    if [ -n "$HOST" ]; then set -- "$@" --host "$HOST"; fi
    if [ -n "$UPLOAD" ]; then set -- "$@" --upload-service "$UPLOAD"; fi
    if [ -n "$ROOM" ]; then set -- "$@" --room "$ROOM"; fi
    if [ -n "$NICK" ]; then set -- "$@" --nick "$NICK"; fi
    if [ "$FORCE" = 1 ]; then set -- "$@" --force; fi
    "$VENV/bin/opencode-xmpp" "$@"
fi

# 6. service -----------------------------------------------------------------
name=$(basename "$DIR" | sed 's/^\.//')
[ -n "$JID" ] && name=${JID%%@*}
[ -n "$name" ] || name=opencode-xmpp
if [ "$DO_SERVICE" = 1 ]; then
    unit_dir="$HOME/.config/systemd/user"
    mkdir -p "$unit_dir"
    "$VENV/bin/opencode-xmpp" unit --config "$CONFIG" --name "$name" \
        > "$unit_dir/opencode-xmpp-$name.service"
    systemctl --user daemon-reload
    systemctl --user enable --now "opencode-xmpp-$name"
    say "service     opencode-xmpp-$name started"
else
    say ""
    say "To run it as a service:"
    say "  opencode-xmpp unit --config $CONFIG --name $name > ~/.config/systemd/user/opencode-xmpp-$name.service"
    say "  systemctl --user daemon-reload && systemctl --user enable --now opencode-xmpp-$name"
fi

# 7. verification ------------------------------------------------------------
if [ "$DO_CHECK" = 1 ] && [ -f "$CONFIG" ]; then
    say ""
    "$VENV/bin/opencode-xmpp" check --config "$CONFIG" || true
    say ""
    say "If the check reports a missing agent, run: opencode-xmpp init --force ..."
    say "If the model fails, run: opencode auth login"
fi
