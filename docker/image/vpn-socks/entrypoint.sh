#!/bin/sh
set -e

# ---------------------------------------------------------------------------
#  OpenConnect VPN  +  Dante SOCKS5  +  tun2socks
#  Connects to Cisco AnyConnect VPN then exposes a SOCKS5 proxy on :1080
# ---------------------------------------------------------------------------

# ── Required environment ──────────────────────────────────────────────────
ANYCONNECT_SERVERS="${ANYCONNECT_SERVERS:-${ANYCONNECT_SERVER:-}}"
: "${ANYCONNECT_SERVERS:?ANYCONNECT_SERVERS or ANYCONNECT_SERVER is required}"
VPN_RECONNECT_SECONDS="${VPN_RECONNECT_SECONDS:-43200}"
VPN_RETRY_SECONDS="${VPN_RETRY_SECONDS:-5}"
VPN_CONNECT_TIMEOUT="${VPN_CONNECT_TIMEOUT:-150}"
VPN_CHECK_INTERVAL="${VPN_CHECK_INTERVAL:-60}"
VPN_CHECK_TIMEOUT="${VPN_CHECK_TIMEOUT:-10}"
VPN_CHECK_ATTEMPTS="${VPN_CHECK_ATTEMPTS:-2}"
export VPN_MTU="${VPN_MTU:-1300}"
case "$VPN_MTU" in ''|*[!0-9]*) echo "VPN_MTU must be an integer" >&2; exit 1;; esac
if [ "$VPN_MTU" -lt 576 ] || [ "$VPN_MTU" -gt 65535 ]; then
  echo "VPN_MTU must be between 576 and 65535" >&2
  exit 1
fi
VPN_CHECK_URLS="${VPN_CHECK_URLS:-https://www.gstatic.com/generate_204 https://www.cloudflare.com/cdn-cgi/trace}"
for value in "$VPN_RECONNECT_SECONDS" "$VPN_RETRY_SECONDS" "$VPN_CONNECT_TIMEOUT" \
             "$VPN_CHECK_INTERVAL" "$VPN_CHECK_TIMEOUT" "$VPN_CHECK_ATTEMPTS"; do
  case "$value" in ''|*[!0-9]*|0) echo "VPN timeouts must be positive integer seconds" >&2; exit 1;; esac
done
: "${ANYCONNECT_USER:?ANYCONNECT_USER is required}"
: "${ANYCONNECT_PASSWORD:?ANYCONNECT_PASSWORD is required}"

