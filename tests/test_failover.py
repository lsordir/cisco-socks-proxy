"""Run the real shell supervisor with fake VPN/network commands; no credentials needed."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE = '''#!/usr/bin/env python3
import os, pathlib, sys, time
state = pathlib.Path(os.environ['TEST_STATE'])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'ip':
    if args == ['route']:
        print('default via 172.19.0.1 dev eth0')
    elif args[:4] == ['-4', '-o', 'addr', 'show']:
        if (state / 'tunnel').exists():
            print('12: tun0 inet 192.168.10.49/21 scope global tun0')
    elif args[:3] == ['link', 'show', 'tun0']:
        print('12: tun0: <UP>')
    elif args[:3] == ['link', 'delete', 'tun0']:
        (state / 'tunnel').unlink(missing_ok=True)
elif name == 'getent':
    print('203.0.113.1 test-server')
elif name == 'openconnect':
    with (state / 'attempts').open('a') as f:
        f.write(args[0] + '\\n')
    if args[0] == 'bad':
        sys.exit(1)
    if args[0] == 'hang':
        time.sleep(300)
        sys.exit(1)
    (state / 'vpn-pid').write_text(str(os.getpid()))
    (state / 'server').write_text(args[0])
    (state / 'tunnel').touch()
    time.sleep(300)
elif name == 'curl':
    assert args[args.index('--proxy') + 1] == 'socks5h://127.0.0.1:1080'
    assert args[args.index('--noproxy') + 1] == ''
    with (state / 'probes').open('a') as f:
        f.write(args[-1] + '\\n')
    if ((state / 'server').read_text() == 'offline'
            or (state / 'outage').exists()
            or args[-1] == 'https://unavailable.test'):
        sys.exit(28)
    print('204', end='')
'''


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        (self.state / 'resolv.conf').write_text('nameserver 127.0.0.11\n')
        for name in ('ip', 'getent', 'openconnect', 'curl'):
            p = self.state / name
            p.write_text(FAKE)
            p.chmod(0o755)
        script = (ROOT / 'docker/image/vpn-socks/entrypoint.sh').read_text()
        # Keep real authentication setup, tunnel checks, routing, rotation and cleanup.
        # Replace only proxy daemons so tests need neither root nor a live network.
        script = script.replace('CREDS=$(mktemp)', '''
start_dante() { sleep 300 & SOCKD_PID=$!; }
start_tun2socks() { sleep 300 & TUN2_PID=$!; }
CREDS=$(mktemp)''')
        self.script = self.state / 'entrypoint.sh'
        self.script.write_text(script)

    def start(self, servers, interval, urls='https://primary.test https://backup.test'):
        env = dict(os.environ, PATH=str(self.state) + ':' + os.environ['PATH'],
                   TEST_STATE=str(self.state), ANYCONNECT_SERVERS=servers,
                   VPN_RESOLV_CONF=str(self.state / 'resolv.conf'),
                   ANYCONNECT_USER='dummy-user', ANYCONNECT_PASSWORD='dummy-password',
                   ANYCONNECT_SERVERCERT='', VPN_RECONNECT_SECONDS=str(interval),
                   VPN_RETRY_SECONDS='1', VPN_CONNECT_TIMEOUT='10',
                   VPN_CHECK_INTERVAL='2', VPN_CHECK_TIMEOUT='1',
                   VPN_CHECK_ATTEMPTS='2', VPN_CHECK_URLS=urls)
        self.output = (self.state / 'output').open('w')
        self.addCleanup(self.output.close)
        self.proc = subprocess.Popen(['sh', str(self.script)], env=env,
                                     stdout=self.output, stderr=self.output,
                                     start_new_session=True)
        self.addCleanup(self.stop)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()

    def wait_for(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        self.fail((self.state / 'output').read_text())

    def attempts(self):
        p = self.state / 'attempts'
        return p.read_text().splitlines() if p.exists() else []

    def test_failure_then_scheduled_rotation_and_wraparound(self):
        self.start('bad,second,third', interval=2)
        self.wait_for(lambda: len(self.attempts()) >= 5, timeout=30)
        self.assertEqual(self.attempts()[:5], ['bad', 'second', 'third', 'bad', 'second'])
        self.stop()
        self.assertFalse((self.state / 'tunnel').exists())
        self.assertEqual((self.state / 'resolv.conf').read_text(), 'nameserver 127.0.0.11\n')

    def test_vpn_process_death_moves_to_next_server(self):
        self.start('first second', interval=60)
        self.wait_for(lambda: 'Ready.' in (self.state / 'output').read_text())
        pid = int((self.state / 'vpn-pid').read_text())
        os.kill(pid, signal.SIGTERM)
        self.wait_for(lambda: len(self.attempts()) >= 2)
        self.assertEqual(self.attempts()[:2], ['first', 'second'])

    def test_connection_timeout_moves_to_next_server(self):
        self.start('hang second', interval=60)
        self.wait_for(lambda: len(self.attempts()) >= 2, timeout=30)
        self.assertEqual(self.attempts()[:2], ['hang', 'second'])

    def test_tunnel_disappearance_moves_to_next_server(self):
        self.start('first second', interval=60)
        self.wait_for(lambda: 'Ready.' in (self.state / 'output').read_text())
        (self.state / 'tunnel').unlink()
        self.wait_for(lambda: len(self.attempts()) >= 2)
        self.assertEqual(self.attempts()[:2], ['first', 'second'])

    def test_connected_server_without_internet_is_skipped(self):
        self.start('offline online', interval=60)
        self.wait_for(lambda: 'Ready.' in (self.state / 'output').read_text())
        self.assertEqual(self.attempts()[:2], ['offline', 'online'])
        self.assertIn('Internet check failed on offline', (self.state / 'output').read_text())

    def test_periodic_internet_outage_moves_to_next_server(self):
        self.start('first second', interval=60)
        self.wait_for(lambda: 'Ready.' in (self.state / 'output').read_text())
        (self.state / 'outage').touch()
        self.wait_for(lambda: len(self.attempts()) >= 2)
        self.assertEqual(self.attempts()[:2], ['first', 'second'])

    def test_one_unavailable_endpoint_does_not_trigger_failover(self):
        self.start('first second', interval=60,
                   urls='https://unavailable.test https://backup.test')
        self.wait_for(lambda: 'Ready.' in (self.state / 'output').read_text())
        self.assertEqual(self.attempts(), ['first'])
        self.assertEqual((self.state / 'probes').read_text().splitlines()[:2],
                         ['https://unavailable.test', 'https://backup.test'])


if __name__ == '__main__':
    unittest.main()
