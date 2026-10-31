#!/bin/sh
set -e
cd "$(dirname "$0")/.."

echo "Starting vpn-socks..."
docker compose up -d vpn-socks

echo "Waiting for SOCKS on 127.0.0.1:1080 (VPN may take 30-60s)..."
i=0
while [ $i -lt 30 ]; do
  if curl -s -x socks5h://127.0.0.1:1080 --connect-timeout 3 -o /dev/null https://ifconfig.io 2>/dev/null; then
    echo "SOCKS OK. VPN exit IP:"
    curl -s -x socks5h://127.0.0.1:1080 https://ifconfig.io
    echo ""
    exit 0
  fi
  sleep 5
  i=$((i + 1))
done

echo "SOCKS not responding. Check: docker compose logs -f vpn-socks"
exit 1
