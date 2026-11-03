"""Expose a selected worker's SOCKS port; unselected probes stay internal."""
import asyncio
import os
from pathlib import Path


async def relay(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


async def handle(reader, writer, targets=None):
    upstream = None
    try:
        # Choose once per TCP connection. Never move an established stream or
        # replay SOCKS/application bytes onto another VPN after a failure.
        for host, port in targets if targets is not None else [('127.0.0.1', 1080)]:
            try:
                remote, upstream = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=5)
                break
            except (OSError, asyncio.TimeoutError):
                continue
        if upstream is None:
            return
        await asyncio.gather(relay(reader, upstream), relay(remote, writer))
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        for stream in (writer, upstream):
            if stream is not None:
                stream.close()


async def main():
    server = await asyncio.start_server(handle, '0.0.0.0', 1082)
    Path(os.environ['VPN_PUBLISH_FILE']).touch()
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
