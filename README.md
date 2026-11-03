# Cisco AnyConnect SOCKS Proxy

Two isolated VPN workers expose SOCKS5 on **127.0.0.1:1080** (slot A) and
**127.0.0.1:1081** (slot B). A controller keeps a working VPN connected while
the spare tests other servers. Supply both SOCKS endpoints to your own load
balancer with health checks. The active slot alternates; neither port is
permanently primary. There is no built-in load balancer.

## Quick start

```bash
cp env/cisco/.env.example env/cisco/.env
# Edit credentials and server list locally, then:
docker compose up -d --build
docker compose logs -f vpn-selector
```

```dotenv
ANYCONNECT_SERVERS=mci3.npvip.net,mci2.npvip.net,mci1.npvip.net,mci.npvip.net
ANYCONNECT_USER=your_username
ANYCONNECT_PASSWORD=your_password
ANYCONNECT_SERVERCERT=
VPN_SCAN_INTERVAL=1800
VPN_OVERLAP_SECONDS=300
VPN_SWITCH_PERCENT=25
```

The private `env/cisco/.env` is ignored by Git. The public sample contains
placeholders only. An empty `ANYCONNECT_SERVERCERT` automatically accepts the
VPN certificate without verifying server identity. For verification, configure
a trusted pin valid for all selected servers.

## Server selection and handover

- At startup, connect the first server that passes the internet check. Keep it
  connected while the second worker tests all other configured servers in turn.
- Repeat the scan every 30 minutes. At most **two VPN sessions** exist at once;
  testing never disconnects the current working tunnel. Each worker has its own
  Docker network namespace and routing table.
- Measure download throughput using two verified HTTPS downloads per benchmark
  through the relevant SOCKS proxy. Reject failed, partial, or tiny downloads;
  compare median bytes/second. The default resource is a 1 MB Cloudflare download.
- Reconnect the best candidate if necessary, then benchmark both it and the
  incumbent again. Switch only if the candidate is at least **25% faster**.
  A failed benchmark preserves the working VPN; small differences do not switch.
- When a winner is verified, keep **both tunnels up for five minutes**. After
  that, stop the old tunnel. No new scans occur during this overlap. If no
  improvement is found, stop the spare and keep only the incumbent.
- After 12 hours, attempt renewal at the next scan; an equally fast candidate
  can replace the incumbent. A slower candidate is rejected and renewal is
  deferred to the next scan rather than interrupting a faster working tunnel.
- If the incumbent fails, use an already healthy spare, or try servers in order
  starting after the failed server. A complete VPN/server failure can interrupt
  connections; it cannot be made seamless by retaining a dead tunnel.

The five-minute timer begins **after** verification. The worker keeps speed-test traffic on an internal SOCKS port. Unselected
candidates are not exposed on the public host ports. Your load balancer decides
which published port to use for new traffic. Existing TCP sessions on the old VPN are terminated
when its five-minute overlap ends; they cannot migrate to another VPN. Stop
sending new sessions to the retiring slot if you want them to drain first.

Throughput is a sample of a particular download path, not a guarantee of speed
to every destination. Benchmarks consume data and briefly compete with real
traffic. Change the URL and minimum size together when using your own resource.

## Configuration

All VPN and selection settings are read from `env/cisco/.env`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `ANYCONNECT_SERVERS` | See sample | Comma/space-separated server list |
| `VPN_SCAN_INTERVAL` | `1800` | Seconds between selection scans |
| `VPN_OVERLAP_SECONDS` | `300` | Keep old and new VPNs during handover |
| `VPN_SWITCH_PERCENT` | `25` | Minimum improvement to replace incumbent |
| `VPN_SPEED_SAMPLES` | `2` | Downloads per benchmark; minimum 2 |
| `VPN_SPEED_TIMEOUT` | `20` | Maximum seconds per download |
| `VPN_SPEED_URL` | Cloudflare 1 MB resource | HTTPS speed-test resource |
| `VPN_SPEED_MIN_BYTES` | `900000` | Reject smaller responses |
| `VPN_RECONNECT_SECONDS` | `43200` | Attempt renewal after this session age |
| `VPN_CONNECT_TIMEOUT` | `150` | Maximum wait for the VPN interface |
| `VPN_CHECK_INTERVAL` | `60` | Internet health-check interval |
| `VPN_CHECK_TIMEOUT` | `10` | Maximum seconds per health request |
| `VPN_CHECK_ATTEMPTS` | `2` | Failed health rounds before disconnection |
| `VPN_CHECK_URLS` | Gstatic and Cloudflare | Space-separated HTTPS health URLs |
| `VPN_DNS` | VPN-provided DNS, then `1.1.1.1 8.8.8.8` | DNS resolvers used inside the tunnel |

The internet check uses remote DNS and HTTPS through SOCKS. One successful
HTTP 2xx endpoint is sufficient. Both failing in two rounds rejects that server.
Workers preserve the actual VPN peer route and use DNS inside the VPN, avoiding
host proxy fake-IP addresses for remote SOCKS destinations. Original container
DNS is restored before connecting the next server.
Workers do not independently rotate servers in dual-slot mode; the controller
owns selection so the account limit remains two connections.

Apply config changes with `docker compose up -d --force-recreate`.
Recreating workers briefly disconnects their tunnels. Restarting just the
controller leaves workers connected and preserves the overlap deadline.
The named `vpn-control` volume stores commands and selection status, never VPN
passwords. Do not share that volume with unrelated containers.

## Status and testing

```bash
docker compose logs -f vpn-selector
# Current active slot, server, retiring slot and deadline:
docker compose exec -T vpn-selector cat /control/selection.json
# Test whichever port is currently running:
curl --proxy socks5h://127.0.0.1:1080 --max-time 15 https://ifconfig.io/ip
curl --proxy socks5h://127.0.0.1:1081 --max-time 15 https://ifconfig.io/ip
./scripts/check-socks.sh

# Local deterministic tests; no VPN credentials required:
python3 -m unittest discover -s tests -v
```

Only the active port normally responds. During the five-minute handover both may
respond; scanning alone does not publish the spare. Until a server passes startup health checks, neither may be available.

## Custom host ports

Set shell variables when starting Compose (these are host-port settings, not
container VPN settings):

```bash
CISCO_SOCKS_PORT=8080 CISCO_SOCKS_PORT_B=8081 docker compose up -d
```

The standalone image still supports the original single-worker entrypoint and
`ANYCONNECT_SERVER` fallback. In that mode, rotation closes existing sessions.

## License

MIT
