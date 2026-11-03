"""Deterministic tests for two-slot selection and five-minute handovers."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'docker/image/vpn-socks/control'))
from controller import ActiveLost, Selector, Slot
from common import write_json


class FakeSlot:
    def __init__(self, name, server=None):
        self.name = self.host = name
        self.server = server
        self.running = bool(server)

    def ready(self):
        return self.running

    def status(self):
        return {'server': self.server, 'running': self.running, 'ready': self.running}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.dict(os.environ, {'ANYCONNECT_SERVERS': 'first,second,third'}, clear=True):
            self.selector = Selector(self.tmp.name)
        self.a = FakeSlot('a', 'first')
        self.b = FakeSlot('b')
        self.selector.slots = [self.a, self.b]
        self.selector.active = self.a
        self.selector.publish = lambda slot: None
        self.actions = []
        self.max_running = 0
        self.scores = {'first': 100, 'second': 200, 'third': 150}

        def stop(slot):
            self.actions.append(('stop', slot.name))
            slot.running = False
            slot.server = None

        def connect(slot, server):
            stop(slot)
            self.assertTrue(self.a.ready(), 'Incumbent was stopped during probing')
            slot.server = server
            slot.running = True
            self.actions.append(('start', slot.name, server))
            self.max_running = max(self.max_running, sum(s.running for s in (self.a, self.b)))
            return True

        self.selector.stop = stop
        self.selector.connect = connect
        self.selector.measure = lambda slot: self.scores.get(slot.server)

    def test_best_server_selected_and_old_tunnel_gets_full_grace(self):
        now = 10000
        with patch('controller.time.time', return_value=now):
            self.selector.scan()
        self.assertIs(self.selector.active, self.b)
        self.assertEqual(self.b.server, 'second')
        self.assertTrue(self.a.ready())
        self.assertEqual(self.max_running, 2)
        with patch('controller.time.time', return_value=now + 299):
            self.selector.tick()
        self.assertTrue(self.a.ready())
        with patch('controller.time.time', return_value=now + 300):
            self.selector.tick()
        self.assertFalse(self.a.ready())
        self.assertTrue(self.b.ready())

    def test_small_improvement_keeps_incumbent_and_stops_spare(self):
        self.scores.update(second=110, third=115)
        self.selector.scan()
        self.assertIs(self.selector.active, self.a)
        self.assertTrue(self.a.ready())
        self.assertFalse(self.b.ready())

    def test_failed_speed_tests_do_not_disconnect_incumbent(self):
        self.scores.update(second=None, third=None)
        self.selector.scan()
        self.assertIs(self.selector.active, self.a)
        self.assertFalse(self.b.ready())

    def test_candidate_is_rechecked_before_promotion(self):
        calls = 0

        def measure(slot):
            nonlocal calls
            if slot is self.b and slot.server == 'second':
                calls += 1
                return 200 if calls == 1 else 80
            return self.scores.get(slot.server)

        self.selector.measure = measure
        self.selector.scan()
        self.assertIs(self.selector.active, self.a)
        self.assertFalse(self.b.ready())

    def test_no_scans_during_overlap_and_before_half_hour(self):
        with patch('controller.time.time', return_value=10000):
            self.selector.promote(self.b)
        self.b.running = True
        self.b.server = 'second'
        self.selector.scan = lambda **_: self.fail('Unscheduled scan')
        with patch('controller.time.time', return_value=10200):
            self.selector.tick()
        with patch('controller.time.time', return_value=10300):
            self.selector.tick()
        with patch('controller.time.time', return_value=11799):
            self.selector.tick()

    def test_twelve_hour_renewal_does_not_choose_a_slower_server(self):
        self.scores.update(second=90, third=80)
        self.selector.scan(force=True)
        self.assertIs(self.selector.active, self.a)
        self.assertFalse(self.b.ready())

    def test_stale_worker_status_is_not_healthy(self):
        slot = Slot(Path(self.tmp.name), 'x', 'host')
        write_json(Path(self.tmp.name) / 'x.command.json', {'revision': 'new'})
        write_json(Path(self.tmp.name) / 'x.status.json',
                   {'ready': True, 'revision': 'new', 'updated': time.time() - 20})
        self.assertFalse(slot.ready())

    def test_old_revision_cannot_mark_a_new_server_ready(self):
        slot = Slot(Path(self.tmp.name), 'x', 'host')
        write_json(Path(self.tmp.name) / 'x.command.json', {'revision': 'new'})
        write_json(Path(self.tmp.name) / 'x.status.json',
                   {'ready': True, 'revision': 'old', 'updated': time.time()})
        self.assertFalse(slot.ready())

    def test_benchmark_uses_socks_and_rejects_short_download(self):
        self.selector.min_bytes = 900000
        # Exercise the real benchmark implementation instead of the fake scores.
        with patch('controller.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '200 12 999999')
            self.assertIsNone(Selector.measure(self.selector, self.a))
            args = run.call_args.args[0]
            self.assertEqual(args[args.index('--proxy') + 1], 'socks5h://a:1080')
            self.assertEqual(args[args.index('--noproxy') + 1], '')

    def test_lost_active_aborts_benchmark(self):
        self.a.running = False
        with self.assertRaises(ActiveLost):
            Selector.measure(self.selector, self.b)

    def test_candidate_failure_during_grace_recovers_to_old_tunnel(self):
        self.b.running = True
        self.b.server = 'second'
        self.selector.promote(self.b)
        self.b.running = False
        self.selector.tick()
        self.assertIs(self.selector.active, self.a)
        self.assertTrue(self.a.ready())

    def test_probe_becoming_ready_during_recovery_is_published_immediately(self):
        self.a.running = False
        self.b.server = 'third'
        # Not ready at recover() entry; ready as soon as the new attempt waits.
        with patch.object(self.b, 'ready', side_effect=[False, True]):
            self.selector.connect = lambda slot, server: Selector.wait(
                self.selector, slot, 'pending', True)
            self.selector.recover()
        self.assertIs(self.selector.active, self.b)
        self.assertIn(('stop', 'a'), self.actions)
        self.assertNotIn(('stop', 'b'), self.actions)

    def test_publishing_does_not_recursively_promote_another_ready_slot(self):
        self.selector.active = None
        self.b.running = True
        with patch.object(self.a, 'status', return_value={
                'revision': 'publish', 'ready': True, 'published': True}):
            self.assertTrue(Selector.wait(self.selector, self.a, 'publish', True, published=True))
        self.assertIsNone(self.selector.active)

    def test_benchmark_uses_two_downloads_and_median(self):
        with patch('controller.subprocess.run') as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, '200 1000000 100'),
                subprocess.CompletedProcess([], 0, '200 1000000 200'),
            ]
            self.assertEqual(Selector.measure(self.selector, self.a), 150)
            self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
