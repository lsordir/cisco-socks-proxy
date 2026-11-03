"""Ensure host fake-IP DNS cannot route the VPN peer into its own tunnel."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RouteTests(unittest.TestCase):
    def check_peer(self, log, dns, expected):
        source = (ROOT / 'docker/image/vpn-socks/entrypoint.sh').read_text()
        function = source[source.index('set_vpn_default_route() {'):source.index('# ── Start Dante')]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'log').write_text(log)
            script = '''
set -e
OC_LOG="$1/log"
TEST_ROUTES="$1/routes"
ORIG_GW=172.19.0.1
ORIG_IF=eth0
VPN_INTERFACE=tun0
ANYCONNECT_SERVER=test.example.com
DNS_RESULT="$2"
log() { :; }
getent() { printf '%s\\n' "$DNS_RESULT"; }
ip() {
  if [ "$1" = route ]; then
    printf '%s\\n' "$*" >> "$TEST_ROUTES"
  else
    echo '12: tun0: <UP>'
  fi
}
''' + function + '\nset_vpn_default_route\n'
            subprocess.run(['sh', '-c', script, 'test', tmp, dns], check=True)
            routes = (path / 'routes').read_text()
            self.assertIn(f'route replace {expected}/32 via 172.19.0.1 dev eth0', routes)
            self.assertLess(routes.index(f'{expected}/32'), routes.index('default dev tun0'))

    def test_uses_actual_ipv4_mapped_peer_instead_of_new_dns_answer(self):
        self.check_peer('Connected to [::ffff:198.18.0.58]:443\n',
                        '::ffff:198.18.0.99 test.example.com', '198.18.0.58')

    def test_normal_ipv4_peer(self):
        self.check_peer('Connected to 209.200.246.238:443\n',
                        '198.18.0.99 test.example.com', '209.200.246.238')

    def test_dns_fallback_normalizes_ipv4_mapped_address(self):
        self.check_peer('', '::ffff:198.18.0.58 test.example.com', '198.18.0.58')

    def test_vpn_dns_is_used_instead_of_host_fake_ip_dns(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolv = Path(tmp) / 'resolv.conf'
            env = dict(os.environ, reason='connect', INTERNAL_IP4_ADDRESS='',
                       INTERNAL_IP4_DNS='9.9.9.9 149.112.112.112',
                       VPN_DNS='', VPN_RESOLV_CONF=str(resolv))
            subprocess.run(['sh', str(ROOT / 'docker/image/vpn-socks/vpnc-script.sh')],
                           env=env, check=True)
            self.assertEqual(resolv.read_text(), 'nameserver 9.9.9.9\nnameserver 149.112.112.112\n')

    def test_explicit_dns_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolv = Path(tmp) / 'resolv.conf'
            env = dict(os.environ, reason='connect', INTERNAL_IP4_ADDRESS='',
                       INTERNAL_IP4_DNS='9.9.9.9', VPN_DNS='1.1.1.1', VPN_RESOLV_CONF=str(resolv))
            subprocess.run(['sh', str(ROOT / 'docker/image/vpn-socks/vpnc-script.sh')],
                           env=env, check=True)
            self.assertEqual(resolv.read_text(), 'nameserver 1.1.1.1\n')


if __name__ == '__main__':
    unittest.main()
