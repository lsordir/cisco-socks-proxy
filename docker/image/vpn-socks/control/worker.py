"""Run at most one VPN session in this worker's isolated network namespace."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from common import read_json, write_json


def main():
    root = Path(os.environ.get('VPN_CONTROL_DIR', '/control'))
    root.mkdir(parents=True, exist_ok=True)
    slot = os.environ['VPN_SLOT']
    ready = Path('/tmp/vpn-ready')
    published = Path('/tmp/vpn-published')
    proc = None
    forward = None
    serving = False
    revision = None
    server = None
    stopping = False

    def terminate(*_):
        nonlocal stopping
        stopping = True

    def stop_forward():
        nonlocal forward
        published.unlink(missing_ok=True)
        if forward is not None:
            if forward.poll() is None:
                forward.terminate()
                forward.wait(timeout=5)
            forward = None

    def stop():
        nonlocal proc
        stop_forward()
        ready.unlink(missing_ok=True)
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    # Do not start another VPN with unconfirmed cleanup. Let
                    # Docker tear down this worker's entire network namespace.
                    raise RuntimeError('VPN cleanup timed out; restarting worker')
            proc = None

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        while not stopping:
            command = read_json(root / f'{slot}.command.json')
            if command.get('revision') != revision:
                new_server = command.get('server')
                if new_server != server or proc is None or proc.poll() is not None:
                    stop()
                    if new_server:
                        env = dict(os.environ, ANYCONNECT_SERVERS=new_server,
                                   ANYCONNECT_SERVER=new_server, VPN_MANAGED='1',
                                   SOCKS_PORT='1080', VPN_READY_FILE=str(ready),
                                   VPN_RECONNECT_SECONDS='2147483647')
                        proc = subprocess.Popen(['/entrypoint.sh'], env=env)
                revision = command.get('revision')
                server = new_server
                serving = bool(command.get('serve'))
            running = proc is not None and proc.poll() is None
            healthy = running and ready.exists()
            if not serving or not healthy:
                stop_forward()
            elif forward is None or forward.poll() is not None:
                published.unlink(missing_ok=True)
                forward = subprocess.Popen([sys.executable, '/app/forward.py'],
                                           env=dict(os.environ, VPN_PUBLISH_FILE=str(published)))
            write_json(root / f'{slot}.status.json', {
                'revision': revision, 'server': server, 'running': running,
                'ready': healthy, 'published': healthy and serving and
                forward is not None and forward.poll() is None and published.exists(),
                'updated': time.time(),
            })
            time.sleep(1)
    finally:
        stop()
        write_json(root / f'{slot}.status.json', {
            'revision': revision, 'server': None, 'running': False,
            'ready': False, 'updated': time.time(),
        })


if __name__ == '__main__':
    main()
