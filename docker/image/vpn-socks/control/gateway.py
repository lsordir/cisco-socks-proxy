"""One stable public SOCKS entrypoint, with selection and draining internally."""
import asyncio
import os
from pathlib import Path
import time

from common import read_json
from forward import handle, run_server


HOSTS = {'a': 'vpn-socks', 'b': 'vpn-socks-b'}


def targets(root, now=None):
    now = time.time() if now is None else now
    selection = read_json(root / 'selection.json')
    names = [selection.get('active')]
    # The retiring tunnel is a fallback only during the overlap. Probing
    # candidates must never receive traffic from the public entrypoint.
    if selection.get('retire_at', 0) > now:
        names.append(selection.get('retiring'))
    result = []
    for name in names:
        if name not in HOSTS:
            continue
        state = read_json(root / f'{name}.status.json')
        command = read_json(root / f'{name}.command.json')
        if (state.get('ready') and state.get('published') and command.get('serve')
                and state.get('revision') == command.get('revision')
                and 0 <= now - state.get('updated', 0) <= 10):
            target = (HOSTS[name], 1082)
            if target not in result:
                result.append(target)
    return result


async def main():
    root = Path(os.environ.get('VPN_CONTROL_DIR', '/control'))

    async def route(reader, writer):
        await handle(reader, writer, targets(root))

    await run_server(route, 1080)


if __name__ == '__main__':
    asyncio.run(main())
