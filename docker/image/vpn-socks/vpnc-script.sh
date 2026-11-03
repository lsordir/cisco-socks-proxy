#!/bin/sh
# Minimal vpnc-script for Docker: interface setup only.
# Routing is handled by entrypoint.sh after the tunnel is confirmed up.
export PATH="/usr/sbin:/sbin:/usr/bin:/bin"

case "${reason:-connect}" in
  connect)
    # Apply the negotiated MTU before traffic starts. Leaving tun0 at 1500
    # can blackhole full-sized TLS packets on paths without working PMTUD.
    dev="${TUNDEV:-tun0}"
    if [ -n "$INTERNAL_IP4_ADDRESS" ]; then
      mtu_limit="${VPN_MTU:-1300}"
      mtu="${INTERNAL_IP4_MTU:-$mtu_limit}"
      for value in "$mtu_limit" "$mtu"; do
        case "$value" in ''|*[!0-9]*) echo "Invalid VPN MTU" >&2; exit 1;; esac
        [ "$value" -ge 576 ] && [ "$value" -le 65535 ] || exit 1
      done
      [ "$mtu" -le "$mtu_limit" ] || mtu="$mtu_limit"
      ip addr add "$INTERNAL_IP4_ADDRESS/${INTERNAL_IP4_NETMASK:-32}" dev "$dev" 2>/dev/null || true
      ip link set dev "$dev" mtu "$mtu" || exit 1
      ip link set dev "$dev" up 2>/dev/null || true
    fi
    # Docker's host DNS may synthesize fake IPs that cannot work through the
    # VPN. Resolve remote SOCKS destinations with DNS inside the VPN instead.
    dns_servers="${VPN_DNS:-${INTERNAL_IP4_DNS:-1.1.1.1 8.8.8.8}}"
    for dns in $dns_servers; do
      case "$dns" in *[!0-9.:]*) continue;; esac
      printf 'nameserver %s\n' "$dns"
    done > "${VPN_RESOLV_CONF:-/etc/resolv.conf}"
    ;;
  disconnect)
    ;;
esac
exit 0
