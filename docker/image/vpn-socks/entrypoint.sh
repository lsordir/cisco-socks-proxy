#!/bin/sh
set -e

# ---------------------------------------------------------------------------
#  OpenConnect VPN  +  Dante SOCKS5  +  tun2socks
#  Connects to Cisco AnyConnect VPN then exposes a SOCKS5 proxy on :1080
# ---------------------------------------------------------------------------

# ── Required environment ──────────────────────────────────────────────────
: "${ANYCONNECT_SERVER:?ANYCONNECT_SERVER is required}"
: "${ANYCONNECT_USER:?ANYCONNECT_USER is required}"
: "${ANYCONNECT_PASSWORD:?ANYCONNECT_PASSWORD is required}"

SOCKS_PORT="${SOCKS_PORT:-1080}"
VPN_INTERFACE="tun0"
TUN2SOCKS_INTERFACE="tun1"
TUN2SOCKS_SUBNET="198.18.0.1/15"
MAX_WAIT_ITERATIONS=75            # 75 x 2s = 150s max wait for tunnel

# Save original gateway BEFORE OpenConnect touches the routing table
ORIG_GW=$(ip route 2>/dev/null  | awk '/default via/ {print $3; exit}')
ORIG_IF=$(ip route 2>/dev/null  | awk '/default via/ {print $5; exit}')

# ── Helpers ───────────────────────────────────────────────────────────────
log()  { echo "[$(date '+%H:%M:%S')] $*" >&2; }
die()  { log "FATAL: $*"; exit 1; }

# ── Build OpenConnect credentials ─────────────────────────────────────────
build_credentials() {
  local creds="$1"
  # --passwd-on-stdin reads the password before connecting. The username
  # is supplied separately with --user and must not appear in this stream.
  printf '%s\n' "$ANYCONNECT_PASSWORD" > "$creds"
  # Preserve automatic certificate acceptance when no pin is configured.
  if [ -z "$ANYCONNECT_SERVERCERT" ]; then
    printf 'yes\n' >> "$creds"
  fi
}

