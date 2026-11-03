#!/bin/sh
set -e
cd "$(dirname "$0")/.."

docker compose up -d
echo "Waiting for either SOCKS slot (1080 or 1081)..."
i=0
while [ "$i" -lt 30 ]; do
  for port in "${CISCO_SOCKS_PORT:-1080}" "${CISCO_SOCKS_PORT_B:-1081}"; do
    if result=$(curl --fail --silent --noproxy "" --proxy "socks5h://127.0.0.1:$port" \
        --connect-timeout 3 --max-time 8 https://ifconfig.io/ip); then
      echo "SOCKS on $port is working. VPN exit IP: $result"
      exit 0
    fi
  done
  sleep 5
  i=$((i + 1))
done
echo "No SOCKS slot is responding. Check: docker compose logs vpn-selector"
exit 1
