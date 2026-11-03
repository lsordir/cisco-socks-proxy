"""Two-slot VPN selection. Benchmark the spare while keeping the incumbent alive."""
import logging
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import time
import uuid

from common import read_json, write_json


LOG = logging.getLogger('vpn-selector')


def positive(name, default, minimum=1):
    value = int(os.environ.get(name, default))
    if value < minimum:
        raise ValueError(f'{name} must be >= {minimum}')
    return value


class ActiveLost(Exception):
    pass


class Slot:
    def __init__(self, root, name, host):
        self.root, self.name, self.host = root, name, host

    def status(self):
        data = read_json(self.root / f'{self.name}.status.json')
        if time.time() - data.get('updated', 0) > 10:
            return {}
        return data

    def ready(self):
        state = self.status()
        command = read_json(self.root / f'{self.name}.command.json')
        return bool(state.get('ready') and state.get('revision') == command.get('revision'))

    def request(self, server, serve=False):
        revision = uuid.uuid4().hex
        write_json(self.root / f'{self.name}.command.json',
                   {'revision': revision, 'server': server, 'serve': serve})
        return revision


class Selector:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get('VPN_CONTROL_DIR', '/control'))
        self.root.mkdir(parents=True, exist_ok=True)
        self.slots = [Slot(self.root, 'a', 'vpn-socks'), Slot(self.root, 'b', 'vpn-socks-b')]
        self.servers = list(dict.fromkeys(os.environ.get('ANYCONNECT_SERVERS',
                            os.environ.get('ANYCONNECT_SERVER', '')).replace(',', ' ').split()))
        if not self.servers:
            raise ValueError('ANYCONNECT_SERVERS is required')
        self.interval = positive('VPN_SCAN_INTERVAL', 1800)
        self.overlap = positive('VPN_OVERLAP_SECONDS', 300)
        self.margin = positive('VPN_SWITCH_PERCENT', 25)
        self.samples = positive('VPN_SPEED_SAMPLES', 2, minimum=2)
        self.timeout = positive('VPN_SPEED_TIMEOUT', 45)
        self.min_bytes = positive('VPN_SPEED_MIN_BYTES', 900000)
        self.max_age = positive('VPN_RECONNECT_SECONDS', 43200)
        # Startup health checks may take up to 2 * 2 * 10 seconds by default.
        self.connect_timeout = positive('VPN_CONNECT_TIMEOUT', 150) + (
            positive('VPN_CHECK_ATTEMPTS', 2) * positive('VPN_CHECK_TIMEOUT', 10) *
            max(1, len(os.environ.get('VPN_CHECK_URLS', 'one two').split())) + 20)
        self.url = os.environ.get('VPN_SPEED_URL',
                                  'https://speed.cloudflare.com/__down?bytes=1000000')
        if not self.url.startswith('https://'):
            raise ValueError('VPN_SPEED_URL must use https://')
        self.active = None
        self.retiring = None
        self.retire_at = 0
        self.selected_at = time.time()
        self.next_scan = 0
        self.running = True
        saved = read_json(self.root / 'selection.json')
        for slot in self.slots:
            if slot.name == saved.get('active') and slot.ready():
                self.active = slot
                self.selected_at = saved.get('selected_at', time.time())
                self.next_scan = saved.get('next_scan', 0)
        if self.active:
            for slot in self.slots:
                if slot is not self.active and slot.status().get('running'):
                    self.retiring = slot
                    self.retire_at = saved.get('retire_at', time.time() + self.overlap)

    def save(self):
        write_json(self.root / 'selection.json', {
            'active': self.active.name if self.active else None,
            'server': self.active.status().get('server') if self.active else None,
            'retiring': self.retiring.name if self.retiring else None,
            'retire_at': self.retire_at, 'selected_at': self.selected_at,
            'next_scan': self.next_scan, 'updated': time.time(),
        })

    def guard(self):
        if not self.running:
            raise InterruptedError()
        if self.active and not self.active.ready():
            raise ActiveLost()

    def wait(self, slot, revision, starting, published=False):
        end = time.monotonic() + (self.connect_timeout if starting else 25)
        while time.monotonic() < end:
            self.guard()
            state = slot.status()
            if state.get('revision') == revision:
                if not starting and not state.get('running'):
                    return True
                if starting and state.get('ready') and (not published or state.get('published')):
                    return True
                if starting and not state.get('running'):
                    return False
            time.sleep(1)
        return False

    def stop(self, slot):
        revision = slot.request(None)
        if not self.wait(slot, revision, False):
            # Never reuse a slot or exceed the account limit without an ACK.
            raise RuntimeError(f'Worker {slot.name} did not confirm VPN shutdown')

    def connect(self, slot, server):
        self.stop(slot)
        LOG.info('Testing %s in slot %s; active slot stays connected', server, slot.name)
        revision = slot.request(server)
        if self.wait(slot, revision, True):
            return True
        LOG.warning('%s did not pass startup health checks', server)
        self.stop(slot)
        return False

    def measure(self, slot):
        speeds = []
        for _ in range(self.samples):
            self.guard()
            if not slot.ready():
                return None
            command = ['curl', '--silent', '--fail', '--noproxy', '',
                       '--proxy', f'socks5h://{slot.host}:1080', '--output', '/dev/null',
                       '--connect-timeout', str(self.timeout), '--max-time', str(self.timeout),
                       '--write-out', '%{http_code} %{size_download} %{speed_download}', self.url]
            try:
                result = subprocess.run(command, capture_output=True, text=True,
                                        timeout=self.timeout + 2, check=True)
                code, size, speed = result.stdout.split()
                speed = float(speed)
                if not code.startswith('2') or float(size) < self.min_bytes or not math.isfinite(speed) or speed <= 0:
                    return None
                speeds.append(speed)
            except (subprocess.SubprocessError, ValueError):
                return None
        self.guard()
        if not slot.ready():
            return None
        score = statistics.median(speeds)
        LOG.info('Slot %s (%s): median %.2f Mbps', slot.name,
                 slot.status().get('server'), score * 8 / 1_000_000)
        return score

    def promote(self, candidate):
        self.publish(candidate)
        self.retiring = self.active
        self.active = candidate
        self.selected_at = time.time()
        self.next_scan = self.selected_at + self.interval
        self.retire_at = self.selected_at + self.overlap if self.retiring else 0
        LOG.info('Active slot %s (%s); old slot stays up for %ss', candidate.name,
                 candidate.status().get('server'), self.overlap if self.retiring else 0)
        self.save()

    def publish(self, slot):
        revision = slot.request(slot.status()['server'], serve=True)
        if not self.wait(slot, revision, True, published=True):
            raise RuntimeError('Selected worker did not publish its SOCKS endpoint')

    def recover(self):
        previous = self.active.status().get('server') if self.active else None
        self.active = None
        self.retiring = None
        for slot in self.slots:
            if slot.ready():
                self.promote(slot)
                self.next_scan = time.time()
                return
        # No working tunnel: try the configured order until one passes health.
        servers = self.servers
        if previous in servers:
            index = servers.index(previous) + 1
            servers = servers[index:] + servers[:index]
        for server in servers:
            if self.connect(self.slots[0], server):
                self.promote(self.slots[0])
                self.next_scan = time.time()
                return
        LOG.warning('No VPN has internet access; retrying shortly')
        time.sleep(5)

    def scan(self, force=False):
        incumbent = self.active
        spare = next(slot for slot in self.slots if slot is not incumbent)
        current_server = incumbent.status().get('server')
        best_server, best_score = None, 0
        for server in self.servers:
            if server == current_server and len(self.servers) > 1:
                continue
            if self.connect(spare, server):
                score = self.measure(spare)
                if score is not None and score > best_score:
                    best_server, best_score = server, score
        if best_server is not None:
            if spare.status().get('server') != best_server or not spare.ready():
                if not self.connect(spare, best_server):
                    self.stop(spare)
                    return
            # Recheck the winner and incumbent after scanning; cached scores
            # alone must never cause a switch under changing network load.
            candidate_score = self.measure(spare)
            current_score = self.measure(incumbent)
            if candidate_score is not None and (
                    current_score is None or candidate_score >= current_score *
                    (1 if force else (1 + self.margin / 100))):
                self.promote(spare)
                return
        LOG.info('Keeping current VPN; no verified improvement of %s%%', self.margin)
        self.stop(spare)

    def tick(self):
        if not self.active or not self.active.ready():
            self.recover()
            self.save()
            return
        if self.retiring:
            if not self.retiring.status().get('running') or time.time() >= self.retire_at:
                self.stop(self.retiring)
                self.retiring = None
                self.retire_at = 0
                self.save()
            return
        if time.time() >= self.next_scan:
            self.next_scan = time.time() + self.interval
            self.save()
            self.scan(force=time.time() - self.selected_at >= self.max_age)
            self.save()

    def run(self):
        while self.running:
            try:
                self.tick()
            except ActiveLost:
                LOG.warning('Active VPN lost internet; recovering without a third tunnel')
            except InterruptedError:
                break
            # Leave healthy workers connected when the controller restarts.
            except Exception:
                LOG.exception('Selection failed; preserving current VPN')
            time.sleep(2)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    selector = Selector()
    def shutdown(*_):
        selector.running = False
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    selector.run()


if __name__ == '__main__':
    main()
