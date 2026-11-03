"""Exercise real worker command/ACK handling with a harmless fake VPN process."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

CONTROL = Path(__file__).resolve().parents[1] / 'docker/image/vpn-socks/control'
sys.path.insert(0, str(CONTROL))
from controller import Slot


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        session = self.root / 'session.py'
        session.write_text('''import os, pathlib, time
pathlib.Path(os.environ['TEST_PID']).write_text(str(os.getpid()))
pathlib.Path(os.environ['VPN_READY_FILE']).touch()
time.sleep(300)
''')
        source = (CONTROL / 'worker.py').read_text()
        forward = self.root / 'forward.py'
        forward.write_text('''import os, pathlib, time
pathlib.Path(os.environ['VPN_PUBLISH_FILE']).touch()
time.sleep(300)
''')
        source = source.replace("'/app/forward.py'", repr(str(forward)))
        source = source.replace("Path('/tmp/vpn-published')", "Path(" + repr(str(self.root / 'published')) + ")")
        source = source.replace("Path('/tmp/vpn-ready')", repr(str(self.root / 'ready')))
        # Preserve Path behavior while isolating the worker's /tmp file in tests.
        source = source.replace("ready = " + repr(str(self.root / 'ready')),
                                "ready = Path(" + repr(str(self.root / 'ready')) + ")")
        source = source.replace("['/entrypoint.sh']", repr([sys.executable, str(session)]))
        script = self.root / 'worker.py'
        script.write_text(source)
        env = dict(os.environ, PYTHONPATH=str(CONTROL), VPN_SLOT='a',
                   VPN_CONTROL_DIR=str(self.root), TEST_PID=str(self.root / 'pid'))
        self.proc = subprocess.Popen([sys.executable, str(script)], env=env,
                                     stdout=subprocess.DEVNULL)
        self.addCleanup(self.stop_worker)
        self.slot = Slot(self.root, 'a', 'unused')

    def stop_worker(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)

    def wait_for(self, predicate):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if predicate():
                return
            self.assertIsNone(self.proc.poll(), 'Worker unexpectedly exited')
            time.sleep(0.05)
        self.fail('Worker did not acknowledge command')

    def test_stop_acknowledges_only_after_vpn_process_is_gone(self):
        self.slot.request('first')
        self.wait_for(self.slot.ready)
        pid = int((self.root / 'pid').read_text())
        revision = self.slot.request(None)
        self.wait_for(lambda: self.slot.status().get('revision') == revision
                      and not self.slot.status().get('running'))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse((self.root / 'ready').exists())

    def test_failed_vpn_does_not_restart_without_controller_command(self):
        self.slot.request('first')
        self.wait_for(self.slot.ready)
        pid = int((self.root / 'pid').read_text())
        os.kill(pid, signal.SIGTERM)
        self.wait_for(lambda: not self.slot.status().get('running'))
        self.assertFalse(self.slot.ready())
        revision = self.slot.request('second')
        self.wait_for(lambda: self.slot.ready() and self.slot.status().get('revision') == revision)
        self.assertNotEqual(int((self.root / 'pid').read_text()), pid)
        self.assertEqual(self.slot.status()['server'], 'second')

    def test_publishing_selected_slot_does_not_restart_vpn(self):
        self.slot.request('first')
        self.wait_for(self.slot.ready)
        pid = int((self.root / 'pid').read_text())
        self.assertFalse(self.slot.status().get('published'))
        revision = self.slot.request('first', serve=True)
        self.wait_for(lambda: self.slot.status().get('revision') == revision
                      and self.slot.status().get('published'))
        self.assertEqual(int((self.root / 'pid').read_text()), pid)
        self.slot.request('first', serve=False)
        self.wait_for(lambda: not self.slot.status().get('published'))
        self.assertTrue(self.slot.ready())
        self.assertEqual(int((self.root / 'pid').read_text()), pid)


if __name__ == '__main__':
    unittest.main()