SOCKS_PORT="${SOCKS_PORT:-1080}"
VPN_INTERFACE="tun0"
TUN2SOCKS_INTERFACE="tun1"
TUN2SOCKS_SUBNET="198.18.0.1/15"
MAX_WAIT_ITERATIONS=$(( (VPN_CONNECT_TIMEOUT + 1) / 2 ))

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

  ip=$(ip -4 -o addr show "$VPN_INTERFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
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
  # Preserve the actual peer OpenConnect used, including IPv4-mapped IPv6
  # addresses returned by fake-IP DNS/proxy software on the host.
  vpn_ip=$(sed -nE 's/.*Connected to (\[::ffff:)?([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+).*/\2/p' "$OC_LOG" | tail -n 1)
  if [ -z "$vpn_ip" ]; then
    vpn_ip=$(getent hosts "$ANYCONNECT_SERVER" 2>/dev/null | awk '
      {sub(/^::ffff:/, "", $1); if ($1 ~ /^[0-9]+\./) {print $1; exit}}')
  fi
  if [ -n "$vpn_ip" ] && [ -n "$ORIG_GW" ]; then
    ip route replace "$vpn_ip/32" via "$ORIG_GW" dev "$ORIG_IF" || return 1
    log "Host route: $vpn_ip via $ORIG_GW dev $ORIG_IF"
  else
    log "Cannot preserve the VPN server route; refusing to change the default route"
    return 1
  fi

  # 2) Replace default route to go through VPN tunnel
  for dev in "$VPN_INTERFACE"; do
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

  ip -4 -o addr show "$VPN_INTERFACE" | grep -q "$dante_ip" || return 1

  sed "s/EXTERNAL_IP_PLACEHOLDER/$dante_ip/" /etc/sockd.conf.template > /tmp/sockd.conf
  # A separate process group lets cleanup stop active Dante workers as well.
  setsid sockd -f /tmp/sockd.conf &
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

# Test DNS, TCP and verified HTTPS through Dante, never the host connection.
# Any successful endpoint is sufficient; retry a failed round before failover.
check_internet() {
  local attempt=0 url status
  while [ "$attempt" -lt "$VPN_CHECK_ATTEMPTS" ]; do
    for url in $VPN_CHECK_URLS; do
      status=$(curl --silent --fail --noproxy "" \
        --proxy "socks5h://127.0.0.1:$SOCKS_PORT" \
        --connect-timeout "$VPN_CHECK_TIMEOUT" --max-time "$VPN_CHECK_TIMEOUT" \
        --output /dev/null --write-out '%{http_code}' "$url") || continue
      case "$status" in
        2??) log "Internet check passed through VPN ($url)"; return 0;;
      esac
    done
    attempt=$((attempt + 1))
    [ "$attempt" -ge "$VPN_CHECK_ATTEMPTS" ] || sleep 2
  done
  log "Internet check failed on $ANYCONNECT_SERVER; switching to the next server"
  return 1
}

# Stop all session processes before restoring routes or trying another server.
stop_session() {
  [ -z "$VPN_READY_FILE" ] || rm -f "$VPN_READY_FILE"
  local pid
  if [ -n "$SOCKD_PID" ]; then
    kill -TERM -- "-$SOCKD_PID" 2>/dev/null || true
  fi
  for pid in "$SOCKD_PID" "$TUN2_PID" "$OC_PID" "$LOG_PID"; do
    [ -z "$pid" ] || kill "$pid" 2>/dev/null || true
  done
  # Give OpenConnect a bounded graceful shutdown, then reap all children.
  local n=0
  while [ -n "$OC_PID" ] && kill -0 "$OC_PID" 2>/dev/null && [ "$n" -lt 5 ]; do
    sleep 1
    n=$((n + 1))
  done
  if [ -n "$SOCKD_PID" ]; then
    kill -KILL -- "-$SOCKD_PID" 2>/dev/null || true
  fi
  for pid in "$SOCKD_PID" "$TUN2_PID" "$OC_PID" "$LOG_PID"; do
    [ -z "$pid" ] || kill -KILL "$pid" 2>/dev/null || true
    [ -z "$pid" ] || wait "$pid" 2>/dev/null || true
  done
  OC_PID="" SOCKD_PID="" TUN2_PID="" LOG_PID=""
  ip link delete "$TUN2SOCKS_INTERFACE" 2>/dev/null || true
  ip link delete "$VPN_INTERFACE" 2>/dev/null || true
  if [ -n "$ORIG_GW" ] && [ -n "$ORIG_IF" ]; then
    ip route replace default via "$ORIG_GW" dev "$ORIG_IF" metric 0
    ip route del default via "$ORIG_GW" dev "$ORIG_IF" metric 200 2>/dev/null || true
  fi
  cat "$RESOLV_BACKUP" > "${VPN_RESOLV_CONF:-/etc/resolv.conf}"
}

cleanup() {
  trap '' TERM INT
  stop_session
  rm -f "$CREDS" "$OC_LOG" "$RESOLV_BACKUP"
}

CREDS=$(mktemp)
OC_LOG=$(mktemp)
RESOLV_BACKUP=$(mktemp)
cat "${VPN_RESOLV_CONF:-/etc/resolv.conf}" > "$RESOLV_BACKUP"
OC_PID="" SOCKD_PID="" TUN2_PID="" LOG_PID=""
trap 'exit 0' TERM INT
trap cleanup EXIT

# Each attempt runs in the same supervisor; a full failed cycle waits before retrying.
# Intentional word splitting accepts comma- or whitespace-separated server names.
SERVER_LIST=$(printf '%s' "$ANYCONNECT_SERVERS" | tr ',' ' ')
set -f
[ -n "$(printf '%s' "$SERVER_LIST" | tr -d '[:space:]')" ] || die "Server list is empty"
while :; do
  for ANYCONNECT_SERVER in $SERVER_LIST; do
    build_credentials "$CREDS"
    : > "$OC_LOG"
    ip tuntap add mode tun dev "$VPN_INTERFACE"
    set -- --timestamp --user="$ANYCONNECT_USER" --passwd-on-stdin \
      --script=/vpnc-script.sh --interface="$VPN_INTERFACE" \
      --mtu="$VPN_MTU" \
      --reconnect-timeout=1 --force-dpd=10
    if [ -n "$ANYCONNECT_SERVERCERT" ]; then
      set -- "$@" --servercert="$ANYCONNECT_SERVERCERT"
    fi

    log "Connecting to $ANYCONNECT_SERVER..."
    openconnect "$ANYCONNECT_SERVER" "$@" < "$CREDS" > "$OC_LOG" 2>&1 &
    OC_PID=$!
    tail -n +1 -f "$OC_LOG" &
    LOG_PID=$!
    TUN_IP=""
    if wait_for_tunnel && set_vpn_default_route; then
      log "VPN connected ($TUN_IP)"
      start_dante
      start_tun2socks
      connected_at=$(date +%s)
      log "Checking internet access through VPN..."
      if check_internet; then
        [ -z "$VPN_READY_FILE" ] || touch "$VPN_READY_FILE"
        last_check=$(date +%s)
        log "Ready. SOCKS5 on :$SOCKS_PORT; reconnect in ${VPN_RECONNECT_SECONDS}s"
        while kill -0 "$OC_PID" 2>/dev/null && \
              kill -0 "$SOCKD_PID" 2>/dev/null && \
              kill -0 "$TUN2_PID" 2>/dev/null; do
          if [ "$(( $(date +%s) - connected_at ))" -ge "$VPN_RECONNECT_SECONDS" ]; then
            log "Scheduled reconnect: switching to the next server"
            break
          fi
          # Check the VPN interface only; tun2socks must never count as a VPN.
          TUN_IP=$(get_tunnel_ip) || true
          if [ -z "$TUN_IP" ]; then
            log "VPN tunnel disappeared; switching to the next server"
            break
          fi
          if [ "$(( $(date +%s) - last_check ))" -ge "$VPN_CHECK_INTERVAL" ]; then
            check_internet || break
            last_check=$(date +%s)
          fi
          sleep 2
        done
      fi
    fi
    log "Ending session on $ANYCONNECT_SERVER; trying next server"
    stop_session
    [ "${VPN_MANAGED:-0}" != 1 ] || exit 0
    sleep "$VPN_RETRY_SECONDS"
  done
done