# ── Detect tunnel IP ──────────────────────────────────────────────────────
get_tunnel_ip() {
  local ip

  # Try well-known tun/tap devices first
  for dev in tun0 tun1 tun2 tap0; do
    ip=$(ip -4 -o addr show "$dev" 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
    [ -n "$ip" ] && echo "$ip" && return
  done

  # Fallback: any tun/tap/vhost interface
  ip=$(ip -4 -o addr show 2>/dev/null | awk '
    $2 ~ /^tun|^tap|^vhost/ { gsub(/\/.*/,"",$4); print $4; exit }
  ')
  [ -n "$ip" ] && echo "$ip" && return

  # Last resort: parse openconnect log for "Configured as x.x.x.x"
  ip=$(grep -oE 'Configured as [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$OC_LOG" 2>/dev/null \
       | awk '{print $3}')
  [ -n "$ip" ] && echo "$ip"
}

# ── Wait for VPN tunnel ──────────────────────────────────────────────────
wait_for_tunnel() {
  log "Waiting for VPN tunnel (may take 30-60s)..."
  local i=0
  while [ $i -lt $MAX_WAIT_ITERATIONS ]; do
    if ! kill -0 "$OC_PID" 2>/dev/null; then
      log "OpenConnect exited before establishing the VPN tunnel; see its error above."
      return 1
    fi
    TUN_IP=$(get_tunnel_ip) || true
    [ -n "$TUN_IP" ] && return 0

    # Progress every 20s
    if [ $i -gt 0 ] && [ $((i % 10)) -eq 0 ]; then
      log "No tunnel yet (${i}x2s). Interfaces:"
      ip -4 -o addr show 2>/dev/null | sed 's/^/  /' >&2
    fi
    sleep 2
    i=$((i + 1))
  done
  return 1
}

# ── Set VPN as default route ─────────────────────────────────────────────
set_vpn_default_route() {
  # 1) Host route: VPN server itself must keep using the original gateway
  #    Otherwise changing default route kills the tunnel.
  local vpn_ip
  vpn_ip=$(getent hosts "$ANYCONNECT_SERVER" 2>/dev/null | awk '{print $1; exit}')
  if [ -n "$vpn_ip" ] && [ -n "$ORIG_GW" ]; then
    ip route add "$vpn_ip/32" via "$ORIG_GW" dev "$ORIG_IF" 2>/dev/null || true
    log "Host route: $vpn_ip via $ORIG_GW dev $ORIG_IF"
  fi

  # 2) Replace default route to go through VPN tunnel
  for dev in tun0 tun1; do
    if ip link show "$dev" up 2>/dev/null | grep -q "$dev"; then
      ip route replace default dev "$dev" metric 0 2>/dev/null || continue
      # Demote original default so VPN wins
      [ -n "$ORIG_GW" ] && [ -n "$ORIG_IF" ] \
        && ip route replace default via "$ORIG_GW" dev "$ORIG_IF" metric 200 2>/dev/null || true
      log "Default route via $dev (all traffic through VPN)"
      return 0
    fi
  done
  log "WARNING: could not set default route via tun; SOCKS traffic may bypass VPN"
  return 1
}

# ── Start Dante SOCKS5 server ────────────────────────────────────────────
start_dante() {
  # Dante "external" = the address used for outbound connections.
  # MUST be the tunnel IP so traffic goes through the VPN, not eth0.
  local dante_ip="$TUN_IP"

  # Fallback: if tunnel IP isn't bindable, use first non-lo (rare vhost-net case)
  if ! ip -4 -o addr show 2>/dev/null | grep -q "$dante_ip"; then
    dante_ip=$(ip -4 -o addr show 2>/dev/null \
               | awk '$2 !~ /^lo$/ { gsub(/\/.*/,"",$4); print $4; exit }')
    log "WARNING: tunnel IP not bindable, using $dante_ip (traffic may bypass VPN)"
  fi

  sed "s/EXTERNAL_IP_PLACEHOLDER/$dante_ip/" /etc/sockd.conf.template > /tmp/sockd.conf
  sockd -f /tmp/sockd.conf &
  SOCKD_PID=$!
  log "Dante SOCKS5 listening on :$SOCKS_PORT (external=$dante_ip)"
  sleep 1
}

# ── Start tun2socks ──────────────────────────────────────────────────────
start_tun2socks() {
  # tun2socks must bind to the ORIGINAL interface (eth0), not tun0.
  # Using tun0 would loop: tun2socks -> SOCKS -> tun0 -> tun2socks ...
  local bind_if="${ORIG_IF:-eth0}"

  ip tuntap add mode tun dev "$TUN2SOCKS_INTERFACE" 2>/dev/null || true
  ip addr add "$TUN2SOCKS_SUBNET" dev "$TUN2SOCKS_INTERFACE" 2>/dev/null || true
  ip link set dev "$TUN2SOCKS_INTERFACE" up 2>/dev/null || true

  tun2socks --device "$TUN2SOCKS_INTERFACE" \
            --proxy "socks5://127.0.0.1:$SOCKS_PORT" \
            --interface "$bind_if" \
            --loglevel info &
  TUN2_PID=$!
  log "tun2socks: $TUN2SOCKS_INTERFACE ($TUN2SOCKS_SUBNET) -> socks5://127.0.0.1:$SOCKS_PORT via $bind_if"
}

# ── Cleanup ───────────────────────────────────────────────────────────────
cleanup() {
  log "Shutting down..."
  kill $OC_PID $SOCKD_PID $TUN2_PID 2>/dev/null
  rm -f "$CREDS" "$OC_LOG"
  exit 0
}

# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════

CREDS=$(mktemp)
OC_LOG=$(mktemp)
trap cleanup TERM INT
trap 'rm -f "$CREDS" "$OC_LOG"' EXIT

# 1. Build credentials file
build_credentials "$CREDS"

# 2. Prepare tun device
ip tuntap add mode tun dev "$VPN_INTERFACE" 2>/dev/null || true

# 3. Build and launch OpenConnect
set -- --timestamp --user="$ANYCONNECT_USER" --passwd-on-stdin \
  --script=/vpnc-script.sh --interface="$VPN_INTERFACE"
if [ -n "$ANYCONNECT_SERVERCERT" ]; then
  set -- "$@" --servercert="$ANYCONNECT_SERVERCERT"
fi

log "Connecting to $ANYCONNECT_SERVER..."
openconnect "$ANYCONNECT_SERVER" "$@" < "$CREDS" 2>&1 \
  | while IFS= read -r line; do echo "$line"; echo "$line" >> "$OC_LOG"; done &
OC_PID=$!

# 4. Wait for tunnel
TUN_IP=""
if wait_for_tunnel; then
  log "VPN connected ($TUN_IP)"
  set_vpn_default_route || true
else
  kill "$OC_PID" 2>/dev/null || true
  die "VPN tunnel unavailable; refusing to start SOCKS without VPN"
fi

# 5. Start SOCKS5 proxy
start_dante

# 6. Start tun2socks
start_tun2socks

# Debug: show final routing table
log "Routing table:"
ip route 2>/dev/null | sed 's/^/  /' >&2
log "Interfaces:"
ip -4 -o addr show 2>/dev/null | sed 's/^/  /' >&2

log "Ready. SOCKS5 on :$SOCKS_PORT | tun2socks on $TUN2SOCKS_INTERFACE"
wait
