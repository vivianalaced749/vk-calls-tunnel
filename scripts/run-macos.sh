#!/bin/bash
# =============================================================
#  macOS launcher for vk-tunnel-client
#
#  Solves the routing loop: when WireGuard routes 0.0.0.0/0
#  through the tunnel, TURN server traffic also gets captured,
#  killing the tunnel itself. This script adds explicit routes
#  for TURN servers through the real gateway BEFORE WireGuard
#  is activated.
#
#  Usage:
#    bash scripts/run-macos.sh \
#      -peer <server_ip:port> \
#      -vk-link "<vk_call_url>" \
#      [-n 4] [-no-dtls] [-tcp=false]
# =============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLIENT_BIN="${SCRIPT_DIR}/../vk-tunnel-client-macos-arm64"

# Check for patched client first
if [[ -f "${SCRIPT_DIR}/../vk-tunnel-client-patched" ]]; then
    CLIENT_BIN="${SCRIPT_DIR}/../vk-tunnel-client-patched"
fi

if [[ ! -x "$CLIENT_BIN" ]]; then
    echo "ERROR: Client binary not found at $CLIENT_BIN"
    echo "Build it first: go build -o vk-tunnel-client-patched ./cmd/client/"
    exit 1
fi

# Get the default gateway before WireGuard messes with routing
DEFAULT_GW=$(route -n get default 2>/dev/null | awk '/gateway:/{print $2}')
if [[ -z "$DEFAULT_GW" ]]; then
    echo "ERROR: Cannot determine default gateway"
    exit 1
fi
echo "[*] Default gateway: $DEFAULT_GW"

# Start the tunnel client, capture output to extract TURN IPs
LOGFILE=$(mktemp /tmp/vk-tunnel-XXXXXX.log)
echo "[*] Starting tunnel client..."
"$CLIENT_BIN" "$@" > "$LOGFILE" 2>&1 &
CLIENT_PID=$!

# Wait for streams to be ready
sleep 5

if ! kill -0 "$CLIENT_PID" 2>/dev/null; then
    echo "ERROR: Client crashed. Log:"
    cat "$LOGFILE"
    exit 1
fi

# Extract TURN server IPs from log
TURN_IPS=$(grep -oE 'via [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$LOGFILE" | awk '{print $2}' | sort -u)

if [[ -z "$TURN_IPS" ]]; then
    echo "WARNING: Could not extract TURN server IPs from log"
    cat "$LOGFILE"
else
    echo "[*] TURN servers detected:"
    ADDED_ROUTES=()
    for ip in $TURN_IPS; do
        echo "    $ip → routing via $DEFAULT_GW (bypass tunnel)"
        sudo route -n add "$ip/32" "$DEFAULT_GW" 2>/dev/null || \
        sudo route -n change "$ip/32" "$DEFAULT_GW" 2>/dev/null || true
        ADDED_ROUTES+=("$ip")
    done
    echo ""
fi

# Also route the peer server IP directly (extract from -peer arg)
PEER_IP=""
for arg in "$@"; do
    if [[ "$arg" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+$ ]]; then
        PEER_IP="${arg%%:*}"
    fi
done
# Try to get it from the previous argument
PREV=""
for arg in "$@"; do
    if [[ "$PREV" == "-peer" ]]; then
        PEER_IP="${arg%%:*}"
    fi
    PREV="$arg"
done

if [[ -n "$PEER_IP" ]]; then
    echo "[*] Peer server $PEER_IP → routing via $DEFAULT_GW"
    sudo route -n add "$PEER_IP/32" "$DEFAULT_GW" 2>/dev/null || \
    sudo route -n change "$PEER_IP/32" "$DEFAULT_GW" 2>/dev/null || true
    ADDED_ROUTES+=("$PEER_IP")
fi

echo ""
echo "[*] Tunnel client running (PID $CLIENT_PID)"
echo "[*] Log: $LOGFILE"
echo ""
echo "============================================"
echo "  Now activate WireGuard and test:"
echo "    curl https://ifconfig.me"
echo "============================================"
echo ""
echo "Press Ctrl+C to stop and clean up routes."
echo ""

# Tail the log
cleanup() {
    echo ""
    echo "[*] Stopping tunnel client (PID $CLIENT_PID)..."
    kill "$CLIENT_PID" 2>/dev/null || true
    wait "$CLIENT_PID" 2>/dev/null || true

    echo "[*] Cleaning up routes..."
    for ip in "${ADDED_ROUTES[@]}"; do
        sudo route -n delete "$ip/32" 2>/dev/null || true
    done

    echo "[*] Done. Don't forget to deactivate WireGuard!"
    rm -f "$LOGFILE"
}
trap cleanup EXIT INT TERM

tail -f "$LOGFILE"
