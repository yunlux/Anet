#!/usr/bin/env bash
set -euo pipefail

umask 077

EXPECTED_WHEEL_SHA256=""
LABEL=""
PORT="4246"
ADVERTISE=""
LAN_ZONE=""
WHEEL=""
ANET_ROOT="${ANET_ROOT:-$HOME/Library/Application Support/Anet}"
ALLOW_EXISTING="0"

usage() {
  cat <<'EOF'
Usage:
  bootstrap-macos.sh <anet-wheel> --sha256 <HEX> --advertise <LAN-IP> \
    --lan-zone <OPAQUE-ZONE> --label <NODE-LABEL> [--port 4246] \
    [--allow-existing]

The script verifies the pinned wheel, installs it into an isolated venv,
initializes a new node if needed, and exports a public Peer Card. It does not
add peers, expose a public address, read Agent-runtime secrets, or start a daemon.
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

WHEEL=$1
shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    --advertise)
      [[ $# -ge 2 ]] || { echo "--advertise requires a value" >&2; exit 2; }
      ADVERTISE=$2
      shift 2
      ;;
    --sha256)
      [[ $# -ge 2 ]] || { echo "--sha256 requires a value" >&2; exit 2; }
      EXPECTED_WHEEL_SHA256=$(printf '%s' "$2" | tr '[:lower:]' '[:upper:]')
      shift 2
      ;;
    --lan-zone)
      [[ $# -ge 2 ]] || { echo "--lan-zone requires a value" >&2; exit 2; }
      LAN_ZONE=$2
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || { echo "--port requires a value" >&2; exit 2; }
      PORT=$2
      shift 2
      ;;
    --label)
      [[ $# -ge 2 ]] || { echo "--label requires a value" >&2; exit 2; }
      LABEL=$2
      shift 2
      ;;
    --allow-existing)
      ALLOW_EXISTING="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -f "$WHEEL" ]] || { echo "wheel not found: $WHEEL" >&2; exit 1; }
[[ "$EXPECTED_WHEEL_SHA256" =~ ^[0-9A-F]{64}$ ]] || {
  echo "--sha256 must be the independently verified 64-character artifact hash" >&2
  exit 2
}
[[ -n "$ADVERTISE" ]] || { echo "--advertise LAN IP is required" >&2; exit 2; }
[[ "$LABEL" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || {
  echo "--label is required and must be 1-64 safe characters" >&2
  exit 2
}
[[ "$LAN_ZONE" =~ ^[A-Za-z0-9_-]{8,64}$ ]] || {
  echo "--lan-zone must be an opaque 8-64 character value" >&2
  exit 2
}
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1024 && PORT <= 65535 )) || {
  echo "port must be between 1024 and 65535" >&2
  exit 2
}

if command -v shasum >/dev/null 2>&1; then
  ACTUAL_SHA256=$(shasum -a 256 "$WHEEL" | awk '{print toupper($1)}')
else
  echo "shasum is required to verify the release artifact" >&2
  exit 1
fi
[[ "$ACTUAL_SHA256" == "$EXPECTED_WHEEL_SHA256" ]] || {
  echo "wheel SHA-256 mismatch" >&2
  echo "expected: $EXPECTED_WHEEL_SHA256" >&2
  echo "actual:   $ACTUAL_SHA256" >&2
  exit 1
}

PYTHON=${PYTHON:-python3}
"$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || {
  echo "Python 3.11 or newer is required" >&2
  exit 1
}

VENV="$ANET_ROOT/venv"
NODE_HOME="$ANET_ROOT/nodes/$LABEL"
PUBLIC_DIR="$ANET_ROOT/public"

DEFAULT_ANET_ROOT="$HOME/Library/Application Support/Anet"
TARGET_REUSABLE="0"
if [[ -x "$VENV/bin/python" && -f "$NODE_HOME/config.json" ]]; then
  TARGET_REUSABLE="1"
fi

FOREIGN_ROOTS=""
if [[ "$ANET_ROOT" != "$DEFAULT_ANET_ROOT" ]]; then
  if [[ -e "$DEFAULT_ANET_ROOT/venv" || -d "$DEFAULT_ANET_ROOT/nodes" ]]; then
    FOREIGN_ROOTS="$DEFAULT_ANET_ROOT"
  fi
fi

TARGET_MARKERS=""
if [[ -e "$VENV" || -d "$ANET_ROOT/nodes" ]]; then
  TARGET_MARKERS="$ANET_ROOT"
fi
AHUB_ROOTS=""
for candidate in \
  "$ANET_ROOT/ahub" \
  "$ANET_ROOT/ahub-data" \
  "$HOME/Library/Application Support/Ahub"; do
  if [[ -f "$candidate/ahub.sqlite3" || -f "$candidate/control.sqlite3" ]]; then
    AHUB_ROOTS="${AHUB_ROOTS:+$AHUB_ROOTS }$candidate"
  fi
done
ACTIVE_ANET=""
if command -v pgrep >/dev/null 2>&1; then
  ACTIVE_ANET=$(pgrep -af 'python.*-m anet|anet serve|anet-fabric' || true)
fi
if command -v launchctl >/dev/null 2>&1; then
  if launchctl print "gui/$(id -u)/net.anet.supervisor" >/dev/null 2>&1; then
    ACTIVE_ANET="${ACTIVE_ANET:+$ACTIVE_ANET }net.anet.supervisor"
  fi
fi

echo "Anet install preflight: target=$ANET_ROOT reusable=$TARGET_REUSABLE" >&2
if [[ -n "$AHUB_ROOTS" ]]; then
  echo "Anet install preflight: existing Ahub data=$AHUB_ROOTS" >&2
fi
if [[ "$ALLOW_EXISTING" != "1" && "$TARGET_REUSABLE" != "1" && \
      ( -n "$FOREIGN_ROOTS" || -n "$TARGET_MARKERS" || -n "$ACTIVE_ANET" ) ]]; then
  [[ -n "$FOREIGN_ROOTS" ]] && echo "existing roots: $FOREIGN_ROOTS" >&2
  [[ -n "$TARGET_MARKERS" ]] && echo "incomplete target: $TARGET_MARKERS" >&2
  [[ -n "$ACTIVE_ANET" ]] && echo "active Anet supervisor/process detected" >&2
  echo "Anet install preflight stopped; use the existing target or --allow-existing" >&2
  exit 17
fi

mkdir -p "$ANET_ROOT" "$PUBLIC_DIR"

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check "$WHEEL"

if [[ ! -f "$NODE_HOME/config.json" ]]; then
  "$VENV/bin/python" -m anet --home "$NODE_HOME" init \
    --label "$LABEL" \
    --host "0.0.0.0" \
    --port "$PORT" \
    --locator-context "lan:$LAN_ZONE" \
    --advertise "tls://$ADVERTISE:$PORT?scope=lan&zone=$LAN_ZONE&priority=20"
fi

CARD="$PUBLIC_DIR/$LABEL.card.json"
"$VENV/bin/python" -m anet --home "$NODE_HOME" doctor
"$VENV/bin/python" -m anet --home "$NODE_HOME" card --out "$CARD"

cat <<EOF
Anet node is prepared but not started.
Node home: $NODE_HOME
Public Peer Card: $CARD

Review a signed PairOffer received through the authenticated NATS channel,
then explicitly accept it and return the generated response before starting:
  "$VENV/bin/python" -m anet --home "$NODE_HOME" pair-accept <peer.offer.json> --out "$PUBLIC_DIR/$LABEL.response.json"
  "$VENV/bin/python" -m anet --home "$NODE_HOME" serve
EOF
