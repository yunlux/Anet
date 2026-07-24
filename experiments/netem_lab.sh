#!/usr/bin/env bash
set -euo pipefail

# Run from WSL/Linux. The script creates a temporary network namespace and
# veth pair, injects netem faults, and removes every kernel object on exit.

PY=${ANET_PYTHON:-"$HOME/.local/anet/current/venv/bin/python"}
RESULTS=${1:-"$PWD/runtime/experiments"}
RESULTS=$(mkdir -p "$RESULTS" && realpath "$RESULTS")
RUN_ID=${ANET_LAB_RUN_ID:-"$(date -u +%Y%m%dT%H%M%SZ)"}
ROOT=$(mktemp -d /tmp/anet-netem.XXXXXX)
NS="anetns$$"
VH="ah$$"
VN="an$$"
OCTET=$((50 + ($$ % 150)))
HOST_IP="10.203.${OCTET}.1"
NODE_IP="10.203.${OCTET}.2"
CIDR="10.203.${OCTET}.0/24"
A="$ROOT/a"
B="$ROOT/b"
DROP="$ROOT/drop"
RUN_USER=$(id -un)

cleanup() {
  set +e
  for pid in $(sudo ip netns pids "$NS" 2>/dev/null); do
    sudo kill "$pid" 2>/dev/null
  done
  sudo ip netns del "$NS" 2>/dev/null
  sudo ip link del "$VH" 2>/dev/null
  case "$ROOT" in
    /tmp/anet-netem.*) sudo rm -rf -- "$ROOT" ;;
    *) printf 'Refusing unexpected cleanup path: %s\n' "$ROOT" >&2 ;;
  esac
}
trap cleanup EXIT INT TERM

if [[ ! -x "$PY" ]]; then
  printf 'Anet Python is unavailable: %s\n' "$PY" >&2
  exit 1
fi
if ip route show table all | grep -Fq "$CIDR"; then
  printf 'Chosen lab subnet is already in use: %s\n' "$CIDR" >&2
  exit 1
fi

mkdir -p "$DROP"
sudo ip netns add "$NS"
sudo ip link add "$VH" type veth peer name "$VN"
sudo ip link set "$VN" netns "$NS"
sudo ip addr add "$HOST_IP/24" dev "$VH"
sudo ip link set "$VH" up
sudo ip netns exec "$NS" ip addr add "$NODE_IP/24" dev "$VN"
sudo ip netns exec "$NS" ip link set "$VN" up
sudo ip netns exec "$NS" ip link set lo up

"$PY" -m anet --home "$A" init \
  --label netem-a --host "$HOST_IP" --port 47401 \
  --advertise "tls://$HOST_IP:47401" >/dev/null
"$PY" -m anet --home "$B" init \
  --label netem-b --host "$NODE_IP" --port 47402 \
  --advertise "tls://$NODE_IP:47402" >/dev/null
"$PY" -m anet --home "$A" card --keys-only --out "$ROOT/a.keys.json" >/dev/null
"$PY" -m anet --home "$B" card --out "$ROOT/b.card.json" >/dev/null
"$PY" -m anet --home "$A" peer-add "$ROOT/b.card.json" >/dev/null
"$PY" -m anet --home "$B" peer-add "$ROOT/a.keys.json" >/dev/null
AID=$("$PY" -m anet --home "$A" status | "$PY" -c \
  'import json,sys; print(json.load(sys.stdin)["node_id"])')
BID=$("$PY" -m anet --home "$B" status | "$PY" -c \
  'import json,sys; print(json.load(sys.stdin)["node_id"])')
"$PY" -m anet --home "$A" carrier-add "$DROP" \
  --name netem-drop --peer "$BID" --mode fallback \
  --interval 0.5 --retry-seconds 0 >/dev/null
"$PY" -m anet --home "$B" carrier-add "$DROP" \
  --name netem-drop --peer "$AID" --mode fallback \
  --interval 0.5 --retry-seconds 0 >/dev/null
"$PY" -m anet --home "$A" routing-config \
  --no-listen --failure-threshold 2 --recovery-threshold 3 --cooldown 0 >/dev/null
"$PY" -m anet --home "$B" routing-config \
  --no-direct --failure-threshold 2 --recovery-threshold 3 --cooldown 0 >/dev/null

sudo ip netns exec "$NS" sudo -u "$RUN_USER" \
  "$PY" -m anet --home "$B" serve >"$ROOT/b.stdout" 2>"$ROOT/b.stderr" &
for _ in $(seq 1 50); do
  if sudo ip netns exec "$NS" ss -ltn | grep -q 47402; then
    break
  fi
  sleep 0.1
done
sudo ip netns exec "$NS" ss -ltn | grep -q 47402

printf '%s\n' 'BASELINE'
"$PY" -m anet --home "$A" benchmark "$BID" \
  --count 5 --spacing 0.1 --timeout 10 \
  --out "$RESULTS/${RUN_ID}-baseline.jsonl"

sudo ip netns exec "$NS" tc qdisc add dev "$VN" root \
  netem delay 120ms 30ms distribution normal
printf '%s\n' 'DELAY_120MS_JITTER_30MS'
"$PY" -m anet --home "$A" benchmark "$BID" \
  --count 5 --spacing 0.1 --timeout 10 \
  --out "$RESULTS/${RUN_ID}-delay120-jitter30.jsonl"

sudo ip netns exec "$NS" tc qdisc replace dev "$VN" root \
  netem delay 50ms 10ms loss 5%
printf '%s\n' 'DELAY_50MS_LOSS_5PCT'
"$PY" -m anet --home "$A" benchmark "$BID" \
  --count 8 --spacing 0.1 --timeout 15 --min-success-rate 0.75 \
  --out "$RESULTS/${RUN_ID}-delay50-loss5.jsonl"

sudo ip netns exec "$NS" tc qdisc replace dev "$VN" root netem delay 50ms
sudo tc qdisc add dev "$VH" root netem delay 50ms rate 1mbit
printf '%s\n' 'RATE_1MBIT_PAYLOAD_262144_BYTES'
"$PY" -m anet --home "$A" benchmark "$BID" \
  --count 2 --spacing 0.1 --timeout 20 --qos bulk --payload-bytes 262144 \
  --out "$RESULTS/${RUN_ID}-rate1mbit-payload262144.jsonl"
sudo tc qdisc del dev "$VH" root

sudo ip netns exec "$NS" tc qdisc replace dev "$VN" root netem loss 100%
printf '%s\n' 'BLACKHOLE_100PCT_WITH_FALLBACK'
"$PY" -m anet --home "$A" probe "$BID" --qos control --timeout 25 \
  | tee "$RESULTS/${RUN_ID}-blackhole.json"

sudo ip netns exec "$NS" tc qdisc del dev "$VN" root
printf '%s\n' 'QDISC_RESTORED'
sudo ip netns exec "$NS" tc qdisc show dev "$VN"
