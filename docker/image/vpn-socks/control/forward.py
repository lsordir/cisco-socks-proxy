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


async def handle(reader, writer):
    upstream = None
    try:
        remote, upstream = await asyncio.wait_for(
            asyncio.open_connection('127.0.0.1', 1080), timeout=5)
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
