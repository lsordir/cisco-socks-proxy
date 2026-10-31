# Cisco AnyConnect SOCKS Proxy

Connect to a Cisco AnyConnect VPN via **OpenConnect** and expose a **SOCKS5** proxy.
All traffic routed through the proxy goes over the VPN tunnel.

Runs inside a single Docker container:

- **OpenConnect** -- connects to the VPN
- **Dante** -- SOCKS5 server on port 1080
- **tun2socks** -- optional TUN device (`tun1` 198.18.0.1/15) for transparent proxying

## Quick start

1. Copy the sample configuration, then edit `env/cisco/.env` locally:

```bash
cp env/cisco/.env.example env/cisco/.env
```

```
ANYCONNECT_SERVERS=mci3.npvip.net,mci2.npvip.net,mci1.npvip.net,mci.npvip.net
VPN_RECONNECT_SECONDS=43200
ANYCONNECT_USER=your_username
ANYCONNECT_PASSWORD=your_password
ANYCONNECT_SERVERCERT=pin-sha256:REPLACE_WITH_VERIFIED_SERVER_PIN
```

Set `ANYCONNECT_SERVERCERT` to a verified pin, or leave it empty to accept
the server certificate automatically. Automatic acceptance does not verify
the server's identity and allows man-in-the-middle attacks.
The real `.env` file is ignored by Git; never commit VPN credentials.

2. Start and test:

```bash
docker compose up -d --build

# VPN takes ~30-60s. Then:
curl -x socks5h://127.0.0.1:1080 https://ifconfig.io

# Or use the helper script (retries up to ~150s):
./scripts/check-socks.sh
```

## Reconnection and server failover

Docker Compose tries `mci3.npvip.net`, `mci2.npvip.net`,
`mci1.npvip.net`, then `mci.npvip.net`, in that order, using the same
credentials. After a connection failure, disconnection, or 12 hours of an
established session, it stops the proxy and VPN and tries the next server.
After the last server it returns to the first. Attempts wait 5 seconds;
establishing a tunnel is limited to 150 seconds. Broken connections are
detected using OpenConnect dead-peer detection and process/interface checks.
Existing proxy connections close during a reconnect.

Edit `env/cisco/.env` to change the server list or reconnect interval:

```dotenv
ANYCONNECT_SERVERS=mci3.npvip.net,mci2.npvip.net,mci1.npvip.net,mci.npvip.net
VPN_RECONNECT_SECONDS=43200
```

Apply changes with `docker compose up -d --force-recreate`.
The entrypoint also accepts a single `ANYCONNECT_SERVER` when no list is set.
A configured certificate pin must be valid for each server in the list.

## Custom port

```bash
CISCO_SOCKS_PORT=8080 docker compose -f docker-compose.yml -f docker/docker-compose.publish.yml up -d
```

## License

MIT
