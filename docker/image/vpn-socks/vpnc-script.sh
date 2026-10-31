#!/bin/sh
# Minimal vpnc-script for Docker: interface setup only.
# Routing is handled by entrypoint.sh after the tunnel is confirmed up.
export PATH="/usr/sbin:/sbin:/usr/bin:/bin"

case "${reason:-connect}" in
  connect)
    # OpenConnect sets INTERNAL_IP4_ADDRESS and TUNDEV; just bring the interface up.
    dev="${TUNDEV:-tun0}"
    if [ -n "$INTERNAL_IP4_ADDRESS" ]; then
      ip addr add "$INTERNAL_IP4_ADDRESS/${INTERNAL_IP4_NETMASK:-32}" dev "$dev" 2>/dev/null || true
      ip link set dev "$dev" up 2>/dev/null || true
    fi
    ;;
  disconnect)
    ;;
esac
exit 0
