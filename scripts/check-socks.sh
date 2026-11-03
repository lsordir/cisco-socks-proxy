#!/bin/sh
set -e
cd "$(dirname "$0")/.."

docker compose up -d
echo "Waiting for SOCKS gateway on ${CISCO_SOCKS_PORT:-1080}..."
i=0
while [ "$i" -lt 30 ]; do
  if result=$(curl --fail --silent --noproxy "" --proxy "socks5h://127.0.0.1:${CISCO_SOCKS_PORT:-1080}" \
      --connect-timeout 3 --max-time 8 https://ifconfig.io/ip); then
    echo "SOCKS gateway is working. VPN exit IP: $result"
    exit 0
  fi
  sleep 5
  i=$((i + 1))
done
echo "SOCKS gateway is not responding. Check: docker compose logs vpn-selector"
exit 1
